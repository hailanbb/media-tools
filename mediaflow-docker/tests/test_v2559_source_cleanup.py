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

    def test_pure_auxiliary_tree_is_cleaned_in_place_then_season_deleted(self):
        disk = Cloud()
        result = self.run_cleanup(disk)
        self.assertTrue(result.ok, result.message)
        self.assertIn(SRC, disk.deleted)
        self.assertNotIn(AUX, disk.tree)
        self.assertNotIn(DST + '/合并保留', disk.tree)
        self.assertEqual(disk.moves, [])
        self.assertEqual(self.rec.status, 'cleaned_redundant')
        self.assertIn('1 个冗余刮削文件', self.rec.status_detail)
        logs = self.session.query(f.OperationLog).all()
        receipt = json.loads(logs[0].detail_json)['preserved_auxiliary_folders'][0]
        self.assertTrue(receipt['cleaned_in_place'])
        self.assertEqual(receipt['target_path'], '')
        self.assertTrue(all(force for _, force in disk.reads))

    def test_existing_archive_folder_and_same_name_assets_are_not_overwritten(self):
        disk = Cloud()
        disk.tree[DST].append(item(DST + '/season.nfo', 999))
        result = self.run_cleanup(disk)
        self.assertTrue(result.ok, result.message)
        self.assertEqual([x['size'] for x in disk.tree[DST] if x['name'] == 'season.nfo'], [999])
        self.assertNotIn(DST + '/合并保留', disk.tree)
        self.assertEqual(disk.moves, [])

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
        disk = Cloud()
        disk.tree[SRC].append(item(SRC + '/Show.S01E01.srt'))
        disk.move_result = {'success': False}
        self.assert_blocked(self.run_cleanup(disk), disk)

    def test_false_positive_delete_ack_on_aux_does_not_allow_season_delete(self):
        disk = Cloud()
        original = disk.delete_file
        async def fake_del(path):
            return {'success': True} if path.startswith(AUX) else await original(path)
        disk.delete_file = fake_del
        self.assert_blocked(self.run_cleanup(disk), disk)

    def test_destination_missing_attachment_blocks_deletion(self):
        disk = Cloud()
        disk.tree[SRC] = [item(SRC + '/Show.S01E01.mkv')]
        kept = DST + '/合并保留'
        disk.tree[DST].append(folder(kept))
        disk.tree[kept] = []
        legacy_ev = {
            'source_cleanup_auxiliary': {
                'source_path': AUX,
                'target_path': kept,
                'inventory': {'旧目录/season.nfo': item(AUX + '/旧目录/season.nfo')},
                'state': 'pending',
            }
        }
        self.rec = self.season(
            status='already_archived', archive_target_path=DST,
            duplicate_episodes_json='[]', evidence_json=json.dumps(legacy_ev)
        )
        self.assert_blocked(self.run_cleanup(disk, new=False), disk)

    def test_destination_size_mismatch_blocks_deletion(self):
        disk = Cloud()
        disk.tree[SRC] = [item(SRC + '/Show.S01E01.mkv')]
        kept = DST + '/合并保留'
        disk.tree[DST].append(folder(kept))
        disk.tree[kept] = [folder(kept + '/旧目录')]
        disk.tree[kept + '/旧目录'] = [item(kept + '/旧目录/season.nfo', 1)]
        legacy_ev = {
            'source_cleanup_auxiliary': {
                'source_path': AUX,
                'target_path': kept,
                'inventory': {
                    '旧目录': folder(AUX + '/旧目录'),
                    '旧目录/season.nfo': item(AUX + '/旧目录/season.nfo', 100),
                },
                'state': 'pending',
            }
        }
        self.rec = self.season(
            status='already_archived', archive_target_path=DST,
            duplicate_episodes_json='[]', evidence_json=json.dumps(legacy_ev)
        )
        self.assert_blocked(self.run_cleanup(disk, new=False), disk)

    def test_archive_coverage_must_be_complete_before_any_move(self):
        disk = Cloud(); disk.tree[DST] = []
        self.assert_blocked(self.run_cleanup(disk), disk)
        self.assertIn(AUX, disk.tree)
        self.assertEqual(disk.moves, [])

    def test_archive_video_changes_during_aux_cleanup_prevent_season_delete(self):
        disk = Cloud(); original = disk.delete_file
        async def delete_and_mutate(path):
            res = await original(path)
            if path == AUX:
                disk.tree[DST][0]['size'] += 1
            return res
        disk.delete_file = delete_and_mutate
        self.assert_blocked(self.run_cleanup(disk), disk)

    def test_failed_delete_receipt_and_retry_do_not_duplicate_preservation(self):
        disk = Cloud(); original = disk.delete_file
        fail_src = True
        async def delete_season_guard(path):
            if path == SRC and fail_src:
                return {'success': False}
            return await original(path)
        disk.delete_file = delete_season_guard
        self.assert_blocked(self.run_cleanup(disk), disk)
        self.assertEqual(json.loads(self.rec.evidence_json)['source_cleanup_auxiliary']['state'], 'pending')
        self.assertNotIn(AUX, disk.tree)
        fail_src = False
        result = self.run_cleanup(disk, new=False)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(json.loads(self.rec.evidence_json)['source_cleanup_auxiliary']['state'], 'completed')

    def test_source_receives_new_video_during_final_attachment_verification(self):
        disk = Cloud(); original = disk.delete_file
        async def delete_and_inject(path):
            res = await original(path)
            if path == AUX:
                disk.tree[SRC].append(item(SRC + '/Show.S01E02.mkv'))
            return res
        disk.delete_file = delete_and_inject
        self.assert_blocked(self.run_cleanup(disk), disk)

    def test_persisted_plan_survives_delete_ack_failure_after_real_aux_delete(self):
        disk = Cloud(); original = disk.delete_file
        fail_once = True
        async def delete_lost_ack(path):
            nonlocal fail_once
            res = await original(path)
            if path == AUX and fail_once:
                fail_once = False
                raise OSError('connection lost after remote aux delete')
            return res
        disk.delete_file = delete_lost_ack
        self.assert_blocked(self.run_cleanup(disk), disk)
        self.assertIn('source_cleanup_auxiliary', json.loads(self.rec.evidence_json))
        self.assertTrue(self.run_cleanup(disk, new=False).ok)

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
