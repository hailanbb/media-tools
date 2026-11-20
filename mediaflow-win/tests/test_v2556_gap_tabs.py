"""无资源分流、批量主动放弃与统一版本；内存数据库和模拟云盘，零网络。"""
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import test_v2551_review_closure as f
from test_v2552_recommended import Tree, folder
from app.api import source_discard as api
from app.services.gap_fill_tracker import compute_season_gap_fill_view, build_identity_key, refresh_task_summary
from app.models.gap_fill import GapFillSeasonRun, GapFillEpisode


class DB(f.ImmediateDB):
    async def rollback(self):
        self.session.rollback()


class GapTabs(unittest.TestCase):
    tearDown = f.DatabaseRegression.tearDown
    season = f.DatabaseRegression.season

    def setUp(self):
        f.DatabaseRegression.setUp(self)
        self.db = DB(self.session)
        self.disk = Tree({'/src': []})
        stack = ExitStack()
        stack.enter_context(patch.object(api, 'CloudDrive2Client', return_value=self.disk))
        stack.enter_context(patch.object(api, 'get_settings', return_value=SimpleNamespace(
            source_root='/src', archive_root='/archive', mp_url='', mp_token='')))
        self.addCleanup(stack.close)

    def make(self, rid='one', outcome='zero_hits', missing=(2,), targets=(2,)):
        show = f'/src/{rid}'
        rec = self.season(rid, show_name=rid, show_path=show, season_path=show+'/Season 1',
            tmdb_id=str(len(rid)+100), status='download_needed', total_episodes=3,
            local_episode_count=1, recognized_episodes_json='[1]', missing_episodes_json=json.dumps(missing),
            duplicate_episodes_json='[]', evidence_json='{}')
        key = build_identity_key(rec.show_name, rec.tmdb_id, 1)
        if outcome:
            self.session.add(GapFillSeasonRun(task_id='t', season_record_id=rid, season_path=rec.season_path,
                show_name=rec.show_name, season_number=1, identity_key=key, outcome_status=outcome,
                target_episodes_json=json.dumps(targets)))
            self.session.commit()
        self.disk.tree['/src'].append(folder(show))
        self.disk.tree[show] = [folder(rec.season_path), folder(show+'/Season 2')]
        self.disk.tree[rec.season_path] = [dict(f.file('Show.S01E01.mkv'), path=rec.season_path+'/Show.S01E01.mkv')]
        self.disk.tree[show+'/Season 2'] = []
        return rec

    def inflight(self, rec, state='intent_submitting', ep=2):
        self.session.add(GapFillEpisode(task_id='t', season_record_id=rec.id, season_path=rec.season_path,
            season_number=1, identity_key=build_identity_key(rec.show_name, rec.tmdb_id, 1),
            episode_number=ep, state=state))
        self.session.commit()

    def view(self, rec):
        return f.immediate(compute_season_gap_fill_view(self.db, rec))

    def preview(self, *ids):
        return f.immediate(api.preview_batch(api.BatchPreviewRequest(task_id='t', season_record_ids=list(ids)), self.db))

    def execute(self, token, confirm=True):
        return f.immediate(api.execute_batch(api.BatchExecuteRequest(preview_token=token, confirm_abandon=confirm), self.db))

    def test_no_resource_outcomes_and_reload(self):
        for i, outcome in enumerate(('zero_hits', 'low_seeders', 'no_qualified_candidate', 'wrong_identity')):
            rec = self.make(str(i), outcome)
            self.assertEqual(self.view(rec)['view_bucket'], 'no_resource')
            self.session.expire_all()
            self.assertEqual(self.view(rec)['view_bucket'], 'no_resource')

    def test_errors_unsearched_aborted_and_rejection_are_not_no_resource(self):
        for i, outcome in enumerate((None, 'search_error', 'submit_error', 'aborted_unstarted', 'rejected', 'deferred_overlap')):
            rec = self.make(str(i), outcome)
            self.assertEqual(self.view(rec)['view_bucket'], 'todo')

    def test_new_missing_episode_is_not_hidden_by_old_search(self):
        self.assertEqual(self.view(self.make(missing=(2,3), targets=(2,)))['view_bucket'], 'todo')

    def test_partial_inflight_is_todo_and_all_pending_is_receipts(self):
        rec = self.make(missing=(2,3), targets=(2,3))
        self.inflight(rec)
        self.assertEqual(self.view(rec)['view_bucket'], 'todo')
        self.inflight(rec, state='queued', ep=3)
        self.assertEqual(self.view(rec)['view_bucket'], 'pushed_pending')

    def test_summary_uses_same_no_resource_subset(self):
        self.make('a'); self.make('bb', None)
        result = f.immediate(refresh_task_summary('t', self.db))
        self.assertEqual(result['download_needed'], 2)
        self.assertEqual(result['download_no_resource'], 1)
        self.assertEqual(result['download_pushed'], 0)

    def test_preview_no_mutation_and_excludes_errors(self):
        self.make('a'); self.make('bb', 'search_error')
        preview = self.preview('a', 'bb')
        self.assertEqual(len(preview['items']), 1)
        self.assertEqual(len(preview['blocked']), 1)
        self.assertEqual(self.disk.deleted, [])
        self.assertFalse(preview['items'][0]['will_delete_show_dir'])

    def test_group_with_unsearched_directory_stays_todo_and_blocks_batch(self):
        first = self.make('a')
        other = self.make('other', None)
        other.tmdb_id = first.tmdb_id
        other.show_name = first.show_name
        self.session.commit()
        summary = f.immediate(refresh_task_summary('t', self.db))
        self.assertEqual(summary['download_no_resource'], 0)
        result = self.preview(first.id)
        self.assertEqual(len(result['items']), 0)
        self.assertEqual(len(result['blocked']), 1)
        self.assertEqual(self.disk.deleted, [])

    def test_batch_confirmation_and_idempotent_receipt(self):
        self.make('a'); self.make('bb')
        preview = self.preview('a', 'bb')
        with self.assertRaises(api.HTTPException): self.execute(preview['preview_token'], False)
        self.assertEqual(self.disk.deleted, [])
        result = self.execute(preview['preview_token'])
        self.assertEqual(result['completed'], 2)
        self.assertEqual(self.disk.deleted, ['/src/a/Season 1', '/src/bb/Season 1'])
        self.assertIn('/src/a/Season 2', self.disk.tree)
        self.assertIn('/src/a', self.disk.tree)
        self.assertEqual(self.execute(preview['preview_token']), result)
        self.assertEqual(len(self.disk.deleted), 2)
        self.assertEqual(f.immediate(api.batch_status(preview['preview_token']))['state'], 'completed')

    def test_changed_season_fails_but_other_season_completes(self):
        rec = self.make('a'); self.make('bb')
        preview = self.preview('a', 'bb')
        self.disk.tree[rec.season_path].append(dict(f.file('new.mkv'), path=rec.season_path+'/new.mkv'))
        result = self.execute(preview['preview_token'])
        self.assertEqual((result['completed'], result['failed']), (1, 1))
        self.assertEqual(self.disk.deleted, ['/src/bb/Season 1'])
        self.assertEqual(rec.status, 'download_needed')

    def test_new_inflight_after_preview_blocks_delete(self):
        rec = self.make(); token = self.preview(rec.id)['preview_token']
        self.inflight(rec)
        result = self.execute(token)
        self.assertEqual(result['completed'], 0)
        self.assertEqual(self.disk.deleted, [])

    def test_search_outcome_changed_after_preview_blocks_batch(self):
        rec = self.make(); token = self.preview(rec.id)['preview_token']
        row = self.session.query(GapFillSeasonRun).one()
        row.outcome_status = 'search_error'; self.session.commit()
        self.assertEqual(self.execute(token)['completed'], 0)
        self.assertEqual(self.disk.deleted, [])

    def test_wrong_task_and_duplicate_ids(self):
        rec = self.make()
        p = self.preview(rec.id, rec.id, 'missing')
        self.assertEqual((len(p['items']), len(p['blocked'])), (1,1))
        p = f.immediate(api.preview_batch(api.BatchPreviewRequest(task_id='another', season_record_ids=[rec.id]), self.db))
        self.assertEqual(len(p['items']), 0)
        with self.assertRaises(api.HTTPException): self.execute(p['preview_token'])

    def test_interrupted_batch_is_not_reissued(self):
        rec = self.make(); token = self.preview(rec.id)['preview_token']
        plan = api.batch_journal.load(token); plan['state'] = 'running'; api.batch_journal.save(token, plan)
        with self.assertRaises(api.HTTPException): self.execute(token)
        self.assertEqual(f.immediate(api.batch_status(token))['state'], 'needs_repreview')
        self.assertEqual(self.disk.deleted, [])

    def test_expired_preview_does_not_delete(self):
        rec = self.make(); token = self.preview(rec.id)['preview_token']
        plan = api.batch_journal.load(token)
        plan['created_at'] = '2020-01-01T00:00:00+00:00'
        api.batch_journal.save(token, plan)
        with self.assertRaises(api.HTTPException): self.execute(token)
        self.assertEqual(self.disk.deleted, [])

    def test_same_batch_running_rejects_duplicate_request(self):
        rec = self.make(); token = self.preview(rec.id)['preview_token']
        lock = api.asyncio.Lock()
        api._BATCH_LOCKS[token] = lock
        f.immediate(lock.acquire())
        try:
            with self.assertRaises(api.HTTPException): self.execute(token)
            self.assertEqual(self.disk.deleted, [])
        finally:
            lock.release()

    def test_invalid_parent_after_delete_does_not_report_success(self):
        rec = self.make(); token = self.preview(rec.id)['preview_token']
        original = self.disk.list_files
        async def listing(path, **kw):
            if self.disk.deleted and path == rec.show_path: return None
            return await original(path, **kw)
        with patch.object(self.disk, 'list_files', side_effect=listing):
            result = self.execute(token)
        self.assertEqual((result['completed'], result['failed']), (0,1))
        self.assertEqual(rec.status, 'download_needed')
        self.assertEqual(len(self.disk.deleted), 1)
        self.execute(token)
        self.assertEqual(len(self.disk.deleted), 1)


class VersionTests(unittest.TestCase):
    def test_html_and_version_endpoint_share_version_without_cache(self):
        import logging
        from app import main
        self.addCleanup(logging.shutdown)
        from app.api.system import app_version
        from app.config import VERSION
        page = f.immediate(main.root())
        info = f.immediate(app_version())
        self.assertIn(f'>v{VERSION}</span>', page.body.decode('utf-8'))
        self.assertNotIn('__MEDIAFLOW_VERSION__', page.body.decode('utf-8'))
        self.assertEqual(json.loads(info.body)['version'], VERSION)
        self.assertEqual(page.headers['cache-control'], 'no-store')
        self.assertEqual(info.headers['cache-control'], 'no-store')
        self.assertEqual({r.path for r in main.app.routes if getattr(r, 'endpoint', None) == main.root},
                         {'/', '/index.html', '/static/index.html'})


if __name__ == '__main__': unittest.main(verbosity=2)
