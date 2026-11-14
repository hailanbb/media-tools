"""逐集补缺持久化追踪、对账入库与真实待补计算引擎。

核心原则：
1. 按稳定季键 `(task_id, season_path, identity_key)` 持久化，重检重建 SeasonRecord.id 后仍自动继承，更换作品或映射口径后自动审计隔离（superseded）；
2. 在调起 MP 提交前先落盘意图（intent_submitting），提交后立即落盘脱敏回执（绝不落盘站点 passkey/cookie/私有下载链接）；
3. 默认待补清单严格扣除 `accepted/queued/transfer_pending/submitted_unverified`，保留未命中与部分失败集，支持同页切换“已推送待入库”；
4. 支持一键“核对入库”，联合 MP 下载队列 + 整理队列 + 下载/转移历史 + 双库实体文件核对：已入库则收口，已取消且无实物则退回待补。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.moviepilot import parse_episode_str, parse_season_str
from app.core.identity_resolver import normalize_title_token, translate_search_to_local_episodes
from app.models.gap_fill import GapFillEpisode, GapFillSeasonRun
from app.models.operation import OperationLog
from app.models.season import SeasonRecord
from app.models.task import Task

# 会从“默认待补清单”扣除（阻断本轮及后续重复推种）的集状态
BLOCKING_GAP_FILL_STATES = {
    "intent_submitting",
    "queued",
    "transfer_pending",
    "submitted_unverified",
}

IN_FLIGHT_RECONCILE_STATES = {
    "intent_submitting",
    "queued",
    "transfer_pending",
    "submitted_unverified",
}


def sanitize_text_no_secrets(text: str | None) -> str:
    """过滤潜在站内 passkey/authkey/token 参数，绝不落盘或暴露给前端。"""
    if not text:
        return ""
    cleaned = re.sub(
        r"((?:[?&;\s]|^)(?:passkey|authkey|sign|token|key|secret|pwd|cookie)=)[^&\s;\"']+",
        r"\1[REDACTED]",
        str(text),
        flags=re.IGNORECASE,
    )
    return cleaned


def build_identity_key(
    show_name: str,
    tmdb_id: int | str | None,
    season_number: int,
    mapping: dict[str, Any] | None = None,
) -> str:
    """生成受作品与跨条目映射口径约束的稳定身份键。"""
    tid_str = str(tmdb_id).strip() if tmdb_id is not None and str(tmdb_id).strip().isdigit() else ""
    work_part = f"tmdb:{tid_str}" if tid_str else f"name:{normalize_title_token(show_name)}"
    if mapping and mapping.get("target_tmdb_id"):
        st = int(mapping["target_tmdb_id"])
        ss = int(mapping.get("target_season") or 1)
        nm = str(mapping.get("numbering_mode") or "independent")
        off = int(mapping.get("episode_offset") or 0)
        search_part = f"st:{st}:S{ss}:nm:{nm}:{off}"
    else:
        search_part = f"st:{tid_str or '0'}:S{int(season_number)}:nm:standard:0"
    return f"{work_part}|S{int(season_number)}|{search_part}"


def _norm_path(path: str | None) -> str:
    return (path or "").rstrip("/")


def _extract_mp_download_id(reply: Any) -> str:
    if not isinstance(reply, dict):
        return ""
    for key in ("download_id", "hash", "task_id", "id"):
        val = reply.get(key)
        if val:
            return sanitize_text_no_secrets(str(val))
    data = reply.get("data")
    if isinstance(data, dict):
        for key in ("download_id", "hash", "task_id", "id"):
            val = data.get(key)
            if val:
                return sanitize_text_no_secrets(str(val))
    return ""


async def supersede_changed_identity_records(
    db: AsyncSession,
    task_id: str,
    season_path: str,
    active_identity_key: str,
    new_season_record_id: str,
) -> None:
    """重检新建记录后关联旧记录；若作品或映射口径已改，旧推送标记 superseded 仅作审计不阻塞新口径。"""
    npath = _norm_path(season_path)
    ep_rows = (
        await db.execute(
            select(GapFillEpisode).where(
                GapFillEpisode.task_id == task_id,
                GapFillEpisode.season_path == npath,
                GapFillEpisode.state != "superseded",
            )
        )
    ).scalars().all()
    for row in ep_rows:
        if row.identity_key == active_identity_key:
            row.season_record_id = new_season_record_id
        else:
            row.state = "superseded"
            row.reason = f"{row.reason}（作品/季映射口径已变更，旧记录已自动归档审计）".strip("（）")

    run_rows = (
        await db.execute(
            select(GapFillSeasonRun).where(
                GapFillSeasonRun.task_id == task_id,
                GapFillSeasonRun.season_path == npath,
            )
        )
    ).scalars().all()
    for r in run_rows:
        if r.identity_key == active_identity_key:
            r.season_record_id = new_season_record_id


async def record_submit_intent(
    db: AsyncSession,
    rec: SeasonRecord,
    identity_key: str,
    episodes: list[int],
    *,
    batch_run_id: str,
    search_tmdb_id: int | None,
    search_season_number: int,
    numbering_mode: str,
    torrent_title: str,
    torrent_hash: str,
    site_id: str,
    site_name: str,
    size_bytes: int,
    seeders: int,
    covered_all: list[int],
    diag_summary: dict[str, Any],
) -> list[GapFillEpisode]:
    """在调用 MP submit_download 前先落盘逐集意图，防范提交中崩溃/超时造成状态丢失或重推。"""
    npath = _norm_path(rec.season_path)
    now_dt = datetime.now(timezone.utc)
    safe_title = sanitize_text_no_secrets(torrent_title)
    safe_diag = json.dumps(diag_summary, ensure_ascii=False)
    safe_covered = json.dumps(sorted(set(covered_all)))
    tid = int(rec.tmdb_id) if rec.tmdb_id and str(rec.tmdb_id).isdigit() else None

    created_or_updated: list[GapFillEpisode] = []
    for ep_num in sorted(set(episodes)):
        existing = (
            await db.execute(
                select(GapFillEpisode).where(
                    GapFillEpisode.task_id == rec.task_id,
                    GapFillEpisode.season_path == npath,
                    GapFillEpisode.identity_key == identity_key,
                    GapFillEpisode.episode_number == int(ep_num),
                )
            )
        ).scalar_one_or_none()

        if existing:
            existing.season_record_id = rec.id
            existing.show_name = rec.show_name
            existing.tmdb_id = tid
            existing.search_tmdb_id = search_tmdb_id
            existing.season_number = rec.season_number
            existing.search_season_number = search_season_number
            existing.numbering_mode = numbering_mode
            existing.batch_run_id = batch_run_id
            existing.state = "intent_submitting"
            existing.outcome_code = "submitting"
            existing.reason = "正在向 MoviePilot 提交下载请求..."
            existing.torrent_hash = str(torrent_hash or "").casefold()[:120]
            existing.torrent_title = safe_title
            existing.site_id = str(site_id or "")[:64]
            existing.site_name = str(site_name or "")[:120]
            existing.size_bytes = int(size_bytes or 0)
            existing.seeders = int(seeders or 0)
            existing.covered_episodes_json = safe_covered
            existing.search_diag_json = safe_diag
            existing.is_legacy_inferred = False
            existing.updated_at = now_dt
            created_or_updated.append(existing)
        else:
            row = GapFillEpisode(
                task_id=rec.task_id,
                season_record_id=rec.id,
                season_path=npath,
                show_name=rec.show_name,
                tmdb_id=tid,
                search_tmdb_id=search_tmdb_id,
                season_number=rec.season_number,
                search_season_number=search_season_number,
                numbering_mode=numbering_mode,
                identity_key=identity_key,
                episode_number=int(ep_num),
                batch_run_id=batch_run_id,
                state="intent_submitting",
                outcome_code="submitting",
                reason="正在向 MoviePilot 提交下载请求...",
                torrent_hash=str(torrent_hash or "").casefold()[:120],
                torrent_title=safe_title,
                site_id=str(site_id or "")[:64],
                site_name=str(site_name or "")[:120],
                size_bytes=int(size_bytes or 0),
                seeders=int(seeders or 0),
                covered_episodes_json=safe_covered,
                search_diag_json=safe_diag,
                is_legacy_inferred=False,
            )
            db.add(row)
            created_or_updated.append(row)

    await db.commit()
    return created_or_updated


async def finalize_submit_outcome(
    db: AsyncSession,
    rows: list[GapFillEpisode],
    *,
    outcome_state: str,
    outcome_code: str,
    reason: str,
    reply: Any = None,
) -> None:
    """根据 MP 提交真实返回即时更新落盘状态（受理/超时待核实/明确拒绝）。"""
    mp_id = _extract_mp_download_id(reply)
    safe_reason = sanitize_text_no_secrets(reason)
    now_dt = datetime.now(timezone.utc)
    for row in rows:
        row.state = outcome_state
        row.outcome_code = outcome_code
        row.reason = safe_reason
        if mp_id:
            row.mp_download_id = mp_id
        row.updated_at = now_dt
    await db.commit()


async def record_season_run_snapshot(
    db: AsyncSession,
    rec: SeasonRecord,
    identity_key: str,
    *,
    batch_run_id: str,
    outcome_status: str,
    summary_text: str,
    target_episodes: list[int],
    accepted_episodes: list[int],
    unverified_episodes: list[int],
    failed_episodes: dict[str, Any],
    candidate_count: int = 0,
    max_seeders_seen: int = 0,
    min_seeders_required: int = 2,
    sites_summary: dict[str, Any] | None = None,
    search_diag: dict[str, Any] | None = None,
) -> GapFillSeasonRun:
    """更新当前季的最近一次推送与检索诊断快照，确保整季0命中/做种不足/部分失败/中止均完整保留。"""
    npath = _norm_path(rec.season_path)
    tid = int(rec.tmdb_id) if rec.tmdb_id and str(rec.tmdb_id).isdigit() else None
    existing = (
        await db.execute(
            select(GapFillSeasonRun).where(
                GapFillSeasonRun.task_id == rec.task_id,
                GapFillSeasonRun.season_path == npath,
                GapFillSeasonRun.identity_key == identity_key,
            )
        )
    ).scalar_one_or_none()

    safe_summary = sanitize_text_no_secrets(summary_text)
    if existing:
        existing.season_record_id = rec.id
        existing.show_name = rec.show_name
        existing.tmdb_id = tid
        existing.season_number = rec.season_number
        existing.batch_run_id = batch_run_id
        existing.outcome_status = outcome_status
        existing.summary_text = safe_summary
        existing.target_episodes_json = json.dumps(sorted(set(target_episodes)))
        existing.accepted_episodes_json = json.dumps(sorted(set(accepted_episodes)))
        existing.unverified_episodes_json = json.dumps(sorted(set(unverified_episodes)))
        existing.failed_episodes_json = json.dumps(failed_episodes, ensure_ascii=False)
        existing.candidate_count = int(candidate_count or 0)
        existing.max_seeders_seen = int(max_seeders_seen or 0)
        existing.min_seeders_required = int(min_seeders_required or 2)
        existing.sites_summary_json = json.dumps(sites_summary or {}, ensure_ascii=False)
        existing.search_diag_json = json.dumps(search_diag or {}, ensure_ascii=False)
        await db.commit()
        return existing

    run = GapFillSeasonRun(
        task_id=rec.task_id,
        season_record_id=rec.id,
        season_path=npath,
        show_name=rec.show_name,
        tmdb_id=tid,
        season_number=rec.season_number,
        identity_key=identity_key,
        batch_run_id=batch_run_id,
        outcome_status=outcome_status,
        summary_text=safe_summary,
        target_episodes_json=json.dumps(sorted(set(target_episodes))),
        accepted_episodes_json=json.dumps(sorted(set(accepted_episodes))),
        unverified_episodes_json=json.dumps(sorted(set(unverified_episodes))),
        failed_episodes_json=json.dumps(failed_episodes, ensure_ascii=False),
        candidate_count=int(candidate_count or 0),
        max_seeders_seen=int(max_seeders_seen or 0),
        min_seeders_required=int(min_seeders_required or 2),
        sites_summary_json=json.dumps(sites_summary or {}, ensure_ascii=False),
        search_diag_json=json.dumps(search_diag or {}, ensure_ascii=False),
    )
    db.add(run)
    await db.commit()
    return run


async def record_unsubmitted_episode_outcomes(
    db: AsyncSession,
    rec: SeasonRecord,
    identity_key: str,
    episodes: list[int],
    *,
    batch_run_id: str,
    outcome_state: str,
    outcome_code: str,
    reason: str,
    search_tmdb_id: int | None = None,
    search_season_number: int = 0,
    numbering_mode: str = "standard",
    diag_summary: dict[str, Any] | None = None,
) -> None:
    """记录未提交成功集数（无结果/做种不足/不符/中止未执行）原因；绝不覆盖已在途阻塞状态。"""
    npath = _norm_path(rec.season_path)
    tid = int(rec.tmdb_id) if rec.tmdb_id and str(rec.tmdb_id).isdigit() else None
    safe_reason = sanitize_text_no_secrets(reason)
    safe_diag = json.dumps(diag_summary or {}, ensure_ascii=False)
    for ep_num in sorted(set(episodes)):
        existing = (
            await db.execute(
                select(GapFillEpisode).where(
                    GapFillEpisode.task_id == rec.task_id,
                    GapFillEpisode.season_path == npath,
                    GapFillEpisode.identity_key == identity_key,
                    GapFillEpisode.episode_number == int(ep_num),
                )
            )
        ).scalar_one_or_none()
        if existing and existing.state in BLOCKING_GAP_FILL_STATES:
            # 保护已推送待入库集不被“本轮未命中/中止”误覆盖
            continue
        if existing:
            existing.season_record_id = rec.id
            existing.batch_run_id = batch_run_id
            existing.state = outcome_state
            existing.outcome_code = outcome_code
            existing.reason = safe_reason
            existing.search_diag_json = safe_diag
            existing.is_legacy_inferred = False
        else:
            db.add(
                GapFillEpisode(
                    task_id=rec.task_id,
                    season_record_id=rec.id,
                    season_path=npath,
                    show_name=rec.show_name,
                    tmdb_id=tid,
                    search_tmdb_id=search_tmdb_id,
                    season_number=rec.season_number,
                    search_season_number=search_season_number,
                    numbering_mode=numbering_mode,
                    identity_key=identity_key,
                    episode_number=int(ep_num),
                    batch_run_id=batch_run_id,
                    state=outcome_state,
                    outcome_code=outcome_code,
                    reason=safe_reason,
                    search_diag_json=safe_diag,
                    is_legacy_inferred=False,
                )
            )
    await db.commit()


def _parse_json_safe(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _dt_ts(dt: datetime | None) -> float:
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).timestamp()
    return dt.timestamp()


async def compute_season_gap_fill_view(
    db: AsyncSession,
    rec: SeasonRecord,
) -> dict[str, Any]:
    """计算单季真实待补集 vs 已推送待入库状态（合并新持久化表 + 历史 OperationLog 安全兜底）。"""
    physical_missing = sorted({int(x) for x in _parse_json_safe(rec.missing_episodes_json, []) if str(x).isdigit()})
    mapping = _parse_json_safe(rec.mapping_json, None) or None
    identity_key = build_identity_key(rec.show_name, rec.tmdb_id, rec.season_number, mapping)
    npath = _norm_path(rec.season_path)

    ep_rows = (
        await db.execute(
            select(GapFillEpisode).where(
                GapFillEpisode.season_path == npath,
                GapFillEpisode.identity_key == identity_key,
                GapFillEpisode.state != "superseded",
            )
        )
    ).scalars().all()

    # 优先采用当前 task_id 的记录，同时兼顾跨任务针对同一季路径+身份的在途推送防重
    ep_by_num: dict[int, GapFillEpisode] = {}
    valid_rows = [r for r in ep_rows if hasattr(r, "episode_number") and r.episode_number is not None]
    for row in sorted(valid_rows, key=lambda r: (getattr(r, "task_id", None) == rec.task_id, _dt_ts(getattr(r, "updated_at", None) or getattr(r, "created_at", None)), getattr(r, "id", 0))):
        ep_by_num[int(row.episode_number)] = row

    run_row = (
        await db.execute(
            select(GapFillSeasonRun)
            .where(
                GapFillSeasonRun.task_id == rec.task_id,
                GapFillSeasonRun.season_path == npath,
                GapFillSeasonRun.identity_key == identity_key,
            )
            .order_by(GapFillSeasonRun.updated_at.desc(), GapFillSeasonRun.id.desc())
        )
    ).scalar_one_or_none()

    # 历史任务兼容：若尚未落入 GapFillEpisode 新表，尝试安全读取 OperationLog(action="download_gap_fill")
    legacy_inferred_eps: dict[int, dict[str, Any]] = {}
    if not ep_by_num and physical_missing:
        op_rows = (
            await db.execute(
                select(OperationLog)
                .where(
                    OperationLog.task_id == rec.task_id,
                    OperationLog.action == "download_gap_fill",
                    OperationLog.status == "completed",
                )
                .order_by(OperationLog.id.desc())
            )
        ).scalars().all()
        rec_clean_norm = normalize_title_token(rec.show_name)
        for op in op_rows:
            d = _parse_json_safe(op.detail_json, {})
            if not isinstance(d, dict):
                continue
            if op.season_record_id != rec.id:
                op_show = normalize_title_token(str(d.get("show") or ""))
                if not (op_show and op_show == rec_clean_norm and int(d.get("season") or 0) == int(rec.season_number)):
                    continue
            if d.get("mp_accepted") is False:
                continue
            cov = [int(x) for x in (d.get("covered") or []) if str(x).isdigit()]
            if not cov:
                continue
            for ep in cov:
                if ep in physical_missing and ep not in legacy_inferred_eps:
                    legacy_inferred_eps[ep] = {
                        "episode": ep,
                        "state": "submitted_unverified",
                        "outcome_code": "legacy_inferred",
                        "reason": "由历史补缺日志推断（建议点「核对入库」确认队列/实文件）",
                        "torrent_title": sanitize_text_no_secrets(str(d.get("title") or "")),
                        "site": str(d.get("site") or ""),
                        "size_bytes": int(d.get("size_bytes") or 0),
                        "seeds": int(d.get("seeds") or 0),
                        "is_legacy_inferred": True,
                    }

    blocking_eps: list[int] = []
    queued_eps: list[int] = []
    unverified_eps: list[int] = []
    reconciled_in_lib_eps: list[int] = []
    cancelled_freed_eps: list[int] = []
    episodes_detail: list[dict[str, Any]] = []

    phys_set = set(physical_missing)
    all_relevant_eps = sorted(phys_set | set(ep_by_num.keys()))
    for ep_num in all_relevant_eps:
        row = ep_by_num.get(ep_num)
        if row:
            # 若物理缺集中已不再缺少该集且曾经推送过，直接视为物理已覆盖
            if ep_num not in phys_set and row.state in IN_FLIGHT_RECONCILE_STATES:
                eff_state = "in_library"
                eff_code = "reconciled_in_library"
                eff_reason = "已核对入库（双库已存在实文件）"
            else:
                eff_state = row.state
                eff_code = row.outcome_code
                eff_reason = row.reason

            if eff_state in BLOCKING_GAP_FILL_STATES and ep_num in phys_set:
                blocking_eps.append(ep_num)
                if eff_state in ("submitted_unverified", "intent_submitting"):
                    unverified_eps.append(ep_num)
                else:
                    queued_eps.append(ep_num)
            elif eff_state == "in_library":
                reconciled_in_lib_eps.append(ep_num)
            elif eff_code == "mp_cancelled_freed":
                cancelled_freed_eps.append(ep_num)

            episodes_detail.append({
                "episode": ep_num,
                "state": eff_state,
                "outcome_code": eff_code,
                "reason": eff_reason,
                "mp_download_id": row.mp_download_id,
                "torrent_hash": row.torrent_hash,
                "torrent_title": sanitize_text_no_secrets(row.torrent_title),
                "site": row.site_name or row.site_id,
                "size_bytes": row.size_bytes,
                "seeders": row.seeders,
                "is_legacy_inferred": row.is_legacy_inferred,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            })
        elif ep_num in legacy_inferred_eps:
            item = legacy_inferred_eps[ep_num]
            blocking_eps.append(ep_num)
            unverified_eps.append(ep_num)
            episodes_detail.append(item)
        elif ep_num in phys_set:
            episodes_detail.append({
                "episode": ep_num,
                "state": "todo",
                "outcome_code": "todo",
                "reason": "待补缺失集",
                "mp_download_id": "",
                "torrent_hash": "",
                "torrent_title": "",
                "site": "",
                "size_bytes": 0,
                "seeders": 0,
                "is_legacy_inferred": False,
                "updated_at": None,
            })

    actionable_missing = sorted(phys_set - set(blocking_eps))
    is_gap_season = rec.status in ("download_needed", "period_missing")
    if not is_gap_season:
        view_bucket = "none"
    elif actionable_missing:
        view_bucket = "todo"
    elif blocking_eps:
        view_bucket = "pushed_pending"
    else:
        view_bucket = "none"

    last_run_info = None
    if run_row:
        last_run_info = {
            "batch_run_id": run_row.batch_run_id,
            "outcome_status": run_row.outcome_status,
            "summary_text": run_row.summary_text,
            "target_episodes": _parse_json_safe(run_row.target_episodes_json, []),
            "accepted_episodes": _parse_json_safe(run_row.accepted_episodes_json, []),
            "unverified_episodes": _parse_json_safe(run_row.unverified_episodes_json, []),
            "failed_episodes": _parse_json_safe(run_row.failed_episodes_json, {}),
            "candidate_count": run_row.candidate_count,
            "max_seeders_seen": run_row.max_seeders_seen,
            "min_seeders_required": run_row.min_seeders_required,
            "sites_summary": _parse_json_safe(run_row.sites_summary_json, {}),
            "search_diag": _parse_json_safe(run_row.search_diag_json, {}),
            "updated_at": run_row.updated_at.isoformat() if run_row.updated_at else None,
        }

    return {
        "identity_key": identity_key,
        "physical_missing_episodes": physical_missing,
        "actionable_missing_episodes": actionable_missing,
        "pushed_episodes": sorted(set(blocking_eps)),
        "queued_episodes": sorted(set(queued_eps)),
        "unverified_episodes": sorted(set(unverified_eps)),
        "reconciled_in_library_episodes": sorted(set(reconciled_in_lib_eps)),
        "cancelled_freed_episodes": sorted(set(cancelled_freed_eps)),
        "view_bucket": view_bucket,
        "episodes": episodes_detail,
        "last_run": last_run_info,
    }


def _match_torrent_in_items(
    row: GapFillEpisode,
    items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """按 torrent_hash / mp_download_id / 归一化标题 / 剧名+季号 匹配队列或历史记录。"""
    r_hash = (row.torrent_hash or "").strip().casefold()
    r_id = (row.mp_download_id or "").strip().casefold()
    r_title = normalize_title_token(row.torrent_title)
    r_show = normalize_title_token(re.sub(r"\[tmdbid[=:]\d+\]", "", row.show_name or ""))

    for it in items:
        if not isinstance(it, dict):
            continue
        it_hash = str(it.get("hash") or it.get("info_hash") or "").strip().casefold()
        it_id = str(it.get("download_id") or it.get("id") or "").strip().casefold()
        if r_hash and it_hash and r_hash == it_hash:
            return it
        if r_id and it_id and r_id == it_id:
            return it
        it_title_raw = str(it.get("title") or it.get("name") or it.get("torrent_name") or "")
        if r_title and normalize_title_token(it_title_raw) == r_title:
            return it
        # 按 tmdb_id + season 匹配历史或队列
        it_tid = str(it.get("tmdb_id") or it.get("tmdbid") or "")
        st_tid = str(row.search_tmdb_id or row.tmdb_id or "")
        if st_tid and it_tid == st_tid:
            it_sn = it.get("season")
            if it_sn is None and it.get("seasons"):
                parsed_sns = parse_season_str(it.get("seasons"))
                it_sn = next(iter(parsed_sns), None)
            target_sn = row.search_season_number or row.season_number
            if it_sn is None or int(it_sn) == int(target_sn):
                ep_str = str(it.get("episodes") or it.get("episode") or "")
                if ep_str:
                    nums = parse_episode_str(ep_str)
                    mapped_row_ep = row.episode_number
                    if row.numbering_mode != "standard" and row.search_season_number:
                        # 仅当同一集或未偏移时直接对比
                        pass
                    if mapped_row_ep in nums or not nums:
                        return it
                elif r_show and r_show in normalize_title_token(it_title_raw):
                    return it
    return None


async def reconcile_gap_fill_records(
    task_id: str,
    db: AsyncSession,
    *,
    mp_client=None,
    cd2_client=None,
    season_record_id: str | None = None,
    season_record_ids: list[str] | None = None,
    force_release_missing: bool = False,
) -> dict[str, Any]:
    """对账「已推送待入库」集：联合 MP 下载队列 + 整理队列 + 历史 + 双库实体状态更新收口。"""
    from app.clients.clouddrive2 import CloudDrive2Client
    from app.clients.moviepilot import MoviePilotClient
    from app.config import get_settings
    from app.core.episode_parser import is_video_file, parse_episode
    from app.core.inspector import _verified_video, compute_archive_target, _find_archive_season

    settings = get_settings()
    close_mp = False
    close_cd2 = False
    if mp_client is None and settings.mp_url and settings.mp_token:
        mp_client = MoviePilotClient()
        close_mp = True
    if cd2_client is None and settings.cd2_url:
        cd2_client = CloudDrive2Client()
        close_cd2 = True

    summary = {
        "ok": True,
        "checked_seasons": 0,
        "reconciled_in_library": 0,
        "released_to_todo": 0,
        "still_pending": 0,
        "mp_query_ok": False,
        "warnings": [],
    }

    try:
        active_downloads: list[dict[str, Any]] = []
        transfer_queue: list[dict[str, Any]] = []
        download_history: list[dict[str, Any]] = []
        transfer_history: list[dict[str, Any]] = []
        mp_query_ok = False

        if mp_client is not None:
            try:
                dl_res = await mp_client.get_active_downloads()
                if isinstance(dl_res, list):
                    active_downloads = dl_res
                    mp_query_ok = True
            except Exception as exc:
                summary["warnings"].append(f"读取 MP 活跃下载队列失败: {type(exc).__name__}")
            try:
                tq_res = await mp_client.get_transfer_queue()
                if isinstance(tq_res, list):
                    transfer_queue = tq_res
                elif isinstance(tq_res, dict):
                    transfer_queue = list(tq_res.get("data") or tq_res.get("items") or [])
            except Exception:
                pass
            try:
                if hasattr(mp_client, "get_download_history"):
                    dh_res = await mp_client.get_download_history(page=1, count=100)
                    if isinstance(dh_res, list):
                        download_history = dh_res
            except Exception:
                pass
            try:
                th_res = await mp_client.get_transfer_history(page=1, count=100)
                if isinstance(th_res, list):
                    transfer_history = th_res
            except Exception:
                pass

        summary["mp_query_ok"] = mp_query_ok

        stmt = select(SeasonRecord).where(SeasonRecord.task_id == task_id)
        if season_record_ids:
            stmt = stmt.where(SeasonRecord.id.in_(season_record_ids))
        elif season_record_id:
            stmt = stmt.where(SeasonRecord.id == season_record_id)
        seasons = list((await db.execute(stmt)).scalars().all())

        archive_cache: dict[str, list[dict]] = {}
        now_dt = datetime.now(timezone.utc)

        for rec in seasons:
            if rec.status in ("manually_handled", "verified_complete", "cleaned_redundant", "source_absent"):
                continue

            mapping = _parse_json_safe(rec.mapping_json, None) or None
            identity_key = build_identity_key(rec.show_name, rec.tmdb_id, rec.season_number, mapping)
            npath = _norm_path(rec.season_path)

            in_flight_rows = list(
                (
                    await db.execute(
                        select(GapFillEpisode).where(
                            GapFillEpisode.task_id == task_id,
                            GapFillEpisode.season_path == npath,
                            GapFillEpisode.identity_key == identity_key,
                            GapFillEpisode.state.in_(list(IN_FLIGHT_RECONCILE_STATES)),
                        )
                    )
                ).scalars().all()
            )
            # 若尚无新版 DB 记录但存在历史 OperationLog 推断，先物化进 GapFillEpisode 以便完成对账闭环
            if not in_flight_rows:
                view_snap = await compute_season_gap_fill_view(db, rec)
                if view_snap.get("unverified_episodes"):
                    tid_int = int(rec.tmdb_id) if rec.tmdb_id and str(rec.tmdb_id).isdigit() else None
                    st_tid = int(mapping["target_tmdb_id"]) if mapping and mapping.get("target_tmdb_id") else tid_int
                    st_sn = int(mapping["target_season"]) if mapping and mapping.get("target_season") else rec.season_number
                    for ep_d in view_snap.get("episodes") or []:
                        if ep_d.get("is_legacy_inferred"):
                            row = GapFillEpisode(
                                task_id=task_id,
                                season_record_id=rec.id,
                                season_path=npath,
                                show_name=rec.show_name,
                                tmdb_id=tid_int,
                                search_tmdb_id=st_tid,
                                season_number=rec.season_number,
                                search_season_number=st_sn,
                                numbering_mode=str((mapping or {}).get("numbering_mode") or "standard"),
                                identity_key=identity_key,
                                episode_number=int(ep_d["episode"]),
                                state="submitted_unverified",
                                outcome_code="legacy_inferred",
                                reason=str(ep_d.get("reason") or ""),
                                torrent_title=str(ep_d.get("torrent_title") or ""),
                                site_name=str(ep_d.get("site") or ""),
                                size_bytes=int(ep_d.get("size_bytes") or 0),
                                seeders=int(ep_d.get("seeds") or 0),
                                is_legacy_inferred=True,
                            )
                            db.add(row)
                            in_flight_rows.append(row)
                    if in_flight_rows:
                        await db.flush()
            if not in_flight_rows:
                continue

            summary["checked_seasons"] += 1

            # 1. 强读双库最新物理已覆盖集号（源季目录 + 归档季目录）
            present_eps: set[int] = set(_parse_json_safe(rec.recognized_episodes_json, []))
            cd2_checked = False
            if cd2_client is not None:
                try:
                    src_files = await cd2_client.list_files(rec.season_path, force_refresh=True)
                    src_eps: set[int] = set()
                    for item in src_files:
                        if item.get("isDirectory") or not is_video_file(item["name"]):
                            continue
                        ep = parse_episode(item["name"], item.get("path", ""), item.get("size", 0))
                        if _verified_video(ep, rec.season_number):
                            src_eps.update(ep.episode_numbers)
                    present_eps = set(src_eps)
                    cd2_checked = True

                    if rec.archive_target_path:
                        try:
                            arch_files = await cd2_client.list_files(rec.archive_target_path, force_refresh=True)
                            arch_sn = int((mapping or {}).get("target_season") or rec.season_number)
                            for item in arch_files:
                                if item.get("isDirectory") or not is_video_file(item["name"]):
                                    continue
                                ep = parse_episode(item["name"], item.get("path", ""), item.get("size", 0))
                                if mapping and _verified_video(ep, arch_sn):
                                    present_eps.update(translate_search_to_local_episodes(ep.episode_numbers, mapping))
                                elif _verified_video(ep, rec.season_number):
                                    present_eps.update(ep.episode_numbers)
                        except Exception:
                            pass
                    elif settings.archive_root:
                        expected_arc = compute_archive_target(rec.show_path, rec.season_path, settings.archive_root, settings.source_root)
                        tmdb_m = re.search(r"\[tmdbid[=:](\d+)\]", rec.show_name, re.I)
                        try:
                            actual, _ = await _find_archive_season(cd2_client, archive_cache, expected_arc, rec.season_number, tmdb_m)
                            if actual:
                                for item in await cd2_client.list_files(actual, force_refresh=True):
                                    if item.get("isDirectory") or not is_video_file(item["name"]):
                                        continue
                                    ep = parse_episode(item["name"], item.get("path", ""), item.get("size", 0))
                                    if _verified_video(ep, rec.season_number):
                                        present_eps.update(ep.episode_numbers)
                        except Exception:
                            pass
                except Exception as cd2_err:
                    summary["warnings"].append(f"核对 {rec.show_name} 网盘文件失败: {type(cd2_err).__name__}")

            if cd2_checked and rec.total_episodes and rec.total_episodes > 0:
                new_missing = [ep for ep in range(1, rec.total_episodes + 1) if ep not in present_eps]
                rec.recognized_episodes_json = json.dumps(sorted(present_eps))
                rec.missing_episodes_json = json.dumps(new_missing)
                if not new_missing and rec.status in ("download_needed", "period_missing"):
                    dups = _parse_json_safe(rec.duplicate_episodes_json, [])
                    rec.status = "review_needed" if dups else "archive_ready"
                    rec.status_detail = f"经核对入库，全 {rec.total_episodes} 集已齐全{'（含重复集待复核）' if dups else '，可归档'}"

            # 2. 对每条在途集做队列 / 历史 / 实物状态裁决
            for row in in_flight_rows:
                row.last_reconciled_at = now_dt
                if row.episode_number in present_eps:
                    row.state = "in_library"
                    row.outcome_code = "reconciled_in_library"
                    row.reason = "经核对入库，源端或归档库已存在该集实文件"
                    summary["reconciled_in_library"] += 1
                    continue

                if not mp_query_ok:
                    summary["still_pending"] += 1
                    continue

                in_queue = _match_torrent_in_items(row, active_downloads)
                if in_queue:
                    row.state = "queued"
                    row.outcome_code = "accepted"
                    row.reason = "MoviePilot 下载队列正在处理中"
                    summary["still_pending"] += 1
                    continue

                in_tq = _match_torrent_in_items(row, transfer_queue)
                if in_tq:
                    row.state = "transfer_pending"
                    row.outcome_code = "accepted"
                    row.reason = "已进入 MoviePilot 媒体整理队列，等待网盘实文件入库"
                    summary["still_pending"] += 1
                    continue

                dl_hist = _match_torrent_in_items(row, download_history)
                dl_hist_state = str((dl_hist or {}).get("state") or (dl_hist or {}).get("status") or "").casefold()
                if dl_hist and any(k in dl_hist_state for k in ("fail", "cancel", "delete", "error", "abort")):
                    row.state = "cancelled_or_failed"
                    row.outcome_code = "mp_cancelled_freed"
                    row.reason = f"MP 下载历史状态为 {dl_hist_state}，且双库无文件，已退回待补"
                    summary["released_to_todo"] += 1
                    continue

                tr_hist = _match_torrent_in_items(row, transfer_history)
                tr_status = str((tr_hist or {}).get("status") or "").casefold()
                if tr_hist and tr_status in ("true", "1", "success", "completed", "已整理"):
                    # MP 已整理完成但刚切库未出现实文件：先标待整理再观察，但若用户强制对账释放且刚重读 CD2 仍物理缺失 -> 可释放提示
                    if force_release_missing and cd2_checked:
                        row.state = "cancelled_or_failed"
                        row.outcome_code = "mp_cancelled_freed"
                        row.reason = "核对时双库实体目录仍无该集文件，已退回待补清单"
                        summary["released_to_todo"] += 1
                    else:
                        row.state = "transfer_pending"
                        row.reason = "MP 历史显示整理完成，待核实实体文件"
                        summary["still_pending"] += 1
                    continue

                # 队列、整理队列、转移成功记录均未命中 + 实体缺失：
                # 针对 submitted_unverified / intent_submitting 保留观察期（300 秒）：
                # 处于观察期内且未显式指定 force_release_missing 时，不因单次队列抖动误放回待补
                row_age = now_dt.timestamp() - _dt_ts(row.updated_at or row.created_at)
                in_observation_window = row.state in ("submitted_unverified", "intent_submitting") and row_age < 300
                if in_observation_window and not force_release_missing:
                    row.reason = f"提交回执待确认中（已提交 {int(max(0, row_age))} 秒，对账观察期 300 秒），保持在途锁定防重"
                    summary["still_pending"] += 1
                elif force_release_missing or row.state in ("submitted_unverified", "intent_submitting"):
                    row.state = "cancelled_or_failed"
                    row.outcome_code = "mp_cancelled_freed"
                    row.reason = "MP 队列已无在途下载且双库未见实文件（已过观察期或主动释放），已自动退回待补"
                    summary["released_to_todo"] += 1
                else:
                    summary["still_pending"] += 1

        await db.commit()
    finally:
        if close_mp and mp_client is not None:
            await mp_client.close()
        if close_cd2 and cd2_client is not None:
            await cd2_client.close()

    return summary


async def refresh_task_summary(task_id: str, db: AsyncSession) -> dict[str, Any]:
    """统一重算 Task.summary_json（按作品季互斥分流与唯一主待办归并）。"""
    task = await db.get(Task, task_id)
    stmt = select(SeasonRecord).where(SeasonRecord.task_id == task_id)
    res = await db.execute(stmt)
    all_seasons = [
        s
        for s in res.scalars().all()
        if s.status not in ("manually_handled", "verified_complete", "cleaned_redundant", "source_absent")
    ]

    import inspect as _inspect
    from app.models.directory_group import DirectoryGroup
    from app.core.season_classifier import consolidate_seasons_to_work_records

    scalars_fn = getattr(db, "scalars", None)
    dg_call = (
        scalars_fn(
            select(DirectoryGroup.id).where(
                DirectoryGroup.task_id == task_id,
                DirectoryGroup.status == "pending_confirm",
            )
        )
        if callable(scalars_fn)
        else None
    )
    pending_dir_group_ids = (
        set((await dg_call).all()) if _inspect.isawaitable(dg_call) else set()
    )

    records_with_gap_view: list[dict[str, Any]] = []
    for s in all_seasons:
        gap_view = None
        if s.status in ("download_needed", "period_missing") or s.missing_episodes_json:
            gap_view = await compute_season_gap_fill_view(db, s)
        records_with_gap_view.append({
            "id": s.id,
            "task_id": s.task_id,
            "show_name": s.show_name,
            "show_path": s.show_path,
            "tmdb_id": s.tmdb_id,
            "season_number": s.season_number,
            "season_path": s.season_path,
            "total_episodes": s.total_episodes,
            "local_episode_count": s.local_episode_count,
            "recognized_episodes": _parse_json_safe(s.recognized_episodes_json, []),
            "missing_episodes": _parse_json_safe(s.missing_episodes_json, []),
            "duplicate_episodes": _parse_json_safe(s.duplicate_episodes_json, []),
            "status": s.status,
            "status_detail": s.status_detail,
            "archive_target_path": s.archive_target_path,
            "season_finished": s.season_finished,
            "evidence": _parse_json_safe(getattr(s, "evidence_json", None), {}),
            "conflict": _parse_json_safe(getattr(s, "conflict_json", None), {}),
            "mapping": _parse_json_safe(getattr(s, "mapping_json", None), {}),
            "gap_fill_state": gap_view,
        })

    _, counts = consolidate_seasons_to_work_records(records_with_gap_view, pending_dir_group_ids)

    summary = {
        "archive_ready": counts["archive"],
        "review_needed": counts["review"],
        "download_needed": max(0, counts["download"] - counts["download_pushed"]),
        "download_pushed": counts["download_pushed"],
        "airing": counts["airing"],
        "archive_check": counts["anomaly"],
        "already_archived": 0,
        "total_seasons": counts["total_work_seasons"],
        "pending_directory_groups": len(pending_dir_group_ids),
    }
    if task:
        task.summary_json = json.dumps(summary, ensure_ascii=False)  # type: ignore[assignment]
    return summary
