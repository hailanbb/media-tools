"""媒体整理修复服务层 — 调度失败发现、接管基线、逐文件诊断、执行与结果核验。"""

from __future__ import annotations

import json
import logging
import os
import posixpath
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.moviepilot import MoviePilotClient
from app.core.transfer_repair import (
    calculate_plan_hash,
    diagnose_failure_record,
    extract_season_and_episodes,
    normalize_file_path,
)
from app.models.transfer_repair import (
    TransferRepairBaseline,
    TransferRepairItem,
    TransferRepairRule,
)

log = logging.getLogger("mediaflow.transfer_repair_service")


async def get_or_create_baseline(db: AsyncSession, mp_client: MoviePilotClient | None = None) -> TransferRepairBaseline:
    """获取或初始化接管基线（R01, R29: 首次启用建立基线，历史失败不导入待修复）。"""
    res = await db.execute(select(TransferRepairBaseline).order_by(TransferRepairBaseline.id.asc()).limit(1))
    baseline = res.scalar_one_or_none()
    if baseline:
        return baseline

    # 初次建立基线
    max_history_id = 0
    if mp_client:
        try:
            page_data = await mp_client.get_transfer_history_paginated(status=False, page=1, count=1)
            records = page_data.get("list", [])
            if records and isinstance(records[0], dict) and records[0].get("id"):
                max_history_id = int(records[0]["id"])
        except Exception as e:
            log.warning("初次读取 MP 历史获取基线 ID 失败: %s", e)

    baseline = TransferRepairBaseline(
        instance_id="default",
        takeover_at=datetime.now(timezone.utc),
        baseline_history_id=max_history_id,
        last_scanned_history_id=max_history_id,
        is_enabled=True,
        auto_repair_enabled=False,
    )
    db.add(baseline)
    await db.commit()
    await db.refresh(baseline)
    log.info("已建立媒体整理修复接管基线: 起点历史 ID = %d", max_history_id)
    return baseline


async def scan_new_failures(
    db: AsyncSession,
    mp_client: MoviePilotClient,
    max_pages: int = 5,
    page_size: int = 50,
) -> dict[str, Any]:
    """增量扫描 MoviePilot 新发生的整理失败并执行智能诊断 (R01, R02, R29, R30)。"""
    baseline = await get_or_create_baseline(db, mp_client)
    if not baseline.is_enabled:
        return {"status": "disabled", "message": "自动扫描已暂停"}

    # 加载当前有效规则
    rules_res = await db.execute(
        select(TransferRepairRule).where(TransferRepairRule.is_active == True)  # noqa: E712
    )
    active_rules = [
        {
            "id": r.id,
            "scope_kind": r.scope_kind,
            "scope_key": r.scope_key,
            "show_title": r.show_title,
            "season_number": r.season_number,
            "episode_offset": r.episode_offset,
        }
        for r in rules_res.scalars().all()
    ]

    new_failures: list[dict[str, Any]] = []
    scanned_total = 0
    highest_seen_id = baseline.last_scanned_history_id or baseline.baseline_history_id

    for page in range(1, max_pages + 1):
        try:
            data = await mp_client.get_transfer_history_paginated(status=False, page=page, count=page_size)
        except Exception as exc:
            log.error("扫描 MP 失败历史异常 (page %d): %s", page, exc)
            break

        records = data.get("list", [])
        if not records:
            break

        scanned_total += len(records)
        reached_baseline = False

        for rec in records:
            rec_id = int(rec.get("id") or 0)
            if rec_id > highest_seen_id:
                highest_seen_id = rec_id

            # R29/R30: 严格过滤基线之前的旧历史！
            if rec_id <= baseline.baseline_history_id:
                reached_baseline = True
                continue

            new_failures.append(rec)

        if reached_baseline or len(records) < page_size:
            break

    # 处理新失败记录，归并至文件修复项
    created_count = 0
    updated_count = 0

    for rec in new_failures:
        rec_id = int(rec.get("id") or 0)
        src_path = normalize_file_path(
            rec.get("src")
            or rec.get("source_path")
            or rec.get("file_path")
            or rec.get("path")
            or ""
        )
        if not src_path:
            continue

        # 检查是否已存在该源文件的修复项
        item_res = await db.execute(
            select(TransferRepairItem).where(TransferRepairItem.source_path == src_path)
        )
        item = item_res.scalar_one_or_none()

        # 检查源文件物理存在性与健康度
        source_exists = os.path.exists(src_path) if os.name == "nt" or "/" in src_path else False
        file_size = os.path.getsize(src_path) if source_exists else 0
        file_mtime = (
            datetime.fromtimestamp(os.path.getmtime(src_path), tz=timezone.utc)
            if source_exists
            else None
        )

        diag = diagnose_failure_record(
            rec,
            source_exists=source_exists,
            target_exists=False,
            storage_healthy=True,
            active_rules=active_rules,
        )

        if item:
            # 增量关联历史 ID
            existing_ids = json.loads(item.mp_history_ids_json or "[]")
            if rec_id not in existing_ids:
                existing_ids.append(rec_id)
            item.mp_history_ids_json = json.dumps(existing_ids, ensure_ascii=False)
            item.latest_history_id = max(item.latest_history_id, rec_id)
            item.raw_error = str(rec.get("error") or rec.get("reason") or item.raw_error)
            item.updated_at = datetime.now(timezone.utc)
            updated_count += 1
        else:
            item = TransferRepairItem(
                source_path=src_path,
                file_basename=posixpath.basename(src_path),
                file_size=file_size,
                file_mtime=file_mtime,
                mp_history_ids_json=json.dumps([rec_id], ensure_ascii=False),
                latest_history_id=rec_id,
                raw_error=str(rec.get("error") or rec.get("reason") or ""),
                error_category=diag["error_category"],
                main_block_reason=diag["main_block_reason"],
                title=diag["title"],
                media_type=diag["media_type"],
                tmdb_id=diag["tmdb_id"],
                season_number=diag["season_number"],
                episode_numbers_json=json.dumps(diag["episode_numbers"], ensure_ascii=False),
                part=diag["part"],
                is_special=diag["is_special"],
                special_type=diag["special_type"],
                status=diag["status"],
                resolution_code=diag["resolution_code"],
                skip_reason=diag["skip_reason"],
                plan_json=json.dumps(diag["plan_json"], ensure_ascii=False),
                plan_hash=diag["plan_hash"],
            )
            db.add(item)
            created_count += 1

    baseline.last_scanned_history_id = highest_seen_id
    baseline.last_scanned_at = datetime.now(timezone.utc)
    await db.commit()

    return {
        "status": "success",
        "baseline_history_id": baseline.baseline_history_id,
        "scanned_records": scanned_total,
        "new_failures_found": len(new_failures),
        "new_items_created": created_count,
        "items_updated": updated_count,
    }


async def get_repair_summary(db: AsyncSession, mp_client: MoviePilotClient | None = None) -> dict[str, Any]:
    """获取媒体整理修复仪表盘统计数据 (第 9.1 节)。"""
    baseline = await get_or_create_baseline(db, mp_client)

    # 查运行中队列
    mp_queue_count = 0
    if mp_client:
        try:
            q = await mp_client.get_transfer_queue()
            mp_queue_count = len(q) if isinstance(q, list) else 0
        except Exception:
            pass

    # 聚合各类状态数量
    res = await db.execute(
        select(
            TransferRepairItem.status,
            TransferRepairItem.resolution_code,
            func.count(TransferRepairItem.id),
        ).group_by(TransferRepairItem.status, TransferRepairItem.resolution_code)
    )
    rows = res.all()

    counts: dict[str, int] = {
        "pending": 0,
        "ready": 0,
        "waiting_env": 0,
        "needs_confirmation": 0,
        "executing": 0,
        "resolved_today": 0,
        "already_present_verified": 0,
        "repaired_today": 0,
        "skipped": 0,
        "protected": 0,
    }

    for st, res_code, cnt in rows:
        if st in ("discovered", "diagnosing", "ready", "needs_confirmation", "waiting_source", "waiting_environment", "conflict"):
            counts["pending"] += cnt
        if st == "ready":
            counts["ready"] += cnt
        elif st == "waiting_environment":
            counts["waiting_env"] += cnt
        elif st == "needs_confirmation":
            counts["needs_confirmation"] += cnt
        elif st in ("submitted", "verifying"):
            counts["executing"] += cnt
        elif st == "resolved":
            counts["resolved_today"] += cnt
            if res_code == "already_present_verified":
                counts["already_present_verified"] += cnt
            elif res_code == "repaired":
                counts["repaired_today"] += cnt
        elif st == "skipped":
            counts["skipped"] += cnt
        elif st == "protected":
            counts["protected"] += cnt

    return {
        "baseline_history_id": baseline.baseline_history_id,
        "takeover_at": baseline.takeover_at.isoformat() if baseline.takeover_at else None,
        "last_scanned_at": baseline.last_scanned_at.isoformat() if baseline.last_scanned_at else None,
        "is_enabled": baseline.is_enabled,
        "auto_repair_enabled": baseline.auto_repair_enabled,
        "max_concurrency": baseline.max_concurrency,
        "batch_quota": baseline.batch_quota,
        "retry_budget": baseline.retry_budget,
        "mp_queue_count": mp_queue_count,
        "counts": counts,
    }


async def get_repair_items(
    db: AsyncSession,
    tab: str = "pending",
    category: str | None = None,
    keyword: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """查询媒体整理修复项列表，支持标签页分流与作品聚合。"""
    query = select(TransferRepairItem)

    if tab == "pending":
        query = query.where(
            TransferRepairItem.status.in_(
                ["discovered", "diagnosing", "ready", "needs_confirmation", "waiting_source", "waiting_environment", "conflict"]
            )
        )
    elif tab == "executing":
        query = query.where(TransferRepairItem.status.in_(["submitted", "verifying"]))
    elif tab == "resolved":
        query = query.where(TransferRepairItem.status == "resolved")
    elif tab == "rules_records":
        query = query.where(TransferRepairItem.status.in_(["skipped", "protected"]))

    if category:
        query = query.where(TransferRepairItem.error_category == category)
    if keyword:
        query = query.where(
            (TransferRepairItem.title.ilike(f"%{keyword}%"))
            | (TransferRepairItem.file_basename.ilike(f"%{keyword}%"))
        )

    # 查总数
    total_res = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_res.scalar() or 0

    # 分页查列表
    query = query.order_by(TransferRepairItem.updated_at.desc()).offset(offset).limit(limit)
    items = (await db.execute(query)).scalars().all()

    items_data = []
    for it in items:
        plan = json.loads(it.plan_json or "{}")
        eps = json.loads(it.episode_numbers_json or "[]")
        items_data.append(
            {
                "id": it.id,
                "source_path": it.source_path,
                "file_basename": it.file_basename,
                "file_size": it.file_size,
                "title": it.title,
                "media_type": it.media_type,
                "tmdb_id": it.tmdb_id,
                "season_number": it.season_number,
                "episode_numbers": eps,
                "part": it.part,
                "is_special": it.is_special,
                "special_type": it.special_type,
                "status": it.status,
                "error_category": it.error_category,
                "main_block_reason": it.main_block_reason,
                "raw_error": it.raw_error,
                "resolution_code": it.resolution_code,
                "skip_reason": it.skip_reason,
                "plan_hash": it.plan_hash,
                "plan": plan,
                "is_protected": it.is_protected,
                "retry_count": it.retry_count,
                "updated_at": it.updated_at.isoformat() if it.updated_at else None,
            }
        )

    return {"total": total, "items": items_data}


async def update_item_plan_choice(
    db: AsyncSession,
    item_id: str,
    choice_data: dict[str, Any],
) -> dict[str, Any]:
    """用户补充一次选择并重新生成核验计划 (第 9.4 节)。"""
    item = await db.get(TransferRepairItem, item_id)
    if not item:
        return {"ok": False, "message": "修复项不存在"}

    title = str(choice_data.get("title") or item.title or "").strip()
    media_type = str(choice_data.get("media_type") or item.media_type or "tv")
    season_number = choice_data.get("season_number")
    if season_number is not None:
        season_number = int(season_number)
    episodes = choice_data.get("episode_numbers")
    if episodes is not None and isinstance(episodes, list):
        episodes = [int(e) for e in episodes]
    else:
        episodes = json.loads(item.episode_numbers_json or "[]")

    part_str = choice_data.get("part") or item.part
    is_special = bool(choice_data.get("is_special", item.is_special))
    special_type = choice_data.get("special_type") or item.special_type

    # 更新计划与哈希
    plan = json.loads(item.plan_json or "{}")
    plan.update(
        {
            "title": title,
            "media_type": media_type,
            "season_number": season_number,
            "episode_numbers": episodes,
            "part": part_str,
            "is_special": is_special,
            "special_type": special_type,
            "user_confirmed": True,
            "safe_to_execute": bool(title and (media_type == "movie" or (season_number and (episodes or is_special)))),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    p_hash = calculate_plan_hash(plan)

    # 判定新状态
    if plan["safe_to_execute"]:
        item.status = "ready"
        item.main_block_reason = f"用户已确认《{title}》参数，预览一致，等待整理"
    else:
        item.status = "needs_confirmation"

    item.title = title
    item.media_type = media_type
    item.season_number = season_number
    item.episode_numbers_json = json.dumps(episodes, ensure_ascii=False)
    item.part = part_str
    item.is_special = is_special
    item.special_type = special_type
    item.plan_json = json.dumps(plan, ensure_ascii=False)
    item.plan_hash = p_hash
    item.updated_at = datetime.now(timezone.utc)

    # 若用户勾选了复用规则 (第 6.4 节)，创建新规则
    remember_scope = choice_data.get("remember_scope")
    if remember_scope in ("this_package", "this_dir_template"):
        parent_dir = posixpath.dirname(item.source_path)
        scope_key = posixpath.basename(parent_dir) if remember_scope == "this_package" else parent_dir
        rule = TransferRepairRule(
            scope_kind=remember_scope,
            scope_key=scope_key,
            show_title=title,
            media_type=media_type,
            season_number=season_number or 1,
            numbering_mode=str(choice_data.get("numbering_mode") or "standard"),
            episode_offset=int(choice_data.get("episode_offset") or 0),
            note=f"由修复项 #{item.id[:8]} 确认保存",
        )
        db.add(rule)

    await db.commit()
    return {"ok": True, "plan_hash": p_hash, "status": item.status, "message": "选择已更新并生成计划"}


async def execute_repair_plans(
    db: AsyncSession,
    mp_client: MoviePilotClient,
    item_ids: list[str],
) -> dict[str, Any]:
    """批量或单项执行已就绪的整理计划 (第 7 节)。"""
    baseline = await get_or_create_baseline(db, mp_client)
    res = await db.execute(select(TransferRepairItem).where(TransferRepairItem.id.in_(item_ids)))
    items = res.scalars().all()

    results: list[dict[str, Any]] = []

    for item in items:
        if item.is_protected:
            results.append({"id": item.id, "ok": False, "reason": "条目已被保护，跳过执行"})
            continue

        if item.status not in ("ready", "needs_confirmation"):
            results.append({"id": item.id, "ok": False, "reason": f"当前状态 [{item.status}] 不支持直接执行"})
            continue

        plan = json.loads(item.plan_json or "{}")
        # 组装 MP v2.15.6 ManualTransferItem 契约载荷
        episodes = json.loads(item.episode_numbers_json or "[]")
        ep_detail = episodes[0] if len(episodes) == 1 else None

        payload: dict[str, Any] = {
            "logid": item.latest_history_id if item.latest_history_id > 0 else None,
            "in_path": item.source_path,
            "type_name": "电影" if item.media_type == "movie" else "电视剧",
            "tmdbid": item.tmdb_id,
            "season": item.season_number,
            "episode_detail": ep_detail,
            "episode_part": item.part,
            "from_history": False,
            "reorganize": False,
            "preview": False,
        }

        try:
            mp_res = await mp_client.manual_transfer_v2(payload)
            success = bool(mp_res.get("success", True) if isinstance(mp_res, dict) else True)
            msg = mp_res.get("message") if isinstance(mp_res, dict) else "请求已提交"

            if success:
                item.status = "resolved"
                item.resolution_code = "repaired"
                item.main_block_reason = "已完成手动整理并由 MoviePilot 受理"
                item.last_execution_json = json.dumps(
                    {
                        "executed_at": datetime.now(timezone.utc).isoformat(),
                        "payload": payload,
                        "response": mp_res,
                    },
                    ensure_ascii=False,
                )
                results.append({"id": item.id, "ok": True, "message": msg})
            else:
                item.retry_count += 1
                if item.retry_count >= baseline.retry_budget:
                    item.status = "waiting_environment"
                    item.main_block_reason = f"多次重试均未成功: {msg}"
                results.append({"id": item.id, "ok": False, "reason": msg})

        except Exception as e:
            item.retry_count += 1
            results.append({"id": item.id, "ok": False, "reason": f"执行网络异常: {e}"})

        item.updated_at = datetime.now(timezone.utc)

    await db.commit()
    return {"results": results, "total": len(items)}
