"""用户明确放弃补缺后的单季删源；无其他需保留季时一并清理剧集目录并刷新 CD2。"""
import asyncio
import json
import logging
import posixpath
import re
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.clients.clouddrive2 import CloudDrive2Client
from app.clients.moviepilot import MoviePilotClient, parse_season_str
from app.config import get_settings
from app.core import batch_journal
from app.core.auxiliary_inventory import is_ignorable_junk_or_scrape_temp
from app.core.directory_merge import _directory_inventory_snapshot
from app.core.episode_parser import is_subtitle_file, is_video_file
from app.core.season_classifier import TERMINAL_STATUSES, build_work_key, get_record_work_main_category
from app.database import get_db
from app.models.gap_fill import GapFillEpisode
from app.models.operation import OperationLog
from app.models.season import SeasonRecord
from app.models.task import Task
from app.services.gap_fill_tracker import (
    IN_FLIGHT_RECONCILE_STATES,
    compute_season_gap_fill_view,
    refresh_task_summary,
)
from app.services.source_presence import record_source_absent, source_entry

router = APIRouter()
log = logging.getLogger(__name__)
_BATCH_LOCKS = {}

_SAFE_SHOW_ROOT_EXTS = {
    ".nfo", ".jpg", ".jpeg", ".png", ".webp", ".avif", ".tbn", ".svg", ".gif", ".bmp",
    ".xml", ".txt", ".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx",
}
_INACTIVE_SHOW_SEASON_STATUSES = {"source_absent", "archived", "cleaned_redundant"}


class PreviewRequest(BaseModel):
    season_record_id: str


class ExecuteRequest(PreviewRequest):
    preview_token: str
    confirm_abandon: bool = False


class BatchPreviewRequest(BaseModel):
    task_id: str
    season_record_ids: list[str] = Field(min_length=1, max_length=100)


class BatchExecuteRequest(BaseModel):
    preview_token: str
    confirm_abandon: bool = False


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _is_safe_show_root_companion(item: dict) -> bool:
    """判断剧集根目录下除待删季之外的条目是否仅为纯刮削伴生文件或系统隐藏垃圾（零子目录、零视频、零 STRM）。"""
    if item.get("isDirectory"):
        return False
    name = str(item.get("name") or "").strip()
    if not name or is_video_file(name) or name.lower().endswith(".strm"):
        return False
    if is_ignorable_junk_or_scrape_temp(name):
        return True
    ext = posixpath.splitext(name.lower())[1]
    return ext in _SAFE_SHOW_ROOT_EXTS


async def _evaluate_show_dir_deletion(
    db,
    rec: SeasonRecord,
    season_path: str,
    show_items: list[dict],
) -> tuple[bool, str, str, list[dict]]:
    """核验该剧集在数据库与物理网盘双端是否均已无其他需保留季或媒体文件。"""
    settings = get_settings()
    root = posixpath.normpath(settings.source_root or "").rstrip("/")
    archive = posixpath.normpath(settings.archive_root or "").rstrip("/")
    show_path = posixpath.normpath(rec.show_path or "")
    task = await db.get(Task, rec.task_id)
    task_source_path = posixpath.normpath(task.source_path) if (task and task.source_path) else ""

    if not show_path or posixpath.dirname(season_path) != show_path:
        return False, show_path, task_source_path, []
    if not root or not show_path.startswith(root + "/") or show_path == root:
        return False, show_path, task_source_path, []
    if task_source_path and show_path == task_source_path:
        return False, show_path, task_source_path, []
    if archive and (show_path == archive or show_path.startswith(archive + "/")):
        return False, show_path, task_source_path, []

    other_db_rows = list(
        (
            await db.scalars(
                select(SeasonRecord).where(
                    SeasonRecord.show_path == rec.show_path,
                    SeasonRecord.id != rec.id,
                )
            )
        ).all()
    )
    if any(r.status not in _INACTIVE_SHOW_SEASON_STATUSES for r in other_db_rows):
        return False, show_path, task_source_path, []

    other_items = [x for x in show_items if posixpath.normpath(x.get("path") or "") != season_path]
    if not all(_is_safe_show_root_companion(x) for x in other_items):
        return False, show_path, task_source_path, []

    return True, show_path, task_source_path, other_items


async def require_no_resource(db, rec):
    if rec is None or (await compute_season_gap_fill_view(db, rec)).get('view_bucket') != 'no_resource':
        reject("本季已不属于无合格资源清单，请更新预览；在途、接口失败及未搜索项不参与批量删源")
    related = (await db.scalars(select(SeasonRecord).where(
        SeasonRecord.task_id == rec.task_id,
        SeasonRecord.season_number == rec.season_number,
        SeasonRecord.status.not_in(tuple(TERMINAL_STATUSES)),
    ))).all()
    for other in related:
        if other.id != rec.id and build_work_key(other) == build_work_key(rec):
            if (await compute_season_gap_fill_view(db, other)).get('view_bucket') != 'no_resource':
                reject("同一作品季的其他目录尚未满足无合格资源条件，请更新清单后逐项核对")


async def read_listing(cd2, path):
    listing = await cd2.list_files(path, force_refresh=True)
    if not isinstance(listing, list):
        reject("目录读取未返回完整清单，不能确认删除结果")
    return listing


async def _verify_deleted_from_parent(cd2, parent_dir: str, deleted_path: str, error_msg: str) -> list[dict]:
    """强读父目录确认目标已消失（兼容 115 / CD2 删后毫秒级父目录缓存短延迟退避）。"""
    listing = await read_listing(cd2, parent_dir)
    if any(posixpath.normpath(x.get("path") or "") == deleted_path for x in listing) and _has_running_loop():
        for delay in (0.4, 0.8, 1.5):
            await asyncio.sleep(delay)
            listing = await read_listing(cd2, parent_dir)
            if not any(posixpath.normpath(x.get("path") or "") == deleted_path for x in listing):
                break
    if any(posixpath.normpath(x.get("path") or "") == deleted_path for x in listing):
        reject(error_msg)
    return listing


async def _refresh_cd2_directories(cd2, *dirs: str) -> None:
    """对受影响的父目录及初检根目录执行 CloudDrive2 强制刷新。"""
    settings = get_settings()
    root = posixpath.normpath(settings.source_root or "").rstrip("/")
    seen: set[str] = set()
    for raw_d in dirs:
        if not raw_d:
            continue
        norm_d = posixpath.normpath(raw_d)
        if norm_d in seen:
            continue
        if root and not (norm_d == root or norm_d.startswith(root + "/")):
            continue
        seen.add(norm_d)
        try:
            await cd2.list_files(norm_d, force_refresh=True)
        except Exception as exc:
            log.warning("刷新 CloudDrive2 目录缓存异常 (%s): %s", norm_d, exc)


def reject(message):
    raise HTTPException(status_code=409, detail=message)


async def check_scope(db, rec):
    if rec is None:
        reject("季记录已不存在")
    from app.core.season_classifier import get_record_work_context
    work = await get_record_work_context(db, rec)
    if (work.get("gap_fill_state") or {}).get("review_pending"):
        reject("补缺后复核尚未完成，请先核对入库；暂不开放删除源端")
    if await get_record_work_main_category(db, rec) != "download":
        reject("主动放弃补缺仅适用于当前缺集待补的单季")
    task = await db.get(Task, rec.task_id)
    if task and task.status in ("running", "paused", "stopping"):
        reject("当前初检任务尚未结束，请结束后再处理本季")
    settings = get_settings()
    path = posixpath.normpath(rec.season_path or "")
    show = posixpath.normpath(rec.show_path or "")
    root = posixpath.normpath(settings.source_root or "")
    archive = posixpath.normpath(settings.archive_root or "")
    if not settings.source_root or not path.startswith(root.rstrip('/') + '/') or path == root:
        reject("本季目录不在受控源目录内")
    if settings.archive_root and (path == archive or path.startswith(archive.rstrip('/') + '/')):
        reject("目标位于归档库，禁止删除")
    if posixpath.dirname(path) != show or not re.fullmatch(rf"(?:Season\s*|S)0*{rec.season_number}", posixpath.basename(path), re.I):
        reject("仅支持独立的本季目录；平铺整剧或共享目录不能按季整目录删除")
    shared = list((await db.scalars(select(SeasonRecord).where(SeasonRecord.season_path == rec.season_path,
        SeasonRecord.season_number != rec.season_number, SeasonRecord.status != "source_absent"))).all())
    if shared:
        reject("本目录还被其他季引用，禁止删除")
    inflight = list((await db.scalars(select(GapFillEpisode).where(
        GapFillEpisode.season_path == path, GapFillEpisode.state.in_(tuple(IN_FLIGHT_RECONCILE_STATES))))).all())
    if inflight:
        reject("本季仍有在途或提交待确认的补缺任务，请先完成对账")
    if settings.mp_url and settings.mp_token:
        mp = MoviePilotClient()
        try:
            downloads = await mp.get_active_downloads()
            if not isinstance(downloads, list):
                reject("无法核验 MoviePilot 活跃下载状态")
            for dl in downloads:
                tid = str(dl.get('tmdb_id') or dl.get('tmdbid') or '')
                paths = [str(dl.get(k) or '').rstrip('/') for k in ('path','save_path','content_path','target_path')]
                title = str(dl.get('title') or dl.get('name') or '')
                name = re.sub(r'\s*[（(]\d{4}[)）].*$', '', rec.show_name or '').strip()
                overlaps = any(p and (p == path or p.startswith(path+'/') or path.startswith(p+'/')) for p in paths)
                seasons = parse_season_str(dl.get('season_number') or dl.get('season') or dl.get('seasons') or '')
                if seasons and rec.season_number not in seasons and not overlaps:
                    continue
                if (tid and tid == str(rec.tmdb_id)) or overlaps or (name and name in title):
                    reject("MoviePilot 仍有该作品相关下载，请先结束下载并对账")
        except HTTPException:
            raise
        except Exception:
            reject("MoviePilot 下载状态读取失败，暂不能确认删源范围")
        finally:
            await mp.close()
    return path


async def inventory(cd2, path):
    snapshots = {}
    pending = [(path, 0)]
    total = 0
    while pending:
        parent, depth = pending.pop()
        if depth > 8:
            reject("目录层级过深，无法完整预览")
        items = await cd2.list_files(parent, force_refresh=True)
        if not isinstance(items, list):
            reject("目录读取未返回完整清单")
        snapshots[parent] = _directory_inventory_snapshot(items)
        total += len(items)
        if total > 20000:
            reject("单季目录条目过多，无法在本次预览中完整核验")
        for item in items:
            child = posixpath.normpath(item.get('path') or '')
            if posixpath.dirname(child) != parent or child == parent:
                reject("目录清单出现范围外路径，已停止预览")
            if item.get('isDirectory'):
                pending.append((child, depth+1))
    return snapshots


@router.post('/discard-source-season/preview')
async def preview(body: PreviewRequest, db=Depends(get_db)):
    rec = await db.get(SeasonRecord, body.season_record_id)
    path = await check_scope(db, rec)
    will_del_show = False
    show_path = posixpath.normpath(rec.show_path or "")
    task_obj = await db.get(Task, rec.task_id)
    task_source_path = posixpath.normpath(task_obj.source_path) if (task_obj and task_obj.source_path) else ""
    show_root_snapshot = None
    show_other_files: list[dict] = []

    cd2 = CloudDrive2Client()
    try:
        target = await source_entry(cd2, path, get_settings().source_root)
        if target is not None:
            snapshot = await inventory(cd2, path)
            show_items = await read_listing(cd2, show_path)
            will_del_show, show_path, task_source_path, show_other_items = await _evaluate_show_dir_deletion(
                db, rec, path, show_items
            )
            if will_del_show:
                show_root_snapshot = _directory_inventory_snapshot(show_other_items)
                show_other_files = [dict(x) for x in show_other_items if not x.get('isDirectory')]
        else:
            snapshot = {}
    finally:
        await cd2.close()

    token = 'plan_rec_' + uuid4().hex[:12]
    batch_journal.save(token, {
        'kind': 'discard_source_season',
        'task_id': rec.task_id,
        'season_record_id': rec.id,
        'path': path,
        'show_path': show_path,
        'task_source_path': task_source_path,
        'will_delete_show_dir': will_del_show,
        'show_root_snapshot': show_root_snapshot,
        'snapshot': snapshot,
        'directory_id': str((target or {}).get('id') or ''),
        'source_absent': target is None,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'state': 'previewed',
    })

    season_files = [f for items in snapshot.values() for f in items if not f['isDirectory']]
    all_files = season_files + [
        {'name': str(f.get('name') or ''), 'path': posixpath.normpath(f.get('path') or ''), 'size': int(f.get('size') or 0)}
        for f in show_other_files
    ]
    size = sum(int(f.get('size') or 0) for f in all_files)
    videos = sum(is_video_file(f['name']) for f in all_files)
    subs = sum(is_subtitle_file(f['name']) for f in all_files)
    target_delete_path = show_path if will_del_show else path

    if target is None:
        warnings = ['原源目录已不存在；确认后仅更新条目，不执行删除，也不代表归档完成。']
    elif will_del_show:
        warnings = [
            '这是主动放弃本季补缺；将删除此目录内全部视频、不同版本、字幕及附件，不代表这些内容已有归档副本。',
            f'检测到该剧集已无其他需保留季，确认后将一并删除剧集目录【{show_path}】并在目录【{task_source_path or posixpath.dirname(show_path)}】执行 CloudDrive2 刷新。',
        ]
    else:
        warnings = [
            '这是主动放弃本季补缺；将删除此目录内全部视频、不同版本、字幕及附件，不代表这些内容已有归档副本。',
            '同剧仍有其他季或内容需保留，本次仅删除列出的本季目录，保留剧集外层目录及归档目录。',
        ]

    return {
        'ok': True,
        'data': {
            'preview_token': token,
            'season_path': path,
            'show_path': show_path,
            'task_source_path': task_source_path,
            'target_delete_path': target_delete_path,
            'will_delete_show_dir': will_del_show,
            'has_active_mp_downloads': False,
            'source_absent': target is None,
            'file_stats': {
                'video_count': videos,
                'sub_count': subs,
                'other_count': len(all_files) - videos - subs,
                'total_size_display': f'{size / 1024**3:.2f} GiB',
            },
            'warnings': warnings,
            'files': [{'path': f['path'], 'size': int(f.get('size') or 0)} for f in all_files],
        },
    }


@router.post('/discard-source-season')
async def execute(body: ExecuteRequest, db=Depends(get_db)):
    if not body.confirm_abandon:
        reject("尚未明确确认放弃补缺并删除本季")
    plan = batch_journal.load(body.preview_token)
    if not plan or plan.get('kind') != 'discard_source_season' or plan.get('season_record_id') != body.season_record_id:
        reject("未找到本季有效删除预览")
    if plan.get('state') == 'completed':
        return {'ok':True, 'message':'本季源目录已经删除，无需重复执行'}
    from app.api.archive import _RECOMMENDED_TASK_LOCKS
    lock = _RECOMMENDED_TASK_LOCKS.setdefault(plan['task_id'], asyncio.Lock())
    if lock.locked():
        reject("当前任务正在执行其他处理，请完成后再删除本季")
    async with lock:
        rec = await db.get(SeasonRecord, body.season_record_id)
        path = await check_scope(db, rec)
        if plan.get('require_no_resource'):
            await require_no_resource(db, rec)
        if path != plan['path'] or rec.task_id != plan['task_id']:
            reject("本季目录已变化，请重新预览")
        if (datetime.now(timezone.utc)-datetime.fromisoformat(plan['created_at'])).total_seconds() > 1800 and plan.get('state') == 'previewed':
            reject("删除预览已过期，请重新预览")
        cd2 = CloudDrive2Client()
        try:
            parent = posixpath.dirname(path)
            root = get_settings().source_root
            target = await source_entry(cd2, path, root)
            if target is None:
                await record_source_absent(db, rec, show_absent=await source_entry(cd2, rec.show_path, root) is None)
                await refresh_task_summary(rec.task_id, db)
                await db.commit()
                plan['state'] = 'completed'
                batch_journal.save(body.preview_token, plan)
                return {'ok': True, 'message': '已核实源目录不存在并移出待办；未执行删除，不代表归档完成'}
            if plan.get('source_absent'):
                reject('源目录重新出现，请重新预览；不能按不存在的快照删除')
            if plan.get('state') == 'deleting' and target is not None:
                reject("上次删除未完成对账；目录仍存在，请重新预览，不能重复套用旧删除请求")

            remaining_in_show: list[dict] = []
            if plan.get('state') == 'previewed':
                if target is None or (plan.get('directory_id') and str(target.get('id') or '') != plan['directory_id']):
                    reject("本季目录已消失或被替换，请重新预览")
                if await inventory(cd2, path) != plan['snapshot']:
                    reject("本季文件在预览后发生变化，请重新预览")
                if plan.get('will_delete_show_dir'):
                    cur_show_items = await read_listing(cd2, parent)
                    cur_other = [x for x in cur_show_items if posixpath.normpath(x.get('path') or '') != path]
                    if _directory_inventory_snapshot(cur_other) != plan.get('show_root_snapshot'):
                        reject("剧集目录在预览后发生变化，请重新预览")
                plan['state'] = 'deleting'
                batch_journal.save(body.preview_token, plan)
                result = await cd2.delete_file(path)
                if isinstance(result, dict) and result.get('success') is False:
                    reject("云盘未完成删除，请核对执行结果")
                remaining_in_show = await _verify_deleted_from_parent(cd2, parent, path, "删除后仍看到本季目录，尚未通过核验")

            rec.status = 'source_absent'
            rec.local_episode_count = 0
            rec.recognized_episodes_json = '[]'
            rec.duplicate_episodes_json = '[]'

            # 核验该剧是否已无其他需保留季（支持单季直接连删剧集目录，以及同批多季全删后最后一季连带回收剧集目录）
            deleted_show_dir = False
            can_del_show_now, norm_show_path, task_source_path, _ = await _evaluate_show_dir_deletion(
                db, rec, path, remaining_in_show
            )
            if can_del_show_now:
                show_parent = posixpath.dirname(norm_show_path)
                res_show = await cd2.delete_file(norm_show_path)
                if isinstance(res_show, dict) and res_show.get('success') is False:
                    reject("本季已删除，但剧集外层目录删除未完成，请核对网盘目录")
                await _verify_deleted_from_parent(cd2, show_parent, norm_show_path, "删除后仍看到剧集目录，尚未通过核验")
                deleted_show_dir = True
                if task_source_path and task_source_path != show_parent:
                    await _refresh_cd2_directories(cd2, task_source_path)
            else:
                if task_source_path and task_source_path != parent:
                    await _refresh_cd2_directories(cd2, task_source_path)

            ev = json.loads(rec.evidence_json or '{}')
            ev['source_presence'] = {
                'state': 'absent',
                'path': path,
                'show_absent': deleted_show_dir,
                'checked_at': datetime.now(timezone.utc).isoformat(),
                'media_operation': True,
            }
            rec.evidence_json = json.dumps(ev, ensure_ascii=False)
            if deleted_show_dir:
                rec.status_detail = f'用户主动放弃补缺，该剧集已无其他需保留季，已一并删除剧集目录 ({norm_show_path}) 并刷新 CD2'
                done_msg = '已删除本季及剧集目录，并完成 CloudDrive2 目录刷新'
            else:
                rec.status_detail = '用户主动放弃补缺并删除本季源目录；同剧仍有其他内容保留剧集目录'
                done_msg = '已删除本季源目录并刷新父目录，本季标记为主动放弃补缺'

            rows = list((await db.scalars(select(GapFillEpisode).where(GapFillEpisode.season_path == path))).all())
            for row in rows:
                if row.state not in ('completed', 'archived'):
                    row.state = 'cancelled_or_failed'
                    row.outcome_code = 'user_abandoned_deleted'
                    row.reason = '用户主动放弃补缺并删除本季源目录'
            db.add(OperationLog(
                task_id=rec.task_id,
                season_record_id=rec.id,
                action='discard_source_season',
                status='completed',
                detail_json=json.dumps({
                    'path': path,
                    'show_path': norm_show_path,
                    'deleted_show_dir': deleted_show_dir,
                    'preview_token': body.preview_token,
                    'reason': 'user_abandoned_deleted',
                    'verification_passed': True,
                }, ensure_ascii=False),
            ))
            await refresh_task_summary(rec.task_id, db)
            await db.commit()
            plan['state'] = 'completed'
            batch_journal.save(body.preview_token, plan)
            return {'ok': True, 'deleted_show_dir': deleted_show_dir, 'message': done_msg}
        finally:
            await cd2.close()


@router.post('/discard-source-batch/preview')
async def preview_batch(body: BatchPreviewRequest, db=Depends(get_db)):
    """只预览所选无资源季；无法核验项逐季列出，无其他季需保留时将连带清理空置剧集目录。"""
    items, blocked, paths = [], [], set()
    for record_id in dict.fromkeys(body.season_record_ids):
        label = record_id
        try:
            rec = await db.get(SeasonRecord, record_id)
            if rec is None or rec.task_id != body.task_id:
                reject("季记录不属于所选任务")
            label = f'{rec.show_name} S{rec.season_number:02d}'
            await require_no_resource(db, rec)
            if rec.season_path in paths:
                reject("同一物理季目录已纳入预览，不能重复删除")
            data = (await preview(PreviewRequest(season_record_id=record_id), db))['data']
            plan = batch_journal.load(data['preview_token'])
            plan['require_no_resource'] = True
            batch_journal.save(data['preview_token'], plan)
            items.append({'season_record_id': record_id, 'title': label, **data})
            paths.add(rec.season_path)
        except HTTPException as exc:
            blocked.append({'season_record_id': record_id, 'title': label, 'error': exc.detail})
        except Exception:
            await db.rollback()
            log.exception('批量删源预览失败')
            blocked.append({'season_record_id': record_id, 'title': label, 'error': '读取或核验失败，本季未纳入删除范围'})
    token = 'plan_rec_' + uuid4().hex[:12]
    batch_journal.save(token, {'kind': 'discard_source_batch', 'task_id': body.task_id,
        'state': 'previewed', 'items': [{'season_record_id': x['season_record_id'],
            'title': x['title'], 'preview_token': x['preview_token']} for x in items],
        'blocked': blocked, 'results': [], 'created_at': datetime.now(timezone.utc).isoformat()})
    return {'ok': True, 'preview_token': token, 'items': items, 'blocked': blocked,
            'message': f'可执行 {len(items)} 季；未纳入 {len(blocked)} 季。确认后将删除所选季源目录（无其他保留季的剧集将一并清理剧集目录并刷新 CD2）。'}


@router.get('/discard-source-batch/{token}')
async def batch_status(token: str):
    plan = batch_journal.load(token)
    if not plan or plan.get('kind') != 'discard_source_batch':
        reject('未找到批量删源预览')
    state = plan['state']
    lock = _BATCH_LOCKS.get(token)
    if state == 'running' and not (lock and lock.locked()):
        state = 'needs_repreview'
    return {'state': state, 'results': plan.get('results', []), 'blocked': plan.get('blocked', []),
            'total': len(plan['items']), 'result': plan.get('result')}


@router.post('/discard-source-batch/execute')
async def execute_batch(body: BatchExecuteRequest, db=Depends(get_db)):
    if not body.confirm_abandon:
        reject('尚未明确确认放弃所选季补缺并删除源端')
    lock = _BATCH_LOCKS.setdefault(body.preview_token, asyncio.Lock())
    if lock.locked():
        reject('该批次正在执行，请查看回执，不要重复提交')
    async with lock:
        plan = batch_journal.load(body.preview_token)
        if not plan or plan.get('kind') != 'discard_source_batch':
            reject('未找到有效批量预览')
        if plan['state'] == 'completed':
            return plan['result']
        if plan['state'] != 'previewed':
            reject('上次执行尚未完成对账，请查看回执并重新预览剩余项，不能重发整批')
        if not plan['items']:
            reject('本次没有通过预览的可删除季')
        if (datetime.now(timezone.utc) - datetime.fromisoformat(plan['created_at'])).total_seconds() > 1800:
            reject('批量预览已过期，请重新预览')
        plan['state'] = 'running'
        batch_journal.save(body.preview_token, plan)
        for item in plan['items']:
            try:
                response = await execute(ExecuteRequest(season_record_id=item['season_record_id'],
                    preview_token=item['preview_token'], confirm_abandon=True), db)
                result = {**item, 'ok': bool(response.get('ok')), 'message': response.get('message', '')}
            except HTTPException as exc:
                await db.rollback()
                result = {**item, 'ok': False, 'message': exc.detail}
            except Exception:
                await db.rollback()
                log.exception('批量删源单季执行异常')
                result = {**item, 'ok': False, 'message': '执行结果未确认，请核对本季回执；未自动重试'}
            plan['results'].append(result)
            batch_journal.save(body.preview_token, plan)
        completed = sum(bool(x['ok']) for x in plan['results'])
        failed = len(plan['results']) - completed
        plan['state'] = 'completed'
        plan['result'] = {'ok': failed == 0 and not plan['blocked'], 'completed': completed,
            'failed': failed, 'blocked': len(plan['blocked']), 'results': plan['results'],
            'message': f'本轮结束：删除完成 {completed} 季、未完成 {failed} 季、预览未纳入 {len(plan["blocked"])} 季'}
        batch_journal.save(body.preview_token, plan)
        return plan['result']
