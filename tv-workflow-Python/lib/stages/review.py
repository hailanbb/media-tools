"""
阶段三：复核清理（含修复闭环核验、穿透刷新、零字节安全门禁与字幕伴生级联清理）

核心流程：
1. 穿透刷新扫描当前黄色项季目录（强制同步网盘最新文件元数据）
2. 【安全熔断】检查是否存在 0 字节损坏或传输中的视频文件，若有坚决熔断，禁止误删已有可用版本
3. 【整理修复核验】核验修复涉及的集号是否已实际进入网盘。若未闭环严格暂缓清理，不误删其他版本
4. 识别版本完整性与重复副本：
   - 保持原生分集模板正则分组逻辑
   - 多级精准仲裁同集重复副本（分辨率 > 标准命名 > 文件体积 > 时间戳），消除相同时间戳跳过死锁
   - 存在多个完整版本时，自动优选最高画质版本（如 4K/1080p），淘汰低画质版本（如 720p/SD）
   - 混版拼齐完结且无重集时放行标绿，允许流转至归档阶段
5. 伴生文件级联清理：淘汰旧版视频时，同名专属外挂字幕（.srt/.ass/.vtt等）与专属刮削图片同步清理
6. 执行删除后自动触发底层网盘目录页面刷新（refreshDir），并生成删除审计快照日志
7. 完结齐全标绿，连载全集齐全标蓝
"""

from datetime import datetime
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ..clients.clouddrive2 import CloudDriveClient
from ..core.episode_parser import (
    MEDIA_REGEX,
    SCRAPE_REGEX,
    SHARED_SCRAPE,
    episode_from_name,
    range_episodes,
    uniq,
)
from ..core.repair_state import (
    is_repair_pending,
    read_repair_state,
    verify_repair_state,
)
from ..core.report_adapter import read_report, write_report
from ..platform_support.data_store import create_data_store

# 外挂字幕扩展名正则
SUBTITLE_REGEX = re.compile(
    r"\.(?:srt|ass|ssa|vtt|sub|idx|sup)$",
    re.IGNORECASE
)


def rank_copy(f: Dict[str, Any]) -> Tuple[int, int, int, str]:
    """同集重复副本梯级打分（分辨率规格 > 标准命名规整度 > 文件体积 > 修改时间戳）"""
    f_name = f.get("name", "")
    size = int(f.get("size") or 0)
    # 规整命名优先（避免带 (1)、_1、副本 等拷贝后缀）
    has_dup_marker = 1 if re.search(r"[\s._\-(](?:1|2|copy|副本)[\s._\-)]", f_name, re.I) else 0
    clean_score = 0 if has_dup_marker else 1

    # 画质规格偏好
    res_score = 0
    if re.search(r"2160p|4k|uhd", f_name, re.I):
        res_score = 3
    elif re.search(r"1080p|1080i|fhd", f_name, re.I):
        res_score = 2
    elif re.search(r"720p|hd(?![a-z0-9])", f_name, re.I):
        res_score = 1

    mod_str = str(f.get("modified") or "")
    return (res_score, clean_score, size, mod_str)


def score_group(g: Dict[str, Any]) -> float:
    """计算完整版本的综合画质得分，用于多完整版本优选"""
    files = g.get("files") or []
    if not files:
        return 0.0
    total_size = sum(int(f.get("size") or 0) for f in files)
    avg_size_mb = (total_size / len(files)) / (1024 * 1024)
    res_score = 0.0
    k = str(g.get("key") or "").lower()
    if re.search(r"2160p|4k|uhd", k):
        res_score = 3000.0
    elif re.search(r"1080p|1080i|fhd", k):
        res_score = 2000.0
    elif re.search(r"720p|hd(?![a-z0-9])", k):
        res_score = 500.0
    return res_score + avg_size_mb


def run_review(
    config: Any,
    logger: Any,
    report_path: str,
    options: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """执行复核清理阶段"""
    if not report_path:
        raise ValueError("请指定报告路径")

    opts = options or {}
    dry_run = opts.get("dryRun", True) if "dryRun" in opts else (not opts.get("yes", False))

    cd2_cfg = config.get("clouddrive2", {}) if isinstance(config, dict) else getattr(config, "clouddrive2", {})
    paths_cfg = config.get("paths", {}) if isinstance(config, dict) else getattr(config, "paths", {})

    cd2 = CloudDriveClient(
        origin=cd2_cfg.get("origin", ""),
        token=cd2_cfg.get("token", ""),
        logger=logger,
    )

    mode_label = "预览" if dry_run else "执行清理"
    logger.info(f"📄 读取报告... (模式: {mode_label})")
    session = read_report(
        file_path=report_path,
        origin=cd2_cfg.get("origin", ""),
        source=paths_cfg.get("source", ""),
        mode="review"
    )
    logger.info(f"📄 待复核: {len(session.tasks)} 项黄色")
    session.updates = {}

    summary = {
        "total": len(session.tasks),
        "cleaned": 0,
        "skipped": 0,
        "repairPending": 0,
        "errors": 0,
        "deletedFiles": 0
    }

    now_iso = datetime.now().isoformat()
    all_audit_deleted: List[Dict[str, Any]] = []

    for i, task in enumerate(session.tasks, start=1):
        logger.progress("复核", i, len(session.tasks), f"{task.name} Season {task.season}")

        try:
            # 1. 穿透刷新拉取云盘最新文件列表
            try:
                files = cd2.list_files(task.path, force_refresh=True)
            except Exception as e:
                session.updates[task] = {
                    "state": "目录读取失败", "color": None, "note": str(e), "fileCheckedAt": now_iso
                }
                summary["errors"] += 1
                continue

            if any(f.get("directory") for f in files):
                session.updates[task] = {
                    "state": "存在子目录", "color": "yellow", "note": "季内存在子目录，需人工确认", "fileCheckedAt": now_iso
                }
                summary["skipped"] += 1
                continue

            if any(re.search(r"\.(?:part|tmp|download|crdownload|!qb)$", f.get("name", ""), re.I) for f in files):
                session.updates[task] = {
                    "state": "下载中", "color": "yellow", "note": "发现临时下载文件，等待完成", "fileCheckedAt": now_iso
                }
                summary["skipped"] += 1
                continue

            videos = [f for f in files if MEDIA_REGEX.search(f.get("name", ""))]

            # 2. 0 字节破损或正在写入文件安全熔断门禁（绝对防止误删现有好文件）
            zero_byte_videos = [f for f in videos if int(f.get("size") or 0) <= 0]
            if zero_byte_videos:
                zero_names = "、".join(f.get("name", "") for f in zero_byte_videos[:2])
                logger.warn(f"  ⚠️ 发现 {len(zero_byte_videos)} 个 0 字节视频文件 ({zero_names})，触发防误删熔断保护，暂缓清理")
                session.updates[task] = {
                    "state": "存在0字节破损/传输中文件",
                    "color": "yellow",
                    "note": f"发现 {len(zero_byte_videos)} 个 0 字节视频 ({zero_names})，暂缓清理以防误删",
                    "fileCheckedAt": now_iso,
                }
                summary["skipped"] += 1
                continue

            try:
                total = int(task.data.get("来源登记集数"))
            except (ValueError, TypeError):
                total = len(videos)

            # 3. 解析视频集号与版本模板（严格保持原生简单分集正则逻辑）
            parsed = []
            for f in videos:
                f_name = f.get("name", "")
                ep = episode_from_name(f_name, task.season)
                if ep is None:
                    parsed.append(None)
                    continue

                stem = re.sub(r"\.[^.]+$", "", f_name)
                se_re = re.compile(rf"S0*{task.season}\s*E\d+", re.IGNORECASE)
                template = se_re.sub(f"S{task.season}E{{集}}", stem)
                if template == stem:
                    template = re.sub(r"第\s*\d+\s*集", "第{集}集", template)

                m_ext = re.search(r"\.[^.]+$", f_name)
                ext = m_ext.group(0) if m_ext else ""
                key = re.sub(r"\s+", " ", template).strip().lower() + ext

                parsed.append({
                    "episode": ep,
                    "key": key,
                    "name": f_name,
                    "path": f.get("path"),
                    "modified": f.get("modified"),
                    "size": f.get("size")
                })

            valid_parsed = [p for p in parsed if p is not None]
            found_episodes = uniq([p["episode"] for p in valid_parsed])

            # 4. 【整理修复闭环核验】
            current_repair = read_repair_state(task.data)
            if current_repair:
                current_repair = verify_repair_state(current_repair, found_episodes, now_iso)
                if is_repair_pending(current_repair):
                    logger.warn("  ⚠️ 整理修复尚未闭环（部分修复集号尚未在网盘呈现），暂缓清理")
                    session.updates[task] = {
                        "state": "修复未闭环，暂缓清理",
                        "color": "yellow",
                        "repair": current_repair,
                        "note": "已触发 MoviePilot 修复，但对应集号文件尚未在网盘同步到位，避免误删旧版本",
                        "fileCheckedAt": now_iso,
                    }
                    summary["repairPending"] += 1
                    continue

            # 检查是否有无法识别集号的视频
            unknown_videos = [videos[idx].get("name") for idx, p in enumerate(parsed) if p is None]
            if unknown_videos:
                session.updates[task] = {
                    "state": "待核实",
                    "color": "yellow",
                    "repair": current_repair,
                    "note": f"{len(unknown_videos)} 个视频无法识别集号",
                    "fileCheckedAt": now_iso
                }
                summary["skipped"] += 1
                continue

            # 按原生版本模板分组
            groups: Dict[str, Dict[str, Any]] = {}
            for p in valid_parsed:
                k = p["key"]
                if k not in groups:
                    groups[k] = {"key": k, "files": [], "episodes": []}
                groups[k]["files"].append(p)
                groups[k]["episodes"].append(p["episode"])

            for g in groups.values():
                g_eps = uniq(g["episodes"])
                g["episodes"] = g_eps
                g["missing"] = [n for n in range_episodes(total) if n not in g_eps]
                g["complete"] = (len(g["missing"]) == 0)

            complete_groups = [g for g in groups.values() if g["complete"]]

            to_keep: List[Dict[str, Any]] = []
            to_delete: List[Dict[str, Any]] = []
            version_note = ""

            # 5. 完整版本判定与多完整版本画质优选淘汰
            if len(complete_groups) > 1:
                # 多完整版本：按画质评分排序，优选最高画质，淘汰劣质完整版
                complete_groups.sort(key=score_group, reverse=True)
                best = complete_groups[0]
                version_note = f"存在 {len(complete_groups)} 个完整版本，优选保留最佳画质 [{best['key']}]，淘汰劣质版本"
                logger.info(f"  💡 {version_note}")

                # 优选版本内部去重
                for ep in range_episodes(total):
                    copies = [f for f in best["files"] if f["episode"] == ep]
                    copies.sort(key=rank_copy, reverse=True)
                    to_keep.append(copies[0])
                    for dup in copies[1:]:
                        d_item = dict(dup)
                        d_item["reason"] = "同版本较旧或冗余副本"
                        to_delete.append(d_item)

                # 将其余低画质完整版本文件全部纳入清理
                for inferior_g in complete_groups[1:]:
                    for f in inferior_g["files"]:
                        d_item = dict(f)
                        d_item["reason"] = f"淘汰低画质完整版本 ({inferior_g['key']})"
                        to_delete.append(d_item)

                # 其他不完整版本也清理
                for k, g in groups.items():
                    if g in complete_groups:
                        continue
                    for f in g["files"]:
                        d_item = dict(f)
                        d_item["reason"] = "其他不完整版本"
                        to_delete.append(d_item)

            elif len(complete_groups) == 1:
                # 单一完整版本：保留该版本，梯级仲裁去重
                best = complete_groups[0]
                version_note = f"保留版本: {best['key']}"

                for ep in range_episodes(total):
                    copies = [f for f in best["files"] if f["episode"] == ep]
                    copies.sort(key=rank_copy, reverse=True)
                    to_keep.append(copies[0])
                    for dup in copies[1:]:
                        d_item = dict(dup)
                        d_item["reason"] = "同版本较旧或冗余副本"
                        to_delete.append(d_item)

                for k, g in groups.items():
                    if k == best["key"]:
                        continue
                    for f in g["files"]:
                        d_item = dict(f)
                        d_item["reason"] = "其他不完整版本"
                        to_delete.append(d_item)

            else:
                # 无单一完整版本：检查所有集号是否已全齐（混版拼齐）
                all_missing = [n for n in range_episodes(total) if n not in found_episodes]
                if all_missing:
                    missing_desc = f"缺 {'、'.join(str(n) for n in all_missing)} 集"
                    session.updates[task] = {
                        "state": "缺集",
                        "color": "yellow",
                        "repair": current_repair,
                        "note": missing_desc,
                        "fileCheckedAt": now_iso
                    }
                    summary["skipped"] += 1
                    continue
                else:
                    # 混版齐全（1..total 各集均有）：梯级仲裁优选各集最佳副本
                    version_note = f"各集版本不同但已拼齐 (共 {total} 集)"
                    logger.info(f"  💡 {version_note}，按画质梯队自动优选保留单集最佳副本")

                    for ep in range_episodes(total):
                        copies = [p for p in valid_parsed if p["episode"] == ep]
                        copies.sort(key=rank_copy, reverse=True)
                        to_keep.append(copies[0])
                        for dup in copies[1:]:
                            d_item = dict(dup)
                            d_item["reason"] = "混版中较低质量或冗余副本"
                            to_delete.append(d_item)

            # 6. 外挂字幕与刮削文件级联判定（彻底清理孤立幽灵字幕）
            deleted_paths = {f["path"] for f in to_delete}
            kept_paths = {f["path"] for f in to_keep}

            for f in files:
                f_name = f.get("name", "")
                is_sub = bool(SUBTITLE_REGEX.search(f_name))
                is_scrape = bool(SCRAPE_REGEX.search(f_name))
                if not is_sub and not is_scrape:
                    continue

                stem = re.sub(r"\.[^.]+$", "", f_name)
                if is_scrape and SHARED_SCRAPE.search(stem):
                    continue

                clean_stem = re.sub(r"\.(?:zh|chs|cht|chi|eng|sc|tc|jpn|default|forced)$", "", stem, flags=re.IGNORECASE)

                matched_deleted = [
                    v for v in to_delete
                    if stem.lower() == re.sub(r"\.[^.]+$", "", v.get("name", "")).lower()
                    or stem.lower() == re.sub(r"\.[^.]+$", "", v.get("name", "")).lower() + "-thumb"
                    or clean_stem.lower() == re.sub(r"\.[^.]+$", "", v.get("name", "")).lower()
                ]
                matched_kept = [
                    v for v in to_keep
                    if stem.lower() == re.sub(r"\.[^.]+$", "", v.get("name", "")).lower()
                    or clean_stem.lower() == re.sub(r"\.[^.]+$", "", v.get("name", "")).lower()
                ]

                # 仅当对应视频被删除，且保留视频中没有同名视频时级联删除该字幕/刮削
                if matched_deleted and not matched_kept:
                    to_delete.append({
                        "name": f_name,
                        "path": f.get("path"),
                        "modified": f.get("modified"),
                        "size": f.get("size"),
                        "reason": "对应已删视频的外挂字幕" if is_sub else "唯一对应已删视频的刮削文件"
                    })

            broadcast_ended = (task.data.get("播出状态") == "已播完")

            # 7. 无需删除文件时的状态流转（完结齐全标绿，连载当前集齐标蓝）
            if not to_delete:
                if broadcast_ended:
                    state_str = "齐全"
                    color_str = "green"
                else:
                    state_str = "连载中，当前集齐全"
                    color_str = "blue"

                session.updates[task] = {
                    "state": state_str,
                    "color": color_str,
                    "repair": current_repair,
                    "note": version_note,
                    "fileCheckedAt": now_iso,
                }
                continue

            logger.info(f"  📋 保留 {len(to_keep)} 个文件, 拟删除 {len(to_delete)} 个")

            if dry_run:
                session.updates[task] = {
                    "state": "待确认清理（预览）",
                    "color": "yellow",
                    "repair": current_repair,
                    "note": f"拟删除 {len(to_delete)} 个文件 ({version_note})",
                    "remove": [f["path"] for f in to_delete],
                    "keep": [f["path"] for f in to_keep],
                    "fileCheckedAt": now_iso,
                }
                continue

            # 8. 执行实际物理删除
            deleted_count = 0
            for f in to_delete:
                try:
                    cd2.delete_file(f["path"])
                    deleted_count += 1
                    logger.debug(f"    ✅ 已删除: {f['name']}")
                    all_audit_deleted.append({
                        "task": f"{task.name} Season {task.season}",
                        "deleted_file": f["path"],
                        "name": f["name"],
                        "size": f.get("size", 0),
                        "modified": f.get("modified", ""),
                        "reason": f.get("reason", ""),
                        "deleted_at": datetime.now().isoformat(),
                    })
                except Exception as del_err:
                    logger.error(f"    ❌ 删除失败: {f['name']} - {del_err}")

            summary["deletedFiles"] += deleted_count
            summary["cleaned"] += 1

            # 规约：删除后立即刷新季目录缓存
            if deleted_count > 0:
                try:
                    cd2.refresh_dir(task.path)
                    logger.debug(f"    🔄 已刷新季目录: {task.path}")
                except Exception as ref_err:
                    logger.warn(f"    ⚠️ 刷新季目录异常: {ref_err}")

            is_all_clean = (deleted_count == len(to_delete))
            if is_all_clean and broadcast_ended:
                final_state = "齐全"
                final_color = "green"
            elif is_all_clean and not broadcast_ended:
                final_state = "连载中，当前集齐全"
                final_color = "blue"
            else:
                final_state = "已清理"
                final_color = "yellow"

            session.updates[task] = {
                "state": final_state,
                "color": final_color,
                "repair": current_repair,
                "note": f"已删除 {deleted_count}/{len(to_delete)} 个文件 ({version_note})",
                "deleted": [f["path"] for idx, f in enumerate(to_delete) if idx < deleted_count],
                "keep": [f["path"] for f in to_keep],
                "fileCheckedAt": now_iso,
            }

        except Exception as e:
            session.updates[task] = {
                "state": "处理失败", "color": None, "note": str(e), "fileCheckedAt": now_iso
            }
            summary["errors"] += 1

    # 9. 物理删除审计快照存盘
    if not dry_run and all_audit_deleted:
        try:
            log_dir = Path("logs")
            log_dir.mkdir(parents=True, exist_ok=True)
            audit_file = log_dir / f"review_deleted_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(audit_file, "w", encoding="utf-8") as af:
                json.dump(all_audit_deleted, af, ensure_ascii=False, indent=2)
            logger.info(f"   📋 物理删除审计快照已生成: {audit_file.resolve()}")
        except Exception as a_err:
            logger.warn(f"   ⚠️ 写入删除审计日志跳过: {a_err}")

    suffix = "_复核预览" if dry_run else "_复核清理"
    output_path = re.sub(
        r"(?:_补缺下载|_复核预览|_复核清理|_归档预览|_归档完成)*\.xlsx$",
        f"{suffix}.xlsx",
        report_path,
        flags=re.IGNORECASE
    )
    write_report(session, output_path)

    action_label = "预览" if dry_run else "清理"
    logger.info(f"\n📊 复核{action_label}完成:")
    logger.info(
        f"   ✅ 已清理 {summary['cleaned']}  ⏳ 修复待闭环 {summary['repairPending']}  "
        f"⏭ 跳过 {summary['skipped']}  🗑 删除文件 {summary['deletedFiles']}  ❌ 错误 {summary['errors']}"
    )
    logger.info(f"   📄 报告: {output_path}")

    return {**summary, "outputPath": output_path}
