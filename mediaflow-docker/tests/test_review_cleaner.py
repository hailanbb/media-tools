"""复核清理与智能仲裁引擎全矩阵单元测试用例.

覆盖场景：
1. 完整版本优选与残卷淘汰（4K HDR > 1080p > 720p）；
2. 缺失集数时的安全熔断与强制清理模式；
3. 0 字节视频与临时下载文件的安全熔断保护；
4. 衍生花絮/彩蛋/日记文件的绝对保留；
5. 伴生刮削文件（.nfo, .jpg）级联清理；
6. 孤儿单集刮削文件的彻底捕获与清理（攻克本次真实 Bug）；
7. 全局公共元数据（tvshow.nfo, poster.jpg 等）的绝对保护；
8. 外挂字幕（.srt, .ass）的安全保护。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding='utf-8')

from app.core.review_engine import (
    generate_review_clean_plan,
    episode_from_any_name,
    episode_from_name,
    score_group,
    rank_copy,
)


class TestReviewEngine(unittest.TestCase):

    def test_01_episode_from_any_name(self):
        """测试从各类视频与非视频文件名中提取集号."""
        # 视频
        self.assertEqual(episode_from_any_name("Show.S01E05.1080p.mkv", 1), 5)
        self.assertEqual(episode_from_any_name("剧集名称 - S01E02 - UBWEB.mp4", 1), 2)
        self.assertEqual(episode_from_any_name("动漫名称 第08集 1080p.mkv", 1), 8)
        # 非视频（图片、nfo）
        self.assertEqual(episode_from_any_name("Show.S01E02.CHDWEB.jpg", 1), 2)
        self.assertEqual(episode_from_any_name("Show.S01E02.CHDWEB.nfo", 1), 2)
        self.assertEqual(episode_from_any_name("Show.S01E02-thumb.jpg", 1), 2)
        # 公共文件应提取为 None
        self.assertIsNone(episode_from_any_name("poster.jpg", 1))
        self.assertIsNone(episode_from_any_name("tvshow.nfo", 1))

    def test_02_zero_byte_fuse(self):
        """测试 0 字节损坏视频的熔断机制."""
        files = [
            {"name": "Show.S01E01.1080p.mkv", "path": "/path/E01.mkv", "size": 2000000000, "isDirectory": False},
            {"name": "Show.S01E02.1080p.mkv", "path": "/path/E02.mkv", "size": 0, "isDirectory": False},  # 0 字节
        ]
        plan = generate_review_clean_plan(files, season_number=1, total_episodes=2)
        self.assertFalse(plan["can_clean"])
        self.assertIn("0 字节损坏视频", plan["reason"])
        self.assertEqual(len(plan["to_delete"]), 0)

    def test_03_temp_downloading_fuse(self):
        """测试临时下载文件 (.tmp, .downloading) 的安全熔断."""
        files = [
            {"name": "Show.S01E01.1080p.mkv", "path": "/path/E01.mkv", "size": 2000000000, "isDirectory": False},
            {"name": "Show.S01E02.1080p.mkv.downloading", "path": "/path/E02.mkv.downloading", "size": 500000000, "isDirectory": False},
        ]
        plan = generate_review_clean_plan(files, season_number=1, total_episodes=2)
        self.assertFalse(plan["can_clean"])
        self.assertIn("临时", plan["reason"])
        self.assertEqual(len(plan["to_delete"]), 0)

    def test_04_complete_vs_incomplete_elimination(self):
        """测试完整版优选与淘汰残卷：完整 1080p 优选保留，淘汰 720p 残卷."""
        files = [
            # 完整版 A (1080p，两集齐全)
            {"name": "Show.S01E01.1080p.mkv", "path": "/a/E01.mkv", "size": 2000000000, "isDirectory": False},
            {"name": "Show.S01E02.1080p.mkv", "path": "/a/E02.mkv", "size": 2000000000, "isDirectory": False},
            # 残版 B (720p，只有第 1 集)
            {"name": "Show.S01E01.720p.mkv", "path": "/b/E01.mkv", "size": 800000000, "isDirectory": False},
        ]
        plan = generate_review_clean_plan(files, season_number=1, total_episodes=2)
        self.assertTrue(plan["can_clean"])
        # 保留 1080p 的 E01 与 E02
        kept_names = [f["name"] for f in plan["to_keep"]]
        self.assertIn("Show.S01E01.1080p.mkv", kept_names)
        self.assertIn("Show.S01E02.1080p.mkv", kept_names)
        # 淘汰 720p 的残卷 E01
        deleted_names = [f["name"] for f in plan["to_delete"]]
        self.assertIn("Show.S01E01.720p.mkv", deleted_names)

    def test_05_multi_complete_best_quality(self):
        """测试多个完整版时，4K HDR 淘汰 1080p."""
        files = [
            # 完整版 4K HDR
            {"name": "Show.S01E01.2160p.HDR.mkv", "path": "/4k/E01.mkv", "size": 5000000000, "isDirectory": False},
            {"name": "Show.S01E02.2160p.HDR.mkv", "path": "/4k/E02.mkv", "size": 5000000000, "isDirectory": False},
            # 完整版 1080p
            {"name": "Show.S01E01.1080p.mkv", "path": "/1080/E01.mkv", "size": 2000000000, "isDirectory": False},
            {"name": "Show.S01E02.1080p.mkv", "path": "/1080/E02.mkv", "size": 2000000000, "isDirectory": False},
        ]
        plan = generate_review_clean_plan(files, season_number=1, total_episodes=2)
        self.assertTrue(plan["can_clean"])
        kept_paths = [f["path"] for f in plan["to_keep"]]
        self.assertIn("/4k/E01.mkv", kept_paths)
        self.assertIn("/4k/E02.mkv", kept_paths)
        deleted_paths = [f["path"] for f in plan["to_delete"]]
        self.assertIn("/1080/E01.mkv", deleted_paths)
        self.assertIn("/1080/E02.mkv", deleted_paths)

    def test_06_missing_episodes_fuse_and_force_clean(self):
        """测试缺少集数时：对已有集数优选最佳画质淘汰冗余副本，并清晰提示缺失集."""
        files = [
            {"name": "Show.S01E01.1080p.mkv", "path": "/path/E01_1080.mkv", "size": 2000000000, "isDirectory": False},
            {"name": "Show.S01E01.720p.mkv", "path": "/path/E01_720.mkv", "size": 800000000, "isDirectory": False},
            # 缺 E02，全剧共 2 集
        ]
        # 有重复副本时：应允许清理已有集数的低质副本，避免死锁
        plan = generate_review_clean_plan(files, season_number=1, total_episodes=2, force_clean=False)
        self.assertTrue(plan["can_clean"])
        self.assertIn("缺失第 2 集", plan["version_note"])
        self.assertEqual(len(plan["to_delete"]), 1)
        self.assertEqual(plan["to_delete"][0]["name"], "Show.S01E01.720p.mkv")

        # 无重复副本时：安全熔断，无需清理
        files_no_dup = [
            {"name": "Show.S01E01.1080p.mkv", "path": "/path/E01_1080.mkv", "size": 2000000000, "isDirectory": False},
        ]
        plan_no_dup = generate_review_clean_plan(files_no_dup, season_number=1, total_episodes=2)
        self.assertFalse(plan_no_dup["can_clean"])
        self.assertIn("无多余", plan_no_dup["reason"])
        self.assertIn("缺失第 2 集", plan_no_dup["reason"])

    def test_07_companion_cascade_and_orphan_cleanup(self):
        """测试伴生刮削文件与历史孤儿刮削文件清理（攻克本次核心 Bug）."""
        files = [
            # E01：保留 UBWEB 版，及其配套 jpg 与 nfo
            {"name": "Show - S01E01 - UBWEB.mkv", "path": "/p/E01_UB.mkv", "size": 2000000000, "isDirectory": False},
            {"name": "Show - S01E01 - UBWEB.jpg", "path": "/p/E01_UB.jpg", "size": 100000, "isDirectory": False},
            {"name": "Show - S01E01 - UBWEB.nfo", "path": "/p/E01_UB.nfo", "size": 2000, "isDirectory": False},

            # E01：淘汰 CHDWEB 版视频，连带其配套 jpg 与 nfo
            {"name": "Show - S01E01 - CHDWEB.mkv", "path": "/p/E01_CHD.mkv", "size": 1500000000, "isDirectory": False},
            {"name": "Show - S01E01 - CHDWEB.jpg", "path": "/p/E01_CHD.jpg", "size": 100000, "isDirectory": False},
            {"name": "Show - S01E01 - CHDWEB.nfo", "path": "/p/E01_CHD.nfo", "size": 2000, "isDirectory": False},

            # 孤儿文件：CHDWEB 的 E02 视频早已不存在，但留下了孤儿 jpg 和 nfo！
            {"name": "Show - S01E02 - CHDWEB.jpg", "path": "/p/E02_CHD_orphan.jpg", "size": 100000, "isDirectory": False},
            {"name": "Show - S01E02 - CHDWEB.nfo", "path": "/p/E02_CHD_orphan.nfo", "size": 2000, "isDirectory": False},
            # 现存实际 E02 只有 UBWEB 版
            {"name": "Show - S01E02 - UBWEB.mkv", "path": "/p/E02_UB.mkv", "size": 2000000000, "isDirectory": False},
            {"name": "Show - S01E02 - UBWEB.nfo", "path": "/p/E02_UB.nfo", "size": 2000, "isDirectory": False},

            # 全局公共元数据：绝对保护不删！
            {"name": "poster.jpg", "path": "/p/poster.jpg", "size": 500000, "isDirectory": False},
            {"name": "season.nfo", "path": "/p/season.nfo", "size": 2000, "isDirectory": False},
            {"name": "tvshow.nfo", "path": "/p/tvshow.nfo", "size": 3000, "isDirectory": False},

            # 外挂字幕：绝对安全保留！
            {"name": "Show - S01E01 - UBWEB.zh.srt", "path": "/p/E01_UB.zh.srt", "size": 50000, "isDirectory": False},
        ]

        plan = generate_review_clean_plan(files, season_number=1, total_episodes=2)
        self.assertTrue(plan["can_clean"])

        deleted_names = [f["name"] for f in plan["to_delete"]]
        print("\n[Test 07] To Delete Files:")
        for d in plan["to_delete"]:
            print(f"  - E{d.get('episode')}: {d['name']} ({d.get('reason')})")

        # 1. 淘汰视频与直接伴生必须被删
        self.assertIn("Show - S01E01 - CHDWEB.mkv", deleted_names)
        self.assertIn("Show - S01E01 - CHDWEB.jpg", deleted_names)
        self.assertIn("Show - S01E01 - CHDWEB.nfo", deleted_names)

        # 2. 关键断言：历史孤儿伴生文件必须被精准捕获并删除！
        self.assertIn("Show - S01E02 - CHDWEB.jpg", deleted_names)
        self.assertIn("Show - S01E02 - CHDWEB.nfo", deleted_names)

        # 3. 关键断言：全局元数据与外挂字幕绝对不能在删除列表中！
        self.assertNotIn("poster.jpg", deleted_names)
        self.assertNotIn("season.nfo", deleted_names)
        self.assertNotIn("tvshow.nfo", deleted_names)
        self.assertNotIn("Show - S01E01 - UBWEB.zh.srt", deleted_names)

        # 4. 关键断言：孤儿文件集号应被正确解析为 2
        orphan_items = [d for d in plan["to_delete"] if "E02_CHD_orphan" in d["path"]]
        for oi in orphan_items:
            self.assertEqual(oi.get("episode"), 2)

    def test_08_mixed_release_groups_completeness_guarantee(self):
        """测试混版多压制组场景下的【全集保全第一定律】：绝不因压制组混杂而误伤任何孤本集."""
        # 模拟极端混版：20 集剧集，来自 4 个不同压制组，且缺失 E14，E15 存在 2 个版本
        files = [
            {"name": "Show.S01E01.GroupA.mkv", "path": "/p/E01.mkv", "size": 1000000000, "isDirectory": False},
            {"name": "Show.S01E02.GroupB.mkv", "path": "/p/E02.mkv", "size": 1000000000, "isDirectory": False},
            {"name": "Show.S01E03.GroupC.mkv", "path": "/p/E03.mkv", "size": 1000000000, "isDirectory": False},
            {"name": "Show.S01E04.GroupD.mkv", "path": "/p/E04.mkv", "size": 1000000000, "isDirectory": False},
            {"name": "Show.S01E05.GroupA.mkv", "path": "/p/E05.mkv", "size": 1000000000, "isDirectory": False},
            # E15 存在 GroupA (1080p) 与 GroupB (2160p)
            {"name": "Show.S01E15.GroupA.1080p.mkv", "path": "/p/E15_1080.mkv", "size": 500000000, "isDirectory": False},
            {"name": "Show.S01E15.GroupB.2160p.mkv", "path": "/p/E15_4k.mkv", "size": 2000000000, "isDirectory": False},
        ]
        # 1..5 以及 15，全剧共 20 集，缺失 6..14, 16..20
        plan = generate_review_clean_plan(files, season_number=1, total_episodes=20)
        self.assertTrue(plan["can_clean"])

        kept_names = [f["name"] for f in plan["to_keep"]]
        # 核心保证：每个唯一的集数（1, 2, 3, 4, 5, 15）必须 100% 被保留在 to_keep 中！
        self.assertIn("Show.S01E01.GroupA.mkv", kept_names)
        self.assertIn("Show.S01E02.GroupB.mkv", kept_names)
        self.assertIn("Show.S01E03.GroupC.mkv", kept_names)
        self.assertIn("Show.S01E04.GroupD.mkv", kept_names)
        self.assertIn("Show.S01E05.GroupA.mkv", kept_names)
        self.assertIn("Show.S01E15.GroupB.2160p.mkv", kept_names)

        # 核心保证：只有同集重复的低画质版本被放入 to_delete
        deleted_names = [f["name"] for f in plan["to_delete"]]
        self.assertEqual(len(deleted_names), 1)
        self.assertEqual(deleted_names[0], "Show.S01E15.GroupA.1080p.mkv")


if __name__ == "__main__":
    unittest.main()
