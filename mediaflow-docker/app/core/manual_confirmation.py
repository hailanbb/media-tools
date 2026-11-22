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
    "download": "缺集待补",
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

    try:
        exec_res = await db.execute(
            select(ManualSeasonConfirmation)
            .where(ManualSeasonConfirmation.is_active.is_(True))
            .order_by(ManualSeasonConfirmation.updated_at.desc(), ManualSeasonConfirmation.id.desc())
        )
        if hasattr(exec_res, "scalars"):
            sc = exec_res.scalars()
            rows = sc.all() if hasattr(sc, "all") and callable(sc.all) else []
        else:
            rows = []
    except Exception:
        rows = []

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
        new_detail = f"已按人工确认（共 {eff_total} 集，已播毕）核算：归档单端已独立覆盖全 {eff_total} 集，源端本季可在待复核清理核验清源"
        pred_cat = "review"
    elif pos_dups:
        new_status = "download_needed" if missing else "review_needed"
        new_detail = (
            f"已按人工确认（共 {eff_total} 集，已播毕）核算：第 {', '.join(map(str, pos_dups))} 集存在重复副本，优先进入待复核清理"
            + (f"（清理后转缺集待补，缺 {len(missing)} 集）" if missing else "")
        )
        pred_cat = "review"
    elif missing:
        new_status = "download_needed"
        miss_txt = ", ".join(f"E{e:02d}" for e in missing[:8])
        new_detail = f"已按人工确认（共 {eff_total} 集，已播毕）核算：三端合计已覆盖 {len(combined_eps)}/{eff_total} 集，实缺 {miss_txt}"
        pred_cat = "review" if pending_dir_group else "download"
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
        pred_cat = "review" if pending_dir_group else ("archive" if new_status == "archive_ready" else "anomaly")

    block_rsn = "存在待规范化归并的同 ID 多目录组" if pending_dir_group else ""
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


def _rewrite_single_season_tag(filename: str, old_season: int, new_season: int) -> tuple[str | None, str]:
    """精准改写文件名中唯一明确代表本季的 S 标记，保留集号与全部发行后缀；多标记歧义返回 None 禁止盲改 (G18)。"""
    import re
    pat = re.compile(rf"(?i)(?<![a-zA-Z0-9])([Ss])(0?{old_season})([Ee]\d{{1,4}}(?:-[Ee]?\d{{1,4}})?)")
    matches = list(pat.finditer(filename))
    if not matches:
        return filename, ""
    if len(matches) > 1:
        return None, f"文件名含多个季集标记，为防误改已保持原名：{filename}"
    m = matches[0]
    new_tag = f"S{new_season:02d}{m.group(3)}"
    rewritten = filename[:m.start()] + new_tag + filename[m.end():]
    return rewritten, f"季号标记 S{old_season:02d} -> S{new_season:02d}"


def _rewrite_prefix_for_identity(filename: str, season_number: int, new_prefix: str) -> tuple[str | None, str]:
    """在身份/规范名称变更时统一季集标记前的展示前缀，保留 SxxExx 及之后全部文本 (G17)。"""
    from app.core.directory_merge import split_season_episode_boundary
    boundary = split_season_episode_boundary(filename, season_number)
    if not boundary:
        return filename, ""
    old_prefix, suffix = boundary
    if old_prefix == new_prefix:
        return filename, ""
    return f"{new_prefix}{suffix}", f"展示前缀统一为「{new_prefix}」"


def update_nfo_identity_structured(
    nfo_path: str,
    *,
    tmdb_id: int | str | None = None,
    douban_id: str | None = None,
    season_number: int | None = None,
    title: str | None = None,
) -> bool:
    """结构化更新单季/分集 NFO 的身份字段，完整保留用户备注与无关 XML 字段 (Section 5.3)。"""
    import os
    import xml.etree.ElementTree as ET

    if not nfo_path or not os.path.isfile(nfo_path):
        return False
    try:
        tree = ET.parse(nfo_path)
        root = tree.getroot()
    except Exception:
        return False

    changed = False
    if tmdb_id is not None and str(tmdb_id).isdigit():
        tid_str = str(tmdb_id)
        t_el = root.find("tmdbid")
        if t_el is not None:
            if t_el.text != tid_str:
                t_el.text = tid_str
                changed = True
        else:
            t_el = ET.SubElement(root, "tmdbid")
            t_el.text = tid_str
            changed = True
        uid_tmdb = next((u for u in root.findall("uniqueid") if str(u.attrib.get("type") or "").lower() == "tmdb"), None)
        if uid_tmdb is not None:
            if uid_tmdb.text != tid_str:
                uid_tmdb.text = tid_str
                changed = True
        else:
            u_el = ET.SubElement(root, "uniqueid", {"type": "tmdb"})
            u_el.text = tid_str
            changed = True

    if douban_id and str(douban_id).isdigit():
        did_str = str(douban_id)
        uid_db = next((u for u in root.findall("uniqueid") if str(u.attrib.get("type") or "").lower() == "douban"), None)
        if uid_db is not None:
            if uid_db.text != did_str:
                uid_db.text = did_str
                changed = True
        else:
            u_el = ET.SubElement(root, "uniqueid", {"type": "douban"})
            u_el.text = did_str
            changed = True

    if season_number is not None and root.tag in ("episodedetails", "season"):
        s_el = root.find("season")
        if s_el is not None and s_el.text != str(season_number):
            s_el.text = str(season_number)
            changed = True

    if title and root.tag == "tvshow":
        ti_el = root.find("title")
        if ti_el is not None and ti_el.text != title:
            ti_el.text = title
            changed = True

    if changed:
        tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
    return changed


def build_manual_correction_plan(
    *,
    show_name: str,
    show_path: str,
    season_path: str,
    current_tmdb_id: int | str | None,
    current_season: int,
    target_tmdb_id: int | None = None,
    target_title: str = "",
    target_first_air_year: str = "",
    target_season: int | None = None,
    douban_id: str | None = None,
    episode_mapping: dict[str, int] | None = None,
    season_files: list[dict[str, Any]] | None = None,
    sibling_seasons: list[str] | None = None,
) -> dict[str, Any]:
    """为单季人工修正生成固定可执行计划（区分纯资料确认 G19 vs 身份改名迁移 G17 vs 季号修正 G18）。"""
    import posixpath
    import re
    from uuid import uuid4
    from app.core.directory_merge import build_canonical_show_dir_name, normalize_dir_path, sanitize_fs_name
    from app.core.episode_parser import is_subtitle_file, is_video_file

    norm_show = normalize_dir_path(show_path)
    norm_season = normalize_dir_path(season_path)
    cur_tid = str(current_tmdb_id or "").strip()
    new_tid = str(target_tmdb_id) if (target_tmdb_id and int(target_tmdb_id) > 0) else cur_tid
    new_sn = int(target_season) if (target_season and int(target_season) > 0) else int(current_season)

    current_names = {sanitize_fs_name(show_name), sanitize_fs_name(show_name.split(" [tmdbid=")[0]),
                     sanitize_fs_name(show_name.split(" [tmdbid=")[0].split(" (")[0])}
    identity_changed = bool(new_tid and new_tid != cur_tid) or bool(
        target_title and sanitize_fs_name(target_title) and sanitize_fs_name(target_title) not in current_names
    )
    season_changed = (new_sn != int(current_season))
    has_ep_mapping = bool(episode_mapping)

    if not identity_changed and not season_changed and not has_ep_mapping:
        return {
            "plan_id": f"plan_fix_{uuid4().hex[:10]}",
            "has_file_changes": False,
            "identity_changed": False,
            "season_changed": False,
            "new_show_name": show_name,
            "new_show_path": norm_show,
            "new_season_path": norm_season,
            "new_season_number": new_sn,
            "new_tmdb_id": new_tid,
            "keep_old_show_root": True,
            "file_changes": [],
            "nfo_updates": [],
            "ambiguous_files": [],
            "summary_text": "仅确认元数据事实，无目录或文件变更",
        }

    def _p_dirname(p: str) -> str:
        s = str(p or "").rstrip("/\\")
        if "\\" in s and "/" not in s:
            import ntpath
            return ntpath.dirname(s)
        return posixpath.dirname(s.replace("\\", "/"))

    def _p_basename(p: str) -> str:
        s = str(p or "").rstrip("/\\")
        if "\\" in s and "/" not in s:
            import ntpath
            return ntpath.basename(s)
        return posixpath.basename(s.replace("\\", "/"))

    def _p_join(base: str, child: str) -> str:
        if not base:
            return child
        sep = "\\" if ("\\" in base and "/" not in base) else "/"
        clean_base = base.rstrip("/\\")
        return f"{clean_base}{sep}{child}"

    # 计算新规范剧目录与新季目录
    parent_dir = _p_dirname(norm_show) if norm_show else ""
    if identity_changed:
        canon_dir_name, naming_basis = build_canonical_show_dir_name(
            canonical_title=target_title or show_name,
            first_air_year=target_first_air_year,
            tmdb_id=new_tid,
            existing_name=_p_basename(norm_show) if norm_show else show_name,
        )
        new_show_path = _p_join(parent_dir, canon_dir_name) if parent_dir else canon_dir_name
        new_show_name = canon_dir_name
        clean_title_prefix = f"{sanitize_fs_name(target_title or canon_dir_name.split(' [tmdbid=')[0].split(' (')[0])} - "
    else:
        new_show_path = norm_show
        new_show_name = show_name
        naming_basis = "保持原剧目录"
        clean_title_prefix = ""

    new_season_path = _p_join(new_show_path, f"Season {new_sn}") if new_show_path else f"Season {new_sn}"
    has_siblings = bool(sibling_seasons)
    # 若旧剧根还有其他季，绝对保留旧剧根且严禁修改旧剧根共享 tvshow.nfo (G18)
    keep_old_show_root = has_siblings or (new_show_path == norm_show)

    file_changes: list[dict[str, Any]] = []
    nfo_updates: list[dict[str, Any]] = []
    ambiguous_files: list[dict[str, Any]] = []

    for f in (season_files or []):
        if f.get("isDirectory"):
            continue
        fname = str(f.get("name") or "")
        fpath = normalize_dir_path(f.get("path") or f"{norm_season}/{fname}")
        new_name = fname
        reasons: list[str] = []

        # 1. 季号修正（先改 S 标记）
        if season_changed:
            rewritten, r_txt = _rewrite_single_season_tag(new_name, int(current_season), new_sn)
            if rewritten is None:
                ambiguous_files.append({"old_name": fname, "old_path": fpath, "reason": r_txt})
            else:
                new_name = rewritten
                if r_txt:
                    reasons.append(r_txt)

        # 2. 身份/剧名变更（统一前缀）
        if identity_changed and clean_title_prefix and (is_video_file(fname) or is_subtitle_file(fname) or fname.lower().endswith(".nfo")):
            rewritten_p, rp_txt = _rewrite_prefix_for_identity(new_name, new_sn, clean_title_prefix)
            if rewritten_p:
                new_name = rewritten_p
                if rp_txt:
                    reasons.append(rp_txt)

        # 3. 明确确认的集号映射修正（只改确认条目，保留审计）
        if has_ep_mapping and episode_mapping:
            m_ep = re.search(rf"(?i)(?<![a-zA-Z0-9])([Ss]0*{new_sn}[Ee])(\d{{1,4}})(?!\d)", new_name)
            if m_ep:
                old_ep_str = str(int(m_ep.group(2)))
                if old_ep_str in episode_mapping or m_ep.group(2) in episode_mapping:
                    mapped_ep = int(episode_mapping.get(old_ep_str, episode_mapping.get(m_ep.group(2))))
                    new_name = new_name[:m_ep.start()] + f"{m_ep.group(1)}{mapped_ep:02d}" + new_name[m_ep.end():]
                    reasons.append(f"集号映射 E{int(old_ep_str):02d} -> E{mapped_ep:02d}")

        dest_path = f"{new_season_path}/{new_name}"
        if fpath != dest_path or fname != new_name:
            file_changes.append({
                "old_path": fpath,
                "old_name": fname,
                "new_dir": new_season_path,
                "new_name": new_name,
                "new_path": dest_path,
                "kind": "video" if is_video_file(fname) else ("subtitle" if is_subtitle_file(fname) else "attachment"),
                "reason": "；".join(reasons) if reasons else "随单季迁移至目标目录",
            })

        if fname.lower().endswith(".nfo"):
            nfo_updates.append({
                "path": dest_path,
                "tmdb_id": new_tid,
                "douban_id": douban_id,
                "season_number": new_sn,
            })

    summary_parts = []
    if identity_changed:
        summary_parts.append(f"迁入新剧目录「{new_show_name}」")
    if season_changed:
        summary_parts.append(f"调整季号 S{int(current_season):02d} -> S{new_sn:02d}")
    if file_changes:
        summary_parts.append(f"同步移动/改名 {len(file_changes)} 个文件")
    if keep_old_show_root and has_siblings:
        summary_parts.append(f"保留旧剧根其余 {len(sibling_seasons or [])} 个季")

    return {
        "plan_id": f"plan_fix_{uuid4().hex[:10]}",
        "has_file_changes": bool(file_changes or norm_season != new_season_path),
        "identity_changed": identity_changed,
        "season_changed": season_changed,
        "naming_basis": naming_basis,
        "old_show_path": norm_show,
        "old_season_path": norm_season,
        "new_show_name": new_show_name,
        "new_show_path": new_show_path,
        "new_season_path": new_season_path,
        "new_season_number": new_sn,
        "new_tmdb_id": new_tid,
        "keep_old_show_root": keep_old_show_root,
        "sibling_seasons": list(sibling_seasons or []),
        "file_changes": file_changes,
        "nfo_updates": nfo_updates,
        "ambiguous_files": ambiguous_files,
        "summary_text": "，".join(summary_parts) or "将按计划更新目录与文件",
    }


async def execute_manual_correction_plan(
    plan: dict[str, Any],
    *,
    cd2_client: Any = None,
    progress: Any = None,
) -> dict[str, Any]:
    """执行单季人工修正的实物移动、改名、结构化 NFO 更新与核验，部分失败记录剩余步骤支持恢复 (G17, G18, G23)。"""
    import os
    import posixpath
    from app.core.directory_merge import (
        delete_dir_robust,
        list_files_robust,
        move_and_rename_file_robust,
    )

    if not plan.get("has_file_changes"):
        return {
            "ok": True,
            "completed_steps": [],
            "remaining_steps": [],
            "updated_nfos": [],
            "cleaned_dirs": [],
            "partial_failure": False,
        }

    completed_steps: list[dict[str, Any]] = []
    remaining_steps: list[dict[str, Any]] = []
    updated_nfos: list[str] = []
    cleaned_dirs: list[str] = []
    errors: list[str] = []
    prepared_dirs: dict[str, set[str]] = {}
    steps = plan.get("file_changes") or []

    # 1. 逐文件幂等移动/改名
    for index, step in enumerate(steps):
        if progress:
            progress("正在迁移／改名", index, len(steps))
        old_p = step["old_path"]
        new_dir = step["new_dir"]
        new_name = step["new_name"]
        new_p = step["new_path"]
        try:
            if os.path.exists(new_p) and not os.path.exists(old_p):
                completed_steps.append(step)
                continue
            await move_and_rename_file_robust(cd2_client, old_p, new_dir, new_name, prepared_dirs=prepared_dirs)
            completed_steps.append(step)
        except Exception as e:
            # 失败可能发生在移动后、改名前，下一步须重新读取目标，不能复用旧快照。
            prepared_dirs.clear()
            remaining_steps.append({**step, "error": str(e)})
            errors.append(f"{step['old_name']} -> {new_name}: {e}")

    if progress:
        progress("正在核验文件修正结果", len(completed_steps), len(steps))

    if remaining_steps:
        return {
            "ok": False,
            "partial_failure": True,
            "completed_steps": completed_steps,
            "remaining_steps": remaining_steps,
            "updated_nfos": updated_nfos,
            "cleaned_dirs": cleaned_dirs,
            "errors": errors,
        }

    # 2. 结构化更新属于本季的已知 NFO 身份字段（严禁修改旧剧根共享 tvshow.nfo）
    for nfo_item in (plan.get("nfo_updates") or []):
        n_path = nfo_item.get("path") or ""
        if n_path and update_nfo_identity_structured(
            n_path,
            tmdb_id=nfo_item.get("tmdb_id"),
            douban_id=nfo_item.get("douban_id"),
            season_number=nfo_item.get("season_number"),
        ):
            updated_nfos.append(n_path)

    # 3. 实物后验：确认移动后的文件已在目标季目录中
    new_season_path = plan.get("new_season_path") or ""
    if new_season_path and completed_steps:
        after_files = await list_files_robust(cd2_client, new_season_path)
        after_names = {f["name"] for f in after_files}
        missing_after = [s for s in completed_steps if s["new_name"] not in after_names]
        if missing_after:
            return {
                "ok": False,
                "partial_failure": True,
                "completed_steps": [s for s in completed_steps if s not in missing_after],
                "remaining_steps": missing_after,
                "updated_nfos": updated_nfos,
                "cleaned_dirs": cleaned_dirs,
                "errors": [f"后验未见目标文件: {[m['new_name'] for m in missing_after]}"],
            }

    # 4. 清理空旧季目录；仅在不保留旧剧根且旧剧根确已为空时清理旧剧根
    old_season_path = plan.get("old_season_path") or ""
    old_show_path = plan.get("old_show_path") or ""
    if old_season_path and old_season_path != new_season_path:
        if await delete_dir_robust(cd2_client, old_season_path):
            cleaned_dirs.append(old_season_path)
    if old_show_path and not plan.get("keep_old_show_root") and old_show_path != plan.get("new_show_path"):
        if await delete_dir_robust(cd2_client, old_show_path):
            cleaned_dirs.append(old_show_path)

    # 5. 刷新旧新目录父级缓存
    if cd2_client is not None:
        for ref_dir in {
            new_season_path,
            plan.get("new_show_path") or "",
            posixpath.dirname(plan.get("new_show_path") or "") if plan.get("new_show_path") else "",
            posixpath.dirname(old_show_path) if old_show_path else "",
        }:
            if ref_dir:
                try:
                    await cd2_client.list_files(ref_dir, force_refresh=True)
                except Exception:
                    pass

    return {
        "ok": True,
        "partial_failure": False,
        "completed_steps": completed_steps,
        "remaining_steps": [],
        "updated_nfos": updated_nfos,
        "cleaned_dirs": cleaned_dirs,
        "errors": [],
    }
