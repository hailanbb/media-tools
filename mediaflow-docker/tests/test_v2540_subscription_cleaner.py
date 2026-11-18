"""v2.5.40 电影与剧集订阅智能清理与双轨治理全矩阵验收测试套件.

覆盖规范：
- 《MediaFlow电影订阅智能清理需求与交互方案-v2.5.40.md》(F01 - F15)
- 《MediaFlow剧集订阅智能清理需求与交互方案-v2.5.39.md》(T01 - T06)
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients.moviepilot import MoviePilotClient
from app.clients.tmdb import TMDBClient
from app.core.subscription_cleaner import (
    diagnose_movie_subscription,
    diagnose_subscription,
    diagnose_tv_subscription,
    evaluate_subscriptions,
)
from app.services.subscription_service import (
    check_queues,
    identity,
    is_movie_sub,
    pause_subscription,
    resource_check,
    resume_subscription,
)


class TestV2540SubscriptionCleaner(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.now = datetime(2026, 11, 18, 12, 0, 0)

    # ── 🎬 电影智能治理验收测试 (F01 - F15) ────────────────────

    def test_F01_movie_2026_completed_can_clean(self):
        """F01: 当年 (2026) 新片若已核实入库完整文件，允许完成退订（打破年份盲目拦截）."""
        sub = {
            "id": 601,
            "name": "海边来信",
            "year": "2026",
            "type": "电影",
            "state": "R",
            "in_library": True,  # 已核实入库
            "date": "2026-05-01 00:00:00",
        }
        diag = diagnose_movie_subscription(sub, now=self.now)
        self.assertTrue(diag.clean_eligible)
        self.assertEqual(diag.clean_confidence, "high")
        self.assertEqual(diag.clean_rule, "completed")
        self.assertEqual(diag.media_type, "movie")
        self.assertIn("满足收藏目标", diag.clean_reason)

    def test_F02_movie_p_state_without_library_cannot_clean(self):
        """F02: P 状态老片没有可用入库证据时，不能直接当作已完成退订; R 状态已入库新片可完成."""
        # P 状态老片但没有入库证据
        old_p_sub = {
            "id": 602,
            "name": "害群之马",
            "year": "1968",
            "type": "电影",
            "state": "P",
            "not_in_library": True,
            "username": "admin",
            "date": "2025-01-01 00:00:00",
        }
        diag_old = diagnose_movie_subscription(old_p_sub, now=self.now)
        self.assertNotEqual(diag_old.clean_rule, "completed")
        self.assertEqual(diag_old.clean_confidence, "low")  # 手动添加项保留意图

        # R 状态新片已有入库实物证据
        new_r_sub = {
            "id": 603,
            "name": "热映佳作",
            "year": "2026",
            "type": "电影",
            "state": "R",
            "in_library": True,
            "date": "2026-09-01 00:00:00",
        }
        diag_new = diagnose_movie_subscription(new_r_sub, now=self.now)
        self.assertTrue(diag_new.clean_eligible)
        self.assertEqual(diag_new.clean_confidence, "high")

    def test_F03_movie_old_zombie_stalled_recommendation(self):
        """F03: 往年老片 (<=2024) 且自动榜单来源、添加超 60 天无进展，给出死种退订建议."""
        sub = {
            "id": 7147,
            "name": "疯狂恋爱跑天下",
            "year": "1977",
            "type": "电影",
            "state": "R",
            "username": "豆瓣榜单",
            "date": "2026-08-01 00:00:00",
        }
        diag = diagnose_movie_subscription(sub, now=self.now)
        self.assertTrue(diag.clean_eligible)
        self.assertEqual(diag.clean_confidence, "medium")
        self.assertEqual(diag.clean_rule, "zombie")
        self.assertIn("老片", diag.clean_reason)

    def test_F04_movie_and_tv_type_isolation(self):
        """F04: 相同 TMDB ID 时电影与剧集类型严格隔离，电影季号为 None，不关联剧集 SeasonRecord."""
        movie_sub = {"id": 801, "name": "沙丘", "tmdbid": 438631, "type": "电影"}
        tv_sub = {"id": 802, "name": "沙丘：预言", "tmdbid": 438631, "type": "电视剧", "season": 1}

        self.assertTrue(is_movie_sub(movie_sub))
        self.assertFalse(is_movie_sub(tv_sub))

        m_tid, m_season = identity(movie_sub)
        t_tid, t_season = identity(tv_sub)

        self.assertEqual(m_tid, "438631")
        self.assertIsNone(m_season)  # 电影季号严格为 None
        self.assertEqual(t_tid, "438631")
        self.assertEqual(t_season, 1)

    def test_F07_movie_theatrical_window_protection(self):
        """F07: 院线上映中或数字版待发售的电影绝对保护，不按院线时长自动退订."""
        # 院线上映中，数字版待公布
        sub_theatrical = {
            "id": 803,
            "name": "冬日档案",
            "year": "2026",
            "type": "电影",
            "state": "R",
            "release_stage": "in_theaters",
            "date": "2026-06-01 00:00:00",
        }
        diag1 = diagnose_movie_subscription(sub_theatrical, now=self.now)
        self.assertFalse(diag1.clean_eligible)
        self.assertEqual(diag1.clean_confidence, "low")
        self.assertEqual(diag1.clean_rule, "upcoming_protect")
        self.assertIn("院线上映已确认", diag1.clean_reason)

        # 数字版定档待发售
        sub_digital = {
            "id": 804,
            "name": "未来战纪",
            "year": "2026",
            "type": "电影",
            "state": "R",
            "release_stage": "digital_pending",
            "digital_date": "2026-12-25",
            "date": "2026-06-01 00:00:00",
        }
        diag2 = diagnose_movie_subscription(sub_digital, now=self.now)
        self.assertFalse(diag2.clean_eligible)
        self.assertIn("数字版已定档待发售", diag2.clean_reason)

    def test_F08_movie_target_version_pending_protection(self):
        """F08: 用户要求 4K 原盘但库中只有 1080p 时，标记目标版本待满足，继续保留."""
        sub = {
            "id": 805,
            "name": "银翼杀手2049",
            "year": "2017",
            "type": "电影",
            "state": "R",
            "in_library": True,
            "has_target_version": False,  # 目标版本未满足
            "date": "2026-01-01 00:00:00",
        }
        diag = diagnose_movie_subscription(sub, now=self.now)
        self.assertFalse(diag.clean_eligible)
        self.assertEqual(diag.clean_rule, "target_pending")
        self.assertIn("目标版本", diag.clean_rule_name)

    def test_F09_movie_wash_and_keyword_whitelist_protection(self):
        """F09: 洗版监控开启或命中收藏关键字时，绝对锁定保护."""
        # 洗版
        sub_wash = {"id": 806, "name": "星际穿越", "type": "电影", "best_version": 1}
        diag_wash = diagnose_movie_subscription(sub_wash, now=self.now)
        self.assertFalse(diag_wash.clean_eligible)
        self.assertEqual(diag_wash.clean_rule, "protected")
        self.assertIn("洗版", diag_wash.clean_rule_name)

        # 关键字
        sub_kw = {"id": 807, "name": "盗梦空间 [keep收藏]", "type": "电影"}
        diag_kw = diagnose_movie_subscription(sub_kw, now=self.now)
        self.assertFalse(diag_kw.clean_eligible)
        self.assertEqual(diag_kw.clean_rule, "protected")

    def test_F11_movie_manual_addition_preserves_intent(self):
        """F11: 手动添加的老片，默认保留用户收藏意图，不自动判死种."""
        sub_manual = {
            "id": 808,
            "name": "经典老片",
            "year": "1990",
            "type": "电影",
            "state": "R",
            "username": "admin",  # 手动添加
            "date": "2026-01-01 00:00:00",
        }
        diag = diagnose_movie_subscription(sub_manual, now=self.now)
        self.assertFalse(diag.clean_eligible)
        self.assertEqual(diag.clean_rule, "manual_preserve")
        self.assertIn("保留收藏意图", diag.clean_reason)

    # ── 📺 剧集智能治理验收测试 (T01 - T06) ────────────────────

    def test_T01_tv_without_douban_id_valid(self):
        """T01: 剧集只有 TMDB ID 且豆瓣 ID 为空时，正常识别与诊断，不误判无效."""
        sub = {
            "id": 901,
            "name": "火线",
            "year": "2002",
            "type": "电视剧",
            "tmdbid": 1438,
            "douban_id": None,
            "season": 1,
            "total_episode": 13,
            "completed_episode": 13,
            "state": "P",
        }
        diag = diagnose_tv_subscription(sub, now=self.now)
        self.assertTrue(diag.clean_eligible)
        self.assertEqual(diag.clean_rule, "completed")

    def test_T02_tv_airing_protect_despite_full_count(self):
        """T02: 剧集即便当前已播集齐备，若官方排播证实仍处于连载中，绝对锁定保护."""
        sub = {
            "id": 902,
            "name": "在播热剧",
            "type": "电视剧",
            "season": 1,
            "total_episode": 10,
            "completed_episode": 10,
            "state": "R",
            "is_airing": True,  # 仍在排播连载中
        }
        diag = diagnose_tv_subscription(sub, now=self.now)
        self.assertFalse(diag.clean_eligible)
        self.assertEqual(diag.clean_confidence, "low")
        self.assertEqual(diag.clean_rule, "airing_protect")
        self.assertIn("连载在播保护", diag.clean_rule_name)

    def test_T03_tv_short_drama_stalled(self):
        """T03: 短剧虚标总集数 99 且暂停停滞，识别为 short_tv_stalled 建议清理."""
        sub = {
            "id": 903,
            "name": "逆袭短剧",
            "type": "电视剧",
            "season": 1,
            "total_episode": 99,
            "completed_episode": 0,
            "is_short_drama": True,
            "state": "P",
            "date": "2026-01-01 00:00:00",
            "last_update": "2026-05-01 00:00:00",
        }
        diag = diagnose_tv_subscription(sub, now=self.now)
        self.assertTrue(diag.clean_eligible)
        self.assertEqual(diag.clean_rule, "short_tv_stalled")

    def test_T04_tv_mid_stalled(self):
        """T04: 正剧中途断更停滞（部分集数入库但长期无更新），识别为 stalled."""
        sub = {
            "id": 904,
            "name": "断更大剧",
            "type": "电视剧",
            "season": 1,
            "total_episode": 30,
            "completed_episode": 10,
            "lack_episode": 20,
            "state": "P",
            "date": "2026-01-01 00:00:00",
            "last_update": "2026-04-01 00:00:00",
        }
        diag = diagnose_tv_subscription(sub, now=self.now)
        self.assertTrue(diag.clean_eligible)
        self.assertEqual(diag.clean_rule, "stalled")

    # ── 🌐 客户端与服务隔离测试 ─────────────────────────────────

    async def test_mp_search_movie_does_not_append_season(self):
        """验证电影检索时绝不拼接 S01，且以 mtype='电影' 检索."""
        mp = MoviePilotClient()
        mp._get = AsyncMock(return_value={"success": True, "data": []})

        # TMDB ID 搜索电影
        await mp.search_resources(tmdb_id=12345, title="测试电影", media_type="movie")
        mp._get.assert_called_with(
            "/api/v1/search/media/tmdb:12345",
            mtype="电影",
            title="测试电影",
        )

        # 关键词搜索电影：绝不含 S01
        mp._get.reset_mock()
        await mp.search_resources(title="测试电影", media_type="movie")
        mp._get.assert_called_with(
            "/api/v1/search/title",
            keyword="测试电影",
        )
        await mp.close()

    async def test_mp_search_tv_preserves_season(self):
        """验证电视剧检索保持 season 传递与关键词 Sxx 拼接兼容."""
        mp = MoviePilotClient()
        mp._get = AsyncMock(return_value={"success": True, "data": []})

        await mp.search_resources(tmdb_id=12345, season=2, title="测试剧集", media_type="tv")
        mp._get.assert_called_with(
            "/api/v1/search/media/tmdb:12345",
            mtype="电视剧",
            season=2,
            title="测试剧集",
        )
        await mp.close()

    async def test_queue_check_ignores_seeding_and_unrelated(self):
        """验证做种任务与无关作品下载不阻塞退订检查."""
        mp = MoviePilotClient()
        mp.get_active_downloads = AsyncMock(return_value=[
            {"id": 1, "title": "其他作品下载", "tmdb_id": 999999, "status": "downloading"},
            {"id": 2, "title": "目标电影", "tmdb_id": 100, "status": "seeding"},  # 正常做种中，不阻塞！
        ])
        mp.get_transfer_queue = AsyncMock(return_value=[])

        sub = {"id": 10, "name": "目标电影", "tmdbid": 100, "type": "电影"}
        # 应该顺利通过，不抛出 HTTPException 409
        try:
            await check_queues(mp, sub)
        except Exception as e:
            self.fail(f"正常做种任务不应抛出阻塞异常: {e}")
        await mp.close()

    async def test_tmdb_movie_release_dates_parser(self):
        """验证 TMDB 客户端电影多地区多类型发行日期解析."""
        tmdb = TMDBClient()
        mock_payload = {
            "id": 1063260,
            "results": [
                {
                    "iso_3166_1": "US",
                    "release_dates": [
                        {"type": 3, "release_date": "2026-06-01T00:00:00.000Z", "note": ""},
                        {"type": 4, "release_date": "2026-08-15T00:00:00.000Z", "note": "VOD"},
                    ]
                },
                {
                    "iso_3166_1": "CN",
                    "release_dates": [
                        {"type": 3, "release_date": "2026-06-15T00:00:00.000Z", "note": ""},
                    ]
                }
            ]
        }
        tmdb._request = AsyncMock(return_value=(mock_payload, "ok", None))

        summary, status, err = await tmdb.fetch_movie_release_dates(1063260)
        self.assertEqual(status, "ok")
        self.assertEqual(summary["theatrical_date"], "2026-06-01")
        self.assertEqual(summary["digital_date"], "2026-08-15")
        self.assertIn("US", summary["by_region"])
        self.assertIn("CN", summary["by_region"])
        await tmdb.close()


if __name__ == "__main__":
    unittest.main()
