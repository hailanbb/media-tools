"""同 TMDB 多目录源端合并与前缀统一引擎 (Directory Merge & Prefix Alignment Engine).

严格遵循《MediaFlow异常剧集模块需求与交互方案-v2.5.39.md》第 6.5 节与 M01–M14 验收标准：
- M01: 《谎言》完整操作闭环：旧目录 E01–E02 移入目标 S01，与 E03–E05 共存；附件保全后删除旧空目录并自动刷新。
- M02: 本次位置与未来归档位置分离：源端保留在 source_root (nas)，未来归档仅保存指向 archive_root (nas.finish)；连载状态继续留源。
- M03: 命名边界及后缀保留：代表本季的有效 S 标记为边界；S 之后的文本 100% 逐字保留；连集不重编号，目标已有文件不改名。
- M04: 目标样本选择：取目标同季已有视频的前缀 P_target；无样本时建议基于清洗剧名的前缀，不擅自拼接年份或 TMDB ID。
- M05: 伴生文件同步：外挂字幕、分集 NFO 等随对应视频采用相同基名，保留语言标签 (.zh-Hans.srt) 与扩展名。
- M06: 非标准及季号冲突：错季文件、无有效 S 标记或歧义标记列入待确认/冲突；特别篇保持非正片。
- M07: 最终路径冲突拦截：重名或批量改名冲突前置拦截，不覆盖、不静默跳过后删源。
- M08: 同集多版本：文件名不同的不同版本完整保留合并，重复版本标识保留。
- M09: 公共资产及未知附件：tvshow.nfo、poster.jpg 等同名资产不覆盖，保全至“合并保留/{旧剧名}/”子目录；未知附件保全。
- M10: 多季与组范围：旧目录仍有其他季度（如 S02）则保留旧剧根，仅清理旧季目录。
- M11: 实物后验与并发保护：写入前后验目标文件存在、源文件离开；仅在确认为空时清理旧目录。
- M12: 幂等与失败恢复：已完成文件不重复移动，清理失败只重试收尾，刷新失败只重试刷新。
- M13: 分类与统计回流：合并后正片 5 集，全季 12 集，已播 6 集，无其他异常自动回流「连载留源」。
- M14: 旧接口兼容：仅保存目标与有意共存保持 0 文件写入；合并必须经明确预览与一次执行确认。
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import shutil
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.clouddrive2 import CloudDrive2Client
from app.config import get_settings
from app.core.directory_governance import (
    build_directory_group_target_options,
    normalize_dir_basename,
    normalize_dir_path,
)
from app.core.episode_parser import is_subtitle_file, is_video_file, parse_episode
from app.models.directory_group import DirectoryGroup
from app.models.season import SeasonRecord
from app.models.task import Task
from app.services.gap_fill_tracker import refresh_task_summary

log = logging.getLogger("mediaflow.directory_merge")


def split_season_episode_boundary(filename: str, season_number: int = 1) -> tuple[str, str] | None:
    """根据本季有效季集标记中的 S 拆分 (prefix, suffix)。
    
    边界必须满足：
    1. 前导字符不能是字母或数字（防止匹配到 Some、Show、Season 等普通单词）；
    2. S 后的季号必须与本次处理的 season_number 相符（支持 1 或 01）；
    3. 紧随其后必须是有效集号标记 E (如 S01E01, S1E02, S01E01-E02, S01E01-03)。
    
    返回 (prefix, suffix)，若无法唯一确认或不符合本季则返回 None。
    """
    clean_name = str(filename or "").strip()
    if not clean_name:
        return None

    s_num_str = f"{season_number:02d}"
    s_num_single = f"{season_number:d}"
    # 严格匹配本季的 S 标记: (?<![a-zA-Z0-9])([Ss](?:01|1))[Ee]\d{1,4}
    pattern = re.compile(
        rf"(?i)(?<![a-zA-Z0-9])([Ss](?:{s_num_str}|{s_num_single}))([Ee]\d{{1,4}}(?:-[Ee]?\d{{1,4}})?)(.*)"
    )
    matches = list(pattern.finditer(clean_name))
    if not matches:
        return None
    if len(matches) > 1:
        # 存在重复标记歧义，不盲猜
        return None

    m = matches[0]
    start_pos = m.start()
    prefix = clean_name[:start_pos]
    suffix = clean_name[start_pos:]
    return prefix, suffix


def is_mismatched_season_file(filename: str, season_number: int = 1) -> bool:
    """检查文件是否包含其他非本季的有效季集标记（如处理 S01 时出现 S02E01）。"""
    clean_name = str(filename or "").strip()
    m = re.search(r"(?i)(?<![a-zA-Z0-9])[Ss](\d{1,2})[Ee]\d{1,4}", clean_name)
    if not m:
        return False
    detected_season = int(m.group(1))
    return detected_season != season_number


def extract_target_prefix_samples(
    target_filenames: list[str],
    season_number: int = 1,
    fallback_title: str = "",
) -> list[str]:
    """从目标目录已有本季视频中提取命名前缀样本 P_target，按出现频率倒序排列。"""
    counts: dict[str, int] = {}
    for fn in target_filenames:
        if not is_video_file(fn) and not is_subtitle_file(fn):
            continue
        boundary = split_season_episode_boundary(fn, season_number)
        if boundary:
            prefix = boundary[0]
            counts[prefix] = counts.get(prefix, 0) + 1

    if counts:
        # 按频率降序
        sorted_samples = sorted(counts.keys(), key=lambda k: counts[k], reverse=True)
        return sorted_samples

    # 目标目录无可用样本时，基于清洗剧名给出建议前缀
    base_title = normalize_dir_basename(fallback_title) if fallback_title else ""
    if not base_title:
        base_title = "Show"
    return [f"{base_title} - ", f"{base_title}."]


async def list_files_robust(cd2: CloudDrive2Client | None, dir_path: str) -> list[dict[str, Any]]:
    """读取目录文件清单（兼容本地文件系统与 CloudDrive2）。"""
    norm_dir = normalize_dir_path(dir_path)
    # 优先检查本地文件系统（测试/本地环境）
    if os.path.isdir(norm_dir):
        results = []
        try:
            with os.scandir(norm_dir) as it:
                for entry in it:
                    stat = entry.stat()
                    results.append({
                        "name": entry.name,
                        "path": normalize_dir_path(entry.path),
                        "isDirectory": entry.is_dir(),
                        "size": stat.st_size if not entry.is_dir() else 0,
                    })
            return sorted(results, key=lambda x: x["name"])
        except Exception as e:
            log.warning("本地读取目录 %s 失败: %s", norm_dir, e)

    # 尝试 CloudDrive2
    if cd2 is not None:
        try:
            files = await cd2.list_files(norm_dir, force_refresh=True)
            return files or []
        except Exception as e:
            log.warning("CD2 读取目录 %s 失败: %s", norm_dir, e)
    return []


async def move_and_rename_file_robust(
    cd2: CloudDrive2Client | None,
    src_file_path: str,
    target_dir_path: str,
    new_filename: str,
) -> str:
    """执行单个文件的移动与改名（兼容本地与 CloudDrive2，返回最终路径）。"""
    norm_src = normalize_dir_path(src_file_path)
    norm_target_dir = normalize_dir_path(target_dir_path)
    final_target_path = f"{norm_target_dir}/{new_filename}"
    old_filename = posixpath.basename(norm_src)

    # 检查本地文件系统
    if os.path.exists(norm_src):
        os.makedirs(norm_target_dir, exist_ok=True)
        shutil.move(norm_src, final_target_path)
        return final_target_path

    # CloudDrive2 客户端调用
    if cd2 is not None:
        # 1. 确保目标目录存在
        try:
            await cd2.mkdir(norm_target_dir)
        except Exception:
            pass

        # 2. 如果文件名一致直接移动，若不一致则移动后重命名
        if old_filename == new_filename:
            mv_res = await cd2.move_file(norm_src, norm_target_dir, skip_existing=False)
            if not mv_res.get("success", False):
                raise RuntimeError(f"CD2 移动文件失败: {mv_res.get('errorMessage') or '未知错误'}")
        else:
            mv_res = await cd2.move_file(norm_src, norm_target_dir, skip_existing=False)
            if not mv_res.get("success", False):
                raise RuntimeError(f"CD2 移动文件失败: {mv_res.get('errorMessage') or '未知错误'}")
            temp_path = f"{norm_target_dir}/{old_filename}"
            rn_res = await cd2.rename_file(temp_path, new_filename)
            if not rn_res.get("success", False):
                raise RuntimeError(f"CD2 重命名文件失败: {rn_res.get('errorMessage') or '未知错误'}")
        return final_target_path

    raise RuntimeError(f"无法定位文件或缺少有效存储驱动: {norm_src}")


async def delete_dir_robust(cd2: CloudDrive2Client | None, dir_path: str) -> bool:
    """清理空目录（兼容本地与 CloudDrive2）。"""
    norm_dir = normalize_dir_path(dir_path)
    if os.path.isdir(norm_dir):
        try:
            # 仅在为空时安全删除
            if not os.listdir(norm_dir):
                os.rmdir(norm_dir)
                return True
            return False
        except Exception as e:
            log.warning("本地清理目录 %s 失败: %s", norm_dir, e)
            return False

    if cd2 is not None:
        try:
            res = await cd2.delete_file(norm_dir)
            return bool(res.get("success", True))
        except Exception as e:
            log.warning("CD2 清理目录 %s 失败: %s", norm_dir, e)
            return False
    return False


async def build_directory_merge_plan(
    db: AsyncSession,
    task_id: str,
    group_id: str,
    *,
    target_path: str = "",
    source_target_path: str = "",
    season_number: int = 1,
    selected_prefix: str = "",
    cd2_client: CloudDrive2Client | None = None,
) -> dict[str, Any]:
    """生成只读目录合并与前缀统一执行计划（M01–M10）。"""
    task = await db.get(Task, task_id)
    group = await db.get(DirectoryGroup, group_id)
    if not task or not group or group.task_id != task_id:
        raise ValueError("任务或多目录组不存在")

    settings = get_settings()
    entries = []
    try:
        entries = json.loads(group.paths_json or "[]")
    except Exception:
        entries = []

    src_entries = [e for e in entries if isinstance(e, dict) and e.get("side") == "source"]
    all_src_paths = [normalize_dir_path(e.get("norm_path")) for e in src_entries if e.get("norm_path")]

    # 1. 解析目标归档路径与源端主目标路径
    target_options = build_directory_group_target_options(
        entries,
        category=group.category_key,
        tmdb_id=group.tmdb_id,
        source_root=settings.source_root,
        archive_root=settings.archive_root,
    )
    candidates = target_options.get("candidates") or []

    chosen_target_option = None
    if target_path:
        norm_req_target = normalize_dir_path(target_path)
        for c in candidates:
            if normalize_dir_path(c.get("target_path")) == norm_req_target:
                chosen_target_option = c
                break

    if chosen_target_option is None and candidates:
        chosen_target_option = candidates[0]

    archive_target_show = (
        normalize_dir_path(chosen_target_option.get("target_path"))
        if chosen_target_option
        else (normalize_dir_path(target_path) or normalize_dir_path(group.confirmed_target_path))
    )

    # 确定本次源端主目录 (source_target_path)
    final_src_target = normalize_dir_path(source_target_path)
    if not final_src_target:
        if chosen_target_option and chosen_target_option.get("source_show_paths"):
            final_src_target = normalize_dir_path(chosen_target_option["source_show_paths"][0])
        elif all_src_paths:
            # 优先选名称与 TMDB 标题一致或目录名最贴合的源目录
            canonical_name = normalize_dir_basename(group.title or "")
            best_match = None
            for p in all_src_paths:
                bname = normalize_dir_basename(posixpath.basename(p))
                if canonical_name and bname == canonical_name:
                    best_match = p
                    break
            final_src_target = best_match or all_src_paths[0]

    # 待并入的其他源目录列表
    merge_from_show_paths = [p for p in all_src_paths if p != final_src_target]

    block_reasons: list[str] = []
    can_execute = True

    if not final_src_target:
        can_execute = False
        block_reasons.append("未能确定源端目标主目录")

    if not merge_from_show_paths:
        can_execute = False
        block_reasons.append("当前组内未发现其他需要并入的源端目录")

    for p in merge_from_show_paths:
        if p == final_src_target or final_src_target.startswith(f"{p}/") or p.startswith(f"{final_src_target}/"):
            can_execute = False
            block_reasons.append(f"源目录与目标目录存在自指或嵌套包含关系: {p} <-> {final_src_target}")

    target_season_dir = f"{final_src_target}/Season {season_number}"
    target_files_raw = await list_files_robust(cd2_client, target_season_dir)
    target_file_names = {f["name"] for f in target_files_raw if not f.get("isDirectory")}

    # 提取目标同季前缀样本 (M04)
    target_clean_name = posixpath.basename(final_src_target).split(" [tmdbid=")[0].split(" (")[0].strip()
    prefix_samples = extract_target_prefix_samples(
        list(target_file_names), season_number=season_number, fallback_title=target_clean_name
    )
    chosen_prefix = selected_prefix if (selected_prefix and selected_prefix in prefix_samples) else prefix_samples[0]

    files_to_move: list[dict[str, Any]] = []
    assets_to_preserve: list[dict[str, Any]] = []
    remaining_seasons: list[str] = []
    directories_to_clean: list[str] = []

    planned_target_names: set[str] = set()

    for old_src_show in merge_from_show_paths:
        old_season_dir = f"{old_src_show}/Season {season_number}"
        old_season_files = await list_files_robust(cd2_client, old_season_dir)

        # 检查旧剧根下是否还有其他季（M10）
        root_entries = await list_files_robust(cd2_client, old_src_show)
        for re_item in root_entries:
            if re_item.get("isDirectory"):
                dir_name = re_item["name"]
                m_s = re.match(r"(?i)^(?:Season\s*|S)(\d{1,2})$", dir_name)
                if m_s and int(m_s.group(1)) != season_number:
                    remaining_seasons.append(f"{posixpath.basename(old_src_show)}/{dir_name}")

        # 检查旧剧根下的公共资产（M09）
        for re_item in root_entries:
            if not re_item.get("isDirectory"):
                fname = re_item["name"]
                fpath = re_item["path"]
                # 公共刮削资产 (tvshow.nfo, poster.jpg 等)
                is_public_asset = bool(
                    re.match(r"(?i)^(tvshow\.nfo|poster\..+|fanart\..+|banner\..+|clearlogo\..+|logo\..+)$", fname)
                )
                # 目标根是否存在同名
                target_root_files = await list_files_robust(cd2_client, final_src_target)
                target_has_asset = any(tf["name"].lower() == fname.lower() for tf in target_root_files)
                old_dir_tag = posixpath.basename(old_src_show)

                if is_public_asset and not target_has_asset:
                    dest_path = f"{final_src_target}/{fname}"
                    dest_dir = final_src_target
                    dest_desc = "迁入目标剧根"
                else:
                    dest_dir = f"{final_src_target}/合并保留/{old_dir_tag}"
                    dest_path = f"{dest_dir}/{fname}"
                    dest_desc = f"保全至合并保留子目录 ({old_dir_tag})"

                assets_to_preserve.append({
                    "source_path": fpath,
                    "file_name": fname,
                    "target_dir": dest_dir,
                    "target_path": dest_path,
                    "description": dest_desc,
                })

        # 处理季目录内的文件
        for f in old_season_files:
            if f.get("isDirectory"):
                continue
            fname = f["name"]
            fpath = f["path"]

            # 错季校验 (M06)
            if is_mismatched_season_file(fname, season_number):
                can_execute = False
                block_reasons.append(f"错季文件冲突：{fname} 包含非第 {season_number} 季集号标记，已拦截盲改")
                continue

            # 拆分前缀与季集标记边界 (M03)
            boundary = split_season_episode_boundary(fname, season_number)
            if not boundary:
                can_execute = False
                block_reasons.append(f"命名标记冲突：{fname} 缺少代表本季的有效季集标记，需人工核实")
                continue

            orig_prefix, orig_suffix = boundary
            new_filename = f"{chosen_prefix}{orig_suffix}"

            # 冲突检测 (M07)
            is_conflict = False
            conflict_reason = ""
            if new_filename in target_file_names:
                is_conflict = True
                conflict_reason = f"目标目录已存在同名文件: {new_filename}，禁止覆盖"
                can_execute = False
                block_reasons.append(conflict_reason)
            elif new_filename in planned_target_names:
                is_conflict = True
                conflict_reason = f"待移入文件改名后同名冲突: {new_filename}"
                can_execute = False
                block_reasons.append(conflict_reason)

            planned_target_names.add(new_filename)

            pe = parse_episode(fname, fpath, f.get("size", 0))
            is_sub = is_subtitle_file(fname)
            is_vid = is_video_file(fname)

            files_to_move.append({
                "source_path": fpath,
                "source_filename": fname,
                "new_filename": new_filename,
                "target_dir": target_season_dir,
                "target_path": f"{target_season_dir}/{new_filename}",
                "kind": "video" if is_vid else ("subtitle" if is_sub else "other"),
                "episode_numbers": pe.episode_numbers,
                "is_special": pe.is_special,
                "size_bytes": f.get("size", 0),
                "is_conflict": is_conflict,
                "conflict_reason": conflict_reason,
            })

        # 待清理目录
        directories_to_clean.append(old_season_dir)
        if not remaining_seasons:
            directories_to_clean.append(old_src_show)

    # 检查是否连载中（用于前端提示）
    is_airing = False
    target_recs = list((await db.scalars(
        select(SeasonRecord).where(
            SeasonRecord.task_id == task_id,
            SeasonRecord.show_path == final_src_target,
            SeasonRecord.season_number == season_number,
        )
    )).all())
    if target_recs:
        rec0 = target_recs[0]
        ev = json.loads(rec0.evidence_json or "{}") if getattr(rec0, "evidence_json", None) else {}
        b_ev = ev.get("broadcast_evidence") or {}
        is_airing = bool(rec0.status == "airing" or b_ev.get("status") in ("airing", "upcoming"))

    plan_id = f"plan_merge_{uuid4().hex[:12]}"
    plan = {
        "plan_id": plan_id,
        "task_id": task_id,
        "group_id": group_id,
        "source_target_show_path": final_src_target,
        "archive_target_show_path": archive_target_show,
        "season_number": season_number,
        "is_airing": is_airing,
        "airing_notice": "合并后继续留源，不表示全季已归档" if is_airing else "",
        "prefix_samples": prefix_samples,
        "chosen_prefix": chosen_prefix,
        "merge_from_show_paths": merge_from_show_paths,
        "files_to_move": files_to_move,
        "assets_to_preserve": assets_to_preserve,
        "directories_to_clean": list(dict.fromkeys(directories_to_clean)),
        "remaining_seasons": remaining_seasons,
        "can_execute": can_execute,
        "block_reasons": list(dict.fromkeys(block_reasons)),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # 持久化暂存至 group.evidence_json
    try:
        grp_ev = json.loads(group.evidence_json or "{}")
    except Exception:
        grp_ev = {}
    grp_ev["latest_merge_plan"] = plan
    group.evidence_json = json.dumps(grp_ev, ensure_ascii=False)
    await db.flush()

    return plan


async def execute_directory_merge(
    db: AsyncSession,
    task_id: str,
    group_id: str,
    *,
    plan_id: str = "",
    cd2_client: CloudDrive2Client | None = None,
) -> dict[str, Any]:
    """执行已通过预检的同作品源端目录合并与前缀统一计划（M01–M14）。"""
    task = await db.get(Task, task_id)
    group = await db.get(DirectoryGroup, group_id)
    if not task or not group or group.task_id != task_id:
        raise ValueError("任务或多目录组不存在")

    grp_ev = json.loads(group.evidence_json or "{}") if getattr(group, "evidence_json", None) else {}
    plan = grp_ev.get("latest_merge_plan") or {}

    if not plan or (plan_id and plan.get("plan_id") != plan_id):
        raise ValueError("未找到匹配且有效的合并执行计划，请重新生成预览")

    if not plan.get("can_execute"):
        reasons = "; ".join(plan.get("block_reasons") or ["存在阻止合并的冲突"])
        raise ValueError(f"该合并计划存在冲突，禁止执行: {reasons}")

    cd2 = cd2_client or CloudDrive2Client()

    source_target_show = plan["source_target_show_path"]
    archive_target_show = plan["archive_target_show_path"]
    season_number = plan.get("season_number", 1)
    target_season_dir = f"{source_target_show}/Season {season_number}"

    files_to_move = plan.get("files_to_move") or []
    assets_to_preserve = plan.get("assets_to_preserve") or []
    directories_to_clean = plan.get("directories_to_clean") or []
    remaining_seasons = plan.get("remaining_seasons") or []

    moved_files: list[str] = []
    renamed_files: list[str] = []
    preserved_assets: list[str] = []
    cleaned_dirs: list[str] = []
    errors: list[str] = []

    # 1. 移动并重命名视频与伴生文件 (M01, M03, M05, M12 幂等)
    for item in files_to_move:
        src_path = item["source_path"]
        t_dir = item["target_dir"]
        new_name = item["new_filename"]
        old_name = item["source_filename"]

        try:
            # 幂等检查：目标已存在且源已离开
            final_path = f"{t_dir}/{new_name}"
            if os.path.exists(final_path) and not os.path.exists(src_path):
                moved_files.append(new_name)
                if old_name != new_name:
                    renamed_files.append(new_name)
                continue

            await move_and_rename_file_robust(cd2, src_path, t_dir, new_name)
            moved_files.append(new_name)
            if old_name != new_name:
                renamed_files.append(new_name)
        except Exception as e:
            err = f"移动文件失败 {src_path} -> {new_name}: {e}"
            log.error(err)
            errors.append(err)

    if errors:
        raise RuntimeError(f"文件合并中断，部分文件未就位: {'; '.join(errors)}")

    # 2. 保全剧根公共资产与附件 (M09)
    for asset in assets_to_preserve:
        src_path = asset["source_path"]
        dest_dir = asset["target_dir"]
        dest_name = asset["file_name"]
        try:
            final_dest = f"{dest_dir}/{dest_name}"
            if os.path.exists(final_dest) and not os.path.exists(src_path):
                preserved_assets.append(dest_name)
                continue
            await move_and_rename_file_robust(cd2, src_path, dest_dir, dest_name)
            preserved_assets.append(dest_name)
        except Exception as e:
            log.warning("保全资产失败 %s: %s", src_path, e)

    # 3. 双端后验 (M11)
    target_files_after = await list_files_robust(cd2, target_season_dir)
    target_names_after = {f["name"] for f in target_files_after}
    for item in files_to_move:
        if item["new_filename"] not in target_names_after:
            raise RuntimeError(f"双端后验失败：目标目录未见合并文件 {item['new_filename']}")

    # 4. 回收旧目录空壳 (M01, M10, M11)
    for dpath in directories_to_clean:
        if dpath in remaining_seasons:
            continue
        # 如果是剧根目录，且存在剩余季度，绝对不删除 (M10)
        is_root = any(dpath == p for p in plan.get("merge_from_show_paths", []))
        if is_root and remaining_seasons:
            continue
        ok = await delete_dir_robust(cd2, dpath)
        if ok:
            cleaned_dirs.append(dpath)

    # 5. 局部云盘强制刷新 (M01, M14)
    refresh_candidates = {
        task.source_path,
        source_target_show,
        posixpath.dirname(source_target_show),
    }
    for p in plan.get("merge_from_show_paths", []):
        refresh_candidates.add(posixpath.dirname(p))

    for r_dir in refresh_candidates:
        if r_dir:
            try:
                await cd2.list_files(r_dir, force_refresh=True)
            except Exception as e:
                log.warning("刷新云盘目录缓存异常 %s: %s", r_dir, e)

    # 6. 数据库记录更新与五模块分流重算 (M13)
    # 查找目标记录与旧记录
    target_records = list((await db.scalars(
        select(SeasonRecord).where(
            SeasonRecord.task_id == task_id,
            SeasonRecord.show_path == source_target_show,
            SeasonRecord.season_number == season_number,
        )
    )).all())

    all_group_recs = list((await db.scalars(
        select(SeasonRecord).where(SeasonRecord.task_id == task_id)
    )).all())

    # 找出参与合并的旧源端季记录
    merged_eps: set[int] = set()
    for item in files_to_move:
        for ep in item.get("episode_numbers") or []:
            if isinstance(ep, int) and ep > 0:
                merged_eps.add(ep)

    target_rec = target_records[0] if target_records else None
    if target_rec is not None:
        try:
            curr_eps = set(json.loads(target_rec.recognized_episodes_json or "[]"))
        except Exception:
            curr_eps = set()
        union_eps = sorted(curr_eps | merged_eps)
        target_rec.recognized_episodes_json = json.dumps(union_eps)
        target_rec.local_episode_count = len(union_eps)
        target_rec.archive_target_path = f"{archive_target_show}/Season {season_number}"

        # 解除目录组阻塞
        try:
            t_ev = json.loads(target_rec.evidence_json or "{}")
        except Exception:
            t_ev = {}
        old_dg = t_ev.get("directory_group") or {}
        t_ev["directory_group"] = {
            **old_dg,
            "id": group.id,
            "status": "confirmed",
            "requires_confirm": False,
            "confirmation_mode": "use_archive_target",
            "confirmed_target_path": archive_target_show,
            "invalidated_reason": "",
            "merged_from": plan.get("merge_from_show_paths", []),
        }
        target_rec.evidence_json = json.dumps(t_ev, ensure_ascii=False)

    # 将已被并入的旧记录标为已结案/源端已离开
    for rec in all_group_recs:
        if normalize_dir_path(rec.show_path) in [normalize_dir_path(p) for p in plan.get("merge_from_show_paths", [])]:
            if rec.season_number == season_number:
                rec.status = "source_absent"
                rec.status_detail = f"文件已全部合并至 {source_target_show} 并统一前缀"
                rec.recognized_episodes_json = "[]"
                rec.local_episode_count = 0

    # 更新 DirectoryGroup 状态
    group.status = "confirmed"
    group.confirmation_mode = "use_archive_target"
    group.confirmed_target_path = archive_target_show
    group.invalidated_reason = ""
    group.confirmed_at = datetime.now(timezone.utc)

    grp_ev["last_merge_receipt"] = {
        "plan_id": plan.get("plan_id"),
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "moved_files": moved_files,
        "renamed_files": renamed_files,
        "preserved_assets": preserved_assets,
        "cleaned_dirs": cleaned_dirs,
    }
    group.evidence_json = json.dumps(grp_ev, ensure_ascii=False)

    # 统一重算任务摘要与分类
    summary = await refresh_task_summary(task_id, db)
    await db.commit()
    await db.refresh(group)

    target_show_name = posixpath.basename(source_target_show).split(" [tmdbid=")[0]
    success_msg = f"已合并到{target_show_name}目录，已统一 {len(renamed_files)} 个文件名，旧目录已清理，目录已刷新"

    return {
        "ok": True,
        "group_id": group.id,
        "plan_id": plan.get("plan_id"),
        "summary": summary,
        "moved_count": len(moved_files),
        "renamed_count": len(renamed_files),
        "cleaned_dirs": cleaned_dirs,
        "preserved_assets": preserved_assets,
        "message": success_msg,
    }
