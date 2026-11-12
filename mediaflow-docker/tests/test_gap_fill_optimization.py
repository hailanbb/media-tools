"""2026-11-12 补缺选种省容量、站点扩展、整季回退与已有集保护全场景验收测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MF_DATA_DIR", tempfile.mkdtemp(prefix="gap-fill-opt-"))

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clients.moviepilot import MoviePilotClient
from app.database import Base
from app.models.gap_fill import GapFillEpisode, GapFillSeasonRun
from app.models.operation import OperationLog
from app.models.season import SeasonRecord
from app.models.task import Task
from app.services import download_service
from app.services.download_service import (
    execute_gap_fill_download,
    resolve_gap_fill_search_sites,
    select_best_candidates,
    verify_season_pack_candidate,
)


def _cand(
    title: str,
    *,
    seeders: int,
    size: int | None,
    eps: list[int] | None = None,
    season: int = 3,
    tmdb_id: int = 42,
    site: int = 4,
    site_name: str = "彩虹岛",
    total_episode: int | None = None,
    description: str = "",
    files: list | None = None,
    episode_table: list | None = None,
    info_hash: str | None = None,
):
    torrent = {
        "title": title,
        "seeders": seeders,
        "size": size,
        "site": site,
        "site_name": site_name,
        "description": description,
    }
    if files is not None:
        torrent["files"] = files
    if episode_table is not None:
        torrent["episode_table"] = episode_table
    if info_hash:
        torrent["hash"] = info_hash
    meta = {
        "season_list": [season],
        "episode_list": list(eps) if eps is not None else [],
    }
    if total_episode is not None:
        meta["total_episode"] = total_episode
    return {
        "torrent_info": torrent,
        "media_info": {"tmdb_id": tmdb_id},
        "meta_info": meta,
    }


class GapFillOptimizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        download_service._active_downloads_cache = None
        download_service._active_downloads_cache_time = 0.0
        download_service._recently_submitted_tasks.clear()
        download_service._search_sites_cache.clear()

    async def asyncTearDown(self):
        await self.engine.dispose()

    def test_scenario_1_same_tier_prefers_smaller_bytes_order_independent(self):
        """场景 1：10 人做种 2 GiB 与 9 人做种 600 MiB 属同档，无论顺序一律选 600 MiB；未知/0大小靠后。"""
        big = _cand("Show.S03E01.1080p-Big", seeders=10, size=2 * 1024**3, eps=[1])
        small = _cand("Show.S03E01.1080p-Small", seeders=9, size=600 * 1024**2, eps=[1])
        unk_zero = _cand("Show.S03E01.1080p-Zero", seeders=10, size=0, eps=[1])
        unk_none = _cand("Show.S03E01.1080p-None", seeders=10, size=None, eps=[1])

        res_a = select_best_candidates([big, small, unk_zero, unk_none], [1], total=9, season=3)
        res_b = select_best_candidates([unk_none, small, big, unk_zero], [1], total=9, season=3)
        self.assertEqual(len(res_a), 1)
        self.assertEqual(res_a[0][0]["torrent_info"]["title"], "Show.S03E01.1080p-Small")
        self.assertEqual(res_b[0][0]["torrent_info"]["title"], "Show.S03E01.1080p-Small")

        # 仅有未知大小与零大小时依然可用，不抛异常
        res_u = select_best_candidates([unk_zero, unk_none], [1], total=9, season=3)
        self.assertEqual(len(res_u), 1)

    def test_scenario_2_wide_seeder_gap_prefers_high_seeder_tier(self):
        """场景 2：20 人做种 2 GiB 与 2 人做种 600 MiB 不属同档，优先 20 人做种方案。"""
        high = _cand("Show.S03E01.2160p-High", seeders=20, size=2 * 1024**3, eps=[1])
        low = _cand("Show.S03E01.1080p-Low", seeders=2, size=600 * 1024**2, eps=[1])
        res = select_best_candidates([low, high], [1], total=9, season=3)
        self.assertEqual(res[0][0]["torrent_info"]["title"], "Show.S03E01.2160p-High")

    def test_scenario_3_multi_episode_pack_beats_two_standalones(self):
        """场景 3：两个散集共 3 GiB，与同做种档连集包 2 GiB 均能补齐时选连集包，仅提交一次。"""
        ep2 = _cand("Show.S03E02.1080p", seeders=10, size=1536 * 1024**2, eps=[2])
        ep3 = _cand("Show.S03E03.1080p", seeders=10, size=1536 * 1024**2, eps=[3])
        pack = _cand("Show.S03E02-E03.1080p", seeders=9, size=2 * 1024**3, eps=[2, 3])
        res = select_best_candidates([ep2, ep3, pack], [2, 3], total=9, season=3)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0][0]["torrent_info"]["title"], "Show.S03E02-E03.1080p")
        self.assertEqual(res[0][1], [2, 3])
        self.assertFalse(res[0][2])

    async def test_site_resolution_all_modes_and_diagnostics(self):
        """站点动态解析：默认合并天空/观众；为空保留全站；读取失败不缩窄；停用/重名/缺失显式报告。"""
        mp = AsyncMock()
        mp.origin = "http://mp.test"
        mp.get_sites.return_value = [
            {"id": 15, "name": "天空", "domain": "hdsky.me", "is_active": True},
            {"id": 1, "name": "观众", "domain": "audiences.me", "is_active": True},
            {"id": 4, "name": "彩虹岛", "domain": "ptchdbits.co", "is_active": True},
            {"id": 5, "name": "听听歌", "domain": "totheglory.im", "is_active": True},
            {"id": 12, "name": "麒麟", "domain": "hdkyl.in", "is_active": True},
            {"id": 14, "name": "馒头", "domain": "m-team.cc", "is_active": True},
        ]
        mp.get_indexer_sites.return_value = {"ok": True, "value": [4, 14, 5, 12], "error": None}
        plan = await resolve_gap_fill_search_sites(mp)
        self.assertTrue(plan["scope_confirmed"])
        self.assertEqual(plan["sites_param"], [1, 4, 5, 12, 14, 15])

        # 默认为空代表全部有效索引站 -> 保留 sites_param=None，避免只传 [1, 15] 缩窄搜索
        mp.get_indexer_sites.return_value = {"ok": True, "value": [], "error": None}
        plan_all = await resolve_gap_fill_search_sites(mp)
        self.assertTrue(plan_all["scope_confirmed"])
        self.assertIsNone(plan_all["sites_param"])
        self.assertEqual(plan_all["mode"], "all_active")

        # 读取 IndexerSites 失败 -> scope_confirmed=False，sites_param=None 不缩窄
        mp.get_indexer_sites.return_value = {"ok": False, "value": None, "error": "TimeoutError"}
        plan_err = await resolve_gap_fill_search_sites(mp)
        self.assertFalse(plan_err["scope_confirmed"])
        self.assertIsNone(plan_err["sites_param"])
        self.assertTrue(any("未确认" in d for d in plan_err["diagnostics"]))

        # 重名 / 停用 / 缺失诊断
        mp.get_sites.return_value = [
            {"id": 15, "name": "天空", "domain": "hdsky.me", "is_active": False},
            {"id": 1, "name": "观众", "domain": "audiences.me", "is_active": True},
            {"id": 21, "name": "观众", "domain": "audiences.me", "is_active": True},
            {"id": 4, "name": "彩虹岛", "domain": "ptchdbits.co", "is_active": True},
        ]
        mp.get_indexer_sites.return_value = {"ok": True, "value": [4], "error": None}
        plan_diag = await resolve_gap_fill_search_sites(mp, extra_site_names=["天空", "观众", "不存在站"])
        self.assertEqual(plan_diag["sites_param"], [1, 4, 21])  # 停用的天空(15)不放入，两个启用的观众合并
        diag_text = " | ".join(plan_diag["diagnostics"])
        self.assertIn("已停用", diag_text)
        self.assertIn("重名", diag_text)
        self.assertIn("缺失", diag_text)

    async def test_scenario_4_5_6_season_fallback_sites_and_history_protection(self):
        """场景 4、5、6：六站请求、散集不全触发整季包与补搜、日志写明已有集、仅清理真实缺号冲突历史。"""
        async with self.Session() as db:
            db.add(Task(id="t1", name="测试任务", source_path="/qa/src", status="completed"))
            db.add(
                SeasonRecord(
                    id="rec1",
                    task_id="t1",
                    show_name="主厨对决 [tmdbid=42]",
                    tmdb_id="42",
                    show_path="/qa/src/主厨对决",
                    season_path="/qa/src/主厨对决/Season 3",
                    season_number=3,
                    total_episodes=9,
                    local_episode_count=6,
                    recognized_episodes_json="[1,4,5,6,7,8]",
                    missing_episodes_json="[2,3,9]",
                    status="download_needed",
                )
            )
            await db.commit()

            # 用真实 MoviePilotClient 验证 explicit_only 仅删除缺失集记录而保护已有集及整季记录
            mp_real = MoviePilotClient()
            deleted_ids: list[int] = []
            mp_real.get_all_transfer_history_for_title = AsyncMock(return_value=[
                {"id": 101, "title": "主厨对决", "seasons": "S03", "episodes": "E01", "dest": "/a/E01.mkv"},
                {"id": 102, "title": "主厨对决", "seasons": "S03", "episodes": "E09", "dest": "/a/E09.mkv"},
                {"id": 103, "title": "主厨对决", "seasons": "S03", "episodes": "E01-E09", "dest": "/a/pack"},
                {"id": 104, "title": "主厨对决", "seasons": "S03", "episodes": "", "dest": "/a/blank"},
            ])
            mp_real.delete_transfer_history = AsyncMock(side_effect=lambda rid, **kw: deleted_ids.append(rid) or True)

            mp_real.origin = "http://mp.live"
            mp_real.get_sites = AsyncMock(return_value=[
                {"id": 15, "name": "天空", "domain": "hdsky.me", "is_active": True},
                {"id": 1, "name": "观众", "domain": "audiences.me", "is_active": True},
                {"id": 4, "name": "彩虹岛", "domain": "ptchdbits.co", "is_active": True},
                {"id": 5, "name": "听听歌", "domain": "totheglory.im", "is_active": True},
                {"id": 12, "name": "麒麟", "domain": "hdkyl.in", "is_active": True},
                {"id": 14, "name": "馒头", "domain": "m-team.cc", "is_active": True},
            ])
            mp_real.get_indexer_sites = AsyncMock(return_value={"ok": True, "value": [4, 14, 5, 12], "error": None})
            mp_real.get_active_downloads = AsyncMock(return_value=[])
            mp_real.get_transfer_overwrite_policy = AsyncMock(
                return_value={"ok": True, "tv_modes": ["never"], "protects_existing": True, "summary": "TV目录覆盖策略=never"}
            )
            mp_real.get_media_info = AsyncMock(return_value={"original_title": "Chef Show"})
            mp_real.submit_download = AsyncMock(return_value={"success": True, "message": "ok"})

            # 场景 5：E02/E03 正常，E09 散集只有 1 人做种，整季包在天空 5 人做种（观众返回 0 条）
            e02 = _cand("Chef.S03E02.1080p", seeders=6, size=800 * 1024**2, eps=[2], site=4, site_name="彩虹岛")
            e03 = _cand("Chef.S03E03.1080p", seeders=6, size=800 * 1024**2, eps=[3], site=14, site_name="馒头")
            e09_low = _cand("Chef.S03E09.1080p", seeders=1, size=800 * 1024**2, eps=[9], site=5, site_name="听听歌")
            sp_sky = _cand(
                "Chef.S03.Complete.1080p",
                seeders=5,
                size=8 * 1024**3,
                eps=list(range(1, 10)),
                total_episode=9,
                description="全9集",
                site=15,
                site_name="天空",
            )
            mp_real.search_resources = AsyncMock(return_value=[e02, e03, e09_low, sp_sky])

            disk = AsyncMock()
            disk.list_files.return_value = []
            fresh = SimpleNamespace(status="download_needed", tmdb_id=42, season_path="/qa/src/主厨对决/Season 3",
                                    missing_episodes=[2, 3, 9], status_detail="缺2,3,9")
            with patch("app.services.download_service.MoviePilotClient", return_value=mp_real), \
                 patch("app.clients.clouddrive2.CloudDrive2Client", return_value=disk), \
                 patch("app.services.download_service.get_settings", return_value=SimpleNamespace(
                     mp_url="http://mp.live", mp_token="tok", source_root="/qa/src", archive_root="/qa/dst")), \
                 patch("app.core.inspector.run_inspection", new=AsyncMock(return_value=SimpleNamespace(seasons=[fresh]))):
                mp_real.close = AsyncMock()
                res5 = await execute_gap_fill_download(["rec1"], db)

            self.assertTrue(res5["ok"])
            self.assertEqual(len(res5["submitted"]), 1)
            self.assertTrue(res5["submitted"][0]["is_season_pack"])
            self.assertEqual(res5["submitted"][0]["site"], "天空")
            self.assertIn("为补 E02,E03,E09，下载整季 9 集", res5["submitted"][0]["runner_up_diff"])
            self.assertIn("其中 6 集已有", res5["submitted"][0]["runner_up_diff"])
            # 六站均进入请求（即便观众无返回结果也不代表未请求）
            self.assertEqual(res5["search_sites"]["requested_ids"], [1, 4, 5, 12, 14, 15])
            mp_real.search_resources.assert_awaited_once()
            self.assertEqual(mp_real.search_resources.call_args.kwargs.get("sites"), [1, 4, 5, 12, 14, 15])
            # 历史清理仅删除 E09(102)，完整保护 E01(101)、整季(103)、空集(104)！
            self.assertEqual(deleted_ids, [102])
            self.assertEqual(res5["submitted"][0]["protected_history_count"], 2)

            # 场景 6：首轮只有错季散集(S02) -> 自动用原名+季号补搜并带上同样六站，返回正确 S03 整季包
            download_service._recently_submitted_tasks.clear()
            await db.execute(delete(GapFillEpisode).where(GapFillEpisode.task_id == "t1"))
            await db.execute(delete(GapFillSeasonRun).where(GapFillSeasonRun.task_id == "t1"))
            await db.execute(delete(OperationLog).where(OperationLog.task_id == "t1"))
            await db.commit()
            wrong_s02 = _cand("Chef.S02E02.1080p", seeders=10, size=800 * 1024**2, eps=[2], season=2, site=4, site_name="彩虹岛")
            mp_real.search_resources = AsyncMock(side_effect=[[wrong_s02], [sp_sky]])
            mp_real.submit_download.reset_mock()
            with patch("app.services.download_service.MoviePilotClient", return_value=mp_real), \
                 patch("app.clients.clouddrive2.CloudDrive2Client", return_value=disk), \
                 patch("app.services.download_service.get_settings", return_value=SimpleNamespace(
                     mp_url="http://mp.live", mp_token="tok", source_root="/qa/src", archive_root="/qa/dst")), \
                 patch("app.core.inspector.run_inspection", new=AsyncMock(return_value=SimpleNamespace(seasons=[fresh]))):
                res6 = await execute_gap_fill_download(["rec1"], db)

            self.assertTrue(res6["ok"])
            self.assertEqual(mp_real.search_resources.await_count, 2)
            # 精确搜索与整季补搜均传六站
            for call in mp_real.search_resources.call_args_list:
                self.assertEqual(call.kwargs.get("sites"), [1, 4, 5, 12, 14, 15])
            self.assertEqual(res6["submitted"][0]["title"], "Chef.S03.Complete.1080p")

    async def test_scenario_7_rejects_ambiguous_partial_and_incomplete_variety_packs(self):
        """场景 7：仅标 S03 未知覆盖、“共9个文件”未证明集号、仅含前7集、或综艺包缺一期下段不当整季包；提交其他合格散集并列明缺号。"""
        bare_s03 = _cand("Show.S03.1080p.WEB-DL", seeders=10, size=8 * 1024**3, eps=[], total_episode=0, description="")
        nine_unknown_files = _cand(
            "Show.S03.Pack",
            seeders=10,
            size=8 * 1024**3,
            eps=[],
            files=[{"name": f"clip_{i}.mkv", "size": 500} for i in range(1, 10)],
        )
        partial_7_of_9 = _cand("Show.S03.WEB-DL", seeders=10, size=6 * 1024**3, eps=[], total_episode=7, description="全7集")

        for c in (bare_s03, nine_unknown_files, partial_7_of_9):
            ok, _, reason = verify_season_pack_candidate(c, season_num=3, total_eps=9)
            self.assertFalse(ok, f"应拒绝不完整或未知覆盖资源 {c['torrent_info']['title']}, 实际通过: {reason}")

        # 前导零 全08集 真实 PT 整季包（例如 Next Gen Chef 1080p）应核验通过
        leading_zero_pack = _cand("Show.S01.1080p.HDSWEB", seeders=2, size=14 * 1024**3, season=1, total_episode=8, description="综艺 全08集 [中字]")
        ok_lz, eps_lz, _ = verify_season_pack_candidate(leading_zero_pack, season_num=1, total_eps=8)
        self.assertTrue(ok_lz)
        self.assertEqual(eps_lz, set(range(1, 9)))

        # 按期综艺包缺第5期下段
        incomplete_variety = _cand(
            "Variety.S02.Complete",
            seeders=8,
            size=10 * 1024**3,
            eps=[],
            season=2,
            files=[
                {"name": f"Variety.S02.第{p}期{seg}.mp4", "size": 500}
                for p in (1, 2, 3, 4, 5)
                for seg in (("上",) if p == 5 else ("上", "下"))
            ],
        )
        ok_var, _, var_reason = verify_season_pack_candidate(incomplete_variety, season_num=2, total_eps=5, by_period=True)
        self.assertFalse(ok_var)
        self.assertIn("缺少分段", var_reason)

        # 合格散集 E02 仍可提交，E09 未覆盖明确列出
        e02_good = _cand("Show.S03E02.1080p", seeders=5, size=900 * 1024**2, eps=[2], season=3, tmdb_id=42)
        async with self.Session() as db:
            db.add(Task(id="t7", name="测试7", source_path="/qa/src", status="completed"))
            db.add(
                SeasonRecord(
                    id="rec7",
                    task_id="t7",
                    show_name="剧7 [tmdbid=42]",
                    tmdb_id="42",
                    show_path="/qa/src/剧7",
                    season_path="/qa/src/剧7/Season 3",
                    season_number=3,
                    total_episodes=9,
                    local_episode_count=7,
                    recognized_episodes_json="[1,3,4,5,6,7,8]",
                    missing_episodes_json="[2,9]",
                    status="download_needed",
                )
            )
            await db.commit()
            mp = AsyncMock()
            mp.get_active_downloads.return_value = []
            mp.get_media_info.return_value = {"original_title": "Show7"}
            mp.search_resources.side_effect = [[bare_s03, nine_unknown_files, partial_7_of_9, e02_good], []]
            mp.submit_download.return_value = {"success": True}
            mp.clear_transfer_history_for_episodes.return_value = {"cleared_count": 0, "records": [], "protected_count": 0}
            disk = AsyncMock()
            disk.list_files.return_value = []
            fresh = SimpleNamespace(status="download_needed", tmdb_id=42, season_path="/qa/src/剧7/Season 3",
                                    missing_episodes=[2, 9], status_detail="缺2,9")
            with patch("app.services.download_service.MoviePilotClient", return_value=mp), \
                 patch("app.clients.clouddrive2.CloudDrive2Client", return_value=disk), \
                 patch("app.services.download_service.get_settings", return_value=SimpleNamespace(
                     mp_url="http://mp.live", mp_token="tok", source_root="/qa/src", archive_root="/qa/dst")), \
                 patch("app.core.inspector.run_inspection", new=AsyncMock(return_value=SimpleNamespace(seasons=[fresh]))):
                res7 = await execute_gap_fill_download(["rec7"], db)

            self.assertTrue(res7["ok"])
            self.assertEqual(len(res7["submitted"]), 1)
            self.assertEqual(res7["submitted"][0]["title"], "Show.S03E02.1080p")
            self.assertTrue(any("未覆盖缺号 [9]" in s for s in res7["skip_reasons"]))

    async def test_scenario_8_cross_site_dedup_queue_overlap_and_timeout_check(self):
        """场景 8：跨站同种去重仅提交一次、同季活跃下载重叠暂缓整包、提交超时先查队列防重。"""
        async with self.Session() as db:
            db.add(Task(id="t8", name="测试8", source_path="/qa/src", status="completed"))
            db.add(
                SeasonRecord(
                    id="rec8",
                    task_id="t8",
                    show_name="剧8 [tmdbid=42]",
                    tmdb_id="42",
                    show_path="/qa/src/剧8",
                    season_path="/qa/src/剧8/Season 3",
                    season_number=3,
                    total_episodes=9,
                    local_episode_count=7,
                    recognized_episodes_json="[1,3,4,5,6,7,8]",
                    missing_episodes_json="[2,9]",
                    status="download_needed",
                )
            )
            await db.commit()

            # A. 跨站同种整季包仅提交一次，并保留两个站点来源；模拟超时后查队列已存在 -> 不重推、标记已入列
            c_sky = _cand("Show8.S03.Complete.1080p", seeders=8, size=5 * 1024**3, eps=list(range(1, 10)), total_episode=9, description="全9集", site=15, site_name="天空")
            c_aud = _cand("Show8.S03.Complete.1080p", seeders=9, size=5 * 1024**3, eps=list(range(1, 10)), total_episode=9, description="全9集", site=1, site_name="观众")
            mp = AsyncMock()
            q_calls = 0

            async def fake_active_downloads():
                nonlocal q_calls
                q_calls += 1
                if q_calls == 1:
                    return []
                return [{"title": "Show8.S03.Complete.1080p", "tmdb_id": 42, "season": 3, "episode": ""}]

            # 使用真实异步函数替代 Mock 使非 mock 模式生效，从而同步实测 _recently_submitted_tasks 防两批重复点击
            mp.get_active_downloads = fake_active_downloads
            mp.get_sites = AsyncMock()
            mp.get_indexer_sites = AsyncMock()
            mp.search_resources.return_value = [c_sky, c_aud]
            mp.submit_download.side_effect = TimeoutError("read timed out")
            mp.clear_transfer_history_for_episodes.return_value = {"cleared_count": 0, "records": [], "protected_count": 0}
            disk = AsyncMock()
            disk.list_files.return_value = []
            fresh = SimpleNamespace(status="download_needed", tmdb_id=42, season_path="/qa/src/剧8/Season 3",
                                    missing_episodes=[2, 9], status_detail="缺2,9")
            with patch("app.services.download_service.MoviePilotClient", return_value=mp), \
                 patch("app.clients.clouddrive2.CloudDrive2Client", return_value=disk), \
                 patch("app.services.download_service.get_settings", return_value=SimpleNamespace(
                     mp_url="http://mp.live", mp_token="tok", source_root="/qa/src", archive_root="/qa/dst")), \
                 patch("app.core.inspector.run_inspection", new=AsyncMock(return_value=SimpleNamespace(seasons=[fresh]))):
                res_a = await execute_gap_fill_download(["rec8"], db)

            self.assertTrue(res_a["ok"], res_a)
            self.assertEqual(mp.submit_download.await_count, 1)
            self.assertEqual(set(res_a["submitted"][0]["site_sources"]), {"观众(9)", "天空(8)"})
            self.assertTrue(res_a["submitted"][0]["mp_accepted"])
            self.assertFalse(res_a["submitted"][0]["library_reconciled"])
            self.assertIn("已确认入列", res_a["submitted"][0]["message"])

            # 紧接着第二批重复点击：即便清空远端队列返回 []，本地待确认清单仍拦截，不再重复搜索与提交
            mp.search_resources.reset_mock()
            mp.submit_download.reset_mock()
            download_service._active_downloads_cache = []
            download_service._active_downloads_cache_time = download_service.time.time()
            with patch("app.services.download_service.MoviePilotClient", return_value=mp), \
                 patch("app.clients.clouddrive2.CloudDrive2Client", return_value=disk), \
                 patch("app.services.download_service.get_settings", return_value=SimpleNamespace(
                     mp_url="http://mp.live", mp_token="tok", source_root="/qa/src", archive_root="/qa/dst")), \
                 patch("app.core.inspector.run_inspection", new=AsyncMock(return_value=SimpleNamespace(seasons=[fresh]))):
                res_repeat = await execute_gap_fill_download(["rec8"], db)
            self.assertTrue(res_repeat["skipped"], res_repeat)
            mp.search_resources.assert_not_awaited()

            # B. 目标季已有活跃下载 E02，缺失 [2, 9]，仅剩 E09 但只有整季包 -> 存在文件重叠，暂缓整包不重复提交
            download_service._recently_submitted_tasks.clear()
            download_service._active_downloads_cache = None
            await db.execute(delete(GapFillEpisode).where(GapFillEpisode.task_id == "t8"))
            await db.execute(delete(GapFillSeasonRun).where(GapFillSeasonRun.task_id == "t8"))
            await db.execute(delete(OperationLog).where(OperationLog.task_id == "t8"))
            await db.commit()
            mp.get_active_downloads = AsyncMock(return_value=[{"tmdb_id": 42, "season": 3, "episode": "E02", "title": "Show8"}])
            sp_only = _cand(
                "Show8.S03.Complete.1080p",
                seeders=5,
                size=8 * 1024**3,
                eps=list(range(1, 10)),
                total_episode=9,
                description="全9集",
            )
            mp.search_resources.side_effect = [[sp_only], []]
            with patch("app.services.download_service.MoviePilotClient", return_value=mp), \
                 patch("app.clients.clouddrive2.CloudDrive2Client", return_value=disk), \
                 patch("app.services.download_service.get_settings", return_value=SimpleNamespace(
                     mp_url="http://mp.live", mp_token="tok", source_root="/qa/src", archive_root="/qa/dst")), \
                 patch("app.core.inspector.run_inspection", new=AsyncMock(return_value=SimpleNamespace(seasons=[fresh]))):
                res_overlap = await execute_gap_fill_download(["rec8"], db)

            self.assertTrue(res_overlap["skipped"], res_overlap)
            self.assertIn("暂缓整包", res_overlap["message"])
            mp.submit_download.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
