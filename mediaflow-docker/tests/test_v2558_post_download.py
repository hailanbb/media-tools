"""补缺后复核：内存数据库、模拟只读云盘、真实解析与清理规则，无媒体操作。"""
import json
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import patch

import test_v2551_review_closure as f
from app.services import gap_fill_tracker as tracker, post_download_review as review
from app.models.gap_fill import GapFillEpisode
from app.core.episode_parser import is_video_file, parse_episode
from app.core.inspector import diagnose_season, _verified_video
from app.core.review_engine import generate_review_clean_plan
from app.core.season_classifier import classify_single_record, get_record_work_context


class Cloud:
    def __init__(self, files):
        self.files = {'/fixture/Show/Season 1': files}
        self.fail = set()
        self.reads = []
        self.change_after = 0

    async def list_files(self, path, **kwargs):
        assert kwargs.get('force_refresh') is True
        self.reads.append(path)
        if path in self.fail:
            raise RuntimeError('simulated unavailable')
        files = [dict(x) for x in self.files.get(path, [])]
        if self.change_after and len(self.reads) >= self.change_after and files:
            files[0]['size'] += 1
        return files


class MP:
    downloads = []
    transfers = []
    failed = False
    async def get_active_downloads(self):
        if self.failed: raise RuntimeError('offline')
        return self.downloads
    async def get_transfer_queue(self): return self.transfers
    async def get_download_history(self, **kw): return []
    async def get_transfer_history(self, **kw): return []


def video(name):
    return dict(name=name, path='/fixture/Show/Season 1/'+name, size=100000, isDirectory=False)


class PostDownloadTests(unittest.TestCase):
    setUp_db = f.DatabaseRegression.setUp
    tearDown_db = f.DatabaseRegression.tearDown
    season = f.DatabaseRegression.season

    def setUp(self):
        self.setUp_db()
        settings = SimpleNamespace(mp_url='', mp_token='', cd2_url='', archive_root='', source_root='/fixture')
        self.patches = [patch('app.config.get_settings', return_value=settings),
                        patch.object(review, 'get_settings', return_value=settings),
                        patch.object(review, 'run_inspection', self.inspect)]
        for p in self.patches: p.start()
        self.conflict = {}
        self.scan_calls = 0
        self.rec = self.season(show_name='Show', show_path='/fixture/Show', season_path='/fixture/Show/Season 1',
            tmdb_id='200', total_episodes=3, season_finished=True, status='download_needed',
            recognized_episodes_json='[1,2]', missing_episodes_json='[3]', duplicate_episodes_json='[]',
            local_episode_count=2, evidence_json=json.dumps({'review_plan_preview': {'can_clean': False}}))
        self.ledger = GapFillEpisode(task_id='t', season_record_id=self.rec.id, season_path=self.rec.season_path,
            identity_key=tracker.build_identity_key('Show', '200', 1), show_name='Show', tmdb_id=200,
            season_number=1, episode_number=3, state='queued', torrent_hash='fixture-hash')
        self.session.add(self.ledger); self.session.commit()
        self.cloud = Cloud([video(f'Show.S01E{i:02}.1080p.WEB-DL.mkv') for i in range(1, 4)])

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tearDown_db()

    async def inspect(self, **kwargs):
        self.scan_calls += 1
        self.assertEqual(kwargs['source_path'], self.rec.season_path)
        self.assertEqual(kwargs['only_season_path'], self.rec.season_path)
        self.assertFalse(kwargs['persist_metadata'])
        reader = kwargs['cd2_client']
        self.assertFalse(hasattr(reader, 'delete_file'))
        files = await reader.list_files(self.rec.season_path)
        parsed = [parse_episode(x['name'], x['path'], x.get('size', 0)) for x in files
                  if not x.get('isDirectory') and is_video_file(x['name'])]
        counts = Counter(e for p in parsed if _verified_video(p, 1) for e in p.episode_numbers)
        recognized = sorted(counts)
        duplicates = sorted(e for e, count in counts.items() if count > 1)
        plan = generate_review_clean_plan(files, 1, 3)
        status, detail = diagnose_season(recognized, 3, True, bool(duplicates), duplicates)
        diag = SimpleNamespace(season_path=self.rec.season_path, season_number=1, tmdb_id='200',
            total_episodes=3, local_episode_count=len(parsed), recognized_episodes=recognized,
            duplicate_episodes=duplicates, missing_episodes=sorted({1,2,3}-set(recognized)),
            season_finished=True, status=status, status_detail=detail, archive_target_path=None,
            evidence={'meta_status':'ok', 'metadata_binding_verified':True, 'review_plan_preview':plan},
            conflict=dict(self.conflict))
        return SimpleNamespace(seasons=[diag], errors=[])

    def reconcile(self, mp=None, **kwargs):
        result = f.immediate(tracker.reconcile_gap_fill_records('t', self.db,
            cd2_client=self.cloud, mp_client=mp, **kwargs))
        self.session.expire_all()
        return result

    def category(self): return f.immediate(get_record_work_context(self.db, self.rec))['main_category']

    def test_complete_unique_mixed_quality_is_archive(self):
        self.cloud.files[self.rec.season_path][-1] = video('Show.S01E03.2160p.WEB-DL.mkv')
        result = self.reconcile()
        self.assertEqual(result['post_review_completed'], 1)
        self.assertEqual(self.category(), 'archive')
        self.assertEqual(self.ledger.state, 'in_library')

    def test_full_pack_new_duplicates_go_to_review_and_download_stays_completed(self):
        self.cloud.files[self.rec.season_path].append(video('Show.S01E01.2160p.WEB-DL.mkv'))
        self.reconcile()
        self.assertEqual(json.loads(self.rec.duplicate_episodes_json), [1])
        self.assertTrue(json.loads(self.rec.evidence_json)['review_plan_preview']['can_clean'])
        self.assertEqual(self.category(), 'review')
        self.assertEqual(self.ledger.state, 'in_library')
        view = f.immediate(tracker.compute_season_gap_fill_view(self.db, self.rec))
        self.assertEqual(view['actionable_missing_episodes'], [])

    def test_stale_duplicate_marker_is_replaced(self):
        self.rec.duplicate_episodes_json = '[1]'
        self.rec.evidence_json = json.dumps({'review_plan_preview': {'can_clean': True}})
        self.session.commit(); self.reconcile()
        self.assertEqual(json.loads(self.rec.duplicate_episodes_json), [])
        self.assertEqual(self.category(), 'archive')

    def test_extra_regular_episode_blocks_archive_but_settles_valid_gap(self):
        self.cloud.files[self.rec.season_path].append(video('Show.S01E04.1080p.WEB-DL.mkv'))
        self.reconcile()
        self.assertEqual(self.category(), 'anomaly')
        self.assertEqual(self.ledger.state, 'in_library')
        self.assertIn('超出', self.rec.status_detail)

    def test_unidentified_video_blocks_archive(self):
        self.cloud.files[self.rec.season_path].append(video('unidentified.mkv'))
        self.reconcile(); self.assertEqual(self.category(), 'anomaly')

    def test_known_special_does_not_fill_regular_gap_or_block_complete_season(self):
        self.cloud.files[self.rec.season_path].append(video('Show.S00E01.特别篇.mkv'))
        self.reconcile()
        self.assertEqual(json.loads(self.rec.recognized_episodes_json), [1,2,3])
        self.assertEqual(self.category(), 'archive')

    def test_identity_conflict_preserves_inflight_instead_of_false_arrival(self):
        self.conflict = {'blocking': True, 'kind': 'mixed_works', 'message':'foreign identity'}
        self.reconcile(); self.assertEqual(self.category(), 'anomaly')
        self.assertEqual(self.ledger.state, 'queued')

    def test_source_read_failure_does_not_use_old_coverage_or_force_release(self):
        self.rec.recognized_episodes_json = '[1,2,3]'; self.session.commit()
        self.cloud.fail.add(self.rec.season_path)
        result = self.reconcile(mp=MP(), force_release_missing=True)
        self.assertEqual(result['released_to_todo'], 0)
        self.assertEqual(result['reconciled_in_library'], 0)
        self.assertEqual(self.ledger.state, 'queued')
        view = f.immediate(tracker.compute_season_gap_fill_view(self.db, self.rec))
        self.assertEqual(view['view_bucket'], 'pushed_pending')
        self.assertEqual(view['actionable_missing_episodes'], [])

    def test_archive_read_failure_keeps_pending_even_if_source_complete(self):
        self.rec.archive_target_path='/fixture/archive'; self.session.commit()
        self.cloud.fail.add('/fixture/archive'); self.reconcile()
        self.assertTrue(review.review_pending(self.rec))
        self.assertEqual(self.ledger.state, 'queued')

    def test_confirmed_absent_archive_is_different_from_read_failure(self):
        self.rec.archive_target_path='/fixture/archive'; self.session.commit()
        original=self.cloud.list_files
        async def listing(path, **kw):
            if path=='/fixture/archive': raise FileNotFoundError('confirmed absent')
            return await original(path,**kw)
        with patch.object(self.cloud,'list_files',listing): self.reconcile()
        self.assertEqual(self.category(),'archive')

    def test_invalid_directory_payload_cannot_clear_inflight(self):
        async def listing(*args,**kwargs): return None
        with patch.object(self.cloud,'list_files',listing): self.reconcile(mp=MP(),force_release_missing=True)
        self.assertTrue(review.review_pending(self.rec))
        self.assertEqual(self.ledger.state,'queued')

    def test_writing_download_waits_before_review(self):
        mp=MP(); mp.downloads=[{'hash':'fixture-hash','state':'downloading'}]
        self.reconcile(mp=mp)
        self.assertEqual(self.scan_calls, 0)
        self.assertTrue(review.review_pending(self.rec))

    def test_seeding_is_not_an_incomplete_download(self):
        mp=MP(); mp.downloads=[{'hash':'fixture-hash','state':'seeding'}]
        self.reconcile(mp=mp)
        self.assertEqual(self.category(), 'archive')

    def test_failed_mp_queue_does_not_release_or_archive(self):
        mp=MP(); mp.failed=True
        self.reconcile(mp=mp, force_release_missing=True)
        self.assertTrue(review.review_pending(self.rec))
        self.assertEqual(self.ledger.state, 'queued')

    def test_changing_files_wait_and_retry_can_finish(self):
        self.cloud.change_after=3
        self.reconcile(); self.assertTrue(review.review_pending(self.rec))
        self.cloud.change_after=0
        self.reconcile(); self.assertEqual(self.category(), 'archive')

    def test_completed_legacy_ledger_is_reviewed_once(self):
        self.ledger.state='in_library'; self.rec.status='archive_ready'; self.session.commit()
        self.reconcile(); self.reconcile()
        self.assertEqual(self.scan_calls, 1)

    def test_pending_review_without_inflight_can_retry(self):
        self.ledger.state='in_library'; review.set_pending(self.rec,'previous read failed'); self.session.commit()
        self.reconcile(); self.assertFalse(review.review_pending(self.rec))
        self.assertEqual(self.category(), 'archive')

    def test_attachment_only_child_is_preserved_without_blocking(self):
        path=self.rec.season_path+'/合并保留'
        self.cloud.files[self.rec.season_path].append(dict(name='合并保留',path=path,isDirectory=True))
        self.cloud.files[path]=[dict(name='cover.jpg',path=path+'/cover.jpg',size=100,isDirectory=False)]
        self.reconcile(); self.assertEqual(self.category(), 'archive')

    def test_child_with_unreviewed_video_blocks_archive(self):
        path=self.rec.season_path+'/other'
        self.cloud.files[self.rec.season_path].append(dict(name='other',path=path,isDirectory=True))
        self.cloud.files[path]=[video('Unknown.mkv')]
        self.reconcile(); self.assertEqual(self.category(), 'anomaly')

    def test_grouped_complementary_coverage_cannot_override_pending_review(self):
        other=self.season(rid='other', tmdb_id='200', show_name='Show', season_path='/fixture/other/Season 1',
            total_episodes=3, recognized_episodes_json='[3]', missing_episodes_json='[1,2]',
            duplicate_episodes_json='[]',status='archive_ready',archive_target_path='/fixture/archive',
            evidence_json=json.dumps({'review_plan_preview': {'can_clean': False}}))
        self.rec.archive_target_path='/fixture/archive';review.set_pending(self.rec,'not ready'); self.session.commit()
        work=f.immediate(get_record_work_context(self.db,other))
        self.assertEqual(work['main_category'],'download')
        self.assertEqual(work['gap_fill_state']['view_bucket'],'pushed_pending')
        summary=f.immediate(tracker.refresh_task_summary('t',self.db))
        self.assertEqual(summary['download_pushed'],1)

    def test_pending_review_blocks_source_delete_even_without_inflight(self):
        from app.api.source_discard import check_scope
        from fastapi import HTTPException
        self.ledger.state='in_library'; review.set_pending(self.rec,'needs review'); self.session.commit()
        with self.assertRaises(HTTPException) as raised:
            f.immediate(check_scope(self.db,self.rec))
        self.assertEqual(raised.exception.status_code,409)
        self.assertIn('复核',raised.exception.detail)

    def test_pending_review_blocks_download_service_before_search(self):
        from app.services import download_service as downloads
        review.set_pending(self.rec,'needs review'); self.session.commit()
        class NoSearch(MP):
            async def close(self): pass
        settings=SimpleNamespace(mp_url='http://fixture.invalid',mp_token='fixture',
                                 archive_root='',source_root='/fixture')
        with patch.object(downloads,'get_settings',return_value=settings), \
             patch.object(downloads,'MoviePilotClient',return_value=NoSearch()):
            result=f.immediate(downloads.execute_gap_fill_download([self.rec.id],self.db))
        self.assertEqual(result['submitted'],[])
        self.assertTrue(any('复核' in reason for reason in result['skip_reasons']))

    def test_pending_or_unverified_identity_does_not_fake_arrival_in_view(self):
        self.rec.missing_episodes_json='[]'
        for state in ({'state':'pending'}, {'state':'completed','coverage_verified':False}):
            self.rec.evidence_json=json.dumps({'post_download_review':state}); self.session.commit()
            view=f.immediate(tracker.compute_season_gap_fill_view(self.db,self.rec))
            self.assertEqual(view['reconciled_in_library_episodes'],[])
            self.assertEqual(view['episodes'][0]['state'],'queued')

    def run_real_inspection(self):
        from app.core import inspector
        self.cloud.files[self.rec.show_path]=[dict(name='Season 1',path=self.rec.season_path,isDirectory=True)]
        async def metadata(**kwargs):
            kwargs['evidence_out'].update(work_info={'tmdb_id':200,'name':'Show','seasons_present':[1],'status':'Ended'})
            return 200,{1:{'total':3,'finished':True,'douban_checked':True}}
        async def gather(*coros, **kwargs): return [await c for c in coros]
        class NoNetwork:
            async def close(self): pass
        with patch.object(review,'run_inspection',inspector.run_inspection), \
             patch.object(inspector,'_resolve_show_metadata',metadata), \
             patch('app.clients.tmdb.TMDBClient',return_value=NoNetwork()), \
             patch.object(inspector.asyncio,'create_task',lambda coro:coro), \
             patch.object(inspector.asyncio,'gather',gather):
            result=self.reconcile()
        self.assertEqual(result['post_review_completed'],1,result)

    def test_real_inspection_engine_detects_new_duplicates(self):
        self.cloud.files[self.rec.season_path].append(video('Show.S01E01.2160p.WEB-DL.mkv'))
        self.run_real_inspection()
        self.assertEqual(self.category(),'review')
        self.assertEqual(json.loads(self.rec.duplicate_episodes_json),[1])
        self.assertEqual(self.ledger.state,'in_library')

    def test_real_engine_unique_quality_mix_can_archive(self):
        self.cloud.files[self.rec.season_path][-1]=video('Show.S01E03.2160p.WEB-DL.mkv')
        self.run_real_inspection()
        self.assertEqual(self.category(),'archive')

    def test_real_engine_distinct_editions_are_retained(self):
        self.cloud.files[self.rec.season_path].append(video('Show.S01E01.加长版.1080p.mkv'))
        self.run_real_inspection()
        evidence=json.loads(self.rec.evidence_json)
        self.assertFalse(evidence['review_plan_preview']['can_clean'])
        self.assertTrue(evidence['review_resolution']['all_distinct_retained'])
        self.assertEqual(self.category(),'archive')


if __name__ == '__main__': unittest.main()
