"""人工确认性能、预览与执行边界；内存库和模拟云盘，无真实媒体 I/O。"""
import json
import posixpath
import unittest
from unittest.mock import patch

import test_v2551_review_closure as f
from app.api import tasks
from app.core import directory_merge as merge
from app.core.manual_confirmation import execute_manual_correction_plan
from fastapi import HTTPException


class Cloud:
    def __init__(self, count=3):
        self.root = '/fixture/show'
        self.season = self.root + '/Season 1'
        self.files = {self.season: [dict(name=f'Old - S01E{i:02}.mkv',
            path=f'{self.season}/Old - S01E{i:02}.mkv', size=100, id=str(i)) for i in range(1, count+1)]}
        self.reads = []
        self.mkdirs = []
        self.moves = []
        self.renames = []
        self.fail_rename = None
        self.fail_read = False
        self.closed = False

    async def mkdir(self, path):
        self.mkdirs.append(path)
        self.files.setdefault(path, [])

    async def list_files(self, path, **kwargs):
        self.reads.append(path)
        if self.fail_read:
            raise RuntimeError('simulated unavailable')
        return [dict(x) for x in self.files.get(path, [])]

    async def move_file(self, path, target, *, skip_existing=False):
        assert skip_existing
        self.moves.append((path, target))
        source = self.files[posixpath.dirname(path)]
        row = next(x for x in source if x['path'] == path)
        assert not any(x['name'] == row['name'] for x in self.files.get(target, []))
        source.remove(row)
        self.files.setdefault(target, []).append({**row, 'path': target+'/'+row['name']})
        return {'success': True}

    async def rename_file(self, path, name):
        self.renames.append((path, name))
        if name == self.fail_rename:
            return {'success': False, 'errorMessage': 'simulated failure'}
        rows = self.files[posixpath.dirname(path)]
        assert not any(x['name'] == name for x in rows)
        row = next(x for x in rows if x['path'] == path)
        row.update(name=name, path=posixpath.dirname(path)+'/'+name)
        return {'success': True}

    async def close(self): self.closed = True


class ManualSaveTests(unittest.TestCase):
    setUp = f.DatabaseRegression.setUp
    tearDown = f.DatabaseRegression.tearDown
    season = f.DatabaseRegression.season

    def make(self, cloud):
        return self.season(show_name='Old', show_path=cloud.root, season_path=cloud.season,
                           tmdb_id='100', season_number=1, total_episodes=3,
                           recognized_episodes_json='[1,2,3]', duplicate_episodes_json='[]',
                           status='archive_ready', local_episode_count=3,
                           evidence_json=json.dumps({'review_plan_preview': {'can_clean': False}}))

    def plan(self, cloud, rec):
        return f.immediate(tasks._build_correction_plan_for_record(self.db, rec,
            tasks.ManualConfirmRequest(target_title='New', target_tmdb_id=200), cloud))[0]

    def test_metadata_only_never_reads_cloud(self):
        cloud = Cloud(); rec = self.make(cloud)
        with patch.object(merge, 'list_files_robust', side_effect=AssertionError('must not read')):
            plan, _ = f.immediate(tasks._build_correction_plan_for_record(self.db, rec,
                tasks.ManualConfirmRequest(confirm_current_identity=True, total_episodes=3,
                                           broadcast_status='finished', target_tmdb_id=100)))
        self.assertFalse(plan['has_file_changes'])

    def test_real_preview_reads_files_and_has_stable_fingerprint(self):
        cloud = Cloud(); rec = self.make(cloud)
        with patch('app.clients.clouddrive2.CloudDrive2Client', return_value=cloud):
            preview = f.immediate(tasks.preview_manual_season_confirmation('t', rec.id,
                tasks.ManualConfirmRequest(target_title='New', target_tmdb_id=200), self.db))
        self.assertTrue(cloud.closed)
        plan = preview['correction_plan']
        self.assertEqual(len(plan['file_changes']), 3)
        self.assertEqual(plan['correction_fingerprint'], self.plan(cloud, rec)['correction_fingerprint'])
        cloud.files[cloud.season][0]['size'] += 1
        self.assertNotEqual(plan['correction_fingerprint'], self.plan(cloud, rec)['correction_fingerprint'])
        self.assertFalse(cloud.moves or cloud.renames)

    def test_unchanged_title_with_year_does_not_read_cloud(self):
        cloud = Cloud(); rec = self.make(cloud)
        rec.show_name = 'Old (2026) [tmdbid=100]'
        with patch.object(merge, 'list_files_robust', side_effect=AssertionError('must not read')):
            plan, _ = f.immediate(tasks._build_correction_plan_for_record(self.db, rec,
                tasks.ManualConfirmRequest(target_tmdb_id=100, target_title='Old (2026)')))
        self.assertFalse(plan['has_file_changes'])

    def test_confirmed_file_save_finishes_in_archive_with_progress_receipt(self):
        cloud = Cloud(); rec = self.make(cloud)
        plan = self.plan(cloud, rec)
        async def keep_directories(*args, **kwargs): return False
        with patch('app.clients.clouddrive2.CloudDrive2Client', return_value=cloud), \
             patch.object(merge, 'delete_dir_robust', keep_directories):
            result = f.immediate(tasks.save_manual_season_confirmation('t', rec.id,
                tasks.ManualConfirmRequest(target_tmdb_id=200, target_title='New', total_episodes=3,
                    broadcast_status='finished', correction_fingerprint=plan['correction_fingerprint']), self.db))
        self.assertTrue(result['ok'])
        self.assertEqual(result['target_category'], 'archive')
        self.assertEqual(result['season']['main_category'], 'archive')
        self.assertEqual(len(cloud.moves), 3)
        state = f.immediate(tasks.manual_confirmation_progress('t', rec.id))
        self.assertTrue(state['ok'])
        self.assertFalse(state['running'])
        self.assertEqual(state['completed'], 3)
        self.assertEqual(state['stage'], '保存完成')

    def test_unreadable_preview_is_not_empty_success(self):
        cloud = Cloud(); cloud.fail_read = True; rec = self.make(cloud)
        with self.assertRaises(HTTPException) as raised:
            self.plan(cloud, rec)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertFalse(cloud.moves or cloud.renames)

    def test_unconfirmed_or_changed_file_plan_cannot_execute(self):
        cloud = Cloud(); rec = self.make(cloud)
        for token in ('', 'stale'):
            with patch('app.clients.clouddrive2.CloudDrive2Client', return_value=cloud):
                with self.assertRaises(HTTPException) as raised:
                    f.immediate(tasks.save_manual_season_confirmation('t', rec.id,
                        tasks.ManualConfirmRequest(target_tmdb_id=200, target_title='New',
                            correction_fingerprint=token), self.db))
            self.assertEqual(raised.exception.status_code, 409)
        self.assertFalse(cloud.moves or cloud.renames)

    def same_directory_plan(self, cloud):
        return {'has_file_changes': True, 'new_season_path': cloud.season,
                'old_season_path': cloud.season, 'keep_old_show_root': True,
                'file_changes': [dict(old_path=x['path'], old_name=x['name'],
                    new_dir=cloud.season, new_name=x['name'].replace('Old', 'New'),
                    new_path=x['path'].replace('Old', 'New')) for x in cloud.files[cloud.season]]}

    def test_61_files_share_one_target_listing_and_report_progress(self):
        cloud = Cloud(61); plan = self.same_directory_plan(cloud); events = []
        result = f.immediate(execute_manual_correction_plan(plan, cd2_client=cloud,
            progress=lambda *event: events.append(event)))
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['completed_steps']), 61)
        self.assertEqual(len(cloud.mkdirs), 1)
        self.assertEqual(cloud.reads.count(cloud.season), 3)  # 准备、后验、完成刷新
        self.assertEqual(events[-1][1:], (61, 61))

    def test_collision_still_blocks_and_never_overwrites(self):
        cloud = Cloud(2); plan = self.same_directory_plan(cloud)
        plan['file_changes'][1]['new_name'] = plan['file_changes'][0]['new_name']
        result = f.immediate(execute_manual_correction_plan(plan, cd2_client=cloud))
        self.assertFalse(result['ok'])
        self.assertEqual(len(result['completed_steps']), 1)
        self.assertEqual(len(result['remaining_steps']), 1)
        self.assertEqual(len(cloud.renames), 1)

    def test_partial_failure_invalidates_shared_inventory(self):
        cloud = Cloud(2); plan = self.same_directory_plan(cloud)
        cloud.fail_rename = plan['file_changes'][0]['new_name']
        result = f.immediate(execute_manual_correction_plan(plan, cd2_client=cloud))
        self.assertFalse(result['ok'])
        self.assertEqual(len(cloud.mkdirs), 2)
        self.assertEqual(len(result['completed_steps']), 1)

    def test_running_save_rejects_duplicate_and_exposes_progress(self):
        key = ('fixture-task', 'fixture-record')
        tasks._MANUAL_SAVE_PROGRESS[key] = dict(running=True, started=tasks.time.monotonic(),
            stage='正在迁移／改名', completed=3, total=61)
        try:
            progress = f.immediate(tasks.manual_confirmation_progress(*key))
            self.assertEqual(progress['completed'], 3)
            self.assertNotIn('started', progress)
            with self.assertRaises(HTTPException) as raised:
                f.immediate(tasks.save_manual_season_confirmation(*key, tasks.ManualConfirmRequest(), self.db))
            self.assertEqual(raised.exception.status_code, 409)
        finally:
            tasks._MANUAL_SAVE_PROGRESS.pop(key, None)


if __name__ == '__main__': unittest.main()
