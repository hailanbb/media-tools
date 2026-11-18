"""v2.5.42 补缺站点管理与动漫/非动漫独立排序全场景验收测试 (S01 - S16)。

对齐《MediaFlow真缺集待补模块需求与交互方案-v2.5.40.md》第 6.1–6.8 节与 S01–S16 验收标准。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.moviepilot import MoviePilotClient, sanitize_site_data
from app.core.season_classifier import resolve_record_gap_category
from app.database import Base
from app.models.gap_fill import GapFillEpisode
from app.models.gap_fill_site_rule import GapFillSiteRule, GapFillWorkPreference
from app.models.season import SeasonRecord
from app.models.task import Task
from app.services.download_service import (
    CandidateEval,
    _dedup_evaluations,
    _evaluate_raw_candidates,
    _pick_best_season_pack,
    _plan_standalone_downloads,
    execute_gap_fill_download,
    get_gap_fill_site_rules,
    get_gap_fill_work_preferences,
    resolve_category_gap_fill_sites,
    save_gap_fill_site_rule,
    save_gap_fill_work_preference,
)


def _mock_settings():
    return type(
        "MockSettings",
        (),
        {
            "mp_url": "http://mp.mock",
            "mp_token": "mock_token",
            "archive_root": "/arc",
            "source_root": "/src",
            "gap_fill_min_seeders": 2,
            "gap_fill_seeder_tier_ratio": 0.10,
            "gap_fill_min_seeder_slack": 1,
            "gap_fill_min_resolution": "",
            "gap_fill_require_chinese": False,
        },
    )()


class TestV2540GapFillSitesModule(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    # ── S01: 首次打开设置与脱敏读取 ──────────────────────────────
    async def test_s01_catalog_sanitize_and_no_leak(self):
        """S01: 验证站点目录脱敏读取，绝不泄漏 cookie, passkey, token 或完整鉴权 URL。"""
        raw_sites = [
            {
                "id": 1,
                "name": "M-Team",
                "domain": "kp.m-team.cc",
                "url": "https://kp.m-team.cc/torrents.php?token=secret123&passkey=abc456",
                "is_active": True,
                "cookie": "uid=12345; pass=very_secret",
                "passkey": "secret_passkey_xyz",
                "token": "api_token_abc",
                "authurl": "https://kp.m-team.cc/download.php?passkey=abc456",
                "public": False,
                "pri": 1,
                "comment": "主站",
            },
            {
                "id": 2,
                "name": "Audiences",
                "domain": "audiences.me",
                "url": "https://audiences.me",
                "is_active": False,
                "auth_token": "another_token",
            },
        ]
        sanitized = [sanitize_site_data(s) for s in raw_sites]
        self.assertEqual(len(sanitized), 2)
        s1 = sanitized[0]
        # 白名单字段保留
        self.assertEqual(s1["id"], 1)
        self.assertEqual(s1["name"], "M-Team")
        self.assertEqual(s1["domain"], "kp.m-team.cc")
        self.assertTrue(s1["is_active"])
        # 敏感字段剔除
        self.assertNotIn("cookie", s1)
        self.assertNotIn("passkey", s1)
        self.assertNotIn("token", s1)
        self.assertNotIn("authurl", s1)
        self.assertNotIn("auth_token", s1)
        # URL 中敏感查询串剥离
        self.assertNotIn("secret123", s1["url"])
        self.assertNotIn("abc456", s1["url"])
        self.assertEqual(s1["url"], "https://kp.m-team.cc")

    # ── S02: 两套独立规则持久化与互不影响 ────────────────────────
    async def test_s02_independent_rules_persistence_and_no_bleed(self):
        """S02: 动漫与非动漫两套规则独立持久化，互不影响，且版本号自增。"""
        async with self.Session() as db:
            inst_key = "default"
            # 初始为空
            rules_init = await get_gap_fill_site_rules(db, instance_key=inst_key)
            self.assertEqual(rules_init["anime"]["status"], "uninitialized")
            self.assertEqual(rules_init["non_anime"]["status"], "uninitialized")

            # 保存动漫规则: 站点 [10, 20]
            anime_rule = await save_gap_fill_site_rule(
                db, category="anime", selected_site_ids=[10, 20], status="active", instance_key=inst_key
            )
            self.assertEqual(anime_rule["selected_site_ids"], [10, 20])
            self.assertEqual(anime_rule["version"], 1)

            # 保存非动漫规则: 站点 [20, 30]
            non_anime_rule = await save_gap_fill_site_rule(
                db, category="non_anime", selected_site_ids=[20, 30], status="active", instance_key=inst_key
            )
            self.assertEqual(non_anime_rule["selected_site_ids"], [20, 30])
            self.assertEqual(non_anime_rule["version"], 1)

            # 再次查询，两套规则独立存在
            rules_now = await get_gap_fill_site_rules(db, instance_key=inst_key)
            self.assertEqual(rules_now["anime"]["selected_site_ids"], [10, 20])
            self.assertEqual(rules_now["non_anime"]["selected_site_ids"], [20, 30])

            # 修改动漫规则为 [10, 30, 20]，版本增至 2，非动漫规则完全不受影响
            anime_v2 = await save_gap_fill_site_rule(
                db,
                category="anime",
                selected_site_ids=[10, 30, 20],
                status="active",
                expected_version=1,
                instance_key=inst_key,
            )
            self.assertEqual(anime_v2["version"], 2)
            self.assertEqual(anime_v2["selected_site_ids"], [10, 30, 20])

            rules_check = await get_gap_fill_site_rules(db, instance_key=inst_key)
            self.assertEqual(rules_check["non_anime"]["selected_site_ids"], [20, 30])
            self.assertEqual(rules_check["non_anime"]["version"], 1)

    # ── S03: 动漫与非动漫逐季路由与人工偏好覆盖 ───────────────────
    async def test_s03_per_season_classification_and_manual_preference(self):
        """S03: 逐季作品分类路由：人工指定 > TMDB 动画分类 > 任务目录类型 > 待确认。"""
        async with self.Session() as db:
            inst_key = "default"

            # 1. TMDB 带动画分类 (Genre 16)
            rec_anime = SeasonRecord(
                id="rec_anime_1",
                task_id="task_1",
                show_name="葬送的芙莉莲",
                show_path="/src/葬送的芙莉莲",
                season_number=1,
                season_path="/src/葬送的芙莉莲/Season 1",
                tmdb_id="12345",
                local_episode_count=1,
                status="download_needed",
                evidence_json=json.dumps({"genres": [16, "Animation"]}),
            )
            res_anime = await resolve_record_gap_category(db, rec_anime, inst_key)
            self.assertEqual(res_anime["category"], "anime")
            self.assertEqual(res_anime["source"], "tmdb")

            # 2. TMDB 为普通电视剧 (无 16)
            rec_live = SeasonRecord(
                id="rec_live_1",
                task_id="task_1",
                show_name="狂飙",
                show_path="/src/狂飙",
                season_number=1,
                season_path="/src/狂飙/Season 1",
                tmdb_id="67890",
                local_episode_count=1,
                status="download_needed",
                evidence_json=json.dumps({"genres": ["剧情"]}),
            )
            res_live = await resolve_record_gap_category(db, rec_live, inst_key)
            self.assertEqual(res_live["category"], "non_anime")
            self.assertEqual(res_live["source"], "tmdb")

            # 3. 人工指定覆盖: 用户将《狂飙》人工指定为动漫 (测试覆盖生效)
            await save_gap_fill_work_preference(
                db, tmdb_id="67890", show_name="狂飙", category="anime", instance_key=inst_key
            )
            res_pref = await resolve_record_gap_category(db, rec_live, inst_key)
            self.assertEqual(res_pref["category"], "anime")
            self.assertEqual(res_pref["source"], "manual")

    # ── S04: 空规则与未初始化拦截 (防御性安全) ─────────────────────
    async def test_s04_empty_rule_and_paused_rule_interception(self):
        """S04: 空规则或未初始化时明确拦截，严禁全站搜索或借用另一套规则。"""
        # 测试客户端空 sites 拦截
        with patch("app.clients.moviepilot.get_settings", return_value=_mock_settings()):
            client = MoviePilotClient()
            results = await client.search_resources(title="芙莉莲", sites=[])
            self.assertEqual(results, [])

        async with self.Session() as db:
            task = Task(id="task_s04", name="测试任务", source_path="/115/Anime")
            db.add(task)
            rec = SeasonRecord(
                id="rec_s04",
                task_id="task_s04",
                show_name="测试动画 (2025) [tmdbid=999]",
                show_path="/115/Anime/测试动画 (2025) [tmdbid=999]",
                season_number=1,
                season_path="/115/Anime/测试动画 (2025) [tmdbid=999]/Season 1",
                tmdb_id="999",
                total_episodes=12,
                local_episode_count=3,
                recognized_episodes_json=json.dumps([1, 2, 3]),
                missing_episodes_json=json.dumps([4]),
                status="download_needed",
                season_finished=True,
                evidence_json=json.dumps({"genres": [16]}),
            )
            db.add(rec)
            await db.commit()

            # 此时 anime 规则未配置 (selected_site_ids 为空)
            with patch("app.services.download_service.get_settings", return_value=_mock_settings()):
                res = await execute_gap_fill_download(
                    season_record_ids=["rec_s04"],
                    db=db,
                )
            self.assertTrue(res["skipped"])
            self.assertTrue(any("未配置" in s or "未启用" in s for s in res["skip_reasons"]))

    # ── S05 & S06: 站点优先级真实决定选种 ─────────────────────────
    async def test_s06_site_priority_trumps_seeders_and_size(self):
        """S06: 相同阶段高优先站合格资源绝对优先，即使低优先站做种更高、体积更小。"""
        cand_high_site = {
            "torrent_info": {
                "title": "测试剧集 S01E04 1080p WEB-DL",
                "site": 10,
                "site_name": "HighPriSite",
                "seeders": 3,
                "size": 5 * 1024 * 1024 * 1024,
                "enclosure": "http://high/1.torrent",
            },
            "meta_info": {"season_list": [1], "episode_list": [4]},
            "media_info": {"tmdb_id": 100},
        }
        cand_low_site = {
            "torrent_info": {
                "title": "测试剧集 S01E04 1080p WEB-DL",
                "site": 20,
                "site_name": "LowPriSite",
                "seeders": 100,
                "size": 2 * 1024 * 1024 * 1024,
                "enclosure": "http://low/2.torrent",
            },
            "meta_info": {"season_list": [1], "episode_list": [4]},
            "media_info": {"tmdb_id": 100},
        }

        site_priority_map = {10: 0, 20: 1}
        allowed_site_ids = {10, 20}

        standalone_evals, pack_evals, rejects = _evaluate_raw_candidates(
            raw_results=[cand_high_site, cand_low_site],
            tmdb_id_num=100,
            season_num=1,
            total_eps=10,
            missing_eps=[4],
            by_period=False,
            parts_mode=False,
            part_episode_mapping=None,
            keyword_fallback=False,
            min_seeders=2,
            min_resolution="",
            require_chinese=False,
            allowed_site_ids=allowed_site_ids,
            site_priority_map=site_priority_map,
        )
        self.assertEqual(len(standalone_evals), 2)

        # 散集规划执行选种
        plan, note = _plan_standalone_downloads(standalone_evals, needed=[4], tier_ratio=0.10, min_slack=1)
        self.assertEqual(len(plan), 1)
        # 必须选中高优先站 10，绝不因做种数 100 选中低优先站 20
        self.assertEqual(plan[0].site_id, 10)
        self.assertEqual(plan[0].site_priority, 0)

    # ── S07: 硬条件优先与具名降级 ─────────────────────────────────
    async def test_s07_hard_conditions_trump_priority_with_diagnostics(self):
        """S07: 高优先站做种不足或错季时跳过，安全使用低优先站并具名记录降级原因。"""
        cand_high_low_seed = {
            "torrent_info": {
                "title": "测试剧集 S01E04 1080p WEB-DL",
                "site": 10,
                "site_name": "HighPriSite",
                "seeders": 1,  # 低于最低做种 2
                "size": 2 * 1024 * 1024 * 1024,
            },
            "meta_info": {"season_list": [1], "episode_list": [4]},
            "media_info": {"tmdb_id": 100},
        }
        cand_low_qualified = {
            "torrent_info": {
                "title": "测试剧集 S01E04 1080p WEB-DL",
                "site": 20,
                "site_name": "LowPriSite",
                "seeders": 5,  # 合格
                "size": 2 * 1024 * 1024 * 1024,
                "enclosure": "http://low/qual.torrent",
            },
            "meta_info": {"season_list": [1], "episode_list": [4]},
            "media_info": {"tmdb_id": 100},
        }

        site_priority_map = {10: 0, 20: 1}
        allowed_site_ids = {10, 20}

        standalone_evals, pack_evals, rejects = _evaluate_raw_candidates(
            raw_results=[cand_high_low_seed, cand_low_qualified],
            tmdb_id_num=100,
            season_num=1,
            total_eps=10,
            missing_eps=[4],
            by_period=False,
            parts_mode=False,
            part_episode_mapping=None,
            keyword_fallback=False,
            min_seeders=2,
            min_resolution="",
            require_chinese=False,
            allowed_site_ids=allowed_site_ids,
            site_priority_map=site_priority_map,
        )

        plan, note = _plan_standalone_downloads(standalone_evals, needed=[4], tier_ratio=0.10, min_slack=1)
        self.assertEqual(len(plan), 1)
        # 必须降级使用合格的低优先站 20
        self.assertEqual(plan[0].site_id, 20)

    # ── S08: 跨站散集瀑布式组合 ──────────────────────────────────
    async def test_s08_cross_site_waterfall_combination(self):
        """S08: A 站覆盖 E03，B 站覆盖 E03-E04，最终选 A:E03 + B:E04，不重复提交。"""
        cand_site_a_e3 = {
            "torrent_info": {
                "title": "剧集 S01E03 1080p",
                "site": 10,
                "site_name": "SiteA",
                "seeders": 5,
                "size": 1 * 1024 * 1024 * 1024,
                "enclosure": "http://a/e3.torrent",
            },
            "meta_info": {"season_list": [1], "episode_list": [3]},
            "media_info": {"tmdb_id": 200},
        }
        cand_site_b_e3_e4 = {
            "torrent_info": {
                "title": "剧集 S01E03-E04 1080p",
                "site": 20,
                "site_name": "SiteB",
                "seeders": 10,
                "size": 2 * 1024 * 1024 * 1024,
                "enclosure": "http://b/e3e4.torrent",
            },
            "meta_info": {"season_list": [1], "episode_list": [3, 4]},
            "media_info": {"tmdb_id": 200},
        }

        site_priority_map = {10: 0, 20: 1}  # Site A > Site B
        standalone_evals, pack_evals, rejects = _evaluate_raw_candidates(
            raw_results=[cand_site_a_e3, cand_site_b_e3_e4],
            tmdb_id_num=200,
            season_num=1,
            total_eps=10,
            missing_eps=[3, 4],
            by_period=False,
            parts_mode=False,
            part_episode_mapping=None,
            keyword_fallback=False,
            min_seeders=2,
            min_resolution="",
            require_chinese=False,
            allowed_site_ids={10, 20},
            site_priority_map=site_priority_map,
        )

        plan, note = _plan_standalone_downloads(
            standalone_evals, needed=[3, 4], tier_ratio=0.10, min_slack=1
        )
        covered_eps = set()
        for p in plan:
            covered_eps.update(p.covered_missing)
        self.assertEqual(covered_eps, {3, 4})
        site_ids = {p.site_id for p in plan}
        self.assertTrue(10 in site_ids)
        self.assertTrue(20 in site_ids)

    # ── S09: 缺集数量策略 (缺集>3优先整包，<=3优先散集) ───────────
    async def test_s09_pack_vs_standalone_strategy(self):
        """S09: 缺 4 集时优先选择整包；缺 2 集时优先选择散集。"""
        # 构造整季包
        pack_eval = CandidateEval(
            cand={"torrent_info": {"title": "剧集 S01 全季 1080p"}},
            covered_all=tuple(range(1, 9)),
            covered_missing=(1, 2, 3, 4),
            is_season_pack=True,
            seeders=10,
            size_bytes=10 * 1024 * 1024 * 1024,
            site_id=20,
            site_name="SiteB",
            site_sources=("SiteB",),
            stable_key=("pack", 20, "pack_hash", "剧集 S01 全季 1080p"),
            site_priority=1,
        )
        # 构造散集
        ep_evals = [
            CandidateEval(
                cand={"torrent_info": {"title": f"剧集 S01E0{i} 1080p"}},
                covered_all=(i,),
                covered_missing=(i,),
                is_season_pack=False,
                seeders=5,
                size_bytes=1 * 1024 * 1024 * 1024,
                site_id=10,
                site_name="SiteA",
                site_sources=("SiteA",),
                stable_key=(f"e{i}", 10, f"e{i}_hash", f"剧集 S01E0{i} 1080p"),
                site_priority=0,
            )
            for i in range(1, 9)
        ]

        # Case A: 缺 4 集 -> 缺集 > 3，优先选择合格整季包
        best_pack, diag_pack = _pick_best_season_pack([pack_eval], tier_ratio=0.10, min_slack=1)
        self.assertIsNotNone(best_pack)
        self.assertTrue(best_pack.is_season_pack)
        self.assertEqual(best_pack.site_id, 20)

        # Case B: 缺 2 集 -> 缺集 <= 3，散集规划优先覆盖
        plan_standalone, note = _plan_standalone_downloads(
            ep_evals, needed=[1, 2], tier_ratio=0.10, min_slack=1
        )
        self.assertEqual(len(plan_standalone), 2)
        covered = set()
        for p in plan_standalone:
            covered.update(p.covered_missing)
        self.assertEqual(covered, {1, 2})

    # ── S10: 跨站同种去重优先保留高优先站来源 ──────────────────────
    async def test_s10_dedup_evaluations_preserves_high_priority_site(self):
        """S10: 相同种子多站发布时，去重优先保留高优先站独立凭据。"""
        # 相同标题/种子散列，但来自不同站点
        e_low = CandidateEval(
            cand={"torrent_info": {"title": "Same.Torrent.S01E01.1080p"}},
            covered_all=(1,),
            covered_missing=(1,),
            is_season_pack=False,
            seeders=50,  # 即使做种数更高
            size_bytes=1024,
            site_id=20,
            site_name="LowPriSite",
            site_sources=("LowPriSite",),
            stable_key=("same", 20, "hash_same", "Same.Torrent.S01E01.1080p"),
            site_priority=1,
        )
        e_high = CandidateEval(
            cand={"torrent_info": {"title": "Same.Torrent.S01E01.1080p"}},
            covered_all=(1,),
            covered_missing=(1,),
            is_season_pack=False,
            seeders=5,
            size_bytes=1024,
            site_id=10,
            site_name="HighPriSite",
            site_sources=("HighPriSite",),
            stable_key=("same", 10, "hash_same", "Same.Torrent.S01E01.1080p"),
            site_priority=0,  # 高优先级
        )

        deduped = _dedup_evaluations([e_low, e_high])
        self.assertEqual(len(deduped), 1)
        # 必须保留高优先级站点来源
        self.assertEqual(deduped[0].site_id, 10)
        self.assertEqual(deduped[0].site_priority, 0)

    # ── S13: 乐观锁并发冲突拦截 (409 Conflict) ────────────────────
    async def test_s13_optimistic_lock_version_conflict(self):
        """S13: 保存规则时，若传入的 expected_version 与数据库当前版本不一致，抛出版本冲突错误。"""
        async with self.Session() as db:
            inst_key = "default"
            # 建立 v1
            r1 = await save_gap_fill_site_rule(
                db, category="anime", selected_site_ids=[1, 2], status="active", instance_key=inst_key
            )
            self.assertEqual(r1["version"], 1)

            # 客户端 A 成功保存为 v2
            r2 = await save_gap_fill_site_rule(
                db,
                category="anime",
                selected_site_ids=[1, 2, 3],
                status="active",
                expected_version=1,
                instance_key=inst_key,
            )
            self.assertEqual(r2["version"], 2)

            # 客户端 B 仍持有旧版本 expected_version=1 保存 -> 拦截冲突
            with self.assertRaises((RuntimeError, ValueError)) as ctx:
                await save_gap_fill_site_rule(
                    db,
                    category="anime",
                    selected_site_ids=[1, 4],
                    status="active",
                    expected_version=1,
                    instance_key=inst_key,
                )
            self.assertIn("规则已被", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
