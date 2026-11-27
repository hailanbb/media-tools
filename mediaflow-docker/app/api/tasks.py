"""初检任务 CRUD 端点."""

from __future__ import annotations

import json
import logging
import posixpath
import re
import uuid
import time
import hashlib
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from collections import Counter

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.source_presence import source_entry, record_source_absent
from app.config import get_settings
from app.core.directory_governance import (
    build_physical_dir_entry,
    classify_tmdb_directory_group,
    confirm_directory_group,
    enrich_directory_groups_from_db,
    extract_dir_tmdb_id,
    normalize_dir_path,
    resolve_category_key,
    serialize_directory_group,
    sync_directory_groups_for_task,
)
from app.core.identity_resolver import (
    build_season_mapping_payload,
    get_verified_metadata_snapshot,
    reevaluate_offline_conflict,
    resolve_active_season_mapping,
)
from app.core.inspector import diagnose_season, metadata_reference_note, unverified_metadata_candidates
from app.core.manual_confirmation import (
    evaluate_confirmation_outcome,
    orm_confirmation_to_dict,
    resolve_active_manual_confirmation,
)
from app.database import get_db
from app.models.directory_group import DirectoryGroup
from app.models.manual_confirmation import ManualSeasonConfirmation
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
log = logging.getLogger(__name__)


def _task_to_out(task: Task) -> TaskOut:
    """将 ORM Task 转为 API 输出，解析 summary_json。"""
    summary = None
    if task.summary_json:
        try:
            d = json.loads(task.summary_json)
            summary = TaskSummary(**d)
        except Exception:
            pass
    from app.services.task_service import TASK_CANCELLED
    stopping = task.id in TASK_CANCELLED and task.status in ("running", "paused")
    return TaskOut(
        id=task.id,
        name=task.name,
        source_path=task.source_path,
        media_type=task.media_type,
        status="stopping" if stopping else task.status,
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
    """删除任务及其所有关联剧季记录与目录组."""
    from sqlalchemy import delete
    from app.models.season import SeasonRecord
    from app.models.directory_group import DirectoryGroup
    from app.models.operation import OperationLog
    from app.models.gap_fill import GapFillEpisode, GapFillSeasonRun

    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    from app.services.task_service import (
        TASK_RUNNING_WORKERS,
        TASK_PROGRESS,
        TASK_CANCELLED,
        TASK_STOP_EVENTS,
    )
    worker = TASK_RUNNING_WORKERS.pop(task_id, None)
    if worker and not worker.done():
        worker.cancel()
    TASK_STOP_EVENTS.pop(task_id, None)
    TASK_CANCELLED.discard(task_id)
    TASK_PROGRESS.pop(task_id, None)

    # 显式执行原子批量删除，避免级联触发 ORM 单条反序列化开销与数据格式瑕疵导致的阻断
    await db.execute(delete(SeasonRecord).where(SeasonRecord.task_id == task_id))
    await db.execute(delete(DirectoryGroup).where(DirectoryGroup.task_id == task_id))
    await db.execute(delete(OperationLog).where(OperationLog.task_id == task_id))
    await db.execute(delete(GapFillEpisode).where(GapFillEpisode.task_id == task_id))
    await db.execute(delete(GapFillSeasonRun).where(GapFillSeasonRun.task_id == task_id))
    await db.execute(delete(Task).where(Task.id == task_id))
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
    # 保留已有 summary_json 作为 fallback 快照，避免重检立即中止时丢失已有数字
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
    from app.core.season_classifier import resolve_record_gap_category
    gap_view = await compute_season_gap_fill_view(db, r)
    gap_cat_info = await resolve_record_gap_category(db, r)
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
        "gap_category": gap_cat_info.get("category") or "pending",
        "gap_category_source": gap_cat_info.get("source") or "pending",
        "gap_category_reason": gap_cat_info.get("reason") or "",
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

    terminal_tuple = ("manually_handled", "verified_complete", "cleaned_redundant", "source_absent", "archived")
    if status and status in terminal_tuple:
        return []

    stmt = select(SeasonRecord).where(
        SeasonRecord.task_id == task_id,
        SeasonRecord.status.not_in(tuple(s for s in terminal_tuple if s != "cleaned_redundant")),
    )
    stmt = stmt.order_by(SeasonRecord.show_name, SeasonRecord.season_number)
    result = await db.execute(stmt)
    records = result.scalars().all()
    from app.services.source_cleanup_state import pending_root_cleanup_ids
    pending_roots = await pending_root_cleanup_ids(db, records)
    records = [r for r in records if r.status != "cleaned_redundant" or r.id in pending_roots]
    serialized = [await _serialize_season_record(db, r) for r in records]
    for item in serialized:
        if item["id"] in pending_roots:
            item["status"] = "already_archived"
            item["status_detail"] = "本季已清理，旧回执显示剧集外层未清完；可继续核验并收尾"
            item["evidence"]["source_root_followup"] = True

    from app.models.directory_group import DirectoryGroup
    from app.core.season_classifier import consolidate_seasons_to_work_records

    pending_dg = (
        await db.execute(
            select(DirectoryGroup.id).where(
                DirectoryGroup.task_id == task_id,
                DirectoryGroup.status == "pending_confirm",
            )
        )
    ).scalars().all()
    pending_dir_group_ids = set(pending_dg)

    work_records, _ = consolidate_seasons_to_work_records(serialized, pending_dir_group_ids)

    if status:
        status_to_cat = {
            "airing": "airing",
            "archive": "archive",
            "archive_ready": "archive",
            "period_ready": "archive",
            "download": "download",
            "download_needed": "download",
            "period_missing": "download",
            "review": "review",
            "anomaly": "anomaly",
            "held": "held",
        }
        groups = {
            "archive_ready": ("archive_ready", "period_ready"),
            "review_needed": ("review_needed", "period_review", "identity_review", "already_archived"),
            "download_needed": ("download_needed", "period_missing"),
        }
        if status in status_to_cat:
            target_cat = status_to_cat[status]
            work_records = [w for w in work_records if w.get("main_category") == target_cat]
        else:
            allowed_st = groups.get(status, (status,))
            work_records = [
                w for w in work_records
                if w.get("status") in allowed_st and w.get("main_category") != "airing"
            ]

    if gap_view in ("todo", "pushed_pending"):
        work_records = [
            item
            for item in work_records
            if (item.get("gap_fill_state") or {}).get("view_bucket") == gap_view
        ]
    return work_records


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
    if rec.status in ("manually_handled", "verified_complete", "cleaned_redundant", "source_absent", "archived"):
        return False

    recognized = [int(x) for x in _safe_json(rec.recognized_episodes_json, []) if str(x).isdigit()]
    duplicates = [int(x) for x in _safe_json(rec.duplicate_episodes_json, []) if str(x).isdigit()]
    tid_int = int(rec.tmdb_id) if rec.tmdb_id and str(rec.tmdb_id).isdigit() else None
    active_map = await resolve_active_season_mapping(
        db, tid_int, rec.season_number, rec.show_path, rec.season_path
    )

    diag = None
    remote_error = ""
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
        except Exception as exc:
            remote_error = f"{type(exc).__name__}: {exc}"
            log.warning("远程实时核查重算未完成 [任务 %s 季记录 %s (%s)]: %s", rec.task_id, rec.id, rec.show_name, exc, exc_info=True)
            diag = None

    now_dt = datetime.now(timezone.utc)
    if fetch_remote and (diag is None or (diag.evidence or {}).get("meta_status") == "fetch_failed"):
        evidence = _safe_json(getattr(rec, "evidence_json", None), {})
        evidence["last_remote_recheck_error"] = remote_error or ((diag.evidence or {}).get("meta_error") if diag else "") or "未取得本季核查结果"
        rec.evidence_json = json.dumps(evidence, ensure_ascii=False)
        return False
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

    # 同季有效人工确认优先于映射重算；否则映射分支提前返回会遗留旧异常证据。
    # 确认仍由 evaluate_confirmation_outcome 检查混放、显式异作品 ID 和集号矛盾。
    manual_conf = await resolve_active_manual_confirmation(
        db,
        tmdb_id=rec.tmdb_id,
        season_number=rec.season_number,
        season_path=rec.season_path,
        show_path=rec.show_path,
    )
    if manual_conf:
        dual_info_mc = (evidence or {}).get("dual_archive_info") or {}
        arc_eps_mc = [int(x) for x in (dual_info_mc.get("archived_episodes") or []) if str(x).isdigit()]
        ms_mc = (evidence or {}).get("mediaserver_check") or {}
        emby_eps_mc = (
            [int(x) for x in (ms_mc.get("season_episodes") or []) if str(x).isdigit()]
            if ms_mc.get("trusted_match") is not False
            else []
        )
        eval_recognized = list(recognized)
        scope_paths_mc = list(manual_conf.get("scope_paths") or [])
        if len(scope_paths_mc) > 1 and rec.task_id:
            peer_rows = list(
                (
                    await db.scalars(
                        select(SeasonRecord).where(
                            SeasonRecord.task_id == rec.task_id,
                            SeasonRecord.season_path.in_(scope_paths_mc),
                        )
                    )
                ).all()
            )
            for pr in peer_rows:
                eval_recognized.extend(
                    int(x) for x in _safe_json(pr.recognized_episodes_json, []) if str(x).isdigit()
                )
            eval_recognized = sorted(set(eval_recognized))
        m_outcome = evaluate_confirmation_outcome(
            conf=manual_conf,
            tmdb_id=rec.tmdb_id,
            season_number=rec.season_number,
            recognized_episodes=eval_recognized,
            archived_episodes=arc_eps_mc,
            emby_episodes=emby_eps_mc,
            duplicate_episodes=duplicates,
            current_total_episodes=rec.total_episodes,
            current_season_finished=rec.season_finished,
            current_status=rec.status,
            current_status_detail=rec.status_detail or "",
            evidence=evidence,
            conflict=conflict if isinstance(conflict, dict) else {},
            pending_dir_group=bool(((evidence or {}).get("directory_group") or {}).get("requires_confirm")),
        )
        rec.status = m_outcome["status"]
        rec.status_detail = m_outcome["status_detail"]
        rec.total_episodes = m_outcome["total_episodes"]
        rec.season_finished = m_outcome["season_finished"]
        rec.missing_episodes_json = json.dumps(m_outcome["missing_episodes"])
        evidence = dict(evidence or {})
        if m_outcome.get("identity_cleared"):
            if isinstance(conflict, dict) and conflict.get("blocking") and conflict.get("kind") != "mixed_works":
                rec.conflict_json = json.dumps(
                    {**conflict, "blocking": False, "resolved_by_manual_confirmation": True},
                    ensure_ascii=False,
                )
            evidence["meta_status"] = "manual_confirmed" if rec.total_episodes else "total_episodes_pending"
        b_ev_mc = dict(evidence.get("broadcast_evidence") or {})
        if manual_conf.get("broadcast_status") == "airing":
            b_ev_mc.update(status="airing", schedule_verified=True, source="manual_confirmation")
            evidence["broadcast_evidence"] = b_ev_mc
        elif manual_conf.get("broadcast_status") == "finished" and not m_outcome["contradiction"].get("has_contradiction"):
            b_ev_mc.update(status="finished", schedule_verified=True, source="manual_confirmation")
            evidence["broadcast_evidence"] = b_ev_mc
        evidence["manual_confirmation"] = {
            **manual_conf,
            "contradiction": m_outcome["contradiction"],
        }
        evidence["last_recalculated_at"] = now_dt.isoformat()
        rec.evidence_json = json.dumps(evidence, ensure_ascii=False)
        if rec.last_checked_at is None:
            rec.last_checked_at = now_dt
        if active_map:
            rec.mapping_json = json.dumps(active_map, ensure_ascii=False)
        active_key = build_identity_key(rec.show_name, rec.tmdb_id, rec.season_number, active_map)
        await supersede_changed_identity_records(db, rec.task_id, rec.season_path, active_key, rec.id)
        return True

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
        b_ev = (evidence or {}).get("broadcast_evidence") if isinstance(evidence, dict) else None
        was_verified_airing = (
            rec.status == "airing"
            or (isinstance(b_ev, dict) and b_ev.get("status") in ("airing", "upcoming"))
            or (rec.season_finished is False and rec.total_episodes is not None and rec.status not in ("identity_review", "unknown"))
        )
        default_fin = False if was_verified_airing else True
        finished = bool(
            (cached_m or {}).get("finished", default_fin) if cached_m else default_fin
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
        evidence = dict(evidence or {})
        evidence["last_recalculated_at"] = now_dt.isoformat()
        rec.evidence_json = json.dumps(evidence, ensure_ascii=False)
        if rec.last_checked_at is None:
            rec.last_checked_at = now_dt
        active_key = build_identity_key(rec.show_name, rec.tmdb_id, rec.season_number, active_map)
        await supersede_changed_identity_records(db, rec.task_id, rec.season_path, active_key, rec.id)
        return True

    # 无激活映射：按最新规则重新评估既有冲突/前冲突，消除外文译名误报并保留真实错配拦截
    cached_tv = await get_verified_metadata_snapshot(db, f"tmdb_tv:{int(tid_int)}") if tid_int else None
    cached_aliases = await get_verified_metadata_snapshot(db, f"tmdb_aliases:{int(tid_int)}") if tid_int else None
    if isinstance(cached_aliases, dict) and cached_aliases.get("aliases"):
        cached_tv = dict(cached_tv or {})
        cached_tv["aliases"] = list(
            dict.fromkeys([*(cached_tv.get("aliases") or []), *(cached_aliases.get("aliases") or [])])
        )
        if cached_aliases.get("en_name") and not cached_tv.get("en_name"):
            cached_tv["en_name"] = cached_aliases["en_name"]

    prev_conflict = (conflict or {}).get("previous_conflict") if isinstance(conflict, dict) else None
    candidate_conflict = (
        prev_conflict
        if (isinstance(prev_conflict, dict) and prev_conflict.get("blocking"))
        else (conflict if isinstance(conflict, dict) and conflict.get("blocking") else None)
    )
    conflict_cleared = False
    if isinstance(candidate_conflict, dict) and candidate_conflict.get("blocking"):
        still_blocking, eval_conflict, _ = reevaluate_offline_conflict(
            show_name=rec.show_name,
            tmdb_id=rec.tmdb_id,
            season_number=rec.season_number,
            conflict=candidate_conflict,
            evidence=evidence,
            primary_work_snapshot=cached_tv,
        )
        if still_blocking:
            rec.conflict_json = json.dumps(eval_conflict, ensure_ascii=False)
            rec.mapping_json = "{}"
            rec.status = "identity_review"
            rec.status_detail = eval_conflict.get("reason") or eval_conflict.get("message") or rec.status_detail
            evidence = dict(evidence or {})
            evidence["last_recalculated_at"] = now_dt.isoformat()
            rec.evidence_json = json.dumps(evidence, ensure_ascii=False)
            if rec.last_checked_at is None:
                rec.last_checked_at = now_dt
            active_key = build_identity_key(rec.show_name, rec.tmdb_id, rec.season_number, None)
            await supersede_changed_identity_records(db, rec.task_id, rec.season_path, active_key, rec.id)
            return True
        rec.conflict_json = "{}"
        conflict_cleared = True

    reference_candidates = unverified_metadata_candidates(evidence, rec.tmdb_id, rec.season_number)
    if reference_candidates:
        rec.status = "unknown"
        rec.total_episodes = None
        rec.season_finished = False
        rec.missing_episodes_json = "[]"
        rec.status_detail = metadata_reference_note(rec.tmdb_id, rec.season_number, reference_candidates, recognized)
        evidence["meta_status"] = "metadata_unverified"
        evidence["broadcast_evidence"] = {
            "status": "evidence_pending", "schedule_verified": False,
            "missing_evidence_reason": "候选资料尚未确认与目录作品对应",
        }
        evidence["matched_work"] = {**(evidence.get("matched_work") or {}), "seasons_present": []}
        evidence["last_recalculated_at"] = now_dt.isoformat()
        rec.evidence_json = json.dumps(evidence, ensure_ascii=False)
        return True

    cached_season = (
        (
            await get_verified_metadata_snapshot(db, f"tmdb:{int(tid_int)}:S{rec.season_number}")
            or await get_verified_metadata_snapshot(db, f"tmdb_season:{tid_int}:S{rec.season_number}")
        )
        if tid_int
        else None
    )
    if cached_season and cached_season.get("candidate_id"):
        cached_season = None
    cached_tot = int(cached_season.get("total") or cached_season.get("episode_count") or 0) if cached_season else 0
    tot = cached_tot if cached_tot > 0 else rec.total_episodes
    finished = bool(cached_season.get("finished", rec.season_finished)) if cached_season else bool(rec.season_finished)
    present_seasons = list(
        (cached_tv or {}).get("seasons")
        or ((evidence or {}).get("matched_work") or {}).get("seasons_present")
        or []
    )
    dual_info = (evidence or {}).get("dual_archive_info") or {}
    archived_eps = {int(x) for x in (dual_info.get("archived_episodes") or []) if str(x).isdigit()}

    evidence = dict(evidence or {})
    b_ev = dict(evidence.get("broadcast_evidence") or {})
    cached_b_ev = dict((cached_season or {}).get("broadcast_evidence") or {}) if isinstance(cached_season, dict) else {}
    orig_fetched_at = b_ev.get("fetched_at") or cached_b_ev.get("fetched_at") or (rec.last_checked_at.isoformat() if rec.last_checked_at else "")
    eff_b_ev = {**cached_b_ev, **b_ev}
    if orig_fetched_at:
        eff_b_ev["fetched_at"] = orig_fetched_at

    # 检查旧排期是否已跨过今日 (Section 3 & Section 4)
    today_iso = datetime.now().strftime("%Y-%m-%d")
    upc_sched = list(eff_b_ev.get("upcoming_schedule") or [])
    if upc_sched:
        past_upc = [x for x in upc_sched if str(x.get("air_date") or "9999-99-99")[:10] <= today_iso]
        if past_upc:
            eff_b_ev["schedule_expired_past_today"] = True
            if len(past_upc) == len(upc_sched) and not finished:
                eff_b_ev["status"] = "evidence_pending"
                eff_b_ev["missing_evidence_reason"] = "上次排期已跨过今日，需重新初检核实最新播期与入库情况"

    # L04: 旧记录仅靠 180 天推定、或只有 season_finished=false 且无核实排期依据时，转入证据待决
    is_legacy_unverified_false = (
        rec.season_finished is False
        and rec.status != "airing"
        and not eff_b_ev.get("schedule_verified")
        and eff_b_ev.get("status") not in ("airing", "upcoming", "finished")
        and not (rec.status == "review_needed" and "连载中" in str(rec.status_detail or ""))
    )
    if (
        eff_b_ev.get("status") == "evidence_pending"
        or eff_b_ev.get("heuristic_180d_blocked")
        or is_legacy_unverified_false
    ):
        pend_reason = eff_b_ev.get("missing_evidence_reason") or "旧记录缺少本季分集播期或终集依据"
        eff_b_ev["status"] = "evidence_pending"
        eff_b_ev["schedule_verified"] = False
        eff_b_ev["missing_evidence_reason"] = pend_reason
        rec.status = "period_review"
        rec.season_finished = False
        rec.missing_episodes_json = "[]"
        rec.status_detail = f"证据待决 · 安全保留：{pend_reason}（已用上次初检资料重新核算）"
    elif tot and tot > 0:
        combined_eps = sorted(set(recognized) | archived_eps) if archived_eps else recognized
        expected_set = set(range(1, tot + 1))
        aired_in_ev = [int(x) for x in (eff_b_ev.get("aired_episodes") or []) if str(x).isdigit() and int(x) > 0]
        if not finished and aired_in_ev:
            missing = [ep for ep in sorted(set(aired_in_ev)) if ep not in set(combined_eps)]
        else:
            missing = [ep for ep in range(1, tot + 1) if ep not in set(combined_eps)]
        rec.total_episodes = tot
        rec.season_finished = finished
        rec.missing_episodes_json = json.dumps(missing)
        # L06: 仅当已证实本季播毕 (finished=True) 时，才允许提前进入 already_archived 或 archive_ready
        if finished and archived_eps and expected_set <= archived_eps and recognized and set(recognized) <= archived_eps:
            rec.status = "already_archived"
            rec.status_detail = f"归档库已包含源端正片集号 {sorted(set(recognized))}，可复核源端重复版本"
        elif finished and archived_eps and not missing and not duplicates and (set(recognized) - archived_eps):
            rec.status = "archive_ready"
            rec.status_detail = f"两库合计已齐，源端含增量集 {sorted(set(recognized) - archived_eps)}"
        else:
            new_status, new_detail = diagnose_season(
                recognized=combined_eps,
                total_episodes=tot,
                season_finished=finished,
                has_duplicates=bool(duplicates),
                duplicate_episodes=duplicates,
                allow_bonus_specials=True,
            )
            rec.status = new_status
            rec.status_detail = new_detail
    elif conflict_cleared:
        if present_seasons and rec.season_number not in [int(s) for s in present_seasons if str(s).isdigit()]:
            rec.status = "unknown"
            present_text = "、".join(f"S{int(s):02d}" for s in present_seasons if str(s).isdigit())
            rec.status_detail = f"主条目 TMDB {tid_int or '?'} 仅收录 {present_text}，当前季表未收录 S{rec.season_number:02d}，请人工核对片源来源。"
        elif recognized:
            rec.status = "unknown"
            rec.status_detail = "已解除外文发行名误报冲突，待重新匹配官方集数后核算。"

    if eff_b_ev:
        evidence["broadcast_evidence"] = eff_b_ev
    evidence["last_recalculated_at"] = now_dt.isoformat()
    rec.evidence_json = json.dumps(evidence, ensure_ascii=False)
    rec.mapping_json = "{}"
    if rec.last_checked_at is None:
        rec.last_checked_at = now_dt
    active_key = build_identity_key(rec.show_name, rec.tmdb_id, rec.season_number, None)
    await supersede_changed_identity_records(db, rec.task_id, rec.season_path, active_key, rec.id)
    return True


async def _rebuild_offline_directory_groups(
    db: AsyncSession,
    task_id: str,
    rows: list[SeasonRecord],
) -> list[dict[str, Any]]:
    """根据任务当前数据库季记录与已有归档库索引快照重建同 TMDB 多目录/异名目录组。"""
    settings = get_settings()
    if not settings.enable_multi_dir_governance:
        synced = await sync_directory_groups_for_task(db, task_id, [])
        return [serialize_directory_group(g) for g in synced]

    existing_groups = list(
        (await db.scalars(select(DirectoryGroup).where(DirectoryGroup.task_id == task_id))).all()
    )
    entries_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}

    # 1. 保留此前初检发现并持久化下来的归档库物理目录（确保离线重算时不会丢掉异名归档目录对比）
    for eg in existing_groups:
        try:
            old_entries = json.loads(eg.paths_json or "[]")
        except Exception:
            old_entries = []
        arc_entries = [
            e for e in old_entries
            if isinstance(e, dict) and e.get("side") == "archive" and e.get("norm_path")
        ]
        if arc_entries:
            entries_by_key.setdefault((eg.category_key, eg.tmdb_id), []).extend(arc_entries)

    # 2. 按当前任务下激活的 SeasonRecord 重建源端物理剧集目录与季集覆盖
    by_show: dict[str, list[SeasonRecord]] = {}
    for rec in rows:
        if rec.status in ("source_absent", "cleaned_redundant", "archived"):
            continue
        by_show.setdefault(normalize_dir_path(rec.show_path), []).append(rec)

    for norm_show, recs in by_show.items():
        if not norm_show:
            continue
        rep = recs[0]
        tid_str = str(rep.tmdb_id or extract_dir_tmdb_id(rep.show_name) or "").strip()
        if not tid_str.isdigit():
            continue
        cat_key = resolve_category_key(norm_show, settings.source_root, settings.archive_root)
        seasons_cov = sorted({int(r.season_number) for r in recs})
        ep_cov = {
            str(r.season_number): sorted(
                {int(x) for x in _safe_json(r.recognized_episodes_json, []) if str(x).isdigit()}
            )
            for r in recs
        }
        entries_by_key.setdefault((cat_key, tid_str), []).append(
            build_physical_dir_entry(
                side="source",
                category=cat_key,
                show_path=norm_show,
                tmdb_id=tid_str,
                seasons=seasons_cov,
                episodes_by_season=ep_cov,
            )
        )
        # 若 dual_archive_info 记录了物理归档主目录，一并纳入（仅存在于归档且非源同路径时）
        for r in recs:
            ev = _safe_json(getattr(r, "evidence_json", None), {})
            dual = (ev or {}).get("dual_archive_info") or {}
            p_arc = normalize_dir_path(dual.get("primary_path") or "")
            if p_arc and p_arc != "/" and normalize_dir_path(r.season_path) != norm_show:
                arc_show = normalize_dir_path(p_arc.rsplit("/", 1)[0])
                if arc_show and arc_show != norm_show:
                    entries_by_key[(cat_key, tid_str)].append(
                        build_physical_dir_entry(
                            side="archive",
                            category=cat_key,
                            show_path=arc_show,
                            tmdb_id=tid_str,
                            seasons=[r.season_number],
                            episodes_by_season={
                                str(r.season_number): [
                                    int(x) for x in (dual.get("archived_episodes") or []) if str(x).isdigit()
                                ]
                            },
                        )
                    )

    detected: list[dict[str, Any]] = []
    for (_, _), ents in sorted(entries_by_key.items()):
        grp = classify_tmdb_directory_group(ents)
        if grp:
            detected.append(grp)

    synced = await sync_directory_groups_for_task(db, task_id, detected)
    return await enrich_directory_groups_from_db(
        db, task_id, [serialize_directory_group(g) for g in synced]
    )


async def _run_task_reevaluate_impl(
    db: AsyncSession,
    task_id: str,
    req: RecalculateRequest,
) -> dict[str, Any]:
    from app.services.task_service import TASK_RUNNING_WORKERS
    task = await db.get(Task, task_id)
    if task and (task.status in ("running", "paused", "stopping") or task_id in TASK_RUNNING_WORKERS):
        raise HTTPException(status_code=409, detail="请在初检结束后重新核查")
    stmt = select(SeasonRecord).where(SeasonRecord.task_id == task_id)
    all_rows = list((await db.execute(stmt)).scalars().all())
    target_set = set(req.record_ids) if req.record_ids else None

    preserved_statuses = ("manually_handled", "verified_complete", "cleaned_redundant", "source_absent", "archived")
    active_before = [r for r in all_rows if r.status not in preserved_statuses]
    before_counts = dict(Counter(r.status for r in active_before))

    updated_count = 0
    preserved_handled = 0
    changes: list[dict[str, Any]] = []
    remote_failures: list[dict[str, str]] = []
    for rec in all_rows:
        if target_set is not None and rec.id not in target_set:
            continue
        if rec.status in preserved_statuses:
            preserved_handled += 1
            continue
        prev_status = rec.status
        prev_conflict = _safe_json(getattr(rec, "conflict_json", None), {})
        if await _apply_in_place_recalculate(db, rec, fetch_remote=req.fetch_remote):
            updated_count += 1
            new_conflict = _safe_json(getattr(rec, "conflict_json", None), {})
            if prev_status != rec.status or bool(prev_conflict.get("blocking")) != bool(new_conflict.get("blocking")):
                changes.append(
                    {
                        "id": rec.id,
                        "show_name": rec.show_name,
                        "season_number": rec.season_number,
                        "before_status": prev_status,
                        "after_status": rec.status,
                        "detail": rec.status_detail,
                    }
                )
        elif req.fetch_remote:
            ev = _safe_json(rec.evidence_json, {})
            remote_failures.append({"id": rec.id, "error": ev.get("last_remote_recheck_error") or "核查未完成"})

    if target_set is not None and len(target_set) == 1:
        groups = list((await db.scalars(select(DirectoryGroup).where(DirectoryGroup.task_id == task_id))).all())
        dir_groups = await enrich_directory_groups_from_db(
            db, task_id, [serialize_directory_group(g) for g in groups]
        )
    else:
        dir_groups = await _rebuild_offline_directory_groups(db, task_id, all_rows)
    active_after = [r for r in all_rows if r.status not in preserved_statuses]
    after_counts = dict(Counter(r.status for r in active_after))
    task_summary = await refresh_task_summary(task_id, db)
    await db.commit()
    expired_hint = ""
    for r in active_after:
        ev_r = _safe_json(getattr(r, "evidence_json", None), {})
        if ((ev_r.get("broadcast_evidence") or {}) if isinstance(ev_r, dict) else {}).get("schedule_expired_past_today"):
            expired_hint = "；部分剧季旧播期已跨过今日，建议点击「重新初检」更新"
            break
    mode_prefix = "已用上次初检资料重新核算" if not req.fetch_remote else "已完成远程重查与重新核算"
    if remote_failures:
        mode_prefix = f"本次有 {len(remote_failures)} 季核查未完成，已保留原判断与文件"
    return {
        "ok": True,
        "updated_count": updated_count,
        "remote_failures": remote_failures,
        "preserved_handled": preserved_handled,
        "before_counts": before_counts,
        "after_counts": after_counts,
        "changes": changes,
        "directory_groups": dir_groups,
        "summary": task_summary,
        "message": (
            f"{mode_prefix}（重算 {updated_count} 季，状态/冲突变更 {len(changes)} 项，"
            f"保留已处理记录 {preserved_handled} 季，多目录待确认组 {task_summary.get('pending_directory_groups', 0)} 个）{expired_hint}"
        ),
    }


@router.post("/{task_id}/recalculate")
async def recalculate_task(
    task_id: str,
    body: RecalculateRequest | None = Body(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """只读重新核算并按新规则重评估归属提示（保留手动处理、跨条目映射与已推送回执）。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return await _run_task_reevaluate_impl(db, task_id, body or RecalculateRequest())


@router.post("/{task_id}/reevaluate")
async def reevaluate_task_identities(
    task_id: str,
    body: RecalculateRequest | None = Body(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """只读重新评估本任务归属提示（返回前后状态差异与多目录组级索引，不改动网盘与下载队列）。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return await _run_task_reevaluate_impl(db, task_id, body or RecalculateRequest())


class DirectoryGroupConfirmRequest(BaseModel):
    mode: str  # "use_archive_target" | "intentional_coexist"
    target_path: str = ""


@router.get("/{task_id}/directory-groups")
async def list_task_directory_groups(
    task_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """列出任务下检测到的同 TMDB 多目录 / 异名目录组级复核卡。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    groups = list(
        (
            await db.scalars(
                select(DirectoryGroup)
                .where(DirectoryGroup.task_id == task_id)
                .order_by(DirectoryGroup.status, DirectoryGroup.tmdb_id)
            )
        ).all()
    )
    enriched = await enrich_directory_groups_from_db(
        db, task_id, [serialize_directory_group(g) for g in groups]
    )
    return {
        "ok": True,
        "groups": enriched,
    }


@router.post("/{task_id}/directory-groups/{group_id}/confirm")
async def confirm_task_directory_group(
    task_id: str,
    group_id: str,
    body: DirectoryGroupConfirmRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """确认同一 TMDB 多目录/异名目录的目标归档路径或标记有意共存（绝不自动移动或删除文件）。"""
    task = await db.get(Task, task_id)
    group = await db.get(DirectoryGroup, group_id)
    if not task or not group or group.task_id != task_id:
        raise HTTPException(status_code=404, detail="多目录确认组不存在")
    from app.services.task_service import TASK_RUNNING_WORKERS
    if task.status in ("running", "paused", "stopping") or task_id in TASK_RUNNING_WORKERS:
        raise HTTPException(status_code=409, detail="请在初检结束后确认目录组")
    try:
        updated = await confirm_directory_group(
            db, group, mode=body.mode, target_path=body.target_path
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    summary = await refresh_task_summary(task_id, db)
    await db.commit()
    await db.refresh(updated)
    enriched = await enrich_directory_groups_from_db(
        db, task_id, [serialize_directory_group(updated)]
    )
    return {
        "ok": True,
        "group": enriched[0],
        "summary": summary,
        "message": (
            f"已确认 TMDB {updated.tmdb_id} 目标归档目录：{updated.confirmed_target_path}（未自动移动或删除文件）"
            if updated.confirmation_mode == "use_archive_target"
            else f"已将 TMDB {updated.tmdb_id} 的目录组标记为“有意共存”（仍保留覆盖与清理保护）"
        ),
    }


class DirectoryGroupMergePreviewRequest(BaseModel):
    target_path: str = ""
    source_target_path: str = ""
    season_number: int | None = None
    season_numbers: list[int] | None = None
    all_seasons: bool | None = None
    smart_collision: bool = True
    selected_prefix: str = ""


class DirectoryGroupMergeExecuteRequest(BaseModel):
    plan_id: str = ""
    target_path: str = ""
    source_target_path: str = ""
    season_number: int | None = None
    season_numbers: list[int] | None = None
    all_seasons: bool | None = None
    smart_collision: bool = True
    selected_prefix: str = ""


def _resolve_merge_season_scope(body: DirectoryGroupMergePreviewRequest | DirectoryGroupMergeExecuteRequest) -> tuple[int, list[int] | None, bool]:
    if body.season_numbers:
        return int(body.season_numbers[0]), list(body.season_numbers), False
    if body.all_seasons is not None:
        return int(body.season_number or 1), None, bool(body.all_seasons)
    # 兼容旧 M10 单季调用：仅显式传 season_number 且未传 all_seasons 时按单季处理；否则默认全季
    if body.season_number is not None:
        return int(body.season_number), None, False
    return 1, None, True


@router.post("/{task_id}/directory-groups/{group_id}/merge-preview")
async def preview_task_directory_group_merge(
    task_id: str,
    group_id: str,
    body: DirectoryGroupMergePreviewRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """生成同 TMDB 多目录源端合并与前缀统一的只读预览计划 (M01–M14, v2.5.83 支持全季与空壳自动识别)。"""
    task = await db.get(Task, task_id)
    group = await db.get(DirectoryGroup, group_id)
    if not task or not group or group.task_id != task_id:
        raise HTTPException(status_code=404, detail="多目录确认组不存在")
    from app.services.task_service import TASK_RUNNING_WORKERS
    if task.status in ("running", "paused", "stopping") or task_id in TASK_RUNNING_WORKERS:
        raise HTTPException(status_code=409, detail="请在初检结束后合并目录组")

    from app.core.directory_merge import build_directory_merge_plan
    from app.clients.clouddrive2 import CloudDrive2Client
    eff_sn, eff_sns, eff_all = _resolve_merge_season_scope(body)
    cd2 = CloudDrive2Client()
    try:
        plan = await build_directory_merge_plan(
            db,
            task_id,
            group_id,
            target_path=body.target_path,
            source_target_path=body.source_target_path,
            season_number=eff_sn,
            season_numbers=eff_sns,
            all_seasons=eff_all,
            smart_collision=body.smart_collision,
            selected_prefix=body.selected_prefix,
            cd2_client=cd2,
        )
        await db.commit()
        return {"ok": True, "plan": plan}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await cd2.close()


@router.post("/{task_id}/directory-groups/{group_id}/merge-execute")
async def execute_task_directory_group_merge(
    task_id: str,
    group_id: str,
    body: DirectoryGroupMergeExecuteRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """一次确认执行源端目录合并、前缀重命名、旧目录清理与云盘刷新 (M01–M14, v2.5.83)。"""
    task = await db.get(Task, task_id)
    group = await db.get(DirectoryGroup, group_id)
    if not task or not group or group.task_id != task_id:
        raise HTTPException(status_code=404, detail="多目录确认组不存在")
    from app.services.task_service import TASK_RUNNING_WORKERS
    if task.status in ("running", "paused", "stopping") or task_id in TASK_RUNNING_WORKERS:
        raise HTTPException(status_code=409, detail="请在初检结束后合并目录组")

    from app.core.directory_merge import build_directory_merge_plan, execute_directory_merge
    from app.clients.clouddrive2 import CloudDrive2Client
    eff_sn, eff_sns, eff_all = _resolve_merge_season_scope(body)
    cd2 = CloudDrive2Client()
    try:
        grp_ev = json.loads(group.evidence_json or "{}") if getattr(group, "evidence_json", None) else {}
        plan = grp_ev.get("latest_merge_plan")
        if not plan or (body.plan_id and plan.get("plan_id") != body.plan_id) or not plan.get("can_execute"):
            plan = await build_directory_merge_plan(
                db,
                task_id,
                group_id,
                target_path=body.target_path,
                source_target_path=body.source_target_path,
                season_number=eff_sn,
                season_numbers=eff_sns,
                all_seasons=eff_all,
                smart_collision=body.smart_collision,
                selected_prefix=body.selected_prefix,
                cd2_client=cd2,
            )

        res = await execute_directory_merge(
            db,
            task_id,
            group_id,
            plan_id=plan.get("plan_id", ""),
            cd2_client=cd2,
        )
        return res
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await cd2.close()


class DirectoryGroupCleanSeasonRequest(BaseModel):
    season_record_id: str


@router.post("/{task_id}/directory-groups/{group_id}/clean-source-season")
async def clean_directory_group_source_season(
    task_id: str,
    group_id: str,
    body: DirectoryGroupCleanSeasonRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """通过已确认的多目录归档目标，对指定源端单季执行严格覆盖核验与安全清理。"""
    task = await db.get(Task, task_id)
    group = await db.get(DirectoryGroup, group_id)
    if not task or not group or group.task_id != task_id:
        raise HTTPException(status_code=404, detail="多目录确认组不存在")

    rec = await db.get(SeasonRecord, body.season_record_id)
    if not rec or rec.task_id != task_id or str(rec.tmdb_id or '') != str(group.tmdb_id or ''):
        raise HTTPException(status_code=404, detail="源季不属于当前目录组")
    presence = await reconcile_source_presence(task_id, rec.id, db)
    if presence.get('source_absent'):
        return dict(presence, success=True)
    if rec.status in ('already_archived', 'cleaned_redundant'):
        from app.api.archive import _clean_source_redundant_impl, _ArchiveRequest
        result = await _clean_source_redundant_impl(_ArchiveRequest(season_record_ids=[rec.id]), db)
        return dict(ok=result.ok, success=result.ok, message=result.message)

    if group.status != "confirmed" or group.confirmation_mode != "use_archive_target" or not group.confirmed_target_path:
        raise HTTPException(status_code=400, detail="该目录组尚未确认唯一的有效归档目标目录，禁止删除源端")

    if group.invalidated_reason:
        raise HTTPException(status_code=400, detail=f"该目录组确认已失效：{group.invalidated_reason}，请先重新确认")

    rec = await db.get(SeasonRecord, body.season_record_id)
    if not rec or rec.task_id != task_id:
        raise HTTPException(status_code=404, detail="源季记录不存在")

    from app.core.season_classifier import get_record_work_main_category
    if rec.status == "airing" or rec.season_finished is False or (await get_record_work_main_category(db, rec)) == "airing":
        raise HTTPException(status_code=409, detail="当前作品季处于「连载留源」状态，严禁清理源端")

    try:
        entries = json.loads(group.paths_json or "[]")
    except Exception:
        entries = []
    source_paths = {
        normalize_dir_path(e.get("norm_path") or "")
        for e in entries
        if isinstance(e, dict) and e.get("side") == "source"
    }
    if normalize_dir_path(rec.show_path) not in source_paths:
        raise HTTPException(status_code=400, detail="该源季不属于当前目录组的源端路径")

    settings = get_settings()
    norm_source_root = posixpath.normpath(settings.source_root.rstrip("/")) if settings.source_root else ""
    norm_archive_root = posixpath.normpath(settings.archive_root.rstrip("/")) if settings.archive_root else ""
    norm_src = posixpath.normpath(rec.season_path)
    norm_confirmed_target = posixpath.normpath(group.confirmed_target_path)

    # 门禁 1: 路径隔离与根目录范围检查
    if not norm_source_root or not (norm_src.startswith(norm_source_root + "/") and norm_src != norm_source_root):
        raise HTTPException(status_code=400, detail="源季路径非受控源根严格子目录，安全拦截")

    if not norm_archive_root or not (norm_confirmed_target.startswith(norm_archive_root + "/") and norm_confirmed_target != norm_archive_root):
        raise HTTPException(status_code=400, detail="已确认归档目标非受控归档根严格子目录，安全拦截")

    if norm_src == norm_confirmed_target or norm_src.startswith(norm_confirmed_target + "/") or norm_confirmed_target.startswith(norm_src + "/"):
        raise HTTPException(status_code=400, detail="严重危险拦截：源路径与归档目标路径重叠，拒绝删除")

    # 门禁 2: 实盘文件核验
    from app.clients.clouddrive2 import CloudDrive2Client
    from app.core.episode_parser import is_video_file, is_subtitle_file, parse_episode
    from app.core.inspector import _parse_season_number, _verified_video
    from app.core.segments import segment_number

    cd2 = CloudDrive2Client()
    try:
        target_seasons = await cd2.list_files(norm_confirmed_target, force_refresh=True)
        target_season_dirs = [
            it for it in target_seasons
            if it.get("isDirectory") and _parse_season_number(it.get("name") or "") == rec.season_number
        ]
        if not target_season_dirs:
            raise HTTPException(status_code=400, detail=f"已确认归档目标中未找到第 S{rec.season_number:02d} 季目录，无法核实全季覆盖")
        archive_season_path = target_season_dirs[0]["path"]

        src_files = await cd2.list_files(rec.season_path, force_refresh=True)
        target_files = await cd2.list_files(archive_season_path, force_refresh=True)

        if any(f.get("isDirectory") for f in src_files):
            sub_names = [f["name"] for f in src_files if f.get("isDirectory")]
            raise HTTPException(status_code=400, detail=f"源季内存在子目录 ({sub_names})，需要先人工核实，安全拦截")

        src_video_eps: set[int] = set()
        src_subs: list[dict] = []
        for sf in src_files:
            sf_name = sf.get("name", "")
            if is_video_file(sf_name):
                p_sf = parse_episode(sf_name, sf.get("path", ""), sf.get("size", 0))
                if _verified_video(p_sf, rec.season_number) and not segment_number(sf_name):
                    src_video_eps.update(p_sf.episode_numbers)
                else:
                    raise HTTPException(status_code=400, detail=f"源端含无法确认或分段的正片视频 ({sf_name})，安全拦截")
            elif is_subtitle_file(sf_name):
                src_subs.append(sf)

        target_video_eps: set[int] = set()
        target_sub_sizes = {}
        for tf in target_files:
            if tf.get("isDirectory"):
                continue
            tf_name = tf.get("name", "")
            if is_video_file(tf_name):
                p_tf = parse_episode(tf_name, tf.get("path", ""), tf.get("size", 0))
                if _verified_video(p_tf, rec.season_number) and not segment_number(tf_name):
                    target_video_eps.update(p_tf.episode_numbers)
            elif is_subtitle_file(tf_name):
                target_sub_sizes[tf_name.casefold()] = tf.get("size")

        tot = int(rec.total_episodes or 0)
        if tot <= 0:
            raise HTTPException(status_code=400, detail="该季官方总集数未知，无法断定归档端单独齐全")
        expected_all = set(range(1, tot + 1))
        if not (expected_all <= target_video_eps):
            missing_in_target = sorted(expected_all - target_video_eps)
            raise HTTPException(status_code=400, detail=f"已确认归档目标未全季齐全（缺少集数: {missing_in_target}），不满足删除条件")

        if src_video_eps and not (src_video_eps <= target_video_eps):
            uncovered = sorted(src_video_eps - target_video_eps)
            raise HTTPException(status_code=400, detail=f"源端含有归档目标未包含的集数 {uncovered}，拒绝删除")

        for sub in src_subs:
            s_name = sub.get("name", "")
            if s_name.casefold() not in target_sub_sizes:
                try:
                    await cd2.move_file(sub.get("path"), archive_season_path, skip_existing=True)
                except Exception:
                    pass

        try:
            del_res = await cd2.delete_file(rec.season_path)
            if isinstance(del_res, dict) and not del_res.get("success", True):
                raise HTTPException(status_code=500, detail=f"网盘删除源季目录失败: {del_res.get('message') or '未知错误'}")
            elif del_res is False:
                raise HTTPException(status_code=500, detail="网盘删除源季目录失败")
        except HTTPException:
            raise
        except Exception as del_err:
            raise HTTPException(status_code=500, detail=f"网盘删除源季目录失败: {del_err}")

        op = OperationLog(
            task_id=task_id,
            season_record_id=rec.id,
            action="clean_source_redundant",
            status="completed",
            detail_json=json.dumps({
                "mode": "directory_group_verified_clean",
                "show": rec.show_name,
                "season": rec.season_number,
                "source_path": rec.season_path,
                "archive_target_path": archive_season_path,
                "deleted_video_count": len(src_video_eps),
                "target_video_count": len(target_video_eps),
            }, ensure_ascii=False),
        )
        db.add(op)
        rec.status = "cleaned_redundant"
        rec.status_detail = f"已通过多目录归档覆盖核验清理源端残留（已确认归档目标 {len(target_video_eps)}/{tot} 全季齐全）"
        summary = await refresh_task_summary(task_id, db)
        await db.commit()

        reloaded_group = await db.get(DirectoryGroup, group_id)
        enriched = await enrich_directory_groups_from_db(
            db, task_id, [serialize_directory_group(reloaded_group)] if reloaded_group else []
        )
        return {
            "ok": True,
            "success": True,
            "message": f"已成功安全清理源端 {rec.show_name} S{rec.season_number:02d} 季目录",
            "group": enriched[0] if enriched else None,
            "summary": summary,
        }
    finally:
        await cd2.close()


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
    force_release_missing: bool = False


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
    from app.services.task_service import TASK_RUNNING_WORKERS
    if task.status in ("running", "paused", "stopping") or task_id in TASK_RUNNING_WORKERS:
        raise HTTPException(status_code=409, detail="请在初检结束后核对入库并复核")
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
            f"退回待补 {reconcile_summary['released_to_todo']} 集；"
            f"完成补缺后复核 {reconcile_summary['post_review_completed']} 季，"
            f"复核待完成 {reconcile_summary['post_review_pending']} 季（暂不归档或重推）"
        ),
    }


_RECONCILE_JOBS: dict[str, dict[str, Any]] = {}
_RECONCILE_TASK_JOBS: dict[str, str] = {}


@router.post("/{task_id}/reconcile-gap-fill-start")
async def start_reconcile_gap_fill(
    task_id: str,
    body: ReconcileGapFillRequest | None = Body(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """异步启动“核对入库”，立即返回 job_id 供前端轮询进度。"""
    import asyncio
    from app.database import async_session
    from app.services.task_service import TASK_RUNNING_WORKERS

    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status in ("running", "paused", "stopping") or task_id in TASK_RUNNING_WORKERS:
        raise HTTPException(status_code=409, detail="请在初检结束后核对入库并复核")

    existing_job_id = _RECONCILE_TASK_JOBS.get(task_id)
    if existing_job_id and existing_job_id in _RECONCILE_JOBS:
        existing = _RECONCILE_JOBS[existing_job_id]
        if existing.get("status") == "running":
            return {"ok": True, **existing}

    req = body or ReconcileGapFillRequest()
    job_id = uuid.uuid4().hex[:12]
    job_state: dict[str, Any] = {
        "job_id": job_id,
        "task_id": task_id,
        "status": "running",
        "stage": "正在启动核对入库任务...",
        "current": 0,
        "total": 0,
        "current_show": "",
        "percent": 2,
        "message": "",
        "reconcile": None,
        "summary": None,
        "error": None,
    }
    _RECONCILE_JOBS[job_id] = job_state
    _RECONCILE_TASK_JOBS[task_id] = job_id

    def _on_progress(prog: dict[str, Any]) -> None:
        job_state.update({
            "stage": prog.get("stage") or job_state["stage"],
            "current": prog.get("current", 0),
            "total": prog.get("total", 0),
            "current_show": prog.get("current_show", ""),
            "percent": prog.get("percent", job_state["percent"]),
        })

    async def _runner() -> None:
        try:
            async with async_session() as bg_db:
                reconcile_summary = await reconcile_gap_fill_records(
                    db=bg_db,
                    task_id=task_id,
                    season_record_ids=req.season_record_ids or None,
                    force_release_missing=req.force_release_missing,
                    progress_cb=_on_progress,
                )
                task_summary = await refresh_task_summary(task_id, bg_db)
                await bg_db.commit()
                msg = (
                    f"核对入库完成：检查 {reconcile_summary['checked_seasons']} 季，"
                    f"确认入库 {reconcile_summary['reconciled_in_library']} 集，"
                    f"仍在队列/待整理 {reconcile_summary['still_pending']} 集，"
                    f"退回待补 {reconcile_summary['released_to_todo']} 集；"
                    f"完成补缺后复核 {reconcile_summary['post_review_completed']} 季，"
                    f"复核待完成 {reconcile_summary['post_review_pending']} 季（暂不归档或重推）"
                )
                job_state.update({
                    "status": "completed",
                    "stage": "核对入库完成",
                    "percent": 100,
                    "reconcile": reconcile_summary,
                    "summary": task_summary,
                    "message": msg,
                })
        except Exception as exc:
            job_state.update({
                "status": "failed",
                "stage": f"核对入库失败: {exc}",
                "error": str(exc),
                "message": f"核对入库失败: {exc}",
            })

    asyncio.create_task(_runner())
    return {"ok": True, **job_state}


@router.get("/{task_id}/reconcile-gap-fill-status/{job_id}")
async def get_reconcile_gap_fill_status(task_id: str, job_id: str) -> dict[str, Any]:
    """查询“核对入库”后台任务实时进度。"""
    job = _RECONCILE_JOBS.get(job_id)
    if not job or job.get("task_id") != task_id:
        raise HTTPException(status_code=404, detail="核对任务不存在或已过期")
    return {"ok": True, **job}


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
    from app.core.season_classifier import get_record_work_context
    work = await get_record_work_context(db, rec)
    work_cat = work.get("main_category")
    if work.get("airing_protected"):
        raise HTTPException(status_code=409, detail="连载留源剧季无需且不可标记为手动处理")
    recognized = json.loads(rec.recognized_episodes_json or "[]")
    is_anomaly = (
        work_cat == "anomaly"
        or rec.status in ("already_archived", "unknown", "archive_check", "period_review", "identity_review")
        or (not rec.total_episodes and rec.status != "airing")
        or (rec.status not in ("archive_ready", "airing") and bool(rec.total_episodes) and max(recognized, default=0) > rec.total_episodes)
    )
    if not is_anomaly:
        raise HTTPException(status_code=409, detail="仅异常剧集可标记为手动处理")
    part_ids = list(work.get("participating_record_ids") or [record_id])
    if record_id not in part_ids:
        part_ids.append(record_id)
    previous = rec.status
    changed = await db.execute(update(SeasonRecord).where(
        SeasonRecord.id.in_(part_ids), SeasonRecord.task_id == task_id
    ).values(status="manually_handled"))
    if not changed.rowcount:
        await db.refresh(rec)
        if rec.status != "manually_handled":
            raise HTTPException(status_code=409, detail="条目状态已更新，请刷新清单")
    if changed.rowcount:
        part_rows = list((await db.scalars(select(SeasonRecord).where(SeasonRecord.id.in_(part_ids)))).all())
        for pr in part_rows:
            db.add(OperationLog(
                task_id=task_id, season_record_id=pr.id, action="manually_handled", status="completed",
                detail_json=json.dumps({"season_path": pr.season_path, "show_name": pr.show_name,
                                        "season_number": pr.season_number, "previous_status": previous}, ensure_ascii=False),
            ))
    await db.refresh(rec)
    from app.api.archive import _refresh_task_summary
    await _refresh_task_summary(task_id, db)
    await db.commit()
    return StatusResponse(ok=True, message="已标记为手动处理，并从任务清单中移除")


class ManualConfirmRequest(BaseModel):
    confirm_current_identity: bool = False
    target_tmdb_id: int | None = None
    target_season: int | None = None
    target_title: str = ""
    target_first_air_year: str = ""
    douban_url: str = ""
    douban_id: str = ""
    episode_mapping: dict[str, int] | None = None
    apply_file_changes: bool = True
    correction_fingerprint: str = ""
    total_episodes: int | None = None
    broadcast_status: str = "unknown"  # "finished" | "airing" | "unknown"
    note: str = ""


class HoldSeasonRequest(BaseModel):
    reason: str = ""


class RefreshModifiedRequest(BaseModel):
    record_ids: list[str] = []
    paths: list[str] = []


async def _collect_work_season_records(db: AsyncSession, rec: SeasonRecord) -> tuple[list[SeasonRecord], dict[str, Any]]:
    from app.core.season_classifier import get_record_work_context
    work = await get_record_work_context(db, rec)
    part_ids = list(work.get("participating_record_ids") or [rec.id])
    if rec.id not in part_ids:
        part_ids.append(rec.id)
    rows = list(
        (
            await db.scalars(
                select(SeasonRecord).where(
                    SeasonRecord.task_id == rec.task_id,
                    SeasonRecord.id.in_(part_ids),
                )
            )
        ).all()
    )
    return (rows or [rec]), work


async def _build_correction_plan_for_record(
    db: AsyncSession,
    rec: SeasonRecord,
    body: ManualConfirmRequest,
    cd2: Any = None,
) -> tuple[dict[str, Any], str | None]:
    import re
    from app.core.directory_merge import list_files_robust
    from app.core.episode_parser import extract_douban_subject_id
    from app.core.manual_confirmation import build_manual_correction_plan

    douban_sid = extract_douban_subject_id(body.douban_url) or extract_douban_subject_id(body.douban_id)

    plan_args = dict(
        show_name=rec.show_name, show_path=rec.show_path or "", season_path=rec.season_path or "",
        current_tmdb_id=rec.tmdb_id, current_season=rec.season_number,
        target_tmdb_id=body.target_tmdb_id, target_title=body.target_title,
        target_first_air_year=body.target_first_air_year, target_season=body.target_season,
        douban_id=douban_sid, episode_mapping=body.episode_mapping,
    )
    draft = build_manual_correction_plan(**plan_args)
    if not (draft.get("identity_changed") or draft.get("season_changed") or body.episode_mapping):
        # 纯事实确认不访问云盘，避免把云盘响应延迟混入保存资料。
        return draft, douban_sid

    from app.clients.clouddrive2 import CloudDrive2Client
    owned_client = cd2 is None
    if owned_client:
        cd2 = CloudDrive2Client()
    try:
        # 两处目录都必须读取成功。读取失败不能冒充“无文件变更”。
        root_entries = await list_files_robust(cd2, rec.show_path)
        season_files = await list_files_robust(cd2, rec.season_path)
        db_siblings = list((await db.scalars(select(SeasonRecord).where(
            SeasonRecord.task_id == rec.task_id, SeasonRecord.show_path == rec.show_path,
            SeasonRecord.season_number != rec.season_number,
        ))).all())
        sibling_seasons = {f"Season {sib.season_number}" for sib in db_siblings}
        for entry in root_entries:
            match = re.match(r"(?i)^(?:Season\s*|S)(\d{1,2})$", entry.get("name", ""))
            if entry.get("isDirectory") and match and int(match.group(1)) != int(rec.season_number):
                sibling_seasons.add(entry["name"])
        plan = build_manual_correction_plan(**plan_args, season_files=season_files,
                                            sibling_seasons=sorted(sibling_seasons))
        inventory = sorted((
            {k: entry.get(k) for k in ("path", "name", "isDirectory", "size", "id", "modified")}
            for entry in season_files
        ), key=lambda entry: str(entry.get("path") or entry.get("name")))
        basis = {k: v for k, v in plan.items() if k != "plan_id"}
        basis["source_inventory"] = inventory
        plan["correction_fingerprint"] = hashlib.sha256(
            json.dumps(basis, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
        return plan, douban_sid
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="无法读取修正范围，请稍后重新预览；未执行文件修改") from exc
    finally:
        if owned_client:
            await cd2.close()


@router.post("/{task_id}/seasons/{record_id}/manual-confirm/preview")
async def preview_manual_season_confirmation(
    task_id: str,
    record_id: str,
    body: ManualConfirmRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """预览人工确认与单季实物修正计划（展示新剧名、目标目录、文件名变化和预计模块，不写入）。"""
    task = await db.get(Task, task_id)
    rec = await db.get(SeasonRecord, record_id)
    if not task or not rec or rec.task_id != task_id:
        raise HTTPException(status_code=404, detail="剧季记录不存在")

    if body.total_episodes is not None and body.total_episodes <= 0:
        raise HTTPException(status_code=400, detail="本季总集数必须为大于 0 的正整数或留空表示尚不清楚")

    part_rows, work = await _collect_work_season_records(db, rec)
    scope_paths = [r.season_path for r in part_rows if r.season_path]
    union_rec: set[int] = set()
    union_arc: set[int] = set()
    union_emby: set[int] = set()
    union_dups: set[int] = set()
    pending_dg = False
    for r in part_rows:
        union_rec.update(int(x) for x in _safe_json(r.recognized_episodes_json, []) if str(x).isdigit() and int(x) > 0)
        union_dups.update(int(x) for x in _safe_json(r.duplicate_episodes_json, []) if str(x).isdigit() and int(x) > 0)
        ev_r = _safe_json(getattr(r, "evidence_json", None), {})
        dual_r = (ev_r.get("dual_archive_info") or {}) if isinstance(ev_r, dict) else {}
        union_arc.update(int(x) for x in (dual_r.get("archived_episodes") or []) if str(x).isdigit() and int(x) > 0)
        ms_r = (ev_r.get("mediaserver_check") or {}) if isinstance(ev_r, dict) else {}
        if ms_r.get("trusted_match") is not False:
            union_emby.update(int(x) for x in (ms_r.get("season_episodes") or []) if str(x).isdigit() and int(x) > 0)
        if ((ev_r.get("directory_group") or {}) if isinstance(ev_r, dict) else {}).get("requires_confirm"):
            pending_dg = True

    eff_tid = body.target_tmdb_id or (int(rec.tmdb_id) if rec.tmdb_id and str(rec.tmdb_id).isdigit() else None)
    eff_sn = body.target_season or rec.season_number
    draft_conf = {
        "identity_confirmed": bool(body.confirm_current_identity or body.target_tmdb_id),
        "confirmed_tmdb_id": eff_tid,
        "confirmed_season_number": eff_sn,
        "confirmed_total_episodes": body.total_episodes,
        "broadcast_status": body.broadcast_status or "unknown",
        "note": body.note,
    }
    outcome = evaluate_confirmation_outcome(
        conf=draft_conf,
        tmdb_id=eff_tid,
        season_number=eff_sn,
        recognized_episodes=sorted(union_rec),
        archived_episodes=sorted(union_arc),
        emby_episodes=sorted(union_emby),
        duplicate_episodes=sorted(union_dups),
        current_total_episodes=rec.total_episodes,
        current_season_finished=rec.season_finished,
        current_status=rec.status,
        current_status_detail=rec.status_detail or "",
        evidence=_safe_json(getattr(rec, "evidence_json", None), {}),
        conflict=_safe_json(getattr(rec, "conflict_json", None), {}),
        pending_dir_group=pending_dg,
    )

    correction_plan, douban_sid = await _build_correction_plan_for_record(db, rec, body, cd2=None)

    return {
        "ok": True,
        "scope_paths": scope_paths,
        "recognized_episodes": sorted(union_rec),
        "archived_episodes": sorted(union_arc),
        "emby_episodes": sorted(union_emby),
        "douban_id": douban_sid or "",
        "correction_plan": correction_plan,
        "predicted_category": outcome["predicted_category"],
        "predicted_category_label": outcome["predicted_category_label"],
        "predicted_status": outcome["status"],
        "predicted_detail": outcome["status_detail"],
        "blocking_reason": outcome["blocking_reason"],
        "contradiction": outcome["contradiction"],
    }


# 单进程内的进行中互斥与只读进度；不充当跨进程锁或断点续做凭证。
_MANUAL_SAVE_PROGRESS: dict[tuple[str, str], dict[str, Any]] = {}


@router.get("/{task_id}/seasons/{record_id}/manual-confirm/progress")
async def manual_confirmation_progress(task_id: str, record_id: str) -> dict[str, Any]:
    state = _MANUAL_SAVE_PROGRESS.get((task_id, record_id))
    if not state:
        return {"running": False, "known": False}
    return {**{k: v for k, v in state.items() if k not in ("started", "ended")}, "known": True,
            "elapsed_seconds": round(state.get("ended", time.monotonic()) - state["started"], 1)}


@router.post("/{task_id}/seasons/{record_id}/manual-confirm")
async def save_manual_season_confirmation(
    task_id: str, record_id: str, body: ManualConfirmRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    key = (task_id, record_id)
    if _MANUAL_SAVE_PROGRESS.get(key, {}).get("running"):
        raise HTTPException(status_code=409, detail="本条目正在保存，请等待回执，不要重复提交")
    for previous in list(_MANUAL_SAVE_PROGRESS):
        if len(_MANUAL_SAVE_PROGRESS) < 128:
            break
        if not _MANUAL_SAVE_PROGRESS[previous].get("running"):
            del _MANUAL_SAVE_PROGRESS[previous]
    state = {"running": True, "started": time.monotonic(), "stage": "正在核对确认范围",
             "completed": 0, "total": 0}
    _MANUAL_SAVE_PROGRESS[key] = state
    try:
        result = await _save_manual_season_confirmation(task_id, record_id, body, db)
        state["stage"] = "保存完成" if result.get("ok") else "修正未完成，请核查回执"
        state["ok"] = bool(result.get("ok"))
        return result
    except Exception:
        state.update(stage="保存未完成，请核查结果后再操作", ok=False)
        raise
    finally:
        state["running"] = False
        state["ended"] = time.monotonic()
        log.info("人工确认保存结束：耗时 %.2f 秒，阶段=%s，文件进度=%s/%s",
                 time.monotonic() - state["started"], state["stage"], state["completed"], state["total"])


async def _save_manual_season_confirmation(
    task_id: str,
    record_id: str,
    body: ManualConfirmRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """保存单季人工确认事实；若有身份/季号/映射修正则同步执行实物迁移改名、NFO与台账迁移并核验分流 (G17-G19)。"""
    from app.core.manual_confirmation import execute_manual_correction_plan
    from app.models.gap_fill import GapFillEpisode, GapFillSeasonRun
    from app.services.gap_fill_tracker import build_identity_key

    task = await db.get(Task, task_id)
    rec = await db.get(SeasonRecord, record_id)
    if not task or not rec or rec.task_id != task_id:
        raise HTTPException(status_code=404, detail="剧季记录不存在")

    from app.services.task_service import TASK_RUNNING_WORKERS
    if task.status in ("running", "paused", "stopping") or task_id in TASK_RUNNING_WORKERS:
        raise HTTPException(status_code=409, detail="请在初检结束后保存人工确认")

    if body.total_episodes is not None and body.total_episodes <= 0:
        raise HTTPException(status_code=400, detail="本季总集数必须为大于 0 的正整数或留空表示尚不清楚")

    from app.core.episode_parser import extract_douban_subject_id
    douban_sid = extract_douban_subject_id(body.douban_url) or extract_douban_subject_id(body.douban_id)

    has_any_choice = bool(
        body.confirm_current_identity
        or (body.target_tmdb_id and body.target_tmdb_id > 0)
        or (body.target_season and int(body.target_season) != int(rec.season_number))
        or (body.total_episodes is not None and body.total_episodes > 0)
        or body.broadcast_status in ("finished", "airing")
        or bool(douban_sid)
        or bool(body.episode_mapping)
    )
    if not has_any_choice:
        raise HTTPException(
            status_code=400,
            detail="请至少明确确认一项事实（如当前作品与季号正确、豆瓣条目、本季总集数或播出状态），全未选择不能作为有效确认提交",
        )

    part_rows, _ = await _collect_work_season_records(db, rec)
    scope_paths = [r.season_path for r in part_rows if r.season_path]
    union_rec = sorted({
        int(x)
        for r in part_rows
        for x in _safe_json(r.recognized_episodes_json, [])
        if str(x).isdigit() and int(x) > 0
    })

    # 1. 生成并可选执行单季实物修正计划（仅在身份/名称/季号/集号映射变更且 apply_file_changes=True 时写文件 · G17/G18/G19）
    old_season_path = rec.season_path
    old_show_path = rec.show_path
    old_identity_key = build_identity_key(rec.show_name, rec.tmdb_id, rec.season_number)
    correction_receipt: dict[str, Any] = {"has_file_changes": False, "ok": True}

    from app.clients.clouddrive2 import CloudDrive2Client

    cd2_fix: CloudDrive2Client | None = None
    needs_fs_check = bool(
        body.apply_file_changes
        and (
            (body.target_tmdb_id and str(body.target_tmdb_id) != str(rec.tmdb_id or ""))
            or bool((body.target_title or "").strip())
            or (body.target_season and int(body.target_season) != int(rec.season_number or 0))
            or bool(body.episode_mapping)
        )
    )
    if needs_fs_check:
        cd2_fix = CloudDrive2Client()
    try:
        state = _MANUAL_SAVE_PROGRESS.get((task_id, record_id))
        if state is not None:
            state["stage"] = "正在核验文件修正范围" if needs_fs_check else "正在保存资料确认"
        correction_plan, _ = await _build_correction_plan_for_record(db, rec, body, cd2=cd2_fix)
        if body.apply_file_changes and correction_plan.get("has_file_changes"):
            if not body.correction_fingerprint or body.correction_fingerprint != correction_plan.get("correction_fingerprint"):
                raise HTTPException(status_code=409, detail="文件修正范围尚未确认或已经变化，请重新预览后确认")
            def report_progress(stage: str, completed: int, total: int) -> None:
                state = _MANUAL_SAVE_PROGRESS.get((task_id, record_id))
                if state is not None:
                    state.update(stage=stage, completed=completed, total=total)
            exec_res = await execute_manual_correction_plan(correction_plan, cd2_client=cd2_fix,
                                                            progress=report_progress)
            correction_receipt = {**correction_plan, **exec_res}
            if not exec_res.get("ok"):
                # 部分失败：记录已完成与剩余步骤，保持「修正未完成」，绝不假装成功
                ev_fail = _safe_json(getattr(rec, "evidence_json", None), {})
                if not isinstance(ev_fail, dict):
                    ev_fail = {}
                ev_fail["correction_state"] = {
                    "plan_id": correction_plan.get("plan_id"),
                    "completed_steps": exec_res.get("completed_steps") or [],
                    "remaining_steps": exec_res.get("remaining_steps") or [],
                    "errors": exec_res.get("errors") or [],
                }
                rec.status = "correction_incomplete"
                rec.status_detail = f"修正未完成：已完成 {len(exec_res.get('completed_steps') or [])} 步，剩余 {len(exec_res.get('remaining_steps') or [])} 步待恢复"
                rec.evidence_json = json.dumps(ev_fail, ensure_ascii=False)
                await refresh_task_summary(task_id, db)
                await db.commit()
                return {
                    "ok": False,
                    "partial_failure": True,
                    "correction_receipt": correction_receipt,
                    "message": rec.status_detail,
                }

            # 实物修正核验通过：更新记录路径、身份与季号，并迁移关联补缺台账/回执 (G17, G18)
            rec.show_name = correction_plan["new_show_name"]
            rec.show_path = correction_plan["new_show_path"]
            rec.season_path = correction_plan["new_season_path"]
            rec.season_number = int(correction_plan["new_season_number"])
            if correction_plan.get("new_tmdb_id"):
                rec.tmdb_id = str(correction_plan["new_tmdb_id"])

            new_identity_key = build_identity_key(rec.show_name, rec.tmdb_id, rec.season_number)
            # 迁移补缺分集台账与批次运行记录，保留原身份关联以防重发已受理下载
            if old_season_path:
                gf_eps = list((await db.scalars(
                    select(GapFillEpisode).where(
                        GapFillEpisode.task_id == task_id,
                        GapFillEpisode.season_path == old_season_path.rstrip("/"),
                    )
                )).all())
                for gfe in gf_eps:
                    gfe.season_path = rec.season_path.rstrip("/")
                    gfe.season_number = rec.season_number
                    gfe.show_name = rec.show_name
                    gfe.tmdb_id = rec.tmdb_id
                    if new_identity_key != old_identity_key:
                        gfe.identity_key = new_identity_key
                        gfe.reason = f"{gfe.reason or ''} [原身份:{old_identity_key}]".strip()

                gf_runs = list((await db.scalars(
                    select(GapFillSeasonRun).where(
                        GapFillSeasonRun.task_id == task_id,
                        GapFillSeasonRun.season_path == old_season_path.rstrip("/"),
                    )
                )).all())
                for gfr in gf_runs:
                    gfr.season_path = rec.season_path.rstrip("/")
                    gfr.season_number = rec.season_number
                    gfr.show_name = rec.show_name
                    gfr.tmdb_id = rec.tmdb_id
                    if new_identity_key != old_identity_key:
                        gfr.identity_key = new_identity_key

            # 更新 scope_paths 指向新路径
            scope_paths = [rec.season_path if p == old_season_path else p for p in scope_paths]
    finally:
        if cd2_fix is not None:
            await cd2_fix.close()

    state = _MANUAL_SAVE_PROGRESS.get((task_id, record_id))
    if state is not None:
        state["stage"] = "正在保存确认并更新分流"

    # 若用户在人工确认中修正了目标 TMDB ID 或季号（跨条目映射），同步写入 SeasonMapping
    cur_tid_int = int(rec.tmdb_id) if rec.tmdb_id and str(rec.tmdb_id).isdigit() else None
    is_remapped = bool(
        (body.target_tmdb_id and body.target_tmdb_id != cur_tid_int)
        or (body.target_season and int(body.target_season) != int(rec.season_number))
        or bool(douban_sid)
    )
    if is_remapped and (body.target_tmdb_id or douban_sid):
        for r in part_rows:
            old_maps = (
                await db.scalars(
                    select(SeasonMapping).where(
                        SeasonMapping.scope_key == r.season_path,
                        SeasonMapping.is_active.is_(True),
                    )
                )
            ).all()
            for om in old_maps:
                om.is_active = False
            db.add(
                SeasonMapping(
                    scope_key=r.season_path,
                    source_tmdb_id=cur_tid_int,
                    source_season=r.season_number,
                    show_path=r.show_path,
                    season_path=r.season_path,
                    mapping_kind="manual_confirm_remap",
                    target_tmdb_id=int(body.target_tmdb_id or cur_tid_int or 0) or None,
                    target_douban_id=douban_sid or "",
                    target_season=int(body.target_season or r.season_number),
                    target_title=str(body.target_title or "").strip(),
                    expected_total_episodes=body.total_episodes,
                    evidence_note=str(body.note or "").strip(),
                    created_by="user",
                    is_active=True,
                )
            )
        await db.flush()

    eff_tid = body.target_tmdb_id or cur_tid_int
    eff_sn = int(body.target_season or rec.season_number)

    # 停用同作用范围的旧 ManualSeasonConfirmation
    lookup_paths = list(set(scope_paths + ([old_season_path] if old_season_path else [])))
    old_confs = (
        await db.scalars(
            select(ManualSeasonConfirmation).where(
                ManualSeasonConfirmation.is_active.is_(True),
                ManualSeasonConfirmation.season_path.in_(lookup_paths),
            )
        )
    ).all()
    for oc in old_confs:
        oc.is_active = False

    new_conf = ManualSeasonConfirmation(
        scope_key=rec.season_path,
        task_id=task_id,
        season_record_id=rec.id,
        show_path=rec.show_path,
        season_path=rec.season_path,
        scope_paths_json=json.dumps(scope_paths, ensure_ascii=False),
        show_name=rec.show_name,
        tmdb_id=str(rec.tmdb_id or ""),
        season_number=rec.season_number,
        identity_confirmed=bool(body.confirm_current_identity or body.target_tmdb_id or douban_sid),
        confirmed_tmdb_id=eff_tid,
        confirmed_season_number=eff_sn,
        confirmed_total_episodes=body.total_episodes,
        broadcast_status=body.broadcast_status if body.broadcast_status in ("finished", "airing") else "unknown",
        note=str(body.note or "").strip(),
        source="manual_confirmation",
        recognized_snapshot_json=json.dumps(union_rec),
        is_active=True,
    )
    db.add(new_conf)
    await db.flush()

    # 若此前同卡合并了多个目录的集数，对主记录与关联记录同步更新
    for r in part_rows:
        ev_r = _safe_json(getattr(r, "evidence_json", None), {})
        if isinstance(ev_r, dict):
            ev_r.pop("hold_for_task", None)
            ev_r.pop("correction_state", None)
            if douban_sid:
                ev_r["douban_binding"] = {"subject_id": douban_sid, "season_number": eff_sn}
            if correction_receipt.get("has_file_changes"):
                ev_r["last_correction_receipt"] = {
                    "plan_id": correction_receipt.get("plan_id"),
                    "old_show_path": old_show_path,
                    "old_season_path": old_season_path,
                    "new_show_path": rec.show_path,
                    "new_season_path": rec.season_path,
                    "moved_count": len(correction_receipt.get("completed_steps") or []),
                    "updated_nfos": correction_receipt.get("updated_nfos") or [],
                }
            r.evidence_json = json.dumps(ev_r, ensure_ascii=False)
        await _apply_in_place_recalculate(db, r, fetch_remote=False)

    task_summary = await refresh_task_summary(task_id, db)
    await db.commit()
    await db.refresh(rec)

    from app.core.season_classifier import get_record_work_context
    updated_work = await get_record_work_context(db, rec)
    cat_key = str(updated_work.get("main_category") or "anomaly")
    from app.core.manual_confirmation import CATEGORY_LABELS
    cat_label = CATEGORY_LABELS.get(cat_key, cat_key)

    recorded_parts: list[str] = []
    if new_conf.identity_confirmed:
        recorded_parts.append(f"作品归属 TMDB:{eff_tid or '原目录'} S{eff_sn:02d}")
    if douban_sid:
        recorded_parts.append(f"豆瓣:{douban_sid}")
    if new_conf.confirmed_total_episodes:
        recorded_parts.append(f"本季共 {new_conf.confirmed_total_episodes} 集")
    if new_conf.broadcast_status == "finished":
        recorded_parts.append("已播毕")
    elif new_conf.broadcast_status == "airing":
        recorded_parts.append("仍在连载")
    if correction_receipt.get("has_file_changes"):
        recorded_parts.append(f"已迁移改名 {len(correction_receipt.get('completed_steps') or [])} 个文件")

    return {
        "ok": True,
        "confirmation": orm_confirmation_to_dict(new_conf),
        "correction_receipt": correction_receipt,
        "season": updated_work,
        "summary": task_summary,
        "target_category": cat_key,
        "target_category_label": cat_label,
        "message": f"已保存人工确认（{' · '.join(recorded_parts)}），当前条目已更新至「{cat_label}」",
    }


@router.delete("/{task_id}/seasons/{record_id}/manual-confirm")
async def revoke_manual_season_confirmation(
    task_id: str,
    record_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """撤销单季人工确认，按当前可用证据重新计算分流（不删除文件）。"""
    task = await db.get(Task, task_id)
    rec = await db.get(SeasonRecord, record_id)
    if not task or not rec or rec.task_id != task_id:
        raise HTTPException(status_code=404, detail="剧季记录不存在")

    from app.services.task_service import TASK_RUNNING_WORKERS
    if task.status in ("running", "paused", "stopping") or task_id in TASK_RUNNING_WORKERS:
        raise HTTPException(status_code=409, detail="请在初检结束后撤销人工确认")

    part_rows, _ = await _collect_work_season_records(db, rec)
    scope_paths = [r.season_path for r in part_rows if r.season_path]

    confs = (
        await db.scalars(
            select(ManualSeasonConfirmation).where(
                ManualSeasonConfirmation.is_active.is_(True),
                ManualSeasonConfirmation.season_path.in_(scope_paths),
            )
        )
    ).all()
    for c in confs:
        c.is_active = False

    # 同时停用由 manual_confirm_remap 写入的单季映射
    remap_rows = (
        await db.scalars(
            select(SeasonMapping).where(
                SeasonMapping.scope_key.in_(scope_paths),
                SeasonMapping.mapping_kind == "manual_confirm_remap",
                SeasonMapping.is_active.is_(True),
            )
        )
    ).all()
    for rm in remap_rows:
        rm.is_active = False
    await db.flush()

    for r in part_rows:
        ev_r = _safe_json(getattr(r, "evidence_json", None), {})
        if isinstance(ev_r, dict):
            ev_r.pop("manual_confirmation", None)
            if ev_r.get("meta_status") in ("manual_confirmed", "total_episodes_pending"):
                ref_cands = unverified_metadata_candidates(ev_r, r.tmdb_id, r.season_number)
                ev_r["meta_status"] = "metadata_unverified" if ref_cands else "no_candidate"
                r.total_episodes = None
                r.season_finished = False
            b_ev_r = ev_r.get("broadcast_evidence")
            if isinstance(b_ev_r, dict) and b_ev_r.get("source") == "manual_confirmation":
                b_ev_r["status"] = "evidence_pending"
                b_ev_r["schedule_verified"] = False
            r.evidence_json = json.dumps(ev_r, ensure_ascii=False)
        await _apply_in_place_recalculate(db, r, fetch_remote=False)

    task_summary = await refresh_task_summary(task_id, db)
    await db.commit()
    from app.core.season_classifier import get_record_work_context
    updated_work = await get_record_work_context(db, rec)
    return {
        "ok": True,
        "revoked_count": len(confs),
        "season": updated_work,
        "summary": task_summary,
        "message": f"已撤销 {rec.show_name} S{rec.season_number:02d} 的人工确认，已按当前可用证据重新分流",
    }


@router.post("/{task_id}/seasons/{record_id}/hold")
async def hold_season_for_task(
    task_id: str,
    record_id: str,
    body: HoldSeasonRequest | None = Body(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """暂不处理，不再提示：移出本轮五模块待办计数，保留原问题与原状态，已扫描总量不变。"""
    task = await db.get(Task, task_id)
    rec = await db.get(SeasonRecord, record_id)
    if not task or not rec or rec.task_id != task_id:
        raise HTTPException(status_code=404, detail="剧季记录不存在")

    from app.services.task_service import TASK_RUNNING_WORKERS
    if task.status in ("running", "paused", "stopping") or task_id in TASK_RUNNING_WORKERS:
        raise HTTPException(status_code=409, detail="请在初检结束后操作暂不处理")

    part_rows, work = await _collect_work_season_records(db, rec)
    scope_paths = [r.season_path for r in part_rows if r.season_path]
    now_iso = datetime.now(timezone.utc).isoformat()
    req_reason = (body.reason if body else "") or rec.status_detail or "本轮暂不处理，安全保留原样"

    hold_payload = {
        "active": True,
        "task_id": task_id,
        "held_at": now_iso,
        "reason": req_reason,
        "original_status": rec.status,
        "original_detail": rec.status_detail,
        "original_sub_category": work.get("anomaly_sub_category") or "pending_evidence",
        "scope_paths": scope_paths,
    }
    for r in part_rows:
        ev_r = _safe_json(getattr(r, "evidence_json", None), {})
        if not isinstance(ev_r, dict):
            ev_r = {}
        ev_r["hold_for_task"] = hold_payload
        r.evidence_json = json.dumps(ev_r, ensure_ascii=False)

    task_summary = await refresh_task_summary(task_id, db)
    await db.commit()
    return {
        "ok": True,
        "hold_info": hold_payload,
        "summary": task_summary,
        "message": f"已将 {rec.show_name} S{rec.season_number:02d} 移入「暂不处理」（可随时恢复处理）",
    }


@router.delete("/{task_id}/seasons/{record_id}/hold")
async def restore_held_season(
    task_id: str,
    record_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """从「暂不处理」恢复处理，根据当前证据重新分流且不产生重复主卡。"""
    task = await db.get(Task, task_id)
    rec = await db.get(SeasonRecord, record_id)
    if not task or not rec or rec.task_id != task_id:
        raise HTTPException(status_code=404, detail="剧季记录不存在")

    from app.services.task_service import TASK_RUNNING_WORKERS
    if task.status in ("running", "paused", "stopping") or task_id in TASK_RUNNING_WORKERS:
        raise HTTPException(status_code=409, detail="请在初检结束后恢复处理")

    part_rows, _ = await _collect_work_season_records(db, rec)
    for r in part_rows:
        ev_r = _safe_json(getattr(r, "evidence_json", None), {})
        if isinstance(ev_r, dict):
            ev_r.pop("hold_for_task", None)
            r.evidence_json = json.dumps(ev_r, ensure_ascii=False)
        await _apply_in_place_recalculate(db, r, fetch_remote=False)

    task_summary = await refresh_task_summary(task_id, db)
    await db.commit()
    from app.core.season_classifier import get_record_work_context
    restored_work = await get_record_work_context(db, rec)
    return {
        "ok": True,
        "season": restored_work,
        "summary": task_summary,
        "message": f"已恢复 {rec.show_name} S{rec.season_number:02d} 的待办处理",
    }


@router.post("/{task_id}/retry-failed")
async def retry_task_failed_seasons(
    task_id: str,
    body: RecalculateRequest | None = Body(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """仅重试本任务中初检请求失败/超时的剧季及其关联记录，不无差别重跑已成功剧季。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    from app.services.task_service import TASK_RUNNING_WORKERS
    if task.status in ("running", "paused", "stopping") or task_id in TASK_RUNNING_WORKERS:
        raise HTTPException(status_code=409, detail="请在初检结束后重试失败项")

    all_rows = list((await db.scalars(select(SeasonRecord).where(SeasonRecord.task_id == task_id))).all())
    req_ids = set((body.record_ids if body else []) or [])
    target_ids: list[str] = []
    for r in all_rows:
        if r.status in ("manually_handled", "verified_complete", "cleaned_redundant", "source_absent", "archived"):
            continue
        ev = _safe_json(getattr(r, "evidence_json", None), {})
        is_failed = (
            (ev.get("meta_status") in ("fetch_failed", "query_failed"))
            or bool(ev.get("last_remote_recheck_error"))
            or bool(((ev.get("broadcast_evidence") or {}) if isinstance(ev, dict) else {}).get("last_check_incomplete"))
        )
        if (req_ids and r.id in req_ids) or (not req_ids and is_failed):
            target_ids.append(r.id)

    if not target_ids:
        return {
            "ok": True,
            "updated_count": 0,
            "remote_failures": [],
            "succeeded_ids": [],
            "message": "当前没有需要重试的失败项",
        }

    res = await _run_task_reevaluate_impl(
        db, task_id, RecalculateRequest(record_ids=target_ids, fetch_remote=True)
    )
    failed_id_set = {f["id"] for f in (res.get("remote_failures") or [])}
    res["succeeded_ids"] = [rid for rid in target_ids if rid not in failed_id_set]
    res["retried_ids"] = target_ids
    return res


@router.post("/{task_id}/seasons/{record_id}/reconcile-source")
async def reconcile_source_presence(task_id: str, record_id: str, db: AsyncSession = Depends(get_db)):
    """五模块共用的外部删除对账；仅强读源目录和更新记录，不删移媒体。"""
    task = await db.get(Task, task_id)
    rec = await db.get(SeasonRecord, record_id)
    if not task or not rec or rec.task_id != task_id:
        raise HTTPException(status_code=404, detail="剧季记录不存在")
    from app.services.task_service import TASK_RUNNING_WORKERS
    from app.api.archive import _RECOMMENDED_TASK_LOCKS
    lock = _RECOMMENDED_TASK_LOCKS.get(task_id)
    if task.status in ('running', 'paused', 'stopping') or task_id in TASK_RUNNING_WORKERS or (lock and lock.locked()):
        raise HTTPException(status_code=409, detail="当前任务正在处理，请完成后核对源目录")
    from app.clients.clouddrive2 import CloudDrive2Client
    cd2 = CloudDrive2Client()
    try:
        settings = get_settings()
        root = settings.source_root
        arc = (settings.archive_root or '').rstrip('/')
        if arc and (rec.season_path == arc or rec.season_path.startswith(arc + '/')):
            raise ValueError('记录指向归档库，不能按源目录对账')
        if await source_entry(cd2, rec.season_path, root) is not None:
            return dict(ok=True, source_absent=False, message='源目录仍存在；未改变记录或文件')
        ev = _safe_json(rec.evidence_json, {})
        if ev.get('source_root_preservation') or ev.get('source_cleanup_auxiliary'):
            return dict(ok=False, source_absent=False, message='源目录已不存在，但附件保全回执仍须核验；请继续清理剧集外层')
        if rec.status in ('already_archived', 'cleaned_redundant') and await source_entry(cd2, rec.show_path, root) is not None:
            return dict(ok=False, source_absent=False, message='本季已不存在，剧集外层仍在；请继续清理剧集外层，不必重新删除本季')
        if rec.status != 'source_absent':
            await record_source_absent(db, rec, show_absent=await source_entry(cd2, rec.show_path, settings.source_root) is None)
            await refresh_task_summary(task_id, db)
            await db.commit()
        return dict(ok=True, source_absent=True, message='已核实原源目录不存在并移出待办；未执行删除，不代表归档完成；下载任务保持原状')
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=f'源目录状态未核实，保留待办：{exc}') from exc
    finally:
        await cd2.close()


@router.post("/{task_id}/refresh-modified")
async def refresh_task_modified_scope(
    task_id: str,
    body: RefreshModifiedRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """按需刷新用户在外部修改过的文件、目录或 TMDB ID 范围（自动发现同父目录下改名后的新路径，不默认重跑整任务）。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    from app.services.task_service import TASK_RUNNING_WORKERS
    if task.status in ("running", "paused", "stopping") or task_id in TASK_RUNNING_WORKERS:
        raise HTTPException(status_code=409, detail="请在初检结束后刷新修改结果")

    all_rows = list((await db.scalars(select(SeasonRecord).where(SeasonRecord.task_id == task_id))).all())
    req_ids = set(body.record_ids or [])
    req_paths = {normalize_dir_path(p) for p in (body.paths or []) if p}

    target_rows: list[SeasonRecord] = []
    for r in all_rows:
        if r.id in req_ids or normalize_dir_path(r.season_path) in req_paths or normalize_dir_path(r.show_path) in req_paths:
            part_rows, _ = await _collect_work_season_records(db, r)
            for pr in part_rows:
                if pr not in target_rows:
                    target_rows.append(pr)

    if not target_rows:
        raise HTTPException(status_code=400, detail="请指定需要刷新修改结果的剧季记录或路径范围")

    from app.clients.clouddrive2 import CloudDrive2Client
    from app.clients.moviepilot import MoviePilotClient
    from app.clients.tmdb import TMDBClient
    from app.core.inspector import _parse_season_number, run_inspection

    settings = get_settings()
    cd2 = CloudDrive2Client()
    mp = MoviePilotClient()
    tmdb = TMDBClient()
    refreshed_ids: list[str] = []
    failed_items: list[dict[str, str]] = []
    now_dt = datetime.now(timezone.utc)

    try:
        for rec in target_rows:
            old_show_path = rec.show_path
            old_season_path = rec.season_path
            inspect_show_path = old_show_path
            inspect_season_path = old_season_path

            # 检查原季路径是否仍在；若用户在外部修改了剧名或 [tmdbid=...]，在父目录下定位新目录
            try:
                cur_files = await cd2.list_files(old_season_path, force_refresh=True)
            except Exception as exc:
                if not (isinstance(exc, FileNotFoundError) or getattr(exc, 'status', None) == 5):
                    failed_items.append({'id': rec.id, 'error': str(exc)})
                    continue
                cur_files = []

            try:
                original_exists = await source_entry(cd2, old_season_path, settings.source_root) is not None
            except Exception as exc:
                failed_items.append({'id': rec.id, 'error': str(exc)})
                continue
            if not cur_files and old_show_path:
                parent_dir = posixpath.dirname(normalize_dir_path(old_show_path))
                try:
                    siblings = await cd2.list_files(parent_dir, force_refresh=True)
                except Exception as exc:
                    failed_items.append({'id': rec.id, 'error': str(exc)})
                    continue
                old_clean = re.sub(r"\[.*?\]|\(\d{4}\)", "", rec.show_name or "").strip().casefold()
                for sib in siblings:
                    if not sib.get("isDirectory"):
                        continue
                    sname = str(sib.get("name") or "")
                    spath = normalize_dir_path(sib.get("path") or f"{parent_dir}/{sname}")
                    sclean = re.sub(r"\[.*?\]|\(\d{4}\)", "", sname).strip().casefold()
                    if old_clean and sclean and (old_clean == sclean or old_clean in sclean or sclean in old_clean):
                        try:
                            sub_items = await cd2.list_files(spath, force_refresh=True)
                        except Exception:
                            sub_items = []
                        season_dir_hit = next(
                            (
                                it for it in sub_items
                                if it.get("isDirectory") and _parse_season_number(it.get("name") or "") == rec.season_number
                            ),
                            None,
                        )
                        if season_dir_hit:
                            inspect_show_path = spath
                            inspect_season_path = normalize_dir_path(
                                season_dir_hit.get("path") or f"{spath}/{season_dir_hit.get('name')}"
                            )
                            break

            if not original_exists and inspect_season_path == old_season_path:
                prior = _safe_json(rec.evidence_json, {})
                if prior.get('source_root_preservation') or prior.get('source_cleanup_auxiliary'):
                    failed_items.append({'id': rec.id, 'error': '附件保全回执仍须核验，请继续清理剧集外层'})
                    continue
                if rec.status in ('already_archived', 'cleaned_redundant') and await source_entry(cd2, old_show_path, settings.source_root) is not None:
                    failed_items.append({'id': rec.id, 'error': '本季目录已不存在，剧集外层仍在；请使用继续清理剧集外层完成保全核验'})
                    continue
                await record_source_absent(db, rec, show_absent=await source_entry(cd2, rec.show_path, settings.source_root) is None)
                refreshed_ids.append(rec.id)
                continue

            try:
                res = await run_inspection(
                    task_id=rec.task_id,
                    source_path=inspect_show_path,
                    cd2_client=cd2,
                    mp_client=mp,
                    tmdb_client=tmdb,
                    archive_root=settings.archive_root,
                    global_source_root=settings.source_root,
                    only_season_path=inspect_season_path,
                    db=db,
                )
                diag = next(
                    (
                        s for s in res.seasons
                        if normalize_dir_path(s.season_path) == normalize_dir_path(inspect_season_path)
                        or s.season_number == rec.season_number
                    ),
                    None,
                )
            except Exception as exc:
                diag = None
                ev_old = _safe_json(getattr(rec, "evidence_json", None), {})
                ev_old["last_remote_recheck_error"] = f"{type(exc).__name__}: {exc}"
                rec.evidence_json = json.dumps(ev_old, ensure_ascii=False)
                failed_items.append({"id": rec.id, "path": old_season_path, "error": str(exc)})
                continue

            if diag is None or (diag.evidence or {}).get("meta_status") == "fetch_failed":
                ev_old = _safe_json(getattr(rec, "evidence_json", None), {})
                err_msg = ((diag.evidence or {}).get("meta_error") if diag else "") or "外部修改路径读取未完成"
                ev_old["last_remote_recheck_error"] = err_msg
                rec.evidence_json = json.dumps(ev_old, ensure_ascii=False)
                failed_items.append({"id": rec.id, "path": inspect_season_path, "error": err_msg})
                continue

            rec.show_name = diag.show_name
            rec.show_path = diag.show_path
            rec.season_path = diag.season_path
            rec.tmdb_id = str(diag.tmdb_id) if diag.tmdb_id is not None else None
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
            refreshed_ids.append(rec.id)
    finally:
        await cd2.close()
        await mp.close()
        await tmdb.close()

    all_after = list((await db.scalars(select(SeasonRecord).where(SeasonRecord.task_id == task_id))).all())
    dir_groups = await _rebuild_offline_directory_groups(db, task_id, all_after)
    task_summary = await refresh_task_summary(task_id, db)
    await db.commit()

    return {
        "ok": len(refreshed_ids) > 0 and len(failed_items) == 0,
        "refreshed_ids": refreshed_ids,
        "failed_items": failed_items,
        "directory_groups": dir_groups,
        "summary": task_summary,
        "message": (
            f"已刷新外部修改范围：成功更新 {len(refreshed_ids)} 季"
            + (f"，{len(failed_items)} 季未完成（保留原有效证据与取证时间）" if failed_items else "")
        ),
    }
