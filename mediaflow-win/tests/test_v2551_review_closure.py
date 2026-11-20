"""v2.5.51 实际回归案例；使用同步内存数据库适配与模拟云盘，不访问真实媒体。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TEST_DIR = tempfile.TemporaryDirectory(prefix="mf-v2551-")
os.environ.update(MF_DATA_DIR=_TEST_DIR.name, MF_CD2_URL="", MF_CD2_TOKEN="", MF_MP_URL="", MF_MP_TOKEN="", MF_TMDB_API_KEY="")

from sqlalchemy import create_engine, select, event
from sqlalchemy.orm import Session
import app.models
from app.database import Base
from app.models.task import Task
from app.models.season import SeasonRecord
from app.models.operation import OperationLog
from app.models.directory_group import DirectoryGroup
from app.core.episode_parser import classify_content_unit, companion_matches_video
from app.core.review_engine import generate_review_clean_plan, build_file_fingerprints, compare_plan_fingerprints
from app.core.directory_merge import delete_dir_robust, list_files_robust, move_and_rename_file_robust
from app.core.season_classifier import classify_single_record
from app.core.inspector import _merge_season_sources
from app.core.identity_resolver import _cluster_release_titles
from app.core import batch_journal
from app.api import archive
from app.services.gap_fill_tracker import refresh_task_summary, compute_season_gap_fill_view


def immediate(coro):
    """仅驱动完全由同步数据库和即时模拟 I/O 组成的协程；任何真实异步等待即失败。"""
    try:
        yielded = coro.send(None)
    except StopIteration as result:
        return result.value
    coro.close()
    raise AssertionError(f"测试出现非模拟异步等待: {yielded!r}")


class ImmediateDB:
    def __init__(self, session): self.session = session
    async def execute(self, *a, **kw): return self.session.execute(*a, **kw)
    async def scalars(self, *a, **kw): return self.session.scalars(*a, **kw)
    async def get(self, *a, **kw): return self.session.get(*a, **kw)
    async def commit(self): self.session.commit()
    async def flush(self): self.session.flush()
    async def refresh(self, obj): self.session.refresh(obj)
    def add(self, obj): self.session.add(obj)


class Disk:
    def __init__(self, files, fail_video=False):
        self.files = [dict(f) for f in files]
        self.deleted = []
        self.fail_video = fail_video
    async def list_files(self, path, **kw): return [dict(f) for f in self.files]
    async def delete_file(self, path):
        if self.fail_video and path.endswith('.mkv'): raise RuntimeError('simulated delete failure')
        self.deleted.append(path)
        self.files = [f for f in self.files if f['path'] != path]
        return {"success": True}
    async def close(self): pass


def file(name, size=100):
    return {"name": name, "path": "/src/Show/Season 1/" + name, "size": size,
            "id": name, "modified": "2026-11-20", "isDirectory": False}


class ReviewRegression(unittest.TestCase):
    def test_derivative_material_is_never_a_regular_duplicate(self):
        files = [file('Show.S01E01.2160p.mkv', 1000)]
        for tag in ('搜证事件簿', '圆桌复盘局', '全纪录', '候场', '1V1', 'Vlog', '加更'):
            files.append(file(f'Show.S01E01.{tag}.1080p.mkv'))
        plan = generate_review_clean_plan(files, 1, 1)
        self.assertFalse(plan['to_delete'], plan)
        for f in files[1:]: self.assertFalse(classify_content_unit(f['name'])['is_regular'])

    def test_part4_whole_middle_lower_audio_and_unknown_editions_are_distinct(self):
        names = ['Show.S01E01.mkv', 'Show.S01E01.Part4.mkv', 'Show.S01E01.Part12.mkv',
                 'Show.S01E01.中.mkv', 'Show.S01E01.下.mkv', 'Show.S01E01.国语.mkv',
                 'Show.S01E01.粤语.mkv', 'Show.S01E01.音乐版.mkv']
        keys = [classify_content_unit(n)['content_unit_key'] for n in names]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertFalse(generate_review_clean_plan([file(n) for n in names], 1, 1)['to_delete'])

    def test_thumbnail_association_and_orphan_receipt(self):
        low, high = file('Show.S01E01.720p.mkv'), file('Show.S01E01.2160p.mkv', 1000)
        thumb = file('Show.S01E01.720p-thumb.jpg')
        plan = generate_review_clean_plan([low, high, thumb], 1, 1)
        attachment = next(d for d in plan['to_delete'] if d['name'] == thumb['name'])
        self.assertEqual(attachment['parent_video_path'], low['path'])
        self.assertFalse(generate_review_clean_plan([high, thumb], 1, 1)['to_delete'])
        recovered = generate_review_clean_plan([high, thumb], 1, 1, verified_deleted_video_names={low['name']})
        self.assertEqual([d['name'] for d in recovered['to_delete']], [thumb['name']])
        self.assertTrue(companion_matches_video('Show.zh.forced.srt', 'Show.mkv'))
        self.assertFalse(companion_matches_video('ShowOther-thumb.jpg', 'Show.mkv'))

    def test_replaced_file_id_invalidates_preview_even_when_size_time_equal(self):
        f = file('Show.S01E01.mkv')
        before = build_file_fingerprints([f])
        f['id'] = 'replacement'
        self.assertTrue(compare_plan_fingerprints(before, [f], set())['needs_repreview'])

    def test_confirmed_group_overrides_stale_record_and_segmented_overflow(self):
        rec = dict(status='period_ready', total_episodes=12, season_finished=True,
                   recognized_episodes=[1, 20], duplicate_episodes=[], missing_episodes=[],
                   evidence={'directory_group': {'id':'g', 'requires_confirm':True},
                             'period_evidence': {'is_segmented':True},
                             'review_plan_preview': {'can_clean':False}})
        self.assertEqual(classify_single_record(rec, set())[0], 'archive')
        self.assertEqual(classify_single_record(rec, {'g'})[0], 'review')

    def test_empty_directory_delete_requires_successful_read(self):
        disk = Disk([file('another-season.mkv')])
        self.assertFalse(immediate(delete_dir_robust(disk, '/src/show')))
        self.assertEqual(disk.deleted, [])
        class Broken(Disk):
            async def list_files(self, *a, **kw): raise OSError('offline')
        broken = Broken([])
        self.assertFalse(immediate(delete_dir_robust(broken, '/src/show')))
        with self.assertRaises(RuntimeError): immediate(list_files_robust(broken, '/src/show'))
        self.assertEqual(broken.deleted, [])

    def test_transitive_bilingual_aliases_are_order_independent(self):
        titles = ['Chef of China', '一饭封神 Chef of China', '一饭封神']
        self.assertEqual(len(_cluster_release_titles(titles, set())), 1)
        self.assertEqual(len(_cluster_release_titles(titles[::-1], set())), 1)
        self.assertEqual(len(_cluster_release_titles(['Gravesend', 'The Neighborhood'], set())), 2)

    def test_journal_survives_memory_loss_and_rejects_paths(self):
        key = 'plan_rec_123456789abc'
        batch_journal.save(key, {'inflight_step':'merge:g', 'step_receipts':{'clean:s':{'status':'completed'}}})
        data = batch_journal.load(key)
        self.assertEqual(data['inflight_step'], 'merge:g')
        self.assertIsNone(batch_journal.load('../mediaflow'))

    def test_count_without_finished_proof_is_not_finished(self):
        merged = _merge_season_sources({}, {'total':12, 'douban_id':'12345', 'finished':False})
        self.assertFalse(merged['finished'])

    def test_failed_douban_binding_falls_back_and_records_each_source(self):
        from app.core import inspector
        async def wait_now(awaitable, timeout=None): return await awaitable
        async def gather_now(*awaitables): return [await a for a in awaitables]
        class MP:
            async def get_douban_media_info(self, subject_id, **kw):
                if subject_id=='11111': raise OSError('binding unavailable')
                return {'title':'Show 第二季','tmdb_id':1,'douban_id':'12345','number_of_episodes':4,'status':'Ended'}
            async def search_media(self,*args,**kw): return [{'douban_id':'12345','title':'Show 第二季'}]
        class HTTP:
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def get(self,*args,**kw): return SimpleNamespace(status_code=403)
        diagnostics=[]
        with patch.object(inspector,'_DOUBAN_SEASON_IDS',{'1':{'2':'11111'}}), patch.object(inspector.httpx,'AsyncClient',return_value=HTTP()), patch.object(inspector.asyncio,'wait_for',wait_now), patch.object(inspector.asyncio,'gather',gather_now):
            did,seasons=immediate(inspector._fetch_seasons_via_douban(MP(),'Show',needed_seasons={2},tmdb_id=1,diagnostics=diagnostics))
        self.assertEqual(did,'12345')
        self.assertEqual(seasons[2]['total'],4)
        self.assertTrue(any(d['status']=='failed' and d.get('subject_id')=='11111' for d in diagnostics))
        self.assertTrue(any(d['status']=='matched' and d.get('subject_id')=='12345' for d in diagnostics))
        self.assertTrue(any(d['reason']=='HTTP 403' for d in diagnostics))

    def test_zero_score_unrelated_candidate_never_becomes_detected_work(self):
        from app.core import inspector
        signals={'video_count':4,'dominant_release_title':'Race Across The World','release_years':[2023]}
        with patch.object(inspector,'analyze_season_release_signals',return_value=signals):
            conflict,_=immediate(inspector._investigate_season_identity(
                show_name='Race Across The World (2023) [tmdbid=221534]',tmdb_id_num=221534,season_num=5,
                season_files=[],parsed_episodes=[],primary_work_info={'original_name':'Race Across The World','seasons_present':[1,2,3,4]},
                primary_seasons_data={},meta_evidence={'candidate_conflicts':{5:[{'tmdb_id':22328,'name':'Homes Across America','score':0}]}},
                active_tmdb=None,mp_client=None))
        self.assertEqual(conflict,{})


class DatabaseRegression(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)
        self.db = ImmediateDB(self.session)
        self.session.add(Task(id='t', name='fixture', source_path='/src', status='completed'))
        self.session.commit()
    def tearDown(self):
        self.session.close()
        self.engine.dispose()
    def season(self, rid='s', **kw):
        args = dict(id=rid, task_id='t', show_name='Show', show_path='/src/Show',
                    season_path='/src/Show/Season 1', season_number=1, total_episodes=1,
                    season_finished=True, local_episode_count=2, status='period_ready',
                    recognized_episodes_json='[1]', missing_episodes_json='[]', duplicate_episodes_json='[1]',
                    evidence_json=json.dumps({'period_evidence':{'is_segmented':True, 'source_status':'period_ready'},
                                             'review_plan_preview':{'can_clean':True}}))
        args.update(kw)
        rec = SeasonRecord(**args)
        self.session.add(rec); self.session.commit()
        return rec

    def test_period_ready_cleanup_removes_thumbnail_and_keeps_period_result(self):
        rec = self.season()
        disk = Disk([file('Show.S01E01.720p.mkv'), file('Show.S01E01.2160p.mkv',1000), file('Show.S01E01.720p-thumb.jpg')])
        res = immediate(archive._clean_duplicates_impl(archive._ArchiveRequest(season_record_ids=['s']), self.db,
                        cd2_client=disk, active_dl_paths_override=set(), skip_summary_refresh=True))
        self.assertTrue(res.ok, res.message)
        self.assertEqual(len(disk.deleted), 2)
        self.assertEqual(rec.status, 'period_ready')
        self.assertFalse(json.loads(rec.evidence_json)['review_plan_preview']['can_clean'])

    def test_failed_video_does_not_delete_its_thumbnail_or_report_success(self):
        self.season()
        disk = Disk([file('Show.S01E01.720p.mkv'), file('Show.S01E01.2160p.mkv',1000), file('Show.S01E01.720p-thumb.jpg')], True)
        res = immediate(archive._clean_duplicates_impl(archive._ArchiveRequest(season_record_ids=['s']), self.db,
                        cd2_client=disk, active_dl_paths_override=set(), skip_summary_refresh=True))
        self.assertFalse(res.ok)
        self.assertEqual(disk.deleted, [])

    def test_cd2_suffix_shrink_keeps_the_planned_stable_id(self):
        self.season()
        files=[file('Show.S01E01.mkv',1000),file('Show.S01E01(1).mkv',100),file('Show.S01E01(2).mkv',200)]
        plan=generate_review_clean_plan(files,1,1)
        keep_ids={f['id'] for f in files if f['path'] in {k['path'] for k in plan['to_keep']}}
        class ShrinkingDisk(Disk):
            async def delete_file(self,path):
                result=await super().delete_file(path)
                for index,f in enumerate(self.files):
                    f['name']='Show.S01E01'+(f'({index})' if index else '')+'.mkv'
                    f['path']='/src/Show/Season 1/'+f['name']
                return result
        disk=ShrinkingDisk(files)
        result=immediate(archive._clean_duplicates_impl(archive._ArchiveRequest(season_record_ids=['s']),self.db,
            cd2_client=disk,active_dl_paths_override=set(),skip_summary_refresh=True))
        self.assertTrue(result.ok,result.message)
        self.assertEqual({f['id'] for f in disk.files},keep_ids)

    def test_historical_thumbnail_uses_verified_operation_receipt(self):
        self.season()
        self.session.add(OperationLog(task_id='t', season_record_id='s', action='clean_duplicate', status='completed',
            detail_json=json.dumps({'verification_passed':True, 'deleted_paths':[file('Show.S01E01.720p.mkv')['path']]})))
        self.session.commit()
        disk = Disk([file('Show.S01E01.2160p.mkv',1000),file('Show.S01E01.720p-thumb.jpg')])
        res = immediate(archive._clean_duplicates_impl(archive._ArchiveRequest(season_record_ids=['s']), self.db,
                        cd2_client=disk, active_dl_paths_override=set(), skip_summary_refresh=True))
        self.assertTrue(res.ok, res.message)
        self.assertEqual(disk.deleted, [file('Show.S01E01.720p-thumb.jpg')['path']])

    def test_summary_queries_are_bounded_as_season_count_increases(self):
        for i in range(30):
            self.season(str(i), season_path=f'/src/Show/Season {i+1}', season_number=i+1,
                        status='download_needed', recognized_episodes_json='[]', missing_episodes_json='[1]',
                        duplicate_episodes_json='[]', evidence_json='{}')
        queries=[]
        def track(conn, cursor, statement, parameters, context, executemany): queries.append(statement)
        event.listen(self.engine, 'before_cursor_execute', track)
        summary = immediate(refresh_task_summary('t', self.db))
        selects = [q for q in queries if q.lstrip().upper().startswith('SELECT')]
        self.assertLessEqual(len(selects), 6, selects)
        rec = self.session.get(SeasonRecord, '0')
        single = immediate(compute_season_gap_fill_view(self.db, rec))
        self.assertIsInstance(single, dict)
        self.assertIsInstance(summary, dict)

    def test_batch_reports_only_selected_scope_and_remaining_review(self):
        self.season('s')
        self.season('unrelated', show_name='Unrelated', tmdb_id='99', show_path='/src/Unrelated',
                    status='identity_review', evidence_json='{}')
        pid = 'plan_rec_abcdef123456'
        archive._RECOMMENDED_BATCH_PLANS[pid] = dict(task_id='t', merge_items=[], clean_items=[],
            redundant_source_items=[], scope_record_ids=['s'], before_categories={'s':'review'})
        with patch.object(archive, 'CloudDrive2Client', return_value=Disk([])):
            result = immediate(archive._run_recommended_batch_plan(archive.RecommendedExecuteRequest(task_id='t',plan_id=pid), self.db))
        self.assertFalse(result['fully_resolved'])
        self.assertEqual(len(result['outcomes']['remaining_review']), 1)
        self.assertEqual(result['outcomes']['remaining_anomalies'], [])

    def test_directory_merge_updates_other_seasons_but_keeps_nonempty_root(self):
        from app.core.directory_merge import execute_directory_merge
        with tempfile.TemporaryDirectory(prefix='mf-merge-v2551-') as tmp:
            old = Path(tmp) / 'Old [tmdbid=123]'
            target = Path(tmp) / 'New [tmdbid=123]'
            (old/'Season 1').mkdir(parents=True)
            (old/'Season 2').mkdir()
            source = old/'Season 1'/'Old.S01E01.mkv'
            source.write_bytes(b'fixture-video')
            (old/'Season 2'/'keep.txt').write_bytes(b'other-season')
            def norm(p): return str(p).replace('\\','/')
            rec = self.season('one', show_path=norm(old), season_path=norm(old/'Season 1'), tmdb_id='123',
                evidence_json=json.dumps({'directory_group':{'id':'g','requires_confirm':True}}))
            other = self.season('two', show_path=norm(old), season_path=norm(old/'Season 2'), season_number=2, tmdb_id='123',
                evidence_json=json.dumps({'directory_group':{'id':'g','requires_confirm':True}}))
            plan = dict(plan_id='merge-test', can_execute=True, source_target_show_path=norm(target),
                        archive_target_show_path='/archive/New', season_number=1, affected_seasons=[1],
                        merge_from_show_paths=[norm(old)], remaining_seasons=['Old/Season 2'],
                        directories_to_clean=[norm(old/'Season 1'),norm(old)],
                        files_to_move=[dict(source_path=norm(source), source_filename=source.name,
                            target_dir=norm(target/'Season 1'), new_filename='New.S01E01.mkv',
                            episode_numbers=[1],season_number=1)])
            group = DirectoryGroup(id='g',task_id='t',tmdb_id='123',category_key='cat:电视剧/综艺',
                paths_json=json.dumps([{'side':'source','norm_path':norm(old),'raw_name':old.name}]),
                evidence_json=json.dumps({'latest_merge_plan':plan}))
            self.session.add(group); self.session.commit()
            result = immediate(execute_directory_merge(self.db,'t','g',plan_id='merge-test',cd2_client=Disk([]),skip_summary_refresh=True))
            self.assertTrue(result['ok'])
            self.assertEqual((target/'Season 1'/'New.S01E01.mkv').read_bytes(),b'fixture-video')
            self.assertTrue((old/'Season 2'/'keep.txt').exists())
            self.assertFalse(json.loads(other.evidence_json)['directory_group']['requires_confirm'])
            self.assertTrue(any(e['norm_path']==norm(target) for e in json.loads(group.paths_json)))

    def test_canonical_plan_uses_real_category_and_blocks_changed_source(self):
        from app.core import directory_merge
        with tempfile.TemporaryDirectory(prefix='mf-plan-v2551-') as tmp:
            root=Path(tmp)/'source'
            old=root/'电视剧'/'综艺'/'Old (2026) [tmdbid=123]'
            (old/'Season 1').mkdir(parents=True)
            source=old/'Season 1'/'Old.S01E01.mkv'
            source.write_bytes(b'fixture')
            def norm(p): return str(p).replace('\\','/')
            self.season('one',show_path=norm(old),season_path=norm(old/'Season 1'),tmdb_id='123')
            group=DirectoryGroup(id='g',task_id='t',tmdb_id='123',category_key='cat:电视剧/综艺',
                paths_json=json.dumps([{'side':'source','norm_path':norm(old),'raw_name':old.name,'seasons':[1]}]))
            self.session.add(group); self.session.commit()
            settings=SimpleNamespace(source_root=norm(root),archive_root=norm(Path(tmp)/'archive'))
            with patch.object(directory_merge,'get_settings',return_value=settings):
                plan=immediate(directory_merge.build_directory_merge_plan(self.db,'t','g',canonical_title='New',first_air_year='2026',auto_canonical_target=True,all_seasons=True))
            self.assertNotIn('cat:',plan['archive_target_show_path'])
            self.assertIn('/archive/电视剧/综艺/',plan['archive_target_show_path'])
            self.assertTrue(source.exists(),'预览不能移动文件')
            source.write_bytes(b'changed fixture contents')
            with self.assertRaisesRegex(RuntimeError,'文件已变化'):
                immediate(directory_merge.execute_directory_merge(self.db,'t','g',plan_id=plan['plan_id'],cd2_client=Disk([]),skip_summary_refresh=True))
            self.assertTrue(source.exists())

    def test_interrupted_batch_requires_reconciliation_without_io(self):
        pid='plan_rec_abcdef000001'
        batch_journal.save(pid, {'task_id':'t','inflight_step':'merge:g'})
        archive._RECOMMENDED_BATCH_PLANS.pop(pid,None)
        with patch.object(archive,'CloudDrive2Client',side_effect=AssertionError('must not access disk')):
            result=immediate(archive._run_recommended_batch_plan(archive.RecommendedExecuteRequest(task_id='t',plan_id=pid),self.db))
        self.assertTrue(result['needs_repreview'])

    def test_noop_cleanup_updates_stale_plan_without_deleting(self):
        rec=self.season(status='review_needed')
        disk=Disk([file('Show.S01E01.2160p.mkv',1000)])
        result=immediate(archive._clean_duplicates_impl(archive._ArchiveRequest(season_record_ids=['s']),self.db,
            cd2_client=disk,active_dl_paths_override=set(),skip_summary_refresh=True))
        self.assertTrue(result.ok,result.message)
        self.assertEqual(disk.deleted,[])
        self.assertFalse(json.loads(rec.evidence_json)['review_plan_preview']['can_clean'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
