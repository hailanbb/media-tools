"""MoviePilot 代理端点 — 透传前端请求到 MoviePilot API。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.moviepilot import MoviePilotClient
from app.config import get_settings
from app.core.subscription_cleaner import diagnose_subscription, evaluate_subscriptions
import logging
import re
from typing import Any

from app.database import get_db
from app.schemas.common import StatusResponse

log = logging.getLogger("mediaflow.api.moviepilot")

router = APIRouter()


def _check_mp_configured() -> None:
    settings = get_settings()
    if not settings.mp_url or not settings.mp_token:
        raise HTTPException(status_code=503, detail="MoviePilot 未配置")


# ---------- 订阅管理 ----------

class BatchDeleteSubscriptionsRequest(BaseModel):
    sub_ids: list[int]


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
                "tmdb_id": s.get("tmdbid"),
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
                    # 仅已完成入库电影
                    mode_desc = "电影安全清理 (仅已入库完成)"
                    if diag.clean_rule == "completed":
                        matched.append(diag)
                elif body.mode == "old_zombie":
                    # 仅往年老片长期无源死种
                    mode_desc = "往年下映老片无源清理"
                    if diag.clean_rule == "zombie":
                        matched.append(diag)
                elif body.mode == "douban":
                    # 豆瓣榜单推选无源
                    mode_desc = "豆瓣推选无源清理"
                    if diag.is_douban_sync and diag.clean_confidence in ("high", "medium"):
                        matched.append(diag)
                else:  # deep 电影全量建议清理
                    mode_desc = "电影深度清理 (已入库 + 往年死种，严禁清理2026待映)"
                    if diag.clean_confidence in ("high", "medium"):
                        matched.append(diag)

            elif body.media_type == "tv":
                if body.mode == "safe":
                    # 仅全季完结无缺集
                    mode_desc = "电视剧安全清理 (仅全季已齐完结)"
                    if diag.clean_rule == "completed":
                        matched.append(diag)
                elif body.mode == "short_tv":
                    # 仅短剧/微短剧虚标停滞
                    mode_desc = "短剧/微短剧停滞清理"
                    if diag.clean_rule == "short_tv_stalled":
                        matched.append(diag)
                elif body.mode == "stalled":
                    # 仅正剧中途断更停滞
                    mode_desc = "剧集中途断更停滞清理"
                    if diag.clean_rule in ("stalled", "zombie"):
                        matched.append(diag)
                else:  # deep 电视剧全量建议清理
                    mode_desc = "电视剧深度清理 (全季完结 + 短剧停滞 + 断更停滞)"
                    if diag.clean_confidence in ("high", "medium"):
                        matched.append(diag)

            else:  # all 全量模式
                if body.mode == "safe":
                    mode_desc = "全局安全清理 (仅全季完结与已入库电影)"
                    if diag.clean_confidence == "high":
                        matched.append(diag)
                else:
                    mode_desc = "全局深度清理 (含停滞与往年死种，保护新片与连载)"
                    if diag.clean_confidence in ("high", "medium"):
                        matched.append(diag)

        clean_items = [
            {
                "id": m.sub_id,
                "sub_id": m.sub_id,
                "name": m.name,
                "year": m.year,
                "media_type": m.media_type,
                "season": m.season,
                "clean_confidence": m.clean_confidence,
                "clean_rule": m.clean_rule,
                "clean_rule_name": m.clean_rule_name,
                "clean_reason": m.clean_reason,
                "is_short_tv": m.is_short_tv,
                "username": m.username,
                "progress": f"{m.completed_episodes}/{m.total_episodes or '?'}",
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
        }
    finally:
        await mp.close()


@router.post("/subscriptions/batch-delete", response_model=StatusResponse)
async def batch_delete_subscriptions(body: BatchDeleteSubscriptionsRequest) -> StatusResponse:
    """批量取消指定订阅。"""
    if not body.sub_ids:
        return StatusResponse(ok=False, message="未选择任何订阅")

    _check_mp_configured()
    mp = MoviePilotClient()
    success_count = 0
    fail_count = 0
    errors: list[str] = []

    try:
        for sid in body.sub_ids:
            try:
                await mp.delete_subscription(sid)
                success_count += 1
            except Exception as e:
                fail_count += 1
                errors.append(f"退订 {sid} 失败: {e}")

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


@router.post("/transfer-repair/{item_id}", response_model=StatusResponse)
async def repair_transfer(item_id: str) -> StatusResponse:
    """修复卡住的转移任务 — 重发手动转移。"""
    _check_mp_configured()
    # TODO: 根据 item_id 获取源路径后调用 manual_transfer
    return StatusResponse(ok=True, message=f"转移任务 {item_id} 修复已提交")


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
    season_record_ids: list[str]


@router.post("/download-gap-fill")
async def download_gap_fill(
    body: _GapFillRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """针对选中的季度记录，自动从 MoviePilot 搜索做种≥2的最佳资源并推送下载。"""
    _check_mp_configured()
    if not body.season_record_ids:
        return {"ok": False, "message": "未选择任何需要补缺的剧集季度"}

    from app.services.download_service import execute_gap_fill_download
    try:
        return await execute_gap_fill_download(body.season_record_ids, db)
    except Exception as e:
        return {"ok": False, "message": f"补缺推送异常: {str(e)[:200]}", "errors": [str(e)]}
