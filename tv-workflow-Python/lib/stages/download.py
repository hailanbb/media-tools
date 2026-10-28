"""
阶段二：补缺下载（含 MoviePilot 整理历史修复联动与专属站点追加搜索）

核心流程：
1. 读取初检报告中的缺失项
2. 【整理修复优先】检查 MoviePilot 整理历史：若已下载但转移受阻，自动解除单条历史并重新触发转移
3. 对需下载项，搜索默认站点并追加专属站点（如“观众”、“天空”），按多维规则打分优选并提交下载
4. 写回更新报告并保存防重复记录到持久化存储
"""

from datetime import datetime
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ..clients.moviepilot import MoviePilotClient
from ..config import PROJECT_ROOT
from ..core.episode_parser import parse_episode_field, positive, uniq
from ..core.repair_state import read_repair_state
from ..core.report_adapter import read_report, write_report
from ..core.title_matcher import clean_title_text, create_normalizer
from ..core.transfer_repair import check_repair, execute_repair
from ..platform_support.data_store import create_data_store

norm = create_normalizer()


def run_download(
    config: Any,
    logger: Any,
    report_path: str,
    options: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """执行补缺下载与整理修复"""
    if not report_path:
        raise ValueError("请指定初检报告路径")

    opts = options or {}
    mp_cfg = config.get("moviepilot", {}) if isinstance(config, dict) else getattr(config, "moviepilot", {})
    cd2_cfg = config.get("clouddrive2", {}) if isinstance(config, dict) else getattr(config, "clouddrive2", {})
    paths_cfg = config.get("paths", {}) if isinstance(config, dict) else getattr(config, "paths", {})

    mp = MoviePilotClient(
        origin=mp_cfg.get("origin", ""),
        token=mp_cfg.get("token", ""),
        logger=logger,
    )
    store = create_data_store("data")

    logger.info("📄 读取初检报告...")
    session = read_report(
        file_path=report_path,
        origin=cd2_cfg.get("origin", ""),
        source=paths_cfg.get("source", ""),
        mode="download"
    )
    logger.info(f"📄 待处理: {len(session.tasks)} 项, 已跳过: {session.skipped} 项绿色/已归档")
    if session.errors:
        logger.warn(f"⚠️ 读取错误 {len(session.errors)} 项")

    summary = {
        "total": len(session.tasks),
        "repaired": 0,
        "submitted": 0,
        "existing": 0,
        "noResource": 0,
        "errors": 0
    }
    session.updates = {}

    transfer_repair_cfg = config.get("transferRepair", {}) if isinstance(config, dict) else getattr(config, "transferRepair", {})
    repair_config = {
        "source": transfer_repair_cfg.get("source") or paths_cfg.get("downloadPath", "/video/downloads/电视剧"),
        "target": transfer_repair_cfg.get("target") or paths_cfg.get("source", "/115/Library_incs/nas/电视剧")
    }

    # 逐项处理
    for i, task in enumerate(session.tasks, start=1):
        d = task.data
        logger.progress("补缺", i, len(session.tasks), f"{task.name} Season {task.season}")

        try:
            total_raw = d.get("来源登记集数")
            try:
                total = int(total_raw)
            except (ValueError, TypeError):
                total = 0

            if not positive(total):
                session.updates[task] = {
                    "state": "来源集数未确认", "color": None, "note": "请先补充初检结果"
                }
                summary["errors"] += 1
                continue

            raw_name = task.name
            clean_name = re.sub(r"\[tmdbid=\d+\]", "", raw_name, flags=re.IGNORECASE)
            clean_name = re.sub(r"[（(](?:19|20)\d{2}[)）]", "", clean_name)
            title = clean_title_text(clean_name)

            m_year = re.search(r"[（(]((?:19|20)\d{2})[)）]", raw_name)
            year = m_year.group(1) if m_year else ""

            m_tmdb = re.search(r"\[tmdbid=(\d+)\]", raw_name, re.IGNORECASE)
            tmdb_id = m_tmdb.group(1) if m_tmdb else str(d.get("TMDB ID") or "").strip()

            missing = parse_episode_field(d.get("缺失集号"), blank_unknown=True)
            future = parse_episode_field(d.get("待播集号") or "")
            undated = parse_episode_field(d.get("播期未知集号") or "")
            excluded = uniq((future or []) + (undated or []))
            needed = [n for n in missing if n not in excluded] if missing is not None else None

            task.title = title
            task.year = year
            task.tmdb = tmdb_id
            task.needed = needed

            # 无缺集时跳过
            if needed is not None and len(needed) == 0:
                session.updates[task] = {
                    "state": "无需下载", "color": None, "note": "报告没有已确认的缺失集号"
                }
                continue

            # ──【整理修复联动】优先检查 MoviePilot 整理历史 ──
            repaired_this_turn = False
            if not opts.get("skipRepair") and tmdb_id and needed:
                try:
                    repair_plan = check_repair(task, mp, repair_config)
                    if repair_plan.get("state") == "ready" and repair_plan.get("items"):
                        items_count = len(repair_plan["items"])
                        logger.info(f"  🔧 发现 {items_count} 个转移受阻集目，正在自动修复...")

                        def save_rec(rec: Dict[str, Any]) -> None:
                            store.put(f"repair:{norm(task.name)}:{task.season}", rec)

                        repair_result = execute_repair(task, repair_plan, mp, repair_config, save=save_rec)
                        rep_eps = "、".join(f"E{it['episode']}" for it in repair_plan["items"])

                        session.updates[task] = {
                            "state": "已提交重转，待复核",
                            "color": "yellow",
                            "repair": repair_result,
                            "note": f"已重新触发 {rep_eps} 整理转移",
                            "at": datetime.now().isoformat(),
                        }
                        summary["repaired"] += 1
                        repaired_this_turn = True
                except Exception as re_err:
                    logger.debug(f"  整理修复检查跳过: {re_err}")

            if repaired_this_turn or opts.get("repairOnly"):
                continue

            # ── 正常搜索与下载流程 ──
            store_key = f"dl:{norm(task.name)}:{task.season}"
            records = store.get(store_key, [])
            accepted = [r for r in records if r.get("state") in ("accepted", "existing")]

            # 若历史防重记录已覆盖全部缺集
            if needed is not None and all(any(n in (r.get("episodes") or []) for r in accepted) for n in needed):
                session.updates[task] = {
                    "state": "已提交下载，待复核",
                    "color": "yellow",
                    "note": "防重复记录已覆盖缺集",
                    "records": accepted,
                    "repair": read_repair_state(task.data) or None
                }
                summary["existing"] += 1
                continue

            # 1. 搜索 MoviePilot 默认站点
            logger.debug(f"  搜索默认站点资源: tmdb={tmdb_id or '无'}, title={title}")
            contexts: List[Dict[str, Any]] = []
            try:
                def_results = mp.search_media(tmdb_id, task.season, title, year)
                if isinstance(def_results, list):
                    contexts.extend(def_results)
            except Exception as e:
                logger.warn(f"  ⚠️ 默认站点搜索异常: {e}")

            # 2. 追加专属站点搜索（默认包含: 观众、天空）
            extra_site_names = mp_cfg.get("extraSearchSites", ["观众", "天空"])
            if isinstance(extra_site_names, list) and extra_site_names:
                try:
                    extra_site_ids = mp.resolve_site_ids(extra_site_names)
                    if extra_site_ids:
                        site_label = "、".join(str(s) for s in extra_site_names)
                        logger.info(f"  🔍 追加搜索专属站点 [{site_label}]: tmdb={tmdb_id or '无'}, title={title}")
                        extra_results = mp.search_media(tmdb_id, task.season, title, year, {"sites": extra_site_ids})
                        if isinstance(extra_results, list) and extra_results:
                            prev_count = len(contexts)
                            seen_keys: Set[str] = set()
                            for c in contexts:
                                t = c.get("torrent_info") or {}
                                k = t.get("enclosure") or t.get("page_url") or f"{t.get('site_name')}:{t.get('title')}:{t.get('size')}"
                                seen_keys.add(k)

                            for ec in extra_results:
                                et = ec.get("torrent_info") or {}
                                ek = et.get("enclosure") or et.get("page_url") or f"{et.get('site_name')}:{et.get('title')}:{et.get('size')}"
                                if ek not in seen_keys:
                                    seen_keys.add(ek)
                                    contexts.append(ec)

                            added_count = len(contexts) - prev_count
                            logger.info(f"     ↳ 从 [{site_label}] 成功追加 {added_count} 条资源 (去重后总计 {len(contexts)} 条)")
                except Exception as extra_err:
                    logger.warn(f"  ⚠️ 追加专属站点搜索跳过: {extra_err}")

            if not contexts:
                session.updates[task] = {
                    "state": "无合格资源", "color": "red", "note": "未找到匹配资源"
                }
                summary["noResource"] += 1
                continue

            # 筛选做种数 >= 2、季号匹配且能有效补缺的候选
            eligible: List[Dict[str, Any]] = []
            for c in contexts:
                m = c.get("media_info")
                meta = c.get("meta_info") or {}
                t = c.get("torrent_info")
                if not t or not m:
                    continue

                seasons = meta.get("season_list") or ([meta.get("begin_season")] if meta.get("begin_season") else [])
                if len(seasons) != 1 or seasons[0] != task.season:
                    continue

                try:
                    seeders = int(t.get("seeders", 0))
                except (ValueError, TypeError):
                    seeders = 0

                if seeders < 2:
                    continue

                # 提取集号与整季属性
                eps = meta.get("episode_list") or []
                is_full = (len(eps) == total) or (meta.get("total_episode") == total and len(eps) == 0)

                # 核心缺集过滤规则：
                # 1) 如果是整季包，允许整季替换以补全缺集；
                # 2) 如果是散集/单集，必须至少命中 1 集 needed 中的缺集！
                #    若该种子只包含本地已有的集数（如本地已有第 9 集且缺 3/4/8，种子仅包含第 9 集），坚决剔除！
                if needed is not None and len(needed) > 0:
                    if not is_full:
                        if eps:
                            hit_needed = [ep for ep in eps if ep in needed]
                            if not hit_needed:
                                logger.debug(f"  ⏭ 排除无用散集: {t.get('title')} (包含集号 {eps} 均不属于缺集 {needed})")
                                continue
                        else:
                            # 既不是整季包，又无法识别出集号的未知资源，防止盲目下载非缺失集
                            logger.debug(f"  ⏭ 排除未知集号散集: {t.get('title')}")
                            continue

                # 排除历史防重记录中已成功提交过的完全相同种子
                t_key = f"{t.get('site_name')}:{t.get('title')}:{t.get('size')}"
                if any(r.get("key") == t_key and r.get("state") == "accepted" for r in records):
                    logger.debug(f"  ⏭ 排除已提交过相同种子: {t.get('title')}")
                    continue

                eligible.append(c)

            if not eligible:
                logger.warn(f"  ⚠️ 检索到 {len(contexts)} 条资源，但做种数不足 2、季号不符或均不包含缺集，未选定有效资源")
                session.updates[task] = {
                    "state": "无合格资源",
                    "color": "red",
                    "note": f"检索到 {len(contexts)} 条，做种不足 2、季号不符或均不包含缺集"
                }
                summary["noResource"] += 1
                continue

            # 打分排序（整季优先、命中缺集数多优先、中文优先、做种≥5优先、小容量优先、做种数多优先）
            def score_candidate(c: Dict[str, Any]) -> Tuple[int, int, int, int, float, int]:
                t = c.get("torrent_info") or {}
                meta = c.get("meta_info") or {}
                try:
                    seeds = int(t.get("seeders", 0))
                except (ValueError, TypeError):
                    seeds = 0
                try:
                    size = float(t.get("size", float("inf")))
                except (ValueError, TypeError):
                    size = float("inf")

                eps = meta.get("episode_list") or []
                is_full = 1 if (len(eps) == total or (meta.get("total_episode") == total and len(eps) == 0)) else 0
                hit_count = len([ep for ep in eps if ep in needed]) if (needed and eps) else (total if is_full else 0)
                has_chinese = 1 if re.search(r"中文|中字|简中|繁中|国语|chs|cht|简日|繁日|双语|baha|巴哈|简繁|内封|mikan", json.dumps(t.get("labels") or [], ensure_ascii=False) + str(t.get("title") or ""), re.I) else 0
                seeds_ge_5 = 1 if seeds >= 5 else 0

                # Python sort 升序：取负数使其排在前面
                return (-is_full, -hit_count, -has_chinese, -seeds_ge_5, size, -seeds)

            eligible.sort(key=score_candidate)
            best_c = eligible[0]
            best_t = best_c.get("torrent_info") or {}
            best_meta = best_c.get("meta_info") or {}
            best_title = str(best_t.get("title") or "")
            best_site = str(best_t.get("site_name") or "")
            best_seeds = int(best_t.get("seeders", 0))
            best_eps = best_meta.get("episode_list") or []
            is_full_desc = "整季" if len(best_eps) == total else "零散"

            logger.info(f"  🎯 选定最佳资源: [{best_site}] {best_title} (做种: {best_seeds}, {is_full_desc})")

            # 提交下载
            try:
                logger.info(f"  🚀 正在向 MoviePilot 提交下载请求...")
                reply = mp.submit_download(best_c.get("media_info") or {}, best_t)
                is_success = bool(reply.get("success", False)) if isinstance(reply, dict) else False

                rec = {
                    "key": f"{best_site}:{best_title}:{best_t.get('size')}",
                    "state": "accepted" if is_success else "rejected",
                    "id": (reply.get("data") or {}).get("download_id", "") if isinstance(reply, dict) else "",
                    "title": best_title,
                    "site": best_site,
                    "episodes": best_eps,
                    "seeds": best_seeds,
                    "at": datetime.now().isoformat(),
                }
                records.append(rec)
                store.put(store_key, records)

                if is_success:
                    logger.info(f"  ✅ MoviePilot 已接受下载任务: {best_title}")
                    session.updates[task] = {
                        "state": "已提交下载，待复核",
                        "color": "yellow",
                        "note": f"{best_title} (做种 {best_seeds})",
                        "records": [rec],
                        "covered": best_eps,
                        "repair": read_repair_state(task.data) or None,
                        "at": rec["at"],
                    }
                    summary["submitted"] += 1
                else:
                    msg = reply.get("message", "未知原因") if isinstance(reply, dict) else "未知原因"
                    logger.error(f"  ❌ MoviePilot 拒绝添加下载: {msg}")
                    session.updates[task] = {
                        "state": "下载提交失败",
                        "color": None,
                        "note": f"MoviePilot 拒绝: {msg}",
                        "records": [rec],
                    }
                    summary["errors"] += 1
            except Exception as e:
                logger.error(f"  💥 提交下载异常: {e}")
                session.updates[task] = {"state": "提交异常", "color": None, "note": str(e)}
                summary["errors"] += 1

        except Exception as e:
            session.updates[task] = {"state": "处理失败", "color": None, "note": str(e)}
            summary["errors"] += 1

    # 写入更新报告
    output_path = re.sub(r"\.xlsx$", "_补缺下载.xlsx", report_path, flags=re.IGNORECASE)
    write_report(session, output_path)

    logger.info("\n📊 补缺下载与整理修复完成:")
    logger.info(
        f"   🔧 自动修复重转 {summary['repaired']}  ✅ 新增下载 {summary['submitted']}  "
        f"📦 现有任务 {summary['existing']}  🔴 无资源 {summary['noResource']}  ❌ 错误 {summary['errors']}"
    )
    logger.info(f"   📄 报告: {output_path}")

    return {**summary, "outputPath": output_path}
