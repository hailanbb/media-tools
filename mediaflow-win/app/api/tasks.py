"""初检任务 CRUD 端点."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.task import Task
from app.models.operation import OperationLog
from app.models.season import SeasonRecord
from app.schemas.common import StatusResponse
from app.schemas.task import TaskCreate, TaskOut, TaskSummary

router = APIRouter()


def _task_to_out(task: Task) -> TaskOut:
    """将 ORM Task 转为 API 输出，解析 summary_json。"""
    summary = None
    if task.summary_json:
        try:
            d = json.loads(task.summary_json)
            summary = TaskSummary(**d)
        except Exception:
            pass
    return TaskOut(
        id=task.id,
        name=task.name,
        source_path=task.source_path,
        media_type=task.media_type,
        status=task.status,
        created_at=task.created_at,
        updated_at=task.updated_at,
        summary=summary,
    )


@router.get("/", response_model=list[TaskOut])
async def list_tasks(db: AsyncSession = Depends(get_db)) -> list[TaskOut]:
    """列出全部初检任务."""
    result = await db.execute(
        select(Task).order_by(Task.created_at.desc())
    )
    return [_task_to_out(t) for t in result.scalars().all()]


@router.post("/", response_model=TaskOut, status_code=201)
async def create_task(
    body: TaskCreate, db: AsyncSession = Depends(get_db)
) -> TaskOut:
    """创建新的初检任务."""
    name = body.name or PurePosixPath(body.source_path.rstrip("/")).name
    task = Task(
        id=uuid.uuid4().hex,
        name=name,
        source_path=body.source_path,
        media_type=body.media_type,
        status="pending",
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return _task_to_out(task)


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(task_id: str, db: AsyncSession = Depends(get_db)) -> TaskOut:
    """获取单个任务."""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _task_to_out(task)


@router.delete("/{task_id}", response_model=StatusResponse)
async def delete_task(
    task_id: str, db: AsyncSession = Depends(get_db)
) -> StatusResponse:
    """删除任务及其所有关联剧季记录."""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    await db.delete(task)
    await db.commit()
    return StatusResponse(ok=True, message="任务已删除")


@router.post("/{task_id}/reinspect", response_model=TaskOut)
async def reinspect_task(
    task_id: str, db: AsyncSession = Depends(get_db)
) -> TaskOut:
    """将任务标记为待重新初检."""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    task.status = "pending"  # type: ignore[assignment]
    task.summary_json = None  # type: ignore[assignment]
    await db.commit()
    await db.refresh(task)
    return _task_to_out(task)



@router.get("/{task_id}/seasons")
async def get_task_seasons(
    task_id: str,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """获取任务关联的剧季记录，可按状态过滤。

    Args:
        task_id: 任务 ID
        status: 可选 — archive_ready / review_needed / download_needed / airing / unknown
    """
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    stmt = select(SeasonRecord).where(SeasonRecord.task_id == task_id, SeasonRecord.status != "manually_handled")
    if status:
        stmt = stmt.where(SeasonRecord.status == status)
    stmt = stmt.order_by(SeasonRecord.show_name, SeasonRecord.season_number)
    result = await db.execute(stmt)
    records = result.scalars().all()

    return [
        {
            "id": r.id,
            "show_name": r.show_name,
            "show_path": r.show_path,
            "tmdb_id": r.tmdb_id,
            "season_number": r.season_number,
            "season_path": r.season_path,
            "total_episodes": r.total_episodes,
            "local_episode_count": r.local_episode_count,
            "recognized_episodes": json.loads(r.recognized_episodes_json or "[]"),
            "missing_episodes": json.loads(r.missing_episodes_json or "[]"),
            "duplicate_episodes": json.loads(r.duplicate_episodes_json or "[]"),
            "status": r.status,
            "status_detail": r.status_detail,
            "archive_target_path": r.archive_target_path,
            "season_finished": r.season_finished,
        }
        for r in records
    ]


@router.post("/{task_id}/seasons/{record_id}/handled", response_model=StatusResponse)
async def mark_season_handled(task_id: str, record_id: str, db: AsyncSession = Depends(get_db)) -> StatusResponse:
    """用户确认手动处置完成后，从当前任务所有清单中隐藏该季。"""
    task = await db.get(Task, task_id)
    rec = await db.get(SeasonRecord, record_id)
    if not task or not rec or rec.task_id != task_id:
        raise HTTPException(status_code=404, detail="剧季记录不存在")
    if rec.status == "manually_handled":
        return StatusResponse(ok=True, message="该条目已标记为手动处理")
    from app.services.task_service import TASK_RUNNING_WORKERS
    if task.status in ("running", "paused") or task_id in TASK_RUNNING_WORKERS:
        raise HTTPException(status_code=409, detail="请在初检结束后标记手动处理")
    recognized = json.loads(rec.recognized_episodes_json or "[]")
    is_anomaly = (
        rec.status in ("already_archived", "unknown")
        or not rec.total_episodes
        or (rec.status != "archive_ready" and max(recognized, default=0) > rec.total_episodes)
    )
    if rec.status == "archive_check" or not is_anomaly:
        raise HTTPException(status_code=409, detail="仅异常剧集可标记为手动处理")
    previous = rec.status
    changed = await db.execute(update(SeasonRecord).where(
        SeasonRecord.id == record_id, SeasonRecord.status != "manually_handled"
    ).values(status="manually_handled"))
    if changed.rowcount:
        db.add(OperationLog(
            task_id=task_id, season_record_id=record_id, action="manually_handled", status="completed",
            detail_json=json.dumps({"season_path": rec.season_path, "show_name": rec.show_name,
                                    "season_number": rec.season_number, "previous_status": previous}, ensure_ascii=False),
        ))
    await db.refresh(rec)
    from app.api.archive import _refresh_task_summary
    await _refresh_task_summary(task_id, db)
    await db.commit()
    return StatusResponse(ok=True, message="已标记为手动处理，并从任务清单中移除")
