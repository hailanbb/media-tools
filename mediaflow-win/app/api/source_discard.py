"""用户明确放弃补缺后的单季删源；与归档安全清源使用独立接口。"""
import json
import posixpath
import re
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from app.clients.clouddrive2 import CloudDrive2Client
from app.clients.moviepilot import MoviePilotClient, parse_season_str
from app.config import get_settings
from app.database import get_db
from app.models.task import Task
from app.models.season import SeasonRecord
from app.models.operation import OperationLog
from app.models.gap_fill import GapFillEpisode
from app.core import batch_journal
from app.core.directory_merge import _directory_inventory_snapshot
from app.core.episode_parser import is_video_file, is_subtitle_file
from app.core.season_classifier import get_record_work_main_category
from app.services.gap_fill_tracker import IN_FLIGHT_RECONCILE_STATES, refresh_task_summary

router = APIRouter()


class PreviewRequest(BaseModel):
    season_record_id: str


class ExecuteRequest(PreviewRequest):
    preview_token: str
    confirm_abandon: bool = False


def reject(message):
    raise HTTPException(status_code=409, detail=message)


async def check_scope(db, rec):
    if rec is None:
        reject("季记录已不存在")
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
    cd2 = CloudDrive2Client()
    try:
        parents = await cd2.list_files(posixpath.dirname(path), force_refresh=True)
        target = next((x for x in parents if posixpath.normpath(x.get('path') or '') == path and x.get('isDirectory')), None)
        if target is None:
            reject("本季目录不存在，请更新条目结果")
        snapshot = await inventory(cd2, path)
    finally:
        await cd2.close()
    token = 'plan_rec_' + uuid4().hex[:12]
    batch_journal.save(token, {'kind':'discard_source_season', 'task_id':rec.task_id, 'season_record_id':rec.id,
        'path':path, 'snapshot':snapshot, 'directory_id':str(target.get('id') or ''),
        'created_at':datetime.now(timezone.utc).isoformat(), 'state':'previewed'})
    files = [f for items in snapshot.values() for f in items if not f['isDirectory']]
    size = sum(f['size'] for f in files)
    videos = sum(is_video_file(f['name']) for f in files)
    subs = sum(is_subtitle_file(f['name']) for f in files)
    return {'ok':True, 'data':{'preview_token':token, 'season_path':path, 'target_delete_path':path,
        'will_delete_show_dir':False, 'has_active_mp_downloads':False,
        'file_stats':{'video_count':videos, 'sub_count':subs, 'other_count':len(files)-videos-subs,
                      'total_size_display':f'{size / 1024**3:.2f} GiB'},
        'warnings':['这是主动放弃本季补缺；将删除此目录内全部视频、不同版本、字幕及附件，不代表这些内容已有归档副本。',
                    '只删除列出的本季目录；不会扩大到整剧根、其他季或归档目录。'],
        'files':[{'path':f['path'],'size':f['size']} for f in files]}}


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
    import asyncio
    lock = _RECOMMENDED_TASK_LOCKS.setdefault(plan['task_id'], asyncio.Lock())
    if lock.locked():
        reject("当前任务正在执行其他处理，请完成后再删除本季")
    async with lock:
        rec = await db.get(SeasonRecord, body.season_record_id)
        path = await check_scope(db, rec)
        if path != plan['path'] or rec.task_id != plan['task_id']:
            reject("本季目录已变化，请重新预览")
        if (datetime.now(timezone.utc)-datetime.fromisoformat(plan['created_at'])).total_seconds() > 1800 and plan.get('state') == 'previewed':
            reject("删除预览已过期，请重新预览")
        cd2 = CloudDrive2Client()
        try:
            parent = posixpath.dirname(path)
            listing = await cd2.list_files(parent, force_refresh=True)
            target = next((x for x in listing if posixpath.normpath(x.get('path') or '') == path), None)
            if plan.get('state') == 'deleting' and target is not None:
                reject("上次删除未完成对账；目录仍存在，请重新预览，不能重复套用旧删除请求")
            if plan.get('state') == 'previewed':
                if target is None or (plan.get('directory_id') and str(target.get('id') or '') != plan['directory_id']):
                    reject("本季目录已消失或被替换，请重新预览")
                if await inventory(cd2,path) != plan['snapshot']:
                    reject("本季文件在预览后发生变化，请重新预览")
                plan['state']='deleting'; batch_journal.save(body.preview_token,plan)
                result=await cd2.delete_file(path)
                if isinstance(result,dict) and result.get('success') is False:
                    reject("云盘未完成删除，请核对执行结果")
                listing=await cd2.list_files(parent,force_refresh=True)
                if any(posixpath.normpath(x.get('path') or '') == path for x in listing):
                    reject("删除后仍看到本季目录，尚未通过核验")
            rec.status='source_absent'
            rec.status_detail='用户主动放弃补缺并删除本季源目录；不代表补缺或归档完成'
            rec.local_episode_count=0
            rec.recognized_episodes_json='[]'
            rec.duplicate_episodes_json='[]'
            rows=list((await db.scalars(select(GapFillEpisode).where(GapFillEpisode.season_path == path))).all())
            for row in rows:
                if row.state not in ('completed','archived'):
                    row.state='cancelled_or_failed'; row.outcome_code='user_abandoned_deleted'
                    row.reason='用户主动放弃补缺并删除本季源目录'
            db.add(OperationLog(task_id=rec.task_id,season_record_id=rec.id,action='discard_source_season',status='completed',
                detail_json=json.dumps({'path':path,'preview_token':body.preview_token,'reason':'user_abandoned_deleted','verification_passed':True},ensure_ascii=False)))
            await refresh_task_summary(rec.task_id,db)
            await db.commit()
            plan['state']='completed'; batch_journal.save(body.preview_token,plan)
            return {'ok':True,'message':'已删除本季源目录并刷新父目录，本季标记为主动放弃补缺'}
        finally:
            await cd2.close()
