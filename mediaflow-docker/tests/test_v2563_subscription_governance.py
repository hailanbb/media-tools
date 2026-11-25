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
        import app.services.subscription_governance_service as sub_gov
        cd2_idx._memory_snapshot = None
        sub_gov._memory_resource_cache = None
        sub_gov._memory_unified_snapshot = None
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
                # 在严格 CD2-only 策略下，先行写入测试集 CD2 实盘快照
                await save_cd2_snapshot(
                    db,
                    {
                        "version": 1,
                        "synced_at": "2026-11-22 12:00:00",
                        "tv_scanned_ok": True,
                        "movie_scanned_ok": True,
                        "tv_by_key": {
                            "90001:1": {
                                "season": 1,
                                "source_count": 2,
                                "archive_count": 0,
                                "total_count": 2,
                                "merged_episodes": [1, 2],
                                "paths": ["/nas/电视剧/可补停滞剧/Season 1"],
                            },
                            "90002:1": {
                                "season": 1,
                                "source_count": 2,
                                "archive_count": 0,
                                "total_count": 2,
                                "merged_episodes": [1, 2],
                                "paths": ["/nas/电视剧/真无源停滞剧/Season 1"],
                            },
                        },
                        "tv_by_title": {},
                        "movie_by_tmdb": {},
                        "movie_by_title": {},
                    },
                )
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

    # ── 1: 验证前端已移除多余「豆瓣推选」过滤 Tab 并落地统一查询与按类操作闭环 ──
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
        self.assertIn("查询最新订阅数据", html)
        self.assertIn("批量一键取消订阅", html)
        self.assertIn("批量补缺下载", html)
        self.assertIn("批量取消订阅", html)
        self.assertIn("loadSubscriptions({ cachedOnly: true })", html)

    async def test_07_unified_query_pipeline_and_cached_only_mode(self):
        from app.api.moviepilot import list_subscriptions
        import app.services.subscription_governance_service as sub_gov

        async with async_session() as db:
            # 1. 进入视图默认 cached_only=True：未查询过时返回空快照，绝不发起外部请求
            initial = await list_subscriptions(cached_only=True, db=db)
            self.assertFalse(initial["has_snapshot"])
            self.assertEqual(initial["total"], 0)

        fake_subs = [
            {
                "id": 8001, "name": "统一查询可补剧", "year": "2024", "type": "电视剧",
                "tmdbid": 88001, "season": 1, "total_episode": 4, "lack_episode": 2,
                "state": "R", "username": "豆瓣榜单",
                "date": "2025-10-01 10:00:00", "last_update": "2025-10-15 10:00:00",
                "poster": "http://img/8001.jpg",
            }
        ]

        async def fake_search(*, tmdb_id=None, **kwargs):
            if tmdb_id == 88001:
                return [
                    {
                        "media_info": {"tmdb_id": 88001, "title": "统一查询可补剧"},
                        "meta_info": {"begin_season": 1, "season_list": [1], "begin_episode": 3, "end_episode": 4, "episode_list": [3, 4]},
                        "torrent_info": {"title": "统一查询可补剧 S01E03-E04 1080p", "site": 1, "site_name": "天空", "seeders": 9, "size": 2 * 1024**3},
                    }
                ]
            return []

        async def fake_build_cd2_inventory(db_session_factory, **kwargs):
            async with db_session_factory() as db:
                await save_cd2_snapshot(
                    db,
                    {
                        "version": 1,
                        "synced_at": "2026-11-23 10:00:00",
                        "tv_scanned_ok": True,
                        "movie_scanned_ok": True,
                        "tv_by_key": {
                            "88001:1": {
                                "season": 1,
                                "source_count": 2,
                                "archive_count": 0,
                                "total_count": 2,
                                "merged_episodes": [1, 2],
                                "paths": ["/nas/电视剧/统一查询可补剧/Season 1"],
                            }
                        },
                        "tv_by_title": {},
                        "movie_by_tmdb": {},
                        "movie_by_title": {},
                    },
                )
            return {"ok": True}

        with patch("app.services.subscription_governance_service.MoviePilotClient") as MockMP, \
             patch("app.services.subscription_governance_service.build_cd2_subscription_inventory", side_effect=fake_build_cd2_inventory), \
             patch("app.services.subscription_governance_service.resolve_category_gap_fill_sites", new=AsyncMock(return_value={
                 "ready": True, "sites_param": "1", "active_ordered_ids": [1], "site_priority_map": {1: 0}
             })):
            mp_inst = MockMP.return_value
            mp_inst.list_subscriptions = AsyncMock(return_value=fake_subs)
            mp_inst.get_active_downloads = AsyncMock(return_value=[])
            mp_inst.search_resources = AsyncMock(side_effect=fake_search)
            mp_inst.submit_download = AsyncMock(return_value={"success": True, "message": "已加入队列"})
            mp_inst.close = AsyncMock()

            st = sub_gov.start_unified_subscription_query(async_session, search_resources=True, reuse_resource_cache=False)
            self.assertEqual(st["status"], "running")
            if sub_gov._unified_query_task:
                await sub_gov._unified_query_task

            final_st = sub_gov.get_unified_query_status()
            self.assertEqual(final_st["status"], "completed")
            self.assertEqual(final_st["resource_downloadable"], 1)

            async with async_session() as db:
                cached = await list_subscriptions(cached_only=True, db=db)
                self.assertTrue(cached["has_snapshot"])
                self.assertEqual(cached["tv"]["partial_stalled"], 1)
                self.assertEqual(cached["tv"]["partial_stalled_downloadable"], 1)
                item = cached["subscriptions"][0]
                self.assertEqual(item["resource_status"], "downloadable")
                self.assertTrue(item["cd2_verified"])
                self.assertEqual(item["data_source"], "cd2")

                # 直接从统一查询缓存执行一键补缺下载（无需额外梳理会话）并验证批量进度状态
                push_res = await execute_batch_subscription_gap_fill(db, audit_id="unified", sub_ids=[8001])
                self.assertTrue(push_res["ok"])
                self.assertEqual(push_res["pushed_count"], 1)
                ba_st = sub_gov.get_batch_action_status()
                self.assertEqual(ba_st["action_type"], "gap_fill_push")
                self.assertEqual(ba_st["status"], "completed")
                self.assertEqual(ba_st["progress_pct"], 100)

    # ── v2.5.67 专项回归：修复 5 大用户实测问题 ─────────────────────────
    async def test_08_cd2_scan_season_episodes_parses_real_filenames(self):
        import asyncio
        from types import SimpleNamespace
        from app.services.subscription_cd2_index import _scan_season_episodes

        show_dir = "/src/电视剧/日本剧集/雾中之犬 (2025) [tmdbid=294254]"
        files_s1 = [
            {"name": "season.nfo", "path": f"{show_dir}/Season 1/season.nfo", "size": 100, "isDirectory": False},
            {"name": "雾中之犬 - S01E01 - FOGDOG.S01E01.2025.1080p.mkv", "path": f"{show_dir}/Season 1/e1.mkv", "size": 500_000_000, "isDirectory": False},
            {"name": "朝雪录 - S01E15 - 朝雪录.Coroner's.Diary.S01E15.2025.1080p.mkv", "path": f"{show_dir}/Season 1/e15.mkv", "size": 600_000_000, "isDirectory": False},
            {"name": "Show.S01E20.4k.Tape.01.2160p.mp4", "path": f"{show_dir}/Season 1/tape.mp4", "size": 100_000_000, "isDirectory": False},
            {"name": "Clip.S01E04.mkv", "path": f"{show_dir}/Season 1/clip.mkv", "size": 50_000_000, "isDirectory": False},
        ]
        fake_cd2 = AsyncMock()
        async def list_files(p, **kw):
            if p == show_dir:
                return [{"name": "Season 1", "path": f"{show_dir}/Season 1", "isDirectory": True}]
            if p == f"{show_dir}/Season 1":
                return files_s1
            return []
        fake_cd2.list_files.side_effect = list_files

        eps, paths, sizes = await _scan_season_episodes(fake_cd2, show_dir, 1, asyncio.Semaphore(2))
        self.assertEqual(sorted(eps), [1, 15])
        self.assertEqual(paths, [f"{show_dir}/Season 1"])
        self.assertEqual(len(sizes), 2)

    def test_09_movie_qiaofu6_p_state_never_marked_completed(self):
        # 案例：《潜伏6》(2026) TMDB 1291595，MP 状态为 P，但 CD2 中没有
        base = {
            "id": 7087, "name": "潜伏6", "year": "2026", "type": "电影",
            "tmdbid": 1291595, "state": "P", "username": "豆瓣榜单",
            "date": "2026-08-23 00:00:00", "poster": "http://img/7087.jpg",
        }
        # 1. strict_cd2_only=True & cd2_verified=False（CD2 尚未核验）
        d1 = diagnose_movie_subscription({**base, "strict_cd2_only": True, "cd2_verified": False}, now=self.now)
        self.assertNotEqual(d1.clean_rule, "completed")
        self.assertEqual(d1.clean_rule, "upcoming_protect")
        self.assertFalse(d1.clean_eligible)

        # 2. cd2_verified=True & cd2_in_library=False（CD2 已核验不存在）
        d2 = diagnose_movie_subscription({**base, "strict_cd2_only": True, "cd2_verified": True, "cd2_in_library": False}, now=self.now)
        self.assertNotEqual(d2.clean_rule, "completed")
        self.assertEqual(d2.clean_rule, "upcoming_protect")

        # 3. 仅当 cd2_verified=True & cd2_in_library=True 时才允许进入 completed
        d3 = diagnose_movie_subscription(
            {**base, "strict_cd2_only": True, "cd2_verified": True, "cd2_in_library": True, "cd2_paths": ["/nas.finish/电影/外语电影/潜伏6 (2026) [tmdbid=1291595]"]},
            now=self.now,
        )
        self.assertEqual(d3.clean_rule, "completed")
        self.assertTrue(d3.clean_eligible)

    def test_10_episode_parser_title_before_diary_and_gap_fill_selection(self):
        from app.core.episode_parser import parse_episode
        from app.services.download_service import _evaluate_raw_candidates

        # 1. 多词剧名中的 Before / Diary 绝不能被误判为 is_extra
        p1 = parse_episode("10 Things I Want to Do Before I Turn 40 S01E11 1080p friDay WEB-DL AAC2.0 H.264-")
        self.assertFalse(p1.is_extra)
        self.assertEqual(p1.season_number, 1)
        self.assertEqual(p1.episode_numbers, [11])

        p2 = parse_episode("朝雪录 - S01E15 - 朝雪录.Coroner's.Diary.S01E15.2025.1080p.WEB-DL.H264.AAC-HHWEB.mkv")
        self.assertFalse(p2.is_extra)
        self.assertEqual(p2.episode_numbers, [15])

        # 2. 真正的独立衍生/花絮必须继续保持 is_extra=True
        for extra_name in (
            "Clip.S01E04.mkv",
            "Sister.Take.Over.S01E20.4k.Tape.01.2160p.WEB-DL.H265.AAC-OurTV.mp4",
            "Show.S01E11.加更版.mp4",
            "Show.S01.特辑.花絮.mkv",
        ):
            self.assertTrue(parse_episode(extra_name).is_extra, f"应继续判定为花絮: {extra_name}")

        # 3. 验证 TMDB 292544 缺 E11/E12 时，站点候选能通过单集选种验证
        raw_cands = [
            {
                "media_info": {"tmdb_id": 292544, "title": "40岁前想要达成的10件事"},
                "meta_info": {"begin_season": 1, "season_list": [1], "episode_list": [11]},
                "torrent_info": {"title": "10 Things I Want to Do Before I Turn 40 S01E11 1080p friDay WEB-DL", "site": 14, "site_name": "馒头", "seeders": 5, "size": 900 * 1024**2},
            },
            {
                "media_info": {"tmdb_id": 292544, "title": "40岁前想要达成的10件事"},
                "meta_info": {"begin_season": 1, "season_list": [1], "episode_list": [12]},
                "torrent_info": {"title": "10 Things I Want to Do Before I Turn 40 S01E12 1080p friDay WEB-DL", "site": 14, "site_name": "馒头", "seeders": 6, "size": 910 * 1024**2},
            },
        ]
        st_evals, _, rej = _evaluate_raw_candidates(
            raw_cands, tmdb_id_num=292544, season_num=1, total_eps=12, missing_eps=[11, 12],
            by_period=False, parts_mode=False, part_episode_mapping=None, keyword_fallback=False,
            min_seeders=2, min_resolution="", require_chinese=False, allowed_site_ids={14}, site_priority_map={14: 1},
        )
        self.assertEqual(len(st_evals), 2, f"unexpected rejections: {rej}")
        self.assertEqual(sorted(ep for ev in st_evals for ep in ev.covered_missing), [11, 12])

    async def test_11_strict_cd2_truth_no_mp_or_stale_record_fallback(self):
        from app.models.metadata_cache import MetadataCache
        from app.models.season import SeasonRecord

        # strict_cd2_only=True 且 cd2_verified=False 时，绝不回退 MP 的 completed_episode
        d_pending = diagnose_tv_subscription(
            {
                "id": 9101, "name": "未核验剧", "type": "电视剧", "season": 1,
                "total_episode": 12, "completed_episode": 12, "lack_episode": 0,
                "state": "P", "username": "豆瓣榜单", "poster": "http://ok",
                "strict_cd2_only": True, "cd2_verified": False,
            },
            now=self.now,
        )
        self.assertEqual(d_pending.data_source, "cd2_pending")
        self.assertEqual(d_pending.completed_episodes, 0)
        self.assertEqual(d_pending.clean_rule_name, "待 CD2 实盘核验")
        self.assertFalse(d_pending.clean_eligible)

        # 验证 TMDB 1 集占位符 (<2 或 < cd2_total_episodes) 不会误覆盖 MP 99 占位符为 completed
        for sid, title, cd2_cnt in [
            (4617, "落尽霜花满地伤", 75),
            (7597, "食贫道充电纪录片", 1),
        ]:
            d_stub = diagnose_tv_subscription(
                {
                    "id": sid, "name": title, "type": "电视剧", "season": 1,
                    "total_episode": 99, "completed_episode": cd2_cnt, "lack_episode": 99 - cd2_cnt,
                    "state": "R", "username": "豆瓣榜单", "poster": "http://ok",
                    "cd2_verified": True, "cd2_total_episodes": cd2_cnt,
                    "tmdb_season_episodes": 1, "season_finished": False,
                    "date": "2025-01-01", "last_update": "2026-11-22",
                },
                now=self.now,
            )
            self.assertEqual(d_stub.total_episodes, 99)
            self.assertNotEqual(d_stub.clean_rule, "completed")

        # tv_scanned_ok=True 且 CD2 快照中无某剧 -> 绝不回退历史 SeasonRecord；同时验证 TMDB 99 集纠偏缓存
        async with async_session() as db:
            db.add(
                SeasonRecord(
                    id="stale-rec-1", task_id="old-task", show_name="旧记录剧", show_path="/src/旧记录剧",
                    tmdb_id="77777", season_number=1, season_path="/src/旧记录剧/Season 1",
                    total_episodes=10, local_episode_count=10, recognized_episodes_json="[1,2,3,4,5,6,7,8,9,10]",
                    status="archive_ready",
                )
            )
            db.add(
                MetadataCache(
                    cache_key="tmdb_tv_season:294254:1",
                    source="tmdb_tv_season",
                    media_id="294254",
                    season_number=1,
                    payload_json='{"tmdb_id": 294254, "season_number": 1, "episode_count": 10, "aired_count": 10, "season_finished": true}',
                    verified=True,
                    status="ok",
                )
            )
            db.add(
                MetadataCache(
                    cache_key="tmdb_tv_season:313356:1",
                    source="tmdb_tv_season",
                    media_id="313356",
                    season_number=1,
                    payload_json='{"tmdb_id": 313356, "season_number": 1, "episode_count": 1, "aired_count": 1, "season_finished": true}',
                    verified=True,
                    status="ok",
                )
            )
            db.add(
                MetadataCache(
                    cache_key="tmdb_tv_season:284720:12",
                    source="tmdb_tv_season",
                    media_id="284720",
                    season_number=12,
                    payload_json='{"tmdb_id": 284720, "season_number": 12, "episode_count": 1, "aired_count": 1, "season_finished": true}',
                    verified=True,
                    status="ok",
                )
            )
            await db.commit()

            await save_cd2_snapshot(
                db,
                {
                    "version": 1, "synced_at": "2026-11-23 10:00:00",
                    "tv_scanned_ok": True, "movie_scanned_ok": True,
                    "tv_by_key": {
                        "294254:1": {
                            "season": 1, "source_count": 0, "archive_count": 10, "total_count": 10,
                            "merged_episodes": list(range(1, 11)),
                            "paths": ["/nas.finish/电视剧/日本剧集/雾中之犬 (2025) [tmdbid=294254]/Season 1"],
                        },
                        "313356:1": {
                            "season": 1, "source_count": 75, "archive_count": 0, "total_count": 75,
                            "merged_episodes": list(range(1, 76)),
                            "paths": ["/nas/电视剧/短剧/落尽霜花满地伤 [tmdbid=313356]/Season 1"],
                        },
                        "284720:12": {
                            "season": 12, "source_count": 1, "archive_count": 0, "total_count": 1,
                            "merged_episodes": [1],
                            "paths": ["/nas/电视剧/纪录片/食贫道 [tmdbid=284720]/Season 12"],
                        },
                    },
                    "tv_by_title": {}, "movie_by_tmdb": {}, "movie_by_title": {},
                },
            )
            subs = [
                {"id": 101, "name": "旧记录剧", "type": "电视剧", "season": 1, "tmdbid": 77777, "total_episode": 10, "completed_episode": 10, "lack_episode": 0, "state": "P"},
                {"id": 2113, "name": "雾中之犬", "year": "2025", "type": "电视剧", "season": 1, "tmdbid": 294254, "total_episode": 99, "completed_episode": 10, "lack_episode": 89, "state": "R"},
                {"id": 4617, "name": "落尽霜花满地伤", "year": "2026", "type": "电视剧", "season": 1, "tmdbid": 313356, "total_episode": 99, "completed_episode": 75, "lack_episode": 24, "state": "R", "date": "2026-01-01", "last_update": "2026-11-22"},
                {"id": 7597, "name": "食贫道充电纪录片", "year": "2025", "type": "电视剧", "season": 12, "tmdbid": 284720, "total_episode": 99, "completed_episode": 1, "lack_episode": 98, "state": "R", "date": "2026-01-01", "last_update": "2026-11-22"},
            ]
            enriched, _ = await enrich_subscriptions_with_cd2_and_cache(db, subs)
            ev = evaluate_subscriptions(enriched)
            by_id = {d.sub_id: d for d in ev["items"]}
            # tv_scanned_ok=True 时，旧 SeasonRecord 被严格忽略，CD2 已收录为 0 集
            self.assertEqual(by_id[101].completed_episodes, 0)
            self.assertTrue(by_id[101].cd2_verified)
            # 《雾中之犬》通过 tmdb_tv_season 纠偏 99 -> 10 集，且 CD2 已收录 10/10 完结
            self.assertEqual(by_id[2113].total_episodes, 10)
            self.assertEqual(by_id[2113].completed_episodes, 10)
            self.assertEqual(by_id[2113].clean_rule, "completed")
            self.assertIn("CD2 已收录", by_id[2113].clean_reason)
            # 单集 TMDB 占位符绝不误标 completed
            self.assertEqual(by_id[4617].total_episodes, 99)
            self.assertNotEqual(by_id[4617].clean_rule, "completed")
            self.assertEqual(by_id[7597].total_episodes, 99)
            self.assertNotEqual(by_id[7597].clean_rule, "completed")

    async def test_12_cd2_source_discovery_cleanup_and_snapshot_removal(self):
        from fastapi import HTTPException
        from app.api.moviepilot import list_subscriptions
        from app.services.subscription_governance_service import save_unified_snapshot
        from app.services.subscription_service import discover_source_season_entries, execute, preview

        # 无 SeasonRecord 时通过 CD2 源目录安全定位「侦讯实境 S4」，并拒绝归档目录/整剧根目录
        src_season = "/src/电视剧/纪录片/侦讯实境 (2022) [tmdbid=203453]/Season 4"
        arch_season = "/dst/电视剧/纪录片/侦讯实境 (2022) [tmdbid=203453]/Season 4"
        show_root = "/src/电视剧/纪录片/侦讯实境 (2022) [tmdbid=203453]"
        vid_file = {"name": "侦讯实境.S04E01.mkv", "path": f"{src_season}/侦讯实境.S04E01.mkv", "size": 500_000_000, "id": "f1", "modified": "2026-11-20", "isDirectory": False}

        sub_rec = {
            "id": 2558, "name": "侦讯实境", "year": "2022", "type": "电视剧",
            "tmdbid": 203453, "season": 4, "total_episode": 15, "completed_episode": 6, "lack_episode": 9,
            "state": "R", "username": "豆瓣榜单", "date": "2025-09-01 00:00:00", "last_update": "2025-11-01 00:00:00",
        }
        fake_settings = type("S", (), {
            "source_root": "/src", "archive_root": "/dst", "library_root": "/dst",
            "cd2_url": "http://cd2:19798", "mp_url": "http://127.0.0.1:3000", "mp_token": "tok",
            "enable_multi_dir_governance": True,
        })()

        mp = AsyncMock()
        mp.list_subscriptions = AsyncMock(return_value=[sub_rec])
        mp.search_resources = AsyncMock(return_value=[])
        mp.get_active_downloads = AsyncMock(return_value=[])
        mp.get_transfer_queue = AsyncMock(return_value=[])
        mp.delete_subscription = AsyncMock(return_value={})

        disk = AsyncMock()
        disk.list_files = AsyncMock(return_value=[vid_file])
        disk.get_file_info = AsyncMock(side_effect=[vid_file, None])
        disk.delete_file = AsyncMock(return_value={"success": True})

        with patch("app.services.subscription_service.get_settings", return_value=fake_settings):
            async with async_session() as db:
                await save_cd2_snapshot(
                    db,
                    {
                        "version": 1, "synced_at": "2026-11-23 10:00:00",
                        "tv_scanned_ok": True, "movie_scanned_ok": True,
                        "tv_by_key": {
                            "203453:4": {
                                "season": 4, "source_count": 1, "archive_count": 1, "total_count": 2,
                                "merged_episodes": [1, 2],
                                "paths": [arch_season, show_root, src_season],
                            }
                        },
                        "tv_by_title": {}, "movie_by_tmdb": {}, "movie_by_title": {},
                    },
                )
                await save_unified_snapshot(
                    db,
                    {
                        "has_snapshot": True, "queried_at": "2026-11-23 10:00:00",
                        "overall": {"total": 1, "high": 0, "medium": 1, "protected": 0, "clean_eligible": 1},
                        "tv": {"total": 1, "high": 0, "medium": 1, "protected": 0, "clean_eligible": 1},
                        "movie": {"total": 0, "high": 0, "medium": 0, "protected": 0, "clean_eligible": 0},
                        "subscriptions": [{"id": 2558, "name": "侦讯实境", "media_type": "tv", "season": 4, "clean_confidence": "medium", "clean_eligible": True, "clean_rule": "stalled"}],
                    },
                )

                entries = await discover_source_season_entries(db, disk, sub_rec)
                # 只允许定位到受控源季目录，绝不含归档目录或整剧根目录
                self.assertEqual([e["path"] for e in entries], [src_season])

                # 拒绝显式传入归档目录或整剧根目录
                with self.assertRaises(HTTPException):
                    await preview(db, mp, disk, 2558, True, f"cd2:{arch_season}")
                with self.assertRaises(HTTPException):
                    await preview(db, mp, disk, 2558, True, f"cd2:{show_root}")

                plan = await preview(db, mp, disk, 2558, True, entries[0]["id"])
                self.assertEqual(len(plan["files"]), 1)
                self.assertEqual(plan["path"], src_season)

                out = await execute(db, mp, disk, plan["operation_id"])
                self.assertTrue(out["ok"])
                mp.delete_subscription.assert_awaited_once_with(2558)
                self.assertIn(vid_file["path"], [c.args[0] for c in disk.delete_file.await_args_list])

                # 执行后快照立即移除 2558，list_subscriptions(cached_only=True) 不再返回它
                after = await list_subscriptions(cached_only=True, db=db)
                self.assertEqual(after["total"], 0)
                self.assertEqual(after["subscriptions"], [])

    def test_13_frontend_text_unsubscribe_button_and_failed_banner(self):
        html = (Path(__file__).resolve().parent.parent / "frontend" / "index.html").read_text(encoding="utf-8")
        # 单条退订按钮改为文字按钮「取消订阅」，不再使用垃圾桶图标
        self.assertIn('title="取消此条订阅（绝不删除本地媒体文件）">取消订阅</button>', html)
        self.assertNotIn('title="取消此条订阅">\n                  <i class="fa-solid fa-trash-can"></i>', html)
        # 不再展示 MP 计数
        self.assertNotIn("MP 记录", html)
        self.assertNotIn("(MP记", html)
        # 统一查询异常状态显式提示
        self.assertIn("st.status === 'failed'", html)
        self.assertIn("查询最新订阅数据失败", html)
        # 智能动态切换：电视剧与电影分开独立查询按钮
        self.assertIn("查询最新电视剧订阅", html)
        self.assertIn("查询最新电影订阅", html)
        self.assertIn("switchMediaTab", html)

    async def test_14_separated_tv_and_movie_query_and_deep_clean_show_dir(self):
        from unittest.mock import AsyncMock, patch
        from app.database import async_session
        from app.services.subscription_service import preview, execute
        from app.services.subscription_cd2_index import save_cd2_snapshot
        from app.services.subscription_governance_service import (
            start_unified_subscription_query,
            get_unified_query_status,
        )

        # 1. 验证 unified-query API 接收 media_type="tv" 并正确反映在状态中
        st_tv = start_unified_subscription_query(
            async_session,
            media_type="tv",
            search_resources=False,
            reuse_resource_cache=True,
        )
        self.assertEqual(st_tv.get("media_type"), "tv")
        self.assertIn("电视剧", st_tv.get("stage_label", ""))

        # 2. 验证退订+彻底删源：删除文件 + 删除季目录 + 自动删除剧集空目录
        src_season = "/nas/电视剧/纪录片/测试剧 (2025) [tmdbid=99999]/Season 1"
        show_dir = "/nas/电视剧/纪录片/测试剧 (2025) [tmdbid=99999]"
        cat_dir = "/nas/电视剧/纪录片"
        sub_rec = {
            "id": 9999, "name": "测试剧", "type": "电视剧", "season": 1, "tmdbid": 99999,
            "total_episode": 1, "completed_episode": 1, "lack_episode": 0, "state": "R",
        }
        vid_file = {
            "name": "测试剧 - S01E01.mkv", "path": f"{src_season}/测试剧 - S01E01.mkv",
            "size": 1000, "modified": "2026-11-23T10:00:00Z", "id": "f1", "isDirectory": False,
        }
        nfo_file = {
            "name": "season.nfo", "path": f"{src_season}/season.nfo",
            "size": 200, "modified": "2026-11-23T10:00:00Z", "id": "f2", "isDirectory": False,
        }

        fake_settings = type("Settings", (), {
            "source_root": "/nas", "archive_root": "/nas.finish", "library_root": "/nas.media",
            "enable_multi_dir_governance": False,
        })()

        mp = AsyncMock()
        mp.list_subscriptions = AsyncMock(return_value=[sub_rec])
        mp.search_resources = AsyncMock(return_value=[])
        mp.get_active_downloads = AsyncMock(return_value=[])
        mp.get_transfer_queue = AsyncMock(return_value=[])
        mp.delete_subscription = AsyncMock(return_value={})

        deleted_paths = []
        async def mock_delete_file(p):
            deleted_paths.append(p)
            return {"success": True}

        # 模拟列出文件：季目录下有视频和 nfo；第一次列出剧目录时返回 Season 1，删除季目录后返回空列表
        disk = AsyncMock()
        disk.list_files = AsyncMock(side_effect=[
            [vid_file, nfo_file],          # preview 时读取季目录
            [vid_file, nfo_file],          # execute 校验季目录
            [],                            # 检查季目录残留文件（已删）
            [],                            # 检查剧集父目录（无其它季，为空）
            [],                            # 刷新分类目录
        ])
        disk.get_file_info = AsyncMock(side_effect=[vid_file, None, nfo_file, None])
        disk.delete_file = AsyncMock(side_effect=mock_delete_file)

        with patch("app.services.subscription_service.get_settings", return_value=fake_settings):
            async with async_session() as db:
                await save_cd2_snapshot(
                    db,
                    {
                        "version": 1, "synced_at": "2026-11-23 10:00:00",
                        "tv_scanned_ok": True, "movie_scanned_ok": True,
                        "tv_by_key": {
                            "99999:1": {
                                "season": 1, "source_count": 1, "archive_count": 0, "total_count": 1,
                                "merged_episodes": [1], "paths": [src_season],
                            }
                        },
                        "tv_by_title": {}, "movie_by_tmdb": {}, "movie_by_title": {},
                    },
                )
                plan = await preview(db, mp, disk, 9999, True, f"cd2:{src_season}")
                # 季内所有文件均纳入清理范围
                self.assertEqual(len(plan["files"]), 2)
                self.assertIn("彻底删除", plan["notice"])

                out = await execute(db, mp, disk, plan["operation_id"])
                self.assertTrue(out["ok"])
                self.assertTrue(out.get("show_dir_deleted"))
                self.assertIn("一并清理剧集空目录并刷新 CD2", out["message"])
                # 校验删除序列：删文件 -> 删季目录 -> 删剧目录
                self.assertIn(vid_file["path"], deleted_paths)
                self.assertIn(nfo_file["path"], deleted_paths)
                self.assertIn(src_season, deleted_paths)
                self.assertIn(show_dir, deleted_paths)

    def test_15_frontend_tab_switch_performance_guards(self):
        html = (Path(__file__).resolve().parent.parent / "frontend" / "index.html").read_text(encoding="utf-8")
        # 1. 清除阻塞 CSSOM 的字体 @import 与 Tailwind v3 非法类名
        self.assertNotIn("@import url('https://fonts.googleapis.com", html)
        self.assertNotIn("py-0.2", html)
        self.assertNotIn("py-0.8", html)
        self.assertNotIn("shadow-2xs", html)
        # 2. CSS 渲染隔离与首屏外视口跳过渲染（含回退）
        self.assertIn("sub-list-viewport", html)
        self.assertIn("content-visibility: auto", html)
        self.assertIn("@supports not (content-visibility: auto)", html)
        # 3. O(1) 预索引、原位切标签高亮、分批渲染与主线程让步
        self.assertIn("rebuildSubBucketIndex", html)
        self.assertIn("updateSubFilterActiveStyles", html)
        self.assertIn("SUB_LIST_INITIAL_BATCH", html)
        self.assertIn("renderMoreSubscriptions", html)
        self.assertIn("yieldToMain", html)
        # 4. 封面懒加载与 w185 缩略图 + 弹窗原位勾选同步 + 防双重刷新
        self.assertIn("_subPosterUrl", html)
        self.assertIn('loading="lazy"', html)
        self.assertIn('decoding="async"', html)
        self.assertIn("_syncModalCheckboxDom", html)
        self.assertIn("else if (!reloaded)", html)

    async def test_16_batch_delete_cleanup_source_and_label_rename(self):
        from unittest.mock import AsyncMock, patch
        from app.api.moviepilot import BatchDeleteSubscriptionsRequest, batch_delete_subscriptions
        from app.database import async_session
        from app.services.subscription_cd2_index import save_cd2_snapshot

        html = (Path(__file__).resolve().parent.parent / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn("🟣 疑似短剧", html)
        self.assertNotIn("微短剧停滞", html)
        self.assertIn("批量取消订阅+删源", html)
        self.assertIn("isBatchCleanupSourceMode", html)
        self.assertIn("cleanup_source: cleanupSource", html)

        src_season = "/nas/电视剧/国产剧/测试断更剧 (2025) [tmdbid=88881]/Season 1"
        show_dir = "/nas/电视剧/国产剧/测试断更剧 (2025) [tmdbid=88881]"
        vid_file = {"name": "测试断更剧 - S01E01.mkv", "path": f"{src_season}/测试断更剧 - S01E01.mkv", "size": 500, "isDirectory": False}
        nfo_file = {"name": "season.nfo", "path": f"{src_season}/season.nfo", "size": 80, "isDirectory": False}

        sub_with_src = {"id": 8801, "name": "测试断更剧", "type": "电视剧", "season": 1, "tmdbid": 88881, "total_episode": 12, "completed_episode": 1, "lack_episode": 11, "state": "P", "date": "2025-01-01", "last_update": "2025-02-01"}
        sub_zero_src = {"id": 8802, "name": "零进度测试剧", "type": "电视剧", "season": 1, "tmdbid": 88882, "total_episode": 12, "completed_episode": 0, "lack_episode": 12, "state": "P", "date": "2025-01-01", "last_update": "2025-02-01"}
        sub_completed = {"id": 8803, "name": "全季完结测试剧", "type": "电视剧", "season": 1, "tmdbid": 88883, "total_episode": 10, "completed_episode": 10, "lack_episode": 0, "state": "P", "date": "2025-01-01", "last_update": "2025-02-01"}

        fake_settings = type("Settings", (), {
            "mp_url": "http://mp.local", "mp_token": "tok",
            "source_root": "/nas", "archive_root": "/nas.finish", "library_root": "/nas.media",
            "enable_multi_dir_governance": False,
        })()

        # 场景 A: 全季完结 cleanup_source=False -> 仅退订，绝不删源
        mp_safe = AsyncMock()
        mp_safe.list_subscriptions = AsyncMock(return_value=[sub_completed])
        mp_safe.delete_subscription = AsyncMock(return_value={})
        mp_safe.close = AsyncMock()
        disk_safe = AsyncMock()
        disk_safe.delete_file = AsyncMock(return_value={"success": True})
        disk_safe.close = AsyncMock()

        with (
            patch("app.api.moviepilot._check_mp_configured"),
            patch("app.api.moviepilot.MoviePilotClient", return_value=mp_safe),
            patch("app.clients.clouddrive2.CloudDrive2Client", return_value=disk_safe),
        ):
            async with async_session() as db:
                res_safe = await batch_delete_subscriptions(
                    BatchDeleteSubscriptionsRequest(sub_ids=[8803], titles=["全季完结测试剧"], cleanup_source=False),
                    db=db,
                )
                self.assertTrue(res_safe.ok)
                self.assertIn("本地媒体文件完整保留", res_safe.message)
                mp_safe.delete_subscription.assert_awaited_once_with(8803)
                disk_safe.delete_file.assert_not_awaited()

        # 场景 B: 批量退订+删源 cleanup_source=True -> 有源删全季+剧空目录，无源安全仅退订
        mp_clean = AsyncMock()
        mp_clean.list_subscriptions = AsyncMock(return_value=[sub_with_src, sub_zero_src])
        mp_clean.delete_subscription = AsyncMock(return_value={})
        mp_clean.close = AsyncMock()

        deleted_paths = []
        async def _mock_del(p):
            deleted_paths.append(p)
            return {"success": True}

        disk_clean = AsyncMock()
        disk_clean.list_files = AsyncMock(side_effect=[
            [vid_file, nfo_file],  # 列出 Season 1
            [],                    # 刷新 show_dir -> 为空
            [],                    # 刷新分类目录
        ])
        disk_clean.delete_file = AsyncMock(side_effect=_mock_del)
        disk_clean.close = AsyncMock()

        with (
            patch("app.api.moviepilot._check_mp_configured"),
            patch("app.api.moviepilot.MoviePilotClient", return_value=mp_clean),
            patch("app.clients.clouddrive2.CloudDrive2Client", return_value=disk_clean),
            patch("app.services.subscription_service.get_settings", return_value=fake_settings),
        ):
            async with async_session() as db:
                await save_cd2_snapshot(
                    db,
                    {
                        "version": 1, "synced_at": "2026-11-24 10:00:00",
                        "tv_scanned_ok": True, "movie_scanned_ok": True,
                        "tv_by_key": {
                            "88881:1": {
                                "season": 1, "source_count": 1, "archive_count": 0, "total_count": 1,
                                "merged_episodes": [1], "paths": [src_season],
                            }
                        },
                        "tv_by_title": {}, "movie_by_tmdb": {}, "movie_by_title": {},
                    },
                )
                res_clean = await batch_delete_subscriptions(
                    BatchDeleteSubscriptionsRequest(
                        sub_ids=[8801, 8802],
                        titles=["测试断更剧", "零进度测试剧"],
                        cleanup_source=True,
                    ),
                    db=db,
                )
                self.assertTrue(res_clean.ok)
                self.assertIn("批量退订+删源完成：成功退订 2 项", res_clean.message)
                self.assertIn("彻底清理 1 个源季目录", res_clean.message)
                self.assertIn("移除 1 个空剧集目录", res_clean.message)
                self.assertIn("1 项源端原本无文件，已安全仅退订", res_clean.message)
                self.assertIn(vid_file["path"], deleted_paths)
                self.assertIn(nfo_file["path"], deleted_paths)
                self.assertIn(src_season, deleted_paths)
                self.assertIn(show_dir, deleted_paths)

    async def test_17_cd2_zero_episode_label_and_path_display(self):
        from datetime import datetime
        from app.core.subscription_cleaner import diagnose_tv_subscription
        from app.database import async_session
        from app.services.subscription_cd2_index import (
            enrich_subscriptions_with_cd2_and_cache,
            save_cd2_snapshot,
        )

        now = datetime(2026, 11, 24, 12, 0, 0)
        # 1. 疑似短剧且 CD2 实盘为 0 集：严禁出现「CD2 已收录」，必须明确「CD2 未收录」
        d_short_zero = diagnose_tv_subscription(
            {
                "id": 9101,
                "name": "重生的钿女",
                "year": "2026",
                "type": "电视剧",
                "season": 1,
                "total_episode": 99,
                "lack_episode": 99,
                "state": "R",
                "username": "豆瓣榜单",
                "date": "2026-09-01 00:00:00",
                "last_update": "2026-10-01 00:00:00",
                "cd2_verified": True,
                "cd2_source_episodes": 0,
                "cd2_archive_episodes": 0,
                "cd2_total_episodes": 0,
                "cd2_paths": [],
            },
            now=now,
        )
        self.assertEqual(d_short_zero.clean_rule, "short_tv_stalled")
        self.assertIn("CD2 未收录（0/99 集）", d_short_zero.clean_reason)
        self.assertNotIn("CD2 已收录", d_short_zero.clean_reason)

        # 2. 疑似短剧且 CD2 归档库实存 60 集：保留「CD2 已收录 60/99 集」
        d_short_arch = diagnose_tv_subscription(
            {
                "id": 9102,
                "name": "等一场巴黎的风",
                "year": "2026",
                "type": "电视剧",
                "season": 1,
                "total_episode": 99,
                "lack_episode": 39,
                "state": "R",
                "username": "豆瓣榜单",
                "date": "2026-01-01 00:00:00",
                "last_update": "2026-02-01 00:00:00",
                "cd2_verified": True,
                "cd2_source_episodes": 0,
                "cd2_archive_episodes": 60,
                "cd2_total_episodes": 60,
                "cd2_avg_video_mb": 12.9,
                "cd2_paths": ["/nas.finish/电视剧/国产剧/等一场巴黎的风 (2026) [tmdbid=314453]/Season 1"],
            },
            now=now,
        )
        self.assertEqual(d_short_arch.clean_rule, "short_tv_stalled")
        self.assertIn("CD2 已收录 60/99 集", d_short_arch.clean_reason)

        # 3. 零进度死种与异常特征 0 集：同样明确「CD2 未收录（实存 0 集）」
        d_zombie = diagnose_tv_subscription(
            {
                "id": 9103,
                "name": "普通老剧",
                "year": "2024",
                "type": "电视剧",
                "season": 1,
                "total_episode": 24,
                "lack_episode": 24,
                "state": "R",
                "username": "豆瓣榜单",
                "date": "2026-08-01 00:00:00",
                "last_update": "2026-09-01 00:00:00",
                "cd2_verified": True,
                "cd2_source_episodes": 0,
                "cd2_archive_episodes": 0,
                "cd2_total_episodes": 0,
                "cd2_paths": [],
            },
            now=now,
        )
        self.assertEqual(d_zombie.clean_rule, "zombie")
        self.assertIn("CD2 未收录（实存 0 集）", d_zombie.clean_reason)
        self.assertNotIn("CD2 已收录", d_zombie.clean_reason)

        # 4. cd2_meta 透传 source_root 与 archive_root
        async with async_session() as db:
            await save_cd2_snapshot(
                db,
                {
                    "version": 1,
                    "synced_at": "2026-11-24 12:00:00",
                    "source_root": "/115/Library_incs/nas",
                    "archive_root": "/115/Library_incs/nas.finish",
                    "tv_scanned_ok": True,
                    "movie_scanned_ok": True,
                    "tv_by_key": {},
                    "tv_by_title": {},
                    "movie_by_tmdb": {},
                    "movie_by_title": {},
                },
            )
            _, meta = await enrich_subscriptions_with_cd2_and_cache(db, [])
            self.assertEqual(meta.get("source_root"), "/115/Library_incs/nas")
            self.assertEqual(meta.get("archive_root"), "/115/Library_incs/nas.finish")

        # 5. 前端徽章、归档库提示与路径一键复制守卫
        html = (Path(__file__).resolve().parent.parent / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn("CD2 未收录", html)
        self.assertIn("仅在归档库", html)
        self.assertIn("退订后保留归档文件", html)
        self.assertIn("_classifyCd2Path", html)
        self.assertIn("_renderSubCd2PathsHtml", html)
        self.assertIn("copySubCd2Path", html)

    async def test_18_batch_gap_fill_progress_active_dedup_and_clear_transfer(self):
        from unittest.mock import AsyncMock, patch
        from app.database import async_session
        from app.services import subscription_governance_service as sgs

        # 1. 校验电视剧活跃下载队列匹配：按 TMDB+季精确匹配、忽略纯做种、不混淆季号
        active_list = [
            {"state": "downloading", "tmdb_id": 77001, "season": 1, "title": "已在途断更剧 S01"},
            {"state": "seeding", "tmdb_id": 77002, "season": 1, "title": "仅做种剧集 S01"},
            {"state": "downloading", "title": "喜剧之王单口季 S02E03.mkv"},
        ]
        self.assertTrue(sgs._is_tv_season_in_active_downloads(active_list, "77001", "已在途断更剧", 1))
        self.assertFalse(sgs._is_tv_season_in_active_downloads(active_list, "77001", "已在途断更剧", 2))
        self.assertFalse(sgs._is_tv_season_in_active_downloads(active_list, "77002", "仅做种剧集", 1))
        self.assertTrue(sgs._is_tv_season_in_active_downloads(active_list, "", "喜剧之王单口季", 2))
        self.assertFalse(sgs._is_tv_season_in_active_downloads(active_list, "", "喜剧之王单口季", 1))

        # 2. 校验批量补缺推送：无虚高起点偏置、跳过活跃队列条目、TV 推送成功后自动清 MP 转移记录
        fake_audit_id = "audit-test-v2571"
        sgs._audit_jobs[fake_audit_id] = {
            "items": [
                {
                    "id": 9201,
                    "name": "测试补缺剧A",
                    "media_type": "tv",
                    "tmdb_id": 77002,
                    "season": 1,
                    "audit_status": "downloadable",
                    "_selected_payloads": [
                        {"media_info": {"title": "测试补缺剧A"}, "torrent_info": {"title": "Test.A.S01.1080p"}}
                    ],
                },
                {
                    "id": 9202,
                    "name": "已在途断更剧",
                    "media_type": "tv",
                    "tmdb_id": 77001,
                    "season": 1,
                    "audit_status": "downloadable",
                    "_selected_payloads": [
                        {"media_info": {"title": "已在途断更剧"}, "torrent_info": {"title": "Active.S01.1080p"}}
                    ],
                },
            ]
        }

        progress_trace = []
        orig_update = sgs.update_batch_action_progress

        def _capture_progress(**kwargs):
            snap = orig_update(**kwargs)
            progress_trace.append((snap["processed"], snap["total"], snap["progress_pct"], snap["message"]))
            return snap

        mp_mock = AsyncMock()
        mp_mock.get_active_downloads = AsyncMock(return_value=active_list)
        mp_mock.submit_download = AsyncMock(return_value={"success": True, "message": "ok"})
        mp_mock.clear_transfer_history_for_episodes = AsyncMock(return_value={"cleared_count": 3, "records": []})
        mp_mock.close = AsyncMock()

        try:
            with (
                patch("app.services.subscription_governance_service.MoviePilotClient", return_value=mp_mock),
                patch("app.services.subscription_governance_service.update_batch_action_progress", side_effect=_capture_progress),
            ):
                async with async_session() as db:
                    res = await sgs.execute_batch_subscription_gap_fill(
                        db, audit_id=fake_audit_id, sub_ids=[9201, 9202]
                    )
            self.assertTrue(res["ok"])
            self.assertEqual(res["pushed_count"], 1)
            self.assertEqual(res["skipped_active_count"], 1)
            self.assertEqual(res["cleared_transfer_count"], 3)
            self.assertIn("同步清理 MP 媒体整理中 3 条历史转移记录", res["message"])
            self.assertIn("跳过 1 部已在活跃下载队列中", res["message"])
            mp_mock.submit_download.assert_awaited_once()
            mp_mock.clear_transfer_history_for_episodes.assert_awaited_once()
            # 确认进度百分比无 5%/8% 虚高：第 1 部推送前为 0%，完成后为 50%，全部完成为 100%
            pcts = [p[2] for p in progress_trace]
            self.assertEqual(pcts[0], 0)
            self.assertEqual(pcts[1], 0)
            self.assertIn(50, pcts)
            self.assertEqual(pcts[-1], 100)
        finally:
            sgs._audit_jobs.pop(fake_audit_id, None)

    async def test_19_downloadable_cache_ttl_and_fallback_live_retry(self):
        from datetime import datetime
        from unittest.mock import AsyncMock, patch
        from app.database import async_session
        from app.services import subscription_governance_service as sgs

        # 1. 校验 downloadable 缓存 4 小时 TTL 判定
        now = datetime(2026, 11, 24, 14, 0, 0)
        self.assertTrue(sgs._is_downloadable_cache_fresh({"checked_at": "2026-11-24 11:30:00"}, now=now))
        self.assertFalse(sgs._is_downloadable_cache_fresh({"checked_at": "2026-11-23 10:39:58"}, now=now))
        self.assertFalse(sgs._is_downloadable_cache_fresh({}, now=now))

        # 2. 校验推送失败后自动实时重搜：剧 A 换到馒头重试成功并清 MP 转移记录；剧 B 重搜无源记录站点报错并更新缓存
        fake_audit_id = "audit-test-v2572"
        sgs._audit_jobs[fake_audit_id] = {
            "items": [
                {
                    "id": 9301,
                    "name": "冬去春来",
                    "media_type": "tv",
                    "tmdb_id": 291470,
                    "season": 1,
                    "total_episodes": 32,
                    "missing_episodes": [31, 32],
                    "cd2_verified": True,
                    "audit_status": "downloadable",
                    "_selected_payloads": [
                        {
                            "media_info": {"title": "冬去春来"},
                            "torrent_info": {"site_name": "天空", "title": "Expired.HDSky.S01", "enclosure": "https://hdsky.me/download.php?id=605328&t=expired"},
                        }
                    ],
                },
                {
                    "id": 9302,
                    "name": "朱迪正义",
                    "media_type": "tv",
                    "tmdb_id": 114294,
                    "season": 4,
                    "total_episodes": 120,
                    "missing_episodes": [107],
                    "cd2_verified": True,
                    "audit_status": "downloadable",
                    "_selected_payloads": [
                        {
                            "media_info": {"title": "朱迪正义"},
                            "torrent_info": {"site_name": "天空", "title": "Expired.Judy.S04", "enclosure": "https://hdsky.me/download.php?id=637319&t=expired"},
                        }
                    ],
                },
            ]
        }

        async def _mock_submit(m_info, t_info):
            if "hdsky.me" in str(t_info.get("enclosure") or ""):
                return {"success": False, "message": "种子下载失败或链接已过期"}
            return {"success": True, "message": "已入列"}

        async def _mock_reaudit(sub, **kwargs):
            if int(sub.get("id") or 0) == 9301:
                return {
                    "id": 9301,
                    "name": "冬去春来",
                    "media_type": "tv",
                    "tmdb_id": 291470,
                    "season": 1,
                    "audit_status": "downloadable",
                    "plan_summary": "[馒头] 选定 1 个种子",
                    "selected_torrents": [{"site_name": "馒头", "title": "Fresh.MTeam.S01"}],
                    "checked_at": "2026-11-24 14:00:00",
                    "_selected_payloads": [
                        {
                            "media_info": {"title": "冬去春来"},
                            "torrent_info": {"site_name": "馒头", "title": "Fresh.MTeam.S01", "enclosure": "https://m-team.cc/dl?id=999"},
                        }
                    ],
                }
            return {
                "id": 9302,
                "name": "朱迪正义",
                "media_type": "tv",
                "tmdb_id": 114294,
                "season": 4,
                "audit_status": "no_resource",
                "reason": "全网补缺站点未搜到 S04 种子",
                "checked_at": "2026-11-24 14:00:00",
                "_selected_payloads": [],
            }

        mp_mock = AsyncMock()
        mp_mock.get_active_downloads = AsyncMock(return_value=[])
        mp_mock.submit_download = AsyncMock(side_effect=_mock_submit)
        mp_mock.clear_transfer_history_for_episodes = AsyncMock(return_value={"cleared_count": 2, "records": []})
        mp_mock.close = AsyncMock()
        tmdb_mock = AsyncMock()
        tmdb_mock.close = AsyncMock()

        try:
            with (
                patch("app.services.subscription_governance_service.MoviePilotClient", return_value=mp_mock),
                patch("app.services.subscription_governance_service.TMDBClient", return_value=tmdb_mock),
                patch("app.services.subscription_governance_service.resolve_category_gap_fill_sites", new=AsyncMock(return_value={"ready": True})),
                patch("app.services.subscription_governance_service._audit_single_subscription_item", new=AsyncMock(side_effect=_mock_reaudit)),
            ):
                async with async_session() as db:
                    res = await sgs.execute_batch_subscription_gap_fill(
                        db, audit_id=fake_audit_id, sub_ids=[9301, 9302]
                    )

            self.assertTrue(res["ok"])
            self.assertEqual(res["pushed_count"], 1)
            self.assertEqual(res["retried_success_count"], 1)
            self.assertEqual(res["failed_count"], 1)
            self.assertEqual(res["cleared_transfer_count"], 2)
            self.assertIn("缓存失效已自动重搜换种成功", res["message"])

            by_id = {d["sub_id"]: d for d in res["details"]}
            self.assertTrue(by_id[9301]["ok"])
            self.assertTrue(by_id[9301]["retried_live"])
            self.assertFalse(by_id[9302]["ok"])
            self.assertIn("天空", by_id[9302]["sites"])
            self.assertIn("种子下载失败或链接已过期", by_id[9302]["error"])
            self.assertIn("实时重搜结果", by_id[9302]["error"])

            # 前端报错透出函数守卫
            html = (Path(__file__).resolve().parent.parent / "frontend" / "index.html").read_text(encoding="utf-8")
            self.assertIn("_formatGapFillPushAlert", html)
            self.assertIn("失败明细：", html)
        finally:
            sgs._audit_jobs.pop(fake_audit_id, None)

    async def test_20_movie_gap_fill_blocks_homonym_empty_media_info_and_500_guard(self):
        import asyncio
        import json
        import httpx
        from unittest.mock import AsyncMock, patch
        from app.clients.moviepilot import MoviePilotClient
        from app.database import async_session
        from app.models.metadata_cache import MetadataCache
        from app.services import subscription_governance_service as sgs

        # 1. 验证 MoviePilotClient.submit_download 前置拦截空 media_info（避免发送 {} 触发 MP 500）及真实 HTTP 500 detail 透出
        mp_real = MoviePilotClient()
        try:
            with self.assertRaises(ValueError) as cm:
                await mp_real.submit_download({}, {"enclosure": "https://hdsky.me/dl?id=1"})
            self.assertIn("media_info 为空", str(cm.exception))

            req = httpx.Request("POST", "https://mp.test/api/v1/download/")
            resp_500 = httpx.Response(500, json={"detail": "downloader path missing"}, request=req)
            with self.assertRaises(httpx.HTTPStatusError) as cm_http:
                MoviePilotClient._raise_for_status_with_detail(resp_500)
            self.assertIn("downloader path missing", str(cm_http.exception))
            self.assertNotIn("developer.mozilla.org", str(cm_http.exception))
        finally:
            await mp_real.close()

        # 2. 验证电影关键词回退时严格拦截同名异年错片（大闹天宫 2021 != 1961，逃亡者 2023 != The Runner 2026，害群之马 1968 != 1996）
        wrong_cands = [
            {
                "media_info": {},
                "meta_info": {"en_name": "The Monkey King", "year": "1961"},
                "torrent_info": {"title": "The Monkey King 1961 WEB-DL 4K", "site": 15, "site_name": "天空", "seeders": 32, "size": 6 * 1024**3},
            },
            {
                "media_info": {},
                "meta_info": {"en_name": "The Runner", "year": "2026"},
                "torrent_info": {"title": "The Runner 2026 2160p WEB-DL", "site": 15, "site_name": "天空", "seeders": 21, "size": 9 * 1024**3},
            },
        ]
        best_wrong, _, rej_wrong = _select_best_movie_candidate(
            wrong_cands,
            tmdb_id_num=1063260,
            min_seeders=2,
            tier_ratio=0.10,
            min_slack=1,
            min_resolution="",
            require_chinese=False,
            allowed_site_ids={15},
            site_priority_map={15: 0},
            expected_year="2021",
            expected_titles=["大闹天宫", "The Macaque Monkey"],
            keyword_fallback=True,
        )
        self.assertIsNone(best_wrong)
        self.assertTrue(any("电影年份不符" in r for r in rej_wrong))

        # 3. 若关键词回退命中「同年份 + 同片名/别名」真种，自动通过 get_media_info 补齐 58 键 media_info 后放行
        sub_real = {
            "id": 9901, "name": "真命老片", "year": "2021", "type": "电影", "tmdbid": 555001,
            "state": "R", "username": "admin", "poster": "http://ok",
        }
        async def _fake_movie_search(tmdb_id=None, title=None, media_type="movie", **kwargs):
            if tmdb_id:
                return []
            return [
                {
                    "media_info": {},
                    "meta_info": {"en_name": "Wrong Old", "year": "1961"},
                    "torrent_info": {"title": "Wrong Old 1961 1080p", "enclosure": "https://pt/wrong", "site": 15, "site_name": "天空", "seeders": 50, "size": 5 * 1024**3},
                },
                {
                    "media_info": {},
                    "meta_info": {"en_name": "True Movie Alias", "year": "2021"},
                    "torrent_info": {"title": "True Movie Alias 2021 1080p BluRay", "enclosure": "https://pt/true", "site": 15, "site_name": "天空", "seeders": 12, "size": 8 * 1024**3},
                },
            ]

        mp_stub = AsyncMock()
        mp_stub.search_resources = AsyncMock(side_effect=_fake_movie_search)
        mp_stub.get_media_info = AsyncMock(return_value={
            "tmdb_id": 555001, "title": "真命老片", "en_title": "True Movie Alias",
            "original_title": "True Movie Alias", "year": "2021", "type": "电影",
        })
        tmdb_stub = AsyncMock()
        audited = await sgs._audit_single_subscription_item(
            sub_real,
            mp=mp_stub,
            tmdb=tmdb_stub,
            category_plans={"non_anime": {"ready": True, "sites_param": "15", "active_ordered_ids": [15], "site_priority_map": {15: 0}}},
            active_downloads=[],
            sem=asyncio.Semaphore(1),
            is_aborted_fn=lambda: False,
            min_seeders=2, tier_ratio=0.10, min_slack=1, min_resolution="", require_chinese=False,
        )
        self.assertEqual(audited["audit_status"], "downloadable")
        self.assertEqual(audited["selected_torrents"][0]["title"], "True Movie Alias 2021 1080p BluRay")
        self.assertEqual(audited["_selected_payloads"][0]["media_info"]["tmdb_id"], 555001)
        self.assertEqual(audited["_selected_payloads"][0]["media_info"]["title"], "真命老片")

        # 4. 验证 load_resource_audit_cache 自动自愈历史空 media_info 的脏缓存 + resolve_category_gap_fill_sites 重试参数顺序正确
        async with async_session() as db:
            dirty_cache = {
                "id:6725": {
                    "id": 6725, "name": "大闹天宫", "media_type": "movie", "tmdb_id": 1063260,
                    "audit_status": "downloadable", "checked_at": "2026-11-25 16:45:57",
                    "selected_torrents": [{"title": "The Monkey King 1961", "site_name": "天空", "seeders": 32}],
                    "_selected_payloads": [{"media_info": {}, "torrent_info": {"title": "The Monkey King 1961", "enclosure": "https://hdsky.me/dl"}}],
                }
            }
            db.add(MetadataCache(
                cache_key=sgs.UNIFIED_RESOURCE_CACHE_KEY,
                source="sub_resource_audit", media_id="global", season_number=0,
                payload_json=json.dumps(dirty_cache, ensure_ascii=False), verified=True, status="ok",
            ))
            await db.commit()
            sgs._memory_resource_cache = None

            healed = await sgs.load_resource_audit_cache(db)
            self.assertEqual(healed["id:6725"]["audit_status"], "no_resource")
            self.assertEqual(healed["id:6725"]["_selected_payloads"], [])
            res_blocked = await sgs.execute_batch_subscription_gap_fill(db, audit_id="unified", sub_ids=[6725])
            self.assertFalse(res_blocked["ok"])
            self.assertIn("同名异片", res_blocked["message"])

        # 5. 验证实时重搜回退调用 resolve_category_gap_fill_sites(db, mp, cat_key) 不再抛出 TypeError
        fake_tv_audit = "audit-test-retry-args"
        sgs._audit_jobs[fake_tv_audit] = {
            "items": [{
                "id": 9401, "name": "测试站点参数顺序", "media_type": "tv", "tmdb_id": 123456, "season": 1,
                "audit_status": "downloadable",
                "_selected_payloads": [{"media_info": {"title": "测试站点参数顺序"}, "torrent_info": {"site_name": "天空", "title": "T1", "enclosure": "https://pt/1"}}],
            }]
        }
        captured_site_calls = []
        async def _fake_resolve_sites(db_arg, mp_arg, cat_arg, instance_key=None):
            captured_site_calls.append((mp_arg, cat_arg))
            return {"ready": True, "sites_param": "15", "active_ordered_ids": [15], "site_priority_map": {15: 0}}
        mp_retry = AsyncMock()
        mp_retry.get_active_downloads = AsyncMock(return_value=[])
        mp_retry.submit_download = AsyncMock(return_value={"success": False, "message": "expired"})
        mp_retry.close = AsyncMock()
        try:
            with (
                patch("app.services.subscription_governance_service.MoviePilotClient", return_value=mp_retry),
                patch("app.services.subscription_governance_service.TMDBClient", return_value=AsyncMock(close=AsyncMock())),
                patch("app.services.subscription_governance_service.resolve_category_gap_fill_sites", side_effect=_fake_resolve_sites),
                patch("app.services.subscription_governance_service._audit_single_subscription_item", new=AsyncMock(return_value={"id": 9401, "audit_status": "no_resource", "reason": "none"})),
            ):
                async with async_session() as db:
                    await sgs.execute_batch_subscription_gap_fill(db, audit_id=fake_tv_audit, sub_ids=[9401])
            self.assertEqual([cat for _, cat in captured_site_calls], ["non_anime", "anime"])
            self.assertTrue(all(mp_arg is mp_retry for mp_arg, _ in captured_site_calls))
        finally:
            sgs._audit_jobs.pop(fake_tv_audit, None)

    async def test_21_unified_tv_movie_identity_and_media_info_guard(self):
        import asyncio
        from unittest.mock import AsyncMock
        from app.clients.moviepilot import is_valid_download_payload
        from app.services import download_service as ds
        from app.services import subscription_governance_service as sgs

        # 1. 验证 moviepilot 入口契约与订阅治理共用同一组载荷有效性断言
        self.assertIs(sgs._select_best_movie_candidate, ds.select_best_movie_candidate)
        self.assertFalse(is_valid_download_payload({}, {"enclosure": "https://pt/1"}))
        self.assertTrue(is_valid_download_payload({"tmdb_id": 100}, {"title": "Show.S01E01"}))

        # 2. 验证统一 verify_candidate_media_identity 在 TV 关键词回退下阻断同名异片/早期翻拍，放行严格同作
        tv_wrong_other = {
            "media_info": {},
            "meta_info": {"begin_season": 1, "season_list": [1], "episode_list": [3], "en_name": "Wrong Show"},
            "torrent_info": {"title": "Wrong Show S01E03 1080p", "site": 15, "site_name": "天空", "seeders": 12, "size": 1024**3},
        }
        tv_wrong_remake = {
            "media_info": {},
            "meta_info": {"begin_season": 1, "season_list": [1], "episode_list": [3], "year": "1990"},
            "torrent_info": {"title": "The Flash 1990 S01E03 1080p", "site": 15, "site_name": "天空", "seeders": 20, "size": 1024**3},
        }
        tv_true_empty_mi = {
            "media_info": {},
            "meta_info": {"begin_season": 1, "season_list": [1], "episode_list": [3], "year": "2014"},
            "torrent_info": {"title": "The Flash 2014 S01E03 1080p WEB-DL", "enclosure": "https://pt/flash-e3", "site": 15, "site_name": "天空", "seeders": 8, "size": 2 * 1024**3},
        }
        st_evals, _, rej_tv = ds._evaluate_raw_candidates(
            [tv_wrong_other, tv_wrong_remake, tv_true_empty_mi],
            tmdb_id_num=60735,
            season_num=1,
            total_eps=23,
            missing_eps=[3],
            by_period=False,
            parts_mode=False,
            part_episode_mapping=None,
            keyword_fallback=True,
            min_seeders=2,
            min_resolution="",
            require_chinese=False,
            allowed_site_ids={15},
            site_priority_map={15: 0},
            expected_titles=["闪电侠", "The Flash"],
            expected_year="2014",
        )
        self.assertEqual(len(st_evals), 1)
        self.assertEqual(st_evals[0].cand["torrent_info"]["title"], "The Flash 2014 S01E03 1080p WEB-DL")
        self.assertIn("作品身份不符", rej_tv)

        # 3. 续季跨年容差验证：2013 首播剧在 2025 年发布的 S09（关键词回退无 media_info）仍允许通过边界匹配
        tv_later_season = {
            "media_info": {},
            "meta_info": {"begin_season": 9, "season_list": [9], "episode_list": [10], "year": "2025"},
            "torrent_info": {"title": "Teen Titans Go 2025 S09E10 1080p WEB-DL", "enclosure": "https://pt/ttg-s09e10", "site": 15, "site_name": "天空", "seeders": 6, "size": 500 * 1024**2},
        }
        ok_s9, _, eff_s9 = ds.verify_candidate_media_identity(
            tv_later_season,
            media_type="tv",
            search_tmdb_id=45140,
            search_season=9,
            source_tmdb_id=45140,
            source_season=9,
            keyword_fallback=True,
            expected_titles=["少年泰坦出击", "Teen Titans Go!"],
            expected_year="2013",
        )
        self.assertTrue(ok_s9)
        self.assertEqual(eff_s9, 9)

        # 4. 订阅治理 TV 分支关键词回退时共用 get_media_info 别名扩充与完整 MediaInfo 回填
        sub_tv = {
            "id": 9951, "name": "闪电侠", "year": "2014", "type": "电视剧",
            "tmdbid": 60735, "season": 1, "total_episode": 4, "completed_episode": 2,
            "lack_episode": 2, "state": "R", "username": "admin", "poster": "http://ok",
            "cd2_verified": True, "cd2_episode_numbers": [1, 2, 4], "cd2_total_episodes": 3,
        }
        async def _fake_tv_search(tmdb_id=None, title=None, season=None, media_type="tv", **kwargs):
            if tmdb_id:
                return []
            return [tv_wrong_remake, tv_true_empty_mi]

        mp_tv = AsyncMock()
        mp_tv.search_resources = AsyncMock(side_effect=_fake_tv_search)
        mp_tv.get_media_info = AsyncMock(return_value={
            "tmdb_id": 60735, "title": "闪电侠", "en_title": "The Flash",
            "original_title": "The Flash", "year": "2014", "type": "电视剧",
        })
        tmdb_tv = AsyncMock()
        audited_tv = await sgs._audit_single_subscription_item(
            sub_tv,
            mp=mp_tv,
            tmdb=tmdb_tv,
            category_plans={"non_anime": {"ready": True, "sites_param": "15", "active_ordered_ids": [15], "site_priority_map": {15: 0}}},
            active_downloads=[],
            sem=asyncio.Semaphore(1),
            is_aborted_fn=lambda: False,
            min_seeders=2, tier_ratio=0.10, min_slack=1, min_resolution="", require_chinese=False,
        )
        self.assertEqual(audited_tv["audit_status"], "downloadable")
        self.assertEqual(audited_tv["selected_torrents"][0]["title"], "The Flash 2014 S01E03 1080p WEB-DL")
        self.assertEqual(audited_tv["_selected_payloads"][0]["media_info"]["tmdb_id"], 60735)
        self.assertEqual(audited_tv["_selected_payloads"][0]["media_info"]["title"], "闪电侠")


if __name__ == "__main__":
    unittest.main()
