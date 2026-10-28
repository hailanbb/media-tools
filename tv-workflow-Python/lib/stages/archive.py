"""
阶段四：剧集归档与归档后对账复核（含穿透刷新、多格式别名识别、集数守恒核验与解耦增量归档）

核心流程：
1. 检查各剧集待归档季的完结齐全状态（支持整剧归档与已有剧集增量季安全解耦归档）
2. 目标分类多级递归探测与创建，关键决策点采用穿透刷新（force_refresh=True）消除网盘缓存时滞
3. 迁移处理：
   - 增量季合入模式：将源端完结季目录安全迁入已有目标剧集目录，并在源端仍有留存季时安全保留源根目录
   - 全新整剧移动模式：整剧迁移至目标分类，并穿透刷新两端目录树
4. 归档后对账复核 (Post-Archive Audit)：
   - 目标目录精准集数守恒核验（实到视频数与原报告登记集数精确比对，杜绝漏移）
   - 源目录存量物理对账与多格式 TMDB/标题别名残留排查
   - 原样保留空别名安全清理
5. 输出归档审计快照日志与双格式报表（.xlsx + .csv）
"""

from datetime import datetime
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ..clients.clouddrive2 import CloudDriveClient
from ..core.episode_parser import MEDIA_REGEX
from ..core.repair_state import is_repair_pending, read_repair_state
from ..core.report_adapter import read_report, write_report
from ..core.title_matcher import season_number as parse_season_number


def extract_tmdb_id(text: str) -> Optional[int]:
    """多格式提取 TMDB ID：兼容 [tmdbid=123], {tmdb-123}, [tmdb-123], tmdb:123 等主流格式"""
    if not text:
        return None
    m = re.search(r"(?:\[|\{)tmdb(?:id)?[-_:=]?(\d+)(?:\]|\})", str(text), re.I)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    m = re.search(r"(?:^|[^a-z0-9])tmdb(?:id)?[-_:=](\d+)(?![a-z0-9])", str(text), re.I)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def extract_clean_title(text: str) -> str:
    """提取干净剧集标题用于别名归一化比对"""
    t = re.sub(r"(?:\[|\{)tmdb(?:id)?[-_:=]?\d+(?:\]|\})", "", str(text), flags=re.I)
    t = re.sub(r"[（(](?:19|20)\d{2}[)）]", "", t)
    return re.sub(r"[\s._\-]+", "", t).lower().strip()


def ensure_dir_recursive(cd2: CloudDriveClient, dir_path: str) -> bool:
    """递归确保目录及其所有父目录存在，并刷新目录树"""
    norm_path = dir_path.rstrip("/")
    if not norm_path or norm_path == "/":
        return True
    try:
        if cd2.exists(norm_path):
            return True
    except Exception:
        pass

    parts = norm_path.strip("/").split("/")
    cur = ""
    for p in parts:
        cur += "/" + p
        try:
            if not cd2.exists(cur):
                cd2.mkdir(cur)
                cd2.refresh_dir(cur)
        except Exception:
            pass
    return cd2.exists(norm_path)


def run_archive(
    config: Any,
    logger: Any,
    report_path: str,
    options: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """执行归档阶段"""
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
    source = paths_cfg.get("source", "").rstrip("/")
    dest = paths_cfg.get("destination", "").rstrip("/")
    allow_single_season = bool(opts.get("allowSingleSeasonArchive") or paths_cfg.get("allowSingleSeasonArchive", False))

    mode_label = "预览" if dry_run else "执行归档"
    logger.info(f"📄 读取报告... (模式: {mode_label})")
    session = read_report(
        file_path=report_path,
        origin=cd2_cfg.get("origin", ""),
        source=source,
        mode="archive"
    )
    session.updates = {}

    # 按剧集分组
    show_groups: Dict[str, List[Any]] = {}
    for task in session.tasks:
        key = getattr(task, "showPath", "") or f"{task.category}/{task.name}"
        show_groups.setdefault(key, []).append(task)

    summary = {"total": len(show_groups), "archived": 0, "skipped": 0, "conflicts": 0, "errors": 0}
    show_index = 0

    for show_path, rows in show_groups.items():
        show_index += 1
        show_name = rows[0].name
        cat_name = rows[0].category
        logger.progress("归档", show_index, len(show_groups), f"{cat_name} / {show_name}")

        try:
            # 1. 检查是否存在未闭环的修复记录
            has_unfinished = any(is_repair_pending(read_repair_state(r.data)) for r in rows)
            if has_unfinished:
                for r in rows:
                    session.updates[r] = {
                        "state": "含未闭环修复记录",
                        "color": None,
                        "note": "本剧尚有未闭环的 MoviePilot 整理修复任务，需先复核确认文件，整剧暂缓归档",
                    }
                summary["skipped"] += 1
                continue

            # 2. 检查多级目标分类目录是否存在（递归建立并穿透刷新）
            target_cat_path = f"{dest}/{cat_name}"
            source_cat_path = f"{source}/{cat_name}"
            target_cat_exists = False
            try:
                target_cat_exists = ensure_dir_recursive(cd2, target_cat_path)
                if not target_cat_exists:
                    for r in rows:
                        session.updates[r] = {
                            "state": "缺少目标分类",
                            "color": None,
                            "note": f"归档目标分类 {target_cat_path} 不存在且自动创建失败，请先手动建立",
                        }
                    summary["skipped"] += 1
                    continue
            except Exception as e:
                for r in rows:
                    session.updates[r] = {"state": "归档目标不可达", "color": None, "note": str(e)}
                summary["errors"] += 1
                continue

            # 3. 检查目标是否已有同名剧集（穿透刷新获取最新目录，杜绝生成带 (1) 冲突文件夹）
            target_show_path = f"{dest}/{cat_name}/{show_name}"
            try:
                cat_contents = cd2.list_files(target_cat_path, force_refresh=True)
            except Exception as cat_err:
                for r in rows:
                    session.updates[r] = {"state": "读取目标分类失败", "color": None, "note": str(cat_err)}
                summary["errors"] += 1
                continue

            existing = next(
                (f for f in cat_contents if f.get("directory") and f.get("name", "").lower() == show_name.lower()),
                None
            )

            if existing and existing.get("name") != show_name:
                for r in rows:
                    session.updates[r] = {
                        "state": "名称大小写冲突",
                        "color": None,
                        "note": f"目标已有 \"{existing.get('name')}\"，与源 \"{show_name}\" 大小写不同",
                    }
                summary["conflicts"] += 1
                continue

            # 4. 完结齐全与增量季解耦判定
            non_green = [r for r in rows if r.color != "green"]
            # 若目标已有同名剧集（增量季合入）或开启了单季归档开关，允许绿色完结季增量合入，不被未完成季卡死
            is_incremental_allowed = bool(existing or allow_single_season)
            if non_green and not is_incremental_allowed:
                for r in rows:
                    session.updates[r] = {
                        "state": "整剧含非绿色季",
                        "color": None,
                        "note": f"{len(non_green)} 季未完成，整剧暂缓归档",
                    }
                summary["skipped"] += 1
                continue

            # 选定本次实际待归档的行任务
            target_rows = [r for r in rows if r.color == "green" and not is_repair_pending(read_repair_state(r.data))]
            if not target_rows:
                for r in rows:
                    session.updates[r] = {
                        "state": "无完结齐全季",
                        "color": None,
                        "note": "本剧暂无可归档的完结齐全季",
                    }
                summary["skipped"] += 1
                continue

            # 检查待归档行任务是否存在同季重复行
            target_seasons_num = [r.season for r in target_rows]
            if len(set(target_seasons_num)) != len(target_seasons_num):
                for r in target_rows:
                    session.updates[r] = {
                        "state": "同季重复", "color": None, "note": "同一季在报告中有多行"
                    }
                summary["errors"] += 1
                continue

            # 5. 执行迁移
            if existing:
                # ── 增量季合入模式 ──
                target_contents = cd2.list_files(target_show_path, force_refresh=True)
                source_contents = cd2.list_files(show_path, force_refresh=True)

                source_seasons = [f for f in source_contents if f.get("directory") and parse_season_number(f.get("name", "")) is not None]
                source_files = [f for f in source_contents if not f.get("directory")]
                target_seasons = [f for f in target_contents if f.get("directory") and parse_season_number(f.get("name", "")) is not None]

                # 仅迁移与 target_rows 对应的季目录
                target_seasons_set = set(target_seasons_num)
                seasons_to_move = [
                    s for s in source_seasons if parse_season_number(s.get("name", "")) in target_seasons_set
                ]

                # 检查同季冲突
                season_conflicts = []
                for s in seasons_to_move:
                    s_num = parse_season_number(s.get("name", ""))
                    hit = next(
                        (t for t in target_seasons if parse_season_number(t.get("name", "")) == s_num or t.get("name", "").lower() == s.get("name", "").lower()),
                        None
                    )
                    if hit:
                        season_conflicts.append(s.get("name"))
                        logger.warn(f"  ⚠️ 同季冲突: {s.get('name')} 目标已存在同季目录 \"{hit.get('name')}\"")

                if season_conflicts:
                    for r in target_rows:
                        session.updates[r] = {
                            "state": "同季冲突，整剧暂缓",
                            "color": None,
                            "note": f"目标已存在同季目录: {'、'.join(season_conflicts)}",
                        }
                    summary["conflicts"] += 1
                    continue

                files_to_move = []
                for f in source_files:
                    f_name = f.get("name", "")
                    exists_in_target = any(
                        not t.get("directory") and t.get("name", "").lower() == f_name.lower()
                        for t in target_contents
                    )
                    if not exists_in_target:
                        files_to_move.append(f)

                if dry_run:
                    logger.info(f"  📋 计划: 增量合入已有剧集目录 {target_show_path}")
                    for s in seasons_to_move:
                        logger.info(f"     ↳ 移入季目录: {s.get('name')} -> {target_show_path}/{s.get('name')}")
                    for r in target_rows:
                        session.updates[r] = {
                            "state": "待归档（合入预览）",
                            "color": None,
                            "note": f"将增量合入 {target_show_path}，迁移 {len(seasons_to_move)} 个季目录",
                            "source": r.path,
                            "target": f"{target_show_path}/{r.path.split('/')[-1]}",
                        }
                    continue

                # 执行实际增量合入
                move_failed = False
                for s in seasons_to_move:
                    try:
                        cd2.move_file(s["path"], target_show_path)
                        logger.info(f"  ✅ 已移动季目录: {s.get('name')}")
                        dest_season_path = f"{target_show_path}/{s.get('name')}"
                        cd2.refresh_dir(dest_season_path)
                    except Exception as s_err:
                        logger.error(f"  ❌ 移动季目录失败: {s.get('name')} - {s_err}")
                        move_failed = True
                        for r in target_rows:
                            session.updates[r] = {"state": "移动季失败", "color": None, "note": str(s_err)}
                        summary["errors"] += 1
                        break

                if move_failed:
                    continue

                for f in files_to_move:
                    try:
                        cd2.move_file(f["path"], target_show_path)
                    except Exception as f_err:
                        logger.warn(f"  ⚠️ 移动元数据失败: {f.get('name')} - {f_err}")

                try:
                    cd2.refresh_dir(target_show_path)
                except Exception:
                    pass

                # 源目录清理逻辑：穿透确认源目录是否仍有其他未迁移季
                try:
                    remaining = cd2.list_files(show_path, force_refresh=True)
                    rem_seasons = [f for f in remaining if f.get("directory") and parse_season_number(f.get("name", "")) is not None]
                    if rem_seasons:
                        rem_names = [s.get("name") for s in rem_seasons]
                        logger.info(f"  📁 源目录仍有未迁移/连载季 ({rem_names})，安全保留源剧集目录")
                    else:
                        cd2.delete_file(show_path)
                        logger.info(f"  🗑 源端已无任何季目录，已安全清理源剧集空壳: {show_path}")
                        cd2.refresh_dir(source_cat_path)
                except Exception as del_err:
                    logger.warn(f"  ⚠️ 检查/清理源剧集目录异常: {del_err}")

            else:
                # ── 全新整剧移动模式 ──
                if len(target_rows) == len(rows):
                    # 整剧所有季均完结：整剧目录直接移入目标分类
                    if dry_run:
                        logger.info(f"  📋 计划: 整剧移至 {target_cat_path}")
                        for r in rows:
                            session.updates[r] = {
                                "state": "待归档（整剧预览）",
                                "color": None,
                                "note": f"将移至 {target_show_path}",
                                "source": show_path,
                                "target": target_show_path,
                            }
                        continue

                    try:
                        cd2.move_file(show_path, target_cat_path)
                        logger.info(f"  ✅ 已移动整剧: {show_name}")
                        cd2.refresh_dir(target_show_path)
                        cd2.refresh_dir(target_cat_path)
                        cd2.refresh_dir(source_cat_path)
                    except Exception as m_err:
                        for r in rows:
                            session.updates[r] = {"state": "移动失败", "color": None, "note": str(m_err)}
                        summary["errors"] += 1
                        continue
                else:
                    # 单季归档模式（源端尚有连载季）：先在目标端建立剧集目录，仅迁入完结季
                    ensure_dir_recursive(cd2, target_show_path)
                    cd2.refresh_dir(target_show_path)
                    source_contents = cd2.list_files(show_path, force_refresh=True)
                    source_seasons = [f for f in source_contents if f.get("directory") and parse_season_number(f.get("name", "")) is not None]
                    target_seasons_set = set(target_seasons_num)
                    seasons_to_move = [s for s in source_seasons if parse_season_number(s.get("name", "")) in target_seasons_set]

                    if dry_run:
                        logger.info(f"  📋 计划: 单季先行归档至 {target_show_path}")
                        for s in seasons_to_move:
                            logger.info(f"     ↳ 移入季目录: {s.get('name')}")
                        for r in target_rows:
                            session.updates[r] = {
                                "state": "待归档（单季预览）",
                                "color": None,
                                "note": f"将先行归档至 {target_show_path}/{r.path.split('/')[-1]}",
                                "source": r.path,
                                "target": f"{target_show_path}/{r.path.split('/')[-1]}",
                            }
                        continue

                    move_failed = False
                    for s in seasons_to_move:
                        try:
                            cd2.move_file(s["path"], target_show_path)
                            logger.info(f"  ✅ 已移动季目录: {s.get('name')}")
                            cd2.refresh_dir(f"{target_show_path}/{s.get('name')}")
                        except Exception as s_err:
                            logger.error(f"  ❌ 移动季目录失败: {s.get('name')} - {s_err}")
                            move_failed = True
                            for r in target_rows:
                                session.updates[r] = {"state": "移动季失败", "color": None, "note": str(s_err)}
                            summary["errors"] += 1
                            break

                    if move_failed:
                        continue
                    cd2.refresh_dir(target_show_path)

            # 验证最终移动状态
            try:
                target_exists = cd2.exists(target_show_path)
                if target_exists:
                    for r in target_rows:
                        session.updates[r] = {
                            "state": "已归档",
                            "color": "archived",
                            "note": f"已归档至 {target_show_path}",
                            "source": show_path,
                            "target": target_show_path,
                            "at": datetime.now().isoformat(),
                        }
                    summary["archived"] += 1
                else:
                    for r in target_rows:
                        session.updates[r] = {
                            "state": "验证未通过",
                            "color": "yellow",
                            "note": "目标剧集目录不存在，请人工核实",
                        }
                    summary["errors"] += 1
            except Exception as v_err:
                for r in target_rows:
                    session.updates[r] = {"state": "验证异常", "color": "yellow", "note": str(v_err)}
                summary["errors"] += 1

        except Exception as e:
            for r in rows:
                session.updates[r] = {"state": "归档失败", "color": None, "note": str(e)}
            summary["errors"] += 1

    # ── 归档后对账复核 (Post-Archive Audit) ──
    review_summary: Optional[Dict[str, Any]] = None
    if not dry_run:
        try:
            review_summary = post_archive_review(
                config=config, logger=logger, cd2=cd2, session=session
            )
            summary["postReview"] = review_summary

            # 写入归档操作与对账快照审计日志
            if summary["archived"] > 0:
                try:
                    log_dir = Path("logs")
                    log_dir.mkdir(parents=True, exist_ok=True)
                    audit_file = log_dir / f"archive_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                    audit_data = {
                        "timestamp": datetime.now().isoformat(),
                        "archived_shows_count": summary["archived"],
                        "skipped_shows_count": summary["skipped"],
                        "conflicts_count": summary["conflicts"],
                        "errors_count": summary["errors"],
                        "post_review_summary": review_summary
                    }
                    with open(audit_file, "w", encoding="utf-8") as af:
                        json.dump(audit_data, af, ensure_ascii=False, indent=2)
                    logger.info(f"   📋 归档操作与对账审计快照已生成: {audit_file.resolve()}")
                except Exception as af_err:
                    logger.warn(f"   ⚠️ 写入归档审计日志跳过: {af_err}")

        except Exception as e:
            logger.error(f"❌ 归档后对账复核异常: {e}")

    # 写入报告（自动生成 .xlsx 与 .csv）
    suffix = "_归档预览" if dry_run else "_归档完成"
    output_path = re.sub(
        r"(?:_补缺下载|_复核预览|_复核清理|_归档预览|_归档完成)*\.xlsx$",
        f"{suffix}.xlsx",
        report_path,
        flags=re.IGNORECASE
    )
    write_report(session, output_path)

    action_label = "预览" if dry_run else ""
    logger.info(f"\n📊 归档{action_label}完成:")
    logger.info(
        f"   ✅ 已归档 {summary['archived']}  ⏭ 跳过 {summary['skipped']}  "
        f"⚠️ 冲突 {summary['conflicts']}  ❌ 错误 {summary['errors']}"
    )
    logger.info(f"   📄 报告: {output_path}")

    return {**summary, "outputPath": output_path}


def post_archive_review(
    config: Any,
    logger: Any,
    cd2: CloudDriveClient,
    session: Any,
    auto_clean_empty_aliases: bool = True
) -> Dict[str, Any]:
    """
    归档后全局对账复核 (Post-Archive Audit)

    1. 目标目录精准“集数守恒”对账（实到正片视频数与报告登记集数精确比对）
    2. 源目录存量物理对账
    3. 残留别名目录排查（兼容多格式 TMDB ID 与标题回退比对）与空别名原样清理
    """
    paths_cfg = config.get("paths", {}) if isinstance(config, dict) else getattr(config, "paths", {})
    source = paths_cfg.get("source", "").rstrip("/")
    dest = paths_cfg.get("destination", "").rstrip("/")

    line_sep = "═" * 52
    logger.info(f"\n{line_sep}")
    logger.info("🔍 开始执行: 归档后对账复核 (Post-Archive Audit)")
    logger.info(line_sep)

    show_groups: Dict[str, List[Any]] = {}
    for task in session.tasks:
        key = getattr(task, "showPath", "") or f"{task.category}/{task.name}"
        show_groups.setdefault(key, []).append(task)

    archived_list: List[Dict[str, Any]] = []
    skipped_list: List[Dict[str, Any]] = []
    categories: Set[str] = set()
    archived_tmdb_map: Dict[int, Dict[str, Any]] = {}
    archived_title_map: Dict[str, Dict[str, Any]] = {}

    for show_path, rows in show_groups.items():
        show_name = rows[0].name
        cat_name = rows[0].category
        categories.add(cat_name)

        update = session.updates.get(rows[0]) or {}
        is_archived = (
            update.get("state") == "已归档"
            or rows[0].data.get("处理结果") == "已归档"
            or rows[0].data.get("归档结果") == "已归档"
        )

        tmdb_id = extract_tmdb_id(rows[0].data.get("TMDB ID") or show_name)
        clean_title = extract_clean_title(show_name)

        item = {
            "showName": show_name,
            "catName": cat_name,
            "showPath": show_path,
            "tmdbId": tmdb_id,
            "cleanTitle": clean_title,
            "rows": rows
        }
        if is_archived:
            archived_list.append(item)
            if tmdb_id:
                archived_tmdb_map[tmdb_id] = item
            if clean_title:
                archived_title_map[clean_title] = item
        else:
            skipped_list.append(item)

    review_summary: Dict[str, Any] = {
        "targetVerified": 0,
        "targetFailed": 0,
        "sourceMatches": True,
        "cleanedAliases": [],
        "residualWarnings": [],
        "categories": {}
    }

    # 1. 目标目录精准“集数守恒”对账
    logger.info(f"\n[复核-1] 正在核验目标目录 ({len(archived_list)} 部已归档剧集)...")
    for item in archived_list:
        target_path = f"{dest}/{item['catName']}/{item['showName']}"
        try:
            target_contents = cd2.list_files(target_path, force_refresh=True)
            seasons = [f for f in target_contents if f.get("directory")]
            total_videos = 0
            season_video_map = {}
            for s in seasons:
                season_files = cd2.list_files(s["path"], force_refresh=True)
                v_count = sum(1 for sf in season_files if MEDIA_REGEX.search(sf.get("name", "")))
                total_videos += v_count
                s_num = parse_season_number(s.get("name", ""))
                if s_num is not None:
                    season_video_map[s_num] = v_count

            # 校验各季集数是否守恒
            season_mismatches = []
            for r in item["rows"]:
                exp_ep = 0
                for k in ("已识别集数", "来源登记集数", "视频文件数"):
                    val = r.data.get(k)
                    if val is not None and str(val).isdigit() and int(val) > 0:
                        exp_ep = int(val)
                        break
                s_num = r.season
                actual_ep = season_video_map.get(s_num, 0)
                if exp_ep > 0 and actual_ep < exp_ep:
                    season_mismatches.append(f"S{s_num}(实到{actual_ep}/应到{exp_ep})")

            if total_videos > 0 and not season_mismatches:
                review_summary["targetVerified"] += 1
                for r in item["rows"]:
                    prev_up = session.updates.get(r) or {}
                    prev_fields = prev_up.get("fields") or {}
                    prev_fields["归档复核"] = f"目标已核验 (完整 {len(seasons)} 季 {total_videos} 集，账实相符)"
                    prev_fields["复核核对时间"] = datetime.now().isoformat()
                    prev_up["fields"] = prev_fields
                    session.updates[r] = prev_up
            else:
                review_summary["targetFailed"] += 1
                err_desc = f"集数不守恒: {', '.join(season_mismatches)}" if season_mismatches else "未发现有效视频文件"
                logger.warn(f"  ⚠️ 目标完整性异常: {item['showName']} - {err_desc}")
                for r in item["rows"]:
                    prev_up = session.updates.get(r) or {}
                    prev_fields = prev_up.get("fields") or {}
                    prev_fields["归档复核"] = f"⚠️ 完整性异常: {err_desc}"
                    prev_fields["复核核对时间"] = datetime.now().isoformat()
                    prev_up["fields"] = prev_fields
                    session.updates[r] = prev_up
        except Exception as e:
            review_summary["targetFailed"] += 1
            logger.error(f"  ❌ 目标目录不可达: {item['showName']} - {e}")
            for r in item["rows"]:
                prev_up = session.updates.get(r) or {}
                prev_fields = prev_up.get("fields") or {}
                prev_fields["归档复核"] = f"❌ 目标不存在: {e}"
                prev_fields["复核核对时间"] = datetime.now().isoformat()
                prev_up["fields"] = prev_fields
                session.updates[r] = prev_up

    # 2. 源目录存量对账与别名残留排查
    logger.info("\n[复核-2] 正在对账源目录存量与排查别名残留...")
    for cat_name in categories:
        source_cat_path = f"{source}/{cat_name}"
        dest_cat_path = f"{dest}/{cat_name}"

        try:
            src_list = cd2.list_files(source_cat_path, force_refresh=True)
            source_dirs = [f for f in src_list if f.get("directory")]
        except Exception as e:
            logger.error(f"  ❌ 无法读取源分类目录: {source_cat_path} - {e}")
            continue

        dest_all_tmdb_map: Dict[int, str] = {}
        dest_all_title_map: Dict[str, str] = {}
        try:
            dest_list = cd2.list_files(dest_cat_path, force_refresh=True)
            for d in dest_list:
                if d.get("directory"):
                    t_id = extract_tmdb_id(d.get("name", ""))
                    if t_id:
                        dest_all_tmdb_map[t_id] = d.get("name", "")
                    c_title = extract_clean_title(d.get("name", ""))
                    if c_title:
                        dest_all_title_map[c_title] = d.get("name", "")
        except Exception:
            pass

        expected_skipped = [s for s in skipped_list if s["catName"] == cat_name]
        cat_result = {
            "sourceActualCount": len(source_dirs),
            "expectedCount": len(expected_skipped),
            "unmovedShows": [],
            "cleanedAliases": []
        }

        # 检查是否有本应归档但仍留在源目录的剧集
        for a in [it for it in archived_list if it["catName"] == cat_name]:
            if any(d.get("name") == a["showName"] for d in source_dirs):
                cat_result["unmovedShows"].append(a["showName"])
                logger.error(f"  ❌ 异常残留: 剧集 \"{a['showName']}\" 已标记归档但仍在源目录！")

        effective_tmdb_map = dict(dest_all_tmdb_map)
        for t_id, item in archived_tmdb_map.items():
            effective_tmdb_map[t_id] = item["showName"]

        effective_title_map = dict(dest_all_title_map)
        for c_title, item in archived_title_map.items():
            effective_title_map[c_title] = item["showName"]

        need_refresh = False
        for d in source_dirs:
            d_name = d.get("name", "")
            d_tmdb = extract_tmdb_id(d_name)
            d_title = extract_clean_title(d_name)

            # 别名判定：TMDB 优先匹配，标题归一化回退匹配
            target_match_name = None
            if d_tmdb and d_tmdb in effective_tmdb_map:
                target_match_name = effective_tmdb_map[d_tmdb]
            elif d_title and d_title in effective_title_map:
                target_match_name = effective_title_map[d_title]

            if target_match_name and target_match_name != d_name:
                video_count = 0
                try:
                    sub_items = cd2.list_files(d["path"], force_refresh=True)
                    for sub in sub_items:
                        if sub.get("directory"):
                            season_items = cd2.list_files(sub["path"], force_refresh=True)
                            video_count += sum(1 for sf in season_items if MEDIA_REGEX.search(sf.get("name", "")))
                        elif MEDIA_REGEX.search(sub.get("name", "")):
                            video_count += 1
                except Exception:
                    video_count = -1

                # 原样保留既有空别名清理判定
                if video_count == 0 and auto_clean_empty_aliases:
                    try:
                        cd2.delete_file(d["path"])
                        cat_result["cleanedAliases"].append(d_name)
                        review_summary["cleanedAliases"].append({
                            "category": cat_name, "name": d_name, "targetName": target_match_name, "tmdbId": d_tmdb
                        })
                        logger.info(f"  🗑 已自动清理源端残留空别名目录: {d_name} (目标已有完整归档: {target_match_name})")
                        need_refresh = True
                    except Exception as del_err:
                        logger.warn(f"  ⚠️ 清理空别名目录失败: {d_name} - {del_err}")
                elif video_count > 0:
                    warning = {
                        "category": cat_name, "name": d_name, "targetName": target_match_name,
                        "tmdbId": d_tmdb, "videoCount": video_count
                    }
                    review_summary["residualWarnings"].append(warning)
                    logger.warn(
                        f"  ⚠️ 发现源端残留别名剧集: \"{d_name}\" (含 {video_count} 集)，"
                        f"目标已有归档 \"{target_match_name}\"，建议人工确认"
                    )

        if need_refresh:
            try:
                cd2.refresh_dir(source_cat_path)
                ref_list = cd2.list_files(source_cat_path, force_refresh=True)
                cat_result["sourceActualCount"] = len([f for f in ref_list if f.get("directory")])
            except Exception:
                pass

        review_summary["categories"][cat_name] = cat_result

    # 输出复核对账汇总
    logger.info(f"\n{line_sep}")
    logger.info("📊 归档后对账复核汇总 (Post-Archive Reconciliation)")
    logger.info(line_sep)
    ver_status = f" (❌ 异常 {review_summary['targetFailed']} 部)" if review_summary["targetFailed"] > 0 else " (100% 完整)"
    logger.info(f"  🎯 目标完整性核验: {review_summary['targetVerified']}/{len(archived_list)} 通过{ver_status}")

    for cat_name, cat_res in review_summary["categories"].items():
        is_matched = (cat_res["sourceActualCount"] == cat_res["expectedCount"] and len(cat_res["unmovedShows"]) == 0)
        match_desc = "(✅ 对账完全一致)" if is_matched else "(⚠️ 存量存在差异)"
        logger.info(
            f"  📂 分类 [{cat_name}] 源目录存量: 实际 {cat_res['sourceActualCount']} 部 / "
            f"预期 {cat_res['expectedCount']} 部 {match_desc}"
        )
        if cat_res["unmovedShows"]:
            logger.error(f"     ↳ ❌ 未成功移走剧集: {', '.join(cat_res['unmovedShows'])}")

    if review_summary["cleanedAliases"]:
        logger.info(f"  🗑 自动清理空别名残留: {len(review_summary['cleanedAliases'])} 部:")
        for c in review_summary["cleanedAliases"]:
            logger.info(f"     ↳ 已删除: {c['name']}")

    if review_summary["residualWarnings"]:
        logger.info(f"  ⚠️ 别名残留需人工确认: {len(review_summary['residualWarnings'])} 部:")
        for w in review_summary["residualWarnings"]:
            logger.info(f"     ↳ 源端: {w['name']} (含 {w['videoCount']} 集) | 目标端: {w['targetName']}")

    logger.info(f"{line_sep}\n")
    return review_summary
