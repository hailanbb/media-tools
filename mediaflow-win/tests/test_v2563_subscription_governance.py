"""v2.5.63 影视剧订阅治理深度重构专项验收测试套件.

全面覆盖用户提出的 9 项核心改进诉求：
- 1: 去除多余「豆瓣推选」筛选标签与卡片刷屏徽标，保留稀缺手动订阅标识
- 2.1: 「全季完结」以 CD2 实盘集数齐备 + 播完为准，即使 MP 记录滞后也能精准识别
- 2.2: 短剧/微短剧多维识别（TMDB 单集时长 / 题材命名 / CD2 均体积）+ 严防误杀国漫年番/综艺/纪录片/多季正剧
- 2.3: 「断更停滞」清晰拆分「部分已下·断更停滞」与「零进度死种」，并联动补缺选种引擎实现批量梳理与一键批量补缺推送
- 2.4: 剧集「已收集 X/Y 集」严格以 CD2 源+归档实盘库存为准（覆盖 MP 滞后计数）
- 2.5 & 3.3: 低质/异常特征订阅识别（无封面图、繁体字、小语种、无中文译名、垃圾占位词条）且零误杀标准简体片名
- 3.1: 电影「已入库完成」严格以 CD2 实盘视频核验为准
- 3.2: 往年老片死种电影支持同款补缺站点批量筛查可用资源与一键推送下载（不带季号）
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 安全护栏：隔离测试数据库，绝不触碰生产 data/mediaflow.db
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="mf-sub-gov-test-")
os.environ["MF_DATA_DIR"] = _TMP_DATA_DIR
os.environ.setdefault("MF_MP_URL", "http://127.0.0.1:3000")
os.environ.setdefault("MF_MP_TOKEN", "test-token")

from app.core.subscription_cleaner import (
    detect_low_quality_signals,
    detect_short_drama,
    diagnose_movie_subscription,
    diagnose_tv_subscription,
    evaluate_subscriptions,
)
from app.database import Base, async_session, engine, init_db
from app.services.subscription_cd2_index import (
    enrich_subscriptions_with_cd2_and_cache,
    save_cd2_snapshot,
)
from app.services.subscription_governance_service import (
    _select_best_movie_candidate,
    execute_batch_subscription_gap_fill,
    get_batch_audit_status,
    start_batch_resource_audit,
)


class TestV2563SubscriptionGovernance(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 22, 12, 0, 0)

    async def asyncSetUp(self):
        import app.services.subscription_cd2_index as cd2_idx
        cd2_idx._memory_snapshot = None
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await init_db()

    async def asyncTearDown(self):
        await engine.dispose()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP_DATA_DIR, ignore_errors=True)

    # ── 2.2 多维短剧识别与白名单防误杀 ────────────────────────────
    def test_01_short_drama_multidimensional_detection_and_whitelist(self):
        # 1. TMDB 单集时长 <= 15 分钟（金标准）
        ok1, why1 = detect_short_drama(
            {"tmdb_episode_run_time": [2], "tmdb_origin_country": ["CN"]},
            name="普通剧名测试", season=1, total_ep=80, comp_ep=10,
        )
        self.assertTrue(ok1)
        self.assertIn("2 分钟", why1 or "")

        # 2. S01 + 高集数/99 + 微短剧命名特征（真实生产样例）
        ok2, why2 = detect_short_drama({}, name="她从棺中醒来", season=1, total_ep=99, comp_ep=65)
        self.assertTrue(ok2)
        self.assertIn("醒来", why2 or "")

        ok3, why3 = detect_short_drama({}, name="我在天庭收废品", season=1, total_ep=60, comp_ep=40)
        self.assertTrue(ok3)
        self.assertIn("收废品", why3 or "")

        # 3. CD2 单集平均体积 < 65MB 佐证
        ok4, why4 = detect_short_drama(
            {"cd2_avg_video_mb": 22.5, "cd2_total_episodes": 24},
            name="平凡的一天", season=1, total_ep=60, comp_ep=24,
        )
        self.assertTrue(ok4)
        self.assertIn("22.5MB", why4 or "")

        # 4. 严防误杀国漫年番《仙逆》(200集，动漫目录 / genre=16)
        ok_anime1, _ = detect_short_drama(
            {"cd2_paths": ["/115/Library_incs/nas/动漫/国产动漫/仙逆 (2023) [tmdbid=223911]/Season 1"]},
            name="仙逆", season=1, total_ep=200, comp_ep=129,
        )
        self.assertFalse(ok_anime1)

        ok_anime2, _ = detect_short_drama(
            {"tmdb_genre_ids": [16, 10759], "tmdb_episode_run_time": [12]},
            name="重生之修仙年番", season=1, total_ep=100, comp_ep=50,
        )
        self.assertFalse(ok_anime2)

        # 5. 严防误杀多季综艺与纪录片
        ok_var, _ = detect_short_drama({}, name="密室大逃脱 大神版", season=7, total_ep=99, comp_ep=0)
        self.assertFalse(ok_var)

        ok_var2, _ = detect_short_drama({}, name="喜人奇妙夜·纯享版", season=2, total_ep=76, comp_ep=1)
        self.assertFalse(ok_var2)

        ok_doc, _ = detect_short_drama({"tmdb_genre_ids": [99]}, name="中国玉", season=1, total_ep=99, comp_ep=7)
        self.assertFalse(ok_doc)

    # ── 2.5 & 3.3 低质/异常特征检测（零误报验证） ───────────────────
    def test_02_low_quality_signals_and_simplified_zero_false_positive(self):
        # 无封面图
        f1, r1 = detect_low_quality_signals({"name": "帐篷—1", "poster": "", "backdrop": ""})
        self.assertIn("no_poster", f1)
        self.assertIn("无封面图", r1)

        # 垃圾词条
        f2, _ = detect_low_quality_signals({"name": "重复词条", "poster": "", "backdrop": ""})
        self.assertIn("junk_title", f2)
        self.assertIn("no_poster", f2)

        # 繁体字标题
        f3, r3 = detect_low_quality_signals({"name": "翻盤下半場", "poster": "http://img"})
        self.assertIn("traditional_chinese", f3)
        self.assertTrue(any("繁体字" in x for x in r3))

        # 小语种（韩文 / 日文假名）
        f4, _ = detect_low_quality_signals({"name": "산독기 프로젝트", "poster": "http://img"})
        self.assertIn("minor_script", f4)

        f5, _ = detect_low_quality_signals({"name": "新臺灣日日新報ー暗路", "poster": "", "backdrop": ""})
        self.assertIn("minor_script", f5)
        self.assertIn("traditional_chinese", f5)
        self.assertIn("no_poster", f5)

        # 无中文译名
        f6, _ = detect_low_quality_signals({"name": "OUTLORE with Eleanor Neale", "poster": "http://img"})
        self.assertIn("no_chinese_title", f6)

        # 标准简体中文标题绝对不能误报！
        clean_titles = [
            "漫长的季节", "仙逆", "庆余年", "疯狂动物城2", "流浪地球2",
            "繁花", "唐朝诡事录之西行", "沙丘2 (Dune: Part Two)", "密室大逃脱 大神版",
            "志愿军抗美援朝", "我在他乡挺好的", "去有风的地方", "黑神话",
            "食神·百厨大战", "深夜食堂：东京故事", "肉食猎者",
        ]
        for t in clean_titles:
            flags, reasons = detect_low_quality_signals({"name": t, "poster": "http://ok.jpg"})
            self.assertEqual(flags, [], f"简体标题误报: {t} -> {reasons}")

    # ── 2.1, 2.3, 2.4 & 3.1 CD2 实盘真相优先 + 断更双分桶 ──────────────
    def test_03_cd2_truth_overrides_mp_and_stalled_split(self):
        # 2.4 & 2.1: MP 记 10/12，但 CD2 实存 12/12 -> 以 CD2 为准直接判全季完结！
        tv_cd2_full = {
            "id": 3001, "name": "边水往事", "type": "电视剧", "season": 1,
            "total_episode": 12, "completed_episode": 10, "lack_episode": 2,
            "state": "R", "username": "豆瓣榜单", "poster": "http://ok",
            "cd2_verified": True, "cd2_source_episodes": 0, "cd2_archive_episodes": 12,
            "cd2_total_episodes": 12, "cd2_episode_numbers": list(range(1, 13)),
            "cd2_paths": ["/115/Library_incs/nas.finish/电视剧/国产剧/边水往事 [tmdbid=1]/Season 1"],
        }
        d_full = diagnose_tv_subscription(tv_cd2_full, now=self.now)
        self.assertEqual(d_full.data_source, "cd2")
        self.assertEqual(d_full.completed_episodes, 12)
        self.assertEqual(d_full.mp_completed_episodes, 10)
        self.assertEqual(d_full.clean_rule, "completed")
        self.assertTrue(d_full.clean_eligible)

        # 2.3 细分桶 A: 部分已下·断更停滞 (CD2 有 7/10 集，40 天无更新)
        tv_partial = {
            "id": 3002, "name": "最后通牒", "type": "电视剧", "season": 2,
            "total_episode": 10, "completed_episode": 5, "state": "R",
            "date": "2026-08-01 00:00:00", "last_update": "2026-09-01 00:00:00",
            "username": "豆瓣榜单", "poster": "http://ok",
            "cd2_verified": True, "cd2_source_episodes": 7, "cd2_archive_episodes": 0,
            "cd2_total_episodes": 7, "cd2_episode_numbers": [1, 2, 3, 4, 5, 6, 7],
        }
        d_partial = diagnose_tv_subscription(tv_partial, now=self.now)
        self.assertEqual(d_partial.clean_rule, "stalled")
        self.assertEqual(d_partial.stalled_kind, "partial_stalled")
        self.assertTrue(d_partial.can_gap_fill)
        self.assertEqual(d_partial.completed_episodes, 7)

        # 2.3 细分桶 B: 零进度死种 (CD2 实存 0 集，创建 80 天)
        tv_zero = {
            "id": 3003, "name": "冷门老剧", "type": "电视剧", "season": 1,
            "total_episode": 16, "completed_episode": 0, "state": "R",
            "date": "2026-08-01 00:00:00", "last_update": "2026-08-01 00:00:00",
            "username": "豆瓣榜单", "poster": "http://ok",
            "cd2_verified": True, "cd2_total_episodes": 0, "cd2_episode_numbers": [],
        }
        d_zero = diagnose_tv_subscription(tv_zero, now=self.now)
        self.assertEqual(d_zero.clean_rule, "zombie")
        self.assertEqual(d_zero.stalled_kind, "never_downloaded")
        self.assertTrue(d_zero.can_gap_fill)

        # 3.1 电影以 CD2 为准：即使 MP 是 P 状态，但 CD2 核实未入库 -> 绝不能判 completed！
        mov_p_not_cd2 = {
            "id": 4001, "name": "虚标暂停片", "year": "2025", "type": "电影",
            "state": "P", "username": "admin", "poster": "http://ok",
            "date": "2026-06-01 00:00:00",
            "cd2_verified": True, "cd2_in_library": False, "cd2_paths": [],
        }
        d_mov_no = diagnose_movie_subscription(mov_p_not_cd2, now=self.now)
        self.assertNotEqual(d_mov_no.clean_rule, "completed")
        self.assertEqual(d_mov_no.in_library_status, "not_in_library")

        # 3.1 电影 CD2 实盘存在有效视频 -> 即使 R 状态且当年 2026 新片也直接判 completed！
        mov_cd2_yes = {
            "id": 4002, "name": "热映大片", "year": "2026", "type": "电影",
            "state": "R", "username": "豆瓣榜单", "poster": "http://ok",
            "date": "2026-10-01 00:00:00",
            "cd2_verified": True, "cd2_in_library": True,
            "cd2_paths": ["/115/Library_incs/nas.finish/电影/华语电影/热映大片 (2026) [tmdbid=9999]"],
        }
        d_mov_yes = diagnose_movie_subscription(mov_cd2_yes, now=self.now)
        self.assertEqual(d_mov_yes.clean_rule, "completed")
        self.assertEqual(d_mov_yes.clean_confidence, "high")
        self.assertTrue(d_mov_yes.clean_eligible)
        self.assertIn("CD2", d_mov_yes.clean_rule_name)

        # 3.3 异常特征电影（无封面、小语种等）且榜单同步 -> 归入 low_quality 待清
        mov_lq = {
            "id": 4003, "name": "연", "year": "2026", "type": "电影",
            "state": "R", "username": "豆瓣榜单", "poster": "", "backdrop": "",
            "date": "2026-11-01 00:00:00",
            "cd2_verified": True, "cd2_in_library": False,
        }
        d_mov_lq = diagnose_movie_subscription(mov_lq, now=self.now)
        self.assertEqual(d_mov_lq.clean_rule, "low_quality")
        self.assertTrue(d_mov_lq.is_low_quality)
        self.assertTrue(d_mov_lq.clean_eligible)

    # ── CD2 快照持久化叠加注入测试 ──────────────────────────────────
    async def test_04_cd2_snapshot_enrichment_persists_and_merges(self):
        snapshot = {
            "version": 1,
            "synced_at": "2026-11-22 12:00:00",
            "source_root": "/nas",
            "archive_root": "/nas.finish",
            "tv_scanned_ok": True,
            "movie_scanned_ok": True,
            "tv_by_key": {
                "223911:1": {
                    "season": 1, "source_count": 131, "archive_count": 0, "total_count": 131,
                    "merged_episodes": list(range(1, 132)), "avg_video_mb": 420.0,
                    "paths": ["/nas/动漫/国产动漫/仙逆 (2023) [tmdbid=223911]/Season 1"],
                }
            },
            "tv_by_title": {},
            "movie_by_tmdb": {
                "1063260": {
                    "in_library": True, "video_count": 1, "max_video_mb": 6500.0,
                    "paths": ["/nas.finish/电影/华语电影/大闹天宫 (2021) [tmdbid=1063260]"],
                }
            },
            "movie_by_title": {},
        }
        async with async_session() as db:
            await save_cd2_snapshot(db, snapshot)
            raw_subs = [
                {"id": 37, "name": "仙逆", "year": "2023", "type": "电视剧", "season": 1, "tmdbid": 223911, "total_episode": 200, "completed_episode": 129, "lack_episode": 71, "state": "R", "username": "admin", "poster": "http://ok"},
                {"id": 6725, "name": "大闹天宫", "year": "2021", "type": "电影", "tmdbid": 1063260, "state": "R", "username": "豆瓣榜单", "poster": "http://ok"},
                {"id": 6726, "name": "未入库电影", "year": "2020", "type": "电影", "tmdbid": 888888, "state": "P", "username": "豆瓣榜单", "poster": "http://ok", "date": "2026-01-01 00:00:00"},
            ]
            enriched, meta = await enrich_subscriptions_with_cd2_and_cache(db, raw_subs)
            self.assertTrue(meta["cd2_synced"])

            res = evaluate_subscriptions(enriched)
            by_id = {d.sub_id: d for d in res["items"]}

            # 仙逆：以 CD2 的 131 集覆盖 MP 的 129 集，且绝不误判短剧
            self.assertEqual(by_id[37].completed_episodes, 131)
            self.assertEqual(by_id[37].mp_completed_episodes, 129)
            self.assertEqual(by_id[37].data_source, "cd2")
            self.assertFalse(by_id[37].is_short_tv)

            # 大闹天宫：CD2 已核实入库 -> completed
            self.assertEqual(by_id[6725].clean_rule, "completed")
            self.assertTrue(by_id[6725].cd2_in_library)

            # 未入库电影：尽管 MP 是 P 状态，CD2 核实不在库 -> 绝非 completed，应归入 zombie
            self.assertEqual(by_id[6726].clean_rule, "zombie")
            self.assertFalse(by_id[6726].cd2_in_library)

    # ── 2.3 & 3.2 批量补缺梳理与一键推送闭环测试 ─────────────────────
    async def test_05_movie_candidate_selection_and_tv_batch_audit_push(self):
        # 1. 电影候选选种：优先高优先站点，同站取做种同档内体积最小
        raw_movie_cands = [
            {"media_info": {"tmdb_id": 500}, "meta_info": {}, "torrent_info": {"title": "Movie.2020.4K.REMUX", "site": 1, "site_name": "天空", "seeders": 10, "size": 50 * 1024**3}},
            {"media_info": {"tmdb_id": 500}, "meta_info": {}, "torrent_info": {"title": "Movie.2020.1080p", "site": 1, "site_name": "天空", "seeders": 9, "size": 10 * 1024**3}},
            {"media_info": {"tmdb_id": 500}, "meta_info": {}, "torrent_info": {"title": "Movie.2020.Dead", "site": 1, "site_name": "天空", "seeders": 1, "size": 5 * 1024**3}},
            {"media_info": {"tmdb_id": 500}, "meta_info": {}, "torrent_info": {"title": "Movie.2020.LowPri", "site": 2, "site_name": "观众", "seeders": 50, "size": 8 * 1024**3}},
        ]
        best, max_s, rej = _select_best_movie_candidate(
            raw_movie_cands,
            tmdb_id_num=500, min_seeders=2, tier_ratio=0.10, min_slack=1,
            min_resolution="", require_chinese=False,
            allowed_site_ids={1, 2}, site_priority_map={1: 0, 2: 1},
        )
        self.assertIsNotNone(best)
        self.assertEqual(best["site_name"], "天空")  # 高优先站优先
        self.assertEqual(best["cand"]["torrent_info"]["title"], "Movie.2020.1080p")  # 同档(9 >= 10-1)省容量选 10G
        self.assertEqual(max_s, 50)

        # 2. 端到端电视剧断更停滞批量梳理 -> 筛出可补缺 & 无源 -> 一键批量推送
        fake_subs = [
            {
                "id": 7001, "name": "可补停滞剧", "year": "2025", "type": "电视剧",
                "season": 1, "tmdbid": 90001, "total_episode": 4, "completed_episode": 2,
                "lack_episode": 2, "state": "R", "username": "豆瓣榜单",
                "date": "2026-06-01 00:00:00", "last_update": "2026-07-01 00:00:00",
                "poster": "http://ok",
            },
            {
                "id": 7002, "name": "真无源停滞剧", "year": "2025", "type": "电视剧",
                "season": 1, "tmdbid": 90002, "total_episode": 4, "completed_episode": 2,
                "lack_episode": 2, "state": "R", "username": "豆瓣榜单",
                "date": "2026-06-01 00:00:00", "last_update": "2026-07-01 00:00:00",
                "poster": "http://ok",
            },
        ]

        async def fake_search(tmdb_id=None, season=None, title=None, media_type="tv", **kwargs):
            if tmdb_id == 90001:
                return [
                    {
                        "media_info": {"tmdb_id": 90001, "title": "可补停滞剧"},
                        "meta_info": {"begin_season": 1, "season_list": [1], "begin_episode": 3, "end_episode": 4, "episode_list": [3, 4]},
                        "torrent_info": {"title": "可补停滞剧 S01E03-E04 1080p", "site": 1, "site_name": "天空", "seeders": 8, "size": 2 * 1024**3},
                    }
                ]
            return []

        with patch("app.services.subscription_governance_service.MoviePilotClient") as MockMP, \
             patch("app.services.subscription_governance_service.resolve_category_gap_fill_sites", new=AsyncMock(return_value={
                 "ready": True, "sites_param": "1", "active_ordered_ids": [1], "site_priority_map": {1: 0}
             })):
            mp_inst = MockMP.return_value
            mp_inst.list_subscriptions = AsyncMock(return_value=fake_subs)
            mp_inst.get_active_downloads = AsyncMock(return_value=[])
            mp_inst.search_resources = AsyncMock(side_effect=fake_search)
            mp_inst.submit_download = AsyncMock(return_value={"success": True, "message": "已加入队列"})
            mp_inst.close = AsyncMock()

            async with async_session() as db:
                # 先启动梳理
                started = await start_batch_resource_audit(
                    db, async_session, media_type="tv", bucket="partial_stalled", limit=10
                )
                aid = started["audit_id"]

                # 等待后台任务完成
                from app.services.subscription_governance_service import _audit_tasks
                if aid in _audit_tasks:
                    await _audit_tasks[aid]

                status = get_batch_audit_status(aid)
                self.assertEqual(status["status"], "completed")
                self.assertEqual(status["downloadable_count"], 1)
                self.assertEqual(status["no_resource_count"], 1)
                self.assertEqual(status["downloadable_items"][0]["id"], 7001)
                self.assertEqual(status["no_resource_items"][0]["id"], 7002)

                # 一键批量补缺推送
                push_res = await execute_batch_subscription_gap_fill(db, audit_id=aid, sub_ids=[7001])
                self.assertTrue(push_res["ok"])
                self.assertEqual(push_res["pushed_count"], 1)
                mp_inst.submit_download.assert_awaited_once()

    # ── 1: 验证前端已移除多余「豆瓣推选」过滤 Tab ────────────────────
    def test_06_frontend_removed_redundant_douban_filter_tab(self):
        html = (Path(__file__).resolve().parent.parent / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("btn-mov-douban", html)
        self.assertNotIn("🔵 豆瓣推选", html)
        self.assertIn("subCd2SyncBanner", html)
        self.assertIn("subBatchAuditModal", html)
        self.assertIn("btn-tv-partial_stalled", html)
        self.assertIn("btn-tv-never_downloaded", html)
        self.assertIn("btn-tv-low_quality", html)
        self.assertIn("btn-mov-low_quality", html)


if __name__ == "__main__":
    unittest.main()
