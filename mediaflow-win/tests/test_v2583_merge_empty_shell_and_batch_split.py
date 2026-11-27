"""v2.5.83 专项回归测试：
1. 非 S01 多季/纯空壳旧目录全季合并自动回收（复现 TMDB 127635 小蜘蛛场景）；
2. 单季模式无动作空计划前置拦截（严禁 can_execute=True 放行后执行报错）；
3. Season 0 / Specials 特辑保护迁移 + 嵌套空子目录 + 系统垃圾 (.DS_Store/Thumbs.db) 安全收尾；
4. 115/CD2 删子目录后父目录短时延迟退避重试 + 真实残留精准报错明细；
5. 待复核清理双模式批量处理分流（mode='merge_directory' vs mode='redundant_clean'）；
6. 前端双按钮与全季单条合并契约校验。
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_v2551_review_closure as fixtures
from test_v2552_recommended import Tree
from app.api import archive as archive_api
from app.core import directory_merge as merge
from app.models.directory_group import DirectoryGroup
from app.models.season import SeasonRecord


def _file(path: str, size: int = 1000, *, is_dir: bool = False, fid: str = "fid-1") -> dict:
    return {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "size": 0 if is_dir else size,
        "id": fid,
        "modified": "2026-11-27T00:00:00",
        "isDirectory": is_dir,
    }


class MovableTree(Tree):
    async def mkdir(self, path: str):
        self.tree.setdefault(path, [])
        return {"success": True}

    async def move_file(self, src_path: str, target_dir: str, *, skip_existing: bool = False):
        parent = src_path.rsplit("/", 1)[0]
        src_list = self.tree.get(parent, [])
        found = next((f for f in src_list if f["path"] == src_path), None)
        if not found:
            return {"success": False, "errorMessage": "not found"}
        self.tree[parent] = [f for f in src_list if f["path"] != src_path]
        moved = dict(found, path=f"{target_dir.rstrip('/')}/{found['name']}")
        self.tree.setdefault(target_dir, []).append(moved)
        return {"success": True}


class TestV2583EmptyShellAndDirectoryMerge(unittest.TestCase):
    setUp = fixtures.DatabaseRegression.setUp
    tearDown = fixtures.DatabaseRegression.tearDown

    def test_spidey_empty_shell_s04_auto_recovery_and_ghost_record_closure(self):
        """复现 TMDB 127635：主目录 S04/S05，旧目录只有空 Season 4 -> 全季合并自动回收空壳并清除假缺集记录。"""
        main_show = "/src/小蜘蛛和他的神奇小伙伴们 (2021) [tmdbid=127635]"
        old_show = "/src/小蜘蛛和他的神奇伙伴 (2021) [tmdbid=127635]"
        main_s4 = f"{main_show}/Season 4"
        old_s4 = f"{old_show}/Season 4"

        group = DirectoryGroup(
            id="dg-127635",
            task_id="t",
            tmdb_id="127635",
            category_key="cat:动漫/欧美动漫",
            group_kind="multi_source_dirs",
            title="同一作品有多个目录",
            paths_json=json.dumps([
                {"side": "source", "norm_path": old_show, "raw_name": old_show.rsplit("/", 1)[-1], "seasons": [4]},
                {"side": "source", "norm_path": main_show, "raw_name": main_show.rsplit("/", 1)[-1], "seasons": [4, 5]},
            ], ensure_ascii=False),
            evidence_json=json.dumps({"canonical_tmdb_title": "小蜘蛛和他的神奇小伙伴们", "canonical_tmdb_year": "2021"}),
            status="pending_confirm",
        )
        rec_main_s4 = SeasonRecord(
            id="rec-main-s4",
            task_id="t",
            show_name="小蜘蛛和他的神奇小伙伴们 (2021) [tmdbid=127635]",
            show_path=main_show,
            season_path=main_s4,
            season_number=4,
            tmdb_id="127635",
            local_episode_count=19,
            total_episodes=38,
            recognized_episodes_json=json.dumps(list(range(1, 39))),
            status="archive_ready",
            status_detail="全 38 集齐全，可归档",
            evidence_json=json.dumps({"directory_group": {"id": "dg-127635", "status": "pending_confirm", "requires_confirm": True}}),
        )
        rec_old_s4 = SeasonRecord(
            id="rec-old-s4",
            task_id="t",
            show_name="小蜘蛛和他的神奇伙伴 (2021) [tmdbid=127635]",
            show_path=old_show,
            season_path=old_s4,
            season_number=4,
            tmdb_id="127635",
            local_episode_count=0,
            total_episodes=38,
            recognized_episodes_json="[]",
            status="download_needed",
            status_detail="空壳产生的假缺集",
            evidence_json=json.dumps({"directory_group": {"id": "dg-127635", "status": "pending_confirm", "requires_confirm": True}}),
        )
        self.session.add_all([group, rec_main_s4, rec_old_s4])
        self.session.commit()

        disk = Tree({
            old_show: [_file(old_s4, is_dir=True, fid="d-old-s4")],
            old_s4: [],
            main_show: [_file(main_s4, is_dir=True, fid="d-main-s4")],
            main_s4: [_file(f"{main_s4}/小蜘蛛和他的神奇小伙伴们 - S04E01.mkv", size=5000000, fid="v-1")],
        })

        # 1. 若旧接口只查 S01 且 all_seasons=False，必须前置拦截空计划（禁止 can_execute=True）
        bad_single = fixtures.immediate(merge.build_directory_merge_plan(
            self.db, "t", "dg-127635",
            target_path="/archive/动漫/欧美动漫/小蜘蛛和他的神奇小伙伴们 (2021) [tmdbid=127635]",
            season_number=1,
            all_seasons=False,
            cd2_client=disk,
            persist_plan=False,
        ))
        self.assertFalse(bad_single["can_execute"], bad_single)
        self.assertTrue(any("Season 4" in r for r in bad_single["block_reasons"]), bad_single["block_reasons"])

        # 2. 默认全季合并 (all_seasons=True)：识别为纯空壳回收并一次收尾成功
        plan = fixtures.immediate(merge.build_directory_merge_plan(
            self.db, "t", "dg-127635",
            target_path="/archive/动漫/欧美动漫/小蜘蛛和他的神奇小伙伴们 (2021) [tmdbid=127635]",
            all_seasons=True,
            smart_collision=True,
            cd2_client=disk,
        ))
        self.assertTrue(plan["can_execute"], plan)
        self.assertTrue(plan["is_empty_shell_cleanup"], plan)
        self.assertEqual(plan["directories_to_clean"], [old_s4, old_show])

        res = fixtures.immediate(merge.execute_directory_merge(
            self.db, "t", "dg-127635", plan_id=plan["plan_id"], cd2_client=disk, skip_summary_refresh=True
        ))
        self.assertTrue(res["ok"], res)
        self.assertIn(old_s4, res["cleaned_dirs"])
        self.assertIn(old_show, res["cleaned_dirs"])
        self.assertIn("空壳", res["message"])

        # 验证数据库状态：旧空壳假缺集记录已退场，目录组已确认
        self.session.expire_all()
        old_rec_after = self.session.get(SeasonRecord, "rec-old-s4")
        dg_after = self.session.get(DirectoryGroup, "dg-127635")
        self.assertEqual(old_rec_after.status, "source_absent")
        self.assertEqual(dg_after.status, "confirmed")

    def test_specials_season0_and_junk_files_and_empty_subdirs_cleaned(self):
        """旧目录包含有内容的 Season 0、嵌套空 extrafanart 与 .DS_Store/Thumbs.db -> 全自动保护迁移并清空旧目录。"""
        main_show = "/src/规范剧 (2025) [tmdbid=99]"
        old_show = "/src/旧别名 (2025) [tmdbid=99]"
        old_s0 = f"{old_show}/Season 0"
        old_extra = f"{old_show}/extrafanart"
        old_extra_sub = f"{old_extra}/empty_nested"

        group = DirectoryGroup(
            id="dg-99",
            task_id="t",
            tmdb_id="99",
            category_key="cat:_root_",
            group_kind="multi_source_dirs",
            title="同一作品有多个目录",
            paths_json=json.dumps([
                {"side": "source", "norm_path": old_show, "raw_name": old_show.rsplit("/", 1)[-1], "seasons": [1]},
                {"side": "source", "norm_path": main_show, "raw_name": main_show.rsplit("/", 1)[-1], "seasons": [1]},
            ], ensure_ascii=False),
            evidence_json=json.dumps({"canonical_tmdb_title": "规范剧", "canonical_tmdb_year": "2025"}),
            status="pending_confirm",
        )
        self.session.add(group)
        self.session.commit()

        disk = MovableTree({
            old_show: [
                _file(old_s0, is_dir=True, fid="d-s0"),
                _file(old_extra, is_dir=True, fid="d-ex"),
                _file(f"{old_show}/.DS_Store", size=64, fid="j-1"),
            ],
            old_s0: [
                _file(f"{old_s0}/SP01.mkv", size=2048, fid="sp-1"),
                _file(f"{old_s0}/Thumbs.db", size=32, fid="j-2"),
            ],
            old_extra: [_file(old_extra_sub, is_dir=True, fid="d-ex2")],
            old_extra_sub: [],
            main_show: [],
            f"{main_show}/Season 0": [],
        })

        plan = fixtures.immediate(merge.build_directory_merge_plan(
            self.db, "t", "dg-99",
            target_path="/archive/规范剧 (2025) [tmdbid=99]",
            all_seasons=True,
            smart_collision=True,
            cd2_client=disk,
        ))
        self.assertTrue(plan["can_execute"], plan)
        self.assertEqual(len(plan["assets_to_preserve"]), 1)
        self.assertEqual(plan["assets_to_preserve"][0]["target_dir"], f"{main_show}/Season 0")
        self.assertEqual(len(plan["junk_files_to_clean"]), 2)
        self.assertIn(old_extra_sub, plan["directories_to_clean"])
        self.assertIn(old_s0, plan["directories_to_clean"])
        self.assertIn(old_show, plan["directories_to_clean"])

        res = fixtures.immediate(merge.execute_directory_merge(
            self.db, "t", "dg-99", plan_id=plan["plan_id"], cd2_client=disk, skip_summary_refresh=True
        ))
        self.assertTrue(res["ok"], res)
        self.assertIn("SP01.mkv", res["preserved_assets"])
        self.assertIn(old_show, res["cleaned_dirs"])

    def test_delete_dir_backoff_retry_on_stale_child_and_explicit_leftover_error(self):
        """115/CD2 刚删子目录后父目录短时延迟返回旧缓存 -> 退避重试成功；若真有未知残留 -> 精准列出文件名。"""
        class DelayedCacheDisk:
            def __init__(self, responses):
                self.responses = list(responses)
                self.deleted = []

            async def list_files(self, path, force_refresh=False):
                return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

            async def delete_file(self, path):
                self.deleted.append(path)
                return {"success": True}

        # Case A: 首次强读仍返回刚刚删掉的 Season 4，第 2 次返回空 -> 重试后删除成功
        disk_retry = DelayedCacheDisk([
            [_file("/src/old/Season 4", is_dir=True)],
            [],
        ])
        with patch.object(merge.asyncio, "sleep", return_value=None):
            ok, leftovers = fixtures.immediate(merge.delete_dir_robust_detailed(
                disk_retry, "/src/old", recently_deleted_children={"season 4"}, max_retries=3
            ))
        self.assertTrue(ok)
        self.assertEqual(leftovers, [])
        self.assertEqual(disk_retry.deleted, ["/src/old"])

        # Case B: 真实残留未迁移视频 -> 绝不删除，且精准返回残留文件名
        disk_real = DelayedCacheDisk([
            [_file("/src/old/untracked_video.mkv", size=9999)],
        ])
        ok2, leftovers2 = fixtures.immediate(merge.delete_dir_robust_detailed(
            disk_real, "/src/old", recently_deleted_children={"season 4"}, max_retries=3
        ))
        self.assertFalse(ok2)
        self.assertEqual(leftovers2, ["untracked_video.mkv"])
        self.assertEqual(disk_real.deleted, [])


class TestV2583RecommendedBatchSplit(unittest.TestCase):
    setUp = fixtures.DatabaseRegression.setUp
    tearDown = fixtures.DatabaseRegression.tearDown

    def test_mode_split_merge_directory_vs_redundant_clean(self):
        """验证 recommended-plan 支持 mode='merge_directory' 与 mode='redundant_clean' 严格隔离互不串扰。"""
        main_show = "/src/双模式剧 (2025) [tmdbid=500]"
        old_show = "/src/双模式剧别名 (2025) [tmdbid=500]"
        dup_show = "/src/副本剧 (2025) [tmdbid=600]"
        dup_s1 = f"{dup_show}/Season 1"

        dg = DirectoryGroup(
            id="dg-500",
            task_id="t",
            tmdb_id="500",
            category_key="cat:_root_",
            group_kind="multi_source_dirs",
            title="同一作品有多个目录",
            paths_json=json.dumps([
                {"side": "source", "norm_path": old_show, "raw_name": old_show.rsplit("/", 1)[-1], "seasons": [1]},
                {"side": "source", "norm_path": main_show, "raw_name": main_show.rsplit("/", 1)[-1], "seasons": [1]},
            ], ensure_ascii=False),
            evidence_json=json.dumps({"canonical_tmdb_title": "双模式剧", "canonical_tmdb_year": "2025"}),
            status="pending_confirm",
        )
        rec_dir = SeasonRecord(
            id="rec-500",
            task_id="t",
            show_name="双模式剧 (2025) [tmdbid=500]",
            show_path=main_show,
            season_path=f"{main_show}/Season 1",
            season_number=1,
            tmdb_id="500",
            local_episode_count=10,
            total_episodes=10,
            recognized_episodes_json=json.dumps(list(range(1, 11))),
            status="archive_ready",
            evidence_json=json.dumps({"directory_group": {"id": "dg-500", "status": "pending_confirm", "requires_confirm": True}}),
        )
        rec_dup = SeasonRecord(
            id="rec-600",
            task_id="t",
            show_name="副本剧 (2025) [tmdbid=600]",
            show_path=dup_show,
            season_path=dup_s1,
            season_number=1,
            tmdb_id="600",
            local_episode_count=1,
            total_episodes=1,
            recognized_episodes_json="[1]",
            duplicate_episodes_json="[1]",
            status="review_needed",
            status_detail="存在重复版本",
        )
        self.session.add_all([dg, rec_dir, rec_dup])
        self.session.commit()

        disk = Tree({
            old_show: [_file(f"{old_show}/Season 1", is_dir=True, fid="d-o1")],
            f"{old_show}/Season 1": [],
            main_show: [_file(f"{main_show}/Season 1", is_dir=True, fid="d-m1")],
            f"{main_show}/Season 1": [_file(f"{main_show}/Season 1/双模式剧 - S01E01.mkv", size=5000, fid="vm-1")],
            dup_s1: [
                _file(f"{dup_s1}/副本剧.S01E01.2160p.WEB-DL.mkv", size=4_000_000_000, fid="vd-hi"),
                _file(f"{dup_s1}/副本剧.S01E01.720p.WEB-DL.mp4", size=500_000_000, fid="vd-lo"),
            ],
        })

        with patch.object(archive_api, "CloudDrive2Client", return_value=disk):
            plan_merge = asyncio.run(archive_api.build_recommended_batch_plan(
                archive_api.RecommendedPlanRequest(task_id="t", mode="merge_directory"),
                self.db,
            ))
            plan_clean = asyncio.run(archive_api.build_recommended_batch_plan(
                archive_api.RecommendedPlanRequest(task_id="t", mode="redundant_clean"),
                self.db,
            ))

        # 多目录模式：只含 merge_items，且识别为纯空壳回收
        self.assertEqual(plan_merge["mode"], "merge_directory")
        self.assertEqual(len(plan_merge["merge_items"]), 1)
        self.assertTrue(plan_merge["merge_items"][0]["is_empty_shell_cleanup"])
        self.assertEqual(plan_merge["counts"]["empty_shell_count"], 1)
        self.assertEqual(plan_merge["clean_items"], [])
        self.assertEqual(plan_merge["redundant_source_items"], [])

        # 冗余清理模式：只含 clean_items，不含 merge_items
        self.assertEqual(plan_clean["mode"], "redundant_clean")
        self.assertEqual(plan_clean["merge_items"], [])
        self.assertEqual(len(plan_clean["clean_items"]), 1)
        self.assertEqual(plan_clean["clean_items"][0]["season_record_id"], "rec-600")

    def test_frontend_contract_has_two_split_buttons_and_all_seasons_merge(self):
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn("openRecommendedBatchModal('merge_directory')", html)
        self.assertIn("批量处理多目录问题", html)
        self.assertIn("openRecommendedBatchModal('redundant_clean')", html)
        self.assertIn("批量冗余清理", html)
        self.assertIn("all_seasons: true", html)
        self.assertIn("is_empty_shell_cleanup", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
