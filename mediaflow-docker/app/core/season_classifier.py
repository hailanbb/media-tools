"""MediaFlow 五模块互斥分流与作品季归并核心决策器 (v2.5.45 重构版).

严格遵循《MediaFlow五模块处理流程重构与智能清理方案-v2.5.44.md》：
1. 页面/导航展示顺序（全端一致）：
   待复核清理 (review) → 异常剧集 (anomaly) → 缺集待补 (download) → 齐全可归档 (archive) → 连载留源 (airing)

2. 分流计算优先级（短路求值，播期判断在下载资格之前，避免将未播内容误推为缺集）：
   优先级 1: 待复核清理 (review) — 有源端同内容多余副本（含缺集兼副本、连载稳定副本）、同 ID 目录规范化归并、归档已齐待清源
   优先级 2: 异常剧集 (anomaly) — 身份冲突、编号/口径冲突、资料或目录读取失败、文件内容待识别、播期未知、修正未完成
   优先级 3: 连载留源 (airing) — 已确认正在连载或待播
   优先级 4: 缺集待补 (download) — 已播毕且有确认缺口（含源端仅附件且两端无正片）
   优先级 5: 齐全可归档 (archive) — 已播毕、覆盖齐全且清理/修正已完成

3. 消除分流循环 (G26)：
   若清理分析确认同集多文件全部属于不同内容/剪辑版本（review_resolution.all_distinct_retained 且指纹未变），
   不再因原始重复集号送回待复核清理，直接按实际播期与覆盖分流。

4. 数学不变量：
   catCountReview + catCountAnomaly + catCountDownload + catCountAiring + catCountArchive + catCountHeld == total_work_seasons
"""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

# 导航与汇总展示统一顺序 (G21)
MODULE_DISPLAY_ORDER: tuple[str, ...] = ("review", "anomaly", "download", "archive", "airing")

MODULE_LABELS: dict[str, str] = {
    "review": "待复核清理",
    "anomaly": "异常剧集",
    "download": "缺集待补",
    "airing": "连载留源",
    "archive": "齐全可归档",
    "held": "已跳过本轮",
}

# 分流优先级定义（数值越小优先级越高；播期判断优先于下载资格）
CATEGORY_PRIORITY: dict[str, int] = {
    "review": 1,
    "anomaly": 2,
    "airing": 3,
    "download": 4,
    "archive": 5,
}

# 属于异常剧集的状态集合（already_archived 已正式迁入待复核清理）
ANOMALY_STATUSES = {
    "identity_review",
    "period_review",
    "archive_check",
    "unknown",
    "conflict",
    "failed",
    "partial_archived",
    "archive_unverified",
    "correction_incomplete",
    "read_failed",
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
    b_status = str((evidence.get("broadcast_evidence") or {}).get("status") or "")
    return get_record_field(rec, "status") == "airing" or b_status in ("airing", "upcoming")


def _is_distinct_retained_valid(evidence: dict[str, Any]) -> bool:
    """检查 G26 防循环标记：多版本均已确认独立保留且当前文件指纹未变化。"""
    rev_res = (evidence or {}).get("review_resolution") or {}
    if not isinstance(rev_res, dict) or not rev_res.get("all_distinct_retained"):
        return False
    saved_fp = str(rev_res.get("fingerprint") or "")
    curr_fp = str((evidence or {}).get("asset_fingerprint") or "")
    if saved_fp and curr_fp and saved_fp != curr_fp:
        return False
    return True


def supplemental_coverage_preserved(rec: Any, evidence: dict[str, Any]) -> bool:
    """齐全且已播毕的已核实作品，少量相邻扩展集号保留；不改写资料总数。"""
    total = int(get_record_field(rec, 'total_episodes') or 0)
    episodes = set(_safe_json(get_record_field(rec, 'recognized_episodes_json') or
                              get_record_field(rec, 'recognized_episodes'), []))
    extra = {n for n in episodes if isinstance(n, int) and n > total}
    conflict = _safe_json(get_record_field(rec, 'conflict_json') or get_record_field(rec, 'conflict'), {})
    return bool(total >= 6 and extra and len(extra) <= 3 and max(extra) <= total + 4
        and set(range(1, total+1)) <= episodes and evidence.get('metadata_binding_verified')
        and (evidence.get('broadcast_evidence') or {}).get('status') == 'finished'
        and not evidence.get('source_total_conflict') and not conflict.get('blocking')
        and get_record_field(rec, 'status') not in ANOMALY_STATUSES)


def _predict_post_review_category(rec: Any, evidence: dict[str, Any]) -> str:
    """预测在待复核清理完成本季动作后的去向模块。"""
    status = str(get_record_field(rec, "status") or "")
    if status == "already_archived":
        return "completed_source_cleaned"
    b_ev = (evidence.get("broadcast_evidence") or {}) if isinstance(evidence, dict) else {}
    b_status = str(b_ev.get("status") or "")
    meta_status = str(evidence.get("meta_status") or evidence.get("metadata_status") or "")
    conflict = _safe_json(
        get_record_field(rec, "conflict_json", None) or get_record_field(rec, "conflict", None), {}
    )
    missing = _safe_json(
        get_record_field(rec, "missing_episodes_json", None)
        or get_record_field(rec, "missing_episodes", None),
        [],
    )
    rec_eps = _safe_json(
        get_record_field(rec, "recognized_episodes_json", None)
        or get_record_field(rec, "recognized_episodes", None),
        [],
    )
    total_eps = get_record_field(rec, "total_episodes")
    season_finished = get_record_field(rec, "season_finished")

    if (
        conflict.get("blocking")
        or meta_status in ANOMALY_META_STATUSES
        or status in ANOMALY_STATUSES
        or b_status == "evidence_pending"
        or evidence.get("schedule_evidence_pending")
    ):
        return "anomaly"
    status_detail_txt = str(get_record_field(rec, "status_detail") or "")
    if (
        status == "airing"
        or b_status in ("airing", "upcoming")
        or is_airing_protected(rec)
        or (season_finished is False and ("连载" in status_detail_txt or "待首播" in status_detail_txt))
    ):
        return "airing"
    if status in ("download_needed", "period_missing") or missing:
        return "download"
    if total_eps and int(total_eps) > 0 and season_finished is not False and len([e for e in rec_eps if isinstance(e, int) and e > 0]) >= int(total_eps):
        return "archive"
    if status in ("archive_ready", "period_ready", "review_needed") and season_finished is not False and total_eps:
        return "archive"
    return "anomaly"


def classify_single_record(
    rec: Any,
    pending_dir_group_ids: set[str] | None = None,
) -> tuple[str, str]:
    """判定单个物理剧季记录的基础分类与动作类型 (v2.5.45 清理优先规则).

    返回: (category, action_type)
    category in ('review', 'anomaly', 'airing', 'download', 'archive')
    """
    status = str(get_record_field(rec, "status") or "")
    if status in TERMINAL_STATUSES:
        return "", ""
    evidence = _safe_json(
        get_record_field(rec, "evidence_json", None) or get_record_field(rec, "evidence", None), {}
    )
    if not isinstance(evidence, dict):
        evidence = {}
    conflict = _safe_json(
        get_record_field(rec, "conflict_json", None) or get_record_field(rec, "conflict", None), {}
    )
    if not isinstance(conflict, dict):
        conflict = {}
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
    if pending_dir_group_ids is not None and dir_grp_id:
        dir_grp_requires_confirm = dir_grp_id in pending_dir_group_ids

    meta_status = str(evidence.get("meta_status") or evidence.get("metadata_status") or "")
    b_ev = (evidence.get("broadcast_evidence") or {}) if isinstance(evidence, dict) else {}
    b_status = str(b_ev.get("status") or "")

    regular_duplicates = [d for d in duplicates if isinstance(d, int) and d > 0]
    distinct_retained = _is_distinct_retained_valid(evidence)
    rp_preview = evidence.get("review_plan_preview") or {}
    if isinstance(rp_preview, dict) and "can_clean" in rp_preview:
        has_cleanable_dups = bool(rp_preview.get("can_clean"))
    else:
        has_cleanable_dups = bool(regular_duplicates) and not distinct_retained and status != "period_ready"

    # 显式身份/混放/修正未完成/读取失败/集数超标矛盾属于阻塞级异常，不跳过身份核查直接清理
    mc_contra = (evidence.get("manual_confirmation") or {}).get("contradiction") or {}
    max_rec_ep = max((e for e in rec_eps if isinstance(e, int) and e > 0), default=0)
    has_overflow_contradiction = bool(
        mc_contra.get("has_contradiction")
        or (total_eps and int(total_eps) > 0 and max_rec_ep > int(total_eps) and not supplemental_coverage_preserved(rec, evidence) and not (evidence.get("is_segmented") or (evidence.get("period_evidence") or {}).get("is_segmented")))
    )
    has_blocking_identity = bool(
        conflict.get("blocking")
        or status in ("identity_review", "correction_incomplete", "read_failed")
        or has_overflow_contradiction
    )

    # 1. 待复核清理 (review) 优先分流（G01, G02, G06-G08, G10）
    if not has_blocking_identity:
        # 1A. 归档已齐待清源 -> 迁入待复核清理
        if status == "already_archived":
            return "review", "clean_redundant"
        # 1B. 同 TMDB ID 多目录规范化归并 -> 纳入待复核清理
        if dir_grp_requires_confirm:
            return "review", "merge_directory"
        # 1C. 源端有可证实清理的同内容重复副本（含缺集兼副本 G01、连载期稳定副本 G02）
        if has_cleanable_dups or (status == "review_needed" and not distinct_retained and "can_clean" not in rp_preview):
            return "review", "clean"

    # 2. 异常剧集 (anomaly) 判定
    is_anomaly = False
    action_type = "inspect_info"

    if has_blocking_identity:
        is_anomaly = True
        action_type = "correction_resume" if status == "correction_incomplete" else "inspect_info"
    elif status in ANOMALY_STATUSES:
        is_anomaly = True
        if status == "unknown":
            action_type = "calibrate"
        elif status == "period_review":
            action_type = "verify_basis"
        else:
            action_type = "inspect_info"
    elif meta_status in ANOMALY_META_STATUSES:
        is_anomaly = True
        action_type = "retry_failed" if meta_status in ("fetch_failed", "query_failed") else "inspect_info"
    elif b_status == "evidence_pending" or evidence.get("schedule_evidence_pending") or b_ev.get("heuristic_180d_blocked"):
        is_anomaly = True
        action_type = "verify_broadcast"

    if is_anomaly:
        return "anomaly", action_type

    # 3. 连载留源 (airing) 判定（播期判断在下载资格之前，且仅限已确认连载/待播，不将未知播期显示为连载）
    status_detail_txt = str(get_record_field(rec, "status_detail") or "")
    if (
        status == "airing"
        or b_status in ("airing", "upcoming")
        or is_airing_protected(rec)
        or (season_finished is False and ("连载" in status_detail_txt or "待首播" in status_detail_txt))
    ):
        return "airing", "skip"
    if season_finished is False and b_ev.get("schedule_verified"):
        return "airing", "skip"
    if season_finished is False and not status:
        return "anomaly", "verify_broadcast"

    # 4. 缺集待补 (download) 判定（已播毕且有确认缺口，含源端仅附件且两端无正片）
    if status in ("download_needed", "period_missing") or missing:
        gap_state = get_record_field(rec, "gap_fill_state") or {}
        if gap_state.get("view_bucket") == "pushed_pending":
            return "download", "inspect_gap_detail"
        return "download", "download"

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
        and not has_cleanable_dups
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
            "download_no_resource": 0,
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
        "download_no_resource": 0,
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
        if best_cat == "download" and gap_state.get("view_bucket") == "no_resource":
            if any((get_record_field(it[0], "gap_fill_state") or {}).get("view_bucket") != "no_resource"
                   for it in active_items):
                gap_state = {**gap_state, "view_bucket": "todo"}
                base_dict["gap_fill_state"] = gap_state
        is_pushed_pending = (best_cat == "download" and gap_state.get("view_bucket") == "pushed_pending")

        # 计算待复核清理子动作与完成后预测去向 + 异常剧集具体问题分类 (v2.5.45)
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
        dups_b = [int(x) for x in (base_dict.get("duplicate_episodes") or []) if str(x).isdigit() and int(x) > 0]
        tot_eps_b = base_dict.get("total_episodes")
        st_b = str(base_dict.get("status") or "")
        is_overflow_b = bool(tot_eps_b and rec_eps_b and max(rec_eps_b) > int(tot_eps_b) and st_b not in ("archive_ready", "airing") and not supplemental_coverage_preserved(primary_rec, ev_b))
        rp_prev_b = (ev_b.get("review_plan_preview") or {}) if isinstance(ev_b.get("review_plan_preview"), dict) else {}

        review_actions: list[str] = []
        review_action_kind = ""
        predicted_next_category = ""
        predicted_next_category_label = ""
        if best_cat == "review":
            if dir_grp_req or primary_act == "merge_directory":
                review_actions.append("merge_directory")
            if st_b == "already_archived" or primary_act == "clean_redundant":
                review_actions.append("clean_redundant_source")
            if bool(rp_prev_b.get("can_clean")) or (dups_b and not _is_distinct_retained_valid(ev_b)) or primary_act == "clean":
                review_actions.append("clean_duplicates")
            if not review_actions:
                review_actions.append("clean_duplicates")
            review_action_kind = review_actions[0]
            predicted_next_category = _predict_post_review_category(primary_rec, ev_b)
            pred_label_map = {
                "archive": "完成后进入齐全可归档",
                "download": "完成后进入缺集待补",
                "airing": "完成后返回连载留源",
                "anomaly": "完成后进入异常剧集继续核对",
                "completed_source_cleaned": "源端已清理，归档已保留",
            }
            predicted_next_category_label = pred_label_map.get(predicted_next_category, "")
            base_dict["primary_action_kind"] = review_action_kind

        anomaly_sub = ""
        anomaly_problem_kind = ""
        anomaly_problem_label = ""
        primary_action_kind = base_dict.get("primary_action_kind") or ""
        if best_cat == "anomaly":
            if st_b == "correction_incomplete":
                anomaly_sub = "decision"
                anomaly_problem_kind = "correction_incomplete"
                anomaly_problem_label = "修正未完成"
                primary_action_kind = "resume_correction"
            elif st_b == "read_failed":
                anomaly_sub = "pending_evidence"
                anomaly_problem_kind = "directory_read_failed"
                anomaly_problem_label = "目录读取失败"
                primary_action_kind = "retry_failed"
            elif conf_b.get("blocking") or st_b == "identity_review" or m_contradiction:
                anomaly_sub = "decision"
                anomaly_problem_kind = "season_work_mismatch"
                anomaly_problem_label = "季号与作品不符" if (meta_st_b == "not_listed" or "季" in str(conf_b.get("message") or "")) else "作品归属待核对"
                primary_action_kind = "verify_identity"
            elif is_overflow_b:
                anomaly_sub = "decision"
                anomaly_problem_kind = "episode_basis_mismatch"
                anomaly_problem_label = "集数口径待核对"
                primary_action_kind = "verify_basis"
            elif meta_st_b in ("fetch_failed", "query_failed") or ev_b.get("last_remote_recheck_error"):
                anomaly_sub = "pending_evidence"
                anomaly_problem_kind = "metadata_read_failed"
                anomaly_problem_label = "资料读取失败"
                primary_action_kind = "retry_failed"
            elif meta_st_b == "not_listed":
                anomaly_sub = "decision"
                anomaly_problem_kind = "season_work_mismatch"
                anomaly_problem_label = "季号与作品不符"
                primary_action_kind = "manual_confirm"
            elif st_b == "archive_check":
                anomaly_sub = "pending_evidence"
                anomaly_problem_kind = "content_unrecognized"
                anomaly_problem_label = "文件内容待识别"
                primary_action_kind = "manual_confirm"
            elif st_b == "period_review":
                anomaly_sub = "pending_evidence"
                is_seg_b = bool((ev_b.get("period_evidence") or {}).get("is_segmented"))
                anomaly_problem_kind = "episode_basis_mismatch" if is_seg_b else "broadcast_unknown"
                anomaly_problem_label = "集数口径待核对" if is_seg_b else "播期资料待核实"
                primary_action_kind = "manual_confirm"
            else:
                anomaly_sub = "pending_evidence"
                anomaly_problem_kind = "broadcast_unknown" if (ev_b.get("broadcast_evidence") or {}).get("status") == "evidence_pending" else "episode_basis_mismatch"
                anomaly_problem_label = "播期资料待核实" if anomaly_problem_kind == "broadcast_unknown" else "集数口径待核对"
                primary_action_kind = "manual_confirm"

        base_dict["review_actions"] = review_actions
        base_dict["review_action_kind"] = review_action_kind
        base_dict["predicted_next_category"] = predicted_next_category
        base_dict["predicted_next_category_label"] = predicted_next_category_label
        base_dict["anomaly_sub_category"] = anomaly_sub
        base_dict["anomaly_problem_kind"] = anomaly_problem_kind
        base_dict["anomaly_problem_label"] = anomaly_problem_label
        base_dict["primary_action_kind"] = primary_action_kind

        # 检查任务级「跳过本轮」标记
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
        if best_cat == "download" and gap_state.get("view_bucket") == "no_resource":
            counts["download_no_resource"] += 1
        if best_cat == "anomaly":
            if anomaly_sub == "decision":
                counts["anomaly_decision"] += 1
            elif anomaly_sub == "cleanable":
                counts["anomaly_cleanable"] += 1
            else:
                counts["anomaly_pending_evidence"] += 1
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


_ANIME_DIR_TOKENS = {
    "动漫", "动画", "動漫", "動畫", "国漫", "國漫", "日漫", "国产动漫", "國產動漫",
    "日本动漫", "日本動漫", "欧美动漫", "歐美動漫", "番剧", "番劇", "里番", "新番",
    "anime", "animation", "donghua", "cartoon", "cartoons",
}
_NON_ANIME_DIR_TOKENS = {
    "综艺", "綜藝", "真人秀", "脱口秀", "脫口秀", "真人剧集", "真人劇集", "真人剧", "真人劇",
    "纪录片", "紀錄片", "韩剧", "韓劇", "日剧", "日劇", "美剧", "美劇", "国产剧", "國產劇",
    "华语剧", "華語劇", "港剧", "港劇", "台剧", "台劇", "英剧", "英劇", "泰剧", "泰劇",
    "韩国剧集", "日本剧集", "欧美剧集", "华语剧集", "国产剧集",
    "variety", "reality", "talk show", "talkshow", "documentary", "live action", "live-action",
}
_BROAD_MIXED_DIR_TOKENS = {
    "电视剧", "電視劇", "剧集", "劇集", "媒体库", "媒體庫", "下载", "下載", "待整理",
    "nas", "115", "library", "library_incs", "tv", "tvshows", "shows", "series", "downloads",
    "media", "media_archive", "nas.finish", "finish", "src", "qa",
}
_SEASON_DIR_RE = re.compile(r"^(?:season\s*\d+|s\d{1,2}|specials?)$", re.IGNORECASE)


def _match_category_dir_segment(segment: str) -> tuple[str | None, str]:
    """按目录名/明确词组判定分类（兼容简繁体与英文大小写，不因片名偶含单字误判）。"""
    raw = str(segment or "").strip()
    if not raw or _SEASON_DIR_RE.match(raw):
        return None, ""
    low = raw.lower()
    if low in _BROAD_MIXED_DIR_TOKENS:
        return None, ""
    if low in _ANIME_DIR_TOKENS:
        return "anime", raw
    if low in _NON_ANIME_DIR_TOKENS:
        return "non_anime", raw
    # 检查以分隔符拆分的明确词组（如 "日本-动漫"、"国产_动画"、"TV_Anime"）
    parts = [p.strip().lower() for p in re.split(r"[\s_\-/\\·|]+", raw) if p.strip()]
    if len(parts) > 1:
        for p in parts:
            if p in _ANIME_DIR_TOKENS:
                return "anime", raw
        for p in parts:
            if p in _NON_ANIME_DIR_TOKENS:
                return "non_anime", raw
    # 明确复合前缀/后缀分类目录（如 "国产动漫剧集"、"日本动画"、"华语综艺"）
    for tok in ("国产动漫", "國產動漫", "日本动漫", "日本動漫", "欧美动漫", "歐美動漫", "国漫", "國漫", "日漫", "动漫", "動漫", "动画", "動畫", "番剧", "番劇"):
        if raw.startswith(tok) or raw.endswith(tok):
            return "anime", raw
    for tok in ("真人剧集", "真人劇集", "真人秀", "脱口秀", "脫口秀", "纪录片", "紀錄片", "综艺", "綜藝", "韩剧", "日剧", "美剧", "国产剧", "华语剧"):
        if raw.startswith(tok) or raw.endswith(tok):
            return "non_anime", raw
    return None, ""


def detect_category_from_path(
    path_str: str | None,
    *,
    show_folder_name: str = "",
) -> tuple[str | None, str]:
    """从路径最具体层级向上查找最近的有效分类目录（G20）。

    - 跳过 Season N 及作品自身目录名（避免片名偶含字样误判为分类目录）；
    - 跳过“电视剧/媒体库/下载/待整理”等宽泛目录；
    - 祖先目录“电视剧”绝不覆盖更具体的“动漫”或“综艺”子目录。
    """
    raw = str(path_str or "").replace("\\", "/").strip("/")
    if not raw:
        return None, ""
    segments = [s for s in raw.split("/") if s]
    if not segments:
        return None, ""

    # 剥离末尾 Season N 及作品自身目录（若传入 show_folder_name 或路径含 [tmdbid=]）
    clean_show = str(show_folder_name or "").strip().lower()
    while segments and _SEASON_DIR_RE.match(segments[-1]):
        segments.pop()
    if segments:
        last_low = segments[-1].strip().lower()
        if (clean_show and last_low == clean_show) or "[tmdbid=" in last_low or re.search(r"\(\d{4}\)", last_low):
            segments.pop()

    # 从最深层向上逐级查找最近明确分类目录
    for seg in reversed(segments):
        cat, label = _match_category_dir_segment(seg)
        if cat:
            return cat, label
    return None, ""


async def resolve_record_gap_category(
    db: Any,
    rec: Any,
    instance_key: str | None = None,
) -> dict[str, Any]:
    """逐季作品补缺规则自动判定 (v2.5.45 规范 · 取消强制「类型待确认」 · G20):

    优先级（先按初检目录建立默认规则，再应用作品级证据）：
    1. 用户既有有效设置 (GapFillWorkPreference)，跨初检复用；
    2. 已核实同作品的元数据 / 可信 NFO 动画类型（如综艺目录中误放动画作品，自动改用动漫规则）；
    3. 最近明确分类目录（从 Season N / 剧集目录向上查找最近有效分类目录，如 动漫/国漫/日漫 → 动漫规则，综艺/真人秀 → 非动漫规则）；
    4. 剧集目录名、文件名及已有标签中的明确类型标记（不因标题语言、国家或压制组本身武断判动漫）；
    5. 目录与文件均无类型线索时记录明确诊断来源，绝不生成强制「⚠️ 类型待确认」阻塞选择项。
    """
    from sqlalchemy import select
    from app.models.gap_fill_site_rule import GapFillWorkPreference
    from app.models.task import Task

    show_name = str(get_record_field(rec, "show_name", "") or "")
    tmdb_id_val = get_record_field(rec, "tmdb_id")
    tmdb_id_str = str(tmdb_id_val).strip() if tmdb_id_val is not None else ""
    if not tmdb_id_str and show_name:
        m = re.search(r"\[tmdbid[=:](\d+)\]", show_name, re.I)
        if m:
            tmdb_id_str = m.group(1)

    clean_name = re.sub(r"\[tmdbid[=:]\d+\]", "", show_name, flags=re.I)
    clean_name = re.sub(r"[\(\[（].*?[\)\]）]", " ", clean_name)
    clean_name = re.sub(r"\s+\d{4}$", "", clean_name).strip() or show_name

    # 1. 用户既有有效人工设置（最高优先级，支持随时自愿修改并跨初检复用）
    if db is not None:
        try:
            pref = None
            if tmdb_id_str:
                stmt = select(GapFillWorkPreference).where(
                    GapFillWorkPreference.tmdb_id == tmdb_id_str
                )
                pref = (await db.execute(stmt)).scalars().first()
            if not pref and clean_name:
                stmt2 = select(GapFillWorkPreference).where(
                    GapFillWorkPreference.show_name == clean_name
                )
                pref = (await db.execute(stmt2)).scalars().first()
            if pref and pref.category in ("anime", "non_anime"):
                cat_label = "动漫" if pref.category == "anime" else "非动漫"
                return {
                    "category": pref.category,
                    "source": "manual",
                    "reason": f"用户已保存作品规则({cat_label})",
                    "auto_resolved": True,
                }
        except Exception:
            pass

    # 2. 从路径层级建立目录默认规则（向上查找最近明确分类目录，兼顾 Season N / 剧集目录 / 混合目录）
    season_path = str(get_record_field(rec, "season_path", "") or "")
    show_path = str(get_record_field(rec, "show_path", "") or "")
    show_folder = show_path.replace("\\", "/").rstrip("/").split("/")[-1] if show_path else show_name
    task_source_path = ""
    task_media_type = ""
    task_id = get_record_field(rec, "task_id")
    if task_id and db is not None:
        try:
            task = await db.get(Task, task_id)
            if task:
                task_media_type = str(getattr(task, "media_type", "") or "").lower()
                task_source_path = str(getattr(task, "source_path", "") or "")
        except Exception:
            pass

    dir_cat: str | None = None
    dir_label = ""
    for candidate_path in (season_path, show_path, task_source_path):
        if not candidate_path:
            continue
        c_cat, c_lbl = detect_category_from_path(candidate_path, show_folder_name=show_folder)
        if c_cat:
            dir_cat, dir_label = c_cat, c_lbl
            break
    if not dir_cat and task_media_type == "anime":
        dir_cat, dir_label = "anime", "动漫任务"

    # 3. 作品级元数据 / 可信 NFO 类型检测（优先级高于目录默认，可纠正错放目录的作品）
    evidence = _safe_json(
        get_record_field(rec, "evidence_json", None) or get_record_field(rec, "evidence", None), {}
    )
    if not isinstance(evidence, dict):
        evidence = {}
    matched_work = evidence.get("matched_work") or {}
    genres = (
        evidence.get("genres")
        or evidence.get("genre_ids")
        or (matched_work.get("genres") if isinstance(matched_work, dict) else None)
        or (matched_work.get("genre_ids") if isinstance(matched_work, dict) else None)
        or []
    )
    if isinstance(genres, str):
        genres = [genres]
    type_name = str(
        evidence.get("type_name")
        or evidence.get("type")
        or evidence.get("nfo_genre")
        or ""
    ).strip()

    is_anime_meta: bool | None = None
    if 16 in genres or "16" in [str(g) for g in genres]:
        is_anime_meta = True
    elif any("动画" in str(g) or "动漫" in str(g) or "animation" in str(g).lower() or "anime" in str(g).lower() for g in genres):
        is_anime_meta = True
    elif any(k in type_name.lower() for k in ("动漫", "动画", "動漫", "動畫", "animation", "anime")):
        is_anime_meta = True
    elif genres:
        # 具备非空且已核实的非动画分类列表
        is_anime_meta = False

    if is_anime_meta is True:
        override_note = f"（已按作品动画资料覆盖初检目录「{dir_label}」默认）" if (dir_cat == "non_anime" and dir_label) else ""
        return {
            "category": "anime",
            "source": "tmdb",
            "reason": f"根据已核实作品元数据（动画分类）采用动漫规则{override_note}",
            "directory_default": dir_cat or "",
            "auto_resolved": True,
        }

    if is_anime_meta is False:
        override_note = f"（已按作品真人资料覆盖初检目录「{dir_label}」默认）" if (dir_cat == "anime" and dir_label) else ""
        return {
            "category": "non_anime",
            "source": "tmdb",
            "reason": f"根据已核实作品元数据（非动画分类）采用非动漫规则{override_note}",
            "directory_default": dir_cat or "",
            "auto_resolved": True,
        }

    # 4. 元数据未含明确类型字段时，采用已建立的目录默认规则（不因接口缺字段一律降为非动漫）
    if dir_cat in ("anime", "non_anime"):
        rule_name = "动漫规则" if dir_cat == "anime" else "非动漫规则"
        return {
            "category": dir_cat,
            "source": "directory",
            "reason": f"根据初检目录「{dir_label}」采用{rule_name}",
            "directory_default": dir_cat,
            "auto_resolved": True,
        }

    # 5. 宽泛/混合目录且元数据缺字段：综合检查剧集目录名与样本文件名中的明确类型标记
    sample_files = ((evidence.get("release_evidence") or {}).get("sample_files") or []) if isinstance(evidence, dict) else []
    clue_text = " ".join([show_folder, show_name, *[str(x) for x in sample_files[:5]]])
    if re.search(r"(?:\[(?:Anime|动漫|动画|国漫|日漫|番剧)\]|(?:^|[\s._\-\[\(])(?:Anime|OVA|OAD|Animation)(?:$|[\s._\-\]\)]))", clue_text, re.I):
        return {
            "category": "anime",
            "source": "filename_clue",
            "reason": "根据作品目录/文件名明确动画标记采用动漫规则",
            "auto_resolved": True,
        }
    if re.search(r"(?:综艺|真人秀|脱口秀|纪录片|加更版|纯享版|先导片)", clue_text, re.I):
        return {
            "category": "non_anime",
            "source": "filename_clue",
            "reason": "根据作品目录/文件名明确综艺/真人标记采用非动漫规则",
            "auto_resolved": True,
        }

    # 6. 混合目录且完全无类型线索：检查是否为元数据读取失败，记录可恢复诊断而非弹强制「类型待确认」
    meta_err = str(evidence.get("meta_error") or "").strip()
    if meta_err or evidence.get("meta_status") in ("fetch_failed", "query_failed"):
        return {
            "category": "non_anime",
            "source": "metadata_fetch_failed_fallback",
            "reason": f"混合目录且作品资料读取失败（{meta_err or '接口暂不可用'}），暂按非动漫规则处理，资料恢复后自动复核",
            "metadata_fetch_failed": True,
            "recovery_hint": "可刷新作品元数据或在详情中手动切换规则类型",
            "auto_resolved": True,
        }

    return {
        "category": "non_anime",
        "source": "mixed_default",
        "reason": "混合目录未见动画分类标记，按非动漫规则处理（可在详情中自愿切换）",
        "auto_resolved": True,
    }

