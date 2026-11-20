"""归并保全门禁回归：仅内存数据库、即时模拟云盘，不访问真实媒体。"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_v2551_review_closure as fixtures
from test_v2552_recommended import Tree
from app.core import directory_merge as merge


def asset(path, **extra):
    return dict(name=path.rsplit('/', 1)[-1], path=path, size=100,
                id='fixture-asset', modified='2026-01-01', isDirectory=False, **extra)


class EmptyDirectoryGuards(unittest.TestCase):
    def disk(self, listing, receipt=None):
        class Disk:
            deleted = []

            async def list_files(self, path, force_refresh=False):
                self.force_refresh = force_refresh
                return listing

            async def delete_file(self, path):
                self.deleted.append(path)
                return receipt
        return Disk()

    def test_invalid_listing_never_deletes_directory(self):
        for listing in (None, {}, '', False):
            with self.subTest(listing=listing):
                disk = self.disk(listing, {'success': True})
                self.assertFalse(fixtures.immediate(merge.delete_dir_robust(disk, '/fixture/source')))
                self.assertEqual(disk.deleted, [])
                self.assertTrue(disk.force_refresh)

    def test_nonempty_directory_is_preserved(self):
        disk = self.disk([asset('/fixture/source/season.nfo')], {'success': True})
        self.assertFalse(fixtures.immediate(merge.delete_dir_robust(disk, '/fixture/source')))
        self.assertEqual(disk.deleted, [])

    def test_unknown_or_negative_receipt_is_not_success(self):
        for receipt in (None, {}, {'success': False}, {'success': 'false'}):
            with self.subTest(receipt=receipt):
                disk = self.disk([], receipt)
                self.assertFalse(fixtures.immediate(merge.delete_dir_robust(disk, '/fixture/source')))

    def test_explicit_success_receipt_is_accepted(self):
        disk = self.disk([], {'success': True})
        self.assertTrue(fixtures.immediate(merge.delete_dir_robust(disk, '/fixture/source')))
        self.assertEqual(disk.deleted, ['/fixture/source'])


class InventoryDiagnostics(unittest.TestCase):
    def test_duplicate_paths_are_not_reported_as_zero_changes(self):
        item = asset('/fixture/season.nfo')
        before = [item, dict(item, id='second')]
        after = [dict(item, id='replacement'), dict(item, id='second')]
        self.assertIn('存在重复路径', merge._directory_inventory_difference(before, after))

    def test_same_count_still_explains_id_and_time_changes(self):
        before = [asset('/fixture/season.nfo')]
        after = [dict(before[0], id='replacement', modified='2026-01-02')]
        message = merge._directory_inventory_difference(before, after)
        self.assertIn('信息变化 1 项', message)
        self.assertIn('season.nfo（文件标识、修改时间）', message)

    def test_added_removed_and_type_changes_are_separate(self):
        before = [asset('/fixture/old.nfo'), asset('/fixture/keep.nfo')]
        after = [asset('/fixture/new.nfo'), dict(before[1], isDirectory=True)]
        message = merge._directory_inventory_difference(before, after)
        self.assertIn('新增 1 项、缺失 1 项、信息变化 1 项', message)
        self.assertIn('文件/目录类型', message)

    def test_details_are_bounded_and_listing_order_is_irrelevant(self):
        files = [asset(f'/fixture/{i}.nfo') for i in range(8)]
        self.assertIn('另有 3 项', merge._directory_inventory_difference([], files))
        self.assertEqual(merge._directory_inventory_snapshot(files),
                         merge._directory_inventory_snapshot(list(reversed(files))))


class AssetExecutionGuards(unittest.TestCase):
    def run_plan(self, plan, disk):
        group = SimpleNamespace(task_id='fixture-task', evidence_json=json.dumps({'latest_merge_plan': plan}))
        class DB:
            async def get(self, model, key):
                return group if key == 'fixture-group' else SimpleNamespace()
        return fixtures.immediate(merge.execute_directory_merge(
            DB(), 'fixture-task', 'fixture-group', plan_id='fixture-plan', cd2_client=disk))

    def plan(self, item):
        return dict(plan_id='fixture-plan', can_execute=True,
                    source_target_show_path='/fixture/New', archive_target_show_path='/archive/New',
                    assets_to_preserve=[item])

    def test_legacy_asset_plan_requires_new_preview_before_mutations(self):
        disk = Tree({})
        with self.assertRaisesRegex(RuntimeError, '附件计划缺少文件指纹'):
            self.run_plan(self.plan({'source_path': '/fixture/Old/poster.jpg'}), disk)
        self.assertEqual(disk.deleted, [])

    def test_asset_is_rechecked_after_video_step(self):
        source = asset('/fixture/Old/poster.jpg')
        disk = Tree({'/fixture/Old': [source]})
        item = dict(source_path=source['path'], file_name=source['name'], target_dir='/fixture/New',
                    source_snapshot=merge.build_file_fingerprints([source])[source['path']])
        plan = self.plan(item)
        plan['files_to_move'] = [dict(source_path='/fixture/Old/video.mkv', source_filename='video.mkv',
                                      target_dir='/fixture/New', new_filename='video.mkv')]
        moved = []
        async def move(cd2, src, target, name, prepared):
            moved.append(src)
            disk.tree['/fixture/Old'][0] = dict(source, id='changed-during-video-step')
        with patch.object(merge, 'move_and_rename_file_robust', side_effect=move):
            with self.assertRaisesRegex(RuntimeError, '附件保全未完成') as caught:
                self.run_plan(plan, disk)
        self.assertIn('文件 ID 已变化', str(caught.exception.__cause__))
        self.assertEqual(moved, ['/fixture/Old/video.mkv'])
        self.assertEqual(disk.deleted, [])

    def test_snapshot_difference_stops_before_any_move(self):
        before = asset('/fixture/Old/season.nfo')
        disk = Tree({'/fixture/Old': [dict(before, id='replacement')]})
        plan = self.plan({})
        plan['inventory_snapshots'] = {'/fixture/Old': merge._directory_inventory_snapshot([before])}
        with patch.object(merge, 'move_and_rename_file_robust') as move:
            with self.assertRaisesRegex(RuntimeError, '信息变化 1 项.*文件标识'):
                self.run_plan(plan, disk)
        move.assert_not_called()
        self.assertEqual(disk.deleted, [])


class RootAssetPlan(unittest.TestCase):
    setUp = fixtures.DatabaseRegression.setUp
    tearDown = fixtures.DatabaseRegression.tearDown

    def test_root_asset_has_same_snapshot_as_season_assets(self):
        old = '/src/Old (2026) [tmdbid=1]'
        target = '/src/New (2026) [tmdbid=1]'
        source = asset(old + '/tvshow.nfo')
        group = fixtures.DirectoryGroup(id='g', task_id='t', tmdb_id='1', category_key='cat:_root_',
            paths_json=json.dumps([{'side': 'source', 'norm_path': p, 'raw_name': p.rsplit('/', 1)[-1],
                                    'seasons': [1]} for p in (old, target)]))
        self.session.add(group)
        self.session.commit()
        disk = Tree({old: [source], target: []})
        plan = fixtures.immediate(merge.build_directory_merge_plan(
            self.db, 't', 'g', canonical_title='New', first_air_year='2026', all_seasons=True,
            auto_canonical_target=True, cd2_client=disk))
        self.assertTrue(plan['can_execute'], plan)
        self.assertEqual(plan['assets_to_preserve'][0]['source_snapshot'],
                         merge.build_file_fingerprints([source])[source['path']])
        self.assertEqual(disk.deleted, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
