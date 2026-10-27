"""
阶段三：复核清理（含修复闭环核验）

核心流程：
1. 扫描当前黄色项季目录
2. 【整理修复核验】核验修复涉及的集号是否已实际进入网盘。若未闭环严格暂缓清理，不误删其他版本
3. 识别版本完整性，保留唯一完整版本，清理冗余旧版本与专属刮削文件
4. 执行删除后自动触发底层网盘目录页面刷新（refreshDir）
5. 播完且齐全后标绿
"""

from datetime import datetime
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set

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

    for i, task in enumerate(session.tasks, start=1):
        logger.progress("复核", i, len(session.tasks), f"{task.name} Season {task.season}")

        try:
            try:
                files = cd2.list_files(task.path)
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
            try:
                total = int(task.data.get("来源登记集数"))
            except (ValueError, TypeError):
                total = len(videos)

            # 解析视频集号与版本模板
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

            # ──【整理修复闭环核验】──
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

            # 按版本分组
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

            if len(complete_groups) > 1:
                session.updates[task] = {
                    "state": "多个完整版本，人工判断",
                    "color": "yellow",
                    "repair": current_repair,
                    "note": f"{len(complete_groups)} 个版本均完整，不自动淘汰",
                    "fileCheckedAt": now_iso
                }
                summary["skipped"] += 1
                continue

            if len(complete_groups) == 0:
                all_missing = [n for n in range_episodes(total) if n not in found_episodes]
                missing_desc = f"缺 {'、'.join(str(n) for n in all_missing)} 集" if all_missing else "无单独完整版本"
                session.updates[task] = {
                    "state": "缺集" if all_missing else "混版拼齐，人工判断",
                    "color": "yellow",
                    "repair": current_repair,
                    "note": missing_desc,
                    "fileCheckedAt": now_iso
                }
                summary["skipped"] += 1
                continue

            # 唯一完整版本
            best = complete_groups[0]
            to_keep: List[Dict[str, Any]] = []
            to_delete: List[Dict[str, Any]] = []

            for ep in range_episodes(total):
                copies = [f for f in best["files"] if f["episode"] == ep]
                copies.sort(key=lambda x: str(x.get("modified") or ""), reverse=True)
                if len(copies) > 1 and copies[0].get("modified") == copies[1].get("modified"):
                    session.updates[task] = {
                        "state": "重复副本时间相同",
                        "color": "yellow",
                        "repair": current_repair,
                        "note": f"第 {ep} 集无法判断保留哪份",
                        "fileCheckedAt": now_iso
                    }
                    summary["skipped"] += 1
                    break
                to_keep.append(copies[0])
                for dup in copies[1:]:
                    d_item = dict(dup)
                    d_item["reason"] = "同版本较旧重复副本"
                    to_delete.append(d_item)

            if task in session.updates:
                continue

            for k, g in groups.items():
                if k == best["key"]:
                    continue
                for f in g["files"]:
                    d_item = dict(f)
                    d_item["reason"] = "其他不完整版本"
                    to_delete.append(d_item)

            # 刮削文件级联判定
            deleted_paths = {f["path"] for f in to_delete}
            for f in files:
                f_name = f.get("name", "")
                if not SCRAPE_REGEX.search(f_name):
                    continue
                stem = re.sub(r"\.[^.]+$", "", f_name)
                if SHARED_SCRAPE.search(stem):
                    continue

                matches = [
                    v for v in videos
                    if stem.lower() == re.sub(r"\.[^.]+$", "", v.get("name", "")).lower()
                    or stem.lower() == re.sub(r"\.[^.]+$", "", v.get("name", "")).lower() + "-thumb"
                ]
                if len(matches) == 1 and matches[0].get("path") in deleted_paths:
                    to_delete.append({
                        "name": f_name,
                        "path": f.get("path"),
                        "modified": f.get("modified"),
                        "size": f.get("size"),
                        "reason": "唯一对应已删视频的刮削文件"
                    })

            if not to_delete:
                broadcast_ended = (task.data.get("播出状态") == "已播完")
                session.updates[task] = {
                    "state": "齐全" if broadcast_ended else "集号齐全，播出状态待确认",
                    "color": "green" if broadcast_ended else "yellow",
                    "repair": current_repair,
                    "note": f"保留版本: {best['key']}",
                    "fileCheckedAt": now_iso,
                }
                continue

            logger.info(f"  📋 保留 {len(to_keep)} 个文件, 拟删除 {len(to_delete)} 个")

            if dry_run:
                session.updates[task] = {
                    "state": "待确认清理（预览）",
                    "color": "yellow",
                    "repair": current_repair,
                    "note": f"拟删除 {len(to_delete)} 个文件",
                    "remove": [f["path"] for f in to_delete],
                    "keep": [f["path"] for f in to_keep],
                    "fileCheckedAt": now_iso,
                }
                continue

            # 执行实际物理删除
            deleted_count = 0
            for f in to_delete:
                try:
                    cd2.delete_file(f["path"])
                    deleted_count += 1
                    logger.debug(f"    ✅ 已删除: {f['name']}")
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

            broadcast_ended = (task.data.get("播出状态") == "已播完")
            is_all_clean = (broadcast_ended and deleted_count == len(to_delete))
            session.updates[task] = {
                "state": "齐全" if is_all_clean else "已清理",
                "color": "green" if is_all_clean else "yellow",
                "repair": current_repair,
                "note": f"已删除 {deleted_count}/{len(to_delete)} 个文件",
                "deleted": [f["path"] for idx, f in enumerate(to_delete) if idx < deleted_count],
                "keep": [f["path"] for f in to_keep],
                "fileCheckedAt": now_iso,
            }

        except Exception as e:
            session.updates[task] = {
                "state": "处理失败", "color": None, "note": str(e), "fileCheckedAt": now_iso
            }
            summary["errors"] += 1

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
