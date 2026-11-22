"""只读恢复旧版未完成的剧根收尾待办，不在列表读取时操作媒体或改写记录。"""
import json

from sqlalchemy import select
from app.models.operation import OperationLog


async def pending_root_cleanup_ids(db, records):
    candidates, pending = [], set()
    for rec in records:
        if rec.status != "cleaned_redundant":
            continue
        try:
            state = json.loads(rec.evidence_json or "{}").get("source_root_cleanup") or {}
        except (ValueError, TypeError):
            state = {}
        # 本季持久化结果优先，避免秒级时间戳相同的历史日志按随机 ID 排序后复活旧待办。
        if state.get("cleaned") is True or state.get("status") == "retained_expected":
            continue
        if state:
            pending.add(rec.id)
        else:
            candidates.append(rec.id)
    if not candidates:
        return pending
    logs = (await db.execute(select(OperationLog).where(
        OperationLog.season_record_id.in_(candidates),
        OperationLog.action == "clean_source_redundant",
    ).order_by(OperationLog.created_at.desc(), OperationLog.id.desc()))).scalars().all()
    seen = set()
    for row in logs:
        if row.season_record_id in seen:
            continue
        seen.add(row.season_record_id)
        try:
            data = json.loads(row.detail_json or "{}")
        except (ValueError, TypeError):
            continue
        cleanup = data.get("shell_cleanup") or {}
        if data.get("shell_cleaned") is False and cleanup.get("status") != "retained_expected":
            pending.add(row.season_record_id)
    return pending
