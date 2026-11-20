"""只读核查当前任务：数据库快照、目录计划和指定案例文件；不执行媒体动作。"""
import json
import sqlite3
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from app.clients.clouddrive2 import CloudDrive2Client
from app.core.directory_merge import build_directory_merge_plan
from app.core.season_classifier import consolidate_seasons_to_work_records


def immediate(coro):
    try:
        coro.send(None)
    except StopIteration as exc:
        return exc.value
    coro.close()
    raise RuntimeError('Unexpected asynchronous suspension during read-only audit')


class ReadOnlyHTTP:
    def __init__(self):
        self.client = httpx.Client(timeout=25)

    async def post(self, url, **kwargs):
        if url.rsplit('/', 1)[-1] not in ('GetSubFiles', 'FindFileByPath'):
            raise RuntimeError('Audit allows directory reads only')
        return self.client.post(url, **kwargs)


def main():
    db = sqlite3.connect(f'file:{ROOT / "data/mediaflow.db"}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    task = SimpleNamespace(**dict(db.execute('select * from tasks order by created_at desc limit 1').fetchone()))
    rows = [SimpleNamespace(**dict(r)) for r in db.execute('select * from season_records where task_id=?', (task.id,))]
    groups = [SimpleNamespace(**dict(r)) for r in db.execute('select * from directory_groups where task_id=?', (task.id,))]
    db.close()
    for row in rows:
        row.season_finished = bool(row.season_finished) if row.season_finished is not None else None
        for field in ('last_checked_at','created_at','updated_at'):
            if getattr(row,field,None):
                setattr(row,field,datetime.fromisoformat(getattr(row,field)))
    works, summary = consolidate_seasons_to_work_records(rows, {g.id for g in groups if g.status == 'pending_confirm'})
    report = {'task_id':task.id, 'source_path':task.source_path, 'physical_count':len(rows),
        'work_count':len(works), 'categories':dict(Counter(w['main_category'] for w in works)),
        'statuses':dict(Counter(r.status for r in rows)), 'records':[vars(r) for r in rows], 'groups':[], 'samples':{}}

    def inspect_group(group):
        cd = CloudDrive2Client(); transport = ReadOnlyHTTP(); cd._client = transport
        try:
            plan = immediate(build_directory_merge_plan(None,task.id,group.id,task_obj=task,group_obj=group,
                prefetched_season_records=rows,cd2_client=cd,persist_plan=False,all_seasons=True,
                auto_canonical_target=True,smart_collision=True))
            return {'group_id':group.id, 'tmdb_id':group.tmdb_id, 'plan':plan}
        except Exception as exc:
            return {'group_id':group.id,'tmdb_id':group.tmdb_id,'error':str(exc)}
        finally:
            transport.client.close()

    with ThreadPoolExecutor(max_workers=3) as pool:
        for item in pool.map(inspect_group,groups):
            report['groups'].append(item)
            plan=item.get('plan',{})
            print(item['tmdb_id'], 'ERROR' if item.get('error') else 'OK',
                  len(plan.get('files_to_move',[])),len(plan.get('assets_to_preserve',[])),
                  len(plan.get('isolated_conflicts',[])),flush=True)
    cd=CloudDrive2Client(); transport=ReadOnlyHTTP(); cd._client=transport
    try:
        for row in rows:
            if (row.tmdb_id,row.season_number) in [('296217',1),('222808',2),('277918',12)]:
                report['samples'][row.id]=immediate(cd.list_files(row.season_path,force_refresh=True))
    finally:
        transport.client.close()
    out=ROOT/'data/audits/current-task-v2553.json';out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print('SAVED',out,report['categories'])


if __name__ == '__main__':
    main()
