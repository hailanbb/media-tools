"""人工确认（Manual Confirmation）与跨轮指纹差分（Delta Intelligence）核心逻辑。

严格遵循《MediaFlow异常剧集模块需求与交互方案-v2.5.37.md》第 6.1–6.4 节与第 7 节验收标准：
1. 独立保存来源：来源标注为 manual_confirmation，不写入 TMDB 官方缓存，不覆盖原始查询证据。
2. 逐项独立生效：
   - 仅确认作品归属正确 -> 解除候选/未收录归属质疑，总集数或播毕状态未知时分别显示「总集数待补充」或「播毕状态待确认」；
   - 确认仍在连载 -> 转入「连载留源」；
   - 确认总集数 + 已播毕 -> 结合实物覆盖、重复版本与其他门禁分流至「齐全可归档」「真缺集待补」「待复核清理」或「归档已齐可清源」；
   - 存在目录组待确认、混放作品、未核实分段等其他阻塞时 -> 保留在「异常剧集」并切换主提示为剩余阻塞。
3. 相同弱证据不推翻确认；新实质矛盾（如确认 4 集后发现正片 E05，或新增显式异作品 ID 文件）才暂停对应字段自动准入并保留仍有效字段。
4. 避免误继承：目录 ID、季号或作用范围变化且不再匹配时，不将旧确认套用到已改绑的新作品或新季。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.inspector import diagnose_season
from app.models.manual_confirmation import ManualSeasonConfirmation

CATEGORY_LABELS: dict[str, str] = {
    "archive": "齐全可归档",
    "download": "真缺集待补",
    "airing": "连载留源",
    "review": "待复核清理",
    "anomaly": "异常剧集",
}


def _safe_json(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def _norm_path(p: str | None) -> str:
    return str(p or "").strip().rstrip("/")


def compute_asset_fingerprint(
    *,
    recognized_episodes: list[int] | None = None,
    duplicate_episodes: list[int] | None = None,
    local_episode_count: int = 0,
    file_signatures: list[str] | None = None,
    archived_episodes: list[int] | None = None,
    total_episodes: int | None = None,
    season_finished: bool | None = None,
    meta_status: str = "",
    broadcast_status: str = "",
) -> str:
    """计算季级资产与元数据版本指纹 Fingerprint = Hash(文件明细, 元数据版本)。"""
    payload = {
        "rec_eps": sorted({int(x) for x in (recognized_episodes or []) if str(x).lstrip("-").isdigit()}),
        "dup_eps": sorted({int(x) for x in (duplicate_episodes or []) if str(x).lstrip("-").isdigit()}),
        "local_cnt": int(local_episode_count or 0),
        "files": sorted(str(s) for s in (file_signatures or []) if s),
        "arc_eps": sorted({int(x) for x in (archived_episodes or []) if str(x).lstrip("-").isdigit()}),
        "total": int(total_episodes) if (total_episodes is not None and int(total_episodes or 0) > 0) else None,
        "finished": bool(season_finished) if season_finished is not None else None,
        "meta_status": str(meta_status or ""),
        "b_status": "" if str(broadcast_status or "") == "evidence_pending" else str(broadcast_status or ""),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def orm_confirmation_to_dict(row: ManualSeasonConfirmation) -> dict[str, Any]:
    return {
        "id": row.id,
        "scope_key": row.scope_key,
        "task_id": row.task_id,
        "season_record_id": row.season_record_id,
        "show_path": row.show_path,
        "season_path": row.season_path,
        "scope_paths": _safe_json(row.scope_paths_json, []),
        "show_name": row.show_name,
        "tmdb_id": str(row.tmdb_id or ""),
        "season_number": int(row.season_number or 1),
        "identity_confirmed": bool(row.identity_confirmed),
        "confirmed_tmdb_id": row.confirmed_tmdb_id,
        "confirmed_season_number": row.confirmed_season_number or int(row.season_number or 1),
        "confirmed_total_episodes": row.confirmed_total_episodes,
        "broadcast_status": str(row.broadcast_status or "unknown"),
        "note": str(row.note or ""),
        "source": "manual_confirmation",
        "source_label": "人工确认",
        "recognized_snapshot": _safe_json(row.recognized_snapshot_json, []),
        "contradiction": _safe_json(row.contradiction_json, {}),
        "confirmed_at": (row.updated_at or row.created_at or datetime.now(timezone.utc)).isoformat(),
    }


async def resolve_active_manual_confirmation(
    db: AsyncSession | None,
    *,
    tmdb_id: str | int | None,
    season_number: int,
    season_path: str = "",
    show_path: str = "",
) -> dict[str, Any] | None:
    """按作用范围查找当前季有效的人工确认记录（严格校验作品 ID 与季号防止误继承）。"""
    if db is None:
        return None
    norm_s_path = _norm_path(season_path)
    norm_sh_path = _norm_path(show_path)
    tid_str = str(tmdb_id or "").strip()

    rows = (
        await db.execute(
            select(ManualSeasonConfirmation)
            .where(ManualSeasonConfirmation.is_active.is_(True))
            .order_by(ManualSeasonConfirmation.updated_at.desc(), ManualSeasonConfirmation.id.desc())
        )
    ).scalars().all()

    for row in rows:
        row_s_path = _norm_path(row.season_path)
        row_sh_path = _norm_path(row.show_path)
        scope_paths = [_norm_path(p) for p in _safe_json(row.scope_paths_json, []) if p]

        path_matched = False
        if norm_s_path and (norm_s_path == row_s_path or norm_s_path in scope_paths):
            path_matched = True
        elif norm_sh_path and norm_sh_path == row_sh_path and int(row.season_number) == int(season_number):
            path_matched = True

        if not path_matched:
            continue

        # 避免误继承：若当前记录季号与确认记录季号及确认目标季号均不符，不继承
        valid_seasons = {int(row.season_number)}
        if row.confirmed_season_number:
            valid_seasons.add(int(row.confirmed_season_number))
        if int(season_number) not in valid_seasons:
            continue

        # 避免误继承：若确认记录绑定了特定 TMDB ID，而当前目录已改绑为另一个不同的 TMDB ID，不继承
        valid_tids = {str(x) for x in (row.tmdb_id, row.confirmed_tmdb_id) if x is not None and str(x).strip()}
        if tid_str and valid_tids and tid_str not in valid_tids:
            continue

        return orm_confirmation_to_dict(row)

    return None


def evaluate_confirmation_outcome(
    *,
    conf: dict[str, Any],
    tmdb_id: str | int | None,
    season_number: int,
    recognized_episodes: list[int],
    archived_episodes: list[int],
    emby_episodes: list[int],
    duplicate_episodes: list[int],
    current_total_episodes: int | None,
    current_season_finished: bool | None,
    current_status: str,
    current_status_detail: str,
    evidence: dict[str, Any],
    conflict: dict[str, Any],
    pending_dir_group: bool = False,
) -> dict[str, Any]:
    """根据人工确认记录与当前实物证据计算季状态、主分类、矛盾检测与预告说明。"""
    pos_rec = sorted({int(x) for x in (recognized_episodes or []) if str(x).isdigit() and int(x) > 0})
    pos_arc = sorted({int(x) for x in (archived_episodes or []) if str(x).isdigit() and int(x) > 0})
    pos_emby = sorted({int(x) for x in (emby_episodes or []) if str(x).isdigit() and int(x) > 0})
    pos_dups = sorted({int(x) for x in (duplicate_episodes or []) if str(x).isdigit() and int(x) > 0})
    combined_eps = sorted(set(pos_rec) | set(pos_arc) | set(pos_emby))
    max_ep = max(combined_eps, default=0)

    id_confirmed = bool(conf.get("identity_confirmed"))
    conf_tot = conf.get("confirmed_total_episodes")
    conf_tot_int = int(conf_tot) if (conf_tot is not None and int(conf_tot or 0) > 0) else None
    b_status_conf = str(conf.get("broadcast_status") or "unknown")

    rel_ev = (evidence or {}).get("release_evidence") or {}
    explicit_file_conflicts = list(rel_ev.get("explicit_file_tmdb_conflicts") or [])
    mixed_works = bool(
        (conflict or {}).get("kind") == "mixed_works"
        or rel_ev.get("mixed_works")
    )
    is_segmented_unverified = (
        current_status == "archive_check"
        or (current_status == "period_review" and bool(((evidence or {}).get("period_evidence") or {}).get("is_segmented")))
    )

    contradictions: list[str] = []
    total_contradiction = False
    identity_contradiction = False

    if conf_tot_int is not None and max_ep > conf_tot_int:
        total_contradiction = True
        contradictions.append(
            f"原人工确认本季总集数为 {conf_tot_int} 集，但发现正片 E{max_ep:02d} 超出已确认总集数（已暂停基于总集数 {conf_tot_int} 的自动准入，不自动舍弃超号文件）"
        )

    target_tid_str = str(conf.get("confirmed_tmdb_id") or tmdb_id or "").strip()
    if id_confirmed and explicit_file_conflicts:
        bad_fids = sorted({str(c.get("file_tmdb_id") or "") for c in explicit_file_conflicts if str(c.get("file_tmdb_id") or "") != target_tid_str})
        if bad_fids:
            identity_contradiction = True
            contradictions.append(
                f"原人工确认归属 TMDB {target_tid_str or '?'}，但文件显式标注异作品 TMDB ID ({', '.join(bad_fids)})，请核对文件归属"
            )

    eff_id_confirmed = id_confirmed and not identity_contradiction
    eff_total = None if total_contradiction else (conf_tot_int if conf_tot_int is not None else current_total_episodes)
    # 若原记录因 metadata_unverified 清空了 total_episodes 且用户未确认新总数，保持 None
    if not conf_tot_int and (evidence or {}).get("meta_status") == "metadata_unverified":
        eff_total = None

    b_ev = dict((evidence or {}).get("broadcast_evidence") or {})
    official_schedule_verified = bool(b_ev.get("schedule_verified") and b_ev.get("status") in ("airing", "upcoming", "finished"))

    if b_status_conf == "finished":
        eff_finished = True
        b_resolved = True
    elif b_status_conf == "airing":
        eff_finished = False
        b_resolved = True
    elif official_schedule_verified:
        eff_finished = bool(current_season_finished)
        b_resolved = True
    else:
        eff_finished = False
        b_resolved = False

    contradiction_obj = {
        "has_contradiction": bool(contradictions),
        "total_contradiction": total_contradiction,
        "identity_contradiction": identity_contradiction,
        "reasons": contradictions,
    }

    # 1. 若存在新实质矛盾（如 E05 > 确认总数 4，或显式文件异作品 ID）
    if total_contradiction or identity_contradiction:
        new_status = "identity_review" if identity_contradiction else "review_needed"
        new_detail = "；".join(contradictions)
        return {
            "status": new_status,
            "status_detail": new_detail,
            "total_episodes": conf_tot_int if not total_contradiction else current_total_episodes,
            "season_finished": eff_finished,
            "missing_episodes": [],
            "predicted_category": "anomaly",
            "predicted_category_label": CATEGORY_LABELS["anomaly"],
            "blocking_reason": new_detail,
            "identity_cleared": eff_id_confirmed,
            "contradiction": contradiction_obj,
        }

    # 2. 若存在同作品混放或分段/目录未核实阻塞，不因人工确认掩盖
    if mixed_works:
        msg = (conflict or {}).get("message") or "同季目录混放多个发行作品文件，请先拆分后处理。"
        return {
            "status": "identity_review",
            "status_detail": msg,
            "total_episodes": eff_total,
            "season_finished": eff_finished,
            "missing_episodes": [],
            "predicted_category": "anomaly",
            "predicted_category_label": CATEGORY_LABELS["anomaly"],
            "blocking_reason": msg,
            "identity_cleared": False,
            "contradiction": contradiction_obj,
        }

    if is_segmented_unverified:
        return {
            "status": current_status,
            "status_detail": current_status_detail,
            "total_episodes": eff_total,
            "season_finished": eff_finished,
            "missing_episodes": [],
            "predicted_category": "anomaly",
            "predicted_category_label": CATEGORY_LABELS["anomaly"],
            "blocking_reason": "期号与分段映射或目录未核实内容仍待处理，总集数确认不代替分段核实",
            "identity_cleared": eff_id_confirmed,
            "contradiction": contradiction_obj,
        }

    # 3. 确认仍在连载 -> 转入连载留源（若仍存在待确认多目录组则主分类暂留异常，主操作为确认目录）
    if b_status_conf == "airing":
        new_status = "airing"
        tot_txt = f"全季 {eff_total} 集" if eff_total else "总集数待公布"
        new_detail = f"已按人工确认（仍在连载 · {tot_txt}）保留源目录跟踪：当前已收 {len(combined_eps)} 集。"
        pred_cat = "anomaly" if pending_dir_group else "airing"
        block_rsn = "存在待确认归档目标目录组，请先确认目录" if pending_dir_group else ""
        return {
            "status": new_status,
            "status_detail": new_detail,
            "total_episodes": eff_total,
            "season_finished": False,
            "missing_episodes": [],
            "predicted_category": pred_cat,
            "predicted_category_label": CATEGORY_LABELS[pred_cat],
            "blocking_reason": block_rsn,
            "identity_cleared": eff_id_confirmed,
            "contradiction": contradiction_obj,
        }

    # 4. 仅确认作品归属正确，但总集数或播毕状态仍未知
    if not eff_total or eff_total <= 0:
        new_status = "unknown"
        rec_txt = f"已识别 E{pos_rec[0]:02d}–E{pos_rec[-1]:02d}（共 {len(pos_rec)} 集）" if pos_rec else "暂未识别正片集号"
        new_detail = f"已人工确认作品归属（TMDB {target_tid_str or '?'} S{season_number:02d}）；{rec_txt}，本季总集数待补充，文件安全保留。"
        return {
            "status": new_status,
            "status_detail": new_detail,
            "total_episodes": None,
            "season_finished": eff_finished,
            "missing_episodes": [],
            "predicted_category": "anomaly",
            "predicted_category_label": CATEGORY_LABELS["anomaly"],
            "blocking_reason": "本季总集数待补充",
            "identity_cleared": True,
            "contradiction": contradiction_obj,
        }

    if not b_resolved:
        new_status = "period_review"
        new_detail = (
            f"已人工确认本季共 {eff_total} 集（已收 {len(combined_eps)}/{eff_total} 集）；"
            "本季播毕状态待确认，文件保留在源目录。"
        )
        return {
            "status": new_status,
            "status_detail": new_detail,
            "total_episodes": eff_total,
            "season_finished": False,
            "missing_episodes": [],
            "predicted_category": "anomaly",
            "predicted_category_label": CATEGORY_LABELS["anomaly"],
            "blocking_reason": "播毕状态待确认",
            "identity_cleared": eff_id_confirmed,
            "contradiction": contradiction_obj,
        }

    # 5. 总集数 > 0 且已确认播毕 (eff_finished = True)
    expected_set = set(range(1, eff_total + 1))
    missing = sorted(expected_set - set(combined_eps))

    if pos_arc and expected_set <= set(pos_arc) and pos_rec and set(pos_rec) <= set(pos_arc):
        new_status = "already_archived"
        new_detail = f"已按人工确认（共 {eff_total} 集，已播毕）核算：归档单端已独立覆盖全 {eff_total} 集，源端本季可核验清源"
        pred_cat = "anomaly"
    elif missing:
        new_status = "download_needed"
        miss_txt = ", ".join(f"E{e:02d}" for e in missing[:8])
        new_detail = f"已按人工确认（共 {eff_total} 集，已播毕）核算：三端合计已覆盖 {len(combined_eps)}/{eff_total} 集，实缺 {miss_txt}"
        pred_cat = "anomaly" if pending_dir_group else "download"
    elif pos_dups:
        new_status = "review_needed"
        new_detail = f"已按人工确认（共 {eff_total} 集，已播毕）核算：全 {eff_total} 集已齐，第 {', '.join(map(str, pos_dups))} 集存在重复版本待清理"
        pred_cat = "anomaly" if pending_dir_group else "review"
    else:
        new_status, diag_det = diagnose_season(
            recognized=combined_eps,
            total_episodes=eff_total,
            season_finished=True,
            has_duplicates=False,
            duplicate_episodes=[],
            allow_bonus_specials=True,
        )
        new_detail = f"已按人工确认（共 {eff_total} 集，已播毕）核算：{diag_det}"
        pred_cat = "anomaly" if pending_dir_group else ("archive" if new_status == "archive_ready" else "anomaly")

    block_rsn = "存在待确认归档目标目录组，请先确认目录" if pending_dir_group else ""
    return {
        "status": new_status,
        "status_detail": new_detail,
        "total_episodes": eff_total,
        "season_finished": True,
        "missing_episodes": missing,
        "predicted_category": pred_cat,
        "predicted_category_label": CATEGORY_LABELS[pred_cat],
        "blocking_reason": block_rsn,
        "identity_cleared": eff_id_confirmed,
        "contradiction": contradiction_obj,
    }
