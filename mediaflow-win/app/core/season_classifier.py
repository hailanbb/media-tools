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
}

# 属于元数据异常的 meta_status
ANOMALY_META_STATUSES = {
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


def classify_single_record(
    rec: Any,
    pending_dir_group_ids: set[str] | None = None,
) -> tuple[str, str]:
    """判定单个物理剧季记录的基础分类与动作类型.

    返回: (category, action_type)
    category in ('anomaly', 'airing', 'download', 'review', 'archive')
    """
    status = str(get_record_field(rec, "status") or "")
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
    elif total_eps and rec_eps and max(rec_eps) > total_eps and status not in ("archive_ready", "airing"):
        is_anomaly = True
        action_type = "inspect_info"

    if is_anomaly:
        return "anomaly", action_type

    # 2. 连载留源 (airing) 判定
    if status == "airing" or (season_finished is False and status not in ("archive_ready", "period_ready")):
        return "airing", "skip"

    # 3. 真缺集待补 (download) 判定（含缺集兼重复，缺集优先补齐）
    if status in ("download_needed", "period_missing") or (missing and status not in ("archive_ready", "period_ready")):
        gap_state = get_record_field(rec, "gap_fill_state") or {}
        if gap_state.get("view_bucket") == "pushed_pending":
            return "download", "inspect_gap_detail"
        return "download", "download"

    # 4. 待复核清理 (review) 判定（正片已齐但有重复副本或可淘汰冗余）
    if status == "review_needed" or (duplicates and not missing):
        return "review", "clean"

    # 5. 齐全可归档 (archive) 判定
    if status in ("archive_ready", "period_ready") or (
        not missing and not duplicates and total_eps and len(rec_eps) >= total_eps
    ):
        return "archive", "archive"

    # 兜底
    if missing:
        return "download", "download"
    return "archive", "archive"


def build_work_key(rec: Any) -> str:
    """构建作品季唯一聚合主键 (tmdb_id, season_number) 或 (show_name, season_number)."""
    tmdb_id = get_record_field(rec, "tmdb_id")
    season_number = get_record_field(rec, "season_number", 1)
    if tmdb_id and str(tmdb_id).isdigit() and int(tmdb_id) > 0:
        return f"tmdb:{tmdb_id}:S{season_number}"
    show_name = str(get_record_field(rec, "show_name") or "unknown").strip().lower()
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
        "system_tracking": 0,
        "total_work_seasons": 0,
    }

    # 2. 逐组裁定唯一主待办
    for work_key, items in groups.items():
        # 取组内最优先的分类 (priority 数值最小)
        best_cat = min(items, key=lambda x: CATEGORY_PRIORITY.get(x[1], 99))[1]

        # 从该分类中选出最具有代表性的记录作为主记录
        candidates = [item for item in items if item[1] == best_cat]
        # 优先选本地正片数更多、或有路径更明确的记录
        def _rank_candidate(cand: tuple[Any, str, str]) -> int:
            rec = cand[0]
            rec_eps = _safe_json(
                get_record_field(rec, "recognized_episodes_json") or get_record_field(rec, "recognized_episodes"), []
            )
            return len(rec_eps)

        primary_item = max(candidates, key=_rank_candidate)
        primary_rec, _, primary_act = primary_item

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
                "recognized_episodes": _safe_json(getattr(primary_rec, "recognized_episodes_json", None), []),
                "missing_episodes": _safe_json(getattr(primary_rec, "missing_episodes_json", None), []),
                "duplicate_episodes": _safe_json(getattr(primary_rec, "duplicate_episodes_json", None), []),
                "status": getattr(primary_rec, "status", None),
                "status_detail": getattr(primary_rec, "status_detail", None),
                "archive_target_path": getattr(primary_rec, "archive_target_path", None),
                "season_finished": getattr(primary_rec, "season_finished", None),
                "evidence": _safe_json(getattr(primary_rec, "evidence_json", None), {}),
                "conflict": _safe_json(getattr(primary_rec, "conflict_json", None), {}),
                "mapping": _safe_json(getattr(primary_rec, "mapping_json", None), {}),
                "last_checked_at": (
                    primary_rec.last_checked_at.isoformat() if getattr(primary_rec, "last_checked_at", None) else None
                ),
            }

        base_dict["main_category"] = best_cat
        base_dict["action_type"] = primary_act
        base_dict["is_primary_work_record"] = True
        base_dict["associated_records"] = associated
        base_dict["total_dir_count"] = len(items)

        # 检查是否全部已推送待入库
        gap_state = base_dict.get("gap_fill_state") or {}
        is_pushed_pending = (best_cat == "download" and gap_state.get("view_bucket") == "pushed_pending")

        # 计入统计
        counts[best_cat] += 1
        if is_pushed_pending:
            counts["download_pushed"] += 1
        if best_cat == "anomaly" and (primary_act == "system_tracking" or getattr(primary_rec, "status", None) == "period_review"):
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
