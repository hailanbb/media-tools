"""季删除与剧根收尾的真实路由联调，只有模拟云盘和内存数据库。"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_v2551_review_closure as f
from test_v2559_source_cleanup import Cloud, SRC, DST, folder, item
from app.api import archive, tasks
from app.services.source_cleanup_state import pending_root_cleanup_ids
from app.services.gap_fill_tracker import refresh_task_summary

SHOW = '/src/Show'
TARGET = '/arc/Show'
ROOT_AUX = SHOW + '/合并保留'


def cloud():
    d = Cloud()
    d.tree['/src'] = [folder(SHOW)]
    d.tree[TARGET] = [folder(DST), item(TARGET + '/poster.jpg', 999)]
    d.tree[SHOW] += [folder(ROOT_AUX), item(SHOW + '/poster.jpg'), item(SHOW + '/clearlogo.png', 256)]
    d.tree[ROOT_AUX] = [item(ROOT_AUX + '/season.nfo')]
    return d


class RootCleanup(unittest.TestCase):
    setUp = f.DatabaseRegression.setUp
    tearDown = f.DatabaseRegression.tearDown
    season = f.DatabaseRegression.season

    def run_cleanup(self, disk, *, new=True):
        if new:
            self.rec = self.season(status='already_archived', archive_target_path=DST,
                duplicate_episodes_json='[]', evidence_json='{}')
        async def no_group(*args, **kwargs):
            return None
        with patch.object(archive, 'get_settings', return_value=SimpleNamespace(source_root='/src', archive_root='/arc')), \
                patch.object(archive, 'find_blocking_group_for_record', no_group):
            return f.immediate(archive._clean_source_redundant_impl(
                archive._ArchiveRequest(season_record_ids=['s']), self.db,
                cd2_client=disk, skip_summary_refresh=True))

    def legacy(self, disk):
        disk.tree[SHOW] = [x for x in disk.tree[SHOW] if x['path'] != SRC]
        disk.tree.pop(SRC)
        self.rec = self.season(status='cleaned_redundant', archive_target_path=DST,
            duplicate_episodes_json='[]', evidence_json='{}')
        self.session.add(f.OperationLog(task_id='t', season_record_id='s', action='clean_source_redundant',
            status='completed', detail_json=json.dumps({'shell_cleaned': False})))
        self.session.commit()

    def test_season_and_root_preservation_folders_both_finish(self):
        disk = cloud(); result = self.run_cleanup(disk)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(disk.tree['/src'], [])
        self.assertEqual([x['size'] for x in disk.tree[TARGET] if x['name'] == 'poster.jpg'], [999])
        self.assertEqual([x['size'] for x in disk.tree[TARGET] if x['name'] == 'clearlogo.png'], [256])
        ev = json.loads(self.rec.evidence_json)
        target = ev['source_root_preservation']['target_path']
        self.assertEqual(target, TARGET)
        self.assertEqual(ev['source_root_preservation']['state'], 'completed')
        self.assertTrue(ev['source_root_preservation']['cleaned_in_place'])
        self.assertTrue(ev['source_root_cleanup']['cleaned'])
        self.assertNotIn(TARGET + '/合并保留', disk.tree)
        self.assertNotIn(ROOT_AUX, disk.tree)
        self.assertIn('剧集外层已移除', result.message)
        self.assertEqual(disk.deleted.count(SRC), 1)

    def test_legacy_closed_record_can_retry_only_the_root(self):
        disk = cloud(); self.legacy(disk)
        result = self.run_cleanup(disk, new=False)
        self.assertTrue(result.ok, result.message)
        self.assertNotIn(SRC, disk.deleted)
        self.assertEqual(disk.tree['/src'], [])
        self.assertNotIn(TARGET + '/合并保留', disk.tree)

    def test_another_season_is_retained_and_explained(self):
        disk = cloud(); disk.tree[SHOW].append(folder(SHOW + '/Season 2'))
        result = self.run_cleanup(disk)
        self.assertTrue(result.ok, result.message)
        self.assertIn('外层保留', result.message)
        self.assertIn('Season 2', self.rec.status_detail)
        self.assertIn(SHOW, disk.tree)
        self.assertEqual(f.immediate(pending_root_cleanup_ids(self.db, [self.rec])), set())

    def test_root_video_is_not_moved_as_an_attachment(self):
        disk = cloud(); disk.tree[SHOW].append(item(SHOW + '/unknown.mkv'))
        result = self.run_cleanup(disk)
        self.assertTrue(result.ok)
        self.assertIn('视频', result.message)
        self.assertFalse(any(src == SHOW for src, _, _ in disk.moves))

    def test_hidden_video_in_root_preservation_folder_keeps_pending(self):
        disk = cloud(); disk.tree[ROOT_AUX].append(item(ROOT_AUX + '/unknown.mkv'))
        result = self.run_cleanup(disk)
        self.assertFalse(result.ok, result.message)
        self.assertEqual(self.rec.status, 'already_archived')
        self.assertIn('外层待处理', self.rec.status_detail)
        self.assertEqual(disk.deleted.count(SRC), 1)

    def test_unknown_root_attachment_is_not_discarded(self):
        disk = cloud(); disk.tree[SHOW].append(item(SHOW + '/unknown.bin'))
        self.assertFalse(self.run_cleanup(disk).ok)
        self.assertIn(SHOW, disk.tree)

    def test_root_move_failure_can_resume_without_redeleting_season(self):
        disk = cloud(); original = disk.move_file
        async def move(src, parent, **kw):
            return {'success': False} if src == SHOW + '/clearlogo.png' else await original(src, parent, **kw)
        disk.move_file = move
        self.assertFalse(self.run_cleanup(disk).ok)
        self.assertEqual(self.rec.status, 'already_archived')
        disk.move_file = original
        result = self.run_cleanup(disk, new=False)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(disk.deleted.count(SRC), 1)

    def test_root_ack_failure_after_move_is_reconciled(self):
        disk = cloud(); original = disk.move_file
        async def move(src, parent, **kw):
            result = await original(src, parent, **kw)
            if src == SHOW + '/clearlogo.png':
                raise OSError('lost response')
            return result
        disk.move_file = move
        self.assertFalse(self.run_cleanup(disk).ok)
        self.assertTrue(self.run_cleanup(disk, new=False).ok)
        self.assertEqual(sum(src == SHOW + '/clearlogo.png' for src, _, _ in disk.moves), 1)

    def test_missing_root_attachment_after_move_cannot_be_bypassed_on_retry(self):
        disk = cloud(); original = disk.move_file
        async def move(src, parent, **kw):
            result = await original(src, parent, **kw)
            if src == SHOW + '/clearlogo.png':
                disk.tree[TARGET] = [x for x in disk.tree[TARGET] if x['name'] != 'clearlogo.png']
            return result
        disk.move_file = move
        self.assertFalse(self.run_cleanup(disk).ok)
        self.assertEqual(self.rec.status, 'already_archived')
        # 兼容旧版未完成外层保全回执：若目标附件缺失，源剧根消失后重试依然拒绝闭环
        disk.tree['/src'] = []
        disk.tree.pop(SHOW, None)
        shelf = TARGET + '/合并保留/清源-legacy'
        disk.tree[TARGET].append(folder(TARGET + '/合并保留'))
        disk.tree[TARGET + '/合并保留'] = [folder(shelf)]
        disk.tree[shelf] = []
        ev = json.loads(self.rec.evidence_json)
        ev['source_root_preservation'] = {
            'source_path': SHOW,
            'target_path': shelf,
            'inventory': {'poster.jpg': item(SHOW + '/poster.jpg')},
            'state': 'pending',
        }
        self.rec.evidence_json = json.dumps(ev)
        self.session.commit()
        self.assertFalse(self.run_cleanup(disk, new=False).ok)
        self.assertEqual(self.rec.status, 'already_archived')

    def test_reappeared_season_is_not_deleted_under_old_receipt(self):
        disk = cloud(); self.legacy(disk)
        disk.tree[SHOW].append(folder(SRC))
        disk.tree[SRC] = [item(SRC + '/New.S01E01.mkv')]
        result = self.run_cleanup(disk, new=False)
        self.assertFalse(result.ok)
        self.assertEqual(disk.moves, [])
        self.assertEqual(disk.deleted, [])

    def test_legacy_pending_root_reappears_without_rewriting_record(self):
        disk = cloud(); self.legacy(disk)
        rows = f.immediate(tasks.get_task_seasons('t', db=self.db))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['main_category'], 'review')
        self.assertTrue(rows[0]['evidence']['source_root_followup'])
        self.assertEqual(self.rec.status, 'cleaned_redundant')
        summary = f.immediate(refresh_task_summary('t', self.db))
        self.assertEqual(summary['review_needed'], 1)

    def test_root_completion_removes_legacy_pending_item(self):
        disk = cloud(); self.legacy(disk)
        self.assertTrue(self.run_cleanup(disk, new=False).ok)
        self.assertEqual(f.immediate(tasks.get_task_seasons('t', db=self.db)), [])

    def test_empty_show_delete_requires_success_and_absence(self):
        disk = cloud(); self.legacy(disk); disk.tree[SHOW] = []
        disk.keep_deleted = True
        result = self.run_cleanup(disk, new=False)
        self.assertFalse(result.ok)
        self.assertIn('删除后仍可见', result.message)

    def test_no_source_root_is_idempotent_after_archive_recheck(self):
        disk = cloud(); self.legacy(disk); disk.tree['/src'] = []; disk.tree.pop(SHOW)
        self.assertTrue(self.run_cleanup(disk, new=False).ok)
        self.assertEqual(disk.deleted, [])


if __name__ == '__main__':
    unittest.main()
