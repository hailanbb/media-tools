"""外部删除对账与历史确认恢复；仅内存数据库、模拟目录。"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_v2551_review_closure as f
import test_v2560_source_root as root
import test_v2556_gap_tabs as gap
from test_v2552_recommended import Tree, folder
from app.api import tasks, archive, source_discard
from app.core import directory_governance as governance
from app.services.source_presence import source_entry


class Presence(unittest.TestCase):
    setUp = f.DatabaseRegression.setUp
    tearDown = f.DatabaseRegression.tearDown
    season = f.DatabaseRegression.season

    def reconcile(self, disk, rec):
        with patch('app.clients.clouddrive2.CloudDrive2Client', return_value=disk), patch.object(tasks, 'get_settings', return_value=SimpleNamespace(source_root='/src', archive_root='/arc')):
            return f.immediate(tasks.reconcile_source_presence('t', rec.id, self.db))

    def test_all_five_module_states_leave_pending_without_media_operations(self):
        for i, state in enumerate(('already_archived', 'unknown', 'download_needed', 'ready', 'airing')):
            rec = self.season(str(i), status=state, evidence_json='{}')
            disk = Tree({'/src': []})
            result = self.reconcile(disk, rec)
            self.assertTrue(result['source_absent'])
            self.assertEqual(rec.status, 'source_absent')
            self.assertEqual(disk.deleted, [])
            self.assertEqual(f.immediate(tasks.get_task_seasons('t', db=self.db)), [])
        self.assertEqual(self.session.query(f.OperationLog).count(), 5)

    def test_existing_empty_directory_is_not_absence(self):
        rec = self.season(status='unknown')
        disk = Tree({'/src/Show': [folder(rec.season_path)], rec.season_path: []})
        self.assertFalse(self.reconcile(disk, rec)['source_absent'])
        self.assertEqual(rec.status, 'unknown')

    def test_timeout_and_bad_listing_do_not_close_record(self):
        for value in (TimeoutError('timeout'), None, [{}]):
            rec = self.season(str(value), status='unknown')
            disk = Tree({})
            async def broken(*args, **kwargs):
                if isinstance(value, Exception): raise value
                return value
            disk.list_files = broken
            with self.assertRaises(Exception): self.reconcile(disk, rec)
            self.assertEqual(rec.status, 'unknown')

    def test_missing_source_root_is_unavailable_not_absent(self):
        with self.assertRaises(FileNotFoundError):
            f.immediate(source_entry(Tree({}), '/src/Show/Season 1', '/src'))

    def test_remaining_root_keeps_cleanup_pending(self):
        rec = self.season(status='already_archived', evidence_json='{}')
        disk = Tree({'/src': [folder('/src/Show')], '/src/Show': []})
        self.assertFalse(self.reconcile(disk, rec)['ok'])
        self.assertEqual(rec.status, 'already_archived')

    def test_pending_preservation_is_not_erased_by_source_absence(self):
        rec = self.season(status='already_archived', evidence_json='{"source_cleanup_auxiliary":{"state":"pending"}}')
        self.assertFalse(self.reconcile(Tree({'/src': []}), rec)['ok'])
        self.assertEqual(rec.status, 'already_archived')

    def test_active_task_blocks_reconciliation(self):
        rec = self.season(status='unknown')
        self.session.get(f.Task, 't').status = 'running'
        with self.assertRaises(Exception): self.reconcile(Tree({'/src': []}), rec)
        self.assertEqual(rec.status, 'unknown')

    def test_absence_is_idempotent(self):
        rec = self.season(status='unknown')
        self.reconcile(Tree({'/src': []}), rec)
        self.reconcile(Tree({'/src': []}), rec)
        self.assertEqual(self.session.query(f.OperationLog).count(), 1)

    def test_absent_source_group_disappears_from_list_and_summary_without_deletion(self):
        rec = self.season(status='unknown', tmdb_id='1')
        group = f.DirectoryGroup(id='gone', task_id='t', tmdb_id='1', status='pending_confirm',
            paths_json=json.dumps([dict(side='source', norm_path=rec.show_path), dict(side='archive', norm_path='/arc/Show')]))
        self.session.add(group); self.session.commit()
        self.reconcile(Tree({'/src': []}), rec)
        result = f.immediate(tasks.list_task_directory_groups('t', self.db))
        self.assertEqual(result['groups'], [])
        summary = f.immediate(root.refresh_task_summary('t', self.db))
        self.assertEqual(summary['pending_directory_groups'], 0)
        self.assertEqual(self.session.get(f.DirectoryGroup, 'gone').status, 'pending_confirm')

    def test_remaining_source_or_archive_conflict_keeps_group(self):
        from app.services.source_presence import group_sources_absent
        rec = self.season(status='source_absent', evidence_json='{"source_presence":{"show_absent":true}}')
        entries = [dict(side='source', norm_path=rec.show_path), dict(side='archive', norm_path='/arc/A'), dict(side='archive', norm_path='/arc/B')]
        self.assertFalse(group_sources_absent(entries, [rec]))
        entries.pop(); rec.evidence_json = '{}'
        self.assertFalse(group_sources_absent(entries, [rec]))


class Cleanup(unittest.TestCase):
    setUp = f.DatabaseRegression.setUp
    tearDown = f.DatabaseRegression.tearDown
    season = f.DatabaseRegression.season
    run_cleanup = root.RootCleanup.run_cleanup

    def test_external_season_removal_still_finishes_root(self):
        disk = root.cloud()
        f.immediate(disk.delete_file(root.SRC)); disk.deleted.clear()
        result = self.run_cleanup(disk)
        self.assertTrue(result.ok, result.message)
        self.assertNotIn(root.SRC, disk.deleted)
        self.assertEqual(disk.tree['/src'], [])

    def test_external_show_removal_does_not_require_archive_or_group_confirmation(self):
        disk = Tree({'/src': []})
        result = self.run_cleanup(disk)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.rec.status, 'source_absent')
        self.assertIn('不代表归档完成', result.message)
        self.assertEqual(disk.deleted, [])


class GroupDrift(unittest.TestCase):
    setUp = f.DatabaseRegression.setUp
    tearDown = f.DatabaseRegression.tearDown
    season = f.DatabaseRegression.season

    def make(self, status='confirmed'):
        show, other, target = '/src/Show [tmdbid=1]', '/src/Other [tmdbid=1]', '/arc/Show [tmdbid=1]'
        self.group = f.DirectoryGroup(id='g', task_id='t', tmdb_id='1', status=status,
            confirmation_mode='use_archive_target' if status == 'confirmed' else '',
            confirmed_target_path=target if status == 'confirmed' else '',
            invalidated_reason='网盘中的同 TMDB 目录数量或路径已变化（新增第 3 个目录或目录被移动），原确认已自动失效，请重新确认',
            paths_json=json.dumps([dict(side='source', norm_path=show), dict(side='source', norm_path=other), dict(side='archive', norm_path=target)]))
        self.session.add(self.group); self.session.commit()
        self.rec = self.season(tmdb_id='1', show_path=show, season_path=show+'/Season 1', archive_target_path=target+'/Season 1',
            evidence_json=json.dumps({'directory_group': dict(id='g', status='confirmed', confirmation_mode='use_archive_target', confirmed_target_path=target)}))
        self.disk = Tree({'/src':[folder(show)], '/arc':[folder(target)]})

    def blocked(self, operation='clean_source_redundant'):
        with patch.object(governance, 'get_settings', return_value=SimpleNamespace(enable_multi_dir_governance=True, source_root='/src', archive_root='/arc')):
            return f.immediate(governance.find_blocking_group_for_record(self.db, self.rec, cd2_client=self.disk, operation=operation))

    def test_source_contraction_preserves_confirmation_for_cleanup(self):
        self.make(); self.assertIsNone(self.blocked())
        self.assertEqual(self.group.status, 'confirmed')

    def test_old_invalidated_confirmation_can_finish_cleanup(self):
        self.make('pending_confirm'); self.assertIsNone(self.blocked())
        self.assertEqual(self.group.status, 'pending_confirm')  # scoped proof, no global reapproval

    def test_new_source_still_blocks(self):
        self.make('pending_confirm'); self.disk.tree['/src'].append(folder('/src/New [tmdbid=1]'))
        self.assertIsNotNone(self.blocked())

    def test_archive_removal_still_blocks(self):
        self.make('pending_confirm'); self.disk.tree['/arc'] = []
        self.assertIsNotNone(self.blocked())

    def test_unconfirmed_record_cannot_use_suggested_target(self):
        self.make('pending_confirm'); self.rec.evidence_json='{}'
        self.assertIsNotNone(self.blocked())

    def test_other_operations_still_require_full_snapshot(self):
        self.make(); self.assertIsNotNone(self.blocked('archive'))


class Discard(unittest.TestCase):
    setUp = gap.GapTabs.setUp
    tearDown = gap.GapTabs.tearDown
    season = gap.GapTabs.season
    make = gap.GapTabs.make

    def run_discard(self, rec, token):
        return f.immediate(source_discard.execute(source_discard.ExecuteRequest(season_record_id=rec.id, preview_token=token, confirm_abandon=True), self.db))

    def test_absent_before_preview_is_reconcile_only(self):
        rec = self.make(); f.immediate(self.disk.delete_file(rec.show_path)); self.disk.deleted.clear()
        data = f.immediate(source_discard.preview(source_discard.PreviewRequest(season_record_id=rec.id), self.db))['data']
        self.assertTrue(data['source_absent']); self.assertEqual(rec.status, 'download_needed')
        self.assertTrue(self.run_discard(rec, data['preview_token'])['ok'])
        self.assertEqual(rec.status, 'source_absent'); self.assertEqual(self.disk.deleted, [])

    def test_disappeared_after_preview_reconciles_without_repeat_delete(self):
        rec = self.make()
        data = f.immediate(source_discard.preview(source_discard.PreviewRequest(season_record_id=rec.id), self.db))['data']
        f.immediate(self.disk.delete_file(rec.show_path)); self.disk.deleted.clear()
        self.assertTrue(self.run_discard(rec, data['preview_token'])['ok'])
        self.assertEqual(self.disk.deleted, [])

    def test_reappearance_cannot_use_absence_preview(self):
        rec = self.make(); f.immediate(self.disk.delete_file(rec.season_path))
        data = f.immediate(source_discard.preview(source_discard.PreviewRequest(season_record_id=rec.id), self.db))['data']
        self.disk.tree[rec.show_path].append(folder(rec.season_path)); self.disk.tree[rec.season_path] = []
        self.disk.deleted.clear()
        with self.assertRaises(Exception): self.run_discard(rec, data['preview_token'])
        self.assertEqual(self.disk.deleted, [])


if __name__ == '__main__':
    unittest.main()
