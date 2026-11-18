"""v2.5.38 真缺集待补模块全场景验收测试 (D01 - D09)。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.archive import DeleteSourceSeasonRequest, delete_source_season, preview_delete_source_season
from app.api.moviepilot import _GapFillAbortRequest, mark_gap_fill_aborted
from app.core.inspector import run_inspection
from app.database import Base
from app.models.gap_fill import GapFillEpisode
from app.models.season import SeasonRecord
from app.models.task import Task
from app.services.download_service import _extract_standalone_episodes, execute_gap_fill_download
from app.services.gap_fill_tracker import (
    build_identity_key,
    compute_season_gap_fill_view,
    reconcile_gap_fill_records,
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


class TestV2538GapFillModule(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_d01_variety_multi_period_range_and_skip_ambiguous_candidate(self):
        """D01: 《一路繁花》S02 合格资源覆盖第 7-10 期可直接选种，仅个别含混候选被跳过。"""
        missing_periods = [7, 8, 9, 10]
        ambiguous_cand = {
            "torrent_info": {"title": "一路繁花 第2季 第7期(上) 2025 1080p WEB-DL"},
            "meta_info": {"season_list": [2]},
            "media_info": {"tmdb_id": 250001},
        }
        derivative_cand = {
            "torrent_info": {"title": "一路繁花 第2季 第8期纯享版 2025 1080p WEB-DL"},
            "meta_info": {"season_list": [2]},
            "media_info": {"tmdb_id": 250001},
        }
        qualified_range_cand = {
            "torrent_info": {"title": "一路繁花 第2季 第7-10期 2025 1080p WEB-DL H264"},
            "meta_info": {"season_list": [2]},
            "media_info": {"tmdb_id": 250001},
        }

        self.assertEqual(
            _extract_standalone_episodes(ambiguous_cand, 2, missing_periods, by_period=True, tmdb_id_num=250001),
            [],
        )
        self.assertEqual(
            _extract_standalone_episodes(derivative_cand, 2, missing_periods, by_period=True, tmdb_id_num=250001),
            [],
        )
        self.assertEqual(
            _extract_standalone_episodes(qualified_range_cand, 2, missing_periods, by_period=True, tmdb_id_num=250001),
            [7, 8, 9, 10],
        )

    async def test_d02_conflicting_totals_independent_gap_fill_and_tail_basis(self):
        """D02: 《24小时主厨生死门》S03 两来源总数冲突（8 vs 9 且 E09 排播未证实）时，1..8 缺号可独立补，E09 给出明确暂缓原因。"""
        show_path = "/115/综艺/24小时主厨生死门 (2024) [tmdbid=240003]"
        season_path = f"{show_path}/Season 3"
        disk = AsyncMock()

        async def _list_files(path, force_refresh=False):
            if path == show_path:
                return [{"name": "Season 3", "path": season_path, "isDirectory": True}]
            if path == season_path:
                return [
                    {"name": f"24.Hours.S03E{i:02d}.1080p.mkv", "path": f"{season_path}/24.Hours.S03E{i:02d}.1080p.mkv", "size": 500, "isDirectory": False}
                    for i in (1, 2, 4, 5, 6, 7, 8)
                ]
            return []

        disk.list_files.side_effect = _list_files
        mp = AsyncMock()
        mp.search_media.return_value = [{"id": "36990001", "title": "24小时主厨生死门 第三季"}]
        mp.get_douban_media_info.return_value = {
            "douban_id": "36990001",
            "title": "24小时主厨生死门 第三季",
            "season": 3,
            "number_of_episodes": 8,
            "year": "2024",
        }
        with patch(
            "app.core.inspector._fetch_seasons_by_tmdb_id",
            new=AsyncMock(
                return_value={
                    3: {
                        "total": 9,
                        "finished": False,
                        "partial_listing": True,
                        "season_year": "2024",
                        "broadcast_evidence": {
                            "status": "evidence_pending",
                            "missing_evidence_reason": "TMDB 标注 9 集但 E09 无排播日期",
                        },
                    }
                }
            ),
        ):
            res = await run_inspection("task_d02", show_path, cd2_client=disk, mp_client=mp, tmdb_client=AsyncMock())

        self.assertEqual(len(res.seasons), 1)
        s3 = res.seasons[0]
        self.assertEqual(s3.status, "download_needed")
        self.assertEqual(s3.missing_episodes, [3])
        conflict_meta = (s3.evidence or {}).get("source_total_conflict") or {}
        self.assertTrue(conflict_meta.get("tail_deferred"))
        self.assertEqual(conflict_meta.get("disputed_tail_episodes"), [9])
        self.assertIn("E09", conflict_meta.get("tail_basis", ""))

    async def test_d03_already_archived_blocked_from_gap_fill(self):
        """D03: 《怪奇物语》S05 归档已齐（already_archived）不进入待补，调用补缺自动跳过。"""
        async with self.Session() as db:
            task = Task(id="t_d03", name="t_d03", source_path="/src", status="completed")
            rec = SeasonRecord(
                id="rec_stranger_s5",
                task_id="t_d03",
                show_name="怪奇物语 (2016) [tmdbid=66732]",
                show_path="/src/怪奇物语 (2016) [tmdbid=66732]",
                season_number=5,
                season_path="/src/怪奇物语 (2016) [tmdbid=66732]/Season 5",
                tmdb_id="66732",
                local_episode_count=1,
                recognized_episodes_json=json.dumps(list(range(1, 9))),
                missing_episodes_json="[]",
                duplicate_episodes_json="[]",
                total_episodes=8,
                season_finished=True,
                status="already_archived",
                status_detail="归档已有全 8 集正片，源端本季重复保留",
            )
            db.add_all([task, rec])
            await db.commit()

            with (
                patch("app.services.download_service.get_settings", side_effect=_mock_settings),
                patch("app.clients.moviepilot.MoviePilotClient.get_active_downloads", new=AsyncMock(return_value=[])),
                patch("app.services.download_service.resolve_gap_fill_search_sites", new=AsyncMock(return_value={"sites_param": [4], "id_to_name": {4: "彩虹岛"}, "requested_label": "4:彩虹岛", "requested_ids": [4], "scope_confirmed": True, "mode": "explicit_merged", "diagnostics": []})),
                patch("app.clients.moviepilot.MoviePilotClient.close", new=AsyncMock()),
            ):
                out = await execute_gap_fill_download(["rec_stranger_s5"], db)

            self.assertFalse(out["ok"])
            self.assertTrue(out["skipped"])
            view = await compute_season_gap_fill_view(db, rec)
            self.assertEqual(view["view_bucket"], "none")

    async def test_d04_partial_push_and_exact_low_seeder_per_episode_reason(self):
        """D04: 《菜鸟老警》S03 缺 E03,E07,E09；E03,E07 推送成功，E09 仅 1 人做种留在待补并写明候选数与最高做种数。"""
        async with self.Session() as db:
            task = Task(id="t_d04", name="t_d04", source_path="/src", status="completed")
            rec = SeasonRecord(
                id="rec_rookie_s3",
                task_id="t_d04",
                show_name="菜鸟老警 (2018) [tmdbid=79744]",
                show_path="/src/菜鸟老警 (2018) [tmdbid=79744]",
                season_number=3,
                season_path="/src/菜鸟老警 (2018) [tmdbid=79744]/Season 3",
                tmdb_id="79744",
                local_episode_count=11,
                recognized_episodes_json=json.dumps([1, 2, 4, 5, 6, 8, 10, 11, 12, 13, 14]),
                missing_episodes_json=json.dumps([3, 7, 9]),
                duplicate_episodes_json="[]",
                total_episodes=14,
                season_finished=True,
                status="download_needed",
                status_detail="缺第 3, 7, 9 集",
            )
            db.add_all([task, rec])
            await db.commit()

            fake_fresh = type("Fresh", (), {
                "season_path": rec.season_path,
                "status": "download_needed",
                "tmdb_id": "79744",
                "status_detail": "缺第 3, 7, 9 集",
                "missing_episodes": [3, 7, 9],
                "mapping": None,
                "part_episode_mapping": None,
            })()
            fake_insp = type("Insp", (), {"seasons": [fake_fresh]})()

            cands = [
                {
                    "torrent_info": {"title": "The.Rookie.S03E03.1080p.WEB-DL.mkv", "seeders": 6, "size": 1500000000, "site_name": "彩虹岛", "site": 4},
                    "meta_info": {"season_list": [3], "episode_list": [3]},
                    "media_info": {"tmdb_id": 79744},
                },
                {
                    "torrent_info": {"title": "The.Rookie.S03E07.1080p.WEB-DL.mkv", "seeders": 5, "size": 1500000000, "site_name": "彩虹岛", "site": 4},
                    "meta_info": {"season_list": [3], "episode_list": [7]},
                    "media_info": {"tmdb_id": 79744},
                },
                {
                    "torrent_info": {"title": "The.Rookie.S03E09.1080p.WEB-DL.GroupA.mkv", "seeders": 1, "size": 1500000000, "site_name": "彩虹岛", "site": 4},
                    "meta_info": {"season_list": [3], "episode_list": [9]},
                    "media_info": {"tmdb_id": 79744},
                },
                {
                    "torrent_info": {"title": "The.Rookie.S03E09.1080p.WEB-DL.GroupB.mkv", "seeders": 1, "size": 1400000000, "site_name": "天空", "site": 5},
                    "meta_info": {"season_list": [3], "episode_list": [9]},
                    "media_info": {"tmdb_id": 79744},
                },
                {
                    "torrent_info": {"title": "The.Rookie.S03E09.720p.HDTV.mkv", "seeders": 0, "size": 900000000, "site_name": "观众", "site": 6},
                    "meta_info": {"season_list": [3], "episode_list": [9]},
                    "media_info": {"tmdb_id": 79744},
                },
            ]

            with (
                patch("app.services.download_service.get_settings", side_effect=_mock_settings),
                patch("app.clients.clouddrive2.CloudDrive2Client.list_files", new=AsyncMock(return_value=[])),
                patch("app.clients.clouddrive2.CloudDrive2Client.close", new=AsyncMock()),
                patch("app.core.inspector.run_inspection", new=AsyncMock(return_value=fake_insp)),
                patch("app.clients.moviepilot.MoviePilotClient.get_active_downloads", new=AsyncMock(return_value=[])),
                patch("app.services.download_service.resolve_gap_fill_search_sites", new=AsyncMock(return_value={"sites_param": [4], "id_to_name": {4: "彩虹岛"}, "requested_label": "4:彩虹岛", "requested_ids": [4], "scope_confirmed": True, "mode": "explicit_merged", "diagnostics": []})),
                patch("app.clients.moviepilot.MoviePilotClient.search_resources", new=AsyncMock(return_value=cands)),
                patch("app.clients.moviepilot.MoviePilotClient.clear_transfer_history_for_episodes", new=AsyncMock(return_value={"cleared_count": 0, "records": [], "protected_count": 11})),
                patch("app.clients.moviepilot.MoviePilotClient.submit_download", new=AsyncMock(return_value={"success": True, "download_id": "dl-rookie"})),
                patch("app.clients.moviepilot.MoviePilotClient.close", new=AsyncMock()),
            ):
                out = await execute_gap_fill_download(["rec_rookie_s3"], db, batch_run_id="batch-d04")

            self.assertTrue(out["ok"])
            view = await compute_season_gap_fill_view(db, rec)
            self.assertEqual(view["view_bucket"], "todo")
            self.assertEqual(view["pushed_episodes"], [3, 7])
            self.assertEqual(view["actionable_missing_episodes"], [9])
            ep9_row = next(e for e in view["episodes"] if e["episode_number"] == 9)
            self.assertEqual(ep9_row["outcome"], "low_seeders")
            self.assertIn("E09 找到 3 个候选，最高 1 人做种，本次要求至少 2 人", ep9_row["sanitized_reason"])

    async def test_d05_d06_d07_pack_fallback_abort_and_timeout_unverified(self):
        """D05/D06/D07: 缺 2 集无散集回退整季包且仅清缺号记录；批量中止标记未启动项；超时转提交待确认。"""
        async with self.Session() as db:
            task = Task(id="t_d567", name="t_d567", source_path="/src", status="completed")
            rec_pack = SeasonRecord(
                id="rec_pack",
                task_id="t_d567",
                show_name="整包测试剧 (2024) [tmdbid=55001]",
                show_path="/src/整包测试剧 (2024) [tmdbid=55001]",
                season_number=1,
                season_path="/src/整包测试剧 (2024) [tmdbid=55001]/Season 1",
                tmdb_id="55001",
                local_episode_count=8,
                recognized_episodes_json=json.dumps([1, 4, 5, 6, 7, 8, 9, 10]),
                missing_episodes_json=json.dumps([2, 3]),
                duplicate_episodes_json="[]",
                total_episodes=10,
                season_finished=True,
                status="download_needed",
                status_detail="缺第 2, 3 集",
            )
            rec_unstarted = SeasonRecord(
                id="rec_unstarted",
                task_id="t_d567",
                show_name="未启动剧 (2024) [tmdbid=55002]",
                show_path="/src/未启动剧 (2024) [tmdbid=55002]",
                season_number=1,
                season_path="/src/未启动剧 (2024) [tmdbid=55002]/Season 1",
                tmdb_id="55002",
                local_episode_count=5,
                recognized_episodes_json=json.dumps([1, 2, 3, 4, 5]),
                missing_episodes_json=json.dumps([6]),
                duplicate_episodes_json="[]",
                total_episodes=6,
                season_finished=True,
                status="download_needed",
                status_detail="缺第 6 集",
            )
            db.add_all([task, rec_pack, rec_unstarted])
            await db.commit()

            fake_fresh = type("Fresh", (), {
                "season_path": rec_pack.season_path,
                "status": "download_needed",
                "tmdb_id": "55001",
                "status_detail": "缺第 2, 3 集",
                "missing_episodes": [2, 3],
                "mapping": None,
                "part_episode_mapping": None,
            })()
            fake_insp = type("Insp", (), {"seasons": [fake_fresh]})()
            pack_cand = [
                {
                    "torrent_info": {"title": "PackShow.S01.E01-E10.Complete.1080p.WEB-DL.mkv", "seeders": 9, "size": 12000000000, "site_name": "彩虹岛", "site": 4},
                    "meta_info": {"season_list": [1], "begin_episode": 1, "end_episode": 10},
                    "media_info": {"tmdb_id": 55001},
                }
            ]
            clear_mock = AsyncMock(return_value={"cleared_count": 2, "records": ["E02", "E03"], "protected_count": 8})

            with (
                patch("app.services.download_service.get_settings", side_effect=_mock_settings),
                patch("app.clients.clouddrive2.CloudDrive2Client.list_files", new=AsyncMock(return_value=[])),
                patch("app.clients.clouddrive2.CloudDrive2Client.close", new=AsyncMock()),
                patch("app.core.inspector.run_inspection", new=AsyncMock(return_value=fake_insp)),
                patch("app.clients.moviepilot.MoviePilotClient.get_active_downloads", new=AsyncMock(return_value=[])),
                patch("app.services.download_service.resolve_gap_fill_search_sites", new=AsyncMock(return_value={"sites_param": [4], "id_to_name": {4: "彩虹岛"}, "requested_label": "4:彩虹岛", "requested_ids": [4], "scope_confirmed": True, "mode": "explicit_merged", "diagnostics": []})),
                patch("app.clients.moviepilot.MoviePilotClient.search_resources", new=AsyncMock(return_value=pack_cand)),
                patch("app.clients.moviepilot.MoviePilotClient.clear_transfer_history_for_episodes", new=clear_mock),
                patch("app.clients.moviepilot.MoviePilotClient.submit_download", new=AsyncMock(side_effect=TimeoutError("MP submit timed out"))),
                patch("app.clients.moviepilot.MoviePilotClient.close", new=AsyncMock()),
            ):
                out = await execute_gap_fill_download(["rec_pack"], db, batch_run_id="batch-d567")

            self.assertTrue(out["ok"])
            clear_mock.assert_awaited_once()
            self.assertEqual(clear_mock.await_args.kwargs.get("episodes"), {2, 3})
            self.assertTrue(clear_mock.await_args.kwargs.get("explicit_only"))
            self.assertEqual(out["submitted"][0]["already_owned_count"], 8)
            self.assertTrue(out["submitted"][0]["submitted_unverified"])

            view_pack = await compute_season_gap_fill_view(db, rec_pack)
            self.assertEqual(view_pack["view_bucket"], "pushed_pending")
            self.assertTrue(view_pack["only_unverified_pending"])
            self.assertEqual(view_pack["unverified_count"], 2)

            # D06: 标记未开始的 rec_unstarted 为 aborted_unstarted
            abort_res = await mark_gap_fill_aborted(
                _GapFillAbortRequest(task_id="t_d567", season_record_ids=["rec_unstarted"], batch_run_id="batch-d567"),
                db=db,
            )
            self.assertEqual(abort_res["marked_count"], 1)
            view_unstarted = await compute_season_gap_fill_view(db, rec_unstarted)
            self.assertEqual(view_unstarted["view_bucket"], "todo")
            self.assertEqual(view_unstarted["last_outcome"], "aborted_unstarted")

    async def test_d08_d09_reconcile_and_abandon_delete_source_season(self):
        """D08/D09: 单次队列未命中防重，连续未命中释放回待补；放弃补缺删除源端本季预览与确认闭环。"""
        async with self.Session() as db:
            task = Task(id="t_d08", name="t_d08", source_path="/src", status="completed")
            rec = SeasonRecord(
                id="rec_d08",
                task_id="t_d08",
                show_name="测试剧 (2024) [tmdbid=88001]",
                show_path="/src/测试剧 (2024) [tmdbid=88001]",
                season_number=1,
                season_path="/src/测试剧 (2024) [tmdbid=88001]/Season 1",
                tmdb_id="88001",
                local_episode_count=6,
                recognized_episodes_json=json.dumps([1, 2, 3, 4, 5, 6]),
                missing_episodes_json=json.dumps([7, 8]),
                duplicate_episodes_json="[]",
                total_episodes=8,
                season_finished=True,
                status="download_needed",
                status_detail="缺第 7, 8 集",
            )
            id_key = build_identity_key(rec.show_name, rec.tmdb_id, rec.season_number, None)
            past_dt = datetime.now(timezone.utc) - timedelta(minutes=10)
            ep7 = GapFillEpisode(
                task_id="t_d08",
                season_record_id="rec_d08",
                season_path=rec.season_path,
                identity_key=id_key,
                show_name=rec.show_name,
                tmdb_id=88001,
                season_number=1,
                episode_number=7,
                state="queued",
                outcome_code="accepted",
                reason="已推送",
                torrent_title="TestShow.S01E07.1080p.mkv",
                torrent_hash="hash_ep7",
                created_at=past_dt,
                updated_at=past_dt,
            )
            ep8 = GapFillEpisode(
                task_id="t_d08",
                season_record_id="rec_d08",
                season_path=rec.season_path,
                identity_key=id_key,
                show_name=rec.show_name,
                tmdb_id=88001,
                season_number=1,
                episode_number=8,
                state="queued",
                outcome_code="accepted",
                reason="已推送",
                torrent_title="TestShow.S01E08.1080p.mkv",
                torrent_hash="hash_ep8",
                created_at=past_dt,
                updated_at=past_dt,
            )
            db.add_all([task, rec, ep7, ep8])
            await db.commit()

            mp = AsyncMock()
            mp.get_active_downloads.return_value = []
            mp.get_transfer_queue.return_value = []
            mp.get_download_history.return_value = []
            mp.get_transfer_history.return_value = [
                {"title": "TestShow.S01E08.1080p.mkv", "download_hash": "hash_ep8", "status": True}
            ]
            cd2 = AsyncMock()
            cd2.list_files.return_value = [
                {"name": f"TestShow.S01E{i:02d}.mkv", "path": f"{rec.season_path}/TestShow.S01E{i:02d}.mkv", "size": 1000, "isDirectory": False}
                for i in range(1, 7)
            ]

            sum1 = await reconcile_gap_fill_records(
                db=db, task_id="t_d08", mp_client=mp, cd2_client=cd2, force_release_missing=False
            )
            self.assertEqual(sum1["released_to_todo"], 0)
            self.assertEqual(sum1["still_pending"], 2)

            sum2 = await reconcile_gap_fill_records(
                db=db, task_id="t_d08", mp_client=mp, cd2_client=cd2, force_release_missing=False
            )
            self.assertEqual(sum2["released_to_todo"], 1)
            self.assertEqual(sum2["still_pending"], 1)

            # D09: 预览并执行放弃补缺删除源端本季
            with (
                patch("app.api.archive.get_settings", side_effect=_mock_settings),
                patch("app.api.archive.CloudDrive2Client") as mock_cd2_cls,
            ):
                inst = mock_cd2_cls.return_value
                inst.list_files = AsyncMock(side_effect=lambda p, force_refresh=False: (
                    [{"name": "Season 1", "path": rec.season_path, "isDirectory": True}]
                    if p == rec.show_path
                    else [{"name": "TestShow.S01E01.mkv", "path": f"{rec.season_path}/TestShow.S01E01.mkv", "size": 2048, "isDirectory": False}]
                ))
                inst.delete_file = AsyncMock(return_value=True)
                inst.close = AsyncMock()

                prev = await preview_delete_source_season(DeleteSourceSeasonRequest(season_record_id="rec_d08"), db=db)
                self.assertTrue(prev["ok"])
                self.assertEqual(prev["archive_impact"], "none")
                self.assertEqual(prev["video_count"], 1)

                del_res = await delete_source_season(DeleteSourceSeasonRequest(season_record_id="rec_d08"), db=db)
                self.assertTrue(del_res.ok)
                await db.refresh(rec)
                self.assertEqual(rec.status, "source_absent")


if __name__ == "__main__":
    unittest.main()
