"""初检任务 CRUD 端点."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity_resolver import (
    build_season_mapping_payload,
    get_verified_metadata_snapshot,
    resolve_active_season_mapping,
)
from app.core.inspector import diagnose_season
from app.database import get_db
from app.models.operation import OperationLog
from app.models.season import SeasonRecord
from app.models.season_mapping import SeasonMapping
from app.models.task import Task
from app.schemas.common import StatusResponse
from app.schemas.task import TaskCreate, TaskOut, TaskSummary
from app.services.gap_fill_tracker import (
    build_identity_key,
    compute_season_gap_fill_view,
    reconcile_gap_fill_records,
    refresh_task_summary,
    supersede_changed_identity_records,
)

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



def _safe_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


async def _serialize_season_record(db: AsyncSession, r: SeasonRecord) -> dict[str, Any]:
    gap_view = await compute_season_gap_fill_view(db, r)
    return {
        "id": r.id,
        "show_name": r.show_name,
        "show_path": r.show_path,
        "tmdb_id": r.tmdb_id,
        "season_number": r.season_number,
        "season_path": r.season_path,
        "total_episodes": r.total_episodes,
        "local_episode_count": r.local_episode_count,
        "recognized_episodes": _safe_json(r.recognized_episodes_json, []),
        "missing_episodes": _safe_json(r.missing_episodes_json, []),
        "duplicate_episodes": _safe_json(r.duplicate_episodes_json, []),
        "status": r.status,
        "status_detail": r.status_detail,
        "archive_target_path": r.archive_target_path,
        "season_finished": r.season_finished,
        "evidence": _safe_json(getattr(r, "evidence_json", None), {}),
        "conflict": _safe_json(getattr(r, "conflict_json", None), {}),
        "mapping": _safe_json(getattr(r, "mapping_json", None), {}),
        "gap_fill_state": gap_view,
        "last_checked_at": r.last_checked_at.isoformat() if getattr(r, "last_checked_at", None) else None,
    }


@router.get("/{task_id}/seasons")
async def get_task_seasons(
    task_id: str,
    status: str | None = None,
    gap_view: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """获取任务关联的剧季记录，支持状态与真待补/已推送待入库同页过滤。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    stmt = select(SeasonRecord).where(
        SeasonRecord.task_id == task_id,
        SeasonRecord.status.not_in(("manually_handled", "verified_complete", "cleaned_redundant", "source_absent")),
    )
    if status:
        groups = {
            "archive_ready": ("archive_ready", "period_ready"),
            "review_needed": ("review_needed", "period_review", "identity_review", "already_archived"),
            "download_needed": ("download_needed", "period_missing"),
        }
        stmt = stmt.where(SeasonRecord.status.in_(groups.get(status, (status,))))
    stmt = stmt.order_by(SeasonRecord.show_name, SeasonRecord.season_number)
    result = await db.execute(stmt)
    records = result.scalars().all()

    serialized = [await _serialize_season_record(db, r) for r in records]
    if gap_view in ("todo", "pushed_pending"):
        serialized = [
            item
            for item in serialized
            if (item.get("gap_fill_state") or {}).get("view_bucket") == gap_view
        ]
    return serialized


class RecalculateRequest(BaseModel):
    record_ids: list[str] = []
    fetch_remote: bool = False


async def _apply_in_place_recalculate(
    db: AsyncSession,
    rec: SeasonRecord,
    *,
    fetch_remote: bool = False,
) -> bool:
    """只读重算单季状态（保留 record.id、不删除源/归档端文件、不修改 manually_handled）。"""
    if rec.status in ("manually_handled", "verified_complete", "cleaned_redundant", "source_absent"):
        return False

    recognized = [int(x) for x in _safe_json(rec.recognized_episodes_json, []) if str(x).isdigit()]
    duplicates = [int(x) for x in _safe_json(rec.duplicate_episodes_json, []) if str(x).isdigit()]
    tid_int = int(rec.tmdb_id) if rec.tmdb_id and str(rec.tmdb_id).isdigit() else None
    active_map = await resolve_active_season_mapping(
        db, tid_int, rec.season_number, rec.show_path, rec.season_path
    )

    diag = None
    if fetch_remote:
        try:
            from app.clients.clouddrive2 import CloudDrive2Client
            from app.clients.moviepilot import MoviePilotClient
            from app.clients.tmdb import TMDBClient
            from app.config import get_settings
            from app.core.inspector import run_inspection

            settings = get_settings()
            cd2 = CloudDrive2Client()
            mp = MoviePilotClient()
            tmdb = TMDBClient()
            try:
                res = await run_inspection(
                    task_id=rec.task_id,
                    source_path=rec.show_path,
                    cd2_client=cd2,
                    mp_client=mp,
                    tmdb_client=tmdb,
                    archive_root=settings.archive_root,
                    global_source_root=settings.source_root,
                    only_season_path=rec.season_path,
                    db=db,
                )
                diag = next((s for s in res.seasons if s.season_path == rec.season_path), None)
            finally:
                await cd2.close()
                await mp.close()
                await tmdb.close()
        except Exception:
            diag = None

    now_dt = datetime.now(timezone.utc)
    if diag is not None:
        rec.tmdb_id = diag.tmdb_id
        rec.total_episodes = diag.total_episodes
        rec.local_episode_count = diag.local_episode_count
        rec.recognized_episodes_json = json.dumps(diag.recognized_episodes)
        rec.missing_episodes_json = json.dumps(diag.missing_episodes)
        rec.duplicate_episodes_json = json.dumps(diag.duplicate_episodes)
        rec.status = diag.status
        rec.status_detail = diag.status_detail
        rec.archive_target_path = diag.archive_target_path
        rec.season_finished = diag.season_finished
        rec.evidence_json = json.dumps(diag.evidence or {}, ensure_ascii=False)
        rec.conflict_json = json.dumps(diag.conflict or {}, ensure_ascii=False)
        rec.mapping_json = json.dumps(diag.mapping or {}, ensure_ascii=False)
        rec.last_checked_at = now_dt
        active_key = build_identity_key(rec.show_name, rec.tmdb_id, rec.season_number, diag.mapping or None)
        await supersede_changed_identity_records(db, rec.task_id, rec.season_path, active_key, rec.id)
        return True

    # 离线 / 仅依据数据库缓存与有效单季映射执行安全只读重算
    conflict = _safe_json(getattr(rec, "conflict_json", None), {})
    evidence = _safe_json(getattr(rec, "evidence_json", None), {})

    if active_map:
        target_tid = active_map.get("target_tmdb_id")
        target_sn = int(active_map.get("target_season") or 1)
        cached_m = None
        if target_tid:
            cached_m = await get_verified_metadata_snapshot(
                db, f"tmdb:{int(target_tid)}:S{target_sn}"
            ) or await get_verified_metadata_snapshot(
                db, f"tmdb_season:{int(target_tid)}:S{target_sn}"
            )
        cached_tot = int(cached_m.get("total") or cached_m.get("episode_count") or 0) if cached_m else 0
        tot = active_map.get("expected_total_episodes") or (cached_tot if cached_tot > 0 else rec.total_episodes)
        finished = bool(
            (cached_m or {}).get("finished", True) if cached_m else True
        )
        rec.total_episodes = tot
        rec.season_finished = finished
        missing = [ep for ep in range(1, tot + 1) if ep not in set(recognized)] if tot else []
        rec.missing_episodes_json = json.dumps(missing)
        new_status, new_detail = diagnose_season(
            recognized=recognized,
            total_episodes=tot,
            season_finished=finished,
            has_duplicates=bool(duplicates),
            duplicate_episodes=duplicates,
            allow_bonus_specials=True,
        )
        rec.status = new_status
        label = active_map.get("target_title") or active_map.get("target_original_title") or f"TMDB:{target_tid}"
        rec.status_detail = f"已按单季跨条目/改绑映射 ({label} S{target_sn:02d}) 重算：{new_detail}"
        rec.mapping_json = json.dumps(active_map, ensure_ascii=False)
        if isinstance(conflict, dict) and conflict.get("blocking"):
            rec.conflict_json = json.dumps(
                {**conflict, "blocking": False, "resolved_by_mapping": True, "previous_conflict": conflict},
                ensure_ascii=False,
            )
        rec.last_checked_at = now_dt
        active_key = build_identity_key(rec.show_name, rec.tmdb_id, rec.season_number, active_map)
        await supersede_changed_identity_records(db, rec.task_id, rec.season_path, active_key, rec.id)
        return True

    # 无激活映射：若存在先前保存的冲突（如撤销映射后恢复原身份冲突提示）则恢复，否则读取已核实缓存或保持原安全诊断
    prev_conflict = (conflict or {}).get("previous_conflict") if isinstance(conflict, dict) else None
    if isinstance(prev_conflict, dict) and prev_conflict.get("blocking"):
        rec.conflict_json = json.dumps(prev_conflict, ensure_ascii=False)
        rec.mapping_json = "{}"
        rec.status = "identity_review"
        rec.status_detail = prev_conflict.get("reason") or "已撤销单季映射，恢复原作品/季号冲突拦截，待人工核对"
        rec.last_checked_at = now_dt
        active_key = build_identity_key(rec.show_name, rec.tmdb_id, rec.season_number, None)
        await supersede_changed_identity_records(db, rec.task_id, rec.season_path, active_key, rec.id)
        return True

    if isinstance(conflict, dict) and conflict.get("blocking"):
        rec.mapping_json = "{}"
        rec.status = "identity_review"
        rec.last_checked_at = now_dt
        return True

    cached_season = (
        (
            await get_verified_metadata_snapshot(db, f"tmdb:{int(tid_int)}:S{rec.season_number}")
            or await get_verified_metadata_snapshot(db, f"tmdb_season:{tid_int}:S{rec.season_number}")
        )
        if tid_int
        else None
    )
    cached_tot = int(cached_season.get("total") or cached_season.get("episode_count") or 0) if cached_season else 0
    tot = cached_tot if cached_tot > 0 else rec.total_episodes
    finished = bool(cached_season.get("finished", rec.season_finished)) if cached_season else bool(rec.season_finished)
    if tot and tot > 0:
        missing = [ep for ep in range(1, tot + 1) if ep not in set(recognized)]
        rec.total_episodes = tot
        rec.season_finished = finished
        rec.missing_episodes_json = json.dumps(missing)
        new_status, new_detail = diagnose_season(
            recognized=recognized,
            total_episodes=tot,
            season_finished=finished,
            has_duplicates=bool(duplicates),
            duplicate_episodes=duplicates,
            allow_bonus_specials=True,
        )
        rec.status = new_status
        rec.status_detail = new_detail
    rec.mapping_json = "{}"
    rec.last_checked_at = now_dt
    active_key = build_identity_key(rec.show_name, rec.tmdb_id, rec.season_number, None)
    await supersede_changed_identity_records(db, rec.task_id, rec.season_path, active_key, rec.id)
    return True


@router.post("/{task_id}/recalculate")
async def recalculate_task(
    task_id: str,
    body: RecalculateRequest | None = Body(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """只读重新核算现有季记录（保留记录ID与manually_handled，不删源/归档文件，不重推下载）。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    req = body or RecalculateRequest()
    stmt = select(SeasonRecord).where(SeasonRecord.task_id == task_id)
    if req.record_ids:
        stmt = stmt.where(SeasonRecord.id.in_(req.record_ids))
    rows = list((await db.execute(stmt)).scalars().all())

    updated_count = 0
    preserved_handled = 0
    for rec in rows:
        if rec.status in ("manually_handled", "verified_complete", "cleaned_redundant", "source_absent"):
            preserved_handled += 1
            continue
        if await _apply_in_place_recalculate(db, rec, fetch_remote=req.fetch_remote):
            updated_count += 1

    task_summary = await refresh_task_summary(task_id, db)
    await db.commit()
    return {
        "ok": True,
        "updated_count": updated_count,
        "preserved_handled": preserved_handled,
        "summary": task_summary,
        "message": f"已完成只读重新核算（更新 {updated_count} 季，保留已处理记录 {preserved_handled} 季）",
    }


class SeasonMappingUpsertRequest(BaseModel):
    target_tmdb_id: int | None = None
    target_douban_id: str = ""
    target_season: int = 1
    target_title: str = ""
    target_original_title: str = ""
    search_keywords: list[str] = []
    archive_aliases: list[str] = []
    numbering_mode: str = "independent"
    episode_offset: int = 0
    expected_total_episodes: int | None = None
    mapping_kind: str = "cross_entry_alias"
    evidence_note: str = ""


@router.post("/{task_id}/seasons/{record_id}/season-mapping")
async def upsert_season_mapping(
    task_id: str,
    record_id: str,
    body: SeasonMappingUpsertRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """绑定或确认单季跨条目映射/单季作品改绑（仅影响当前这一季，同剧其他季不受影响）。"""
    task = await db.get(Task, task_id)
    rec = await db.get(SeasonRecord, record_id)
    if not task or not rec or rec.task_id != task_id:
        raise HTTPException(status_code=404, detail="剧季记录不存在")

    if (body.target_tmdb_id is None or body.target_tmdb_id <= 0) and (
        body.expected_total_episodes is None or body.expected_total_episodes <= 0
    ):
        raise HTTPException(status_code=400, detail="请至少提供有效的目标 TMDB ID 或目标总集数")

    prev_conflict = _safe_json(getattr(rec, "conflict_json", None), {})
    existing_active = (
        await db.execute(
            select(SeasonMapping).where(
                SeasonMapping.scope_key == rec.season_path,
                SeasonMapping.is_active.is_(True),
            )
        )
    ).scalars().all()
    for old_m in existing_active:
        old_m.is_active = False

    src_tid = int(rec.tmdb_id) if rec.tmdb_id and str(rec.tmdb_id).isdigit() else None
    new_row = SeasonMapping(
        scope_key=rec.season_path,
        source_tmdb_id=src_tid,
        source_season=rec.season_number,
        show_path=rec.show_path,
        season_path=rec.season_path,
        mapping_kind=body.mapping_kind or "cross_entry_alias",
        target_tmdb_id=body.target_tmdb_id,
        target_douban_id=str(body.target_douban_id or "").strip(),
        target_season=max(1, int(body.target_season or 1)),
        target_title=str(body.target_title or "").strip(),
        target_original_title=str(body.target_original_title or "").strip(),
        search_keywords_json=json.dumps([k.strip() for k in body.search_keywords if str(k).strip()], ensure_ascii=False),
        archive_aliases_json=json.dumps([a.strip() for a in body.archive_aliases if str(a).strip()], ensure_ascii=False),
        numbering_mode=body.numbering_mode or "independent",
        episode_offset=int(body.episode_offset or 0),
        expected_total_episodes=body.expected_total_episodes,
        evidence_note=str(body.evidence_note or "").strip(),
        created_by="user",
        is_active=True,
    )
    db.add(new_row)
    await db.flush()

    mapping_payload = build_season_mapping_payload(
        {
            "mapping_id": f"user:{new_row.id}",
            "source_tmdb_id": src_tid,
            "source_season": rec.season_number,
            "mapping_kind": new_row.mapping_kind,
            "target_tmdb_id": new_row.target_tmdb_id,
            "target_douban_id": new_row.target_douban_id,
            "target_season": new_row.target_season,
            "target_title": new_row.target_title,
            "target_original_title": new_row.target_original_title,
            "search_keywords": body.search_keywords,
            "archive_aliases": body.archive_aliases,
            "numbering_mode": new_row.numbering_mode,
            "episode_offset": new_row.episode_offset,
            "expected_total_episodes": new_row.expected_total_episodes,
            "evidence_note": new_row.evidence_note,
            "created_by": "user",
        }
    )

    if isinstance(prev_conflict, dict) and prev_conflict.get("blocking"):
        rec.conflict_json = json.dumps(
            {**prev_conflict, "blocking": False, "resolved_by_mapping": True, "previous_conflict": prev_conflict},
            ensure_ascii=False,
        )

    await _apply_in_place_recalculate(db, rec, fetch_remote=False)
    # _apply_in_place_recalculate 会读 resolve_active_season_mapping 写入 mapping_json；若请求含 expected_total_episodes 一并确保最新
    rec.mapping_json = json.dumps(mapping_payload, ensure_ascii=False)
    task_summary = await refresh_task_summary(task_id, db)
    await db.commit()

    return {
        "ok": True,
        "season": await _serialize_season_record(db, rec),
        "summary": task_summary,
        "message": f"已为 {rec.show_name} S{rec.season_number:02d} 单独确认跨条目/作品映射（仅作用于本季）",
    }


@router.delete("/{task_id}/seasons/{record_id}/season-mapping")
async def delete_season_mapping(
    task_id: str,
    record_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """一键撤销单季跨条目映射/改绑（仅恢复当前这一季，不影响同剧其他季）。"""
    task = await db.get(Task, task_id)
    rec = await db.get(SeasonRecord, record_id)
    if not task or not rec or rec.task_id != task_id:
        raise HTTPException(status_code=404, detail="剧季记录不存在")

    rows = (
        await db.execute(
            select(SeasonMapping).where(
                SeasonMapping.scope_key == rec.season_path,
                SeasonMapping.is_active.is_(True),
            )
        )
    ).scalars().all()
    for row in rows:
        row.is_active = False

    # 也标记禁用可能由内置 seed 激活记录（若用户撤销的是 seed 映射，创建一条 is_active=False 用户撤销覆盖或仅清空 mapping_json 后重算）
    seed_override = _safe_json(getattr(rec, "mapping_json", None), {})
    await _apply_in_place_recalculate(db, rec, fetch_remote=False)
    task_summary = await refresh_task_summary(task_id, db)
    await db.commit()

    return {
        "ok": True,
        "season": await _serialize_season_record(db, rec),
        "summary": task_summary,
        "revoked_count": len(rows) or (1 if seed_override else 0),
        "message": f"已撤销 {rec.show_name} S{rec.season_number:02d} 的单季映射，已恢复本季原口径状态",
    }


class ReconcileGapFillRequest(BaseModel):
    season_record_ids: list[str] = []
    force_release_missing: bool = True


@router.post("/{task_id}/reconcile-gap-fill")
async def reconcile_task_gap_fill(
    task_id: str,
    body: ReconcileGapFillRequest | None = Body(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """一键“核对入库”：联合 MP 下载队列、整理队列、下载/转移历史与双库实体文件刷新入库状态。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    req = body or ReconcileGapFillRequest()
    reconcile_summary = await reconcile_gap_fill_records(
        db=db,
        task_id=task_id,
        season_record_ids=req.season_record_ids or None,
        force_release_missing=req.force_release_missing,
    )
    task_summary = await refresh_task_summary(task_id, db)
    await db.commit()

    return {
        "ok": True,
        "reconcile": reconcile_summary,
        "summary": task_summary,
        "message": (
            f"核对入库完成：检查 {reconcile_summary['checked_seasons']} 季，"
            f"确认入库 {reconcile_summary['reconciled_in_library']} 集，"
            f"仍在队列/待整理 {reconcile_summary['still_pending']} 集，"
            f"退回待补 {reconcile_summary['released_to_todo']} 集"
        ),
    }


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
        rec.status in ("already_archived", "unknown", "archive_check", "period_review", "identity_review")
        or not rec.total_episodes
        or (rec.status != "archive_ready" and max(recognized, default=0) > rec.total_episodes)
    )
    if not is_anomaly:
        raise HTTPException(status_code=409, detail="仅异常剧集可标记为手动处理")
    previous = rec.status
    changed = await db.execute(update(SeasonRecord).where(
        SeasonRecord.id == record_id, SeasonRecord.status == previous
    ).values(status="manually_handled"))
    if not changed.rowcount:
        await db.refresh(rec)
        if rec.status != "manually_handled":
            raise HTTPException(status_code=409, detail="条目状态已更新，请刷新清单")
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
