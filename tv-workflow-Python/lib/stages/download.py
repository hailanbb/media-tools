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


def is_season_pack(meta: Dict[str, Any], t: Dict[str, Any], task_season: int, total: int) -> bool:
    """精准判断是否为整季资源包"""
    eps = meta.get("episode_list") or []
    # 1. 明确包含所有集数
    if total > 0 and len(eps) == total:
        return True
    # 2. MoviePilot 明确标明总集数等于该季总集数且无特定单集
    total_ep = meta.get("total_episode")
    if total > 0 and total_ep == total and len(eps) == 0:
        return True

    # 3. 整季特征：季号匹配，且无任何单集/起止集标记
    begin_season = meta.get("begin_season")
    season_list = meta.get("season_list") or ([begin_season] if begin_season else [])
    if len(season_list) == 1 and season_list[0] == task_season:
        begin_ep = meta.get("begin_episode")
        end_ep = meta.get("end_episode")
        season_ep = str(meta.get("season_episode") or "").upper().strip()

        # season_episode 仅为 S02 / S2 等季标识，无 E 部分
        is_season_only = bool(re.match(r"^S\d{1,2}$", season_ep))

        if (begin_ep is None or begin_ep == 0) and (end_ep is None or end_ep == 0) and len(eps) == 0:
            title = str(t.get("title") or "")
            sub = str(meta.get("subtitle") or t.get("description") or "")
            combined = f"{title} {sub}"
            if not re.search(r"(?:[Ee][Pp]?\s*\d+|第\s*\d+\s*集|\b\d+\s*集\b)", combined):
                return True
            if is_season_only and not re.search(r"[Ee][Pp]?\s*\d+", title):
                return True

    return False


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

            # 2. 追加专属站点搜索（默认包含: 观众、天空、馒头）
            extra_site_names = mp_cfg.get("extraSearchSites", ["观众", "天空", "馒头"])
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
                is_full = is_season_pack(meta, t, task.season, total)

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

            # 区分整季包与散集多候选选取：
            season_packs = [c for c in eligible if is_season_pack(c.get("meta_info") or {}, c.get("torrent_info") or {}, task.season, total)]
            selected_candidates: List[Tuple[Dict[str, Any], List[int], bool]] = []  # (candidate, hits, is_full)

            if season_packs:
                # 存在整季包时，按整季规则优选最佳整季包
                def score_season_pack(c: Dict[str, Any]) -> Tuple[int, int, float, int]:
                    t = c.get("torrent_info") or {}
                    try:
                        seeds = int(t.get("seeders", 0))
                    except (ValueError, TypeError):
                        seeds = 0
                    try:
                        size = float(t.get("size", float("inf")))
                    except (ValueError, TypeError):
                        size = float("inf")
                    has_chinese = 1 if re.search(r"中文|中字|简中|繁中|国语|chs|cht|简日|繁日|双语|baha|巴哈|简繁|内封|mikan", json.dumps(t.get("labels") or [], ensure_ascii=False) + str(t.get("title") or ""), re.I) else 0
                    seeds_ge_5 = 1 if seeds >= 5 else 0
                    return (-has_chinese, -seeds_ge_5, size, -seeds)

                season_packs.sort(key=score_season_pack)
                best_sp = season_packs[0]
                selected_candidates.append((best_sp, list(range(1, total + 1)), True))
            else:
                # 无整季包时：贪心多候选组合算法，完整覆盖所有可获取的缺失单集/分卷包
                remaining_needed = set(needed) if needed else set()
                while remaining_needed:
                    candidates_for_remaining: List[Tuple[Dict[str, Any], List[int]]] = []
                    for c in eligible:
                        meta = c.get("meta_info") or {}
                        eps = meta.get("episode_list") or []
                        hits = [ep for ep in eps if ep in remaining_needed]
                        if hits:
                            candidates_for_remaining.append((c, hits))

                    if not candidates_for_remaining:
                        break

                    def score_rem(item: Tuple[Dict[str, Any], List[int]]) -> Tuple[int, int, int, float, int]:
                        cand, hits = item
                        t = cand.get("torrent_info") or {}
                        try:
                            seeds = int(t.get("seeders", 0))
                        except (ValueError, TypeError):
                            seeds = 0
                        try:
                            size = float(t.get("size", float("inf")))
                        except (ValueError, TypeError):
                            size = float("inf")
                        has_chinese = 1 if re.search(r"中文|中字|简中|繁中|国语|chs|cht|简日|繁日|双语|baha|巴哈|简繁|内封|mikan", json.dumps(t.get("labels") or [], ensure_ascii=False) + str(t.get("title") or ""), re.I) else 0
                        seeds_ge_5 = 1 if seeds >= 5 else 0
                        # 命中缺失集数多优先，中文优先，做种>=5优先，小容量优先，做种多优先
                        return (-len(hits), -has_chinese, -seeds_ge_5, size, -seeds)

                    candidates_for_remaining.sort(key=score_rem)
                    best_cand, best_hits = candidates_for_remaining[0]
                    selected_candidates.append((best_cand, best_hits, False))
                    remaining_needed -= set(best_hits)

            if not selected_candidates:
                logger.warn(f"  ⚠️ 未能匹配到可覆盖缺集的资源")
                session.updates[task] = {
                    "state": "无合格资源",
                    "color": "red",
                    "note": f"检索到 {len(contexts)} 条，但无资源能覆盖缺失集号",
                }
                summary["noResource"] += 1
                continue

            # 批量或逐个向 MoviePilot 提交下载
            submitted_recs: List[Dict[str, Any]] = []
            all_covered_eps: Set[int] = set()

            for cand, hits, is_sp in selected_candidates:
                cand_t = cand.get("torrent_info") or {}
                cand_meta = cand.get("meta_info") or {}
                title = str(cand_t.get("title") or "")
                site = str(cand_t.get("site_name") or "")
                seeds = int(cand_t.get("seeders", 0))
                desc_type = "整季" if is_sp else f"缺集 {hits}"

                # 排除历史已接受的相同种子
                t_key = f"{site}:{title}:{cand_t.get('size')}"
                if any(r.get("key") == t_key and r.get("state") == "accepted" for r in records):
                    logger.debug(f"  ⏭ 排除已提交过相同种子: {title}")
                    continue

                logger.info(f"  🎯 选定最佳资源: [{site}] {title} (做种: {seeds}, {desc_type})")
                try:
                    logger.info(f"  🚀 正在向 MoviePilot 提交下载请求: {title}...")
                    reply = mp.submit_download(cand.get("media_info") or {}, cand_t)
                    is_success = bool(reply.get("success", False)) if isinstance(reply, dict) else False

                    c_covered = list(range(1, total + 1)) if is_sp else (cand_meta.get("episode_list") or hits)
                    rec = {
                        "key": t_key,
                        "state": "accepted" if is_success else "rejected",
                        "id": (reply.get("data") or {}).get("download_id", "") if isinstance(reply, dict) else "",
                        "title": title,
                        "site": site,
                        "episodes": c_covered,
                        "seeds": seeds,
                        "at": datetime.now().isoformat(),
                    }
                    records.append(rec)
                    store.put(store_key, records)

                    if is_success:
                        logger.info(f"  ✅ MoviePilot 已接受下载任务: {title}")
                        submitted_recs.append(rec)
                        all_covered_eps.update(c_covered)
                        summary["submitted"] += 1
                    else:
                        msg = reply.get("message", "未知原因") if isinstance(reply, dict) else "未知原因"
                        logger.error(f"  ❌ MoviePilot 拒绝添加下载: {msg}")
                        summary["errors"] += 1
                except Exception as sub_err:
                    logger.error(f"  💥 提交下载异常 [{title}]: {sub_err}")
                    summary["errors"] += 1

            if submitted_recs:
                if len(submitted_recs) == 1:
                    first = submitted_recs[0]
                    first_is_sp = selected_candidates[0][2]
                    desc = "整季" if first_is_sp else "零散"
                    note_str = f"{first['title']} (做种 {first['seeds']}, {desc})"
                else:
                    note_str = f"已提交 {len(submitted_recs)} 个资源补缺，共覆盖 {len(all_covered_eps)} 集"

                session.updates[task] = {
                    "state": "已提交下载，待复核",
                    "color": "yellow",
                    "note": note_str,
                    "records": submitted_recs,
                    "covered": sorted(list(all_covered_eps)),
                    "repair": read_repair_state(task.data) or None,
                    "at": submitted_recs[0]["at"],
                }
            elif not submitted_recs and any(c for c in selected_candidates):
                session.updates[task] = {
                    "state": "下载提交失败",
                    "color": None,
                    "note": "MoviePilot 拒绝添加下载任务",
                    "records": records[-len(selected_candidates):],
                }

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
