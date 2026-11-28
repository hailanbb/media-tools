"""v2.5.87 回归测试：
1. 同 ID 异名归档目录优先推荐既有归档目录 + 清源遇到错写预期路径按同分类唯一 [tmdbid=ID] 自愈对齐（复现 TMDB 323005）。
2. 剧根及季内刮削器原子写非隐藏临时文件（如 poster.cpt54r）与 0 字节损坏刮削图自动原地清理、不误报未知附件（复现 TMDB 250446）。
"""

from __future__ import annotations

import json
import posixpath
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_v2551_review_closure as f
from test_v2559_source_cleanup import Cloud, folder, item
from app.api import archive
from app.core.auxiliary_inventory import is_ignorable_junk_or_scrape_temp
from app.core.directory_governance import (
    build_directory_group_target_options,
    build_physical_dir_entry,
    confirm_directory_group,
)
from app.models.directory_group import DirectoryGroup


class TestV2587AliasArchiveAndScrapeTemp(unittest.TestCase):
    setUp = f.DatabaseRegression.setUp
    tearDown = f.DatabaseRegression.tearDown

    def test_scrape_temp_file_pattern_precision(self):
        """验证 poster.cpt54r / .tvshow.nfo.6TlXQz 被识别为刮削临时垃圾，而 unknown.bin / poster.mkv 绝不误杀。"""
        self.assertTrue(is_ignorable_junk_or_scrape_temp("poster.cpt54r"))
        self.assertTrue(is_ignorable_junk_or_scrape_temp("fanart.a8k2p9"))
        self.assertTrue(is_ignorable_junk_or_scrape_temp("tvshow.nfo.tmp"))
        self.assertTrue(is_ignorable_junk_or_scrape_temp(".tvshow.nfo.6TlXQz"))
        self.assertTrue(is_ignorable_junk_or_scrape_temp("._poster.jpg"))

        # 安全边界：普通未知二进制、媒体文件、字幕绝不匹配
        self.assertFalse(is_ignorable_junk_or_scrape_temp("unknown.bin"))
        self.assertFalse(is_ignorable_junk_or_scrape_temp("notes.cpt54r"))
        self.assertFalse(is_ignorable_junk_or_scrape_temp("poster.mkv"))
        self.assertFalse(is_ignorable_junk_or_scrape_temp("poster.srt"))
        self.assertFalse(is_ignorable_junk_or_scrape_temp("poster.bin"))

    def test_alias_dir_prefers_existing_archive_and_preserves_already_archived_target(self):
        """复现 TMDB 323005：归档库已存在别名目录时优先推荐既有归档目录，且确认未建目录名时不冲掉已归档季真实路径。"""
        src_show = "/src/电视剧/日本剧集/换座位后，坐在我后面的男生好像喜欢我 (2026) [tmdbid=323005]"
        arc_alias_show = "/arc/电视剧/日本剧集/换座位后，后面的男生好像喜欢我 (2026) [tmdbid=323005]"
        entries = [
            build_physical_dir_entry(
                side="source",
                category="cat:电视剧/日本剧集",
                show_path=src_show,
                tmdb_id="323005",
                seasons=[1],
            ),
            build_physical_dir_entry(
                side="archive",
                category="cat:电视剧/日本剧集",
                show_path=arc_alias_show,
                tmdb_id="323005",
                seasons=[1],
            ),
        ]
        opts = build_directory_group_target_options(
            entries,
            category="cat:电视剧/日本剧集",
            tmdb_id="323005",
            tmdb_meta={"name": "换座位后，坐在我后面的男生好像喜欢我", "first_air_date": "2026-01-01"},
            source_root="/src",
            archive_root="/arc",
        )
        # 核心断言 1：必须优先推荐归档库已存在的真实目录，避免在归档库制造第二个目录或清源报 not found
        self.assertEqual(opts["suggested_target_path"], arc_alias_show)
        self.assertTrue(opts["candidate_targets"][0]["exists_in_archive"])

        # 核心断言 2：即便人工对 already_archived 记录确认了未新建的源端同名目标，也保留 dual_archive_info 中的真实归档季路径
        rec = f.SeasonRecord(
            id="rec_323005",
            task_id="t",
            show_name="换座位后，坐在我后面的男生好像喜欢我 (2026) [tmdbid=323005]",
            show_path=src_show,
            season_path=f"{src_show}/Season 1",
            tmdb_id="323005",
            season_number=1,
            total_episodes=8,
            local_episode_count=8,
            recognized_episodes_json="[1,2,3,4,5,6,7,8]",
            status="already_archived",
            archive_target_path=f"{arc_alias_show}/Season 1",
            season_finished=True,
            evidence_json=json.dumps({
                "dual_archive_info": {"primary_path": f"{arc_alias_show}/Season 1"}
            }),
        )
        grp = DirectoryGroup(
            id="grp_323005",
            task_id="t",
            tmdb_id="323005",
            category_key="cat:电视剧/日本剧集",
            group_kind="alias_dir_confirm",
            title="同 ID 异名目录",
            status="pending_confirm",
            paths_json=json.dumps(entries, ensure_ascii=False),
        )
        self.session.add_all([rec, grp])
        self.session.commit()

        uncreated_target = "/arc/电视剧/日本剧集/换座位后，坐在我后面的男生好像喜欢我 (2026) [tmdbid=323005]"
        f.immediate(confirm_directory_group(self.db, grp, mode="use_archive_target", target_path=uncreated_target))
        self.assertEqual(rec.archive_target_path, f"{arc_alias_show}/Season 1")

    def test_clean_source_redundant_self_heals_stale_expected_target_to_unique_alias_archive(self):
        """复现 TMDB 323005 现网故障：archive_target_path 已被错写为不存在的同名路径时，按同分类唯一 [tmdbid=323005] 自愈完成清源。"""
        src_cat = "/src/电视剧/日本剧集"
        arc_cat = "/arc/电视剧/日本剧集"
        src_show = f"{src_cat}/换座位后，坐在我后面的男生好像喜欢我 (2026) [tmdbid=323005]"
        src_s1 = f"{src_show}/Season 1"
        stale_arc_s1 = f"{arc_cat}/换座位后，坐在我后面的男生好像喜欢我 (2026) [tmdbid=323005]/Season 1"
        real_arc_show = f"{arc_cat}/换座位后，后面的男生好像喜欢我 (2026) [tmdbid=323005]"
        real_arc_s1 = f"{real_arc_show}/Season 1"

        disk = Cloud()
        disk.tree = {
            "/src": [folder("/src/电视剧")],
            "/src/电视剧": [folder(src_cat)],
            src_cat: [folder(src_show)],
            src_show: [folder(src_s1), item(f"{src_show}/poster.jpg", 500)],
            src_s1: [
                item(f"{src_s1}/Show.S01E01.mkv", 1000),
                item(f"{src_s1}/Show.S01E02.mkv", 1000),
            ],
            arc_cat: [folder(real_arc_show)],
            real_arc_show: [folder(real_arc_s1), item(f"{real_arc_show}/poster.jpg", 800)],
            real_arc_s1: [
                item(f"{real_arc_s1}/Alias.S01E01.mkv", 1200),
                item(f"{real_arc_s1}/Alias.S01E02.mkv", 1200),
            ],
        }

        rec = f.SeasonRecord(
            id="s_323005",
            task_id="t",
            show_name="换座位后，坐在我后面的男生好像喜欢我 (2026) [tmdbid=323005]",
            show_path=src_show,
            season_path=src_s1,
            tmdb_id="323005",
            season_number=1,
            total_episodes=2,
            local_episode_count=2,
            recognized_episodes_json="[1, 2]",
            duplicate_episodes_json="[]",
            status="already_archived",
            archive_target_path=stale_arc_s1,
            season_finished=True,
            evidence_json="{}",
        )
        self.session.add(rec)
        self.session.commit()

        async def no_group(*args, **kwargs):
            return None

        settings = SimpleNamespace(source_root="/src", archive_root="/arc")
        with patch.object(archive, "get_settings", return_value=settings), \
                patch.object(archive, "find_blocking_group_for_record", no_group):
            res = f.immediate(archive._clean_source_redundant_impl(
                archive._ArchiveRequest(season_record_ids=["s_323005"]),
                self.db,
                cd2_client=disk,
                skip_summary_refresh=True,
            ))

        self.assertTrue(res.ok, res.message)
        self.assertEqual(rec.status, "cleaned_redundant")
        self.assertEqual(rec.archive_target_path, real_arc_s1)
        self.assertNotIn(src_s1, disk.tree)
        self.assertNotIn(src_show, disk.tree)
        # 归档库若存在 >=2 个同 TMDB 目录时绝不盲目自愈，必须拒绝删源
        disk2 = Cloud()
        disk2.tree = {
            "/src": [folder("/src/电视剧")],
            "/src/电视剧": [folder(src_cat)],
            src_cat: [folder(src_show)],
            src_show: [folder(src_s1)],
            src_s1: [item(f"{src_s1}/Show.S01E01.mkv", 1000), item(f"{src_s1}/Show.S01E02.mkv", 1000)],
            arc_cat: [folder(real_arc_show), folder(f"{arc_cat}/另一别名 [tmdbid=323005]")],
        }
        rec2 = f.SeasonRecord(
            id="s_ambig",
            task_id="t",
            show_name="换座位后，坐在我后面的男生好像喜欢我 (2026) [tmdbid=323005]",
            show_path=src_show,
            season_path=src_s1,
            tmdb_id="323005",
            season_number=1,
            total_episodes=2,
            local_episode_count=2,
            recognized_episodes_json="[1, 2]",
            duplicate_episodes_json="[]",
            status="already_archived",
            archive_target_path=stale_arc_s1,
            season_finished=True,
            evidence_json="{}",
        )
        self.session.add(rec2)
        self.session.commit()
        with patch.object(archive, "get_settings", return_value=settings), \
                patch.object(archive, "find_blocking_group_for_record", no_group):
            res2 = f.immediate(archive._clean_source_redundant_impl(
                archive._ArchiveRequest(season_record_ids=["s_ambig"]),
                self.db,
                cd2_client=disk2,
                skip_summary_refresh=True,
            ))
        self.assertFalse(res2.ok)
        self.assertIn(src_s1, disk2.tree)

    def test_root_shell_cleans_poster_cpt54r_and_skips_zero_byte_broken_scrape_files(self):
        """复现 TMDB 250446《绑架游戏》：源剧根含 poster.cpt54r 及 0B 损坏刮削图时自动原地销毁并移除外层空壳。"""
        src_cat = "/src/电视剧/日本剧集"
        arc_cat = "/arc/电视剧/日本剧集"
        src_show = f"{src_cat}/绑架游戏 (2024) [tmdbid=250446]"
        src_s1 = f"{src_show}/Season 1"
        arc_show = f"{arc_cat}/绑架游戏 (2024) [tmdbid=250446]"
        arc_s1 = f"{arc_show}/Season 1"

        disk = Cloud()
        disk.tree = {
            "/src": [folder("/src/电视剧")],
            "/src/电视剧": [folder(src_cat)],
            src_cat: [folder(src_show)],
            # 模拟实盘：Season 1 已在上一轮删掉，源剧根残留 poster.cpt54r、0B backdrop.jpg 及缺失的 clearlogo.png
            src_show: [
                item(f"{src_show}/backdrop.jpg", 0),
                item(f"{src_show}/poster.cpt54r", 437721),
                item(f"{src_show}/poster.jpg", 598731),
                item(f"{src_show}/clearlogo.png", 12345),
                item(f"{src_show}/tvshow.nfo", 3838),
            ],
            arc_cat: [folder(arc_show)],
            arc_show: [
                folder(arc_s1),
                item(f"{arc_show}/poster.jpg", 600000),
                item(f"{arc_show}/tvshow.nfo", 3838),
            ],
            arc_s1: [item(f"{arc_s1}/Kidnap.S01E01.mkv", 1000)],
        }

        rec = f.SeasonRecord(
            id="s_250446",
            task_id="t",
            show_name="绑架游戏 (2024) [tmdbid=250446]",
            show_path=src_show,
            season_path=src_s1,
            tmdb_id="250446",
            season_number=1,
            total_episodes=1,
            local_episode_count=1,
            recognized_episodes_json="[1]",
            duplicate_episodes_json="[]",
            status="already_archived",
            archive_target_path=arc_s1,
            season_finished=True,
            evidence_json=json.dumps({
                "source_season_cleanup": {
                    "verified": True,
                    "source_path": src_s1,
                    "archive_target": arc_s1,
                }
            }),
        )
        self.session.add(rec)
        self.session.commit()

        async def no_group(*args, **kwargs):
            return None

        settings = SimpleNamespace(source_root="/src", archive_root="/arc")
        with patch.object(archive, "get_settings", return_value=settings), \
                patch.object(archive, "find_blocking_group_for_record", no_group):
            res = f.immediate(archive._clean_source_redundant_impl(
                archive._ArchiveRequest(season_record_ids=["s_250446"]),
                self.db,
                cd2_client=disk,
                skip_summary_refresh=True,
            ))

        self.assertTrue(res.ok, res.message)
        self.assertEqual(rec.status, "cleaned_redundant")
        self.assertIn("剧集外层已移除", rec.status_detail)
        self.assertNotIn(src_show, disk.tree)
        arc_names = {x["name"]: x["size"] for x in disk.tree[arc_show] if not x["isDirectory"]}
        # 有效缺失资产 clearlogo.png 补齐至归档剧根；0 字节损坏 backdrop.jpg 与临时文件 poster.cpt54r 绝不迁入归档库
        self.assertEqual(arc_names.get("clearlogo.png"), 12345)
        self.assertNotIn("backdrop.jpg", arc_names)
        self.assertNotIn("poster.cpt54r", arc_names)
        self.assertEqual(arc_names.get("poster.jpg"), 600000)


if __name__ == "__main__":
    unittest.main()
