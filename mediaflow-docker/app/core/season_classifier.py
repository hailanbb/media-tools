"""MediaFlow 五模块互斥分流与作品季归并核心决策器.

严格遵循《MediaFlow五模块互斥分流与操作纠错方案-v2.5.23-2026-11-14.md》：
1. 五大主模块严格按优先级互斥分流（短路求值，绝不同时落入两处）：
   优先级 1: 异常剧集 (anomaly)
   优先级 2: 连载留源 (airing)
   优先级 3: 真缺集待补 (download)
   优先级 4: 待复核清理 (review)
   优先级 5: 齐全可归档 (archive)

2. 作品季维度唯一主待办归并：
   以 (tmdb_id, season_number)（未识别时为 (show_name, season_number)）为聚合主键。
   同一作品季无论存在几个物理目录，在初检主界面只呈现 1 个主待办卡片。
   主卡片取组内最优先模块作为该作品季的唯一主分类，其余物理记录作为关联记录挂载。

3. 数学不变量：
   catCountArchive + catCountReview + catCountDownload + catCountAiring + catCountAnomaly == total_work_seasons
   待确认目录组数作为异常剧集的后缀提示展示，绝不与季数混加。
"""

from __future__ import annotations

import json
from typing import Any, Sequence

# 分流优先级定义（数值越小优先级越高）
CATEGORY_PRIORITY: dict[str, int] = {
    "anomaly": 1,
    "airing": 2,
    "download": 3,
    "review": 4,
    "archive": 5,
}

# 属于异常剧集的状态集合
ANOMALY_STATUSES = {
    "identity_review",
    "period_review",
    "already_archived",
    "archive_check",
    "unknown",
    "conflict",
    "failed",
    "partial_archived",
    "archive_unverified",
}

# 属于已完结/终态的状态集合，不落入五大待办模块中的任何一类
TERMINAL_STATUSES = {
    "manually_handled",
    "verified_complete",
    "cleaned_redundant",
    "source_absent",
    "archived",
}

# 属于元数据异常的 meta_status
ANOMALY_META_STATUSES = {
    "metadata_unverified",
    "fetch_failed",
    "not_listed",
    "no_candidate",
    "episodes_tba",
    "query_failed",
    "not_found",
    "episodes_unannounced",
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


def get_record_field(rec: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(rec, dict):
        return rec.get(field_name, default)
    return getattr(rec, field_name, default)


def is_airing_protected(rec: Any) -> bool:
    evidence = _safe_json(get_record_field(rec, "evidence_json") or get_record_field(rec, "evidence"), {})
    return (get_record_field(rec, "status") == "airing"
            or (evidence.get("broadcast_evidence") or {}).get("status") in ("airing", "upcoming"))


def classify_single_record(
    rec: Any,
    pending_dir_group_ids: set[str] | None = None,
) -> tuple[str, str]:
    """判定单个物理剧季记录的基础分类与动作类型.

    返回: (category, action_type)
    category in ('anomaly', 'airing', 'download', 'review', 'archive')
    """
    status = str(get_record_field(rec, "status") or "")
    if status in TERMINAL_STATUSES:
        return "", ""
    evidence = _safe_json(
        get_record_field(rec, "evidence_json", None) or get_record_field(rec, "evidence", None), {}
    )
    conflict = _safe_json(
        get_record_field(rec, "conflict_json", None) or get_record_field(rec, "conflict", None), {}
    )
    rec_eps = _safe_json(
        get_record_field(rec, "recognized_episodes_json", None)
        or get_record_field(rec, "recognized_episodes", None),
        [],
    )
    missing = _safe_json(
        get_record_field(rec, "missing_episodes_json", None)
        or get_record_field(rec, "missing_episodes", None),
        [],
    )
    duplicates = _safe_json(
        get_record_field(rec, "duplicate_episodes_json", None)
        or get_record_field(rec, "duplicate_episodes", None),
        [],
    )
    total_eps = get_record_field(rec, "total_episodes")
    season_finished = get_record_field(rec, "season_finished")

    dir_grp = evidence.get("directory_group") or {}
    dir_grp_id = str(dir_grp.get("id") or "")
    dir_grp_requires_confirm = bool(dir_grp.get("requires_confirm"))
    if pending_dir_group_ids and dir_grp_id in pending_dir_group_ids:
        dir_grp_requires_confirm = True

    meta_status = str(evidence.get("meta_status") or evidence.get("metadata_status") or "")
    b_ev = (evidence.get("broadcast_evidence") or {}) if isinstance(evidence, dict) else {}
    b_status = str(b_ev.get("status") or "")
    status_detail_str = str(get_record_field(rec, "status_detail") or "")

    # 1. 异常剧集 (anomaly) 判定
    is_anomaly = False
    action_type = "inspect_info"

    if dir_grp_requires_confirm:
        is_anomaly = True
        action_type = "inspect_info"
    elif status in ANOMALY_STATUSES:
        is_anomaly = True
        if status == "already_archived":
            action_type = "clean_redundant"
        elif status == "unknown":
            action_type = "calibrate"
        elif status == "period_review":
            action_type = "system_tracking"
        else:
            action_type = "inspect_info"
    elif conflict.get("blocking"):
        is_anomaly = True
        action_type = "inspect_info"
    elif meta_status in ANOMALY_META_STATUSES:
        is_anomaly = True
        action_type = "inspect_info"
    elif b_status == "evidence_pending" or evidence.get("schedule_evidence_pending") or b_ev.get("heuristic_180d_blocked"):
        is_anomaly = True
        action_type = "system_tracking"
    elif total_eps and rec_eps and max(rec_eps) > total_eps and status not in ("archive_ready", "airing"):
        is_anomaly = True
        action_type = "inspect_info"

    if is_anomaly:
        return "anomaly", action_type

    # 2. 连载留源 (airing) 判定
    # L04/L05: 须有明确在播/待播状态或连载期重复诊断；旧记录仅存 season_finished=false 且无状态/无依据时归入证据待决
    if status == "airing" or b_status in ("airing", "upcoming"):
        return "airing", "skip"
    if season_finished is False and status == "review_needed" and (
        "连载中" in status_detail_str or b_ev.get("schedule_verified")
    ):
        return "airing", "skip"
    if season_finished is False and not status:
        return "anomaly", "system_tracking"

    # 3. 真缺集待补 (download) 判定（含缺集兼重复，缺集优先补齐；历史状态与缺集证据矛盾时以缺集为准）
    if status in ("download_needed", "period_missing") or missing:
        gap_state = get_record_field(rec, "gap_fill_state") or {}
        if gap_state.get("view_bucket") == "pushed_pending":
            return "download", "inspect_gap_detail"
        return "download", "download"

    regular_duplicates = [d for d in duplicates if isinstance(d, int) and d > 0]

    # 4. 待复核清理 (review) 判定（正片已齐但有正片重复副本或可淘汰冗余）
    if status == "review_needed" or (regular_duplicates and status != "period_ready"):
        return "review", "clean"

    # 5. 齐全可归档 (archive) 判定（须具备总集数与本地/跨端覆盖依据）
    local_ep_count = int(get_record_field(rec, "local_episode_count") or 0)
    dual_archived_eps = (evidence.get("dual_archive_info") or {}).get("archived_episodes") or []
    has_coverage_evidence = bool(rec_eps) or local_ep_count > 0 or bool(dual_archived_eps)
    valid_total_eps = bool(total_eps and int(total_eps) > 0)

    if status == "period_ready" and (total_eps is None or int(total_eps) > 0) and has_coverage_evidence:
        return "archive", "archive"

    if status == "archive_ready" and valid_total_eps and has_coverage_evidence:
        return "archive", "archive"

    if (
        not missing
        and not regular_duplicates
        and valid_total_eps
        and len([e for e in rec_eps if isinstance(e, int) and e > 0]) >= int(total_eps)
    ):
        return "archive", "archive"

    # 兜底：无明确证据或未知状态转入异常剧集核对，严禁无依据兜底落入 archive
    return "anomaly", "inspect_info"


def build_work_key(rec: Any) -> str:
    """构建作品季唯一聚合主键 (tmdb_id, season_number) 或带路径隔离的回退键.

    遵循规范：
    - 已确认“有意共存 (intentional_coexist)”的收藏保持各自独立卡片与归档路径，不合并；
    - 缺 TMDB ID 时不能仅凭名称相同合并不同物理目录。
    """
    tmdb_id = get_record_field(rec, "tmdb_id")
    season_number = get_record_field(rec, "season_number", 1)
    show_name = str(get_record_field(rec, "show_name") or "unknown").strip().lower()
    path_token = str(
        get_record_field(rec, "show_path") or get_record_field(rec, "season_path") or ""
    ).strip().rstrip("/")
    evidence = _safe_json(
        get_record_field(rec, "evidence_json", None) or get_record_field(rec, "evidence", None), {}
    )
    dir_grp = (evidence or {}).get("directory_group") or {}
    if dir_grp.get("confirmation_mode") == "intentional_coexist" or dir_grp.get("status") == "coexist":
        return f"coexist:{tmdb_id or show_name}:{path_token}:S{season_number}"
    if tmdb_id and str(tmdb_id).isdigit() and int(tmdb_id) > 0:
        return f"tmdb:{tmdb_id}:S{season_number}"
    if path_token:
        return f"show:{show_name}:{path_token}:S{season_number}"
    return f"show:{show_name}:S{season_number}"


def consolidate_seasons_to_work_records(
    records: Sequence[Any],
    pending_dir_group_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """对给定的物理剧季记录列表，按作品季执行唯一主待办归并与优先级互斥分流.

    返回: (work_records, counts)
    每个 work_record 均包含:
      - main_category: 'anomaly' | 'airing' | 'download' | 'review' | 'archive'
      - is_primary_work_record: True
      - associated_records: list[dict] (组内其他物理目录的记录摘要)
      - total_dir_count: 物理目录总数
      - 原始记录的其他字段
    """
    if not records:
        return [], {
            "archive": 0,
            "review": 0,
            "download": 0,
            "download_pushed": 0,
            "airing": 0,
            "anomaly": 0,
            "anomaly_decision": 0,
            "anomaly_cleanable": 0,
            "anomaly_pending_evidence": 0,
            "held": 0,
            "system_tracking": 0,
            "total_work_seasons": 0,
        }

    # 1. 分组
    groups: dict[str, list[tuple[Any, str, str]]] = {}
    for r in records:
        key = build_work_key(r)
        cat, act = classify_single_record(r, pending_dir_group_ids)
        groups.setdefault(key, []).append((r, cat, act))

    work_records: list[dict[str, Any]] = []
    counts = {
        "archive": 0,
        "review": 0,
        "download": 0,
        "download_pushed": 0,
        "airing": 0,
        "anomaly": 0,
        "anomaly_decision": 0,
        "anomaly_cleanable": 0,
        "anomaly_pending_evidence": 0,
        "held": 0,
        "system_tracking": 0,
        "total_work_seasons": 0,
    }

    # 2. 逐组裁定唯一主待办
    for work_key, items in groups.items():
        # 过滤已终态/无待办分类的物理记录
        active_items = [item for item in items if item[1] in CATEGORY_PRIORITY]
        if not active_items:
            continue

        # 取组内最优先的分类 (priority 数值最小)
        best_cat = min(active_items, key=lambda x: CATEGORY_PRIORITY.get(x[1], 99))[1]

        # 检查是否为同一已确认目标下的互补多源目录组 (A06)
        is_complementary_group = False
        union_pos_eps: set[int] = set()
        if len(active_items) > 1 and work_key.startswith("tmdb:") and best_cat not in ("anomaly", "airing"):
            tot_vals = {
                int(get_record_field(it[0], "total_episodes") or 0)
                for it in active_items
                if get_record_field(it[0], "total_episodes")
            }
            if len(tot_vals) == 1 and next(iter(tot_vals)) > 0:
                expected_tot = next(iter(tot_vals))
                has_dup_or_overlap = False
                targets_set: set[str] = set()
                for it_rec, _, _ in active_items:
                    dups = _safe_json(
                        get_record_field(it_rec, "duplicate_episodes_json")
                        or get_record_field(it_rec, "duplicate_episodes"),
                        [],
                    )
                    if any(isinstance(d, int) and d > 0 for d in dups):
                        has_dup_or_overlap = True
                        break
                    r_eps = {
                        int(e)
                        for e in _safe_json(
                            get_record_field(it_rec, "recognized_episodes_json")
                            or get_record_field(it_rec, "recognized_episodes"),
                            [],
                        )
                        if str(e).isdigit() and int(e) > 0
                    }
                    if union_pos_eps & r_eps:
                        has_dup_or_overlap = True
                        break
                    union_pos_eps |= r_eps
                    arc_t = str(get_record_field(it_rec, "archive_target_path") or "").strip().rstrip("/")
                    if arc_t:
                        targets_set.add(arc_t)
                if (
                    not has_dup_or_overlap
                    and set(range(1, expected_tot + 1)) <= union_pos_eps
                    and len(targets_set) == 1
                ):
                    is_complementary_group = True
                    best_cat = "archive"

        # 从该分类中选出最具有代表性的记录作为主记录
        candidates = (
            active_items
            if is_complementary_group
            else [item for item in active_items if item[1] == best_cat]
        )
        # 优先选本地正片数更多、或有路径更明确的记录
        def _rank_candidate(cand: tuple[Any, str, str]) -> int:
            rec = cand[0]
            rec_eps = _safe_json(
                get_record_field(rec, "recognized_episodes_json") or get_record_field(rec, "recognized_episodes"), []
            )
            return len(rec_eps)

        primary_item = max(candidates, key=_rank_candidate)
        primary_rec, _, primary_act = primary_item
        if is_complementary_group:
            primary_act = "archive"

        # 整理关联记录摘要
        associated: list[dict[str, Any]] = []
        for other_rec, other_cat, other_act in items:
            if other_rec is primary_rec:
                continue
            rec_id = get_record_field(other_rec, "id")
            s_path = get_record_field(other_rec, "season_path")
            sh_path = get_record_field(other_rec, "show_path")
            s_name = get_record_field(other_rec, "show_name")
            status = get_record_field(other_rec, "status")
            status_detail = get_record_field(other_rec, "status_detail")
            rec_eps = _safe_json(
                get_record_field(other_rec, "recognized_episodes_json")
                or get_record_field(other_rec, "recognized_episodes"),
                [],
            )
            associated.append({
                "id": rec_id,
                "season_path": s_path,
                "show_path": sh_path,
                "show_name": s_name,
                "status": status,
                "status_detail": status_detail,
                "category": other_cat,
                "action_type": other_act,
                "recognized_episodes": rec_eps,
            })

        # 构造主记录包装字典
        base_dict: dict[str, Any]
        if isinstance(primary_rec, dict):
            base_dict = dict(primary_rec)
        else:
            # SQLAlchemy 模型或对象，转成基础 dict
            base_dict = {
                "id": getattr(primary_rec, "id", None),
                "task_id": getattr(primary_rec, "task_id", None),
                "show_name": getattr(primary_rec, "show_name", None),
                "show_path": getattr(primary_rec, "show_path", None),
                "tmdb_id": getattr(primary_rec, "tmdb_id", None),
                "season_number": getattr(primary_rec, "season_number", 1),
                "season_path": getattr(primary_rec, "season_path", None),
                "total_episodes": getattr(primary_rec, "total_episodes", None),
                "local_episode_count": getattr(primary_rec, "local_episode_count", 0),
                "recognized_episodes": _safe_json(
                    getattr(primary_rec, "recognized_episodes_json", None)
                    or getattr(primary_rec, "recognized_episodes", None),
                    [],
                ),
                "missing_episodes": _safe_json(
                    getattr(primary_rec, "missing_episodes_json", None)
                    or getattr(primary_rec, "missing_episodes", None),
                    [],
                ),
                "duplicate_episodes": _safe_json(
                    getattr(primary_rec, "duplicate_episodes_json", None)
                    or getattr(primary_rec, "duplicate_episodes", None),
                    [],
                ),
                "status": getattr(primary_rec, "status", None),
                "status_detail": getattr(primary_rec, "status_detail", None),
                "archive_target_path": getattr(primary_rec, "archive_target_path", None),
                "season_finished": getattr(primary_rec, "season_finished", None),
                "evidence": _safe_json(
                    getattr(primary_rec, "evidence_json", None) or getattr(primary_rec, "evidence", None), {}
                ),
                "conflict": _safe_json(
                    getattr(primary_rec, "conflict_json", None) or getattr(primary_rec, "conflict", None), {}
                ),
                "mapping": _safe_json(
                    getattr(primary_rec, "mapping_json", None) or getattr(primary_rec, "mapping", None), {}
                ),
                "last_checked_at": (
                    primary_rec.last_checked_at.isoformat() if getattr(primary_rec, "last_checked_at", None) else None
                ),
            }

        base_dict["main_category"] = best_cat
        base_dict["airing_protected"] = best_cat == "airing" or any(is_airing_protected(it[0]) for it in active_items)
        base_dict["can_mark_handled"] = best_cat == "anomaly" and not base_dict["airing_protected"]
        base_dict["action_type"] = primary_act
        base_dict["is_primary_work_record"] = True
        base_dict["associated_records"] = associated
        base_dict["total_dir_count"] = len(items)
        base_dict["is_complementary_group"] = is_complementary_group
        base_dict["participating_record_ids"] = [
            get_record_field(it[0], "id") for it in active_items if get_record_field(it[0], "id")
        ]
        if is_complementary_group:
            base_dict["status"] = "archive_ready"
            base_dict["recognized_episodes"] = sorted(union_pos_eps)
            base_dict["missing_episodes"] = []
            base_dict["status_detail"] = (
                f"多源互补目录合计全 {len(union_pos_eps)} 集已齐（涉及 {len(active_items)} 个源目录），可归档"
            )
        elif best_cat == "airing":
            airing_view = _build_airing_view(active_items, base_dict)
            base_dict["airing_view"] = airing_view
            base_dict["action_type"] = "skip"
            base_dict["recognized_episodes"] = airing_view["covered_episodes"]
            base_dict["missing_episodes"] = []
            if airing_view.get("card_summary"):
                base_dict["status_detail"] = airing_view["card_summary"]
            ev_dict = dict(base_dict.get("evidence") or {})
            ev_dict["broadcast_evidence"] = {
                **(ev_dict.get("broadcast_evidence") or {}),
                **airing_view,
            }
            base_dict["evidence"] = ev_dict
        elif base_dict["airing_protected"]:
            # 多目录等异常仍留在异常模块，但展示合并后的播出覆盖并保护连载文件。
            base_dict["airing_view"] = _build_airing_view(active_items, base_dict)

        # 检查是否全部已推送待入库
        gap_state = base_dict.get("gap_fill_state") or {}
        is_pushed_pending = (best_cat == "download" and gap_state.get("view_bucket") == "pushed_pending")

        # 计算异常剧集唯一互斥子分类与条件式主动作 (v2.5.38)
        ev_b = dict(base_dict.get("evidence") or {})
        conf_b = dict(base_dict.get("conflict") or {})
        dir_grp_b = dict(ev_b.get("directory_group") or {})
        dir_grp_req = bool(dir_grp_b.get("requires_confirm"))
        if pending_dir_group_ids and str(dir_grp_b.get("id") or "") in pending_dir_group_ids:
            dir_grp_req = True
        m_conf_b = dict(ev_b.get("manual_confirmation") or {})
        m_contradiction = bool((m_conf_b.get("contradiction") or {}).get("has_contradiction"))
        meta_st_b = str(ev_b.get("meta_status") or ev_b.get("metadata_status") or "")
        rec_eps_b = [int(x) for x in (base_dict.get("recognized_episodes") or []) if str(x).isdigit() and int(x) > 0]
        tot_eps_b = base_dict.get("total_episodes")
        st_b = str(base_dict.get("status") or "")
        is_overflow_b = bool(tot_eps_b and rec_eps_b and max(rec_eps_b) > int(tot_eps_b) and st_b not in ("archive_ready", "airing"))

        anomaly_sub = ""
        primary_action_kind = ""
        if best_cat == "anomaly":
            if dir_grp_req or conf_b.get("blocking") or st_b == "identity_review" or m_contradiction or is_overflow_b:
                anomaly_sub = "decision"
                primary_action_kind = "confirm_directory" if dir_grp_req else "verify_identity"
            elif (
                st_b == "already_archived"
                and not base_dict["airing_protected"]
                and base_dict.get("season_finished") is not False
                and (tot_eps_b is None or int(tot_eps_b or 0) > 0)
            ):
                anomaly_sub = "cleanable"
                primary_action_kind = "clean_redundant"
            else:
                anomaly_sub = "pending_evidence"
                if meta_st_b in ("fetch_failed", "query_failed") or ev_b.get("last_remote_recheck_error"):
                    primary_action_kind = "retry_failed"
                elif st_b == "period_review" and bool((ev_b.get("period_evidence") or {}).get("is_segmented")):
                    primary_action_kind = "safety_hold"
                elif st_b == "archive_check":
                    primary_action_kind = "safety_hold"
                else:
                    primary_action_kind = "manual_confirm"

        base_dict["anomaly_sub_category"] = anomaly_sub
        base_dict["primary_action_kind"] = primary_action_kind

        # 检查任务级「本次保留，不再提示」标记
        hold_info = None
        for it_r, _, _ in active_items:
            it_ev = _safe_json(get_record_field(it_r, "evidence_json") or get_record_field(it_r, "evidence"), {})
            h_obj = (it_ev.get("hold_for_task") or {}) if isinstance(it_ev, dict) else {}
            if isinstance(h_obj, dict) and h_obj.get("active"):
                hold_info = h_obj
                break

        base_dict["is_held_for_task"] = bool(hold_info)
        base_dict["hold_info"] = hold_info
        if hold_info:
            base_dict["original_main_category"] = best_cat
            base_dict["main_category"] = "held"
            counts["held"] += 1
            counts["total_work_seasons"] += 1
            work_records.append(base_dict)
            continue

        # 计入统计
        counts[best_cat] += 1
        if is_pushed_pending:
            counts["download_pushed"] += 1
        if best_cat == "anomaly":
            if anomaly_sub == "decision":
                counts["anomaly_decision"] += 1
            elif anomaly_sub == "cleanable":
                counts["anomaly_cleanable"] += 1
            else:
                counts["anomaly_pending_evidence"] += 1
            if primary_act == "system_tracking" or getattr(primary_rec, "status", None) == "period_review":
                counts["system_tracking"] += 1
        counts["total_work_seasons"] += 1

        work_records.append(base_dict)

    # 排序：按 show_name, season_number
    work_records.sort(
        key=lambda w: (
            str(w.get("show_name") or ""),
            int(w.get("season_number") or 1),
        )
    )

    return work_records, counts


def _build_airing_view(
    active_items: list[tuple[Any, str, str]],
    base_dict: dict[str, Any],
) -> dict[str, Any]:
    """构建连载留源作品季的三组进度、分端覆盖、在途状态与卡片文案 (L01-L09)."""
    from collections import Counter

    ev = dict(base_dict.get("evidence") or {})
    b_ev = dict(ev.get("broadcast_evidence") or {})
    p_ev = dict(ev.get("period_evidence") or {})
    dual_info = dict(ev.get("dual_archive_info") or {})
    ms_check = dict(ev.get("mediaserver_check") or {})

    # 1. 跨物理目录去重聚合源端正片集号 (L07)
    dir_ep_counter: Counter[int] = Counter()
    source_eps_union: set[int] = set()
    dir_breakdown: list[dict[str, Any]] = []
    has_e00_any = False

    for it_rec, it_cat, _ in active_items:
        it_ev = _safe_json(
            get_record_field(it_rec, "evidence_json") or get_record_field(it_rec, "evidence"), {}
        )
        it_dual = (it_ev.get("dual_archive_info") or {}) if isinstance(it_ev, dict) else {}
        it_b_ev = (it_ev.get("broadcast_evidence") or {}) if isinstance(it_ev, dict) else {}
        raw_rec_eps = _safe_json(
            get_record_field(it_rec, "recognized_episodes_json")
            or get_record_field(it_rec, "recognized_episodes"),
            [],
        )
        if 0 in raw_rec_eps or "0" in raw_rec_eps:
            has_e00_any = True
        explicit_src = it_dual.get("source_episodes") or it_b_ev.get("source_episodes")
        if isinstance(explicit_src, list):
            pos_eps = sorted({int(x) for x in explicit_src if str(x).isdigit() and int(x) > 0})
        else:
            pos_eps = sorted({int(x) for x in raw_rec_eps if str(x).isdigit() and int(x) > 0})
        for ep_n in pos_eps:
            dir_ep_counter[ep_n] += 1
        source_eps_union.update(pos_eps)
        dir_breakdown.append({
            "id": get_record_field(it_rec, "id"),
            "season_path": get_record_field(it_rec, "season_path") or get_record_field(it_rec, "show_path"),
            "status": get_record_field(it_rec, "status"),
            "episodes": pos_eps,
        })

    cross_dir_overlap_eps = sorted(ep_n for ep_n, cnt in dir_ep_counter.items() if cnt > 1)
    archived_eps = sorted({
        int(x) for x in (dual_info.get("archived_episodes") or b_ev.get("archived_episodes") or [])
        if str(x).isdigit() and int(x) > 0
    })
    emby_eps = sorted({
        int(x) for x in (ms_check.get("season_episodes") or b_ev.get("emby_episodes") or [])
        if str(x).isdigit() and int(x) > 0
    })

    # 若单目录记录此前把归档集并入了 recognized_episodes 且未显式保存 source_episodes，按 local_episode_count 还原源端集数
    if (
        len(active_items) == 1
        and archived_eps
        and not dual_info.get("source_episodes")
        and not b_ev.get("source_episodes")
    ):
        loc_cnt = int(base_dict.get("local_episode_count") or 0)
        diff_src = sorted(source_eps_union - set(archived_eps))
        if len(diff_src) == loc_cnt:
            source_eps = diff_src
        else:
            source_eps = sorted(source_eps_union)
    else:
        source_eps = sorted(source_eps_union)

    covered_eps = sorted(set(source_eps) | set(archived_eps) | set(emby_eps))

    # 2. 正片重复与历史底层缺集提示 (L05)
    pos_duplicates = sorted({
        int(d)
        for it_rec, _, _ in active_items
        for d in _safe_json(
            get_record_field(it_rec, "duplicate_episodes_json")
            or get_record_field(it_rec, "duplicate_episodes"),
            [],
        )
        if str(d).isdigit() and int(d) > 0
    })
    underlying_statuses = sorted({
        str(get_record_field(it_rec, "status") or "")
        for it_rec, _, _ in active_items
        if get_record_field(it_rec, "status")
    })

    # 3. 播期三组拆分：已播 / 待播(含今日排播) / 日期未知
    total_eps = base_dict.get("total_episodes")
    is_segmented = bool(p_ev.get("is_segmented"))
    partial_listing = bool(b_ev.get("partial_listing") or (not b_ev.get("schedule_complete", True) and total_eps is None))
    total_unknown = total_eps is None or int(total_eps or 0) <= 0 or bool(b_ev.get("total_unknown"))

    aired_eps = sorted({int(x) for x in (b_ev.get("aired_episodes") or []) if str(x).isdigit() and int(x) > 0})
    today_eps = sorted({int(x) for x in (b_ev.get("today_scheduled_episodes") or []) if str(x).isdigit() and int(x) > 0})
    # 今日排播集若已实际入库，则证实已播
    confirmed_today = [ep for ep in today_eps if ep in covered_eps]
    if confirmed_today:
        aired_eps = sorted(set(aired_eps) | set(confirmed_today))
        today_eps = [ep for ep in today_eps if ep not in covered_eps]

    upcoming_eps = sorted({int(x) for x in (b_ev.get("upcoming_episodes") or []) if str(x).isdigit() and int(x) > 0})
    undated_eps = sorted({int(x) for x in (b_ev.get("undated_episodes") or []) if str(x).isdigit() and int(x) > 0})
    trailer_files = list(b_ev.get("trailer_or_e00_files") or [])
    if has_e00_any and not trailer_files:
        trailer_files = ["E00 先导片/特辑"]

    broadcast_status = str(b_ev.get("status") or ("upcoming" if (not aired_eps and upcoming_eps and not covered_eps) else "airing"))
    has_explicit_schedule = bool(aired_eps or upcoming_eps or today_eps or broadcast_status == "upcoming")

    if has_explicit_schedule:
        collected_aired = sorted(set(aired_eps) & set(covered_eps))
        uncollected_aired = sorted(set(aired_eps) - set(covered_eps))
    else:
        collected_aired = list(covered_eps)
        uncollected_aired = []

    # 4. 在途下载对账：已入库集自动从在途集合移除 (L02)
    gap_state = base_dict.get("gap_fill_state") or {}
    raw_in_transit: set[int] = set()
    for x in (gap_state.get("pushed_episodes") or []):
        if str(x).isdigit() and int(x) > 0:
            raw_in_transit.add(int(x))
    for x in (b_ev.get("in_transit_episodes") or []):
        if str(x).isdigit() and int(x) > 0:
            raw_in_transit.add(int(x))
    for it_rec, _, _ in active_items:
        it_gs = get_record_field(it_rec, "gap_fill_state") or {}
        for x in (it_gs.get("pushed_episodes") or []):
            if str(x).isdigit() and int(x) > 0:
                raw_in_transit.add(int(x))
    in_transit_eps = sorted(raw_in_transit - set(covered_eps))

    next_ep_num = b_ev.get("next_episode_number") or (upcoming_eps[0] if upcoming_eps else (today_eps[0] if today_eps else None))
    next_air_date = b_ev.get("next_air_date") or ""
    fetched_at = b_ev.get("fetched_at") or base_dict.get("last_checked_at") or ""
    last_recalculated_at = ev.get("last_recalculated_at") or ""

    # 5. 生成卡片展示状态与摘要文案
    partial_period_notes = list(p_ev.get("partial_period_notes") or b_ev.get("partial_period_notes") or [])
    unit_word = "期" if is_segmented else "集"

    if broadcast_status == "upcoming":
        badge_text = "待首播 · 正片尚未播出"
        total_label = f"共 {total_eps} {unit_word}" if not total_unknown else f"总{unit_word}数待公布"
        status_text = f"待首播 · 正片 0 {unit_word}（{total_label}）"
        if trailer_files:
            status_text += f" · 先导/预告 {len(trailer_files)} 个"
        segs = ["待首播 · 正片尚未播出"]
        if next_air_date:
            segs.append(f"预计 {next_air_date} 首播")
        if trailer_files:
            segs.append(f"先导片/预告 {len(trailer_files)} 个已单列")
        segs.append("文件保留在源目录，本轮无需操作。")
        card_summary = "；".join(segs)
    elif is_segmented:
        badge_text = "连载中"
        total_label = f"共 {total_eps} 期" if not total_unknown else "总期数待公布"
        status_text = f"已收 {len(covered_eps)} 期正片 · {total_label}"
        segs = []
        if partial_period_notes:
            segs.extend(partial_period_notes)
        elif aired_eps:
            segs.append(f"已播 {len(aired_eps)} 期，已收 {len(collected_aired)} 期")
        else:
            segs.append(f"已收 {len(covered_eps)} 期正片")
        if total_unknown:
            segs.append("全季总期数待公布")
        elif upcoming_eps:
            segs.append(f"后续 {len(upcoming_eps)} 期待播")
        if trailer_files:
            segs.append(f"先导/衍生已单列（不抵正片下段）")
        segs.append("文件保留在源目录，本轮无需操作。")
        card_summary = "；".join(segs)
    elif has_explicit_schedule:
        badge_text = "今日排播" if (today_eps and not aired_eps) else "连载中"
        aired_cnt = len(aired_eps)
        coll_cnt = len(collected_aired)
        upc_cnt = len(upcoming_eps)
        if total_unknown:
            status_text = f"已播 {aired_cnt} 集 / 已收 {coll_cnt} 集 · 总集数待公布"
        else:
            status_text = f"已播 {aired_cnt} 集 / 已收 {coll_cnt} 集（全季 {total_eps} 集）"
        if in_transit_eps:
            status_text += f" · 在途 {len(in_transit_eps)}"
        if pos_duplicates:
            status_text += f"，第 {'、'.join(map(str, pos_duplicates))} 集有多个版本"

        parts_txt: list[str] = []
        if aired_cnt > 0:
            if not uncollected_aired and coll_cnt >= aired_cnt:
                parts_txt.append(f"已播 {aired_cnt} 集，已收齐（已收 {coll_cnt} 集）")
            else:
                parts_txt.append(f"已播 {aired_cnt} 集，已收 {coll_cnt} 集")
        else:
            parts_txt.append(f"已播 0 集，已收 {len(covered_eps)} 集")

        if in_transit_eps:
            ep_labels = "、".join(f"E{e:02d}" for e in in_transit_eps)
            parts_txt.append(f"{ep_labels} 已提交下载，等待入库（在途 {len(in_transit_eps)} 集）")
        elif uncollected_aired:
            ep_labels = "、".join(f"E{e:02d}" for e in uncollected_aired[:6])
            parts_txt.append(f"已播未见：{ep_labels}")

        if today_eps:
            t_labels = "、".join(f"E{e:02d}" for e in today_eps)
            parts_txt.append(f"今日排播 {t_labels}（待证实播出后计入已播）")

        if upc_cnt > 0:
            next_hint = f"（下一集 E{int(next_ep_num):02d} 预计 {next_air_date}）" if (next_ep_num and next_air_date) else ""
            parts_txt.append(f"后续 {upc_cnt} 集待播（{upc_cnt} 集待播）{next_hint}")
        elif b_ev.get("continuing_announcement") or undated_eps:
            parts_txt.append("后续播期待公布")

        if pos_duplicates:
            parts_txt.append(f"第 {','.join(map(str, pos_duplicates))} 集存在重复版本（季终重算后再清理）")

        if b_ev.get("last_check_incomplete"):
            parts_txt.append("本次核查未完成，保留上次有效结论")
        else:
            parts_txt.append("文件保留在源目录，本轮无需操作")
        card_summary = "；".join(parts_txt) + "。"
    else:
        badge_text = "连载中"
        if total_unknown:
            status_text = f"已收 {len(covered_eps)} {unit_word} · 总{unit_word}数待公布"
        else:
            status_text = f"{len(covered_eps)}/{total_eps}{unit_word}"
        if pos_duplicates:
            status_text += f"，第 {'、'.join(map(str, pos_duplicates))} 集有多个版本"
        card_summary = str(base_dict.get("status_detail") or f"连载中：已收 {len(covered_eps)} {unit_word}，文件保留在源目录。")
        if b_ev.get("last_check_incomplete") and "本次核查未完成" not in card_summary:
            card_summary += "（本次核查未完成，保留上次有效结论）"

    return {
        "status": broadcast_status,
        "badge_text": badge_text,
        "status_text": status_text,
        "card_summary": card_summary,
        "schedule_verified": has_explicit_schedule or bool(b_ev.get("schedule_verified")),
        "source": b_ev.get("source") or "tmdb_episodes",
        "fetched_at": fetched_at,
        "last_recalculated_at": last_recalculated_at,
        "date_precision": b_ev.get("date_precision") or ("day" if has_explicit_schedule else "none"),
        "schedule_complete": bool(b_ev.get("schedule_complete", not partial_listing and not total_unknown)),
        "total_unknown": total_unknown,
        "aired_episodes": aired_eps,
        "collected_aired_episodes": collected_aired,
        "uncollected_aired_episodes": uncollected_aired,
        "today_scheduled_episodes": today_eps,
        "upcoming_episodes": upcoming_eps,
        "undated_episodes": undated_eps,
        "in_transit_episodes": in_transit_eps,
        "source_episodes": source_eps,
        "archived_episodes": archived_eps,
        "emby_episodes": emby_eps,
        "covered_episodes": covered_eps,
        "duplicate_episodes": pos_duplicates,
        "cross_dir_overlap_episodes": cross_dir_overlap_eps,
        "directory_breakdown": dir_breakdown,
        "underlying_statuses": underlying_statuses,
        "trailer_or_e00_files": trailer_files,
        "partial_period_notes": partial_period_notes,
        "next_episode_number": next_ep_num,
        "next_air_date": next_air_date,
        "last_check_incomplete": bool(b_ev.get("last_check_incomplete")),
        "last_check_error": str(b_ev.get("last_check_error") or ""),
    }


async def get_record_work_context(db: Any, rec: Any) -> dict:
    """查询指定物理剧季记录在所属任务中按作品季归并后的唯一主分类 (L05 写入安全门禁)."""
    from sqlalchemy import select
    from app.models.season import SeasonRecord
    from app.models.directory_group import DirectoryGroup

    task_id = get_record_field(rec, "task_id")
    rec_id = get_record_field(rec, "id")
    if not task_id or db is None:
        cat, _ = classify_single_record(rec)
        return {"main_category": cat, "airing_protected": cat == "airing" or is_airing_protected(rec)}

    stmt = select(SeasonRecord).where(
        SeasonRecord.task_id == task_id,
        SeasonRecord.status.not_in(tuple(TERMINAL_STATUSES)),
    )
    rows = list((await db.execute(stmt)).scalars().all())
    if not rows:
        cat, _ = classify_single_record(rec)
        return {"main_category": cat, "airing_protected": cat == "airing" or is_airing_protected(rec)}

    dg_stmt = select(DirectoryGroup.id).where(
        DirectoryGroup.task_id == task_id,
        DirectoryGroup.status == "pending_confirm",
    )
    pending_dg_ids = {str(x) for x in (await db.execute(dg_stmt)).scalars().all() if x}
    work_recs, _ = consolidate_seasons_to_work_records(rows, pending_dg_ids)
    target_wkey = build_work_key(rec)
    for w in work_recs:
        if w.get("id") == rec_id or rec_id in (w.get("participating_record_ids") or []):
            return w
        if build_work_key(w) == target_wkey:
            return w
    cat, _ = classify_single_record(rec, pending_dg_ids)
    return {"main_category": cat, "airing_protected": cat == "airing" or is_airing_protected(rec)}


async def get_record_work_main_category(db: Any, rec: Any) -> str:
    return str((await get_record_work_context(db, rec)).get("main_category") or "")
