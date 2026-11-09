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

    def test_07_filter_non_regular_episodes_and_historical_finish(self):
        """测试 TMDB 混入预告片时被剔除，且历史完结剧（首播距今超半年）正确识别已完结与真实总集数."""
        import asyncio
        from unittest.mock import AsyncMock
        from app.core.inspector import _fetch_seasons_by_tmdb_id

        mock_mp = AsyncMock()
        # 模拟 TMDB 返回包含预告片（如山河新疆 S01，TMDB 宣称 4 集，但第 4 集是预告片且无 air_date）
        mock_mp.get_tmdb_seasons.return_value = [
            {"season_number": 1, "episode_count": 4, "air_date": "2020-03-24"}
        ]
        mock_mp.get_tmdb_episodes.return_value = [
            {"episode_number": 1, "name": "天山", "air_date": "2020-03-24"},
            {"episode_number": 2, "name": "昆仑山", "air_date": "2020-03-31"},
            {"episode_number": 3, "name": "阿尔泰山", "air_date": "2020-04-07"},
            {"episode_number": 4, "name": "预告片 - 走进探秘《山河新疆》", "air_date": None},
        ]

        async def run():
            data = await _fetch_seasons_by_tmdb_id(mock_mp, 201303, None)
            self.assertIn(1, data)
            # 必须成功过滤掉第 4 集预告片，真实总集数为 3
            self.assertEqual(data[1]["total"], 3)
            # 首播于 2020 年且 3 集均已播出，必须判定为已完结 True，绝不能误判为连载追更中
            self.assertTrue(data[1]["finished"])

        asyncio.run(run())

    def test_08_auto_recalibrate_season_on_overflow(self):
        """测试超范围异常自愈校准：本地 18 集但元数据误判为 10 集时，自动校准为 18 集可归档."""
        import asyncio
        from unittest.mock import AsyncMock
        from app.core.inspector import auto_recalibrate_season

        mock_tmdb = AsyncMock()
        mock_mp = AsyncMock()

        # 模拟 TMDB 搜索返回 2 个候选：渔村篇 (2015, S2=10集) 与 旌善篇 (2014, S2=18集)
        mock_tmdb.search_tv.return_value = [
            {"id": 68157, "name": "三时三餐 渔村篇", "first_air_date": "2015-01-23"},
            {"id": 68156, "name": "三时三餐 旌善篇", "first_air_date": "2014-10-17"},
        ]

        async def get_tv_detail_mock(tv_id):
            if tv_id == 68157:
                return {"id": 68157, "name": "三时三餐 渔村篇", "seasons": [{"season_number": 2, "episode_count": 10, "air_date": "2015-10-09"}]}
            elif tv_id == 68156:
                return {"id": 68156, "name": "三时三餐 旌善篇", "seasons": [{"season_number": 2, "episode_count": 18, "air_date": "2015-05-15"}]}
            return {}

        mock_tmdb.get_tv_detail.side_effect = get_tv_detail_mock

        async def run():
            # 本地实测 18 集，原元数据为 10 集 (触发超范围异常)
            new_tot, is_fin, new_tid, src = await auto_recalibrate_season(
                show_name="三时三餐 (2014) [tmdbid=211502]",
                season_num=2,
                recognized=list(range(1, 19)),
                current_total=10,
                current_tmdb_id=211502,
                tmdb_client=mock_tmdb,
                mp_client=mock_mp,
            )
            # 必须精准选拔出 18 集的 68156
            self.assertEqual(new_tot, 18)
            self.assertEqual(new_tid, 68156)
            self.assertTrue(is_fin)
            self.assertIn("68156", src)

            # 诊断应直接变为 archive_ready
            diag_status, diag_detail = diagnose_season(
                recognized=list(range(1, 19)),
                total_episodes=new_tot,
                season_finished=is_fin,
                has_duplicates=False,
            )
            self.assertEqual(diag_status, "archive_ready")
            self.assertIn("可归档", diag_detail)

        asyncio.run(run())

    def test_09_resolve_show_metadata_candidate_scoring(self):
        """测试元数据解析引擎的候选打分竞选：年份吻合的条目必须优先胜出."""
        import asyncio
        from unittest.mock import AsyncMock
        from app.core.inspector import _resolve_show_metadata

        mock_tmdb = AsyncMock()
        mock_mp = AsyncMock()

        # 模拟 MP 对无效 ID 211502 返回空
        mock_mp.get_tmdb_seasons.return_value = []

        # 模拟 TMDB 返回候选（2015 年条目排在前面，2014 年条目排在后面）
        mock_tmdb.search_tv.return_value = [
            {"id": 68157, "name": "三时三餐 渔村篇", "first_air_date": "2015-01-23"},
            {"id": 68156, "name": "三时三餐 旌善篇", "first_air_date": "2014-10-17"},
        ]

        async def get_tv_detail_mock(tv_id):
            if tv_id == 68157:
                return {"id": 68157, "name": "三时三餐 渔村篇", "seasons": [{"season_number": 1, "episode_count": 9, "air_date": "2015-01-23"}]}
            elif tv_id == 68156:
                return {"id": 68156, "name": "三时三餐 旌善篇", "seasons": [{"season_number": 1, "episode_count": 11, "air_date": "2014-10-17"}]}
            return {}

        mock_tmdb.get_tv_detail.side_effect = get_tv_detail_mock

        async def run():
            resolved_id, seasons = await _resolve_show_metadata(
                show_name="三时三餐 (2014) [tmdbid=211502]",
                needed_seasons=[1],
                tmdb_id_num=211502,
                mp_client=mock_mp,
                tmdb_client=mock_tmdb,
            )
            # 因为 2014 年份匹配，68156 得分更高，必须优先被选中
            self.assertEqual(resolved_id, 68156)
            self.assertEqual(seasons[1]["total"], 11)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
