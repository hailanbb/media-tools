"""v2.5.85 专项回归测试：彻底废除生成「合并保留」目录并安全清理存量遗留「合并保留」目录。

覆盖场景：
1. 目录归并（含剧根与季级同名 tvshow.nfo/poster.jpg/backdrop.jpg/season04-poster.jpg/season.nfo 及 .tvshow.nfo.6TlXQz 隐藏临时文件）：
   - 目标缺失的标准刮削资产直接补齐至目标剧根/季目录；
   - 目标已存在的同名冗余刮削资产及刮削临时文件经指纹核验后原地销毁，绝不生成任何「合并保留」目录；
2. 存量历史「合并保留」目录（含 Emby 误生成的 season.nfo 及 .tvshow.nfo.6TlXQz）在「批量冗余清理」(mode='redundant_clean') 中被自动发现并原地清除；
3. 若历史「合并保留」目录内含真实视频或未知非刮削文件 (如 S01E02.mkv / unknown.bin)，严格拦截不删并归入 skipped_items 告警；
4. 归档已齐清源与源剧根外层收尾：原地销毁遗留「合并保留」、仅将缺失公共刮削补齐至归档剧根，绝不向归档库 nas.finish 创建「合并保留」。
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_v2551_review_closure as f
from test_v2559_source_cleanup import Cloud, folder, item
from app.api import archive as archive_api
from app.core import directory_merge as merge
from app.models.directory_group import DirectoryGroup
from app.models.season import SeasonRecord


class TestV2585EliminateHebingBaoliu(unittest.TestCase):
    setUp = f.DatabaseRegression.setUp
    tearDown = f.DatabaseRegression.tearDown
    season = f.DatabaseRegression.season

    def test_merge_never_creates_hebing_baoliu_and_cleans_duplicate_scrape_and_temp_files(self):
        """复现小蜘蛛 127635：同名刮削图/NFO 与 .tvshow.nfo.6TlXQz 直接安全清理，缺失 clearlogo.png 补齐至剧根，零合并保留目录。"""
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
                {"side": "source", "norm_path": main_show, "raw_name": main_show.rsplit("/", 1)[-1], "seasons": [4]},
            ], ensure_ascii=False),
            evidence_json=json.dumps({"canonical_tmdb_title": "小蜘蛛和他的神奇小伙伴们", "canonical_tmdb_year": "2021"}),
            status="pending_confirm",
        )
        self.session.add(group)
        self.session.commit()

        disk = Cloud()
        disk.tree = {
            "/src": [folder(old_show), folder(main_show)],
            old_show: [
                folder(old_s4),
                item(f"{old_show}/tvshow.nfo", 1925),
                item(f"{old_show}/poster.jpg", 739720),
                item(f"{old_show}/backdrop.jpg", 878114),
                item(f"{old_show}/season04-poster.jpg", 1108996),
                item(f"{old_show}/clearlogo.png", 1651991),  # 主目录缺失，应补齐到主剧根
                item(f"{old_show}/.tvshow.nfo.6TlXQz", 0),   # 原子写残留垃圾，应直接删除
            ],
            old_s4: [
                item(f"{old_s4}/season.nfo", 300),
            ],
            main_show: [
                folder(main_s4),
                item(f"{main_show}/tvshow.nfo", 2271),
                item(f"{main_show}/poster.jpg", 1167910),
                item(f"{main_show}/backdrop.jpg", 1183272),
                item(f"{main_show}/season04-poster.jpg", 741477),
            ],
            main_s4: [
                item(f"{main_s4}/season.nfo", 320),
                item(f"{main_s4}/小蜘蛛和他的神奇小伙伴们 - S04E01.mkv", 5000000),
            ],
        }

        plan = f.immediate(merge.build_directory_merge_plan(
            self.db, "t", "dg-127635",
            target_path="/arc/动漫/欧美动漫/小蜘蛛和他的神奇小伙伴们 (2021) [tmdbid=127635]",
            all_seasons=True,
            smart_collision=True,
            cd2_client=disk,
        ))
        self.assertTrue(plan["can_execute"], plan)
        # 仅缺失的 clearlogo.png 迁入主剧根，绝不产生任何包含“合并保留”的目标路径
        self.assertEqual([a["file_name"] for a in plan["assets_to_preserve"]], ["clearlogo.png"])
        self.assertEqual(plan["assets_to_preserve"][0]["target_dir"], main_show)
        for a in plan["assets_to_preserve"] + plan["files_to_move"]:
            self.assertNotIn("合并保留", a.get("target_dir", ""))
            self.assertNotIn("合并保留", a.get("target_path", ""))
        self.assertEqual(len(plan["redundant_scrape_to_clean"]), 5)
        self.assertEqual([j["file_name"] for j in plan["junk_files_to_clean"]], [".tvshow.nfo.6TlXQz"])

        res = f.immediate(merge.execute_directory_merge(
            self.db, "t", "dg-127635", plan_id=plan["plan_id"], cd2_client=disk, skip_summary_refresh=True
        ))
        self.assertTrue(res["ok"], res)
        self.assertFalse(any("合并保留" in k for k in disk.tree), f"出现了合并保留目录: {list(disk.tree)}")
        self.assertNotIn(old_show, disk.tree)
        self.assertTrue(any(x["name"] == "clearlogo.png" for x in disk.tree[main_show]))

    def test_batch_redundant_clean_discovers_and_removes_legacy_hebing_and_blocks_videos(self):
        """验证「批量冗余清理」自动发现剧根与季内遗留合并保留（含 Emby 误建 season.nfo 与 .tvshow.nfo.6TlXQz）并原地铲除；含视频则安全跳过。"""
        show_clean = "/src/小蜘蛛和他的神奇小伙伴们 (2021) [tmdbid=127635]"
        s5_clean = f"{show_clean}/Season 5"
        hb_clean = f"{show_clean}/合并保留"
        hb_clean_sub = f"{hb_clean}/小蜘蛛和他的神奇伙伴 (2021) [tmdbid=127635]"

        show_risky = "/src/含视频风险剧 (2025) [tmdbid=888]"
        s1_risky = f"{show_risky}/Season 1"
        hb_risky = f"{s1_risky}/合并保留"

        rec1 = SeasonRecord(
            id="rec-spidey-s5",
            task_id="t",
            show_name="小蜘蛛和他的神奇小伙伴们 (2021) [tmdbid=127635]",
            show_path=show_clean,
            season_path=s5_clean,
            season_number=5,
            tmdb_id="127635",
            local_episode_count=4,
            total_episodes=20,
            recognized_episodes_json="[3,4,5,6]",
            status="download_needed",
        )
        rec2 = SeasonRecord(
            id="rec-risky-s1",
            task_id="t",
            show_name="含视频风险剧 (2025) [tmdbid=888]",
            show_path=show_risky,
            season_path=s1_risky,
            season_number=1,
            tmdb_id="888",
            local_episode_count=1,
            total_episodes=10,
            recognized_episodes_json="[1]",
            status="airing",
        )
        self.session.add_all([rec1, rec2])
        self.session.commit()

        disk = Cloud()
        disk.tree = {
            show_clean: [folder(s5_clean), folder(hb_clean), item(f"{show_clean}/tvshow.nfo", 2271)],
            s5_clean: [item(f"{s5_clean}/S05E03.mkv", 10000)],
            hb_clean: [
                item(f"{hb_clean}/season.nfo", 244),  # Emby 误写入的假季 NFO
                folder(hb_clean_sub),
            ],
            hb_clean_sub: [
                item(f"{hb_clean_sub}/.tvshow.nfo.6TlXQz", 0),
                item(f"{hb_clean_sub}/backdrop.jpg", 878114),
                item(f"{hb_clean_sub}/poster.jpg", 739720),
                item(f"{hb_clean_sub}/tvshow.nfo", 1925),
            ],
            show_risky: [folder(s1_risky)],
            s1_risky: [item(f"{s1_risky}/S01E01.mkv", 10000), folder(hb_risky)],
            hb_risky: [item(f"{hb_risky}/hidden_episode.mkv", 999999)],
        }

        with patch.object(archive_api, "CloudDrive2Client", return_value=disk):
            plan = asyncio.run(archive_api.build_recommended_batch_plan(
                archive_api.RecommendedPlanRequest(task_id="t", mode="redundant_clean"),
                self.db,
            ))
            self.assertEqual(plan["counts"]["legacy_hebing_count"], 1)
            self.assertEqual(plan["legacy_hebing_items"][0]["folder_path"], hb_clean)
            self.assertEqual(plan["legacy_hebing_items"][0]["file_count"], 5)
            # 含视频的合并保留目录必须被安全拦截进 skipped_items
            self.assertTrue(any(s.get("kind") == "clean_legacy_hebing" for s in plan["skipped_items"]))

            exec_res = asyncio.run(archive_api._run_recommended_batch_plan_locked(
                archive_api.RecommendedExecuteRequest(task_id="t", plan_id=plan["plan_id"]),
                self.db,
            ))
            self.assertTrue(exec_res["ok"], exec_res)
            self.assertEqual(len(exec_res["executed_hebing"]), 1)
            self.assertNotIn(hb_clean, disk.tree)
            self.assertIn(hb_risky, disk.tree)  # 含视频目录毫发无损

    def test_source_cleanup_and_root_shell_never_create_archive_hebing(self):
        """验证归档已齐清源与剧集外层收尾：原地销毁源端合并保留，缺失刮削补齐至归档剧根，归档库零合并保留目录。"""
        src_show = "/src/Show"
        src_s1 = "/src/Show/Season 1"
        src_s1_hb = f"{src_s1}/合并保留"
        src_root_hb = f"{src_show}/合并保留"
        arc_show = "/arc/Show"
        arc_s1 = "/arc/Show/Season 1"

        self.rec = self.season(status="already_archived", archive_target_path=arc_s1, duplicate_episodes_json="[]", evidence_json="{}")
        disk = Cloud()
        disk.tree = {
            "/src": [folder(src_show)],
            src_show: [folder(src_s1), folder(src_root_hb), item(f"{src_show}/poster.jpg", 100), item(f"{src_show}/clearlogo.png", 500)],
            src_root_hb: [item(f"{src_root_hb}/season.nfo", 244)],
            src_s1: [item(f"{src_s1}/Show.S01E01.mkv", 1000), folder(src_s1_hb)],
            src_s1_hb: [item(f"{src_s1_hb}/season.nfo", 300)],
            arc_show: [folder(arc_s1), item(f"{arc_show}/poster.jpg", 999)],
            arc_s1: [item(f"{arc_s1}/Show.S01E01.mkv", 1000)],
        }

        async def no_group(*args, **kwargs):
            return None

        with patch.object(archive_api, "get_settings", return_value=SimpleNamespace(source_root="/src", archive_root="/arc")), \
                patch.object(archive_api, "find_blocking_group_for_record", no_group):
            res = f.immediate(archive_api._clean_source_redundant_impl(
                archive_api._ArchiveRequest(season_record_ids=["s"]), self.db, cd2_client=disk, skip_summary_refresh=True
            ))
        self.assertTrue(res.ok, res.message)
        self.assertEqual(disk.tree["/src"], [])
        self.assertFalse(any("合并保留" in p for p in disk.tree), f"归档库不应出现合并保留: {list(disk.tree)}")
        # 归档剧根已有 poster.jpg 未被覆盖（仍为 999），缺失的 clearlogo.png 已直接补齐到归档剧根
        arc_root_files = {x["name"]: x["size"] for x in disk.tree[arc_show] if not x["isDirectory"]}
        self.assertEqual(arc_root_files["poster.jpg"], 999)
        self.assertEqual(arc_root_files["clearlogo.png"], 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
