"""
阶段一：剧集初检

扫描 CloudDrive2 源目录，自适应识别【单剧目录/单分类目录/源根目录】，
逐剧逐季核对本地集号与 TMDB 登记集数，生成初检 Excel 报表。
"""

from datetime import datetime
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.parse

from ..clients.clouddrive2 import CloudDriveClient
from ..clients.tmdb import TMDBClient
from ..config import PROJECT_ROOT
from ..core.episode_parser import (
    MEDIA_REGEX,
    episode_from_name,
    is_positive,
    range_episodes,
    uniq,
)
from ..core.report_adapter import TaskItem, write_report, ReportSession
from ..core.title_matcher import season_number, show_info


def compute_category_rel_path(dir_path: str, source_root: str) -> str:
    """计算分类相对于源根目录的相对路径 (如 "动漫/日韩动漫" 或 "电视剧/国产剧")"""
    norm_dir = dir_path.rstrip("/")
    norm_src = (source_root or "").rstrip("/")
    if norm_src and norm_dir.startswith(norm_src):
        rel = norm_dir[len(norm_src):].lstrip("/")
        if rel:
            return rel
    parts = norm_dir.split("/")
    return parts[-1] if parts else "未分类"


def run_inspect(config: Any, logger: Any, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """执行初检阶段"""
    opts = options or {}
    cd2_cfg = config.get("clouddrive2", {}) if isinstance(config, dict) else getattr(config, "clouddrive2", {})
    tmdb_cfg = config.get("tmdb", {}) if isinstance(config, dict) else getattr(config, "tmdb", {})
    paths_cfg = config.get("paths", {}) if isinstance(config, dict) else getattr(config, "paths", {})

    cd2 = CloudDriveClient(
        origin=cd2_cfg.get("origin", ""),
        token=cd2_cfg.get("token", ""),
        logger=logger,
    )
    tmdb = TMDBClient(
        api_key=tmdb_cfg.get("apiKey", ""),
        language=tmdb_cfg.get("language", "zh-CN"),
        logger=logger,
    )

    origin = cd2_cfg.get("origin", "").rstrip("/")
    root_source = (paths_cfg.get("source") or "/115/Library_incs/nas").rstrip("/")
    base_path = (opts.get("path") or root_source).rstrip("/")

    # 1. 根据规则禁用全库初检：未指定任何检测范围且指向根目录时阻断
    if base_path == root_source and not opts.get("category") and not opts.get("show"):
        raise ValueError("已根据要求禁用全库初检。请使用交互式选剧初检 (python3 bin/tv_workflow.py inspect -i)，或通过 --category / --show / --path 指定检测范围。")

    results: List[TaskItem] = []
    summary = {"total": 0, "green": 0, "blue": 0, "yellow": 0, "red": 0, "noFill": 0, "errors": []}

    logger.info(f"📂 正在连接 CloudDrive2，读取目标目录: {base_path}")

    try:
        root_entries = cd2.list_files(base_path)
    except Exception as e:
        raise RuntimeError(f"无法读取目标路径 {base_path}: {e}")

    # ── 智能识别目录层级 ──────────────────────────────────────────
    category_map: Dict[str, List[Dict[str, Any]]] = {}

    # 1. 判断是否是“单部剧集目录”（自身直接包含 Season 1、Season 2 等）
    is_single_show = any(f.get("directory") and season_number(f.get("name", "")) is not None for f in root_entries)

    if is_single_show:
        show_name = base_path.split("/")[-1]
        parent_path = base_path.rsplit("/", 1)[0]
        cat_name = compute_category_rel_path(parent_path, root_source)
        logger.info(f"🎯 目标识别为【单部剧集】: [{cat_name}] {show_name}")
        category_map[cat_name] = [{
            "name": show_name,
            "path": base_path,
            "directory": True,
            "preloadedContents": root_entries,
        }]
    else:
        def collect_categories(dir_path: str, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            dirs = [f for f in entries if f.get("directory")]
            if not dirs:
                return []

            is_show_dir = False
            try:
                sample_sub = cd2.list_files(dirs[0]["path"])
                is_show_dir = any(
                    (f.get("directory") and season_number(f.get("name", "")) is not None) or MEDIA_REGEX.search(f.get("name", ""))
                    for f in sample_sub
                )
            except Exception:
                pass

            if is_show_dir:
                cat_rel = compute_category_rel_path(dir_path, root_source)
                return [{"cat_rel": cat_rel, "path": dir_path, "shows": dirs}]

            res_list = []
            for d in dirs:
                try:
                    sub_entries = cd2.list_files(d["path"])
                    sub_cats = collect_categories(d["path"], sub_entries)
                    res_list.extend(sub_cats)
                except Exception as err:
                    logger.warn(f"  无法读取子目录 {d.get('name')}: {err}")
            return res_list

        collected = collect_categories(base_path, root_entries)
        for item in collected:
            if opts.get("category"):
                cat_filter = str(opts["category"]).lower()
                if cat_filter not in item["cat_rel"].lower():
                    continue
            category_map[item["cat_rel"]] = item["shows"]

    # ── 剧名/分类关键字过滤 ────────────────────────────────────────
    if opts.get("show"):
        kw = opts["show"].lower()
        cats_to_del = []
        for cat_name, shows in category_map.items():
            if kw in cat_name.lower():
                continue
            filtered = [s for s in shows if kw in s.get("name", "").lower()]
            if filtered:
                category_map[cat_name] = filtered
            else:
                cats_to_del.append(cat_name)
        for c in cats_to_del:
            del category_map[c]

        match_count = sum(len(s) for s in category_map.values())
        logger.info(f"🔍 过滤 [{opts['show']}]：共匹配到 {match_count} 部剧")

    total_shows = sum(len(s) for s in category_map.values())
    if total_shows == 0:
        logger.warn("⚠️ 未在指定范围内发现任何符合条件的剧集目录")
        return {**summary, "outputPath": None}

    # ── 逐分类逐剧初检 ──────────────────────────────────────────
    processed = 0

    for cat_name, shows in category_map.items():
        sorted_shows = sorted(shows, key=lambda s: s.get("modified") or "")

        for show in sorted_shows:
            processed += 1
            info = show_info(show.get("name", ""))
            show_name = show.get("name", "")
            logger.progress("初检", processed, total_shows, f"{cat_name} / {show_name}")

            # 获取剧集下的季目录
            if "preloadedContents" in show:
                show_contents = show["preloadedContents"]
            else:
                try:
                    show_contents = cd2.list_files(show["path"])
                except Exception as e:
                    summary["errors"].append({"show": show_name, "error": str(e)})
                    continue

            season_dirs = [
                f for f in show_contents
                if f.get("directory") and season_number(f.get("name", "")) is not None
            ]
            season_dirs.sort(key=lambda s: season_number(s.get("name", "")) or 0)

            if not season_dirs:
                task = TaskItem(
                    category=cat_name,
                    name=show_name,
                    season=None,
                    path=show["path"],
                    showPath=show["path"],
                    data={
                        "分类": cat_name,
                        "剧集目录": show_name,
                        "季数": "",
                        "处理结果": "无季目录",
                        "源云存储链接": f"{origin}/?page=files&path={urllib.parse.quote(show['path'])}"
                    },
                    color="other",
                )
                results.append(task)
                continue

            # 查询 TMDB
            if info.get("tmdb") and tmdb.api_key:
                try:
                    tmdb.get_show_details(info["tmdb"])
                except Exception as e:
                    logger.warn(f"  TMDB 剧集查询跳过: {e}")

            for s_dir in season_dirs:
                s_num = season_number(s_dir.get("name", ""))
                summary["total"] += 1

                try:
                    files = cd2.list_files(s_dir["path"])
                except Exception as e:
                    summary["errors"].append({"show": show_name, "season": s_num, "error": str(e)})
                    continue

                videos = [f for f in files if not f.get("directory") and MEDIA_REGEX.search(f.get("name", ""))]
                episodes = [episode_from_name(f.get("name", ""), s_num) for f in videos]
                episodes = [ep for ep in episodes if ep is not None]

                identified = uniq(episodes)
                seen_ep: Set[int] = set()
                duplicates = []
                for ep in episodes:
                    if ep in seen_ep:
                        duplicates.append(ep)
                    else:
                        seen_ep.add(ep)
                duplicates = uniq(duplicates)

                unknown = [f.get("name", "") for f in videos if episode_from_name(f.get("name", ""), s_num) is None]

                # 查询 TMDB 登记集数与播出状态
                tmdb_total = None
                broadcast_status = "未确认"
                if info.get("tmdb") and tmdb.api_key:
                    try:
                        season_detail = tmdb.get_season_details(info["tmdb"], s_num)
                        tmdb_eps = season_detail.get("episodes") if isinstance(season_detail, dict) else []
                        if tmdb_eps:
                            tmdb_total = len(tmdb_eps)
                            last_ep = tmdb_eps[-1]
                            air_date_str = last_ep.get("air_date")
                            if air_date_str:
                                try:
                                    air_date = datetime.strptime(air_date_str, "%Y-%m-%d")
                                    if air_date < datetime.now():
                                        broadcast_status = "已播完"
                                    else:
                                        broadcast_status = "连载中"
                                except Exception:
                                    pass
                    except Exception:
                        pass

                missing = range_episodes(tmdb_total) if tmdb_total else []
                missing = [n for n in missing if n not in identified]
                is_complete = bool(tmdb_total and len(missing) == 0)

                color = "other"
                if is_complete and broadcast_status == "已播完":
                    color = "green"
                    summary["green"] += 1
                elif broadcast_status == "连载中":
                    color = "blue"
                    summary["blue"] += 1
                elif len(videos) == 0:
                    color = "red"
                    summary["red"] += 1
                elif len(missing) > 0 or len(unknown) > 0 or len(duplicates) > 0:
                    color = "yellow"
                    summary["yellow"] += 1
                else:
                    summary["noFill"] += 1

                def compact(arr: List[Any]) -> str:
                    return "、".join(str(x) for x in arr) if arr else ""

                if is_complete and broadcast_status == "已播完":
                    res_text = "齐全"
                elif missing:
                    res_text = f"缺 {len(missing)} 集"
                else:
                    res_text = "待核实"

                s_link = f"{origin}/?page=files&path={urllib.parse.quote(s_dir['path'])}"

                task = TaskItem(
                    category=cat_name,
                    name=show_name,
                    season=s_num,
                    path=s_dir["path"],
                    showPath=show["path"],
                    color=color,
                    data={
                        "分类": cat_name,
                        "剧集目录": show_name,
                        "季数": s_num,
                        "源云存储链接": s_link,
                        "视频文件数": len(videos),
                        "已识别集数": len(identified),
                        "来源登记集数": tmdb_total or "",
                        "已识别集号": compact(identified),
                        "重复集号": compact(duplicates),
                        "缺失集号": compact(missing) if tmdb_total else "未确认",
                        "无法识别集号的文件": compact(unknown),
                        "TMDB ID": info.get("tmdb") or "",
                        "数据来源": "tmdb" if tmdb_total else "",
                        "播出状态": broadcast_status,
                        "处理结果": res_text,
                        "待播集号": "",
                        "播期未知集号": "",
                        "报告版本": "tv-workflow/1",
                    }
                )
                results.append(task)
                logger.debug(f"  Season {s_num}: {len(identified)}/{tmdb_total or '?'} 集, 标记: {color}")

    # 写入初检报告
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    tag = f"_{opts['show']}" if opts.get("show") else f"_{opts['category']}" if opts.get("category") else ""
    reports_dir = (PROJECT_ROOT / "reports").resolve()
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path = reports_dir / f"初检报告{tag}_{timestamp}.xlsx"

    session = ReportSession(tasks=results, errors=[], skipped=0, mode="inspect")
    write_report(session, str(output_path))

    logger.info(f"\n📊 初检完成: 共核对 {summary['total']} 季")
    logger.info(f"   🟢 齐全 {summary['green']}  🔵 连载 {summary['blue']}  🟡 待处理 {summary['yellow']}  🔴 空 {summary['red']}  ⬜ 其他 {summary['noFill']}")
    logger.info(f"   📄 报告: {output_path}")

    return {**summary, "outputPath": str(output_path)}
