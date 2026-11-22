"""订阅治理专属：联动补缺选种引擎的批量资源梳理与一键批量推送服务。

设计原则：
- 严格零侵入既有 `download_service.py`、`tasks.py` 与 `SeasonRecord` 流程，仅只读复用其已验证的
  站点规则解析、做种档位、同档省容量与散集/整季包选种纯函数；
- 同时支持：
  1. 电视剧「部分已下·断更停滞 / 零进度死种」按 CD2 实存集号计算真实缺集，批量梳理可补缺资源并一键推送；
  2. 电影「往年老片死种 / 长期未入库」按同样补缺站点优先级 + 做种门槛 + 省容量算法筛查可用资源并一键推送；
- 推送前执行 MoviePilot 活跃队列防重检查，全程写入 OperationLog 审计，支持扫描中途优雅中止。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.moviepilot import MoviePilotClient
from app.clients.tmdb import TMDBClient
from app.config import get_settings
from app.core.subscription_cleaner import diagnose_subscription
from app.models.operation import OperationLog
from app.services.download_service import (
    _candidate_seeders,
    _candidate_site_id,
    _candidate_site_name,
    _candidate_size_bytes,
    _candidate_stable_key,
    _dedup_evaluations,
    _evaluate_raw_candidates,
    _format_bytes,
    _passes_explicit_filters,
    _pick_best_season_pack,
    _plan_standalone_downloads,
    _seeder_tier_floor,
    resolve_category_gap_fill_sites,
)
from app.services.gap_fill_tracker import sanitize_text_no_secrets
from app.services.subscription_cd2_index import enrich_subscriptions_with_cd2_and_cache

log = logging.getLogger("mediaflow.subscription_governance")

# 进程级内存审计任务仓库（保留最近 10 次梳理任务以便用户审阅与一键推送）
_audit_jobs: dict[str, dict[str, Any]] = {}
_audit_tasks: dict[str, asyncio.Task] = {}
_MAX_AUDIT_JOBS = 10


def _prune_old_audits() -> None:
    if len(_audit_jobs) <= _MAX_AUDIT_JOBS:
        return
    ordered = sorted(_audit_jobs.items(), key=lambda kv: kv[1].get("created_at", ""))
    for aid, _ in ordered[:-_MAX_AUDIT_JOBS]:
        t = _audit_tasks.get(aid)
        if t is None or t.done():
            _audit_jobs.pop(aid, None)
            _audit_tasks.pop(aid, None)


def _is_anime_subscription(sub: dict[str, Any]) -> bool:
    """判断订阅作品是否应走「动漫」补缺站点规则。"""
    paths = sub.get("cd2_paths") or []
    if any("/动漫/" in str(p) for p in paths):
        return True
    genres = sub.get("tmdb_genre_ids") or sub.get("genre_ids") or []
    for g in genres:
        try:
            if int(g) == 16:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _select_best_movie_candidate(
    raw_results: list[dict[str, Any]],
    *,
    tmdb_id_num: int,
    min_seeders: int,
    tier_ratio: float,
    min_slack: int,
    min_resolution: str,
    require_chinese: bool,
    allowed_site_ids: set[int] | None,
    site_priority_map: dict[int, int] | None,
) -> tuple[dict[str, Any] | None, int, list[str]]:
    """按补缺同款规则为电影筛选最佳候选资源：硬条件门禁 -> 站点优先级 -> 做种同档 -> 省容量升序。"""
    qualified: list[dict[str, Any]] = []
    reject_reasons: list[str] = []
    max_seeders_seen = 0

    for c in raw_results:
        t = c.get("torrent_info") or {}
        m = c.get("media_info") or {}
        meta = c.get("meta_info") or {}
        if not t:
            continue

        seeds = _candidate_seeders(c)
        if seeds > max_seeders_seen:
            max_seeders_seen = seeds

        sid = _candidate_site_id(c)
        if allowed_site_ids is not None and (sid is None or sid not in allowed_site_ids):
            reject_reasons.append("不在已启用补缺站点内")
            continue

        if seeds < min_seeders:
            reject_reasons.append(f"做种不足(<{min_seeders})")
            continue

        cand_tid = str(m.get("tmdb_id") or "").strip()
        if tmdb_id_num > 0 and cand_tid and cand_tid != str(tmdb_id_num):
            reject_reasons.append("TMDB ID 不符")
            continue

        if not _passes_explicit_filters(t, meta, min_resolution=min_resolution, require_chinese=require_chinese):
            reject_reasons.append("未满足画质/中字过滤")
            continue

        pri = site_priority_map.get(sid, 999999) if (site_priority_map and sid is not None) else 999999
        size_b = _candidate_size_bytes(c)
        qualified.append({
            "cand": c,
            "site_priority": pri,
            "site_name": _candidate_site_name(c),
            "seeders": seeds,
            "size_bytes": size_b,
            "stable_key": _candidate_stable_key(c),
        })

    if not qualified:
        return None, max_seeders_seen, list(dict.fromkeys(reject_reasons))[:4]

    # 1. 最高优先站点档位优先
    best_pri = min(x["site_priority"] for x in qualified)
    pri_pool = [x for x in qualified if x["site_priority"] == best_pri]

    # 2. 同站点档内取做种同档 (H - seeders <= max(min_slack, floor(H * tier_ratio)))
    h = max(x["seeders"] for x in pri_pool)
    floor_s = _seeder_tier_floor(h, tier_ratio=tier_ratio, min_slack=min_slack)
    tier_pool = [x for x in pri_pool if x["seeders"] >= floor_s]

    # 3. 档内省容量升序（未知大小排后）
    tier_pool.sort(
        key=lambda x: (
            x["size_bytes"] is None,
            x["size_bytes"] if x["size_bytes"] is not None else 0,
            -x["seeders"],
            x["stable_key"],
        )
    )
    return tier_pool[0], max_seeders_seen, []


def _is_movie_in_active_downloads(
    active_downloads: list[dict[str, Any]],
    tmdb_id: str,
    title: str,
) -> bool:
    title_cf = (title or "").strip().casefold()
    for dl in active_downloads:
        st = str(dl.get("state") or dl.get("status") or "").lower()
        if st in ("seeding", "uploading", "completed", "finished", "done"):
            continue
        dl_tid = str(dl.get("tmdb_id") or dl.get("tmdbid") or "").strip()
        dl_name = str(dl.get("title") or dl.get("name") or "").casefold()
        if tmdb_id and dl_tid and dl_tid == tmdb_id:
            return True
        if title_cf and len(title_cf) >= 2 and title_cf in dl_name:
            return True
    return False


def _sanitize_audit_item_for_response(item: dict[str, Any]) -> dict[str, Any]:
    """返回前端展示的安全脱敏视图（去除完整 torrent payload 避免响应过大及泄露敏感链接）。"""
    return {k: v for k, v in item.items() if k != "_selected_payloads"}


def get_batch_audit_status(audit_id: str) -> dict[str, Any] | None:
    job = _audit_jobs.get(audit_id)
    if not job:
        return None
    items = [_sanitize_audit_item_for_response(x) for x in job.get("items", [])]
    downloadable = [x for x in items if x.get("audit_status") == "downloadable"]
    no_resource = [x for x in items if x.get("audit_status") in ("no_resource", "skipped", "error")]
    already_queued = [x for x in items if x.get("audit_status") == "already_queued"]
    return {
        "audit_id": job["audit_id"],
        "media_type": job["media_type"],
        "status": job["status"],
        "aborted": job["aborted"],
        "total": job["total"],
        "processed": job["processed"],
        "progress_pct": int(100 * job["processed"] / max(1, job["total"])) if job["total"] > 0 else 100,
        "message": job["message"],
        "downloadable_count": len(downloadable),
        "no_resource_count": len(no_resource),
        "already_queued_count": len(already_queued),
        "downloadable_items": downloadable,
        "no_resource_items": no_resource,
        "already_queued_items": already_queued,
        "created_at": job["created_at"],
    }


def abort_batch_resource_audit(audit_id: str) -> dict[str, Any]:
    job = _audit_jobs.get(audit_id)
    if not job:
        return {"ok": False, "message": "未找到对应的批量梳理任务"}
    job["aborted"] = True
    if job["status"] == "running":
        job["message"] = "正在中止剩余条目的梳理，已扫出的结果将完整保留..."
    return {"ok": True, "message": "已发出中止指令，已扫描出的条目仍可一键补缺或退订"}


async def _run_batch_audit_worker(
    audit_id: str,
    db_session_factory,
    media_type: str,
    target_subs: list[dict[str, Any]],
) -> None:
    job = _audit_jobs[audit_id]
    settings = get_settings()
    min_seeders = max(1, int(getattr(settings, "gap_fill_min_seeders", 2) or 2))
    tier_ratio = float(getattr(settings, "gap_fill_seeder_tier_ratio", 0.10) or 0.10)
    min_slack = max(1, int(getattr(settings, "gap_fill_min_seeder_slack", 1) or 1))
    min_resolution = str(getattr(settings, "gap_fill_min_resolution", "") or "")
    require_chinese = bool(getattr(settings, "gap_fill_require_chinese", False))

    mp = MoviePilotClient()
    tmdb = TMDBClient()
    sem = asyncio.Semaphore(3)  # 温和并发，保护 PT 站点不被风控

    try:
        # 1. 读取动漫/非动漫两套补缺站点规则
        category_plans: dict[str, dict[str, Any]] = {}
        async with db_session_factory() as db:
            for cat in ("non_anime", "anime"):
                try:
                    category_plans[cat] = await resolve_category_gap_fill_sites(db, mp, cat)
                except Exception as exc:
                    category_plans[cat] = {"ready": False, "reason": f"站点规则解析失败: {exc}"}
            await db.commit()

        # 2. 获取当前活跃下载队列做防重保护
        active_downloads: list[dict[str, Any]] = []
        try:
            fetched = await mp.get_active_downloads()
            if isinstance(fetched, list):
                active_downloads = fetched
        except Exception as q_err:
            log.warning("批量梳理获取 MP 下载队列失败（降级继续）: %s", q_err)

        async def _audit_one(sub: dict[str, Any]) -> dict[str, Any]:
            if job["aborted"]:
                return {}

            diag = diagnose_subscription(sub)
            sid = diag.sub_id
            name = diag.name
            year = diag.year
            tid_str = str(sub.get("tmdbid") or sub.get("tmdb_id") or "").strip()
            tid_num = int(tid_str) if tid_str.isdigit() else 0
            poster = str(sub.get("backdrop") or sub.get("poster") or "")
            is_movie = diag.media_type == "movie"
            season = int(diag.season or 1) if not is_movie else None

            base_item: dict[str, Any] = {
                "id": sid,
                "sub_id": sid,
                "name": name,
                "year": year,
                "media_type": diag.media_type,
                "season": season,
                "tmdb_id": tid_num or None,
                "poster": poster,
                "clean_rule": diag.clean_rule,
                "clean_rule_name": diag.clean_rule_name,
                "completed_episodes": diag.completed_episodes,
                "total_episodes": diag.total_episodes,
                "cd2_verified": diag.cd2_verified,
                "data_source": diag.data_source,
                "audit_status": "no_resource",
                "missing_episodes": [],
                "covered_episodes": [],
                "selected_torrents": [],
                "max_seeders_seen": 0,
                "candidate_count": 0,
                "plan_summary": "",
                "reason": "",
                "_selected_payloads": [],
            }

            if tid_num <= 0:
                base_item["audit_status"] = "skipped"
                base_item["reason"] = "缺少有效 TMDB ID，无法精准匹配站点资源"
                return base_item

            cat = "anime" if _is_anime_subscription(sub) else "non_anime"
            plan = category_plans.get(cat) or category_plans.get("non_anime") or {}
            if not plan.get("ready"):
                # 若未显式配置站点规则，安全回退为 MP 默认搜索站点而不静默失败
                sites_param = None
                allowed_site_ids = None
                site_priority_map = {}
            else:
                sites_param = plan.get("sites_param")
                ordered_ids = plan.get("active_ordered_ids") or []
                allowed_site_ids = set(ordered_ids) if ordered_ids else None
                site_priority_map = plan.get("site_priority_map") or {}

            search_kwargs: dict[str, Any] = {"sites": sites_param} if sites_param else {}

            async with sem:
                if job["aborted"]:
                    return {}
                try:
                    if is_movie:
                        if _is_movie_in_active_downloads(active_downloads, tid_str, name):
                            base_item["audit_status"] = "already_queued"
                            base_item["reason"] = "该电影当前已在 MoviePilot 活跃下载队列中"
                            return base_item

                        raw = await mp.search_resources(
                            tmdb_id=tid_num, title=name, media_type="movie", **search_kwargs
                        )
                        if not raw and name:
                            raw = await mp.search_resources(
                                title=name, media_type="movie", **search_kwargs
                            )
                        base_item["candidate_count"] = len(raw)

                        best, max_s, rej = _select_best_movie_candidate(
                            raw,
                            tmdb_id_num=tid_num,
                            min_seeders=min_seeders,
                            tier_ratio=tier_ratio,
                            min_slack=min_slack,
                            min_resolution=min_resolution,
                            require_chinese=require_chinese,
                            allowed_site_ids=allowed_site_ids,
                            site_priority_map=site_priority_map,
                        )
                        base_item["max_seeders_seen"] = max_s

                        if not best:
                            base_item["audit_status"] = "no_resource"
                            if not raw:
                                base_item["reason"] = "已检索配置的补缺站点，全网未命中任何电影种子"
                            else:
                                base_item["reason"] = (
                                    f"检索到 {len(raw)} 个原始结果，但无合格资源（最高做种 {max_s}，原因：{' / '.join(rej) or '不满足条件'}）"
                                )
                            return base_item

                        cand = best["cand"]
                        t_info = cand.get("torrent_info") or {}
                        m_info = cand.get("media_info") or {}
                        base_item["audit_status"] = "downloadable"
                        base_item["selected_torrents"] = [{
                            "title": str(t_info.get("title") or name),
                            "site_name": best["site_name"],
                            "seeders": best["seeders"],
                            "size_display": _format_bytes(best["size_bytes"]),
                            "is_season_pack": False,
                            "covered_episodes": [],
                        }]
                        base_item["plan_summary"] = (
                            f"[{best['site_name']}] 做种 {best['seeders']} · {_format_bytes(best['size_bytes'])}"
                        )
                        base_item["reason"] = f"命中合格电影资源（做种 {best['seeders']} ≥ 门槛 {min_seeders}）"
                        base_item["_selected_payloads"] = [{"media_info": m_info, "torrent_info": t_info}]
                        return base_item
                    else:
                        # 电视剧：结合 CD2 实存集号 + TMDB 真实总集数计算真实缺失集号
                        sn = season or 1
                        total_eps = diag.total_episodes or 0
                        if total_eps <= 0 or total_eps == 99:
                            # 尝试向 TMDB 查询本季真实总集数（仅在未知或 99 占位时）
                            try:
                                s_det = await tmdb.get_season_detail(tid_num, sn)
                                ep_list = s_det.get("episodes") if isinstance(s_det, dict) else None
                                if isinstance(ep_list, list) and ep_list:
                                    # 只计算已播出集数（避免把未来未播集算作缺集）
                                    today_str = datetime.now().strftime("%Y-%m-%d")
                                    aired = [
                                        int(e.get("episode_number"))
                                        for e in ep_list
                                        if isinstance(e, dict)
                                        and e.get("episode_number")
                                        and (not e.get("air_date") or str(e.get("air_date")) <= today_str)
                                    ]
                                    if aired:
                                        total_eps = max(aired)
                                        base_item["total_episodes"] = total_eps
                            except Exception:
                                pass

                        if total_eps <= 0 or total_eps == 99:
                            base_item["audit_status"] = "skipped"
                            base_item["reason"] = "总集数未知或仍为 99 占位符，无法确定精准缺集号"
                            return base_item

                        owned_set = set(diag.cd2_episode_numbers or [])
                        if not owned_set and diag.completed_episodes and not diag.cd2_verified:
                            # 若尚未同步 CD2，则暂以 MP 已收前 N 集估算（并提示同步 CD2）
                            owned_set = set(range(1, min(diag.completed_episodes, total_eps) + 1))

                        missing_eps = [ep for ep in range(1, total_eps + 1) if ep not in owned_set]
                        base_item["missing_episodes"] = missing_eps

                        if not missing_eps:
                            base_item["audit_status"] = "skipped"
                            base_item["reason"] = f"本季 1–{total_eps} 集已全部齐备，无需补缺（可直接退订）"
                            return base_item

                        raw = await mp.search_resources(
                            tmdb_id=tid_num, season=sn, title=name, media_type="tv", **search_kwargs
                        )
                        if not raw and name:
                            raw = await mp.search_resources(
                                title=name, season=sn, media_type="tv", **search_kwargs
                            )
                        base_item["candidate_count"] = len(raw)
                        max_s = max((_candidate_seeders(c) for c in raw), default=0)
                        base_item["max_seeders_seen"] = max_s

                        st_evals, sp_evals, rej = _evaluate_raw_candidates(
                            raw,
                            tmdb_id_num=tid_num,
                            season_num=sn,
                            total_eps=total_eps,
                            missing_eps=missing_eps,
                            by_period=False,
                            parts_mode=False,
                            part_episode_mapping=None,
                            keyword_fallback=False,
                            min_seeders=min_seeders,
                            min_resolution=min_resolution,
                            require_chinese=require_chinese,
                            allowed_site_ids=allowed_site_ids,
                            site_priority_map=site_priority_map,
                        )
                        st_evals = _dedup_evaluations(st_evals)
                        sp_evals = _dedup_evaluations(sp_evals)

                        standalone_plan, standalone_note = _plan_standalone_downloads(
                            st_evals,
                            missing_eps,
                            tier_ratio=tier_ratio,
                            min_slack=min_slack,
                        )
                        covered_st = {ep for ev in standalone_plan for ep in ev.covered_missing}
                        all_st_covered = bool(standalone_plan) and set(missing_eps).issubset(covered_st)

                        selected_evals = []
                        plan_note = ""
                        if len(missing_eps) > 3 and sp_evals:
                            best_sp, sp_note = _pick_best_season_pack(sp_evals, tier_ratio=tier_ratio, min_slack=min_slack)
                            if best_sp:
                                selected_evals = [best_sp]
                                plan_note = sp_note or "缺集超过3集，优先整季包方案"
                        if not selected_evals:
                            if all_st_covered:
                                selected_evals = standalone_plan
                                plan_note = standalone_note
                            elif sp_evals:
                                best_sp, sp_note = _pick_best_season_pack(sp_evals, tier_ratio=tier_ratio, min_slack=min_slack)
                                if best_sp:
                                    selected_evals = [best_sp]
                                    plan_note = sp_note or "散集无法补全，已回退合格整季包"
                            if not selected_evals and standalone_plan:
                                selected_evals = standalone_plan
                                plan_note = standalone_note

                        if not selected_evals:
                            base_item["audit_status"] = "no_resource"
                            if not raw:
                                base_item["reason"] = f"缺 {len(missing_eps)} 集，全网补缺站点未搜到 S{sn:02d} 种子"
                            else:
                                top_rej = " / ".join(list(dict.fromkeys(rej))[:3]) or "未覆盖缺集或做种不足"
                                base_item["reason"] = (
                                    f"缺 {len(missing_eps)} 集，搜到 {len(raw)} 个结果但无合格方案（最高做种 {max_s}：{top_rej}）"
                                )
                            return base_item

                        covered = sorted({ep for ev in selected_evals for ep in ev.covered_missing})
                        base_item["audit_status"] = "downloadable"
                        base_item["covered_episodes"] = covered
                        base_item["selected_torrents"] = [
                            {
                                "title": str((ev.cand.get("torrent_info") or {}).get("title") or name),
                                "site_name": ev.site_name,
                                "seeders": ev.seeders,
                                "size_display": _format_bytes(ev.size_bytes),
                                "is_season_pack": ev.is_season_pack,
                                "covered_episodes": list(ev.covered_missing),
                            }
                            for ev in selected_evals
                        ]
                        base_item["plan_summary"] = (
                            f"选定 {len(selected_evals)} 个种子，可补 {len(covered)}/{len(missing_eps)} 缺集"
                            + ("（整季包）" if any(ev.is_season_pack for ev in selected_evals) else "（散集）")
                        )
                        base_item["reason"] = plan_note or base_item["plan_summary"]
                        base_item["_selected_payloads"] = [
                            {
                                "media_info": ev.cand.get("media_info") or {},
                                "torrent_info": ev.cand.get("torrent_info") or {},
                            }
                            for ev in selected_evals
                        ]
                        return base_item
                except Exception as exc:
                    base_item["audit_status"] = "error"
                    base_item["reason"] = f"检索异常: {type(exc).__name__}: {exc}"
                    return base_item

        # 逐批并发执行并实时推进进度
        for idx, sub in enumerate(target_subs):
            if job["aborted"]:
                break
            res_item = await _audit_one(sub)
            if res_item:
                job["items"].append(res_item)
            job["processed"] = idx + 1
            dl_cnt = sum(1 for x in job["items"] if x.get("audit_status") == "downloadable")
            job["message"] = f"已梳理 {job['processed']}/{job['total']} 项，已筛出 {dl_cnt} 部存在合格可补缺资源..."

        dl_cnt = sum(1 for x in job["items"] if x.get("audit_status") == "downloadable")
        no_cnt = sum(1 for x in job["items"] if x.get("audit_status") in ("no_resource", "skipped", "error"))
        job["status"] = "aborted" if job["aborted"] else "completed"
        job["message"] = (
            f"梳理{'已中止' if job['aborted'] else '完成'}：共检查 {job['processed']}/{job['total']} 项，"
            f"筛出 {dl_cnt} 部可一键补缺下载，{no_cnt} 部暂无合格资源（可直接退订）"
        )
    except Exception as exc:
        log.exception("批量资源梳理任务失败")
        job["status"] = "failed"
        job["message"] = f"批量资源梳理失败: {exc}"
    finally:
        await mp.close()
        await tmdb.close()


async def start_batch_resource_audit(
    db: AsyncSession,
    db_session_factory,
    *,
    media_type: str = "tv",
    bucket: str = "partial_stalled",  # partial_stalled | never_downloaded | all_stalled | old_zombie | custom
    sub_ids: list[int] | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    """启动异步批量资源梳理任务（复用补缺选种引擎筛查可下载资源）。"""
    _prune_old_audits()

    mp = MoviePilotClient()
    try:
        raw_subs = await mp.list_subscriptions()
    finally:
        await mp.close()

    enriched_subs, _ = await enrich_subscriptions_with_cd2_and_cache(db, raw_subs)

    selected_subs: list[dict[str, Any]] = []
    sub_id_set = set(sub_ids or [])

    for s in enriched_subs:
        diag = diagnose_subscription(s)
        if media_type != "all" and diag.media_type != media_type:
            continue
        if sub_id_set:
            if diag.sub_id in sub_id_set:
                selected_subs.append(s)
            continue

        if media_type == "tv":
            if bucket == "partial_stalled" and diag.clean_rule == "stalled":
                selected_subs.append(s)
            elif bucket == "never_downloaded" and diag.clean_rule == "zombie":
                selected_subs.append(s)
            elif bucket == "all_stalled" and diag.clean_rule in ("stalled", "zombie"):
                selected_subs.append(s)
        else:
            if bucket in ("old_zombie", "all_stalled") and diag.clean_rule in ("zombie", "douban_stale"):
                selected_subs.append(s)
            elif bucket == "never_downloaded" and diag.clean_rule in ("zombie", "douban_stale", "active") and diag.in_library_status != "in_library":
                selected_subs.append(s)

    # 限制单批上限防止对 PT 站点造成过大瞬时压力
    max_n = max(1, min(int(limit or 40), 100))
    selected_subs = selected_subs[:max_n]

    audit_id = f"sa-{uuid.uuid4().hex[:10]}"
    _audit_jobs[audit_id] = {
        "audit_id": audit_id,
        "media_type": media_type,
        "bucket": bucket,
        "status": "running" if selected_subs else "completed",
        "aborted": False,
        "total": len(selected_subs),
        "processed": 0,
        "message": (
            f"正在使用补缺站点规则批量梳理 {len(selected_subs)} 部作品的可用资源..."
            if selected_subs
            else "当前分类下没有需要梳理的条目"
        ),
        "items": [],
        "created_at": datetime.now().isoformat(),
    }

    if selected_subs:
        _audit_tasks[audit_id] = asyncio.create_task(
            _run_batch_audit_worker(audit_id, db_session_factory, media_type, selected_subs)
        )

    return get_batch_audit_status(audit_id) or {"audit_id": audit_id, "status": "completed", "total": 0}


async def execute_batch_subscription_gap_fill(
    db: AsyncSession,
    *,
    audit_id: str,
    sub_ids: list[int],
) -> dict[str, Any]:
    """针对批量梳理筛出的合格资源，一键批量推送 MoviePilot 下载。"""
    job = _audit_jobs.get(audit_id)
    if not job:
        return {"ok": False, "message": "梳理会话已过期或不存在，请重新执行资源梳理"}

    target_set = set(sub_ids or [])
    if not target_set:
        return {"ok": False, "message": "未勾选任何需要推送下载的作品"}

    targets = [
        item for item in job.get("items", [])
        if item.get("id") in target_set
        and item.get("audit_status") == "downloadable"
        and item.get("_selected_payloads")
    ]
    if not targets:
        return {"ok": False, "message": "所选条目中没有已通过筛查的可下载方案"}

    mp = MoviePilotClient()
    pushed_count = 0
    failed_count = 0
    details: list[dict[str, Any]] = []

    try:
        for item in targets:
            sub_pushed = 0
            sub_errs: list[str] = []
            for payload in item.get("_selected_payloads") or []:
                m_info = payload.get("media_info") or {}
                t_info = payload.get("torrent_info") or {}
                try:
                    reply = await mp.submit_download(m_info, t_info)
                    if isinstance(reply, dict) and reply.get("success") is True:
                        sub_pushed += 1
                    else:
                        msg = sanitize_text_no_secrets(
                            str(reply.get("message") if isinstance(reply, dict) else reply or "MP 未返回成功")
                        )
                        sub_errs.append(msg)
                except Exception as exc:
                    sub_errs.append(sanitize_text_no_secrets(str(exc)))

            if sub_pushed > 0:
                pushed_count += 1
                item["audit_status"] = "already_queued"
                item["reason"] = f"已成功推送 {sub_pushed} 个补缺种子至 MoviePilot 下载队列"
                db.add(OperationLog(
                    action="subscription_gap_fill_push",
                    status="completed",
                    detail_json=json.dumps({
                        "audit_id": audit_id,
                        "sub_id": item["id"],
                        "name": item["name"],
                        "media_type": item["media_type"],
                        "season": item.get("season"),
                        "pushed_torrents": sub_pushed,
                    }, ensure_ascii=False),
                ))
                details.append({"sub_id": item["id"], "name": item["name"], "ok": True, "pushed": sub_pushed})
            else:
                failed_count += 1
                details.append({
                    "sub_id": item["id"],
                    "name": item["name"],
                    "ok": False,
                    "error": "; ".join(sub_errs) or "推送失败",
                })

        if pushed_count > 0:
            await db.commit()

        msg = f"一键批量补缺完成：成功推送 {pushed_count} 部作品至 MoviePilot 下载队列"
        if failed_count > 0:
            msg += f"，失败 {failed_count} 部"
        return {
            "ok": pushed_count > 0,
            "pushed_count": pushed_count,
            "failed_count": failed_count,
            "message": msg,
            "details": details,
        }
    finally:
        await mp.close()
