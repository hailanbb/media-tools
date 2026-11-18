"""任务服务 — 编排初检任务的生命周期。

职责：
- 创建/查询/删除任务
- 驱动初检引擎
- 将初检结果写入 SeasonRecord
- 更新任务状态和汇总统计
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.operation import OperationLog
from app.models.task import Task
from app.models.season import SeasonRecord
from app.core.inspector import InspectionResult, SeasonDiagnosis, run_inspection
from app.services.gap_fill_tracker import (
    build_identity_key,
    refresh_task_summary,
    supersede_changed_identity_records,
)

import asyncio

TASK_PROGRESS: dict[str, dict] = {}
TASK_EVENTS: dict[str, asyncio.Event] = {}
TASK_CANCELLED: set[str] = set()
TASK_STOP_EVENTS: dict[str, asyncio.Event] = {}
TASK_RUNNING_WORKERS: dict[str, asyncio.Task] = {}


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
    if task_id in TASK_STOP_EVENTS:
        TASK_STOP_EVENTS[task_id].set()
    evt = get_task_control_event(task_id)
    evt.set()
    if task_id in TASK_PROGRESS:
        TASK_PROGRESS[task_id]["status"] = "stopping"
        TASK_PROGRESS[task_id]["message"] = "已收到停止请求，正在结束扫描，随后保存已分析结果"
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
    """后台独立会话执行初检（F09: 具备代次互斥与工作协程生命周期追踪）。"""
    from app.database import async_session
    try:
        async with async_session() as db:
            await execute_inspection(task_id, db)
    except Exception as e:
        import logging
        logging.getLogger("mediaflow.task_service").error("后台初检异常: %s", e)
    finally:
        TASK_STOP_EVENTS.pop(task_id, None)
        TASK_RUNNING_WORKERS.pop(task_id, None)


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
        if task_id in TASK_CANCELLED:
            return
        pct = int((current / total) * 100) if total > 0 else 0
        if control_event and not control_event.is_set():
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
        stop_event = asyncio.Event()
        TASK_STOP_EVENTS[task_id] = stop_event
        if task_id in TASK_CANCELLED:
            stop_event.set()
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
                stop_event=stop_event,
                concurrency=3,
                db=db,
            )
        finally:
            await cd2.close()
            await mp.close()
            await tmdb.close()

        if task_id in TASK_CANCELLED:
            TASK_PROGRESS[task_id].update(status="stopping", current_show="",
                                          message=f"扫描已结束，正在保存 {len(result.seasons)} 季")
        # 人工完成标记按任务和源季路径保留，重扫生成新记录 ID 后仍然有效。
        handled_logs = await db.scalars(select(OperationLog.detail_json).where(
            OperationLog.task_id == task_id, OperationLog.action == "manually_handled"))
        handled_paths = {json.loads(detail)["season_path"] for detail in handled_logs if detail}
        for diag in result.seasons:
            if diag.season_path in handled_paths:
                diag.status = "manually_handled"
        result.compute_summary()

        # 4. 结果安全落库与历史保护（重检中止时不覆盖未扫描范围）
        is_stopped = task_id in TASK_CANCELLED

        if is_stopped and len(result.seasons) == 0:
            # 零产出中止保护：严禁删除旧记录！完整保留上次发布的季记录与汇总
            task.status = "stopped"  # type: ignore[assignment]
            await db.commit()

            TASK_PROGRESS[task_id] = {
                "task_id": task_id,
                "status": "stopped",
                "current": 0,
                "total": 0,
                "current_show": "",
                "percent": 0,
                "message": "初检已停止（本轮未产出新记录，完整保留上次已有结果）",
                "error": None,
            }
            TASK_EVENTS.pop(task_id, None)
            TASK_CANCELLED.discard(task_id)
            return result

        now_checked = datetime.now(timezone.utc)
        scanned_show_paths = {diag.show_path for diag in result.seasons}

        # 快照上一轮记录以支持连载季上游请求失败保护与季终转出追踪 (Section 4, L10, L11)
        from app.core.season_classifier import build_work_key, consolidate_seasons_to_work_records
        prev_all_rows = list(
            (await db.scalars(select(SeasonRecord).where(SeasonRecord.task_id == task_id))).all()
        )
        prev_by_season_path = {r.season_path: r for r in prev_all_rows if r.season_path}
        prev_work_recs, _ = consolidate_seasons_to_work_records(prev_all_rows)
        prev_airing_by_wkey = {
            build_work_key(w): w for w in prev_work_recs if w.get("main_category") == "airing"
        }

        today_iso = datetime.now().strftime("%Y-%m-%d")
        from app.core.inspector import unverified_metadata_candidates
        from app.core.manual_confirmation import compute_asset_fingerprint

        for diag in result.seasons:
            ev_d = dict(diag.evidence or {})
            if diag.season_path in prev_by_season_path:
                prev_r = prev_by_season_path[diag.season_path]
                try:
                    prev_ev = json.loads(prev_r.evidence_json or "{}")
                except Exception:
                    prev_ev = {}
                if not isinstance(prev_ev, dict):
                    prev_ev = {}
                prev_b_ev = dict((prev_ev.get("broadcast_evidence") or {}) if isinstance(prev_ev, dict) else {})
                if ev_d.get("meta_status") == "fetch_failed" and (prev_r.status == "airing" or prev_b_ev.get("status") in ("airing", "upcoming")):
                    upc_sched = list(prev_b_ev.get("upcoming_schedule") or [])
                    all_expired = bool(upc_sched) and all(
                        str(x.get("air_date") or "9999-99-99")[:10] <= today_iso for x in upc_sched
                    )
                    if all_expired:
                        diag.status = "period_review"
                        diag.season_finished = False
                        diag.total_episodes = prev_r.total_episodes
                        diag.status_detail = "证据待决 · 安全保留：本次核查未完成且上次排期已失效，需重新初检核实最新播期。"
                        prev_b_ev["status"] = "evidence_pending"
                        prev_b_ev["last_check_incomplete"] = True
                        ev_d["meta_status"] = "ok"
                        ev_d["broadcast_evidence"] = prev_b_ev
                        diag.evidence = ev_d
                    else:
                        diag.status = "airing"
                        diag.season_finished = False
                        diag.total_episodes = prev_r.total_episodes
                        prev_b_ev["last_check_incomplete"] = True
                        prev_b_ev["last_check_error"] = diag.status_detail
                        base_prev_detail = re.sub(r"（本次核查未完成.*?）", "", str(prev_r.status_detail or "连载留源中"))
                        diag.status_detail = f"{base_prev_detail}（本次核查未完成，保留上次有效结论）"
                        ev_d["meta_status"] = "ok"
                        ev_d["broadcast_evidence"] = prev_b_ev
                        diag.evidence = ev_d

                # 跨轮资产指纹差分智能 (Delta Intelligence)
                prev_fp = str(prev_ev.get("asset_fingerprint") or "")
                if not prev_fp:
                    try:
                        p_rec_eps = json.loads(prev_r.recognized_episodes_json or "[]")
                    except Exception:
                        p_rec_eps = []
                    try:
                        p_dup_eps = json.loads(prev_r.duplicate_episodes_json or "[]")
                    except Exception:
                        p_dup_eps = []
                    p_arc_eps = ((prev_ev.get("dual_archive_info") or {}).get("archived_episodes") or [])
                    prev_fp = compute_asset_fingerprint(
                        recognized_episodes=p_rec_eps,
                        duplicate_episodes=p_dup_eps,
                        local_episode_count=prev_r.local_episode_count or 0,
                        archived_episodes=p_arc_eps,
                        total_episodes=prev_r.total_episodes,
                        season_finished=prev_r.season_finished,
                        meta_status=str(prev_ev.get("meta_status") or ""),
                        broadcast_status=str(prev_b_ev.get("status") or ""),
                    )
                curr_fp = str(ev_d.get("asset_fingerprint") or "")
                if not curr_fp:
                    c_arc_eps = ((ev_d.get("dual_archive_info") or {}).get("archived_episodes") or [])
                    curr_fp = compute_asset_fingerprint(
                        recognized_episodes=diag.recognized_episodes,
                        duplicate_episodes=diag.duplicate_episodes,
                        local_episode_count=diag.local_episode_count or 0,
                        archived_episodes=c_arc_eps,
                        total_episodes=diag.total_episodes,
                        season_finished=diag.season_finished,
                        meta_status=str(ev_d.get("meta_status") or ""),
                        broadcast_status=str((ev_d.get("broadcast_evidence") or {}).get("status") or ""),
                    )
                    ev_d["asset_fingerprint"] = curr_fp

                had_legacy_candidate_pollution = bool(
                    unverified_metadata_candidates(prev_ev, prev_r.tmdb_id, prev_r.season_number)
                    and prev_r.total_episodes is not None
                )
                if prev_r.status in ("unknown", "period_review", "archive_check", "identity_review") and not had_legacy_candidate_pollution:
                    if curr_fp == prev_fp and diag.status in ("unknown", "period_review", "archive_check"):
                        ev_d["delta_state"] = "unchanged_shelved"
                        ev_d["delta_note"] = "跨轮资产与元数据指纹未变，已自动静默折叠"
                    elif curr_fp != prev_fp:
                        ev_d["delta_state"] = "progressed"
                        ev_d["delta_note"] = "检测到物理文件或官方元数据较上轮发生实质变化"
                diag.evidence = ev_d
        result.compute_summary()

        if is_stopped:
            # 部分完成中止：仅删除本轮已完整扫描的剧目旧记录，保留其余未扫描剧目的历史记录
            old_records_to_del = await db.scalars(
                select(SeasonRecord).where(
                    SeasonRecord.task_id == task_id,
                    SeasonRecord.show_path.in_(scanned_show_paths),
                )
            )
            for old in old_records_to_del.all():
                await db.delete(old)
            await db.flush()
        else:
            # 完整扫描成功：全量替换该任务旧记录
            for old in prev_all_rows:
                await db.delete(old)
            await db.flush()

        for diag in result.seasons:
            ev_diag = diag.evidence or {}
            b_ev_diag = (ev_diag.get("broadcast_evidence") or {}) if isinstance(ev_diag, dict) else {}
            prev_r = prev_by_season_path.get(diag.season_path)
            rec_checked_at = (
                prev_r.last_checked_at
                if (b_ev_diag.get("last_check_incomplete") and prev_r and prev_r.last_checked_at)
                else now_checked
            )
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
                evidence_json=json.dumps(ev_diag, ensure_ascii=False),
                conflict_json=json.dumps(diag.conflict or {}, ensure_ascii=False),
                mapping_json=json.dumps(diag.mapping or {}, ensure_ascii=False),
                last_checked_at=rec_checked_at,
            )
            db.add(record)
            await db.flush()
            active_id_key = build_identity_key(
                diag.show_name,
                diag.tmdb_id,
                diag.season_number,
                diag.mapping or None,
            )
            await supersede_changed_identity_records(
                db=db,
                task_id=task_id,
                season_path=diag.season_path,
                active_identity_key=active_id_key,
                new_season_record_id=record.id,
            )

        # 5. 更新任务汇总（扣除已推送全涵盖，保留真实待补清单）与连载季转出记录
        final_status = "stopped" if is_stopped else "completed"

        task.status = final_status  # type: ignore[assignment]
        summary_dict = await refresh_task_summary(task_id, db)

        airing_transitions: list[dict] = []
        if prev_airing_by_wkey:
            curr_rows = await get_task_seasons(task_id, None, db)
            curr_work_recs, _ = consolidate_seasons_to_work_records(curr_rows)
            curr_by_wkey = {build_work_key(w): w for w in curr_work_recs}
            cat_labels = {
                "archive": "齐全可归档",
                "download": "真缺集待补",
                "review": "待复核清理",
                "anomaly": "异常剧集",
            }
            for wkey, prev_w in prev_airing_by_wkey.items():
                curr_w = curr_by_wkey.get(wkey)
                if curr_w and curr_w.get("main_category") != "airing":
                    to_cat = str(curr_w.get("main_category") or "")
                    to_label = cat_labels.get(to_cat, to_cat)
                    airing_transitions.append({
                        "work_key": wkey,
                        "record_id": curr_w.get("id"),
                        "show_name": curr_w.get("show_name"),
                        "season_number": curr_w.get("season_number"),
                        "from_category": "airing",
                        "to_category": to_cat,
                        "to_category_label": to_label,
                        "reason": curr_w.get("status_detail") or f"本季已播毕，转入「{to_label}」",
                    })
            if airing_transitions and isinstance(summary_dict, dict):
                summary_dict["last_airing_transitions"] = airing_transitions
                summary_dict["still_airing_count"] = summary_dict.get("airing", 0)
                task.summary_json = json.dumps(summary_dict, ensure_ascii=False)

        await db.commit()

        total_db_seasons = len(await get_task_seasons(task_id, None, db))

        TASK_PROGRESS[task_id] = {
            "task_id": task_id,
            "status": final_status,
            "current": total_db_seasons,
            "total": total_db_seasons,
            "current_show": "",
            "percent": 100 if not is_stopped else TASK_PROGRESS.get(task_id, {}).get("percent", 50),
            "message": f"初检已停止：已保存已分析的 {len(result.seasons)} 季（当前任务共保留 {total_db_seasons} 季）" if is_stopped else f"初检完成：已分析 {len(result.seasons)} 季",
            "error": None,
        }

        TASK_EVENTS.pop(task_id, None)
        TASK_CANCELLED.discard(task_id)

        return result

    except Exception as exc:
        await db.rollback()
        try:
            task = await db.get(Task, task_id)
            if task:
                task.status = "failed"  # type: ignore[assignment]
                await db.commit()
        except Exception:
            pass
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
    stmt = select(SeasonRecord).where(
        SeasonRecord.task_id == task_id,
        SeasonRecord.status.not_in(("manually_handled", "verified_complete", "cleaned_redundant", "source_absent", "archived")),
    )
    if status:
        stmt = stmt.where(SeasonRecord.status == status)
    stmt = stmt.order_by(SeasonRecord.show_name, SeasonRecord.season_number)
    result = await db.execute(stmt)
    return list(result.scalars().all())
