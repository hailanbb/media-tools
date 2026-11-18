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
    from app.services.subscription_service import subscription, identity
    from app.models.season import SeasonRecord
    from sqlalchemy import select
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        sub = await subscription(mp, sub_id)
        if not sub:
            raise HTTPException(404, "订阅不存在")
        tid, season = identity(sub)
        rows = await db.scalars(select(SeasonRecord).where(SeasonRecord.tmdb_id == tid,
            SeasonRecord.season_number == season, SeasonRecord.status != "manually_handled"))
        unique = {}
        for row in rows:
            unique[row.season_path] = {"id": row.id, "path": row.season_path}
        return list(unique.values())
    finally:
        await mp.close()


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
    mode: str = "safe"       # tv: "safe" | "short_tv" | "stalled" | "deep"
                             # movie: "safe" | "old_zombie" | "douban" | "deep"


@router.get("/subscriptions")
async def list_subscriptions(media_type: str | None = None) -> dict:
    """获取 MP 订阅列表并附加规则引擎智能诊断（支持电影/电视剧分离）。"""
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        subs = await mp.list_subscriptions()
        eval_result = evaluate_subscriptions(subs)
        
        # 组装标准化输出
        items_out = []
        for s, diag in zip(subs, eval_result["items"]):
            if media_type and diag.media_type != media_type:
                continue

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
                "tmdb_id": s.get("tmdbid") or s.get("tmdb_id"),
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
                "is_douban_sync": diag.is_douban_sync,
                "release_stage": diag.release_stage,
                "in_library_status": diag.in_library_status,
                "version_target_satisfied": diag.version_target_satisfied,
            })

        # 统计汇总
        overall = eval_result["overall"]
        tv_stats = eval_result["tv"]
        movie_stats = eval_result["movie"]

        return {
            "overall": overall,
            "tv": tv_stats,
            "movie": movie_stats,
            # 兼容字段
            "total": overall["total"],
            "high_confidence_count": overall["high"],
            "medium_confidence_count": overall["medium"],
            "protected_count": overall["protected"],
            "clean_eligible_total": overall["clean_eligible"],
            "subscriptions": items_out,
        }
    finally:
        await mp.close()


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
    success_count = 0
    fail_count = 0
    errors: list[str] = []

    try:
        for sid in body.sub_ids:
            try:
                await mp.update_subscription_status(sid, body.state)
                success_count += 1
            except Exception as e:
                fail_count += 1
                errors.append(f"订阅 #{sid}: {e}")

        state_name = "暂停观察 (S)" if body.state == "S" else ("活跃追更 (R)" if body.state == "R" else f"待定 ({body.state})")
        if success_count > 0:
            op_log = OperationLog(
                action="batch_update_subscription_status",
                status="completed",
                detail_json=json.dumps({"sub_ids": body.sub_ids, "state": body.state, "success_count": success_count}, ensure_ascii=False),
            )
            db.add(op_log)
            await db.commit()

        msg = f"批量变更状态完成：成功 {success_count} 项设置为 {state_name}"
        if fail_count > 0:
            msg += f"，失败 {fail_count} 项"
        return StatusResponse(ok=success_count > 0, message=msg)
    finally:
        await mp.close()


@router.get("/subscriptions/{sub_id}/mediaserver-check")
async def check_subscription_mediaserver(sub_id: int) -> dict:
    """实时核查单个订阅在媒体服务器中的入库实情（区分电视剧季集映射与电影入库）。"""
    _check_mp_configured()
    from app.services.subscription_service import subscription, is_movie_sub
    mp = MoviePilotClient()
    try:
        sub = await subscription(mp, sub_id)
        if not sub:
            raise HTTPException(404, "订阅不存在")

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
                "media_type": "tv",
                "season": season,
                "in_library_episodes": season_eps,
                "count": len(season_eps),
                "message": f"S{str(season).padStart if False else str(season).zfill(2)} 已入库 {len(season_eps)} 集" if season_eps else "媒体服务器中未见该季入库集数",
            }
    finally:
        await mp.close()


@router.post("/subscriptions/clean-preview")
async def preview_subscription_cleanup(body: SubscriptionCleanPreviewRequest) -> dict:
    """生成订阅清理审计预案，按媒体类型（电视剧 vs 电影）与所选模式精准筛选。"""
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        subs = await mp.list_subscriptions()
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
                    # 仅已完成入库电影 (场景 F01, F02, F10)
                    mode_desc = "电影安全清理 (仅已核实入库完成)"
                    if diag.clean_rule == "completed":
                        matched.append(diag)
                elif body.mode == "old_zombie":
                    # 仅往年老片长期无源死种 (场景 F03, F14)
                    mode_desc = "往年下映老片无源清理"
                    if diag.clean_rule == "zombie":
                        matched.append(diag)
                elif body.mode == "douban":
                    # 豆瓣榜单推选无源
                    mode_desc = "豆瓣推选长期无源清理"
                    if diag.is_douban_sync and diag.clean_rule in ("douban_stale", "zombie", "completed"):
                        matched.append(diag)
                else:  # deep 电影全量建议清理 (严格排除待映保护与手动收藏意图)
                    mode_desc = "电影深度清理 (已入库 + 往年死种，锁定待映与收藏)"
                    if diag.clean_rule in ("completed", "zombie", "douban_stale") and not diag.is_protected:
                        matched.append(diag)

            elif body.media_type == "tv":
                if body.mode == "safe":
                    # 仅全季完结无缺集 (场景 T01, T02)
                    mode_desc = "电视剧安全清理 (仅全季已齐完结)"
                    if diag.clean_rule == "completed":
                        matched.append(diag)
                elif body.mode == "short_tv":
                    # 仅短剧/微短剧虚标停滞 (场景 T06)
                    mode_desc = "短剧/微短剧停滞清理"
                    if diag.clean_rule == "short_tv_stalled":
                        matched.append(diag)
                elif body.mode == "stalled":
                    # 仅正剧中途断更停滞
                    mode_desc = "剧集中途断更停滞清理"
                    if diag.clean_rule in ("stalled", "zombie"):
                        matched.append(diag)
                else:  # deep 电视剧全量建议清理 (排除连载中和洗版保护)
                    mode_desc = "电视剧深度清理 (全季完结 + 短剧停滞 + 断更停滞)"
                    if diag.clean_confidence in ("high", "medium") and not diag.is_protected:
                        matched.append(diag)

            else:  # all 全量模式
                if body.mode == "safe":
                    mode_desc = "全局安全清理 (仅全季完结与已入库电影)"
                    if diag.clean_confidence == "high":
                        matched.append(diag)
                else:
                    mode_desc = "全局深度清理 (含停滞与往年死种，保护新片与连载)"
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
                "username": m.username,
                "release_stage": m.release_stage,
                "in_library_status": m.in_library_status,
                "total_episodes": m.total_episodes,
                "completed_episodes": m.completed_episodes,
                "lack_episodes": m.lack_episodes,
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
    mp = MoviePilotClient()
    success_count = 0
    fail_count = 0
    errors: list[str] = []

    try:
        # 获取当前订阅列表建立 id -> name 映射
        sub_name_map: dict[int, str] = {}
        try:
            current_subs = await mp.list_subscriptions()
            for cs in current_subs:
                cs_id = cs.get("id")
                if cs_id is not None:
                    sub_name_map[int(cs_id)] = cs.get("name") or cs.get("title") or ""
        except Exception as map_err:
            log.warning("获取订阅列表映射失败: %s", map_err)

        for idx, sid in enumerate(body.sub_ids):
            title = (
                sub_name_map.get(sid)
                or (body.titles[idx] if body.titles and idx < len(body.titles) else f"订阅#{sid}")
            )
            mp_deleted = False
            try:
                from app.services.subscription_service import subscription
                current_sub = await subscription(mp, sid)
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
            except Exception as e:
                if mp_deleted:
                    # MP 已成功退订，但本地数据库审计写入异常，避免用户误判未完成而重复提交
                    success_count += 1
                    errors.append(f"退订 {title} 在 MP 已生效，但本地审计日志写入失败: {e}")
                else:
                    fail_count += 1
                    errors.append(f"退订 {title} 失败: {e}")

        if success_count > 0:
            await db.commit()

        msg = f"批量清理完成：成功退订 {success_count} 项"
        if fail_count > 0:
            msg += f"，失败 {fail_count} 项"

        return StatusResponse(ok=success_count > 0, message=msg)
    finally:
        await mp.close()


@router.delete("/subscriptions/{sub_id}", response_model=StatusResponse)
async def delete_subscription(sub_id: int) -> StatusResponse:
    """取消指定订阅。"""
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        await mp.delete_subscription(sub_id)
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


class TransferRepairExecuteRequest(BaseModel):
    item_ids: list[str]


class TransferRepairPlanUpdateRequest(BaseModel):
    title: str | None = None
    media_type: str | None = None
    season_number: int | None = None
    episode_numbers: list[int] | None = None
    part: str | None = None
    is_special: bool | None = None
    special_type: str | None = None
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
    """启动 MoviePilot 整理失败扫描并执行智能诊断 (R01, R02, R29)。"""
    from app.services.transfer_repair_service import scan_new_failures
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        max_p = req.max_pages if req else 5
        p_sz = req.page_size if req else 50
        return await scan_new_failures(db, mp, max_pages=max_p, page_size=p_sz)
    finally:
        await mp.close()


@router.get("/transfer-repair/summary")
async def get_transfer_repair_summary_endpoint(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """获取整理修复仪表盘统计数据 (R01, R29)。"""
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
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """获取整理修复项列表。"""
    from app.services.transfer_repair_service import get_repair_items
    return await get_repair_items(db, tab=tab, category=category, keyword=keyword, limit=limit, offset=offset)


@router.get("/transfer-repair/items/{item_id}")
async def get_transfer_repair_item_detail(
    item_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """获取单个修复项详情、证据快照与计划预览。"""
    from app.models.transfer_repair import TransferRepairItem
    item = await db.get(TransferRepairItem, item_id)
    if not item:
        raise HTTPException(404, "修复项不存在")
    return {
        "id": item.id,
        "source_path": item.source_path,
        "file_basename": item.file_basename,
        "file_size": item.file_size,
        "title": item.title,
        "media_type": item.media_type,
        "tmdb_id": item.tmdb_id,
        "season_number": item.season_number,
        "episode_numbers": json.loads(item.episode_numbers_json or "[]"),
        "part": item.part,
        "is_special": item.is_special,
        "special_type": item.special_type,
        "status": item.status,
        "error_category": item.error_category,
        "main_block_reason": item.main_block_reason,
        "raw_error": item.raw_error,
        "resolution_code": item.resolution_code,
        "skip_reason": item.skip_reason,
        "plan": json.loads(item.plan_json or "{}"),
        "plan_hash": item.plan_hash,
        "is_protected": item.is_protected,
        "retry_count": item.retry_count,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


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
    """批量提交明确计划执行整理并验证 (R03, R16)。"""
    from app.services.transfer_repair_service import execute_repair_plans
    _check_mp_configured()
    mp = MoviePilotClient()
    try:
        return await execute_repair_plans(db, mp, req.item_ids)
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
        item.status = "ready" if item.plan_hash else "diagnosing"
    item.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True, "is_protected": item.is_protected, "status": item.status}


@router.get("/transfer-repair/settings")
async def get_transfer_repair_settings(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """获取媒体整理自动化设置与基线。"""
    from app.services.transfer_repair_service import get_or_create_baseline
    baseline = await get_or_create_baseline(db)
    return {
        "instance_id": baseline.instance_id,
        "baseline_history_id": baseline.baseline_history_id,
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
    baseline = await get_or_create_baseline(db)
    if req.is_enabled is not None:
        baseline.is_enabled = req.is_enabled
    if req.auto_repair_enabled is not None:
        baseline.auto_repair_enabled = req.auto_repair_enabled
    if req.max_concurrency is not None:
        baseline.max_concurrency = req.max_concurrency
    if req.batch_quota is not None:
        baseline.batch_quota = req.batch_quota
    if req.retry_budget is not None:
        baseline.retry_budget = req.retry_budget
    if req.scan_interval_minutes is not None:
        baseline.scan_interval_minutes = req.scan_interval_minutes
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
        return {"ok": False, "message": f"补缺推送异常: {str(e)[:200]}", "errors": [str(e)]}


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
        raise HTTPException(status_code=400, detail=f"保存作品偏好失败: {exc}")

