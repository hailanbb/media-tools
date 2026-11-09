"""任务服务 — 编排初检任务的生命周期。

职责：
- 创建/查询/删除任务
- 驱动初检引擎
- 将初检结果写入 SeasonRecord
- 更新任务状态和汇总统计
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task
from app.models.season import SeasonRecord
from app.core.inspector import InspectionResult, SeasonDiagnosis, run_inspection

import asyncio

TASK_PROGRESS: dict[str, dict] = {}
TASK_EVENTS: dict[str, asyncio.Event] = {}
TASK_CANCELLED: set[str] = set()


def get_task_control_event(task_id: str) -> asyncio.Event:
    if task_id not in TASK_EVENTS:
        evt = asyncio.Event()
        evt.set()
        TASK_EVENTS[task_id] = evt
    return TASK_EVENTS[task_id]


async def pause_task(task_id: str) -> bool:
    evt = get_task_control_event(task_id)
    evt.clear()
    if task_id in TASK_PROGRESS:
        TASK_PROGRESS[task_id]["status"] = "paused"
        TASK_PROGRESS[task_id]["message"] = "初检任务已暂停，点击继续恢复..."
    return True


async def resume_task(task_id: str) -> bool:
    evt = get_task_control_event(task_id)
    evt.set()
    if task_id in TASK_PROGRESS:
        TASK_PROGRESS[task_id]["status"] = "running"
        TASK_PROGRESS[task_id]["message"] = "初检任务已恢复，正在并行加速分析..."
    return True


async def stop_task(task_id: str) -> bool:
    TASK_CANCELLED.add(task_id)
    evt = get_task_control_event(task_id)
    evt.set()
    if task_id in TASK_PROGRESS:
        TASK_PROGRESS[task_id]["status"] = "stopped"
        TASK_PROGRESS[task_id]["message"] = "初检任务已被手动停止，已保存目前进度"
    return True


def get_task_progress(task_id: str) -> dict:
    """获取指定任务的实时初检进度。"""
    if task_id in TASK_PROGRESS:
        return TASK_PROGRESS[task_id]
    return {
        "task_id": task_id,
        "status": "idle",
        "current": 0,
        "total": 0,
        "current_show": "",
        "percent": 0,
        "message": "就绪",
        "error": None,
    }


async def start_background_inspection(task_id: str) -> None:
    """后台独立会话执行初检。"""
    from app.database import async_session
    async with async_session() as db:
        try:
            await execute_inspection(task_id, db)
        except Exception as e:
            import logging
            logging.getLogger("mediaflow.task_service").error("后台初检异常: %s", e)


async def execute_inspection(task_id: str, db: AsyncSession) -> InspectionResult:
    """执行指定任务的初检流程。

    流程：
    1. 从数据库获取任务
    2. 将任务状态设为 running
    3. 调用初检引擎
    4. 将诊断结果写入 SeasonRecord
    5. 更新任务汇总和状态

    Args:
        task_id: 任务 ID
        db: 数据库会话

    Returns:
        InspectionResult
    """
    # 1. 获取任务
    task = await db.get(Task, task_id)
    if not task:
        raise ValueError(f"任务 {task_id} 不存在")

    # 2. 标记为执行中
    task.status = "running"  # type: ignore[assignment]
    await db.commit()

    TASK_PROGRESS[task_id] = {
        "task_id": task_id,
        "status": "running",
        "current": 0,
        "total": 0,
        "current_show": "",
        "percent": 0,
        "message": "正在初始化初检...",
        "error": None,
    }

    async def _on_progress(current: int, total: int, show_name: str, message: str) -> None:
        pct = int((current / total) * 100) if total > 0 else 0
        if task_id in TASK_CANCELLED:
            cur_status = "stopped"
        elif control_event and not control_event.is_set():
            cur_status = "paused"
        else:
            cur_status = "running"

        TASK_PROGRESS[task_id] = {
            "task_id": task_id,
            "status": cur_status,
            "current": current,
            "total": total,
            "current_show": show_name,
            "percent": pct,
            "message": message,
            "error": None,
        }

    try:
        # 3. 调用初检引擎 — 注入真实客户端
        from app.clients.clouddrive2 import CloudDrive2Client
        from app.clients.moviepilot import MoviePilotClient
        from app.clients.tmdb import TMDBClient
        from app.config import get_settings

        settings = get_settings()
        cd2 = CloudDrive2Client()
        mp = MoviePilotClient()
        tmdb = TMDBClient()
        TASK_CANCELLED.discard(task_id)
        control_event = get_task_control_event(task_id)
        control_event.set()

        try:
            result = await run_inspection(
                task_id=task_id,
                source_path=task.source_path,
                cd2_client=cd2,
                mp_client=mp,
                tmdb_client=tmdb,
                archive_root=settings.archive_root,
                global_source_root=settings.source_root,
                progress_callback=_on_progress,
                control_event=control_event,
                cancelled_tasks=TASK_CANCELLED,
                concurrency=3,
            )
        finally:
            await cd2.close()
            await mp.close()
            await tmdb.close()

        # 4. 清除旧记录，写入新结果
        old_records = await db.execute(
            select(SeasonRecord).where(SeasonRecord.task_id == task_id)
        )
        for old in old_records.scalars().all():
            await db.delete(old)

        for diag in result.seasons:
            record = SeasonRecord(
                task_id=task_id,
                show_name=diag.show_name,
                show_path=diag.show_path,
                tmdb_id=diag.tmdb_id,
                season_number=diag.season_number,
                season_path=diag.season_path,
                total_episodes=diag.total_episodes,
                local_episode_count=diag.local_episode_count,
                recognized_episodes_json=json.dumps(diag.recognized_episodes),
                missing_episodes_json=json.dumps(diag.missing_episodes),
                duplicate_episodes_json=json.dumps(diag.duplicate_episodes),
                status=diag.status,
                status_detail=diag.status_detail,
                archive_target_path=diag.archive_target_path,
                season_finished=diag.season_finished,
            )
            db.add(record)

        # 5. 更新任务汇总
        is_stopped = task_id in TASK_CANCELLED
        final_status = "stopped" if is_stopped else "completed"
        
        task.status = final_status  # type: ignore[assignment]
        task.summary_json = json.dumps({  # type: ignore[assignment]
            "archive_ready": result.archive_ready_count,
            "review_needed": result.review_needed_count,
            "download_needed": result.download_needed_count,
            "airing": result.airing_count,
            "already_archived": result.already_archived_count,
            "total_seasons": len(result.seasons),
        })
        await db.commit()

        TASK_PROGRESS[task_id] = {
            "task_id": task_id,
            "status": final_status,
            "current": len(result.seasons),
            "total": len(result.seasons),
            "current_show": "",
            "percent": 100 if not is_stopped else TASK_PROGRESS.get(task_id, {}).get("percent", 50),
            "message": f"初检已停止：已保存已分析的 {len(result.seasons)} 季" if is_stopped else f"初检完成：已分析 {len(result.seasons)} 季",
            "error": None,
        }

        TASK_EVENTS.pop(task_id, None)
        TASK_CANCELLED.discard(task_id)

        return result

    except Exception as exc:
        task.status = "failed"  # type: ignore[assignment]
        await db.commit()
        TASK_PROGRESS[task_id] = {
            "task_id": task_id,
            "status": "failed",
            "current": 0,
            "total": 0,
            "current_show": "",
            "percent": 0,
            "message": f"初检失败: {str(exc)}",
            "error": str(exc),
        }
        raise


async def get_task_seasons(
    task_id: str, status: str | None, db: AsyncSession
) -> list[SeasonRecord]:
    """获取任务关联的剧季记录，可按状态过滤。

    Args:
        task_id: 任务 ID
        status: 可选状态过滤（archive_ready / review_needed / download_needed / airing）
        db: 数据库会话

    Returns:
        SeasonRecord 列表
    """
    stmt = select(SeasonRecord).where(SeasonRecord.task_id == task_id)
    if status:
        stmt = stmt.where(SeasonRecord.status == status)
    stmt = stmt.order_by(SeasonRecord.show_name, SeasonRecord.season_number)
    result = await db.execute(stmt)
    return list(result.scalars().all())
