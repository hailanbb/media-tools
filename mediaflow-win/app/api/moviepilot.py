"""MoviePilot 代理端点 — 透传前端请求到 MoviePilot API。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.moviepilot import MoviePilotClient
from app.config import get_settings
from app.core.subscription_cleaner import diagnose_subscription, evaluate_subscriptions
from datetime import datetime, timezone
import json
import logging
import re
from typing import Any

from app.database import get_db
from app.schemas.common import StatusResponse

log = logging.getLogger("mediaflow.api.moviepilot")

router = APIRouter()


class SubscriptionPlanRequest(BaseModel):
    cleanup: bool = False
    season_record_id: str | None = None


@router.post("/subscriptions/{sub_id}/resource-check")
async def check_subscription_resources(sub_id: int):
    from app.services.subscription_service import subscription, resource_check
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        sub = await subscription(mp, sub_id)
        if not sub:
            raise HTTPException(404, "订阅不存在")
        return await resource_check(mp, sub)
    finally:
        await mp.close()


@router.get("/subscriptions/{sub_id}/sources")
async def subscription_sources(sub_id: int, db: AsyncSession = Depends(get_db)):
    from app.services.subscription_service import discover_source_season_entries, subscription
    from app.clients.clouddrive2 import CloudDrive2Client
    _check_mp_configured()
    mp, disk = MoviePilotClient(), CloudDrive2Client()
    try:
        sub = await subscription(mp, sub_id)
        if not sub:
            raise HTTPException(404, "订阅不存在")
        return await discover_source_season_entries(db, disk, sub)
    finally:
        await mp.close()
        await disk.close()


@router.post("/subscriptions/{sub_id}/operation-preview")
async def preview_subscription_operation(sub_id: int, body: SubscriptionPlanRequest, db: AsyncSession = Depends(get_db)):
    from app.services.subscription_service import preview
    from app.clients.clouddrive2 import CloudDrive2Client
    _check_mp_configured()
    mp, disk = MoviePilotClient(), CloudDrive2Client()
    try:
        return await preview(db, mp, disk, sub_id, body.cleanup, body.season_record_id)
    finally:
        await mp.close()
        await disk.close()


@router.get("/subscription-operations")
async def list_subscription_operations(db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    from app.models.operation import OperationLog
    logs = await db.scalars(select(OperationLog).where(OperationLog.action == "subscription_cleanup",
        OperationLog.status != "completed").order_by(OperationLog.created_at.desc()).limit(100))
    return [{"operation_id": op.id, "status": op.status, **json.loads(op.detail_json)} for op in logs]


@router.post("/subscription-operations/{operation_id}/execute")
async def execute_subscription_operation(operation_id: str, db: AsyncSession = Depends(get_db)):
    from app.services.subscription_service import execute
    from app.clients.clouddrive2 import CloudDrive2Client
    _check_mp_configured()
    mp, disk = MoviePilotClient(), CloudDrive2Client()
    try:
        return await execute(db, mp, disk, operation_id)
    finally:
        await mp.close()
        await disk.close()


def _check_mp_configured() -> None:
    settings = get_settings()
    if not settings.mp_url or not settings.mp_token:
        raise HTTPException(status_code=503, detail="MoviePilot 未配置")


# ---------- 订阅管理 ----------

class BatchDeleteSubscriptionsRequest(BaseModel):
    sub_ids: list[int]
    titles: list[str] | None = None
    force: bool = False  # 是否强制跳过保护检查（用于用户明确确认的人工清理）


class SubscriptionStatusUpdateRequest(BaseModel):
    state: str  # "S" (暂停) | "R" (追更) | "P" (待定)


class BatchUpdateStatusRequest(BaseModel):
    sub_ids: list[int]
    state: str  # "S" | "R" | "P"


class SubscriptionCleanPreviewRequest(BaseModel):
    media_type: str = "all"  # "all" | "tv" | "movie"
    mode: str = "safe"       # tv: "safe" | "never_downloaded" | "partial_stalled" | "short_tv" | "low_quality" | "stalled" | "deep"
                             # movie: "safe" | "old_zombie" | "low_quality" | "douban" | "deep"


class SubscriptionCd2SyncRequest(BaseModel):
    scope: str = "all"  # "all" | "tv" | "movie"


class UnifiedSubscriptionQueryRequest(BaseModel):
    media_type: str = "all"  # "all" | "tv" | "movie"
    search_resources: bool = True
    reuse_resource_cache: bool = True


class BatchResourceAuditRequest(BaseModel):
    media_type: str = "tv"           # "tv" | "movie"
    bucket: str = "partial_stalled"  # tv: "partial_stalled" | "never_downloaded" | "all_stalled" ; movie: "old_zombie" | "never_downloaded"
    sub_ids: list[int] | None = None
    limit: int = 40


class BatchSubscriptionGapFillRequest(BaseModel):
    audit_id: str | None = "unified"
    sub_ids: list[int]


@router.get("/subscriptions")
async def list_subscriptions(
    media_type: str | None = None,
    cached_only: bool = False,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """获取订阅治理数据（支持 cached_only=True 秒读上次统一查询快照，绝不自动触发耗时远程请求）。"""
    from app.services.subscription_governance_service import (
        assemble_subscriptions_payload,
        get_cached_or_empty_subscriptions_payload,
    )
    if cached_only:
        return await get_cached_or_empty_subscriptions_payload(db, media_type=media_type)

    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        raw_subs = await mp.list_subscriptions()
        payload = await assemble_subscriptions_payload(db, raw_subs, save_as_snapshot=True)
        if media_type:
            payload = dict(payload)
            payload["subscriptions"] = [
                x for x in payload["subscriptions"] if x.get("media_type") == media_type
            ]
        return payload
    finally:
        await mp.close()


@router.post("/subscriptions/unified-query")
async def trigger_unified_subscriptions_query(
    body: UnifiedSubscriptionQueryRequest | None = None,
) -> dict:
    """启动统一三阶段订阅数据采集流水线（MoviePilot 订阅 + CD2 实盘库存 + 断更/老片站点补缺资源）。"""
    _check_mp_configured()
    from app.database import async_session
    from app.services.subscription_governance_service import start_unified_subscription_query
    search_res = True if body is None else bool(body.search_resources)
    reuse_cache = True if body is None else bool(body.reuse_resource_cache)
    req_media_type = (body.media_type.lower() if body and body.media_type else "all").strip()
    if req_media_type not in ("all", "tv", "movie"):
        req_media_type = "all"
    return start_unified_subscription_query(
        async_session,
        media_type=req_media_type,
        search_resources=search_res,
        reuse_resource_cache=reuse_cache,
    )


@router.get("/subscriptions/unified-query/status")
async def get_unified_subscriptions_query_status(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """查询统一三阶段订阅数据采集进度与快照版本号。"""
    from app.services.subscription_governance_service import (
        get_unified_query_status,
        load_unified_snapshot,
    )
    await load_unified_snapshot(db)
    return get_unified_query_status()


@router.post("/subscriptions/unified-query/abort")
async def abort_unified_subscriptions_query() -> dict:
    """停止进行中的统一查询（保留已采集的 MoviePilot、CD2 与站点资源数据）。"""
    from app.services.subscription_governance_service import abort_unified_subscription_query
    return abort_unified_subscription_query()


@router.get("/subscriptions/batch-action/status")
async def get_subscription_batch_action_status() -> dict:
    """查询当前批量操作（批量取消订阅 / 批量补缺下载 / 批量变更状态）的实时进度。"""
    from app.services.subscription_governance_service import get_batch_action_status
    return get_batch_action_status()


@router.post("/subscriptions/sync-cd2")
async def sync_subscriptions_cd2(body: SubscriptionCd2SyncRequest | None = None) -> dict:
    """触发后台异步 CD2 全盘实盘库存同步（电影入库判定与剧集实存集数均以 CD2 为准）。"""
    _check_mp_configured()
    from app.database import async_session
    from app.services.subscription_cd2_index import trigger_cd2_sync_background
    scope = (body.scope if body else "all") or "all"
    return trigger_cd2_sync_background(async_session, scope=scope)


@router.get("/subscriptions/sync-cd2/status")
async def get_subscriptions_cd2_sync_status(db: AsyncSession = Depends(get_db)) -> dict:
    """查询 CD2 实盘库存同步任务进度与当前持久化快照概要。"""
    from app.services.subscription_cd2_index import get_cd2_sync_status, load_cd2_snapshot
    await load_cd2_snapshot(db)
    return get_cd2_sync_status()


@router.post("/subscriptions/batch-resource-audit")
async def start_subscription_batch_audit(
    body: BatchResourceAuditRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """启动异步批量资源梳理（复用补缺选种引擎，筛查断更停滞剧集或往年老片死种的可用资源）。"""
    _check_mp_configured()
    from app.database import async_session
    from app.services.subscription_governance_service import start_batch_resource_audit
    return await start_batch_resource_audit(
        db,
        async_session,
        media_type=body.media_type,
        bucket=body.bucket,
        sub_ids=body.sub_ids,
        limit=body.limit,
    )


@router.get("/subscriptions/batch-resource-audit/{audit_id}")
async def poll_subscription_batch_audit(audit_id: str) -> dict:
    """轮询批量资源梳理进度与分组结果（可补缺 vs 无源待退订）。"""
    from app.services.subscription_governance_service import get_batch_audit_status
    res = get_batch_audit_status(audit_id)
    if res is None:
        raise HTTPException(404, "未找到对应的梳理会话")
    return res


@router.post("/subscriptions/batch-resource-audit/{audit_id}/abort")
async def abort_subscription_batch_audit_endpoint(audit_id: str) -> dict:
    """优雅中止进行中的批量资源梳理任务，已扫出结果完整保留。"""
    from app.services.subscription_governance_service import abort_batch_resource_audit
    return abort_batch_resource_audit(audit_id)


@router.post("/subscriptions/batch-gap-fill-push")
async def push_subscription_batch_gap_fill(
    body: BatchSubscriptionGapFillRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """对批量梳理筛出的合格作品，一键批量推送补缺下载至 MoviePilot。"""
    _check_mp_configured()
    from app.services.subscription_governance_service import execute_batch_subscription_gap_fill
    return await execute_batch_subscription_gap_fill(
        db,
        audit_id=body.audit_id,
        sub_ids=body.sub_ids,
    )


@router.post("/subscriptions/{sub_id}/status", response_model=StatusResponse)
@router.put("/subscriptions/{sub_id}/status", response_model=StatusResponse)
async def update_single_subscription_status(
    sub_id: int,
    body: SubscriptionStatusUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> StatusResponse:
    """变更单个订阅状态（如置为 S 暂停观察，或置为 R 恢复追更）。"""
    _check_mp_configured()
    mp = MoviePilotClient()
    from app.models.operation import OperationLog
    try:
        res = await mp.update_subscription_status(sub_id, body.state)
        state_name = "暂停观察 (S)" if body.state == "S" else ("活跃追更 (R)" if body.state == "R" else f"待定 ({body.state})")
        op_log = OperationLog(
            action="update_subscription_status",
            status="completed",
            detail_json=json.dumps({"sub_id": sub_id, "state": body.state, "state_name": state_name}, ensure_ascii=False),
        )
        db.add(op_log)
        await db.commit()
        from app.services.subscription_governance_service import mutate_unified_snapshot_on_state_change
        await mutate_unified_snapshot_on_state_change(db, [sub_id], body.state)
        return StatusResponse(ok=True, message=f"已成功将订阅 #{sub_id} 变更为 {state_name}")
    except Exception as exc:
        return StatusResponse(ok=False, message=f"变更订阅状态失败: {exc}")
    finally:
        await mp.close()


@router.post("/subscriptions/batch-status", response_model=StatusResponse)
async def batch_update_subscription_status(
    body: BatchUpdateStatusRequest,
    db: AsyncSession = Depends(get_db),
) -> StatusResponse:
    """批量变更订阅状态（如将无源候选批量置为 S 暂停观察）。"""
    if not body.sub_ids:
        return StatusResponse(ok=False, message="未选择任何订阅")

    _check_mp_configured()
    mp = MoviePilotClient()
    from app.models.operation import OperationLog
    from app.services.subscription_governance_service import (
        mutate_unified_snapshot_on_state_change,
        update_batch_action_progress,
    )
    success_count = 0
    fail_count = 0
    errors: list[str] = []
    total_count = len(body.sub_ids)
    state_name = "暂停观察 (S)" if body.state == "S" else ("活跃追更 (R)" if body.state == "R" else f"待定 ({body.state})")

    update_batch_action_progress(
        action_type="status_update",
        action_label=f"批量设为{state_name}",
        status="running",
        total=total_count,
        processed=0,
        success_count=0,
        fail_count=0,
        progress_pct=5,
        current_title="",
        message=f"正在批量更新 {total_count} 项订阅状态为 {state_name}...",
    )

    try:
        for idx, sid in enumerate(body.sub_ids):
            update_batch_action_progress(
                processed=idx,
                current_title=f"订阅 #{sid}",
                progress_pct=8 + int(88 * idx / max(1, total_count)),
                message=f"正在变更状态 ({idx + 1}/{total_count})：订阅 #{sid}...",
            )
            try:
                await mp.update_subscription_status(sid, body.state)
                success_count += 1
            except Exception as e:
                fail_count += 1
                errors.append(f"订阅 #{sid}: {e}")
            update_batch_action_progress(
                processed=idx + 1,
                success_count=success_count,
                fail_count=fail_count,
                progress_pct=10 + int(88 * (idx + 1) / max(1, total_count)),
                message=f"已变更状态 ({idx + 1}/{total_count}) · 成功 {success_count} / 失败 {fail_count}",
            )

        if success_count > 0:
            op_log = OperationLog(
                action="batch_update_subscription_status",
                status="completed",
                detail_json=json.dumps({"sub_ids": body.sub_ids, "state": body.state, "success_count": success_count}, ensure_ascii=False),
            )
            db.add(op_log)
            await db.commit()
            await mutate_unified_snapshot_on_state_change(db, body.sub_ids, body.state)

        msg = f"批量变更状态完成：成功 {success_count} 项设置为 {state_name}"
        if fail_count > 0:
            msg += f"，失败 {fail_count} 项"
        update_batch_action_progress(
            status="completed",
            processed=total_count,
            success_count=success_count,
            fail_count=fail_count,
            progress_pct=100,
            message=msg,
        )
        return StatusResponse(ok=success_count > 0, message=msg)
    finally:
        await mp.close()


@router.get("/subscriptions/{sub_id}/mediaserver-check")
async def check_subscription_mediaserver(
    sub_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """实时核查单个订阅的入库实情（优先以 CD2 实盘为准，无 CD2 配置时降级至媒体服务器）。"""
    _check_mp_configured()
    from app.services.subscription_cd2_index import check_single_subscription_cd2
    from app.services.subscription_service import subscription, is_movie_sub
    settings = get_settings()
    mp = MoviePilotClient()
    try:
        sub = await subscription(mp, sub_id)
        if not sub:
            raise HTTPException(404, "订阅不存在")

        # 优先执行 CD2 实盘核验（2.4 & 3.1：以 CD2 为准）
        if settings.cd2_url and (settings.source_root or settings.archive_root):
            cd2_res = await check_single_subscription_cd2(db, sub)
            if cd2_res.get("checked"):
                return cd2_res

        # 兜底：无 CD2 配置时查询 MoviePilot 媒体服务器索引
        tid = int(sub.get("tmdbid") or sub.get("tmdb_id") or 0)
        title = str(sub.get("name") or sub.get("title") or "")
        year = str(sub.get("year") or "")
        is_movie = is_movie_sub(sub)

        if not tid:
            return {"ok": False, "in_library": False, "message": "订阅无有效 TMDB ID，无法查库"}

        if is_movie:
            chk = await mp.check_mediaserver_movie_exists(tid, title=title, year=year)
            return {
                "ok": chk.get("checked", False),
                "source": "mediaserver",
                "media_type": "movie",
                "in_library": chk.get("exists", False),
                "details": chk,
                "message": "媒体服务器中已存在该电影完整条目" if chk.get("exists") else "媒体服务器中未见该电影",
            }
        else:
            chk = await mp.check_mediaserver_exists_remote(tid, title=title, year=year, media_type="电视剧")
            eps_map = chk.get("season_episodes", {})
            season = int(sub.get("season") or 1)
            season_eps = eps_map.get(season, [])
            return {
                "ok": chk.get("checked", False),
                "source": "mediaserver",
                "media_type": "tv",
                "season": season,
                "in_library_episodes": season_eps,
                "count": len(season_eps),
                "message": f"S{str(season).zfill(2)} 已入库 {len(season_eps)} 集" if season_eps else "媒体服务器中未见该季入库集数",
            }
    finally:
        await mp.close()


@router.post("/subscriptions/clean-preview")
async def preview_subscription_cleanup(
    body: SubscriptionCleanPreviewRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """生成订阅清理审计预案，按媒体类型（电视剧 vs 电影）与精细化策略模式筛选（叠加 CD2 实盘真相）。"""
    _check_mp_configured()
    from app.services.subscription_cd2_index import enrich_subscriptions_with_cd2_and_cache
    mp = MoviePilotClient()
    try:
        raw_subs = await mp.list_subscriptions()
        subs, cd2_meta = await enrich_subscriptions_with_cd2_and_cache(db, raw_subs)
        eval_result = evaluate_subscriptions(subs)

        matched = []
        mode_desc = ""

        for diag in eval_result["items"]:
            # 1. 媒体类型过滤
            if body.media_type != "all" and diag.media_type != body.media_type:
                continue

            # 2. 差异化策略规则匹配
            if body.media_type == "movie":
                if body.mode == "safe":
                    mode_desc = "电影安全清理 (CD2 核实已入库完成)"
                    if diag.clean_rule == "completed":
                        matched.append(diag)
                elif body.mode == "old_zombie":
                    mode_desc = "往年老片无源死种清理 (建议先筛查可用资源)"
                    if diag.clean_rule in ("zombie", "douban_stale"):
                        matched.append(diag)
                elif body.mode == "low_quality":
                    mode_desc = "异常特征电影清理 (无封面/繁体/小语种/无中文名)"
                    if (diag.clean_rule == "low_quality" or (diag.is_low_quality and diag.clean_rule != "completed")) and not diag.is_protected:
                        matched.append(diag)
                elif body.mode == "douban":
                    mode_desc = "榜单同步长期无源清理"
                    if diag.is_douban_sync and diag.clean_rule in ("douban_stale", "zombie", "completed"):
                        matched.append(diag)
                else:  # deep
                    mode_desc = "电影深度清理 (已入库 + 异常特征 + 往年死种，锁定待映与收藏)"
                    if diag.clean_rule in ("completed", "zombie", "douban_stale", "low_quality") and not diag.is_protected:
                        matched.append(diag)

            elif body.media_type == "tv":
                if body.mode == "safe":
                    mode_desc = "电视剧安全清理 (CD2 全季已齐完结)"
                    if diag.clean_rule == "completed":
                        matched.append(diag)
                elif body.mode == "never_downloaded":
                    mode_desc = "零进度死种清理 (自订阅起实存 0 集)"
                    if diag.clean_rule == "zombie":
                        matched.append(diag)
                elif body.mode == "partial_stalled":
                    mode_desc = "部分已下·断更停滞 (强烈建议先用补缺模块批量梳理)"
                    if diag.clean_rule == "stalled":
                        matched.append(diag)
                elif body.mode == "short_tv":
                    mode_desc = "微短剧/短剧停滞清理"
                    if diag.clean_rule == "short_tv_stalled":
                        matched.append(diag)
                elif body.mode == "low_quality":
                    mode_desc = "异常特征剧集清理 (无封面/繁体/小语种/无中文名)"
                    if (diag.clean_rule == "low_quality" or (diag.is_low_quality and diag.clean_rule != "completed")) and not diag.is_protected:
                        matched.append(diag)
                elif body.mode == "stalled":
                    mode_desc = "剧集断更与零进度死种清理"
                    if diag.clean_rule in ("stalled", "zombie"):
                        matched.append(diag)
                else:  # deep
                    mode_desc = "电视剧深度清理 (全季完结 + 异常特征 + 零进度死种 + 短剧停滞)"
                    if diag.clean_confidence in ("high", "medium") and not diag.is_protected:
                        matched.append(diag)

            else:  # all 全量模式
                if body.mode == "safe":
                    mode_desc = "全局安全清理 (仅全季完结与已入库电影)"
                    if diag.clean_confidence == "high":
                        matched.append(diag)
                else:
                    mode_desc = "全局深度清理 (含停滞、异常特征与往年死种，保护新片与连载)"
                    if diag.clean_confidence in ("high", "medium") and not diag.is_protected:
                        matched.append(diag)

        # 构建原始订阅字典映射以补全海报与 TMDB ID
        sub_map = {}
        for s in subs:
            sid = s.get("id") or s.get("sub_id")
            if sid is not None:
                try:
                    sub_map[int(sid)] = s
                except (ValueError, TypeError):
                    pass

        clean_items = [
            {
                "id": m.sub_id,
                "sub_id": m.sub_id,
                "name": m.name,
                "year": m.year,
                "media_type": m.media_type,
                "season": m.season,
                "state": m.state,
                "tmdb_id": sub_map.get(m.sub_id, {}).get("tmdbid") or sub_map.get(m.sub_id, {}).get("tmdb_id"),
                "poster": sub_map.get(m.sub_id, {}).get("backdrop") or sub_map.get(m.sub_id, {}).get("poster") or "",
                "clean_confidence": m.clean_confidence,
                "clean_rule": m.clean_rule,
                "clean_rule_name": m.clean_rule_name,
                "clean_reason": m.clean_reason,
                "is_short_tv": m.is_short_tv,
                "short_drama_reason": m.short_drama_reason,
                "username": m.username,
                "release_stage": m.release_stage,
                "in_library_status": m.in_library_status,
                "total_episodes": m.total_episodes,
                "completed_episodes": m.completed_episodes,
                "lack_episodes": m.lack_episodes,
                "stalled_kind": m.stalled_kind,
                "can_gap_fill": m.can_gap_fill,
                "is_low_quality": m.is_low_quality,
                "low_quality_flags": m.low_quality_flags,
                "low_quality_reasons": m.low_quality_reasons,
                "data_source": m.data_source,
                "cd2_verified": m.cd2_verified,
                "cd2_source_episodes": m.cd2_source_episodes,
                "cd2_archive_episodes": m.cd2_archive_episodes,
                "cd2_paths": m.cd2_paths,
                "mp_completed_episodes": m.mp_completed_episodes,
                "progress": f"{m.completed_episodes}/{m.total_episodes or '?'}" if m.media_type == "tv" else (m.year or "电影"),
            }
            for m in matched
        ]

        return {
            "media_type": body.media_type,
            "mode": body.mode,
            "mode_name": mode_desc,
            "matched_count": len(matched),
            "total_evaluated": len(eval_result["items"]),
            "cd2_meta": cd2_meta,
            "to_clean": clean_items,
            "subscriptions": clean_items,  # 兼容字段
            "to_delete": clean_items,      # 兼容字段
            "to_keep": [],                 # 兼容字段
        }
    finally:
        await mp.close()


@router.post("/subscriptions/batch-delete", response_model=StatusResponse)
async def batch_delete_subscriptions(
    body: BatchDeleteSubscriptionsRequest,
    db: AsyncSession = Depends(get_db),
) -> StatusResponse:
    """批量取消指定订阅（自动关联剧名并写入审计日志）。"""
    if not body.sub_ids:
        return StatusResponse(ok=False, message="未选择任何订阅")

    _check_mp_configured()
    from app.models.operation import OperationLog
    from app.services.subscription_governance_service import (
        mutate_unified_snapshot_on_delete,
        update_batch_action_progress,
    )
    mp = MoviePilotClient()
    success_count = 0
    fail_count = 0
    deleted_ids: list[int] = []
    errors: list[str] = []
    total_count = len(body.sub_ids)

    update_batch_action_progress(
        action_type="unsubscribe",
        action_label="批量取消订阅",
        status="running",
        total=total_count,
        processed=0,
        success_count=0,
        fail_count=0,
        progress_pct=5,
        current_title="",
        message=f"正在准备批量取消 {total_count} 项订阅，正在同步校验订阅状态...",
    )

    try:
        # 一次性获取当前订阅列表建立 id -> sub_dict 映射（避免在循环中重复请求 N 次 list_subscriptions）
        sub_name_map: dict[int, str] = {}
        sub_obj_map: dict[int, dict] = {}
        try:
            current_subs = await mp.list_subscriptions()
            for cs in current_subs:
                cs_id = cs.get("id") or cs.get("sub_id")
                if cs_id is not None:
                    cid = int(cs_id)
                    sub_obj_map[cid] = cs
                    sub_name_map[cid] = cs.get("name") or cs.get("title") or ""
        except Exception as map_err:
            log.warning("获取订阅列表映射失败: %s", map_err)

        for idx, sid in enumerate(body.sub_ids):
            title = (
                sub_name_map.get(sid)
                or (body.titles[idx] if body.titles and idx < len(body.titles) else f"订阅#{sid}")
            )
            update_batch_action_progress(
                processed=idx,
                current_title=title,
                progress_pct=10 + int(86 * idx / max(1, total_count)),
                message=f"正在取消订阅 ({idx + 1}/{total_count})：《{title}》...",
            )
            mp_deleted = False
            try:
                from app.services.subscription_service import subscription
                current_sub = sub_obj_map.get(int(sid)) if sub_obj_map else await subscription(mp, sid)
                if not current_sub or (not getattr(body, "force", False) and diagnose_subscription(current_sub).is_protected):
                    raise ValueError("订阅已不存在或处于保护状态，请重新核查")
                await mp.delete_subscription(sid)
                mp_deleted = True
                op_log = OperationLog(
                    action="unsubscribe",
                    status="completed",
                    detail_json=json.dumps({"sub_id": sid, "title": title}, ensure_ascii=False),
                )
                db.add(op_log)
                success_count += 1
                deleted_ids.append(int(sid))
            except Exception as e:
                if mp_deleted:
                    success_count += 1
                    deleted_ids.append(int(sid))
                    errors.append(f"退订 {title} 在 MP 已生效，但本地审计日志写入失败: {e}")
                else:
                    fail_count += 1
                    errors.append(f"退订 {title} 失败: {e}")

            update_batch_action_progress(
                processed=idx + 1,
                success_count=success_count,
                fail_count=fail_count,
                progress_pct=12 + int(86 * (idx + 1) / max(1, total_count)),
                message=f"已取消订阅 ({idx + 1}/{total_count})：《{title}》 (成功 {success_count} · 失败 {fail_count})",
            )

        if success_count > 0:
            await db.commit()
            await mutate_unified_snapshot_on_delete(db, deleted_ids)

        msg = f"批量清理完成：成功退订 {success_count} 项"
        if fail_count > 0:
            msg += f"，失败 {fail_count} 项"

        update_batch_action_progress(
            status="completed",
            processed=total_count,
            success_count=success_count,
            fail_count=fail_count,
            progress_pct=100,
            message=msg,
        )
        return StatusResponse(ok=success_count > 0, message=msg)
    finally:
        await mp.close()


@router.delete("/subscriptions/{sub_id}", response_model=StatusResponse)
async def delete_subscription(
    sub_id: int,
    db: AsyncSession = Depends(get_db),
) -> StatusResponse:
    """取消指定订阅。"""
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        await mp.delete_subscription(sub_id)
        from app.services.subscription_governance_service import mutate_unified_snapshot_on_delete
        await mutate_unified_snapshot_on_delete(db, [sub_id])
        return StatusResponse(ok=True, message=f"订阅 {sub_id} 已取消")
    except Exception as e:
        return StatusResponse(ok=False, message=str(e))
    finally:
        await mp.close()


# ---------- 转移队列 ----------

@router.get("/transfer-queue")
async def list_transfer_queue() -> list[dict]:
    """获取 MP 转移队列。"""
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        queue = await mp.get_transfer_queue()
        if isinstance(queue, list):
            return queue
        if isinstance(queue, dict):
            return queue.get("data") or queue.get("items") or []
        return []
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取转移队列失败: {e}")
    finally:
        await mp.close()


# ---------- 媒体整理智能修复 (Transfer Repair) ----------

class TransferRepairScanRequest(BaseModel):
    max_pages: int = 5
    page_size: int = 50


class TransferRepairBaselineResetRequest(BaseModel):
    clear_existing_items: bool = True
    custom_baseline_id: int | None = None


class TransferRepairExecuteRequest(BaseModel):
    item_ids: list[str]
    skip_preview: bool = False


class TransferRepairPlanUpdateRequest(BaseModel):
    title: str | None = None
    media_type: str | None = None
    tmdb_id: int | None = None
    season_number: int | None = None
    episode_numbers: list[int] | None = None
    part: str | None = None
    is_special: bool | None = None
    special_type: str | None = None
    special_handling: str | None = "standard"
    remember_scope: str | None = None
    numbering_mode: str | None = "standard"
    episode_offset: int | None = 0


class TransferRepairSettingsRequest(BaseModel):
    is_enabled: bool | None = None
    auto_repair_enabled: bool | None = None
    max_concurrency: int | None = None
    batch_quota: int | None = None
    retry_budget: int | None = None
    scan_interval_minutes: int | None = None


class TransferRepairRuleCreateRequest(BaseModel):
    scope_kind: str
    scope_key: str
    show_title: str
    media_type: str = "tv"
    tmdb_id: int | None = None
    season_number: int = 1
    numbering_mode: str = "standard"
    episode_offset: int = 0
    note: str = ""


@router.post("/transfer-repair/scan")
async def scan_transfer_repair(
    req: TransferRepairScanRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """启动 MoviePilot 整理失败扫描并执行真实存储核查与智能诊断 (R01, R02, R29)。"""
    from app.services.transfer_repair_service import scan_new_failures
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        max_p = req.max_pages if req else 5
        p_sz = req.page_size if req else 50
        return await scan_new_failures(db, mp, max_pages=max_p, page_size=p_sz)
    finally:
        await mp.close()


@router.post("/transfer-repair/baseline/reset")
async def reset_transfer_repair_baseline_endpoint(
    req: TransferRepairBaselineResetRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """重新锚定接管基线至 MoviePilot 最新失败 ID（可同步清理误导入旧记录）。"""
    from app.services.transfer_repair_service import reset_baseline
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        clear_items = req.clear_existing_items if req else True
        custom_id = req.custom_baseline_id if req else None
        res = await reset_baseline(db, mp, clear_existing_items=clear_items, custom_baseline_id=custom_id)
        if not res.get("ok"):
            raise HTTPException(502, res.get("message") or "基线重设失败")
        return res
    finally:
        await mp.close()


@router.get("/transfer-repair/summary")
async def get_transfer_repair_summary_endpoint(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """获取整理修复仪表盘统计数据与基线健康度 (R01, R29)。"""
    from app.services.transfer_repair_service import get_repair_summary
    mp = MoviePilotClient() if get_settings().mp_url else None
    try:
        return await get_repair_summary(db, mp)
    finally:
        if mp:
            await mp.close()


@router.get("/transfer-repair/items")
async def list_transfer_repair_items(
    tab: str = "pending",
    category: str | None = None,
    keyword: str | None = None,
    group_by: str = "work",
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """获取整理修复项列表与作品/故障组聚合视图。"""
    from app.services.transfer_repair_service import get_repair_items
    return await get_repair_items(
        db,
        tab=tab,
        category=category,
        keyword=keyword,
        group_by=group_by,
        limit=limit,
        offset=offset,
    )


@router.get("/transfer-repair/items/{item_id}")
async def get_transfer_repair_item_detail(
    item_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """获取单个修复项完整详情、原识别与新识别对比、真实证据快照与核验进度。"""
    from app.models.transfer_repair import TransferRepairItem
    item = await db.get(TransferRepairItem, item_id)
    if not item:
        raise HTTPException(404, "修复项不存在")
    return {
        "id": item.id,
        "source_path": item.source_path,
        "file_basename": item.file_basename,
        "file_size": item.file_size,
        "src_storage": item.src_storage or item.storage or "local",
        "dest_storage": item.dest_storage or item.target_storage or "",
        "latest_history_id": item.latest_history_id,
        "history_ids": json.loads(item.mp_history_ids_json or "[]"),
        "mp_title": item.mp_title,
        "mp_seasons": item.mp_seasons,
        "mp_episodes": item.mp_episodes,
        "mp_mode": item.mp_mode,
        "mp_date": item.mp_date,
        "title": item.title,
        "media_type": item.media_type,
        "tmdb_id": item.tmdb_id,
        "season_number": item.season_number,
        "episode_numbers": json.loads(item.episode_numbers_json or "[]"),
        "part": item.part,
        "is_special": item.is_special,
        "special_type": item.special_type,
        "target_path": item.target_path,
        "status": item.status,
        "error_category": item.error_category,
        "main_block_reason": item.main_block_reason,
        "failure_group_key": item.failure_group_key,
        "raw_error": item.raw_error,
        "resolution_code": item.resolution_code,
        "skip_reason": item.skip_reason,
        "plan": json.loads(item.plan_json or "{}"),
        "plan_hash": item.plan_hash,
        "evidence": json.loads(item.evidence_json or "{}"),
        "target_verify": json.loads(item.target_verify_json or "{}"),
        "preview": json.loads(item.preview_json or "{}"),
        "last_execution": json.loads(item.last_execution_json or "{}"),
        "verification": json.loads(item.verification_json or "{}"),
        "is_protected": item.is_protected,
        "retry_count": item.retry_count,
        "submitted_at": item.submitted_at.isoformat() if item.submitted_at else None,
        "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


@router.post("/transfer-repair/items/{item_id}/recheck")
async def recheck_transfer_repair_item_endpoint(
    item_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """对单条修复项执行只读重诊断（不等同重新整理，第 10.3 节）。"""
    from app.services.transfer_repair_service import recheck_single_item
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        res = await recheck_single_item(db, mp, item_id)
        if not res.get("ok"):
            raise HTTPException(400, res.get("message"))
        return res
    finally:
        await mp.close()


@router.post("/transfer-repair/items/{item_id}/preview")
async def preview_transfer_repair_item_endpoint(
    item_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """实时调用 MoviePilot preview=true 生成逐文件差异预览（R17）。"""
    from app.services.transfer_repair_service import preview_repair_item
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        return await preview_repair_item(db, mp, item_id)
    finally:
        await mp.close()


@router.post("/transfer-repair/items/{item_id}/verify")
async def verify_transfer_repair_item_endpoint(
    item_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """手动触发已提交任务的落地核验（检查 MP 历史与目标存储实物，第 7.3 节）。"""
    from app.services.transfer_repair_service import verify_repair_item
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        return await verify_repair_item(db, mp, item_id)
    finally:
        await mp.close()


@router.post("/transfer-repair/items/{item_id}/plan")
async def update_transfer_repair_plan_endpoint(
    item_id: str,
    req: TransferRepairPlanUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """用户补充一次选择并生成新计划预览 (R04, R24)。"""
    from app.services.transfer_repair_service import update_item_plan_choice
    res = await update_item_plan_choice(db, item_id, req.model_dump())
    if not res.get("ok"):
        raise HTTPException(400, res.get("message"))
    return res


@router.post("/transfer-repair/execute")
async def execute_transfer_repairs(
    req: TransferRepairExecuteRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """批量提交明确计划执行预览校验、整理并进入落地核验 (R03, R16, R17)。"""
    from app.services.transfer_repair_service import execute_repair_plans
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        return await execute_repair_plans(db, mp, req.item_ids, skip_preview=req.skip_preview)
    finally:
        await mp.close()


@router.post("/transfer-repair/items/{item_id}/protect")
async def toggle_item_protect(
    item_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """保护或解除保护修复项 (R26)。"""
    from app.models.transfer_repair import TransferRepairItem
    item = await db.get(TransferRepairItem, item_id)
    if not item:
        raise HTTPException(404, "修复项不存在")
    item.is_protected = not item.is_protected
    if item.is_protected:
        item.status = "protected"
        item.main_block_reason = "用户已手动设置保护，暂停自动修复"
    else:
        plan = json.loads(item.plan_json or "{}")
        item.status = "ready" if plan.get("safe_to_execute") else "diagnosing"
    item.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True, "is_protected": item.is_protected, "status": item.status}


@router.get("/transfer-repair/settings")
async def get_transfer_repair_settings(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """获取媒体整理自动化设置与基线（始终携带 mp_client 防止 0 基线固化）。"""
    from app.services.transfer_repair_service import get_or_create_baseline
    mp = MoviePilotClient() if get_settings().mp_url else None
    try:
        baseline = await get_or_create_baseline(db, mp)
    finally:
        if mp:
            await mp.close()
    return {
        "instance_id": baseline.instance_id,
        "baseline_status": baseline.baseline_status,
        "baseline_history_id": baseline.baseline_history_id,
        "baseline_date": baseline.baseline_date,
        "mp_version": baseline.mp_version,
        "takeover_at": baseline.takeover_at.isoformat() if baseline.takeover_at else None,
        "is_enabled": baseline.is_enabled,
        "auto_repair_enabled": baseline.auto_repair_enabled,
        "max_concurrency": baseline.max_concurrency,
        "batch_quota": baseline.batch_quota,
        "retry_budget": baseline.retry_budget,
        "scan_interval_minutes": baseline.scan_interval_minutes,
    }


@router.put("/transfer-repair/settings")
async def update_transfer_repair_settings(
    req: TransferRepairSettingsRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """更新媒体整理自动化设置。"""
    from app.services.transfer_repair_service import get_or_create_baseline
    mp = MoviePilotClient() if get_settings().mp_url else None
    try:
        baseline = await get_or_create_baseline(db, mp)
    finally:
        if mp:
            await mp.close()
    if req.is_enabled is not None:
        baseline.is_enabled = req.is_enabled
    if req.auto_repair_enabled is not None:
        baseline.auto_repair_enabled = req.auto_repair_enabled
    if req.max_concurrency is not None:
        baseline.max_concurrency = max(1, min(int(req.max_concurrency), 5))
    if req.batch_quota is not None:
        baseline.batch_quota = max(1, min(int(req.batch_quota), 50))
    if req.retry_budget is not None:
        baseline.retry_budget = max(1, min(int(req.retry_budget), 5))
    if req.scan_interval_minutes is not None:
        baseline.scan_interval_minutes = max(1, min(int(req.scan_interval_minutes), 1440))
    baseline.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True, "message": "设置已保存"}


@router.get("/transfer-repair/rules")
async def list_transfer_repair_rules(
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """查询整理复用规则列表 (R24)。"""
    from app.models.transfer_repair import TransferRepairRule
    res = await db.execute(select(TransferRepairRule).order_by(TransferRepairRule.created_at.desc()))
    rules = res.scalars().all()
    return [
        {
            "id": r.id,
            "scope_kind": r.scope_kind,
            "scope_key": r.scope_key,
            "show_title": r.show_title,
            "media_type": r.media_type,
            "tmdb_id": r.tmdb_id,
            "season_number": r.season_number,
            "numbering_mode": r.numbering_mode,
            "episode_offset": r.episode_offset,
            "is_active": r.is_active,
            "hit_count": r.hit_count,
            "note": r.note,
        }
        for r in rules
    ]


@router.post("/transfer-repair/rules")
async def create_transfer_repair_rule(
    req: TransferRepairRuleCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """新增媒体整理经验复用规则。"""
    from app.models.transfer_repair import TransferRepairRule
    rule = TransferRepairRule(
        scope_kind=req.scope_kind,
        scope_key=req.scope_key,
        show_title=req.show_title,
        media_type=req.media_type,
        tmdb_id=req.tmdb_id,
        season_number=req.season_number,
        numbering_mode=req.numbering_mode,
        episode_offset=req.episode_offset,
        note=req.note,
    )
    db.add(rule)
    await db.commit()
    return {"ok": True, "id": rule.id, "message": "规则已创建"}


@router.delete("/transfer-repair/rules/{rule_id}")
async def delete_transfer_repair_rule(
    rule_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """删除或停用经验复用规则。"""
    from app.models.transfer_repair import TransferRepairRule
    rule = await db.get(TransferRepairRule, rule_id)
    if not rule:
        raise HTTPException(404, "规则不存在")
    await db.delete(rule)
    await db.commit()
    return {"ok": True, "message": "规则已删除"}


@router.post("/transfer-repair/{item_id}", response_model=StatusResponse)
async def repair_transfer(
    item_id: str,
    db: AsyncSession = Depends(get_db),
) -> StatusResponse:
    """修复整理任务 — 优先执行文件计划，兼容旧队列直接重试。"""
    from app.models.transfer_repair import TransferRepairItem
    from app.services.transfer_repair_service import execute_repair_plans

    # 1. 如果存在精确的 TransferRepairItem 记录，通过新引擎执行
    item = await db.get(TransferRepairItem, item_id)
    if item:
        _check_mp_configured()
        mp = MoviePilotClient()
        try:
            exec_res = await execute_repair_plans(db, mp, [item_id])
            first_res = exec_res.get("results", [{}])[0]
            ok = bool(first_res.get("ok"))
            msg = first_res.get("message") or first_res.get("reason") or "执行完成"
            return StatusResponse(ok=ok, message=msg)
        finally:
            await mp.close()

    # 2. 否则回退兼容旧版队列逻辑
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        queue = await mp.get_transfer_queue()
        matched = None
        if isinstance(queue, list):
            for it in queue:
                if str(it.get("id")) == str(item_id) or str(it.get("item_id")) == str(item_id):
                    matched = it
                    break

        source_path = None
        if matched:
            source_path = (
                matched.get("source_path")
                or matched.get("src")
                or matched.get("path")
                or matched.get("file_path")
                or matched.get("title")
            )

        if not source_path:
            return StatusResponse(
                ok=False,
                message=f"未能在 MoviePilot 队列或修复库中找到任务 #{item_id}，建议在后台查看",
            )

        res = await mp.manual_transfer(source_path=source_path)
        success = res.get("success", True) if isinstance(res, dict) else True
        msg = (
            res.get("message")
            if isinstance(res, dict) and res.get("message")
            else f"已向 MoviePilot 提交任务 #{item_id} 重新转移: {source_path}"
        )
        return StatusResponse(ok=bool(success), message=msg)
    except Exception as e:
        return StatusResponse(ok=False, message=f"发起转移修复失败: {e}")
    finally:
        await mp.close()


# ---------- 资源搜索与下载 ----------

class _SearchRequest(BaseModel):
    keyword: str
    tmdb_id: int | None = None
    season: int | None = None


@router.post("/search-resources")
async def search_resources(body: _SearchRequest) -> list[dict]:
    """搜索 PT 资源。"""
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        results = await mp.search_resources(
            tmdb_id=body.tmdb_id,
            season=body.season,
            title=body.keyword,
        )
        return results[:50]  # 限制返回数量
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"资源搜索失败: {e}")
    finally:
        await mp.close()


class _DownloadRequest(BaseModel):
    title: str
    download_url: str
    media_info: dict | None = None
    torrent_info: dict | None = None
    clean_transfer_history: bool = True
    season: int | None = None
    episodes: list[int] | None = None
    show_title: str | None = None


@router.post("/download", response_model=StatusResponse)
async def download_resource(body: _DownloadRequest) -> StatusResponse:
    """提交资源下载（默认在下载前自动清理重叠的媒体整理历史，确保入库不被跳过）。"""
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        media = body.media_info or {"title": body.title}
        torrent = body.torrent_info or {"title": body.title, "enclosure": body.download_url}

        cleared_count = 0
        if body.clean_transfer_history:
            # 提取剧名、季、集
            raw_title = body.show_title or media.get("title") or body.title
            clean_title = re.sub(r"\[tmdbid[=:]\d+\]", "", str(raw_title), flags=re.I)
            clean_title = re.sub(r"[\(\[（].*?[\)\]）]", " ", clean_title)
            clean_title = re.sub(r"\s+\d{4}$", "", clean_title).strip()
            if not clean_title:
                clean_title = raw_title

            # 季号解析
            target_season = body.season
            if target_season is None:
                meta = body.torrent_info.get("meta_info") if isinstance(body.torrent_info, dict) else {}
                target_season = meta.get("begin_season") if isinstance(meta, dict) else None
                if target_season is None:
                    s_m = re.search(r"S(\d{1,2})", str(body.title), re.I)
                    if s_m:
                        target_season = int(s_m.group(1))

            if clean_title and target_season is not None:
                try:
                    c_res = await mp.clear_transfer_history_for_episodes(
                        title=clean_title,
                        season=target_season,
                        episodes=set(body.episodes) if body.episodes else None,
                    )
                    cleared_count = c_res.get("cleared_count", 0)
                except Exception as ce:
                    log.warning("下载前自动清理转移历史失败（不阻断下载）: %s", ce)

        await mp.submit_download(media, torrent)
        msg = "下载任务已提交"
        if cleared_count > 0:
            msg += f"（已前置仅清理 {cleared_count} 条旧整理记录，确保新剧集正常入库）"
        return StatusResponse(ok=True, message=msg)
    except Exception as e:
        return StatusResponse(ok=False, message=f"下载失败: {e}")
    finally:
        await mp.close()


# ---------- 媒体整理与转移历史 ----------

class InspectAndCleanTransferRequest(BaseModel):
    title: str
    season: int
    episodes: list[int] | None = None
    execute: bool = False  # False 则仅预览，True 则执行仅删除转移记录


@router.get("/transfer-history")
async def get_transfer_history(title: str | None = None, page: int = 1, count: int = 50) -> dict:
    """查询 MoviePilot 媒体整理历史记录。"""
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        items = await mp.get_transfer_history(title=title, page=page, count=count)
        return {"items": items, "total": len(items)}
    finally:
        await mp.close()


@router.post("/transfer-history/inspect-and-clean")
async def inspect_and_clean_transfer(body: InspectAndCleanTransferRequest) -> dict:
    """检查或执行【仅删除转移记录】（绝对不删除任何实体源/目标文件）。"""
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        if body.execute:
            return await mp.clear_transfer_history_for_episodes(
                title=body.title,
                season=body.season,
                episodes=set(body.episodes) if body.episodes else None,
            )
        else:
            records = await mp.find_transfer_history(
                title=body.title,
                season=body.season,
                episodes=set(body.episodes) if body.episodes else None,
            )
            return {
                "matched_count": len(records),
                "records": [
                    {
                        "id": r.get("id"),
                        "title": r.get("title"),
                        "seasons": r.get("seasons"),
                        "episodes": r.get("episodes"),
                        "dest": r.get("dest"),
                    }
                    for r in records
                ],
            }
    finally:
        await mp.close()


# ---------- 活跃下载 ----------

@router.get("/downloads")
async def list_active_downloads() -> list[dict]:
    """获取当前活跃下载任务。"""
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        downloads = await mp.get_active_downloads()
        if isinstance(downloads, list):
            return downloads
        return []
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取下载列表失败: {e}")
    finally:
        await mp.close()



# ---------- 智能补缺下载 ----------

class _GapFillRequest(BaseModel):
    season_record_ids: list[str] = []
    record_ids: list[str] = []
    batch_run_id: str | None = None
    selected_episodes: list[int] | None = None
    episodes_by_record: dict[str, list[int]] | None = None
    confirmed_categories: dict[str, str] | None = None

    def get_target_ids(self) -> list[str]:
        return self.season_record_ids or self.record_ids


@router.post("/download-gap-fill")
async def download_gap_fill(
    body: _GapFillRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """针对选中的季度记录，自动从 MoviePilot 搜索做种≥2的最佳资源并推送下载。"""
    _check_mp_configured()
    target_ids = body.get_target_ids()
    if not target_ids:
        return {"ok": False, "message": "未选择任何需要补缺的剧集季度"}

    from app.services.download_service import execute_gap_fill_download
    try:
        return await execute_gap_fill_download(
            target_ids,
            db,
            batch_run_id=body.batch_run_id,
            selected_episodes=body.selected_episodes,
            episodes_by_record=body.episodes_by_record,
            confirmed_categories=body.confirmed_categories,
        )
    except Exception as e:
        await db.rollback()
        import logging
        logging.getLogger(__name__).exception("补缺请求失败；已回滚本次数据库事务")
        busy = "database is locked" in str(e).lower() or "PendingRollback" in type(e).__name__
        message = ("数据库繁忙，本次推送未完整完成；已受理项须先核对下载台账，请勿直接重复推送" if busy
                   else "补缺推送未完整完成，请查看服务日志并核对已受理项")
        return {"ok": False, "message": message, "error_code": "database_busy" if busy else "gap_fill_failed", "errors": [message]}


class _GapFillAbortRequest(BaseModel):
    task_id: str
    season_record_ids: list[str] = []
    batch_run_id: str = ""


@router.post("/gap-fill/mark-aborted")
async def mark_gap_fill_aborted(
    body: _GapFillAbortRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """记录用户在批处理中途点击“中止后续任务”时尚未启动的剧季，确保“本次未执行”独立审计且绝不污染已在途集。"""
    if not body.season_record_ids:
        return {"ok": True, "marked_count": 0, "message": "无尚未开始的剩余剧季"}

    from app.models.season import SeasonRecord
    from app.services.gap_fill_tracker import (
        build_identity_key,
        compute_season_gap_fill_view,
        record_season_run_snapshot,
        record_unsubmitted_episode_outcomes,
        refresh_task_summary,
    )

    recs = list(
        (
            await db.execute(
                select(SeasonRecord).where(
                    SeasonRecord.task_id == body.task_id,
                    SeasonRecord.id.in_(body.season_record_ids),
                )
            )
        )
        .scalars()
        .all()
    )

    marked = 0
    for rec in recs:
        if rec.status not in ("download_needed", "period_missing"):
            continue
        mapping = json.loads(rec.mapping_json or "{}") if getattr(rec, "mapping_json", None) else None
        if isinstance(mapping, dict) and not mapping.get("target_tmdb_id"):
            mapping = None
        id_key = build_identity_key(rec.show_name, rec.tmdb_id, rec.season_number, mapping)
        view = await compute_season_gap_fill_view(db, rec)
        actionable = view.get("actionable_missing_episodes") or []
        if not actionable:
            continue
        await record_unsubmitted_episode_outcomes(
            db,
            rec,
            id_key,
            actionable,
            batch_run_id=body.batch_run_id or "",
            outcome_state="aborted_unstarted",
            outcome_code="aborted_unstarted",
            reason="用户主动中止后续批量任务，本季尚未启动（本次未执行）",
            search_tmdb_id=int(mapping["target_tmdb_id"]) if mapping and mapping.get("target_tmdb_id") else (int(rec.tmdb_id) if rec.tmdb_id and str(rec.tmdb_id).isdigit() else None),
            search_season_number=int(mapping.get("target_season") or 1) if mapping and mapping.get("target_tmdb_id") else rec.season_number,
            numbering_mode=str(mapping.get("numbering_mode") or "standard") if mapping else "standard",
        )
        await record_season_run_snapshot(
            db,
            rec,
            id_key,
            batch_run_id=body.batch_run_id or "",
            outcome_status="aborted_unstarted",
            summary_text="用户中止后续任务，本季本次未执行",
            target_episodes=actionable,
            accepted_episodes=view.get("pushed_episodes") or [],
            unverified_episodes=view.get("unverified_episodes") or [],
            failed_episodes={str(ep): {"code": "aborted_unstarted", "reason": "本次未执行（用户中止）"} for ep in actionable},
        )
        marked += 1

    await refresh_task_summary(body.task_id, db)
    await db.commit()
    return {"ok": True, "marked_count": marked, "message": f"已记录 {marked} 季尚未启动（本次未执行）"}


# ── 补缺站点管理与动漫/非动漫独立规则 (v2.5.40 规范) ───────────────

class _GapFillSiteRuleSaveRequest(BaseModel):
    category: str
    selected_site_ids: list[int | str] = []
    status: str = "active"
    expected_version: int | None = None


class _GapFillWorkPreferenceRequest(BaseModel):
    tmdb_id: str | None = None
    show_name: str = ""
    category: str


@router.get("/gap-fill/sites/catalog")
async def get_gap_fill_sites_catalog(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """读取 MoviePilot 站点目录（严格白名单安全脱敏，禁透传 Cookie/Passkey/Token）。"""
    _check_mp_configured()
    client = MoviePilotClient()
    try:
        sites = await client.get_sites(sanitize=True)
        from app.services.download_service import get_mp_instance_key
        return {
            "ok": True,
            "sites": sites,
            "instance_key": get_mp_instance_key(client),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取 MoviePilot 站点目录失败: {exc}")
    finally:
        await client.close()


@router.get("/gap-fill/sites/rules")
async def get_gap_fill_site_rules_endpoint(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """读取动漫与非动漫两套独立补缺站点规则（若未配置则返回初始草稿）。"""
    _check_mp_configured()
    client = MoviePilotClient()
    try:
        from app.services.download_service import get_gap_fill_site_rules
        rules = await get_gap_fill_site_rules(db, mp=client)
        return {"ok": True, **rules}
    finally:
        await client.close()


@router.post("/gap-fill/sites/rules")
async def save_gap_fill_site_rule_endpoint(
    body: _GapFillSiteRuleSaveRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """保存某类别的补缺站点规则（带乐观锁版本校验）。"""
    _check_mp_configured()
    client = MoviePilotClient()
    try:
        from app.services.download_service import save_gap_fill_site_rule
        saved = await save_gap_fill_site_rule(
            db,
            category=body.category,
            selected_site_ids=body.selected_site_ids,
            status=body.status,
            expected_version=body.expected_version,
            mp=client,
        )
        return {"ok": True, "rule": saved}
    except RuntimeError as r_err:
        raise HTTPException(status_code=409, detail=str(r_err))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"保存补缺站点规则失败: {exc}")
    finally:
        await client.close()


@router.get("/gap-fill/work-preferences")
async def get_work_preferences_endpoint(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """获取所有作品的人工补缺分类偏好设置。"""
    client = MoviePilotClient()
    try:
        from app.services.download_service import get_gap_fill_work_preferences
        prefs = await get_gap_fill_work_preferences(db, mp=client)
        return {"ok": True, "preferences": prefs}
    finally:
        await client.close()


@router.post("/gap-fill/work-preference")
async def save_work_preference_endpoint(
    body: _GapFillWorkPreferenceRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """保存用户对某作品的人工补缺分类偏好（跨初检复用）。"""
    client = MoviePilotClient()
    try:
        from app.services.download_service import save_gap_fill_work_preference
        pref = await save_gap_fill_work_preference(
            db,
            tmdb_id=body.tmdb_id,
            show_name=body.show_name,
            category=body.category,
            mp=client,
        )
        return {"ok": True, "preference": pref}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"保存作品分类偏好失败: {exc}")
    finally:
        await client.close()

