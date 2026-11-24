"""订阅治理专属：统一三阶段数据采集流水线（MoviePilot + CD2 实盘 + 站点补缺资源）与一键批量补缺/退订服务。

设计原则：
- 严格零侵入既有 `download_service.py`、`tasks.py` 与 `SeasonRecord` 流程，仅只读复用其已验证的
  站点规则解析、做种档位、同档省容量与散集/整季包选种纯函数；
- 统一采集闭环：
  1. 阶段 1/3：拉取 MoviePilot 最新订阅并融合本地 TMDB/初检缓存；
  2. 阶段 2/3：全盘扫描 CD2 源目录与归档目录实盘库存，生成全分类诊断快照；
  3. 阶段 3/3：自动对「部分已下·断更（剧集）」与「往年老片死种（电影）」并发搜寻 PT 站点可补缺资源，
     实时写回持久化快照，供分类页直接展示并一键执行「批量补缺下载」或「批量取消订阅」；
- 进入「订阅清理」视图时仅秒读持久化快照（cached_only=True），绝不自动触发耗时远程请求。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.moviepilot import MoviePilotClient
from app.clients.tmdb import TMDBClient
from app.config import get_settings
from app.core.subscription_cleaner import diagnose_subscription, evaluate_subscriptions
from app.models.metadata_cache import MetadataCache
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
from app.services.subscription_cd2_index import (
    build_cd2_subscription_inventory,
    enrich_subscriptions_with_cd2_and_cache,
)

log = logging.getLogger("mediaflow.subscription_governance")

UNIFIED_RESOURCE_CACHE_KEY = "sub_resource_audit_cache_v1"
UNIFIED_SNAPSHOT_CACHE_KEY = "sub_unified_snapshot_v1"

# 进程级内存缓存与任务状态
_memory_resource_cache: dict[str, dict[str, Any]] | None = None
_memory_unified_snapshot: dict[str, Any] | None = None

_unified_query_state: dict[str, Any] = {
    "job_id": "",
    "media_type": "all",
    "status": "idle",        # idle | running | completed | stopped | failed
    "stage": "",             # mp_fetch | cd2_scan | resource_search | done
    "stage_label": "尚未执行查询",
    "progress_pct": 0,
    "message": "请点击「查询最新订阅数据」获取 MoviePilot、CD2 实盘与站点可补缺资源",
    "data_ready": False,
    "aborted": False,
    "resource_total": 0,
    "resource_processed": 0,
    "resource_downloadable": 0,
    "resource_no_source": 0,
    "queried_at": None,
    "snapshot_version": 0,
}
_unified_query_task: asyncio.Task | None = None

# 所有批量执行操作（批量取消订阅 / 批量补缺下载 / 批量变更状态）的统一实时进度跟踪器
_batch_action_state: dict[str, Any] = {
    "action_id": "",
    "action_type": "",     # "unsubscribe" | "gap_fill_push" | "status_update"
    "action_label": "",
    "status": "idle",      # "idle" | "running" | "completed" | "failed"
    "total": 0,
    "processed": 0,
    "success_count": 0,
    "fail_count": 0,
    "progress_pct": 0,
    "current_title": "",
    "message": "",
    "updated_at": None,
}


def update_batch_action_progress(
    *,
    action_type: str | None = None,
    action_label: str | None = None,
    status: str | None = None,
    total: int | None = None,
    processed: int | None = None,
    success_count: int | None = None,
    fail_count: int | None = None,
    progress_pct: int | None = None,
    current_title: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    if action_type is not None:
        _batch_action_state["action_type"] = action_type
    if action_label is not None:
        _batch_action_state["action_label"] = action_label
    if status is not None:
        if status == "running" and _batch_action_state.get("status") != "running":
            _batch_action_state["action_id"] = f"ba-{uuid.uuid4().hex[:8]}"
        _batch_action_state["status"] = status
    if total is not None:
        _batch_action_state["total"] = total
    if processed is not None:
        _batch_action_state["processed"] = processed
    if success_count is not None:
        _batch_action_state["success_count"] = success_count
    if fail_count is not None:
        _batch_action_state["fail_count"] = fail_count
    eff_total = _batch_action_state.get("total") or 0
    if progress_pct is not None:
        _batch_action_state["progress_pct"] = max(0, min(100, int(progress_pct)))
    elif eff_total > 0 and processed is not None:
        raw_pct = int(100 * processed / max(1, eff_total))
        _batch_action_state["progress_pct"] = 100 if _batch_action_state.get("status") == "completed" else min(99, max(0, raw_pct))
    if current_title is not None:
        _batch_action_state["current_title"] = current_title
    if message is not None:
        _batch_action_state["message"] = message
    _batch_action_state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return dict(_batch_action_state)


def get_batch_action_status() -> dict[str, Any]:
    return dict(_batch_action_state)

# 兼容保留：独立批量审计任务仓库
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


def _resource_cache_keys_for_sub(
    *,
    sub_id: int | str | None,
    media_type: str | None,
    tmdb_id: int | str | None,
    season: int | None,
) -> list[str]:
    keys: list[str] = []
    if sub_id is not None and str(sub_id).strip():
        keys.append(f"id:{str(sub_id).strip()}")
    tid = str(tmdb_id or "").strip()
    if tid and tid != "None":
        mt = (media_type or "tv").strip().lower()
        sn = int(season or 0) if mt == "tv" else 0
        keys.append(f"tmdb:{mt}:{tid}:{sn}")
    return keys


async def load_resource_audit_cache(db: AsyncSession) -> dict[str, dict[str, Any]]:
    """从 SQLite metadata_cache 加载已搜寻过的站点补缺资源缓存。"""
    global _memory_resource_cache
    if _memory_resource_cache is not None:
        return _memory_resource_cache
    try:
        row = (
            await db.execute(
                select(MetadataCache).where(MetadataCache.cache_key == UNIFIED_RESOURCE_CACHE_KEY)
            )
        ).scalar_one_or_none()
        if row and row.payload_json:
            parsed = json.loads(row.payload_json)
            if isinstance(parsed, dict):
                _memory_resource_cache = parsed
                return _memory_resource_cache
    except Exception as exc:
        log.warning("加载订阅站点资源缓存失败: %s", exc)
    _memory_resource_cache = {}
    return _memory_resource_cache


async def save_resource_audit_cache(db: AsyncSession, cache_data: dict[str, dict[str, Any]]) -> None:
    """持久化站点补缺资源缓存至 metadata_cache。"""
    global _memory_resource_cache
    _memory_resource_cache = cache_data
    try:
        payload_str = json.dumps(cache_data, ensure_ascii=False)
        row = (
            await db.execute(
                select(MetadataCache).where(MetadataCache.cache_key == UNIFIED_RESOURCE_CACHE_KEY)
            )
        ).scalar_one_or_none()
        if row:
            row.payload_json = payload_str
            row.verified = True
            row.status = "ok"
            row.updated_at = datetime.now()
        else:
            db.add(
                MetadataCache(
                    cache_key=UNIFIED_RESOURCE_CACHE_KEY,
                    source="sub_resource_audit",
                    media_id="global",
                    season_number=0,
                    payload_json=payload_str,
                    verified=True,
                    status="ok",
                )
            )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        log.warning("保存订阅站点资源缓存失败: %s", exc)


def _store_item_in_resource_cache(
    cache_data: dict[str, dict[str, Any]],
    res_item: dict[str, Any],
) -> None:
    if not res_item or not res_item.get("id"):
        return
    entry = dict(res_item)
    entry["checked_at"] = entry.get("checked_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    keys = _resource_cache_keys_for_sub(
        sub_id=entry.get("id"),
        media_type=entry.get("media_type"),
        tmdb_id=entry.get("tmdb_id"),
        season=entry.get("season"),
    )
    for k in keys:
        cache_data[k] = entry


RESOURCE_DOWNLOADABLE_CACHE_TTL_SECONDS = 4 * 3600  # 4 小时：防止 PT 站短时效签名直链隔天失效


def _is_downloadable_cache_fresh(
    entry: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    ttl_seconds: int = RESOURCE_DOWNLOADABLE_CACHE_TTL_SECONDS,
) -> bool:
    """判断 downloadable 种子缓存是否仍在有效期内（防止短时效签名链接过期）。"""
    if not isinstance(entry, dict):
        return False
    checked_raw = str(entry.get("checked_at") or "").strip()
    if not checked_raw:
        return False
    try:
        checked_dt = datetime.strptime(checked_raw[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    cur = now or datetime.now()
    age = (cur - checked_dt).total_seconds()
    return 0 <= age <= ttl_seconds


def _lookup_item_in_resource_cache(
    cache_data: dict[str, dict[str, Any]],
    *,
    sub_id: int | str | None,
    media_type: str | None,
    tmdb_id: int | str | None,
    season: int | None,
) -> dict[str, Any] | None:
    for k in _resource_cache_keys_for_sub(
        sub_id=sub_id, media_type=media_type, tmdb_id=tmdb_id, season=season
    ):
        hit = cache_data.get(k)
        if isinstance(hit, dict):
            return hit
    return None


async def load_unified_snapshot(db: AsyncSession) -> dict[str, Any] | None:
    """加载上次统一查询生成的完整订阅快照。"""
    global _memory_unified_snapshot
    if _memory_unified_snapshot is not None:
        return _memory_unified_snapshot
    try:
        row = (
            await db.execute(
                select(MetadataCache).where(MetadataCache.cache_key == UNIFIED_SNAPSHOT_CACHE_KEY)
            )
        ).scalar_one_or_none()
        if row and row.payload_json:
            parsed = json.loads(row.payload_json)
            if isinstance(parsed, dict) and isinstance(parsed.get("subscriptions"), list):
                _memory_unified_snapshot = parsed
                if parsed.get("queried_at") and not _unified_query_state.get("queried_at"):
                    _unified_query_state["queried_at"] = parsed["queried_at"]
                    _unified_query_state["data_ready"] = True
                return _memory_unified_snapshot
    except Exception as exc:
        log.warning("加载统一订阅快照失败: %s", exc)
    return None


async def save_unified_snapshot(db: AsyncSession, snapshot: dict[str, Any]) -> None:
    """保存统一查询生成的完整订阅快照至 SQLite。"""
    global _memory_unified_snapshot
    _memory_unified_snapshot = snapshot
    try:
        payload_str = json.dumps(snapshot, ensure_ascii=False)
        row = (
            await db.execute(
                select(MetadataCache).where(MetadataCache.cache_key == UNIFIED_SNAPSHOT_CACHE_KEY)
            )
        ).scalar_one_or_none()
        if row:
            row.payload_json = payload_str
            row.verified = True
            row.status = "ok"
            row.updated_at = datetime.now()
        else:
            db.add(
                MetadataCache(
                    cache_key=UNIFIED_SNAPSHOT_CACHE_KEY,
                    source="sub_unified_snapshot",
                    media_id="global",
                    season_number=0,
                    payload_json=payload_str,
                    verified=True,
                    status="ok",
                )
            )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        log.warning("保存统一订阅快照失败: %s", exc)


def _recalculate_snapshot_stats(items: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """根据标准化订阅条目列表重新计算 overall / tv / movie 统计指标。"""
    tv_items = [x for x in items if x.get("media_type") == "tv"]
    mov_items = [x for x in items if x.get("media_type") == "movie"]

    tv_stats = {
        "total": len(tv_items),
        "high": sum(1 for x in tv_items if x.get("clean_confidence") == "high"),
        "medium": sum(1 for x in tv_items if x.get("clean_confidence") == "medium"),
        "protected": sum(1 for x in tv_items if x.get("clean_confidence") == "low"),
        "clean_eligible": sum(1 for x in tv_items if x.get("clean_eligible")),
        "short_tv_stalled": sum(1 for x in tv_items if x.get("clean_rule") == "short_tv_stalled"),
        "partial_stalled": sum(1 for x in tv_items if x.get("clean_rule") == "stalled"),
        "never_downloaded": sum(1 for x in tv_items if x.get("clean_rule") == "zombie"),
        "low_quality": sum(1 for x in tv_items if x.get("is_low_quality")),
        "low_quality_cleanable": sum(
            1 for x in tv_items if x.get("is_low_quality") and x.get("clean_eligible")
        ),
        "gap_fill_candidates": sum(1 for x in tv_items if x.get("can_gap_fill")),
        "partial_stalled_downloadable": sum(
            1
            for x in tv_items
            if x.get("clean_rule") == "stalled" and x.get("resource_status") == "downloadable"
        ),
        "partial_stalled_no_resource": sum(
            1
            for x in tv_items
            if x.get("clean_rule") == "stalled"
            and x.get("resource_status") in ("no_resource", "skipped", "error")
        ),
        "partial_stalled_checked": sum(
            1
            for x in tv_items
            if x.get("clean_rule") == "stalled"
            and x.get("resource_status") not in (None, "", "unchecked")
        ),
    }

    movie_stats = {
        "total": len(mov_items),
        "high": sum(1 for x in mov_items if x.get("clean_confidence") == "high"),
        "medium": sum(1 for x in mov_items if x.get("clean_confidence") == "medium"),
        "protected": sum(1 for x in mov_items if x.get("clean_confidence") == "low"),
        "clean_eligible": sum(1 for x in mov_items if x.get("clean_eligible")),
        "upcoming_protected": sum(1 for x in mov_items if x.get("clean_rule") == "upcoming_protect"),
        "douban_stale": sum(1 for x in mov_items if x.get("clean_rule") == "douban_stale"),
        "old_zombie": sum(1 for x in mov_items if x.get("clean_rule") in ("zombie", "douban_stale")),
        "low_quality": sum(1 for x in mov_items if x.get("is_low_quality")),
        "low_quality_cleanable": sum(
            1 for x in mov_items if x.get("is_low_quality") and x.get("clean_eligible")
        ),
        "gap_fill_candidates": sum(1 for x in mov_items if x.get("can_gap_fill")),
        "old_zombie_downloadable": sum(
            1
            for x in mov_items
            if x.get("clean_rule") in ("zombie", "douban_stale")
            and x.get("resource_status") == "downloadable"
        ),
        "old_zombie_no_resource": sum(
            1
            for x in mov_items
            if x.get("clean_rule") in ("zombie", "douban_stale")
            and x.get("resource_status") in ("no_resource", "skipped", "error")
        ),
        "old_zombie_checked": sum(
            1
            for x in mov_items
            if x.get("clean_rule") in ("zombie", "douban_stale")
            and x.get("resource_status") not in (None, "", "unchecked")
        ),
    }

    overall = {
        "total": len(items),
        "high": tv_stats["high"] + movie_stats["high"],
        "medium": tv_stats["medium"] + movie_stats["medium"],
        "protected": tv_stats["protected"] + movie_stats["protected"],
        "clean_eligible": tv_stats["clean_eligible"] + movie_stats["clean_eligible"],
    }
    return overall, tv_stats, movie_stats


async def assemble_subscriptions_payload(
    db: AsyncSession,
    raw_subs: list[dict[str, Any]],
    *,
    save_as_snapshot: bool = True,
    queried_at: str | None = None,
) -> dict[str, Any]:
    """将原始 MP 订阅与 CD2 实盘快照、TMDB 缓存及已搜寻的站点资源缓存融合，生成标准化响应。"""
    enriched_subs, cd2_meta = await enrich_subscriptions_with_cd2_and_cache(db, raw_subs)
    eval_result = evaluate_subscriptions(enriched_subs)
    res_cache = await load_resource_audit_cache(db)

    items_out: list[dict[str, Any]] = []
    for s, diag in zip(enriched_subs, eval_result["items"]):
        tid = s.get("tmdbid") or s.get("tmdb_id")
        cached_res = _lookup_item_in_resource_cache(
            res_cache,
            sub_id=diag.sub_id,
            media_type=diag.media_type,
            tmdb_id=tid,
            season=diag.season,
        )
        res_status = cached_res.get("audit_status") if cached_res else "unchecked"
        res_torrents = cached_res.get("selected_torrents") or [] if cached_res else []
        res_summary = (
            (cached_res.get("plan_summary") or cached_res.get("reason") or "")
            if cached_res
            else ""
        )
        res_reason = (cached_res.get("reason") or "") if cached_res else ""
        res_missing = (cached_res.get("missing_episodes") or []) if cached_res else []
        res_checked_at = cached_res.get("checked_at") if cached_res else None

        items_out.append({
            "id": diag.sub_id,
            "name": diag.name,
            "year": diag.year,
            "media_type": diag.media_type,
            "season": diag.season,
            "total_episodes": diag.total_episodes,
            "current_episodes": diag.completed_episodes,
            "lack_episodes": diag.lack_episodes,
            "state": diag.state,
            "username": diag.username,
            "tmdb_id": tid,
            "poster": s.get("backdrop") or s.get("poster") or "",
            # 智能清理决策字段
            "clean_eligible": diag.clean_eligible,
            "clean_confidence": diag.clean_confidence,
            "clean_rule": diag.clean_rule,
            "clean_rule_name": diag.clean_rule_name,
            "clean_reason": diag.clean_reason,
            "days_inactive": diag.days_inactive,
            "created_days_ago": diag.created_days_ago,
            "is_protected": diag.is_protected,
            "is_short_tv": diag.is_short_tv,
            "short_drama_reason": diag.short_drama_reason,
            "is_douban_sync": diag.is_douban_sync,
            "release_stage": diag.release_stage,
            "in_library_status": diag.in_library_status,
            "version_target_satisfied": diag.version_target_satisfied,
            # v2.5.63+ 扩展维度：停滞分桶、异常特征、CD2 实盘真相
            "stalled_kind": diag.stalled_kind,
            "can_gap_fill": diag.can_gap_fill,
            "is_low_quality": diag.is_low_quality,
            "low_quality_flags": diag.low_quality_flags,
            "low_quality_reasons": diag.low_quality_reasons,
            "data_source": diag.data_source,
            "cd2_verified": diag.cd2_verified,
            "cd2_in_library": diag.cd2_in_library,
            "cd2_source_episodes": diag.cd2_source_episodes,
            "cd2_archive_episodes": diag.cd2_archive_episodes,
            "cd2_total_episodes": diag.cd2_total_episodes,
            "cd2_paths": diag.cd2_paths,
            "mp_completed_episodes": diag.mp_completed_episodes,
            # v2.5.64 统一站点资源搜寻结果字段
            "resource_status": res_status,
            "resource_plan_summary": res_summary,
            "resource_reason": res_reason,
            "resource_torrents": res_torrents,
            "resource_missing_episodes": res_missing,
            "resource_checked_at": res_checked_at,
        })

    overall, tv_stats, movie_stats = _recalculate_snapshot_stats(items_out)
    q_time = queried_at or _unified_query_state.get("queried_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    snapshot = {
        "has_snapshot": True,
        "queried_at": q_time,
        "overall": overall,
        "tv": tv_stats,
        "movie": movie_stats,
        "cd2_meta": cd2_meta,
        "unified_query": get_unified_query_status(),
        "total": overall["total"],
        "high_confidence_count": overall["high"],
        "medium_confidence_count": overall["medium"],
        "protected_count": overall["protected"],
        "clean_eligible_total": overall["clean_eligible"],
        "subscriptions": items_out,
    }
    if save_as_snapshot:
        await save_unified_snapshot(db, snapshot)
    return snapshot


async def get_cached_or_empty_subscriptions_payload(
    db: AsyncSession,
    media_type: str | None = None,
) -> dict[str, Any]:
    """仅读取本地持久化快照（进入订阅清理页面时调用，绝不触发外部请求）。"""
    snap = await load_unified_snapshot(db)
    res_cache = await load_resource_audit_cache(db)
    if not snap:
        empty_stats = {
            "total": 0,
            "high": 0,
            "medium": 0,
            "protected": 0,
            "clean_eligible": 0,
            "short_tv_stalled": 0,
            "partial_stalled": 0,
            "never_downloaded": 0,
            "low_quality": 0,
            "low_quality_cleanable": 0,
            "gap_fill_candidates": 0,
            "partial_stalled_downloadable": 0,
            "partial_stalled_no_resource": 0,
            "partial_stalled_checked": 0,
            "upcoming_protected": 0,
            "douban_stale": 0,
            "old_zombie": 0,
            "old_zombie_downloadable": 0,
            "old_zombie_no_resource": 0,
            "old_zombie_checked": 0,
        }
        return {
            "has_snapshot": False,
            "queried_at": None,
            "overall": {"total": 0, "high": 0, "medium": 0, "protected": 0, "clean_eligible": 0},
            "tv": dict(empty_stats),
            "movie": dict(empty_stats),
            "cd2_meta": {"cd2_synced": False, "cd2_synced_at": None},
            "unified_query": get_unified_query_status(),
            "total": 0,
            "high_confidence_count": 0,
            "medium_confidence_count": 0,
            "protected_count": 0,
            "clean_eligible_total": 0,
            "subscriptions": [],
        }

    # 将最新的站点资源缓存实时叠加到快照条目上
    all_items = list(snap.get("subscriptions") or [])
    for item in all_items:
        cached_res = _lookup_item_in_resource_cache(
            res_cache,
            sub_id=item.get("id"),
            media_type=item.get("media_type"),
            tmdb_id=item.get("tmdb_id"),
            season=item.get("season"),
        )
        if cached_res:
            item["resource_status"] = cached_res.get("audit_status") or "unchecked"
            item["resource_plan_summary"] = cached_res.get("plan_summary") or cached_res.get("reason") or ""
            item["resource_reason"] = cached_res.get("reason") or ""
            item["resource_torrents"] = cached_res.get("selected_torrents") or []
            item["resource_missing_episodes"] = cached_res.get("missing_episodes") or []
            item["resource_checked_at"] = cached_res.get("checked_at")

    overall, tv_stats, movie_stats = _recalculate_snapshot_stats(all_items)
    filtered_items = (
        [x for x in all_items if x.get("media_type") == media_type]
        if media_type
        else all_items
    )
    out = dict(snap)
    out["has_snapshot"] = True
    out["overall"] = overall
    out["tv"] = tv_stats
    out["movie"] = movie_stats
    out["unified_query"] = get_unified_query_status()
    out["total"] = overall["total"]
    out["high_confidence_count"] = overall["high"]
    out["medium_confidence_count"] = overall["medium"]
    out["protected_count"] = overall["protected"]
    out["clean_eligible_total"] = overall["clean_eligible"]
    out["subscriptions"] = filtered_items
    return out


async def mutate_unified_snapshot_on_delete(db: AsyncSession, deleted_ids: list[int]) -> None:
    """退订完成后立即从本地持久化快照中移除对应条目并重算分类统计，无需重新全量请求外部系统。"""
    if not deleted_ids:
        return
    snap = await load_unified_snapshot(db)
    if not snap or not isinstance(snap.get("subscriptions"), list):
        return
    del_set = {int(x) for x in deleted_ids if str(x).isdigit()}
    remaining = [x for x in snap["subscriptions"] if int(x.get("id") or 0) not in del_set]
    overall, tv_stats, movie_stats = _recalculate_snapshot_stats(remaining)
    snap["subscriptions"] = remaining
    snap["overall"] = overall
    snap["tv"] = tv_stats
    snap["movie"] = movie_stats
    snap["total"] = overall["total"]
    snap["high_confidence_count"] = overall["high"]
    snap["medium_confidence_count"] = overall["medium"]
    snap["protected_count"] = overall["protected"]
    snap["clean_eligible_total"] = overall["clean_eligible"]
    await save_unified_snapshot(db, snap)


async def mutate_unified_snapshot_on_state_change(
    db: AsyncSession, sub_ids: list[int], new_state: str
) -> None:
    """暂停/恢复状态变更后立即同步更新本地持久化快照。"""
    if not sub_ids:
        return
    snap = await load_unified_snapshot(db)
    if not snap or not isinstance(snap.get("subscriptions"), list):
        return
    id_set = {int(x) for x in sub_ids if str(x).isdigit()}
    for item in snap["subscriptions"]:
        if int(item.get("id") or 0) in id_set:
            item["state"] = new_state
    await save_unified_snapshot(db, snap)


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

    best_pri = min(x["site_priority"] for x in qualified)
    pri_pool = [x for x in qualified if x["site_priority"] == best_pri]

    h = max(x["seeders"] for x in pri_pool)
    floor_s = _seeder_tier_floor(h, tier_ratio=tier_ratio, min_slack=min_slack)
    tier_pool = [x for x in pri_pool if x["seeders"] >= floor_s]

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


_ACTIVE_TV_SEASON_RE = re.compile(
    r"(?:^|[^a-z0-9])s0*(\d{1,2})(?:[^0-9]|$)|第\s*0*(\d{1,2})\s*季",
    re.IGNORECASE,
)


def _is_tv_season_in_active_downloads(
    active_downloads: list[dict[str, Any]],
    tmdb_id: str,
    title: str,
    season: int,
) -> bool:
    """检查指定剧集与季号是否已在 MoviePilot 活跃下载队列中（忽略已完成/纯做种任务）。"""
    title_cf = (title or "").strip().casefold()
    for dl in active_downloads:
        if not isinstance(dl, dict):
            continue
        st = str(dl.get("state") or dl.get("status") or "").lower()
        if st in ("seeding", "uploading", "completed", "finished", "done"):
            continue
        media_obj = (
            dl.get("media")
            if isinstance(dl.get("media"), dict)
            else (dl.get("media_info") if isinstance(dl.get("media_info"), dict) else {})
        )
        dl_tid = str(
            dl.get("tmdb_id")
            or dl.get("tmdbid")
            or media_obj.get("tmdb_id")
            or media_obj.get("tmdbid")
            or ""
        ).strip()
        dl_name = str(dl.get("title") or dl.get("name") or media_obj.get("title") or "").strip()
        dl_name_cf = dl_name.casefold()

        identity_match = False
        if tmdb_id and dl_tid and dl_tid == tmdb_id:
            identity_match = True
        elif not dl_tid and title_cf and len(title_cf) >= 2 and title_cf in dl_name_cf:
            identity_match = True
        if not identity_match:
            continue

        raw_sn = dl.get("season") if dl.get("season") is not None else media_obj.get("season")
        dl_season: int | None = None
        if raw_sn is not None and str(raw_sn).strip().isdigit():
            dl_season = int(str(raw_sn).strip())
        else:
            m_sn = _ACTIVE_TV_SEASON_RE.search(dl_name)
            if m_sn:
                dl_season = int(m_sn.group(1) or m_sn.group(2))
        if dl_season is not None:
            if dl_season == season:
                return True
        elif season == 1:
            return True
    return False


def _sanitize_audit_item_for_response(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k != "_selected_payloads"}


async def _audit_single_subscription_item(
    sub: dict[str, Any],
    *,
    mp: MoviePilotClient,
    tmdb: TMDBClient,
    category_plans: dict[str, dict[str, Any]],
    active_downloads: list[dict[str, Any]],
    sem: asyncio.Semaphore,
    is_aborted_fn,
    min_seeders: int,
    tier_ratio: float,
    min_slack: int,
    min_resolution: str,
    require_chinese: bool,
) -> dict[str, Any]:
    """对单条订阅执行补缺站点搜寻与最优种子决策。"""
    if is_aborted_fn():
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
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "_selected_payloads": [],
    }

    if tid_num <= 0:
        base_item["audit_status"] = "skipped"
        base_item["reason"] = "缺少有效 TMDB ID，无法精准匹配站点资源"
        return base_item

    cat = "anime" if _is_anime_subscription(sub) else "non_anime"
    plan = category_plans.get(cat) or category_plans.get("non_anime") or {}
    if not plan.get("ready"):
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
        if is_aborted_fn():
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
                sn = season or 1
                if _is_tv_season_in_active_downloads(active_downloads, tid_str, name, sn):
                    base_item["audit_status"] = "already_queued"
                    base_item["reason"] = f"该剧 S{sn:02d} 当前已在 MoviePilot 活跃下载队列中，已自动跳过防重复推送"
                    return base_item

                total_eps = diag.total_episodes or 0
                if total_eps <= 0 or total_eps == 99:
                    try:
                        s_det = await tmdb.get_season_detail(tid_num, sn)
                        ep_list = s_det.get("episodes") if isinstance(s_det, dict) else None
                        if isinstance(ep_list, list) and ep_list:
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
                top_site = selected_evals[0].site_name if selected_evals else "PT"
                base_item["plan_summary"] = (
                    f"[{top_site}] 选定 {len(selected_evals)} 个种子，可补 {len(covered)}/{len(missing_eps)} 缺集"
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


# ============================================================================
# 统一三阶段查询流水线（按钮「查询最新订阅数据」唯一入口）
# ============================================================================

def get_unified_query_status() -> dict[str, Any]:
    return dict(_unified_query_state)


def abort_unified_subscription_query() -> dict[str, Any]:
    if _unified_query_state.get("status") != "running":
        return {"ok": False, "message": "当前没有正在运行的订阅查询任务"}
    _unified_query_state["aborted"] = True
    _unified_query_state["message"] = "正在停止站点资源搜寻，已获取的 MoviePilot、CD2 与站点数据将完整保留..."
    return {"ok": True, "message": "已发出停止指令，已获取的数据已保存"}


async def _run_unified_query_worker(
    job_id: str,
    db_session_factory,
    *,
    media_type: str = "all",
    search_resources: bool = True,
    reuse_resource_cache: bool = True,
) -> None:
    settings = get_settings()
    min_seeders = max(1, int(getattr(settings, "gap_fill_min_seeders", 2) or 2))
    tier_ratio = float(getattr(settings, "gap_fill_seeder_tier_ratio", 0.10) or 0.10)
    min_slack = max(1, int(getattr(settings, "gap_fill_min_seeder_slack", 1) or 1))
    min_resolution = str(getattr(settings, "gap_fill_min_resolution", "") or "")
    require_chinese = bool(getattr(settings, "gap_fill_require_chinese", False))

    media_type_clean = (media_type or "all").lower().strip()
    if media_type_clean not in ("all", "tv", "movie"):
        media_type_clean = "all"
    type_label = (
        "电视剧" if media_type_clean == "tv"
        else ("电影" if media_type_clean == "movie" else "剧集与电影")
    )

    mp = MoviePilotClient()
    tmdb = TMDBClient()
    sem = asyncio.Semaphore(3)

    try:
        # ── 阶段 1/3：获取 MoviePilot 最新订阅清单 ──────────────────────
        _unified_query_state.update({
            "stage": "mp_fetch",
            "stage_label": f"阶段 1/3 · 获取 {type_label}订阅",
            "progress_pct": 8,
            "message": f"正在从 MoviePilot 拉取最新{type_label}订阅列表...",
        })
        raw_subs = await mp.list_subscriptions()
        if _unified_query_state.get("aborted"):
            _unified_query_state.update({
                "status": "stopped",
                "stage": "done",
                "message": "查询已停止",
            })
            return

        _unified_query_state.update({
            "progress_pct": 18,
            "message": f"已获取 MoviePilot 共 {len(raw_subs)} 条订阅，准备扫描 CD2 {type_label}实盘库存...",
        })

        # ── 阶段 2/3：全盘扫描 CD2 实盘库存（源目录 + 归档目录） ─────────
        _unified_query_state.update({
            "stage": "cd2_scan",
            "stage_label": f"阶段 2/3 · 扫描 CD2 实盘库存 ({type_label})",
            "progress_pct": 20,
            "message": f"正在扫描 CD2 受控目录，核实{type_label}实盘状态...",
        })

        def _cd2_progress(pct: int, stage: str, msg: str, scanned: int, total: int, tv_cnt: int, mov_cnt: int) -> None:
            mapped_pct = 20 + int((max(0, min(pct, 100)) * 30) / 100)
            _unified_query_state.update({
                "stage": "cd2_scan",
                "stage_label": f"阶段 2/3 · 扫描 CD2 实盘库存 ({type_label})",
                "progress_pct": mapped_pct,
                "message": f"[阶段 2/3] {msg}",
            })

        await build_cd2_subscription_inventory(
            db_session_factory,
            scope=media_type_clean,
            raw_subs=raw_subs,
            progress_cb=_cd2_progress,
        )

        # 基于 CD2 快照进行初步注入，识别存在 99 集占位符或总集数未知且 CD2 已有实存的剧集并向 TMDB 纠偏
        async with db_session_factory() as db:
            enriched_subs, _ = await enrich_subscriptions_with_cd2_and_cache(db, raw_subs)

        fix_pairs: list[tuple[int, int]] = []
        if media_type_clean != "movie":
            seen_fix_pairs: set[tuple[int, int]] = set()
            for s in enriched_subs:
                if _unified_query_state.get("aborted"):
                    break
                if diagnose_subscription(s).media_type != "tv":
                    continue
                raw_tot = s.get("total_episode") or s.get("total_episodes")
                if s.get("cd2_verified") and int(s.get("cd2_total_episodes") or 0) > 0:
                    if (raw_tot is None or int(raw_tot) in (0, 99)) and not s.get("tmdb_season_episodes"):
                        tid_raw = str(s.get("tmdbid") or s.get("tmdb_id") or "").strip()
                        if tid_raw.isdigit():
                            sn_val = int(s.get("season") or 1)
                            pair = (int(tid_raw), sn_val)
                            if pair not in seen_fix_pairs:
                                seen_fix_pairs.add(pair)
                                fix_pairs.append(pair)

        if fix_pairs and not _unified_query_state.get("aborted"):
            _unified_query_state.update({
                "progress_pct": 52,
                "message": f"[阶段 2/3] 正在通过 TMDB 纠正 {len(fix_pairs)} 部剧集的真实季总集数（消除 99 集占位符）...",
            })
            tmdb_sem = asyncio.Semaphore(5)
            today_str = datetime.now().strftime("%Y-%m-%d")
            cutoff_str = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

            async def _fetch_tmdb_season_meta(tid_int: int, sn_val: int) -> tuple[int, int, dict[str, Any]] | None:
                if _unified_query_state.get("aborted"):
                    return None
                async with tmdb_sem:
                    try:
                        det = await tmdb.get_season_detail(tid_int, sn_val)
                    except Exception:
                        return None
                if not isinstance(det, dict):
                    return None
                eps = det.get("episodes")
                if not isinstance(eps, list) or not eps:
                    return None
                ep_nums = [
                    int(e.get("episode_number"))
                    for e in eps
                    if isinstance(e, dict) and e.get("episode_number") and int(e.get("episode_number")) > 0
                ]
                if not ep_nums:
                    return None
                ep_count = max(len(ep_nums), max(ep_nums))
                aired_eps = [
                    e for e in eps
                    if isinstance(e, dict) and e.get("air_date") and str(e.get("air_date"))[:10] <= today_str
                ]
                all_have_air = all(isinstance(e, dict) and bool(e.get("air_date")) for e in eps)
                max_air = max(
                    (str(e.get("air_date"))[:10] for e in eps if isinstance(e, dict) and e.get("air_date")),
                    default="",
                )
                finished_flag = bool(ep_count >= 2 and all_have_air and max_air and max_air <= cutoff_str)
                return tid_int, sn_val, {
                    "tmdb_id": tid_int,
                    "season_number": sn_val,
                    "episode_count": ep_count,
                    "aired_count": len(aired_eps),
                    "season_finished": finished_flag,
                    "last_air_date": max_air,
                }

            season_meta_results = await asyncio.gather(
                *(_fetch_tmdb_season_meta(tid_i, sn_i) for tid_i, sn_i in fix_pairs),
                return_exceptions=True,
            )
            valid_metas = [r for r in season_meta_results if isinstance(r, tuple) and len(r) == 3]
            if valid_metas:
                async with db_session_factory() as db:
                    for tid_i, sn_i, payload in valid_metas:
                        ck = f"tmdb_tv_season:{tid_i}:{sn_i}"
                        p_str = json.dumps(payload, ensure_ascii=False)
                        row = (
                            await db.execute(select(MetadataCache).where(MetadataCache.cache_key == ck))
                        ).scalar_one_or_none()
                        if row:
                            row.payload_json = p_str
                            row.verified = True
                            row.status = "ok"
                            row.updated_at = datetime.now()
                        else:
                            db.add(
                                MetadataCache(
                                    cache_key=ck,
                                    source="tmdb_tv_season",
                                    media_id=str(tid_i),
                                    season_number=sn_i,
                                    payload_json=p_str,
                                    verified=True,
                                    status="ok",
                                )
                            )
                    await db.commit()

        # 用最新 CD2 实盘库存与 TMDB 季纠偏缓存重新组装并持久化全分类快照
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with db_session_factory() as db:
            enriched_subs, _ = await enrich_subscriptions_with_cd2_and_cache(db, raw_subs)
            await assemble_subscriptions_payload(db, raw_subs, save_as_snapshot=True, queried_at=now_str)
            res_cache = await load_resource_audit_cache(db)
            if not reuse_resource_cache:
                if media_type_clean == "all":
                    res_cache.clear()
                elif media_type_clean == "tv":
                    for k in list(res_cache.keys()):
                        if ":tv:" in k or k.endswith(":tv"):
                            res_cache.pop(k, None)
                elif media_type_clean == "movie":
                    for k in list(res_cache.keys()):
                        if ":movie:" in k or k.endswith(":movie"):
                            res_cache.pop(k, None)
                await save_resource_audit_cache(db, res_cache)

        _unified_query_state.update({
            "data_ready": True,
            "queried_at": now_str,
            "progress_pct": 55,
            "snapshot_version": int(_unified_query_state.get("snapshot_version") or 0) + 1,
        })

        if not search_resources or _unified_query_state.get("aborted"):
            _unified_query_state.update({
                "status": "stopped" if _unified_query_state.get("aborted") else "completed",
                "stage": "done",
                "stage_label": "查询完成",
                "progress_pct": 100,
                "message": f"已完成 MoviePilot 与 CD2 {type_label}实盘数据同步（共 {len(raw_subs)} 条订阅）",
            })
            return

        # ── 阶段 3/3：通过 MoviePilot 搜寻断更剧集与老片死种的 PT 站点资源 ──
        target_subs: list[dict[str, Any]] = []
        for s in enriched_subs:
            diag = diagnose_subscription(s)
            if media_type_clean in ("all", "tv") and diag.media_type == "tv" and diag.clean_rule == "stalled":
                target_subs.append(s)
            elif media_type_clean in ("all", "movie") and diag.media_type == "movie" and diag.clean_rule in ("zombie", "douban_stale"):
                target_subs.append(s)

        # 电影数量少优先排前面，剧集按已收集集数降序（越接近收齐的越优先搜寻补缺）
        target_subs.sort(
            key=lambda s: (
                0 if str(s.get("type") or "") in ("电影", "movie") else 1,
                -int(s.get("cd2_total_episodes") or 0),
            )
        )

        total_targets = len(target_subs)
        _unified_query_state.update({
            "stage": "resource_search",
            "stage_label": f"阶段 3/3 · 搜寻{type_label}可补缺资源",
            "resource_total": total_targets,
            "resource_processed": 0,
            "resource_downloadable": 0,
            "resource_no_source": 0,
            "progress_pct": 56 if total_targets > 0 else 100,
            "message": (
                f"[阶段 3/3] 正在通过 MoviePilot 搜寻 {total_targets} 部{type_label}「部分已下·断更 / 老片死种」的站点资源..."
                if total_targets > 0
                else f"全部数据查询完成（当前无需要搜寻站点资源的{type_label}断更条目）"
            ),
        })

        if total_targets == 0:
            _unified_query_state.update({
                "status": "completed",
                "stage": "done",
                "stage_label": "查询完成",
                "progress_pct": 100,
            })
            return

        category_plans: dict[str, dict[str, Any]] = {}
        async with db_session_factory() as db:
            for cat in ("non_anime", "anime"):
                try:
                    category_plans[cat] = await resolve_category_gap_fill_sites(db, mp, cat)
                except Exception as exc:
                    category_plans[cat] = {"ready": False, "reason": f"站点规则解析失败: {exc}"}
            await db.commit()

        active_downloads: list[dict[str, Any]] = []
        try:
            fetched = await mp.get_active_downloads()
            if isinstance(fetched, list):
                active_downloads = fetched
        except Exception:
            pass

        processed = 0
        dl_cnt = 0
        no_cnt = 0

        async def _audit_one_target(sub: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
            if _unified_query_state.get("aborted"):
                return None, False
            diag = diagnose_subscription(sub)
            tid = sub.get("tmdbid") or sub.get("tmdb_id")
            cached_hit = None
            if reuse_resource_cache:
                cached_hit = _lookup_item_in_resource_cache(
                    res_cache,
                    sub_id=diag.sub_id,
                    media_type=diag.media_type,
                    tmdb_id=tid,
                    season=diag.season,
                )
            if cached_hit and cached_hit.get("audit_status") in (
                "downloadable",
                "no_resource",
                "already_queued",
                "skipped",
            ):
                # downloadable 含站点下载直链（部分 PT 站带短时效签名），超 TTL 强制重新搜种刷新
                if cached_hit.get("audit_status") != "downloadable" or _is_downloadable_cache_fresh(cached_hit):
                    return cached_hit, False

            res_item = await _audit_single_subscription_item(
                sub,
                mp=mp,
                tmdb=tmdb,
                category_plans=category_plans,
                active_downloads=active_downloads,
                sem=sem,
                is_aborted_fn=lambda: bool(_unified_query_state.get("aborted")),
                min_seeders=min_seeders,
                tier_ratio=tier_ratio,
                min_slack=min_slack,
                min_resolution=min_resolution,
                require_chinese=require_chinese,
            )
            return (res_item or None), True

        BATCH_SIZE = 3
        for start_idx in range(0, total_targets, BATCH_SIZE):
            if _unified_query_state.get("aborted"):
                break
            batch = target_subs[start_idx:start_idx + BATCH_SIZE]
            batch_results = await asyncio.gather(*(_audit_one_target(s) for s in batch))

            batch_network = False
            last_name = ""
            aborted_hit = False
            for res_item, used_net in batch_results:
                if not res_item:
                    aborted_hit = True
                    continue
                if used_net:
                    _store_item_in_resource_cache(res_cache, res_item)
                    batch_network = True
                processed += 1
                last_name = str(res_item.get("name") or last_name)
                st = res_item.get("audit_status")
                if st == "downloadable":
                    dl_cnt += 1
                elif st in ("no_resource", "skipped", "error"):
                    no_cnt += 1

            if processed > 0:
                pct = 55 + int((processed * 45) / max(1, total_targets))
                _unified_query_state.update({
                    "resource_processed": processed,
                    "resource_downloadable": dl_cnt,
                    "resource_no_source": no_cnt,
                    "progress_pct": min(99, pct),
                    "message": (
                        f"[阶段 3/3] 正在搜寻站点补缺资源：{last_name} "
                        f"({processed}/{total_targets} · 已命中 {dl_cnt} 部可补缺 / {no_cnt} 部无源)"
                    ),
                })

            # 每完成 2 批网络搜寻或到达末尾时落盘并递增 snapshot_version
            if (batch_network and (processed % 6 == 0 or start_idx + BATCH_SIZE >= total_targets)) or processed == total_targets:
                async with db_session_factory() as db:
                    await save_resource_audit_cache(db, res_cache)
                    await assemble_subscriptions_payload(
                        db, raw_subs, save_as_snapshot=True, queried_at=now_str
                    )
                _unified_query_state["snapshot_version"] = (
                    int(_unified_query_state.get("snapshot_version") or 0) + 1
                )

            if aborted_hit or _unified_query_state.get("aborted"):
                break

        # 收尾落盘
        async with db_session_factory() as db:
            await save_resource_audit_cache(db, res_cache)
            await assemble_subscriptions_payload(db, raw_subs, save_as_snapshot=True, queried_at=now_str)

        was_aborted = bool(_unified_query_state.get("aborted"))
        _unified_query_state.update({
            "status": "stopped" if was_aborted else "completed",
            "stage": "done",
            "stage_label": "已停止（保留已获数据）" if was_aborted else "查询完成",
            "progress_pct": 100,
            "snapshot_version": int(_unified_query_state.get("snapshot_version") or 0) + 1,
            "message": (
                f"查询{'已停止（已查数据完整保留）' if was_aborted else '全部完成'}："
                f"CD2 实盘已对齐，断更/老片站点已查 {processed}/{total_targets} 部"
                f"（命中 {dl_cnt} 部可一键补缺下载，{no_cnt} 部无合格源）"
            ),
        })
    except Exception as exc:
        log.exception("统一订阅数据查询任务失败")
        _unified_query_state.update({
            "status": "failed",
            "stage": "done",
            "stage_label": "查询异常",
            "message": f"查询最新订阅数据失败: {exc}",
        })
    finally:
        await mp.close()
        await tmdb.close()


def start_unified_subscription_query(
    db_session_factory,
    *,
    media_type: str = "all",
    search_resources: bool = True,
    reuse_resource_cache: bool = True,
) -> dict[str, Any]:
    """启动统一三阶段订阅数据采集任务（若已在运行则直接返回当前状态）。"""
    global _unified_query_task
    if (
        _unified_query_state.get("status") == "running"
        and _unified_query_task is not None
        and not _unified_query_task.done()
    ):
        return get_unified_query_status()

    job_id = f"uq-{uuid.uuid4().hex[:8]}"
    media_type_clean = (media_type or "all").lower().strip()
    if media_type_clean not in ("all", "tv", "movie"):
        media_type_clean = "all"
    type_label = (
        "电视剧" if media_type_clean == "tv"
        else ("电影" if media_type_clean == "movie" else "剧集与电影")
    )
    _unified_query_state.update({
        "job_id": job_id,
        "media_type": media_type_clean,
        "status": "running",
        "stage": "mp_fetch",
        "stage_label": f"阶段 1/3 · 获取 {type_label}订阅",
        "progress_pct": 5,
        "message": f"正在启动{type_label}订阅查询：连接 MoviePilot、CD2 与补缺站点...",
        "aborted": False,
        "resource_total": 0,
        "resource_processed": 0,
        "resource_downloadable": 0,
        "resource_no_source": 0,
    })
    _unified_query_task = asyncio.create_task(
        _run_unified_query_worker(
            job_id,
            db_session_factory,
            media_type=media_type_clean,
            search_resources=search_resources,
            reuse_resource_cache=reuse_resource_cache,
        )
    )
    return get_unified_query_status()


# ============================================================================
# 兼容保留与复用：专项批量资源梳理 & 一键批量补缺下载推送
# ============================================================================

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
    sem = asyncio.Semaphore(3)

    try:
        category_plans: dict[str, dict[str, Any]] = {}
        async with db_session_factory() as db:
            res_cache = await load_resource_audit_cache(db)
            for cat in ("non_anime", "anime"):
                try:
                    category_plans[cat] = await resolve_category_gap_fill_sites(db, mp, cat)
                except Exception as exc:
                    category_plans[cat] = {"ready": False, "reason": f"站点规则解析失败: {exc}"}
            await db.commit()

        active_downloads: list[dict[str, Any]] = []
        try:
            fetched = await mp.get_active_downloads()
            if isinstance(fetched, list):
                active_downloads = fetched
        except Exception as q_err:
            log.warning("批量梳理获取 MP 下载队列失败（降级继续）: %s", q_err)

        for idx, sub in enumerate(target_subs):
            if job["aborted"]:
                break
            res_item = await _audit_single_subscription_item(
                sub,
                mp=mp,
                tmdb=tmdb,
                category_plans=category_plans,
                active_downloads=active_downloads,
                sem=sem,
                is_aborted_fn=lambda: bool(job["aborted"]),
                min_seeders=min_seeders,
                tier_ratio=tier_ratio,
                min_slack=min_slack,
                min_resolution=min_resolution,
                require_chinese=require_chinese,
            )
            if res_item:
                job["items"].append(res_item)
                _store_item_in_resource_cache(res_cache, res_item)
            job["processed"] = idx + 1
            dl_cnt = sum(1 for x in job["items"] if x.get("audit_status") == "downloadable")
            job["message"] = f"已梳理 {job['processed']}/{job['total']} 项，已筛出 {dl_cnt} 部存在合格可补缺资源..."

        async with db_session_factory() as db:
            await save_resource_audit_cache(db, res_cache)

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
    bucket: str = "partial_stalled",
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
    audit_id: str | None = None,
    sub_ids: list[int],
) -> dict[str, Any]:
    """针对前面已通过 MoviePilot 搜寻出的合格站点资源，一键批量推送补缺下载。

    支持两种来源：
    1. 统一查询持久化缓存（`audit_id` 为空或 `"unified"`）：直接从 `sub_resource_audit_cache_v1` 提取对应 `sub_ids` 的优选种子推送；
    2. 专项梳理会话（传入具体 `audit_id`）：从对应 `_audit_jobs[audit_id]` 中提取推送。
    """
    target_set = {int(x) for x in (sub_ids or []) if str(x).isdigit()}
    if not target_set:
        return {"ok": False, "message": "未勾选任何需要推送下载的作品"}

    res_cache = await load_resource_audit_cache(db)
    targets: list[dict[str, Any]] = []
    snap = await load_unified_snapshot(db)
    snap_map: dict[int, dict[str, Any]] = {}
    if snap and isinstance(snap.get("subscriptions"), list):
        for s in snap["subscriptions"]:
            if s.get("id") is not None:
                snap_map[int(s["id"])] = s

    if audit_id and audit_id != "unified" and audit_id in _audit_jobs:
        job = _audit_jobs[audit_id]
        targets = [
            item
            for item in job.get("items", [])
            if int(item.get("id") or 0) in target_set
            and item.get("audit_status") == "downloadable"
            and item.get("_selected_payloads")
        ]
    else:
        # 从统一查询缓存中按 sub_id 查找（结合快照中的 tmdb_id/season 辅助查找）
        for sid in target_set:
            s_meta = snap_map.get(sid) or {}
            hit = _lookup_item_in_resource_cache(
                res_cache,
                sub_id=sid,
                media_type=s_meta.get("media_type"),
                tmdb_id=s_meta.get("tmdb_id"),
                season=s_meta.get("season"),
            )
            if (
                hit
                and hit.get("audit_status") == "downloadable"
                and hit.get("_selected_payloads")
            ):
                targets.append(hit)

    if not targets:
        return {"ok": False, "message": "所选条目中没有已搜到合格种子的可下载方案，请先执行「查询最新订阅数据」"}

    total_targets = len(targets)
    update_batch_action_progress(
        action_type="gap_fill_push",
        action_label="批量补缺下载推送",
        status="running",
        total=total_targets,
        processed=0,
        success_count=0,
        fail_count=0,
        progress_pct=0,
        current_title="",
        message=f"正在准备向 MoviePilot 批量推送 {total_targets} 部作品的补缺下载任务...",
    )

    mp = MoviePilotClient()
    tmdb_fallback: TMDBClient | None = None
    fallback_category_plans: dict[str, dict[str, Any]] | None = None
    pushed_count = 0
    retried_success_count = 0
    skipped_active_count = 0
    failed_count = 0
    cleared_transfer_total = 0
    cache_dirty = False
    details: list[dict[str, Any]] = []

    async def _submit_payloads(
        payloads: list[dict[str, Any]],
    ) -> tuple[int, list[str], list[str], list[str], set[tuple[str, str]]]:
        pushed = 0
        errs: list[str] = []
        sites_seen: list[str] = []
        p_titles: list[str] = []
        failed_keys: set[tuple[str, str]] = set()
        for payload in payloads:
            m_info = payload.get("media_info") or {}
            t_info = payload.get("torrent_info") or {}
            site_label = str(t_info.get("site_name") or t_info.get("site") or "PT").strip()
            if site_label and site_label not in sites_seen:
                sites_seen.append(site_label)
            m_title = str(m_info.get("title") or "").strip()
            if m_title:
                p_titles.append(m_title)
            pkey = (str(t_info.get("enclosure") or "").strip(), str(t_info.get("title") or "").strip())
            try:
                reply = await mp.submit_download(m_info, t_info)
                if isinstance(reply, dict) and reply.get("success") is True:
                    pushed += 1
                else:
                    raw_msg = str(reply.get("message") if isinstance(reply, dict) else reply or "MP 未返回成功")
                    msg = sanitize_text_no_secrets(raw_msg)
                    errs.append(f"[{site_label}] {msg}")
                    failed_keys.add(pkey)
            except Exception as exc:
                errs.append(f"[{site_label}] {sanitize_text_no_secrets(str(exc))}")
                failed_keys.add(pkey)
        return pushed, errs, sites_seen, p_titles, failed_keys

    try:
        active_downloads: list[dict[str, Any]] = []
        try:
            dl_res = await mp.get_active_downloads()
            if isinstance(dl_res, list):
                active_downloads = dl_res
        except Exception as dl_err:
            log.warning("批量补缺推送前获取 MP 活跃下载队列失败（继续执行推送）: %s", dl_err)

        for idx, item in enumerate(targets):
            item_name = str(item.get("name") or f"#{item.get('id')}")
            item_is_movie = str(item.get("media_type") or "").lower() == "movie"
            item_tid = str(item.get("tmdb_id") or item.get("tmdbid") or "").strip()
            item_season = int(item.get("season") or 1) if not item_is_movie else None

            update_batch_action_progress(
                processed=idx,
                current_title=item_name,
                progress_pct=int(100 * idx / max(1, total_targets)),
                message=f"正在推送第 {idx + 1}/{total_targets} 部（已完成 {idx} 部）：《{item_name}》...",
            )

            # 推送前活跃队列二次防重：已在下载中的作品直接跳过，避免重复下种
            already_active = (
                _is_movie_in_active_downloads(active_downloads, item_tid, item_name)
                if item_is_movie
                else _is_tv_season_in_active_downloads(active_downloads, item_tid, item_name, item_season or 1)
            )
            if already_active:
                skipped_active_count += 1
                item["audit_status"] = "already_queued"
                item["reason"] = (
                    "该电影当前已在 MoviePilot 活跃下载队列中，已跳过重复推送"
                    if item_is_movie
                    else f"该剧 S{(item_season or 1):02d} 当前已在 MoviePilot 活跃下载队列中，已跳过重复推送"
                )
                _store_item_in_resource_cache(res_cache, item)
                cache_dirty = True
                details.append({
                    "sub_id": item["id"],
                    "name": item["name"],
                    "ok": True,
                    "skipped_active": True,
                    "pushed": 0,
                    "cleared_transfer_count": 0,
                })
                update_batch_action_progress(
                    processed=idx + 1,
                    success_count=pushed_count,
                    fail_count=failed_count,
                    progress_pct=min(99, int(100 * (idx + 1) / max(1, total_targets))),
                    message=f"已处理 {idx + 1}/{total_targets} 部：《{item_name}》已在队列中跳过 (已推 {pushed_count} · 跳过 {skipped_active_count} · 失败 {failed_count})",
                )
                continue

            sub_pushed, sub_errs, sites_tried, payload_titles, failed_keys = await _submit_payloads(
                list(item.get("_selected_payloads") or [])
            )
            retried_live = False

            # 缓存种子全部推送失败时，自动触发一次实时重搜并换有效活种重试（防范 PT 站短时效签名直链过期或单站临时失效）
            if sub_pushed == 0 and item_tid.isdigit():
                log.warning(
                    "[订阅补缺 %s] 缓存种子提交失败（站点: %s），自动触发实时重搜回退: %s",
                    item_name,
                    "/".join(sites_tried) or "未知",
                    "; ".join(sub_errs),
                )
                try:
                    if fallback_category_plans is None:
                        fallback_category_plans = {}
                        for cat_key in ("non_anime", "anime"):
                            try:
                                fallback_category_plans[cat_key] = await resolve_category_gap_fill_sites(
                                    db, cat_key, mp=mp
                                )
                            except Exception:
                                fallback_category_plans[cat_key] = {"ready": False}
                    if tmdb_fallback is None:
                        tmdb_fallback = TMDBClient()

                    s_snap = snap_map.get(int(item["id"])) or {}
                    retry_sub: dict[str, Any] = dict(s_snap)
                    retry_sub.setdefault("id", item["id"])
                    retry_sub.setdefault("name", item_name)
                    retry_sub.setdefault("year", item.get("year") or "")
                    retry_sub["type"] = "电影" if item_is_movie else "电视剧"
                    retry_sub["media_type"] = "movie" if item_is_movie else "tv"
                    retry_sub["tmdbid"] = int(item_tid)
                    retry_sub["tmdb_id"] = int(item_tid)
                    if not item_is_movie:
                        retry_sub["season"] = item_season or 1
                        if item.get("total_episodes"):
                            retry_sub["total_episode"] = item["total_episodes"]
                        if item.get("cd2_verified") is not None:
                            retry_sub["cd2_verified"] = bool(item["cd2_verified"])
                        if s_snap.get("cd2_episode_numbers") is not None:
                            retry_sub["cd2_episode_numbers"] = list(s_snap["cd2_episode_numbers"])
                        elif item.get("missing_episodes") and item.get("total_episodes"):
                            miss_set = set(item["missing_episodes"])
                            retry_sub["cd2_episode_numbers"] = [
                                e for e in range(1, int(item["total_episodes"]) + 1) if e not in miss_set
                            ]
                            retry_sub["cd2_total_episodes"] = len(retry_sub["cd2_episode_numbers"])

                    settings = get_settings()
                    fresh_item = await _audit_single_subscription_item(
                        retry_sub,
                        mp=mp,
                        tmdb=tmdb_fallback,
                        category_plans=fallback_category_plans,
                        active_downloads=active_downloads,
                        sem=asyncio.Semaphore(1),
                        is_aborted_fn=lambda: False,
                        min_seeders=max(1, int(settings.gap_fill_min_seeders or 2)),
                        tier_ratio=0.10,
                        min_slack=1,
                        min_resolution="any",
                        require_chinese=False,
                    )
                    if fresh_item and fresh_item.get("audit_status") == "downloadable" and fresh_item.get("_selected_payloads"):
                        fresh_payloads = [
                            p
                            for p in fresh_item.get("_selected_payloads") or []
                            if (
                                str((p.get("torrent_info") or {}).get("enclosure") or "").strip(),
                                str((p.get("torrent_info") or {}).get("title") or "").strip(),
                            )
                            not in failed_keys
                        ]
                        if fresh_payloads:
                            retried_live = True
                            r_pushed, r_errs, r_sites, r_titles, _ = await _submit_payloads(fresh_payloads)
                            for s_lbl in r_sites:
                                if s_lbl not in sites_tried:
                                    sites_tried.append(s_lbl)
                            if r_pushed > 0:
                                sub_pushed = r_pushed
                                retried_success_count += 1
                                payload_titles = r_titles or payload_titles
                                item["selected_torrents"] = fresh_item.get("selected_torrents") or []
                                item["plan_summary"] = fresh_item.get("plan_summary") or ""
                                item["_selected_payloads"] = fresh_payloads
                                item["checked_at"] = fresh_item.get("checked_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            elif r_errs:
                                sub_errs.append("实时重搜重试仍失败：" + "; ".join(r_errs))
                    elif fresh_item and fresh_item.get("audit_status") == "no_resource":
                        retried_live = True
                        fresh_reason = str(fresh_item.get("reason") or "实时重搜当前无合格可用资源")
                        sub_errs.append(f"实时重搜结果：{fresh_reason}")
                        item["audit_status"] = "no_resource"
                        item["reason"] = fresh_reason
                        item["plan_summary"] = ""
                        item["selected_torrents"] = []
                        item["_selected_payloads"] = []
                        item["checked_at"] = fresh_item.get("checked_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        _store_item_in_resource_cache(res_cache, item)
                        cache_dirty = True
                except Exception as retry_exc:
                    log.warning("[订阅补缺 %s] 失败后实时重搜异常: %s", item_name, retry_exc)

            item_cleared_transfer = 0
            if sub_pushed > 0:
                # 对齐独立补缺模块：电视剧推送成功后立即清理 MP「媒体整理」该季旧转移记录（仅删记录，绝不删实体文件）
                if not item_is_movie and hasattr(mp, "clear_transfer_history_for_episodes"):
                    try:
                        aliases = list(dict.fromkeys([t for t in [item_name, *payload_titles] if t]))
                        try:
                            clean_res = await mp.clear_transfer_history_for_episodes(
                                title=item_name,
                                season=item_season or 1,
                                episodes=None,
                                explicit_only=False,
                                tmdb_id=item_tid or None,
                                title_aliases=aliases,
                            )
                        except TypeError:
                            clean_res = await mp.clear_transfer_history_for_episodes(
                                title=item_name,
                                season=item_season or 1,
                                episodes=None,
                            )
                        if isinstance(clean_res, dict):
                            item_cleared_transfer = int(clean_res.get("cleared_count") or 0)
                            cleared_transfer_total += item_cleared_transfer
                            if item_cleared_transfer > 0:
                                log.info(
                                    "[订阅补缺 %s S%02d] 推送下载后已清理 MoviePilot「媒体整理」中 %d 条转移记录（仅删记录，不删实体文件）",
                                    item_name,
                                    item_season or 1,
                                    item_cleared_transfer,
                                )
                    except Exception as clean_err:
                        log.warning(
                            "[订阅补缺 %s] 推送下载后清理 MoviePilot 转移历史记录异常（不阻断下载）: %s",
                            item_name,
                            clean_err,
                        )

                pushed_count += 1
                item["audit_status"] = "already_queued"
                item["reason"] = (
                    f"已成功推送 {sub_pushed} 个补缺种子至 MoviePilot 下载队列"
                    + ("（原缓存失效，已自动重搜换种成功）" if retried_live else "")
                    + (f"（已清 {item_cleared_transfer} 条 MP 转移记录）" if item_cleared_transfer > 0 else "")
                )
                _store_item_in_resource_cache(res_cache, item)
                cache_dirty = True
                db.add(OperationLog(
                    action="subscription_gap_fill_push",
                    status="completed",
                    detail_json=json.dumps({
                        "audit_id": audit_id or "unified",
                        "sub_id": item["id"],
                        "name": item["name"],
                        "media_type": item["media_type"],
                        "season": item.get("season"),
                        "pushed_torrents": sub_pushed,
                        "retried_live": retried_live,
                        "cleared_transfer_count": item_cleared_transfer,
                    }, ensure_ascii=False),
                ))
                details.append({
                    "sub_id": item["id"],
                    "name": item["name"],
                    "ok": True,
                    "pushed": sub_pushed,
                    "sites": sites_tried,
                    "retried_live": retried_live,
                    "cleared_transfer_count": item_cleared_transfer,
                })
            else:
                failed_count += 1
                err_text = "; ".join(sub_errs) or "推送失败"
                log.warning(
                    "[订阅补缺 %s] 推送最终失败（站点: %s）: %s",
                    item_name,
                    "/".join(sites_tried) or "未知",
                    err_text,
                )
                details.append({
                    "sub_id": item["id"],
                    "name": item["name"],
                    "ok": False,
                    "sites": sites_tried,
                    "retried_live": retried_live,
                    "error": err_text,
                })

            update_batch_action_progress(
                processed=idx + 1,
                success_count=pushed_count,
                fail_count=failed_count,
                progress_pct=min(99, int(100 * (idx + 1) / max(1, total_targets))),
                message=f"已完成 {idx + 1}/{total_targets} 部：《{item_name}》 (成功 {pushed_count} · 失败 {failed_count})",
            )

        if cache_dirty or pushed_count > 0 or skipped_active_count > 0:
            await save_resource_audit_cache(db, res_cache)
            await db.commit()

        msg = f"批量补缺下载完成：成功推送 {pushed_count} 部作品至 MoviePilot 下载队列"
        if retried_success_count > 0:
            msg += f"（其中 {retried_success_count} 部缓存失效已自动重搜换种成功）"
        if cleared_transfer_total > 0:
            msg += f"，并同步清理 MP 媒体整理中 {cleared_transfer_total} 条历史转移记录（仅删记录，绝不删实体文件）"
        if skipped_active_count > 0:
            msg += f"，跳过 {skipped_active_count} 部已在活跃下载队列中的作品"
        if failed_count > 0:
            msg += f"，失败 {failed_count} 部"
        update_batch_action_progress(
            status="completed",
            processed=total_targets,
            success_count=pushed_count,
            fail_count=failed_count,
            progress_pct=100,
            message=msg,
        )
        return {
            "ok": pushed_count > 0 or skipped_active_count > 0,
            "pushed_count": pushed_count,
            "retried_success_count": retried_success_count,
            "skipped_active_count": skipped_active_count,
            "cleared_transfer_count": cleared_transfer_total,
            "failed_count": failed_count,
            "message": msg,
            "details": details,
        }
    except Exception as exc:
        update_batch_action_progress(
            status="failed",
            message=f"批量补缺下载异常: {exc}",
        )
        raise
    finally:
        await mp.close()
        if tmdb_fallback is not None:
            await tmdb_fallback.close()
