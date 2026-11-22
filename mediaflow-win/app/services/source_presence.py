"""按用户请求核对源路径；目录不存在与读取失败严格分开，不操作媒体。"""
import json
import posixpath
from datetime import datetime, timezone

from app.core.source_cleanup_assets import checked_listing
from app.models.operation import OperationLog


async def source_entry(client, path, root):
    root = (root or '').rstrip('/')
    if not root or path != posixpath.normpath(path) or not path.startswith(root + '/'):
        raise ValueError('源路径不在受控源根的严格子目录内')
    parent = posixpath.dirname(path)
    try:
        items = checked_listing(await client.list_files(parent, force_refresh=True), parent)
    except Exception as exc:
        # 只有明确 NOT_FOUND 可继续向上核对；超时/权限/异常清单不能当作删除。
        if not (isinstance(exc, FileNotFoundError) or getattr(exc, 'status', None) == 5) or parent == root:
            raise
        if await source_entry(client, parent, root) is not None:
            raise ValueError('父目录存在但读取失败，暂不能核实源路径') from exc
        return None
    hit = next((x for x in items if x['path'] == path), None)
    if hit is not None and not hit['isDirectory']:
        raise ValueError('原目录路径已被文件占用，请手动复核')
    return hit


async def record_source_absent(db, rec, *, show_absent=False, reason='已强读核实原源目录不存在；可能已在外部删除或移动，不代表已归档'):
    """仅结束该物理路径的待办；不取消下载、不伪造媒体删除或归档回执。"""
    evidence = json.loads(rec.evidence_json or '{}')
    evidence['source_presence'] = dict(state='absent', path=rec.season_path, show_absent=show_absent,
        checked_at=datetime.now(timezone.utc).isoformat(), media_operation=False)
    rec.evidence_json = json.dumps(evidence, ensure_ascii=False)
    rec.status = 'source_absent'
    rec.status_detail = reason
    rec.local_episode_count = 0
    rec.recognized_episodes_json = '[]'
    rec.duplicate_episodes_json = '[]'
    db.add(OperationLog(task_id=rec.task_id, season_record_id=rec.id,
        action='reconcile_source_absence', status='completed',
        detail_json=json.dumps(dict(path=rec.season_path, reason=reason,
            media_operation=False, archive_verified=False), ensure_ascii=False)))


def group_sources_absent(entries, records):
    """全部源剧根有外部消失证据才隐藏旧组；保留组历史及归档多目录问题。"""
    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        return False
    sources = {e.get('norm_path') for e in entries if e.get('side') == 'source'}
    if not sources or sum(e.get('side') == 'archive' for e in entries) > 1:
        return False
    for path in sources:
        rows = [r for r in records if r.show_path == path]
        if not rows or any(r.status != 'source_absent' for r in rows):
            return False
        try:
            verified = any((json.loads(r.evidence_json or '{}').get('source_presence') or {}).get('show_absent') is True for r in rows)
        except (ValueError, TypeError, AttributeError):
            return False
        if not verified:
            return False
    return True
