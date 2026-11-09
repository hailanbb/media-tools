"""初检引擎触发端点 — 驱动真实初检流程、多子任务并发加速与实时生命周期控制。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.task import Task
from app.schemas.common import StatusResponse
from app.services.task_service import (
    TASK_PROGRESS,
    execute_inspection,
    get_task_progress,
    pause_task,
    resume_task,
    start_background_inspection,
    stop_task,
)

router = APIRouter()
_RUN_TASK_LOCK = asyncio.Lock()


@router.post("/{task_id}/run", response_model=StatusResponse)
async def run_inspection(task_id: str, db: AsyncSession = Depends(get_db)) -> StatusResponse:
    """提交初检任务到后台执行（多子任务并发加速，实时反馈进度）。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    from app.services.task_service import TASK_RUNNING_WORKERS
    async with _RUN_TASK_LOCK:
        existing_worker = TASK_RUNNING_WORKERS.get(task_id)
        if existing_worker and not existing_worker.done():
            if task.status == "paused":
                return StatusResponse(ok=False, message="任务已处于暂停状态，请点击【恢复】继续执行或【停止】终止")
            return StatusResponse(ok=False, message="任务正在后台执行中，请勿重复拉起")

        prog = get_task_progress(task_id)
        if task.status == "running" and prog.get("status") == "running":
            return StatusResponse(ok=False, message="任务正在执行中，请等待完成")

        # 1. 立即重置内存中的进度状态为初始 running
        TASK_PROGRESS[task_id] = {
            "task_id": task_id,
            "status": "running",
            "current": 0,
            "total": 0,
            "current_show": "",
            "percent": 1,
            "message": "正在初始化初检引擎，准备并发探测目录与剧集...",
            "error": None,
        }

        # 2. 标记数据库任务状态为 running
        task.status = "running"
        await db.commit()

        # 3. 异步启动后台分析并登记 worker (P1-7 原子认领)
        TASK_RUNNING_WORKERS[task_id] = asyncio.create_task(start_background_inspection(task_id))
    return StatusResponse(
        ok=True,
        message="初检任务已在后台启动，多协程并发加速分析中..."
    )


@router.post("/{task_id}/pause", response_model=StatusResponse)
async def pause_inspection(task_id: str, db: AsyncSession = Depends(get_db)) -> StatusResponse:
    """暂停正在进行的初检任务。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    await pause_task(task_id)
    task.status = "paused"
    await db.commit()

    return StatusResponse(ok=True, message="初检任务已暂停")


@router.post("/{task_id}/resume", response_model=StatusResponse)
async def resume_inspection(task_id: str, db: AsyncSession = Depends(get_db)) -> StatusResponse:
    """恢复/继续暂停的初检任务。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    await resume_task(task_id)
    task.status = "running"
    await db.commit()

    return StatusResponse(ok=True, message="初检任务已恢复执行")


@router.post("/{task_id}/stop", response_model=StatusResponse)
async def stop_inspection(task_id: str, db: AsyncSession = Depends(get_db)) -> StatusResponse:
    """停止/终止初检任务，保留已分析好的数据。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    await stop_task(task_id)
    task.status = "stopped"
    await db.commit()

    return StatusResponse(ok=True, message="初检任务已停止")


@router.get("/{task_id}/progress")
async def get_inspection_progress(task_id: str) -> dict:
    """获取指定任务的实时初检进度状态。"""
    return get_task_progress(task_id)
