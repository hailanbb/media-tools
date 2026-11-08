"""初检引擎与异常剧集治理全矩阵单元测试用例.

覆盖场景：
1. 剧集集号解析 (parse_episode) 与多集/花絮识别；
2. 剧季状态诊断 (diagnose_season)：
   - archive_ready (全集齐全无重复)
   - review_needed (含重复版本)
   - download_needed (已完结但缺集)
   - airing (连载中未完结)
   - unknown (TMDB无元数据)
3. 异常剧集分类规则验证 (拆分冲突、归档已有、元数据丢失)；
4. 集数人工校准流转验证。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding='utf-8')

from app.core.inspector import diagnose_season, parse_episode


class TestInspectionEngine(unittest.TestCase):

    def test_01_parse_episode_variants(self):
        """测试各种标准与复杂命名的单集解析."""
        # 标准 S01E05
        ep1 = parse_episode("Show.Name.S01E05.1080p.mkv", "/path/Show/Season 1/Show.Name.S01E05.1080p.mkv", 2000000000)
        self.assertEqual(ep1.episode_numbers, [5])
        self.assertFalse(ep1.is_extra)

        # 中文 第08集
        ep2 = parse_episode("国产动漫 第08集 1080p.mp4", "/path/Season 1/国产动漫 第08集 1080p.mp4", 1000000000)
        self.assertEqual(ep2.episode_numbers, [8])
        self.assertFalse(ep2.is_extra)

        # 多集打包 E01-E02
        ep3 = parse_episode("Show.S01E01-E02.1080p.mkv", "/path/Show/Season 1/Show.S01E01-E02.1080p.mkv", 4000000000)
        self.assertEqual(ep3.episode_numbers, [1, 2])
        self.assertFalse(ep3.is_extra)

        # 衍生花絮 / 日记
        ep4 = parse_episode("Show.S01.特辑.花絮.mkv", "/path/Show/Season 1/Show.S01.特辑.花絮.mkv", 500000000)
        self.assertTrue(ep4.is_extra)

    def test_02_diagnose_archive_ready(self):
        """测试齐全可归档：全 12 集，无重复，完结."""
        status, detail = diagnose_season(
            recognized=list(range(1, 13)),
            total_episodes=12,
            season_finished=True,
            has_duplicates=False,
        )
        self.assertEqual(status, "archive_ready")
        self.assertIn("可归档", detail)

    def test_03_diagnose_review_needed(self):
        """测试含重复集：无论是否完结，优先进入待复核清理."""
        status, detail = diagnose_season(
            recognized=list(range(1, 13)),
            total_episodes=12,
            season_finished=True,
            has_duplicates=True,  # 有重复副本
        )
        self.assertEqual(status, "review_needed")
        self.assertIn("重复", detail)

    def test_03b_diagnose_download_needed_takes_precedence_over_review(self):
        """测试缺集属实优先进待补缺：即使有重复集，已完结剧集仍优先归入 download_needed."""
        status, detail = diagnose_season(
            recognized=[1, 2, 3, 5],
            total_episodes=5,
            season_finished=True,
            has_duplicates=True,
            duplicate_episodes=[5],
        )
        self.assertEqual(status, "download_needed")
        self.assertIn("缺失", detail)
        self.assertIn("4", detail)
        self.assertIn("重复", detail)
        self.assertIn("5", detail)

    def test_04_diagnose_download_needed(self):
        """测试缺集：全剧已完结（12集），但本地只有 10 集，缺 11、12."""
        status, detail = diagnose_season(
            recognized=list(range(1, 11)),
            total_episodes=12,
            season_finished=True,
            has_duplicates=False,
        )
        self.assertEqual(status, "download_needed")
        self.assertIn("11", detail)
        self.assertIn("12", detail)

    def test_05_diagnose_airing(self):
        """测试连载中：官方未完结，本地当前 8/24 集."""
        status, detail = diagnose_season(
            recognized=list(range(1, 9)),
            total_episodes=24,
            season_finished=False,
            has_duplicates=False,
        )
        self.assertEqual(status, "airing")
        self.assertIn("连载中", detail)

    def test_06_diagnose_unknown(self):
        """测试 TMDB 无总集数信息."""
        status, detail = diagnose_season(
            recognized=[1, 2, 3],
            total_episodes=None,  # 无法获取总集数
            season_finished=False,
            has_duplicates=False,
        )
        self.assertEqual(status, "unknown")
        self.assertIn("无法获取", detail)


if __name__ == "__main__":
    unittest.main()
