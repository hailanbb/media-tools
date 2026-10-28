"""
阶段二：补缺下载（含 MoviePilot 整理历史修复联动、活跃下载队列实时对齐与多维精准选种）

核心流程：
1. 读取初检报告中的缺失项
2. 【整理修复优先】检查 MoviePilot 整理历史：若已下载但转移受阻，自动解除单条历史并重新触发转移
3. 【实时下载队列对齐】查询 MoviePilot 活跃下载任务，若缺集已在下载中则避免重复提交，仅补齐剩余缺集
4. 对需下载项，搜索默认站点并追加专属站点（如“观众”、“天空”、“馒头”），按多维规则打分优选：
   - 整季包能效比决策（Yield Ratio）：缺集少且散集可100%覆盖时优先散集，避免下几十G大包
   - 散集贪心多候选覆盖：清晰度梯队（优先1080p/2160p，惩罚720p/SD）、单集健康码率（200MB~3.5GB）、冗余惩罚、发布组一致性
   - 频控与提交熔断：连续提交间隔 0.5s，单季上限 15 个任务
5. 写回更新报告并保存防重复记录到持久化存储
"""

from datetime import datetime
import json
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union

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


def format_ranges(eps: Union[List[int], Set[int]]) -> str:
    """将数字列表折叠成紧凑格式，如 [10, 12, 13, 14, 15] -> '10、12-15'"""
    if not eps:
        return ""
    sorted_eps = sorted(set(eps))
    ranges = []
    start = sorted_eps[0]
    prev = sorted_eps[0]
    for n in sorted_eps[1:]:
        if n == prev + 1:
            prev = n
        else:
            ranges.append(f"{start}-{prev}" if start != prev else f"{start}")
            start = n
            prev = n
    ranges.append(f"{start}-{prev}" if start != prev else f"{start}")
    return "、".join(ranges)


def parse_active_episodes(ep_str: str, total: int = 0) -> Set[int]:
    """解析 MoviePilot active download 中的集数字符串（如 'E01-E06', 'E12', ''）"""
    ep_str = str(ep_str or "").strip()
    if not ep_str:
        return set(range(1, total + 1)) if total > 0 else set()
    eps: Set[int] = set()
    for m in re.finditer(r"(?:E|EP)?(\d+)(?:\s*[-~～至到_\ufffd]\s*(?:E|EP)?(\d+))?", ep_str, re.IGNORECASE):
        start = int(m.group(1))
        if m.group(2):
            end = int(m.group(2))
            if start <= end:
                eps.update(range(start, end + 1))
            else:
                eps.update(range(end, start + 1))
        else:
            eps.add(start)
    return eps


def get_resolution_score(t: Dict[str, Any], meta: Dict[str, Any]) -> int:
    """解析清晰度并计算画质梯队得分（优先 1080p/2160p，惩罚 720p/480p）"""
    text = f"{t.get('title', '')} {meta.get('resource_pix', '')} {' '.join(t.get('labels') or [])}".lower()
    if re.search(r"2160p|4k|uhd", text):
        return 900
    if re.search(r"1080p|1080i|fhd", text):
        return 1000
    if re.search(r"720p|hd(?![a-z0-9])", text):
        return -500
    if re.search(r"480p|576p|sd(?![a-z0-9])", text):
        return -1000
    return 0


def get_bitrate_health_score(size_bytes: float, ep_count: int) -> int:
    """健康码率区间奖励：单集 200MB~3.5GB 属于标准画质甜点区间"""
    if ep_count <= 0 or size_bytes <= 0 or size_bytes == float("inf"):
        return 0
    size_mb = (size_bytes / ep_count) / (1024 * 1024)
    if 200 <= size_mb <= 3500:
        return 200
    if size_mb < 80:
        return -600  # 码率过低压制版或残次种
    if size_mb > 15000:
        return -150  # 超大原盘防爆盘
    return 0


def extract_release_group(title: str) -> str:
    """提取发布组名称，如 CHDWEB, ADWeb, FLUX"""
    m = re.search(r"[-@]([A-Za-z0-9_]+)(?:\s*\[.*?\])?(?:\.mkv|\.mp4)?$", str(title).strip())
    return m.group(1).lower() if m else ""


def calculate_candidate_score(
    cand: Dict[str, Any],
    hits: List[int],
    all_cand_eps: List[int],
    preferred_group: Optional[str] = None
) -> float:
    """综合计算候选资源的选取加权得分（清晰度、码率、做种、多候选净增益与发布组一致性）"""
    t = cand.get("torrent_info") or {}
    meta = cand.get("meta_info") or {}
    title = str(t.get("title") or "")
    hits_count = len(hits)
    redundant_count = max(0, len(all_cand_eps) - hits_count)

    # 1. 命中集数净增益 (命中+350，冗余集扣120以避免为了1集下整季大包)
    net_gain = hits_count * 350 - redundant_count * 120

    # 2. 清晰度梯队分 (1080p: +1000, 2160p: +900, 720p: -500, SD: -1000)
    res_score = get_resolution_score(t, meta)

    # 3. 单集健康码率分
    try:
        size_bytes = float(t.get("size", 0))
    except (ValueError, TypeError):
        size_bytes = 0.0
    ep_count = len(all_cand_eps) if all_cand_eps else 1
    bitrate_score = get_bitrate_health_score(size_bytes, ep_count)

    # 4. 中文与字幕偏好
    labels_text = json.dumps(t.get("labels") or [], ensure_ascii=False) + title
    has_chinese = 1 if re.search(r"中文|中字|简中|繁中|国语|chs|cht|简日|繁日|双语|baha|巴哈|简繁|内封|mikan", labels_text, re.I) else 0
    chinese_score = 400 if has_chinese else 0

    # 5. 做种数偏好 (做种>=5加200，并微量累加健康种)
    try:
        seeds = int(t.get("seeders", 0))
    except (ValueError, TypeError):
        seeds = 0
    seeds_score = (200 + min(seeds, 50)) if seeds >= 5 else (seeds * 20)

    # 6. 发布组连续性偏好 (后续分集尽量与已选中的首选发布组一致)
    group = extract_release_group(title)
    group_score = 250 if preferred_group and group and group == preferred_group else 0

    return net_gain + res_score + bitrate_score + chinese_score + seeds_score + group_score


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
        "source": transfer_repair_cfg.get("source") or paths_cfg.get("downloadPath") or "/video/downloads/电视剧",
        "target": transfer_repair_cfg.get("target") or paths_cfg.get("source") or "/115/Library_incs/nas/电视剧",
    }

    # ── 预取 MoviePilot 实时活跃下载队列（用于防重与排队对齐） ──
    active_downloads: List[Dict[str, Any]] = []
    try:
        raw_active = mp.get_active_downloads()
        if isinstance(raw_active, list):
            active_downloads = raw_active
        elif isinstance(raw_active, dict):
            active_downloads = raw_active.get("data") or raw_active.get("items") or []
        logger.info(f"📡 MoviePilot 活跃下载队列: 共有 {len(active_downloads)} 个下载任务")
    except Exception as act_err:
        logger.warn(f"⚠️ 查询 MoviePilot 活跃下载队列跳过: {act_err}")

    # 逐项处理
    for i, task in enumerate(session.tasks):
        d = task.data
        logger.progress("补缺", i + 1, len(session.tasks), f"{task.name} Season {task.season}")

        try:
            total = int(d.get("来源登记集数", 0)) if str(d.get("来源登记集数", "")).isdigit() else 0
            if total <= 0:
                session.updates[task] = {
                    "state": "来源集数未确认", "color": None, "note": "请先补充初检结果"
                }
                summary["errors"] += 1
                continue

            raw_name = task.name
            clean_name = re.sub(r"\[tmdbid=\d+\]", "", raw_name, flags=re.IGNORECASE)
            clean_name = re.sub(r"[（(](?:19|20)\d{2}[)）]", "", clean_name)
            title = clean_title_text(clean_name)
            year_match = re.search(r"[（(]((?:19|20)\d{2})[)）]", raw_name)
            year = year_match.group(1) if year_match else ""
            tmdb_match = re.search(r"\[tmdbid=(\d+)\]", raw_name, flags=re.IGNORECASE)
            tmdb_id = tmdb_match.group(1) if tmdb_match else str(d.get("TMDB ID") or "").strip()

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

            # ──【实时下载队列核对与动态去重】──
            active_episodes_in_queue: Set[int] = set()
            for item in active_downloads:
                media = item.get("media") or {}
                it_tmdb = str(media.get("tmdbid") or item.get("tmdbid") or "").strip()
                it_season_str = str(media.get("season") or item.get("season_episode") or "").strip()
                s_match = re.search(r"\d+", it_season_str)
                it_season = int(s_match.group()) if s_match else None

                is_match = False
                if tmdb_id and it_tmdb and it_tmdb == str(tmdb_id):
                    is_match = (it_season == task.season)
                elif not tmdb_id or not it_tmdb:
                    it_name = media.get("title") or item.get("name") or item.get("title") or ""
                    if norm(it_name) == norm(task.title):
                        is_match = (it_season == task.season) if it_season is not None else True

                if is_match:
                    eps_found = parse_active_episodes(media.get("episode"), total)
                    active_episodes_in_queue.update(eps_found)

            # 若活跃下载队列已完全覆盖所需缺失集
            if needed is not None and len(needed) > 0 and set(needed).issubset(active_episodes_in_queue):
                covered_str = format_ranges(sorted(list(set(needed))))
                logger.info(f"  📥 下载队列正在下载全部缺失集 (覆盖: {covered_str})，跳过重复提交")
                session.updates[task] = {
                    "state": "已提交下载，待复核",
                    "color": "yellow",
                    "note": f"下载队列正在下载缺集 (覆盖: {covered_str})",
                    "repair": read_repair_state(task.data) or None,
                    "at": datetime.now().isoformat(),
                }
                summary["existing"] += 1
                continue

            # 若活跃下载队列覆盖了部分缺失集，剔除已在下载中的集数，仅需补齐剩余缺失
            if needed is not None and active_episodes_in_queue:
                overlap = [ep for ep in needed if ep in active_episodes_in_queue]
                if overlap:
                    needed = [ep for ep in needed if ep not in active_episodes_in_queue]
                    logger.info(f"  📥 下载队列已在下载集数: {format_ranges(overlap)}，本次仅需补齐剩余: {format_ranges(needed)}")
                    task.needed = needed
                    if not needed:
                        session.updates[task] = {
                            "state": "已提交下载，待复核",
                            "color": "yellow",
                            "note": f"下载队列正在下载缺集 (覆盖: {format_ranges(overlap)})",
                            "repair": read_repair_state(task.data) or None,
                            "at": datetime.now().isoformat(),
                        }
                        summary["existing"] += 1
                        continue

            # ── 检查本地持久化防重记录 ──
            store_key = f"dl:{norm(task.name)}:{task.season}"
            records = store.get(store_key, [])
            now_ts = datetime.now().timestamp()
            valid_accepted = []
            for r in records:
                if r.get("state") in ("accepted", "existing"):
                    at_str = r.get("at")
                    is_recent = False
                    if at_str:
                        try:
                            t_record = datetime.fromisoformat(at_str).timestamp()
                            if (now_ts - t_record) < 1800:  # 30分钟内提交
                                is_recent = True
                        except Exception:
                            pass
                    r_eps = set(r.get("episodes") or [])
                    if is_recent or (r_eps and r_eps.intersection(active_episodes_in_queue)):
                        valid_accepted.append(r)

            if needed is not None and all(any(n in (r.get("episodes") or []) for r in valid_accepted) for n in needed):
                session.updates[task] = {
                    "state": "已提交下载，待复核",
                    "color": "yellow",
                    "note": "近期下载记录已覆盖缺集",
                    "records": valid_accepted,
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
                if needed is not None and len(needed) > 0:
                    if not is_full:
                        if eps:
                            hit_needed = [ep for ep in eps if ep in needed]
                            if not hit_needed:
                                logger.debug(f"  ⏭ 排除无用散集: {t.get('title')} (包含集号 {eps} 均不属于缺集 {needed})")
                                continue
                        else:
                            logger.debug(f"  ⏭ 排除未知集号散集: {t.get('title')}")
                            continue

                # 排除历史防重记录中已在近期成功提交过的完全相同种子
                t_key = f"{t.get('site_name')}:{t.get('title')}:{t.get('size')}"
                if any(r.get("key") == t_key and r.get("state") == "accepted" for r in valid_accepted):
                    logger.debug(f"  ⏭ 排除近期已提交过相同种子: {t.get('title')}")
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

            # 区分整季包与散集
            season_packs = [c for c in eligible if is_season_pack(c.get("meta_info") or {}, c.get("torrent_info") or {}, task.season, total)]
            standalone_candidates = [c for c in eligible if not is_season_pack(c.get("meta_info") or {}, c.get("torrent_info") or {}, task.season, total)]
            selected_candidates: List[Tuple[Dict[str, Any], List[int], bool]] = []

            # ── 整季包能效比决策 (Season Pack Yield Ratio) ──
            prefer_standalone = False
            if season_packs and needed and total > 0:
                missing_count = len(needed)
                missing_ratio = missing_count / total
                if missing_count <= 3 and missing_ratio < 0.35:
                    standalone_covered: Set[int] = set()
                    for sc in standalone_candidates:
                        sc_eps = (sc.get("meta_info") or {}).get("episode_list") or []
                        standalone_covered.update(sc_eps)
                    if set(needed).issubset(standalone_covered):
                        prefer_standalone = True
                        logger.info(
                            f"  💡 缺失 {missing_count} 集 (占比 {missing_ratio:.1%})，且散集可 100% 覆盖，"
                            f"优先采用散集精准补缺（避免下整季大包）"
                        )

            if season_packs and not prefer_standalone:
                # 优选综合得分最高的整季包
                best_sp = max(
                    season_packs,
                    key=lambda c: calculate_candidate_score(c, list(range(1, total + 1)), list(range(1, total + 1)))
                )
                selected_candidates.append((best_sp, list(range(1, total + 1)), True))
            else:
                # 散集贪心多候选覆盖算法（含画质梯队、健康码率、冗余惩罚、发布组一致性与单季15个上限熔断）
                remaining_needed = set(needed) if needed else set()
                preferred_group: Optional[str] = None
                max_submissions = 15

                while remaining_needed and len(selected_candidates) < max_submissions:
                    scored_candidates: List[Tuple[float, Dict[str, Any], List[int]]] = []
                    for c in eligible:
                        meta = c.get("meta_info") or {}
                        eps = meta.get("episode_list") or []
                        hits = [ep for ep in eps if ep in remaining_needed]
                        if not hits:
                            continue

                        score = calculate_candidate_score(c, hits, eps, preferred_group)
                        scored_candidates.append((score, c, hits))

                    if not scored_candidates:
                        break

                    # 取综合得分最高候选
                    scored_candidates.sort(key=lambda x: x[0], reverse=True)
                    best_score, best_cand, best_hits = scored_candidates[0]

                    selected_candidates.append((best_cand, best_hits, False))
                    remaining_needed -= set(best_hits)

                    # 记录首选发布组，后续候选尽量保持同组一致
                    if not preferred_group:
                        preferred_group = extract_release_group(str((best_cand.get("torrent_info") or {}).get("title") or ""))

            if not selected_candidates:
                logger.warn(f"  ⚠️ 未能匹配到可覆盖缺集的资源")
                session.updates[task] = {
                    "state": "无合格资源",
                    "color": "red",
                    "note": f"检索到 {len(contexts)} 条，但无资源能覆盖缺失集号",
                }
                summary["noResource"] += 1
                continue

            # 批量向 MoviePilot 提交下载
            submitted_recs: List[Dict[str, Any]] = []
            all_covered_eps: Set[int] = set()

            for cand_idx, (cand, hits, is_sp) in enumerate(selected_candidates):
                cand_t = cand.get("torrent_info") or {}
                cand_meta = cand.get("meta_info") or {}
                title = str(cand_t.get("title") or "")
                site = str(cand_t.get("site_name") or "")
                seeds = int(cand_t.get("seeders", 0))
                desc_type = "整季" if is_sp else f"缺集 {format_ranges(hits)}"

                t_key = f"{site}:{title}:{cand_t.get('size')}"
                logger.info(f"  🎯 选定最佳资源 [{cand_idx + 1}/{len(selected_candidates)}]: [{site}] {title} (做种: {seeds}, {desc_type})")

                # 请求节流：连续提交之间暂停 0.5s，保护 MoviePilot / 下载器客户端稳定
                if cand_idx > 0:
                    time.sleep(0.5)

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
                if len(submitted_recs) == 1 and selected_candidates[0][2]:
                    # 单个整季包
                    first = submitted_recs[0]
                    note_str = f"[{first['site']}] {first['title']} (做种 {first['seeds']}, 整季覆盖)"
                elif len(submitted_recs) == 1:
                    first = submitted_recs[0]
                    cov_str = format_ranges(sorted(list(all_covered_eps)))
                    note_str = f"[{first['site']}] {first['title']} (覆盖: {cov_str})"
                else:
                    cov_str = format_ranges(sorted(list(all_covered_eps)))
                    uncovered = [ep for ep in (needed or []) if ep not in all_covered_eps]
                    if uncovered:
                        uncov_str = format_ranges(sorted(uncovered))
                        note_str = f"已提交 {len(submitted_recs)} 个资源补缺 (覆盖: {cov_str}，仍缺(无源): {uncov_str})"
                    else:
                        note_str = f"已提交 {len(submitted_recs)} 个资源补缺 (完整覆盖缺集: {cov_str})"

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
