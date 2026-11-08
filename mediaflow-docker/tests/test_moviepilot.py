"""MoviePilot 智能订阅运维双轨分离治理单元测试用例.

覆盖场景：
1. 电视剧订阅安全判定：
   - 全剧完结且全部入库 (total == current) -> 允许清理 (clean_eligible=True, confidence=high)
   - 活跃追更连载中 (近7天有更新，current < total) -> 绝对保护 (clean_eligible=False, confidence=low)
2. 电影订阅安全判定：
   - 完结已暂停且历史完成 -> 允许清理 (clean_eligible=True, confidence=high)
   - 活跃连载中/待映 -> 绝对拦截 (clean_eligible=False, confidence=low)
3. 媒体类型双轨过滤与统计 (media_type='tv' vs 'movie')。
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding='utf-8')

from app.core.subscription_cleaner import diagnose_subscription, evaluate_subscriptions


class TestMoviePilotCleaner(unittest.TestCase):

    def test_01_tv_completed_safe_clean(self):
        """测试电视剧：全剧完结且集数已齐，允许安全清理."""
        sub = {
            "id": 101,
            "name": "漫长的季节",
            "type": "电视剧",
            "season": 1,
            "total_episode": 12,
            "completed_episode": 12,
            "state": "P",  # Paused (已完结)
            "date": "2024-01-01 00:00:00",
            "last_update": "2024-02-01 00:00:00",
        }
        item = diagnose_subscription(sub)
        self.assertTrue(item.clean_eligible)
        self.assertEqual(item.media_type, "tv")
        self.assertEqual(item.clean_confidence, "high")
        self.assertIn("完结", item.clean_rule_name)

    def test_02_tv_airing_protected(self):
        """测试电视剧：活跃连载追更中 (近3天有新集活动，8/24集)，坚决保护不清理."""
        now = datetime.now()
        recent_date = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        sub = {
            "id": 102,
            "name": "连载大剧",
            "type": "电视剧",
            "season": 1,
            "total_episode": 24,
            "completed_episode": 8,
            "state": "R",  # Running
            "date": recent_date,
            "last_update": recent_date,
        }
        item = diagnose_subscription(sub, now=now)
        self.assertFalse(item.clean_eligible)
        self.assertEqual(item.clean_confidence, "low")
        self.assertIn("连载", item.clean_reason)

    def test_03_movie_completed_safe_clean(self):
        """测试电影：已完结已暂停且历史完成，允许安全清理."""
        sub = {
            "id": 201,
            "name": "沙丘2 (Dune: Part Two)",
            "type": "电影",
            "state": "P",
            "date": "2024-03-01 00:00:00",
            "last_update": "2024-04-01 00:00:00",
        }
        item = diagnose_subscription(sub)
        self.assertTrue(item.clean_eligible)
        self.assertEqual(item.media_type, "movie")
        self.assertEqual(item.clean_confidence, "high")

    def test_04_movie_downloading_protected(self):
        """测试电影：新创建仍在下载中 (状态 R)，坚决拦截不清理."""
        now = datetime.now()
        recent_date = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        sub = {
            "id": 202,
            "name": "阿凡达3",
            "type": "电影",
            "state": "R",
            "date": recent_date,
            "last_update": recent_date,
        }
        item = diagnose_subscription(sub, now=now)
        self.assertFalse(item.clean_eligible)
        self.assertEqual(item.clean_confidence, "low")

    def test_05_evaluate_subscriptions_dual_track(self):
        """测试批量评估与双轨分离统计."""
        subs = [
            {"id": 1, "name": "剧A (完结)", "type": "电视剧", "season": 1, "total_episode": 10, "completed_episode": 10, "state": "P", "last_update": "2024-01-01 00:00:00"},
            {"id": 2, "name": "剧B (连载)", "type": "电视剧", "season": 1, "total_episode": 20, "completed_episode": 5, "state": "R", "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            {"id": 3, "name": "影C (完结)", "type": "电影", "state": "P", "last_update": "2024-01-01 00:00:00"},
            {"id": 4, "name": "影D (下载中)", "type": "电影", "state": "R", "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        ]
        res = evaluate_subscriptions(subs)
        self.assertEqual(res["overall"]["total"], 4)
        self.assertEqual(res["overall"]["clean_eligible"], 2)  # 剧A 和 影C
        self.assertEqual(res["tv"]["total"], 2)
        self.assertEqual(res["movie"]["total"], 2)




from unittest.mock import AsyncMock, MagicMock, patch
from app.clients.moviepilot import MoviePilotClient, parse_season_str, parse_episode_str


class TestMoviePilotTransferHistory(unittest.TestCase):
    """MoviePilot 媒体整理（转移历史）解析与安全清理单元测试。"""

    def test_01_parse_season_str(self):
        self.assertEqual(parse_season_str("S01"), {1})
        self.assertEqual(parse_season_str("S01,S02"), {1, 2})
        self.assertEqual(parse_season_str("1"), {1})
        self.assertEqual(parse_season_str(""), set())
        self.assertEqual(parse_season_str(None), set())

    def test_02_parse_episode_str(self):
        self.assertEqual(parse_episode_str("E15"), {15})
        self.assertEqual(parse_episode_str("E01-E04"), {1, 2, 3, 4})
        self.assertEqual(parse_episode_str("第1-3集"), {1, 2, 3})
        self.assertEqual(parse_episode_str("14"), {14})
        self.assertEqual(parse_episode_str(""), set())
        self.assertEqual(parse_episode_str(None), set())

    def test_03_find_matching_transfer_history_season_pack(self):
        """测试整季补缺：应匹配出该季所有的历史转移记录以防跳过整理。"""
        import asyncio

        mock_records = [
            {"id": 1, "title": "香港探秘地图", "seasons": "S01", "episodes": "E01"},
            {"id": 2, "title": "香港探秘地图", "seasons": "S01", "episodes": "E02"},
            {"id": 3, "title": "香港探秘地图", "seasons": "S02", "episodes": "E01"},
        ]

        async def run():
            mp = MoviePilotClient()
            mp.get_all_transfer_history_for_title = AsyncMock(return_value=mock_records)
            # 整季匹配 (episodes=None)
            matched = await mp.find_transfer_history("香港探秘地图", season=1, episodes=None)
            self.assertEqual(len(matched), 2)
            self.assertEqual([m["id"] for m in matched], [1, 2])
            await mp.close()

        asyncio.run(run())

    def test_04_find_matching_transfer_history_single_episode(self):
        """测试部分集补缺：仅匹配重叠集号的历史记录，无关集数不匹配。"""
        import asyncio

        mock_records = [
            {"id": 1, "title": "香港探秘地图", "seasons": "S01", "episodes": "E14"},
            {"id": 2, "title": "香港探秘地图", "seasons": "S01", "episodes": "E15"},
            {"id": 3, "title": "香港探秘地图", "seasons": "S01", "episodes": "E01-E10"},
        ]

        async def run():
            mp = MoviePilotClient()
            mp.get_all_transfer_history_for_title = AsyncMock(return_value=mock_records)
            # 仅补 E14
            matched = await mp.find_transfer_history("香港探秘地图", season=1, episodes={14})
            self.assertEqual(len(matched), 1)
            self.assertEqual(matched[0]["id"], 1)

            # 补 E05 和 E15
            matched2 = await mp.find_transfer_history("香港探秘地图", season=1, episodes={5, 15})
            self.assertEqual(len(matched2), 2)
            self.assertEqual([m["id"] for m in matched2], [2, 3])
            await mp.close()

        asyncio.run(run())

    def test_05_delete_transfer_history_strict_safety_invariants(self):
        """测试仅删除转移记录的安全红线：deletesrc 和 deletedest 必须绝对为 False！"""
        import asyncio

        async def run():
            mp = MoviePilotClient()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"success": True}
            mock_resp.raise_for_status = MagicMock()

            captured_params = {}

            async def mock_request(method, url, params=None, json=None, headers=None):
                nonlocal captured_params
                captured_params = params
                return mock_resp

            mp._client.request = AsyncMock(side_effect=mock_request)

            ok = await mp.delete_transfer_history(8888, delete_src=False, delete_dest=False)
            self.assertTrue(ok)
            self.assertIn("deletesrc", captured_params)
            self.assertIn("deletedest", captured_params)
            # 绝对安全不变量验证：绝不能为 True
            self.assertFalse(captured_params["deletesrc"], "安全红线违背：deletesrc 必须为 False！")
            self.assertFalse(captured_params["deletedest"], "安全红线违背：deletedest 必须为 False！")

            await mp.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
