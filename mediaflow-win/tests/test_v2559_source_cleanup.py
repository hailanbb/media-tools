"""清源附件保全回归：同步内存数据库及模拟云盘，不访问真实媒体。"""
import json
import posixpath
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_v2551_review_closure as f
from test_v2552_recommended import Tree, folder
from app.api import archive


SRC = '/src/Show/Season 1'
DST = '/arc/Show/Season 1'
AUX = SRC + '/合并保留'


def item(path, size=100):
    return dict(f.file(posixpath.basename(path), size), path=path)


class Cloud(Tree):
    def __init__(self):
        super().__init__({
            '/src/Show': [folder(SRC)], SRC: [item(SRC + '/Show.S01E01.mkv'), folder(AUX)],
            DST: [item(DST + '/Show.S01E01.mkv')],
            AUX: [folder(AUX + '/旧目录')], AUX + '/旧目录': [item(AUX + '/旧目录/season.nfo')],
        })
        self.moves = []
        self.delete_result = {'success': True}
        self.keep_deleted = False
        self.move_result = {'success': True}

    async def mkdir(self, path):
        parent = posixpath.dirname(path)
        if parent not in self.tree:
            raise OSError('missing parent')
        if path not in self.tree:
            self.tree[path] = []
            self.tree[parent].append(folder(path))
        return {'success': True}

    async def move_file(self, source, parent, *, skip_existing=False):
        self.moves.append((source, parent, skip_existing))
        if self.move_result != {'success': True}:
            return self.move_result
        target = parent + '/' + posixpath.basename(source)
        if any(x['path'] == target for x in self.tree[parent]):
            return {'success': True}  # CD2 Skip 并不保证真正移动，必须双端复核。
        old_parent = posixpath.dirname(source)
        entry = next(x for x in self.tree[old_parent] if x['path'] == source)
        self.tree[old_parent].remove(entry)
        self.tree[parent].append(dict(entry, path=target))
        for old in list(self.tree):
            if old == source or old.startswith(source + '/'):
                new = target + old[len(source):]
                self.tree[new] = [dict(x, path=target + x['path'][len(source):], id='relocated-' + str(x['id']))
                                  for x in self.tree.pop(old)]
        return {'success': True}

    async def delete_file(self, path):
        if self.delete_result != {'success': True} or self.keep_deleted:
            return self.delete_result
        return await super().delete_file(path)


class CleanupRegression(unittest.TestCase):
    setUp = f.DatabaseRegression.setUp
    tearDown = f.DatabaseRegression.tearDown
    season = f.DatabaseRegression.season

    def run_cleanup(self, disk, *, new=True):
        if new:
            self.rec = self.season(status='already_archived', archive_target_path=DST,
                                   duplicate_episodes_json='[]', evidence_json='{}')
        async def no_group(*args, **kwargs):
            return None
        async def keep_show(*args, **kwargs):
            return dict(cleaned=False, status='retained_expected', reason='fixture other season retained')
        settings = SimpleNamespace(source_root='/src', archive_root='/arc')
        with patch.object(archive, 'get_settings', return_value=settings), \
                patch.object(archive, 'find_blocking_group_for_record', no_group), \
                patch.object(archive, '_inspect_and_cleanup_source_show_dir', keep_show):
            return f.immediate(archive._clean_source_redundant_impl(
                archive._ArchiveRequest(season_record_ids=['s']), self.db,
                cd2_client=disk, skip_summary_refresh=True))

    def assert_blocked(self, result, disk):
        self.assertFalse(result.ok, result.message)
        self.assertNotIn(SRC, disk.deleted)
        self.assertEqual(self.rec.status, 'already_archived')

    def test_pure_auxiliary_tree_is_preserved_then_season_deleted(self):
        disk = Cloud()
        result = self.run_cleanup(disk)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(disk.deleted, [SRC])
        self.assertIn(DST + '/合并保留/旧目录', disk.tree)
        self.assertEqual(disk.tree[DST + '/合并保留/旧目录'][0]['name'], 'season.nfo')
        self.assertEqual(self.rec.status, 'cleaned_redundant')
        self.assertIn('1 个附件', self.rec.status_detail)
        logs = self.session.query(f.OperationLog).all()
        receipt = json.loads(logs[0].detail_json)['preserved_auxiliary_folders'][0]
        self.assertEqual(receipt['target_path'], DST + '/合并保留')
        self.assertTrue(all(force for _, force in disk.reads))

    def test_existing_archive_folder_and_same_name_assets_are_not_overwritten(self):
        disk = Cloud()
        kept = DST + '/合并保留'
        disk.tree[DST].append(folder(kept))
        disk.tree[kept] = [item(kept + '/season.nfo', 999)]
        result = self.run_cleanup(disk)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(disk.tree[kept][0]['size'], 999)
        self.assertTrue(disk.moves[0][1].startswith(kept + '/清源-'))
        self.assertTrue(disk.moves[0][2])

    def test_empty_preservation_folder_can_be_cleaned(self):
        disk = Cloud(); disk.tree[AUX] = []
        self.assertTrue(self.run_cleanup(disk).ok)

    def test_video_or_unknown_content_inside_folder_still_blocks(self):
        for name in ('Show.S01E02.mkv', 'unknown.bin'):
            with self.subTest(name=name):
                disk = Cloud(); disk.tree[AUX] = [item(AUX + '/' + name)]
                result = self.run_cleanup(disk, new=not hasattr(self, 'rec'))
                self.assert_blocked(result, disk)
                self.assertEqual(disk.moves, [])
                self.assertIn('视频或待核对文件', result.message)

    def test_failed_nested_listing_preserves_source(self):
        disk = Cloud(); del disk.tree[AUX + '/旧目录']
        self.assert_blocked(self.run_cleanup(disk), disk)
        self.assertEqual(disk.moves, [])

    def test_invalid_nested_payload_preserves_source(self):
        disk = Cloud()
        original = disk.list_files
        async def read(path, **kwargs):
            return None if path == AUX else await original(path, **kwargs)
        disk.list_files = read
        self.assert_blocked(self.run_cleanup(disk), disk)

    def test_out_of_scope_child_path_is_rejected(self):
        disk = Cloud(); disk.tree[AUX] = [item('/src/Other/season.nfo')]
        self.assert_blocked(self.run_cleanup(disk), disk)
        self.assertEqual(disk.moves, [])

    def test_attachment_changed_between_preflight_and_move_blocks(self):
        disk = Cloud(); original = disk.list_files; reads = []
        async def read(path, **kwargs):
            if path == AUX + '/旧目录':
                reads.append(path)
                if len(reads) == 2:
                    disk.tree[path][0]['id'] = 'replaced'
            return await original(path, **kwargs)
        disk.list_files = read
        result = self.run_cleanup(disk)
        self.assert_blocked(result, disk)
        self.assertIn('清单已变化', result.message)
        self.assertEqual(disk.moves, [])

    def test_move_failure_is_not_cleanup_success(self):
        disk = Cloud(); disk.move_result = {'success': False}
        self.assert_blocked(self.run_cleanup(disk), disk)

    def test_false_positive_move_ack_does_not_allow_delete(self):
        disk = Cloud()
        async def noop(*args, **kwargs):
            return {'success': True}
        disk.move_file = noop
        self.assert_blocked(self.run_cleanup(disk), disk)

    def test_destination_missing_attachment_blocks_deletion(self):
        disk = Cloud(); original = disk.move_file
        async def move(*args, **kwargs):
            result = await original(*args, **kwargs)
            disk.tree[DST + '/合并保留/旧目录'] = []
            return result
        disk.move_file = move
        self.assert_blocked(self.run_cleanup(disk), disk)
        # 原附件目录已移走也不能在下一次重试绕过失败的保全后验。
        self.assert_blocked(self.run_cleanup(disk, new=False), disk)
        self.assertEqual(len(disk.moves), 1)

    def test_destination_size_mismatch_blocks_deletion(self):
        disk = Cloud(); original = disk.move_file
        async def move(*args, **kwargs):
            result = await original(*args, **kwargs)
            disk.tree[DST + '/合并保留/旧目录'][0]['size'] = 1
            return result
        disk.move_file = move
        self.assert_blocked(self.run_cleanup(disk), disk)

    def test_archive_coverage_must_be_complete_before_any_move(self):
        disk = Cloud(); disk.tree[DST] = []
        self.assert_blocked(self.run_cleanup(disk), disk)
        self.assertEqual(disk.moves, [])

    def test_archive_video_changes_during_move_prevent_cleanup(self):
        disk = Cloud(); original = disk.move_file
        async def move(*args, **kwargs):
            result = await original(*args, **kwargs)
            disk.tree[DST][0]['size'] += 1
            return result
        disk.move_file = move
        self.assert_blocked(self.run_cleanup(disk), disk)

    def test_failed_delete_receipt_and_retry_do_not_duplicate_preservation(self):
        disk = Cloud(); disk.delete_result = {'success': False}
        self.assert_blocked(self.run_cleanup(disk), disk)
        self.assertEqual(json.loads(self.rec.evidence_json)['source_cleanup_auxiliary']['state'], 'pending')
        self.assertEqual(len(disk.moves), 1)
        disk.delete_result = {'success': True}
        result = self.run_cleanup(disk, new=False)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(len(disk.moves), 1)
        self.assertEqual(json.loads(self.rec.evidence_json)['source_cleanup_auxiliary']['state'], 'completed')

    def test_source_receives_new_video_during_final_attachment_verification(self):
        disk = Cloud(); original = disk.list_files; reads = []
        async def read(path, **kwargs):
            if path == DST + '/合并保留':
                reads.append(path)
                if len(reads) == 2:
                    disk.tree[SRC].append(item(SRC + '/Show.S01E02.mkv'))
            return await original(path, **kwargs)
        disk.list_files = read
        self.assert_blocked(self.run_cleanup(disk), disk)

    def test_persisted_plan_survives_move_ack_failure_after_real_move(self):
        disk = Cloud(); original = disk.move_file
        async def move(*args, **kwargs):
            await original(*args, **kwargs)
            raise OSError('connection lost after remote move')
        disk.move_file = move
        self.assert_blocked(self.run_cleanup(disk), disk)
        self.assertIn('source_cleanup_auxiliary', json.loads(self.rec.evidence_json))
        self.assertTrue(self.run_cleanup(disk, new=False).ok)
        self.assertEqual(len(disk.moves), 1)

    def test_delete_success_but_source_still_visible_is_not_counted(self):
        disk = Cloud(); disk.keep_deleted = True
        self.assert_blocked(self.run_cleanup(disk), disk)

    def test_ordinary_redundant_season_without_subfolder_still_cleans(self):
        disk = Cloud(); disk.tree[SRC] = disk.tree[SRC][:1]
        self.assertTrue(self.run_cleanup(disk).ok)
        self.assertEqual(disk.moves, [])

    def test_other_subfolder_is_not_granted_deletion_permission(self):
        disk = Cloud(); disk.tree[SRC].append(folder(SRC + '/其他'))
        self.assert_blocked(self.run_cleanup(disk), disk)
        self.assertEqual(disk.moves, [])


if __name__ == '__main__':
    unittest.main()
