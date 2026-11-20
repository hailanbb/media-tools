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
    compute_group_paths_signature,
    normalize_dir_basename,
    normalize_dir_path,
)
from app.core.review_engine import build_file_fingerprints
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


def sanitize_fs_name(name: str) -> str:
    """文件系统非法字符规范化（保留空格、中文与括号，剔除 Windows/Linux 非法路径字符）。"""
    cleaned = re.sub(r'[\\/:*?"<>|]+', " ", str(name or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned


def build_canonical_show_dir_name(
    *,
    canonical_title: str = "",
    first_air_year: str = "",
    tmdb_id: str | int | None = None,
    aliases: list[str] | None = None,
    original_title: str = "",
    existing_name: str = "",
) -> tuple[str, str]:
    """按“规范剧名 (作品首播年) [tmdbid=ID]”生成同 ID 规范目录名及本轮采用依据 (G06)。"""
    chosen_title = ""
    basis = ""
    if sanitize_fs_name(canonical_title):
        chosen_title = sanitize_fs_name(canonical_title)
        basis = "TMDB 简体中文规范剧名"
    else:
        cn_alias = next(
            (sanitize_fs_name(a) for a in (aliases or []) if re.search(r"[\u4e00-\u9fff]", str(a or ""))),
            "",
        )
        if cn_alias:
            chosen_title = cn_alias
            basis = "有效中文别名"
        elif sanitize_fs_name(original_title):
            chosen_title = sanitize_fs_name(original_title)
            basis = "作品原名"
        else:
            fallback = re.sub(r"\s*\[tmdbid=\d+\]\s*", " ", str(existing_name or ""), flags=re.I)
            fallback = re.sub(r"\s*\(\d{4}\)\s*", " ", fallback).strip()
            chosen_title = sanitize_fs_name(fallback) or "Unknown"
            basis = "现有目录名称"

    # 若 chosen_title 自身已含年份/ID，先剥离避免重复拼接
    chosen_title = re.sub(r"\s*\[tmdbid=\d+\]\s*", "", chosen_title, flags=re.I).strip()
    m_yr = re.search(r"\((\d{4})\)$", chosen_title)
    yr = str(first_air_year or "").strip()[:4]
    if not yr.isdigit() and m_yr:
        yr = m_yr.group(1)
    if m_yr:
        chosen_title = chosen_title[:m_yr.start()].strip()

    parts = [chosen_title]
    if yr and yr.isdigit() and len(yr) == 4:
        parts.append(f"({yr})")
    tid_str = str(tmdb_id or "").strip()
    if tid_str.isdigit():
        parts.append(f"[tmdbid={tid_str}]")
    return " ".join(parts), basis


def _path_basename(p: str | None) -> str:
    s = str(p or "").rstrip("/\\")
    if not s:
        return ""
    if "\\" in s and "/" not in s:
        import ntpath
        return ntpath.basename(s)
    return posixpath.basename(s.replace("\\", "/"))


def _path_dirname(p: str | None) -> str:
    s = str(p or "").rstrip("/\\")
    if not s:
        return ""
    if "\\" in s and "/" not in s:
        import ntpath
        return ntpath.dirname(s)
    return posixpath.dirname(s.replace("\\", "/"))


def detect_explicit_file_tmdb_id(filename: str) -> str | None:
    """提取文件名中显式标注的 TMDB ID（仅显式异 ID 才判异作品冲突，英文译名/压制组差异不算 · G07）。"""
    m = re.search(r"\[?tmdbid[=:](\d+)\]?", str(filename or ""), re.IGNORECASE)
    return m.group(1) if m else None


def select_prefix_with_basis(
    target_filenames: list[str],
    season_number: int = 1,
    canonical_title: str = "",
    selected_prefix: str = "",
) -> tuple[str, list[str], str]:
    """按 4.2 节优先级自动选择前缀模板并给出可解释依据：
    1. 用户明确指定且合法 -> 使用指定值；
    2. 目标存在唯一前缀 -> 沿用目标前缀；
    3. 目标存在多种前缀 -> 优先与规范名称一致的模板 -> 票数最多模板 -> 确定项目格式 `规范剧名 - `。
    """
    counts: dict[str, int] = {}
    for fn in target_filenames:
        if not is_video_file(fn) and not is_subtitle_file(fn):
            continue
        boundary = split_season_episode_boundary(fn, season_number)
        if boundary:
            p = boundary[0]
            counts[p] = counts.get(p, 0) + 1

    clean_canon = sanitize_fs_name(canonical_title)
    default_fmt = f"{clean_canon} - " if clean_canon else "Show - "
    alt_fmt = f"{clean_canon}." if clean_canon else "Show."

    if not counts:
        samples = [default_fmt, alt_fmt]
        if selected_prefix and selected_prefix in samples:
            return selected_prefix, samples, "用户指定前缀"
        return default_fmt, samples, f"目标同季无已有样本，按规范剧名生成标准前缀「{default_fmt}」"

    sorted_by_votes = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    existing_samples = [k for k, _ in sorted_by_votes]
    samples = list(existing_samples)
    if clean_canon:
        for cand_fmt in (default_fmt, alt_fmt):
            if cand_fmt and cand_fmt not in samples:
                samples.append(cand_fmt)

    if selected_prefix and selected_prefix in samples:
        return selected_prefix, samples, "用户指定前缀"
    if selected_prefix and selected_prefix.strip():
        return selected_prefix, [selected_prefix, *samples], "用户指定前缀"

    if len(existing_samples) == 1:
        only_p = existing_samples[0]
        if clean_canon and normalize_dir_basename(only_p) != normalize_dir_basename(clean_canon):
            # 若目标同季仅有旧前缀且与选定规范目录剧名不同，仍提供标准规范前缀供下拉切换，默认沿用已有或切为规范模板
            return only_p, samples, f"沿用目标同季一致前缀「{only_p}」({counts[only_p]} 个文件)"
        return only_p, samples, f"沿用目标同季一致前缀「{only_p}」({counts[only_p]} 个文件)"

    # 多种前缀：优先匹配规范名称
    if clean_canon:
        norm_canon = normalize_dir_basename(clean_canon)
        for cand_p, cnt in sorted_by_votes:
            if normalize_dir_basename(cand_p) == norm_canon:
                return cand_p, samples, f"目标存在多种前缀，自动优先采用与规范名称一致的模板「{cand_p}」({cnt} 个文件)"

    # 次选数量最多的模板；同票采用确定项目格式
    top_votes = sorted_by_votes[0][1]
    top_cands = [k for k, v in sorted_by_votes if v == top_votes]
    if len(top_cands) == 1:
        return top_cands[0], samples, f"目标存在多种前缀，自动采用出现次数最多的模板「{top_cands[0]}」({top_votes} 个文件)"
    return default_fmt, [default_fmt, *[s for s in samples if s != default_fmt]], f"目标多模板同票，采用项目标准格式「{default_fmt}」"


def _directory_inventory_snapshot(files):
    return sorted(({"path": normalize_dir_path(f.get("path", "")), "name": f.get("name", ""),
                    "id": str(f.get("id") or ""), "size": int(f.get("size") or 0),
                    "modified": str(f.get("modified") or ""), "isDirectory": bool(f.get("isDirectory"))}
                   for f in files), key=lambda f: (f["path"], f["id"]))


async def list_files_robust(
    cd2: CloudDrive2Client | None,
    dir_path: str,
    *,
    force_refresh: bool = True,
) -> list[dict[str, Any]]:
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
                        "modified": str(stat.st_mtime_ns),
                        "id": f"{stat.st_dev}:{stat.st_ino}",
                    })
            return sorted(results, key=lambda x: x["name"])
        except Exception as e:
            raise RuntimeError(f"本地读取目录失败: {norm_dir}") from e

    # 尝试 CloudDrive2
    if cd2 is not None:
        try:
            files = await cd2.list_files(norm_dir, force_refresh=force_refresh)
            if not isinstance(files, list):
                raise RuntimeError("目录读取未返回完整清单")
            return files
        except Exception as e:
            raise RuntimeError(f"CD2 读取目录失败: {norm_dir}") from e
    raise RuntimeError(f"目录不存在或未提供云盘读取客户端: {norm_dir}")


async def move_and_rename_file_robust(
    cd2: CloudDrive2Client | None,
    src_file_path: str,
    target_dir_path: str,
    new_filename: str,
    prepared_dirs: dict[str, set[str]] | None = None,
) -> str:
    """执行单个文件的移动与改名（兼容本地与 CloudDrive2，返回最终路径）。"""
    norm_src = normalize_dir_path(src_file_path)
    norm_target_dir = normalize_dir_path(target_dir_path)
    final_target_path = f"{norm_target_dir}/{new_filename}"
    old_filename = _path_basename(norm_src)
    if normalize_dir_path(final_target_path) == norm_src:
        return final_target_path

    # 检查本地文件系统
    if os.path.exists(norm_src):
        os.makedirs(norm_target_dir, exist_ok=True)
        if os.path.exists(final_target_path) and os.path.normcase(norm_src) != os.path.normcase(final_target_path):
            raise FileExistsError(f"目标已存在，禁止覆盖: {final_target_path}")
        shutil.move(norm_src, final_target_path)
        return final_target_path

    # CloudDrive2 客户端调用
    if cd2 is not None:
        prepared_dirs = prepared_dirs if prepared_dirs is not None else {}
        if norm_target_dir not in prepared_dirs:
            try:
                await cd2.mkdir(norm_target_dir)
            except Exception:
                pass  # 仅由随后成功列举确认目标已存在；读取失败继续抛出
            listed = await cd2.list_files(norm_target_dir, force_refresh=True)
            prepared_dirs[norm_target_dir] = {f.get("name", "").casefold() for f in listed}
        existing_names = prepared_dirs[norm_target_dir]
        same_directory = normalize_dir_path(_path_dirname(norm_src)) == norm_target_dir
        if new_filename.casefold() in existing_names or (not same_directory and old_filename.casefold() in existing_names):
            raise FileExistsError(f"目标目录存在同名文件，禁止覆盖: {final_target_path}")

        if not same_directory:
            mv_res = await cd2.move_file(norm_src, norm_target_dir, skip_existing=True)
            if not mv_res.get("success", False):
                raise RuntimeError(f"CD2 移动文件失败: {mv_res.get('errorMessage') or '未知错误'}")
        if old_filename != new_filename:
            temp_path = f"{norm_target_dir}/{old_filename}"
            rn_res = await cd2.rename_file(temp_path, new_filename)
            if not rn_res.get("success", False):
                raise RuntimeError(f"CD2 重命名文件失败: {rn_res.get('errorMessage') or '未知错误'}")
            existing_names.discard(old_filename.casefold())
        existing_names.add(new_filename.casefold())
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
            if await cd2.list_files(norm_dir, force_refresh=True):
                return False
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
    season_numbers: list[int] | None = None,
    all_seasons: bool = False,
    selected_prefix: str = "",
    canonical_title: str = "",
    first_air_year: str = "",
    auto_canonical_target: bool = False,
    smart_collision: bool = False,
    active_download_paths: set[str] | None = None,
    cd2_client: CloudDrive2Client | None = None,
    persist_plan: bool = True,
    force_refresh: bool = True,
    task_obj: Task | None = None,
    group_obj: DirectoryGroup | None = None,
    prefetched_season_records: list[SeasonRecord] | None = None,
) -> dict[str, Any]:
    """生成只读目录合并与前缀统一执行计划（M01–M14, G06–G09）。"""
    from app.core.episode_parser import classify_content_unit

    task = task_obj if task_obj is not None else await db.get(Task, task_id)
    group = group_obj if group_obj is not None else await db.get(DirectoryGroup, group_id)
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
    try:
        grp_ev_in = json.loads(group.evidence_json or "{}") if getattr(group, "evidence_json", None) else {}
    except Exception:
        grp_ev_in = {}
    if not isinstance(grp_ev_in, dict):
        grp_ev_in = {}

    tmdb_meta_from_ev = None
    if grp_ev_in.get("canonical_tmdb_title"):
        tmdb_year_ev = str(grp_ev_in.get("canonical_tmdb_year") or "").strip()[:4]
        tmdb_meta_from_ev = {
            "name": grp_ev_in.get("canonical_tmdb_title"),
            "original_name": grp_ev_in.get("canonical_tmdb_original_name") or "",
            "first_air_date": f"{tmdb_year_ev}-01-01" if tmdb_year_ev.isdigit() else "",
        }

    target_options = build_directory_group_target_options(
        entries,
        category=group.category_key,
        tmdb_id=group.tmdb_id,
        tmdb_meta=tmdb_meta_from_ev,
        source_root=settings.source_root,
        archive_root=settings.archive_root,
    )
    candidates = target_options.get("candidate_targets") or target_options.get("candidates") or []

    norm_req_target = normalize_dir_path(target_path) if target_path else ""
    chosen_target_option = None
    if norm_req_target:
        for c in candidates:
            if normalize_dir_path(c.get("target_path")) == norm_req_target:
                chosen_target_option = c
                break

    if chosen_target_option is None and not norm_req_target and candidates:
        chosen_target_option = candidates[0]

    grp_title_clean = "" if (group.title or "").strip() in ("同一作品有多个目录", "同一作品在归档库有多个目录", "同 ID 异名目录") else (group.title or "")
    eff_canon_title = canonical_title or str(grp_ev_in.get("canonical_tmdb_title") or "").strip() or grp_title_clean
    eff_first_year = first_air_year or str(grp_ev_in.get("canonical_tmdb_year") or "").strip()

    # 计算规范目录名（G06: 规范剧名 (作品首播年) [tmdbid=ID]）
    canon_dir_name, naming_basis = build_canonical_show_dir_name(
        canonical_title=eff_canon_title,
        first_air_year=eff_first_year,
        tmdb_id=group.tmdb_id,
        existing_name=_path_basename(all_src_paths[0]) if all_src_paths else "",
    )

    archive_target_show = (
        norm_req_target
        or (normalize_dir_path(chosen_target_option.get("target_path")) if chosen_target_option else "")
        or normalize_dir_path(group.confirmed_target_path)
    )

    source_target_will_create = False
    # 确定本次源端主目录 (source_target_path)
    final_src_target = normalize_dir_path(source_target_path)
    if not final_src_target and norm_req_target and not auto_canonical_target:
        # 用户在弹窗显式勾选或自定义了目标归档目录：源端主目录与规范剧名严格与之对齐
        req_dir_name = _path_basename(norm_req_target)
        matched_src = None
        if chosen_target_option and chosen_target_option.get("source_show_paths"):
            opt_srcs = [normalize_dir_path(p) for p in chosen_target_option["source_show_paths"] if p]
            matched_src = next((p for p in opt_srcs if _path_basename(p).casefold() == req_dir_name.casefold()), None) or (opt_srcs[0] if opt_srcs else None)
        if not matched_src and req_dir_name:
            matched_src = next((p for p in all_src_paths if _path_basename(p).casefold() == req_dir_name.casefold()), None)
        if matched_src:
            final_src_target = matched_src
        elif all_src_paths and req_dir_name:
            parent_cat_dir = _path_dirname(all_src_paths[0]).rstrip("/\\")
            sep = "\\" if ("\\" in parent_cat_dir and "/" not in parent_cat_dir) else "/"
            final_src_target = normalize_dir_path(f"{parent_cat_dir}{sep}{req_dir_name}")
            source_target_will_create = True

    if not final_src_target and auto_canonical_target and all_src_paths and canon_dir_name:
        # 大小写等价检查：若已有源目录与规范目录名一致则直接复用，否则新建受控规范目录 (G06)
        matched_existing = next(
            (p for p in all_src_paths if _path_basename(p).casefold() == canon_dir_name.casefold()),
            None,
        )
        if matched_existing:
            final_src_target = matched_existing
        else:
            parent_cat_dir = _path_dirname(all_src_paths[0]).rstrip("/\\")
            sep = "\\" if ("\\" in parent_cat_dir and "/" not in parent_cat_dir) else "/"
            final_src_target = normalize_dir_path(f"{parent_cat_dir}{sep}{canon_dir_name}")
            source_target_will_create = True
            if settings.archive_root and group.category_key.startswith("cat:"):
                category_path = group.category_key.removeprefix("cat:").strip("/")
                if category_path == "_root_":
                    category_path = ""
                archive_target_show = normalize_dir_path(posixpath.join(settings.archive_root, category_path, canon_dir_name))
            elif archive_target_show:
                archive_target_show = normalize_dir_path(
                    f"{_path_dirname(archive_target_show)}/{canon_dir_name}"
                )

    if not final_src_target:
        if chosen_target_option and chosen_target_option.get("source_show_paths"):
            final_src_target = normalize_dir_path(chosen_target_option["source_show_paths"][0])
        elif all_src_paths:
            canonical_name = normalize_dir_basename(eff_canon_title or "")
            best_match = None
            for p in all_src_paths:
                bname = normalize_dir_basename(_path_basename(p))
                if canonical_name and bname == canonical_name:
                    best_match = p
                    break
            final_src_target = best_match or all_src_paths[0]

    # 待并入的其他源目录列表（大小写等价去重）
    merge_from_show_paths = [
        p for p in all_src_paths if p.casefold() != final_src_target.casefold()
    ]

    block_reasons: list[str] = []
    can_execute = True

    if not final_src_target:
        can_execute = False
        block_reasons.append("未能确定源端目标主目录")

    archive_paths = {normalize_dir_path(e.get("norm_path")) for e in entries if e.get("side") == "archive"}
    align_only = bool(not merge_from_show_paths and len(all_src_paths) == 1
                      and not source_target_will_create and archive_target_show and len(archive_paths) <= 1)
    if not merge_from_show_paths and not align_only:
        can_execute = False
        block_reasons.append("源端无需归并，但归档目标尚不能唯一确定")

    for p in merge_from_show_paths:
        if p == final_src_target or final_src_target.startswith(f"{p}/") or p.startswith(f"{final_src_target}/"):
            can_execute = False
            block_reasons.append(f"源目录与目标目录存在自指或嵌套包含关系: {p} <-> {final_src_target}")

    # 确定本次作用季列表（G08: 支持一次组级计划覆盖多个季）
    probe_cache: dict[str, list[dict[str, Any]]] = {}
    inventory_snapshots: dict[str, list[dict[str, Any]]] = {}

    async def read_inventory(path):
        # 归并决策不能使用云盘尚未加载的空缓存；每个目录本次只强读一次。
        if path not in probe_cache:
            listing = await list_files_robust(cd2_client, path, force_refresh=True)
            probe_cache[path] = listing
            inventory_snapshots[path] = _directory_inventory_snapshot(listing)
        return probe_cache[path]

    discovered_seasons: set[int] = set()
    if season_numbers:
        discovered_seasons.update(int(s) for s in season_numbers if int(s) > 0)
    elif all_seasons:
        for src_show in merge_from_show_paths:
            root_entries_probe = await read_inventory(src_show)
            probe_cache[src_show] = root_entries_probe
            for re_item in root_entries_probe:
                if re_item.get("isDirectory"):
                    m_s = re.match(r"(?i)^(?:Season\s*|S)(\d{1,2})$", re_item["name"])
                    if m_s and int(m_s.group(1)) > 0:
                        discovered_seasons.add(int(m_s.group(1)))
        # 已知季也纳入收尾，不能因空根回落为 S01 而遗留 S06/S07 的旧记录。
        for entry in entries:
            if entry.get("side") == "source":
                discovered_seasons.update(int(sn) for sn in entry.get("seasons", []) if int(sn) > 0)
        if not discovered_seasons:
            discovered_seasons.add(int(season_number))
    else:
        discovered_seasons.add(int(season_number))

    target_seasons = sorted(discovered_seasons)
    primary_season = target_seasons[0]

    raw_target_show_title = _path_basename(final_src_target) or _path_basename(archive_target_show)
    raw_target_show_title = re.sub(r"\s*\[tmdbid[=:]\d+\]\s*$", "", raw_target_show_title, flags=re.I).strip()
    raw_target_show_title = re.sub(r"\s*\(\d{4}\)\s*$", "", raw_target_show_title).strip()
    target_clean_name = (
        sanitize_fs_name(canonical_title)
        or sanitize_fs_name(raw_target_show_title)
        or eff_canon_title
    )

    active_dl_norm = {str(x).strip().rstrip("/").lower() for x in (active_download_paths or set()) if x}

    files_to_move: list[dict[str, Any]] = []
    collision_dedup_items: list[dict[str, Any]] = []
    isolated_conflicts: list[dict[str, Any]] = []
    deferred_active_downloads: list[dict[str, Any]] = []
    assets_to_preserve: list[dict[str, Any]] = []
    remaining_seasons: list[str] = []
    directories_to_clean: list[str] = []
    per_season_prefixes: dict[int, dict[str, Any]] = {}

    # 预先读取目标根文件以处理剧根公共资产 (M09)
    target_root_files = [] if source_target_will_create else await read_inventory(final_src_target)
    target_root_names_lower = {tf["name"].lower() for tf in target_root_files if not tf.get("isDirectory")}

    # 检查旧剧根公共资产与未纳入本次合并的其他季
    for old_src_show in merge_from_show_paths:
        root_entries = await read_inventory(old_src_show)
        old_remaining_for_show: list[str] = []
        for re_item in root_entries:
            if re_item.get("isDirectory"):
                dir_name = re_item["name"]
                m_s = re.match(r"(?i)^(?:Season\s*|S)(\d{1,2})$", dir_name)
                if m_s and int(m_s.group(1)) not in target_seasons:
                    rem_label = f"{posixpath.basename(old_src_show)}/{dir_name}"
                    remaining_seasons.append(rem_label)
                    old_remaining_for_show.append(rem_label)
                elif not m_s:
                    rem_label = f"{posixpath.basename(old_src_show)}/{dir_name}"
                    remaining_seasons.append(rem_label)
                    old_remaining_for_show.append(rem_label)
                    can_execute = False
                    block_reasons.append(f"季外子目录尚未纳入保全计划: {rem_label}")
            else:
                fname = re_item["name"]
                fpath = re_item["path"]
                is_public_asset = bool(
                    re.match(r"(?i)^(tvshow\.nfo|poster\..+|fanart\..+|banner\..+|clearlogo\..+|logo\..+)$", fname)
                )
                old_dir_tag = posixpath.basename(old_src_show)
                if is_public_asset and fname.lower() not in target_root_names_lower:
                    dest_path = f"{final_src_target}/{fname}"
                    dest_dir = final_src_target
                    dest_desc = "迁入目标剧根"
                    target_root_names_lower.add(fname.lower())
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

        # 逐季规划移动与改名 (G07, G08, G09)
        show_has_isolated_or_deferred = False
        for cur_sn in target_seasons:
            season_entry = next((e for e in root_entries if e.get("isDirectory") and re.fullmatch(rf"(?:Season\s*|S)0*{cur_sn}", e.get("name", ""), re.I)), None)
            if not season_entry:
                continue
            old_season_dir = season_entry.get("path") or f"{old_src_show}/{season_entry['name']}"
            old_season_files = await read_inventory(old_season_dir)
            if not old_season_files:
                directories_to_clean.append(old_season_dir)
                continue

            target_season_dir = f"{final_src_target}/Season {cur_sn}"
            if cur_sn not in per_season_prefixes:
                target_exists = any(e.get("isDirectory") and e.get("name") == f"Season {cur_sn}" for e in target_root_files)
                t_files_raw = await read_inventory(target_season_dir) if target_exists else []
                t_file_map = {f["name"]: f for f in t_files_raw if not f.get("isDirectory")}
                chosen_p, p_samples, p_basis = select_prefix_with_basis(
                    list(t_file_map.keys()),
                    season_number=cur_sn,
                    canonical_title=target_clean_name,
                    selected_prefix=selected_prefix if cur_sn == primary_season else "",
                )
                per_season_prefixes[cur_sn] = {
                    "chosen_prefix": chosen_p,
                    "prefix_samples": p_samples,
                    "prefix_selection_basis": p_basis,
                    "target_file_map": t_file_map,
                    "planned_target_names": set(),
                }

            s_ctx = per_season_prefixes[cur_sn]
            s_chosen_prefix = s_ctx["chosen_prefix"]
            t_file_map = s_ctx["target_file_map"]
            planned_names = s_ctx["planned_target_names"]
            season_has_blocked = False

            for f in old_season_files:
                if f.get("isDirectory"):
                    isolated_conflicts.append({"season_number": cur_sn, "source_path": f.get("path"),
                        "filename": f.get("name"), "reason": "季内子目录尚未完整盘点，保留原目录"})
                    season_has_blocked = True
                    show_has_isolated_or_deferred = True
                    continue
                fname = f["name"]
                fpath = f["path"]
                fpath_low = str(fpath).strip().rstrip("/").lower()

                # 检查活动下载器路径依赖：仅局部延后，不丢失下载器关联
                if active_dl_norm and any(fpath_low == ap or ap in fpath_low for ap in active_dl_norm):
                    deferred_active_downloads.append({
                        "season_number": cur_sn,
                        "source_path": fpath,
                        "filename": fname,
                        "reason": "下载器存在活动写入路径依赖，已局部延后",
                    })
                    season_has_blocked = True
                    show_has_isolated_or_deferred = True
                    continue

                # 检查显式异作品 TMDB ID 冲突（G07：局部隔离争议文件/季，不阻断其他干净季）
                explicit_fid = detect_explicit_file_tmdb_id(fname)
                if explicit_fid and group.tmdb_id and str(explicit_fid) != str(group.tmdb_id):
                    isolated_conflicts.append({
                        "season_number": cur_sn,
                        "source_path": fpath,
                        "filename": fname,
                        "file_tmdb_id": explicit_fid,
                        "group_tmdb_id": str(group.tmdb_id),
                        "reason": f"文件显式标注异作品 TMDB ID ({explicit_fid} != {group.tmdb_id})，已局部隔离",
                    })
                    season_has_blocked = True
                    show_has_isolated_or_deferred = True
                    continue

                # 错季校验 (M06)
                if is_mismatched_season_file(fname, cur_sn):
                    if all_seasons:
                        isolated_conflicts.append({
                            "season_number": cur_sn,
                            "source_path": fpath,
                            "filename": fname,
                            "reason": f"错季文件冲突：{fname} 包含非第 {cur_sn} 季标记，已局部隔离",
                        })
                        season_has_blocked = True
                        show_has_isolated_or_deferred = True
                    else:
                        can_execute = False
                        block_reasons.append(f"错季文件冲突：{fname} 包含非第 {cur_sn} 季集号标记，已拦截盲改")
                    continue

                # 拆分前缀与季集标记边界 (M03)
                boundary = split_season_episode_boundary(fname, cur_sn)
                if not boundary:
                    # season.nfo、海报等季级附件不含 SxxEyy，不能当作错命名视频。
                    # 不覆盖规范目录已有资料，原名保全到有来源标识的目录。
                    if not is_video_file(fname) and not is_subtitle_file(fname):
                        dest_dir = f"{target_season_dir}/合并保留/{posixpath.basename(old_src_show)}"
                        assets_to_preserve.append({"source_path": fpath, "file_name": fname,
                            "target_dir": dest_dir, "target_path": f"{dest_dir}/{fname}",
                            "source_snapshot": build_file_fingerprints([f]).get(fpath),
                            "description": "保全季级附件（保留原名，不覆盖已有资料）"})
                        continue
                    if all_seasons:
                        isolated_conflicts.append({
                            "season_number": cur_sn,
                            "source_path": fpath,
                            "filename": fname,
                            "reason": f"命名标记冲突：{fname} 缺少有效季集标记，已局部隔离",
                        })
                        season_has_blocked = True
                        show_has_isolated_or_deferred = True
                    else:
                        can_execute = False
                        block_reasons.append(f"命名标记冲突：{fname} 缺少代表本季的有效季集标记，需人工核实")
                    continue

                orig_prefix, orig_suffix = boundary
                new_filename = f"{s_chosen_prefix}{orig_suffix}"
                dest_season_dir = target_season_dir

                # 冲突检测与智能防覆盖处置 (M07, G09)
                is_conflict = False
                conflict_reason = ""
                collision_resolution = ""

                if new_filename in t_file_map or new_filename in planned_names:
                    if smart_collision:
                        existing_f = t_file_map.get(new_filename)
                        src_unit = classify_content_unit(fname, season_number=cur_sn, filepath=fpath, file_size=int(f.get("size") or 0))
                        tgt_unit = (
                            classify_content_unit(existing_f["name"], season_number=cur_sn, filepath=existing_f.get("path", ""), file_size=int(existing_f.get("size") or 0))
                            if existing_f
                            else None
                        )
                        if (
                            existing_f
                            and is_video_file(fname)
                            and src_unit.get("content_kind") == "regular"
                            and bool(src_unit.get("episode_numbers"))
                            and src_unit["content_unit_key"] == tgt_unit["content_unit_key"]
                            and int(f.get("size") or 0) > 0
                            and int(existing_f.get("size") or 0) > 0
                            and int(f.get("size") or 0) <= int(existing_f.get("size") or 0)
                        ):
                            # 已证实等价副本且目标侧不劣于源侧 -> 归入副本清理，绝不覆盖
                            collision_dedup_items.append({
                                "season_number": cur_sn,
                                "source_path": fpath,
                                "source_filename": fname,
                                "source_snapshot": build_file_fingerprints([f]).get(fpath),
                                "target_snapshot": build_file_fingerprints([existing_f]).get(existing_f.get("path")),
                                "replaced_by": existing_f["name"],
                                "replaced_by_path": existing_f.get("path", ""),
                                "reason": f"与目标目录同名文件 [{existing_f['name']}] 为同内容等价副本，转入副本清理",
                            })
                            continue
                        # 不同内容或无法证明等价 -> 优先保留原区分前缀，仍碰撞则放入保留子目录 (G09)
                        if fname not in t_file_map and fname not in planned_names:
                            new_filename = fname
                            collision_resolution = "keep_original_prefix"
                        else:
                            old_dir_tag = posixpath.basename(old_src_show)
                            dest_season_dir = f"{target_season_dir}/合并保留/{old_dir_tag}"
                            new_filename = fname
                            collision_resolution = "preserve_in_subdir"
                    else:
                        is_conflict = True
                        conflict_reason = (
                            f"目标目录已存在同名文件: {new_filename}，禁止覆盖"
                            if new_filename in t_file_map
                            else f"待移入文件改名后同名冲突: {new_filename}"
                        )
                        can_execute = False
                        block_reasons.append(conflict_reason)

                planned_names.add(new_filename)

                pe = parse_episode(fname, fpath, f.get("size", 0))
                is_sub = is_subtitle_file(fname)
                is_vid = is_video_file(fname)

                files_to_move.append({
                    "season_number": cur_sn,
                    "source_path": fpath,
                    "source_filename": fname,
                    "new_filename": new_filename,
                    "target_dir": dest_season_dir,
                    "target_path": f"{dest_season_dir}/{new_filename}",
                    "kind": "video" if is_vid else ("subtitle" if is_sub else "other"),
                    "episode_numbers": pe.episode_numbers,
                    "is_special": pe.is_special,
                    "size_bytes": f.get("size", 0),
                    "source_snapshot": build_file_fingerprints([f]).get(fpath),
                    "is_conflict": is_conflict,
                    "conflict_reason": conflict_reason,
                    "collision_resolution": collision_resolution,
                })

            if not season_has_blocked:
                directories_to_clean.append(old_season_dir)

        if not old_remaining_for_show and not show_has_isolated_or_deferred:
            directories_to_clean.append(old_src_show)

    if source_target_will_create and not (files_to_move or assets_to_preserve):
        can_execute = False
        block_reasons.append("源目录强制读取后未发现可迁入内容，不能创建空目标并迁移媒体记录")

    # 若主季尚未填充前缀信息（例如单季目录为空）
    if primary_season not in per_season_prefixes:
        target_season_dir = f"{final_src_target}/Season {primary_season}"
        target_exists = any(e.get("isDirectory") and e.get("name") == f"Season {primary_season}" for e in target_root_files)
        t_files_raw = await read_inventory(target_season_dir) if target_exists else []
        t_names = [f["name"] for f in t_files_raw if not f.get("isDirectory")]
        chosen_p, p_samples, p_basis = select_prefix_with_basis(
            t_names, season_number=primary_season, canonical_title=target_clean_name, selected_prefix=selected_prefix
        )
        per_season_prefixes[primary_season] = {
            "chosen_prefix": chosen_p,
            "prefix_samples": p_samples,
            "prefix_selection_basis": p_basis,
        }

    primary_p_ctx = per_season_prefixes[primary_season]
    prefix_samples = primary_p_ctx["prefix_samples"]
    chosen_prefix = primary_p_ctx["chosen_prefix"]
    prefix_selection_basis = primary_p_ctx["prefix_selection_basis"]

    # 检查是否连载中（用于前端提示）
    is_airing = False
    if prefetched_season_records is not None:
        target_recs = [
            r for r in prefetched_season_records
            if r.task_id == task_id and r.show_path == final_src_target and r.season_number in target_seasons
        ]
    else:
        target_recs = list((await db.scalars(
            select(SeasonRecord).where(
                SeasonRecord.task_id == task_id,
                SeasonRecord.show_path == final_src_target,
                SeasonRecord.season_number.in_(target_seasons),
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
        "rules_version": "v2.5.53",
        "task_id": task_id,
        "group_id": group_id,
        "canonical_dir_name": canon_dir_name,
        "naming_basis": naming_basis,
        "source_target_show_path": final_src_target,
        "source_target_will_create": source_target_will_create,
        "archive_target_show_path": archive_target_show,
        "season_number": primary_season,
        "affected_seasons": target_seasons,
        "is_airing": is_airing,
        "airing_notice": "合并后继续留源，不表示全季已归档" if is_airing else "",
        "prefix_samples": prefix_samples,
        "chosen_prefix": chosen_prefix,
        "prefix_selection_basis": prefix_selection_basis,
        "per_season_prefixes": {
            str(sn): {
                "chosen_prefix": ctx["chosen_prefix"],
                "prefix_selection_basis": ctx["prefix_selection_basis"],
            }
            for sn, ctx in per_season_prefixes.items()
        },
        "merge_from_show_paths": merge_from_show_paths,
        "files_to_move": files_to_move,
        "collision_dedup_items": collision_dedup_items,
        "isolated_conflicts": isolated_conflicts,
        "deferred_active_downloads": deferred_active_downloads,
        "assets_to_preserve": assets_to_preserve,
        "directories_to_clean": list(dict.fromkeys(directories_to_clean)),
        "remaining_seasons": remaining_seasons,
        "can_execute": can_execute,
        "align_only": align_only,
        "inventory_snapshots": inventory_snapshots,
        "block_reasons": list(dict.fromkeys(block_reasons)),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # 持久化暂存至 group.evidence_json
    if persist_plan:
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
    skip_summary_refresh: bool = False,
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
    affected_seasons = [int(s) for s in (plan.get("affected_seasons") or [season_number]) if int(s) > 0]

    files_to_move = plan.get("files_to_move") or []
    collision_dedup_items = plan.get("collision_dedup_items") or []
    assets_to_preserve = plan.get("assets_to_preserve") or []
    directories_to_clean = plan.get("directories_to_clean") or []
    remaining_seasons = plan.get("remaining_seasons") or []
    merge_from_norm = {normalize_dir_path(p) for p in (plan.get("merge_from_show_paths") or [])}

    async def verify_snapshot(path, snapshot, cache=None):
        if not snapshot:
            return
        parent = _path_dirname(path)
        listing = cache.get(parent) if cache is not None else None
        if listing is None:
            listing = await list_files_robust(cd2, parent, force_refresh=True)
            if cache is not None:
                cache[parent] = listing
        current = next((f for f in listing if normalize_dir_path(f.get("path", "")) == normalize_dir_path(path)), None)
        if current is None or int(current.get("size") or 0) != int(snapshot.get("size") or 0):
            raise RuntimeError(f"文件已变化或不存在，请更新归并预览: {path}")
        if snapshot.get("id") and str(current.get("id") or "") != str(snapshot["id"]):
            raise RuntimeError(f"文件 ID 已变化，请更新归并预览: {path}")
        if snapshot.get("modified") and current.get("modified") and str(current["modified"]) != str(snapshot["modified"]):
            raise RuntimeError(f"文件修改时间已变化，请更新归并预览: {path}")

    # 包含目录条目，防止预览后的新增季/附件被遗漏后仍假报整组完成。
    for inventory_path, snapshot in (plan.get("inventory_snapshots") or {}).items():
        current = await list_files_robust(cd2, inventory_path, force_refresh=True)
        if _directory_inventory_snapshot(current) != snapshot:
            raise RuntimeError(f"目录文件已变化，请更新归并预览: {inventory_path}")
    if plan.get("align_only"):
        from app.core.directory_governance import _check_live_group_drift
        drift = await _check_live_group_drift(cd2, group, get_settings())
        if drift:
            raise RuntimeError(drift)

    source_check_cache = {}
    for item in files_to_move:
        await verify_snapshot(item["source_path"], item.get("source_snapshot"), source_check_cache)

    prepared_dirs: dict[str, set[str]] = {}
    moved_files: list[str] = []
    renamed_files: list[str] = []
    dedup_cleaned_files: list[str] = []
    preserved_assets: list[str] = []
    cleaned_dirs: list[str] = []
    refresh_failed: list[str] = []
    errors: list[str] = []

    # 1. 移动并重命名视频与伴生文件 (M01, M03, M05, M12 幂等, G06/G08)
    for item in files_to_move:
        src_path = item["source_path"]
        t_dir = item["target_dir"]
        new_name = item["new_filename"]
        old_name = item["source_filename"]

        try:
            final_path = f"{t_dir}/{new_name}"
            if os.path.exists(final_path) and not os.path.exists(src_path):
                moved_files.append(new_name)
                if old_name != new_name:
                    renamed_files.append(new_name)
                continue

            await move_and_rename_file_robust(cd2, src_path, t_dir, new_name, prepared_dirs)
            moved_files.append(new_name)
            if old_name != new_name:
                renamed_files.append(new_name)
        except Exception as e:
            err = f"移动文件失败 {src_path} -> {new_name}: {e}"
            log.error(err)
            errors.append(err)

    if errors:
        raise RuntimeError(f"文件合并中断，部分文件未就位: {'; '.join(errors)}")

    # 1B. 同名碰撞中的已证实等价副本清理（不覆盖目标，安全回收源端冗余 · G09）
    for c_item in collision_dedup_items:
        src_path = c_item["source_path"]
        try:
            await verify_snapshot(src_path, c_item.get("source_snapshot"))
            await verify_snapshot(c_item["replaced_by_path"], c_item.get("target_snapshot"))
            if os.path.exists(src_path):
                os.remove(src_path)
                dedup_cleaned_files.append(c_item["source_filename"])
            elif cd2 is not None:
                await cd2.delete_file(src_path)
                dedup_cleaned_files.append(c_item["source_filename"])
        except Exception as e:
            raise RuntimeError(f"同名副本尚未核实清理，已保留源端: {src_path}") from e

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
            await move_and_rename_file_robust(cd2, src_path, dest_dir, dest_name, prepared_dirs)
            target_listing = await list_files_robust(cd2, dest_dir, force_refresh=True)
            target_file = next((f for f in target_listing if f.get("name") == dest_name and not f.get("isDirectory")), None)
            source_listing = await list_files_robust(cd2, _path_dirname(src_path), force_refresh=True)
            if target_file is None or any(normalize_dir_path(f.get("path", "")) == normalize_dir_path(src_path) for f in source_listing):
                raise RuntimeError("附件迁入后未通过双端核验")
            expected_size = (asset.get("source_snapshot") or {}).get("size")
            if expected_size is not None and int(target_file.get("size") or 0) != int(expected_size):
                raise RuntimeError("附件迁入后大小不一致")
            preserved_assets.append(dest_name)
        except Exception as e:
            raise RuntimeError(f"附件保全未完成，已停止收尾: {src_path}") from e

    # 3. 双端后验 (M11, G08 支持多季后验)
    checked_dirs_cache: dict[str, set[str]] = {}
    for item in files_to_move:
        t_dir = item["target_dir"]
        if t_dir not in checked_dirs_cache:
            t_files_after = await list_files_robust(cd2, t_dir)
            checked_dirs_cache[t_dir] = {f["name"] for f in t_files_after}
        if item["new_filename"] not in checked_dirs_cache[t_dir]:
            raise RuntimeError(f"双端后验失败：目标目录 {t_dir} 未见合并文件 {item['new_filename']}")

    # 4. 回收旧目录空壳 (M01, M10, M11)
    for dpath in directories_to_clean:
        if dpath in remaining_seasons:
            continue
        is_root = any(normalize_dir_path(dpath) == p for p in merge_from_norm)
        if is_root and remaining_seasons:
            continue
        ok = await delete_dir_robust(cd2, dpath)
        if ok:
            cleaned_dirs.append(dpath)

    planned_roots = ({normalize_dir_path(p) for p in directories_to_clean} & merge_from_norm) if not remaining_seasons else set()
    if planned_roots - {normalize_dir_path(p) for p in cleaned_dirs}:
        raise RuntimeError("文件迁入后旧剧根尚未核验为空，保留原记录并等待收尾")

    if not files_to_move and not collision_dedup_items and not assets_to_preserve and not plan.get("align_only"):
        if any(p not in {normalize_dir_path(x) for x in cleaned_dirs} for p in merge_from_norm):
            raise RuntimeError("旧目录仍有内容，未完成空目录收尾，保留目录组待处理状态")

    # 5. 局部云盘强制刷新 (M01, M14, 刷新失败单列不掩盖媒体合并成功)
    refresh_candidates = {
        task.source_path,
        source_target_show,
        posixpath.dirname(source_target_show),
    }
    for p in plan.get("merge_from_show_paths", []):
        refresh_candidates.add(posixpath.dirname(p))
    for sn in affected_seasons:
        refresh_candidates.add(f"{source_target_show}/Season {sn}")

    refreshed_entries = {}
    for r_dir in refresh_candidates:
        if r_dir:
            try:
                refreshed_entries[normalize_dir_path(r_dir)] = await cd2.list_files(r_dir, force_refresh=True)
            except Exception as e:
                log.warning("刷新云盘目录缓存异常 %s: %s", r_dir, e)
                refresh_failed.append(f"{r_dir}: {e}")

    # 6. 数据库记录按季独立更新与五模块分流重算 (M13, G06, G08 不串季)
    all_group_recs = list((await db.scalars(
        select(SeasonRecord).where(SeasonRecord.task_id == task_id)
    )).all())

    merged_eps_by_season: dict[int, set[int]] = {sn: set() for sn in affected_seasons}
    for item in files_to_move:
        sn_i = int(item.get("season_number") or season_number)
        for ep in item.get("episode_numbers") or []:
            if isinstance(ep, int) and ep > 0:
                merged_eps_by_season.setdefault(sn_i, set()).add(ep)

    target_show_basename = posixpath.basename(source_target_show)
    norm_source_target = normalize_dir_path(source_target_show)

    for sn in affected_seasons:
        t_recs = [
            r for r in all_group_recs
            if normalize_dir_path(r.show_path) == norm_source_target and int(r.season_number) == sn
        ]
        src_recs = [
            r for r in all_group_recs
            if normalize_dir_path(r.show_path) in merge_from_norm and int(r.season_number) == sn
        ]

        # G06: 若目标为新建规范目录，尚无对应 SeasonRecord，则将首个源季记录就地迁移为新规范目录的主记录
        target_rec = t_recs[0] if t_recs else None
        migrated_primary = None
        if target_rec is None and src_recs and any(int(f.get("season_number") or season_number) == sn for f in files_to_move):
            target_rec = src_recs[0]
            migrated_primary = target_rec
            target_rec.show_name = target_show_basename
            target_rec.show_path = source_target_show
            target_rec.season_path = f"{source_target_show}/Season {sn}"

        if target_rec is not None:
            try:
                curr_eps = set(json.loads(target_rec.recognized_episodes_json or "[]"))
            except Exception:
                curr_eps = set()
            union_eps = sorted(curr_eps | merged_eps_by_season.get(sn, set()))
            target_rec.recognized_episodes_json = json.dumps(union_eps)
            target_rec.local_episode_count = len(union_eps)
            target_rec.archive_target_path = f"{archive_target_show}/Season {sn}"

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

        # 将其余已被并入的旧记录标为 source_absent
        for rec in src_recs:
            if rec is migrated_primary:
                continue
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

    # 本次执行造成的目录变化更新快照；其他季同步确认，避免自身操作被误判为外部漂移。
    old_entries = json.loads(group.paths_json or "[]")
    cleaned_norms = {normalize_dir_path(p) for p in cleaned_dirs}
    entries = [e for e in old_entries if normalize_dir_path(e.get("norm_path") or "") not in cleaned_norms]
    if not any(e.get("side") == "source" and normalize_dir_path(e.get("norm_path") or "") == norm_source_target for e in entries):
        target_entry = next((f for f in refreshed_entries.get(normalize_dir_path(posixpath.dirname(source_target_show)), []) if normalize_dir_path(f.get("path", "")) == norm_source_target), {})
        entries.append({"side": "source", "norm_path": norm_source_target, "raw_name": target_show_basename,
                        "stable_id": str(target_entry.get("id") or ""), "tmdb_id": str(group.tmdb_id),
                        "category": group.category_key, "seasons": affected_seasons})
    group.paths_json = json.dumps(entries, ensure_ascii=False)
    group.paths_signature = compute_group_paths_signature(entries)
    for rec in all_group_recs:
        ev = json.loads(rec.evidence_json or "{}")
        dg = ev.get("directory_group") or {}
        if dg.get("id") == group.id:
            ev["directory_group"] = {**dg, "status": "confirmed", "requires_confirm": False,
                                     "invalidated_reason": "", "confirmed_target_path": archive_target_show}
            rec.evidence_json = json.dumps(ev, ensure_ascii=False)

    grp_ev["last_merge_receipt"] = {
        "plan_id": plan.get("plan_id"),
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "affected_seasons": affected_seasons,
        "moved_files": moved_files,
        "renamed_files": renamed_files,
        "dedup_cleaned_files": dedup_cleaned_files,
        "preserved_assets": preserved_assets,
        "cleaned_dirs": cleaned_dirs,
        "refresh_failed": refresh_failed,
    }
    group.evidence_json = json.dumps(grp_ev, ensure_ascii=False)

    summary = {} if skip_summary_refresh else await refresh_task_summary(task_id, db)
    await db.commit()
    await db.refresh(group)

    target_show_name = posixpath.basename(source_target_show).split(" [tmdbid=")[0]
    success_msg = f"已合并到{target_show_name}目录，已统一 {len(renamed_files)} 个文件名，已清理 {len(cleaned_dirs)} 个实际空目录，目录已刷新"
    if refresh_failed:
        success_msg += f"（{len(refresh_failed)} 个目录刷新待重试）"

    return {
        "ok": True,
        "group_id": group.id,
        "plan_id": plan.get("plan_id"),
        "affected_seasons": affected_seasons,
        "summary": summary,
        "moved_count": len(moved_files),
        "renamed_count": len(renamed_files),
        "dedup_cleaned_count": len(dedup_cleaned_files),
        "cleaned_dirs": cleaned_dirs,
        "preserved_assets": preserved_assets,
        "refresh_failed": refresh_failed,
        "message": success_msg,
    }
