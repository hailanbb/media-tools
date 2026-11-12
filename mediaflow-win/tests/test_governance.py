"""分段覆盖、联合操作失败恢复和源目录隔离回归。"""
import json
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from test_manual_actions import ManualActionsTest, async_session, OperationLog
from app.core.segments import segment_report, episode_segments, diagnose_periods
from app.core.review_engine import generate_review_clean_plan
from app.services.subscription_service import preview, execute, resource_check
from app.clients.clouddrive2 import CloudDrive2Client
from app.core.episode_parser import parse_episode
from fastapi import HTTPException
import httpx
import unittest


class GovernanceTests(ManualActionsTest):
    def file(self, name="X.S02E01.mp4", size=100):
        return {"name": name, "path": "/qa/src/Show/Season 2/" + name, "size": size,
                "id": name, "modified": "2026-01-01", "isDirectory": False}

    def test_period_classification(self):
        mapping = {n: [1, 2, 3] for n in range(1, 11)}
        ordinals = {n: (n - 1) // 3 + 1 for n in range(1, 31)}
        def check(names, total=10, finished=True, archive=False):
            return diagnose_periods([self.file(name) for name in names], 2, mapping, ordinals,
                                    total, finished, archive)
        six = [f"Show.S02E{n:02}.mkv" for n in range(1, 7)]
        status, _, missing, _ = check(six + six[:3])
        self.assertEqual((status, missing), ("period_missing", [7, 8, 9, 10]))
        self.assertEqual(check(six, 12, False)[0], "airing")
        whole = [f"Show.S02E{n:02}.mkv" for n in range(1, 31)]
        self.assertEqual(check(whole + six)[0], "period_ready")
        self.assertEqual(check(whole, archive=True)[0], "period_review")
        self.assertEqual(check(whole[:10])[0], "period_review")
        self.assertIsNone(check(["unidentified.mkv"]))

    async def test_preserve_versions_moves_only_the_season(self):
        from app.models.season import SeasonRecord
        disk = AsyncMock()
        disk.list_files.return_value = []
        disk.move_file.return_value = {"success": True}
        async with async_session() as db:
            season = await db.get(SeasonRecord, "ready")
            season.status = "period_ready"
            season.archive_target_path = "/qa/dst/Show/Season 1"
            verdict = SimpleNamespace(season_path=season.season_path, status=season.status,
                                      archive_target_path=season.archive_target_path)
            await db.commit()
        with patch("app.api.archive.CloudDrive2Client", return_value=disk), patch(
                "app.clients.moviepilot.MoviePilotClient", return_value=AsyncMock()), patch(
                "app.core.inspector.run_inspection", new=AsyncMock(return_value=SimpleNamespace(seasons=[verdict]))):
            reply = await self.http.post("/api/archive/execute", json={"season_record_ids": ["ready"]})
        self.assertTrue(reply.json()["ok"], reply.json())
        disk.move_file.assert_awaited_once_with(verdict.season_path, "/qa/dst/Show", skip_existing=True)
        disk.delete_file.assert_not_awaited()

    async def test_archive_incremental_merge_read_failure_protects_source(self):
        """P0: 增量合并末次读取源季失败时，显式区分读取失败，保留源季绝不误删，状态记为待核实。"""
        from app.models.season import SeasonRecord
        disk = AsyncMock()
        target_parent = "/qa/dst/Show"
        existing_season_dir = {"name": "Season 1", "path": "/qa/dst/Show/Season 1", "isDirectory": True}
        src_files = [
            {"name": "Show.S01E03.mkv", "path": "/qa/src/Show/Season 1/Show.S01E03.mkv", "isDirectory": False, "size": 100}
        ]
        dest_files = [
            {"name": "Show.S01E01.mkv", "path": "/qa/dst/Show/Season 1/Show.S01E01.mkv", "isDirectory": False, "size": 100},
            {"name": "Show.S01E02.mkv", "path": "/qa/dst/Show/Season 1/Show.S01E02.mkv", "isDirectory": False, "size": 100}
        ]
        call_count = 0
        async def fake_list_files(path, force_refresh=False):
            nonlocal call_count
            call_count += 1
            if path == "/qa/src/Show/Season 1":
                if call_count >= 4:
                    raise RuntimeError("网盘网络中断：读取源季失败")
                return src_files
            elif path == "/qa/dst/Show":
                return [existing_season_dir]
            elif path == "/qa/dst/Show/Season 1":
                return dest_files
            return []

        disk.list_files.side_effect = fake_list_files
        disk.move_file.return_value = {"success": True}
        disk.delete_file = AsyncMock()

        async with async_session() as db:
            season = await db.get(SeasonRecord, "ready")
            season.status = "archive_ready"
            season.season_path = "/qa/src/Show/Season 1"
            season.show_path = "/qa/src/Show"
            season.archive_target_path = "/qa/dst/Show/Season 1"
            await db.commit()

        with patch("app.api.archive.CloudDrive2Client", return_value=disk):
            reply = await self.http.post("/api/archive/execute", json={"season_record_ids": ["ready"]})

        disk.delete_file.assert_not_awaited()
        async with async_session() as db:
            updated = await db.get(SeasonRecord, "ready")
            self.assertEqual(updated.status, "review_needed")
            self.assertIn("读取源季失败", updated.status_detail)

    async def test_archive_candidate_multiple_seasons_halt(self):
        """P0: 目标季匹配到多个候选目录时安全停止并列出候选路径。"""
        from app.models.season import SeasonRecord
        disk = AsyncMock()
        candidate_dirs = [
            {"name": "Season 1", "path": "/qa/dst/Show/Season 1", "isDirectory": True},
            {"name": "Season 01", "path": "/qa/dst/Show/Season 01", "isDirectory": True}
        ]
        src_files = [
            {"name": "Show.S01E01.mkv", "path": "/qa/src/Show/Season 1/Show.S01E01.mkv", "isDirectory": False, "size": 100}
        ]
        async def fake_list(path, force_refresh=False):
            if path == "/qa/src/Show/Season 1":
                return src_files
            elif path == "/qa/dst/Show":
                return candidate_dirs
            return []
        disk.list_files.side_effect = fake_list
        disk.move_file = AsyncMock()
        disk.delete_file = AsyncMock()

        async with async_session() as db:
            season = await db.get(SeasonRecord, "ready")
            season.status = "archive_ready"
            season.season_path = "/qa/src/Show/Season 1"
            season.show_path = "/qa/src/Show"
            season.archive_target_path = "/qa/dst/Show/Season 1"
            await db.commit()

        with patch("app.api.archive.CloudDrive2Client", return_value=disk):
            reply = await self.http.post("/api/archive/execute", json={"season_record_ids": ["ready"]})

        disk.move_file.assert_not_awaited()
        disk.delete_file.assert_not_awaited()
        async with async_session() as db:
            updated = await db.get(SeasonRecord, "ready")
            self.assertEqual(updated.status, "review_needed")
            self.assertIn("多个候选季目录", updated.status_detail)

    async def test_period_download_rejects_ambiguous_numbering(self):
        from app.models.season import SeasonRecord
        from app.services.download_service import execute_gap_fill_download
        mp, disk = self.clients()
        def candidate(title):
            return {"torrent_info": {"title": title, "seeders": 3}, "media_info": {"tmdb_id": 42},
                    "meta_info": {"season_list": [2], "episode_list": [7]}}
        right = candidate("Show S02 第7期")
        mp.search_resources.return_value = [candidate("Show S02E07"), candidate("Show S02 第7期（下）"), right]
        mp.submit_download.return_value = {"success": True}
        async with async_session() as db:
            season = await db.get(SeasonRecord, "gap")
            season.status, season.tmdb_id, season.total_episodes = "period_missing", "42", 10
            season.missing_episodes_json = "[7,8]"
            await db.commit()
            fresh = SimpleNamespace(status=season.status, tmdb_id=42, season_path=season.season_path, missing_episodes=[7])
            with patch("app.services.download_service.MoviePilotClient", return_value=mp), patch(
                    "app.clients.clouddrive2.CloudDrive2Client", return_value=disk), patch(
                    "app.services.download_service.get_settings", return_value=SimpleNamespace(
                        mp_url="test", mp_token="fixture", source_root="/qa/src", archive_root="/qa/dst")), patch(
                    "app.core.inspector.run_inspection", new=AsyncMock(return_value=SimpleNamespace(seasons=[fresh]))):
                await execute_gap_fill_download(["gap"], db)
        mp.submit_download.assert_awaited_once_with(right["media_info"], right["torrent_info"])
        mp.clear_transfer_history_for_episodes.assert_not_awaited()

    def test_queue_episode_field_excludes_season_and_title_numbers(self):
        from app.services.download_service import parse_active_episodes
        self.assertEqual(parse_active_episodes("24 in 24 S03E08 Part 1", 9), {8})
        self.assertEqual(parse_active_episodes("E02-E03, E09", 9), {2, 3, 9})
        self.assertEqual(parse_active_episodes("[2, 3, 9]", 9), {2, 3, 9})

    async def test_numbered_episode_with_part_in_title_allows_other_gaps(self):
        from app.services.download_service import execute_gap_fill_download
        from app.models.season import SeasonRecord
        mp, disk = self.clients()
        disk.list_files.return_value = [self.file("Show.S03E08.Elevation.Part.1.mkv"),
                                       self.file("unknown-Part2.nfo"), {**self.file("Part1"), "isDirectory": True}]
        def candidate(title, numbers, tid=42, season=3):
            return {"torrent_info": {"title": title, "seeders": 5}, "media_info": {"tmdb_id": tid},
                    "meta_info": {"season_list": [season], "episode_list": numbers}}
        good = [candidate(f"Show.S03E{n:02}" + (".Elevation.Part.2" if n == 9 else ""), [n])
                for n in [2, 3, 9]]
        # 站点只在结构化起止集号中给出 E02，节目名称中的 24 不能当成集号。
        good[0] = candidate("24 in 24 Last Chef Standing", [])
        good[0]["meta_info"].update(begin_episode=2, end_episode=2)
        invalid = [candidate("Show.S03E08.Part1", [8]), candidate("Show.S03.Complete", list(range(1, 10))),
                   candidate("Other.S03E02", [2], tid=55), candidate("Show.S02E02", [2], season=2),
                   candidate("Show.S03E08.Part1", [9])]
        mp.search_resources.return_value = invalid + good
        mp.submit_download.return_value = {"success": True}
        async with async_session() as db:
            rec = await db.get(SeasonRecord, "gap")
            rec.tmdb_id, rec.season_number, rec.total_episodes = "42", 3, 9
            rec.season_path = rec.show_path + "/Season 3"
            rec.missing_episodes_json = "[2,3,7,9]"
            await db.commit()
            fresh = SimpleNamespace(status="download_needed", tmdb_id=42, season_path=rec.season_path,
                                    missing_episodes=[2,3,9], status_detail="缺 2、3、9")
            with patch("app.services.download_service.MoviePilotClient", return_value=mp), patch(
                    "app.clients.clouddrive2.CloudDrive2Client", return_value=disk), patch(
                    "app.services.download_service.get_settings", return_value=SimpleNamespace(
                        mp_url="test", mp_token="fixture", source_root="/qa/src", archive_root="/qa/dst")), patch(
                    "app.core.inspector.run_inspection", new=AsyncMock(return_value=SimpleNamespace(seasons=[fresh]))):
                result = await execute_gap_fill_download(["gap"], db)
                self.assertTrue(result["ok"], result)
                self.assertEqual({s["title"] for s in result["submitted"]}, {c["torrent_info"]["title"] for c in good})
                mp.clear_transfer_history_for_episodes.assert_not_awaited()
                mp.get_active_downloads.return_value = [{"tmdb_id":42,"season":3,"episode":"E02-E03","title":"Show"},
                                                       {"tmdb_id":42,"season":3,"title":"Show.S03E09.Part2"}]
                mp.search_resources.reset_mock()
                result = await execute_gap_fill_download(["gap"], db)
                self.assertTrue(result["skipped"], result)
                self.assertIn("下载队列", result["message"])
                mp.search_resources.assert_not_awaited()
                # 用户在 MP 取消/清除了此前受理的任务且网盘仍无实文件，经“核对入库”退回待补后再重试检索
                from app.services.gap_fill_tracker import reconcile_gap_fill_records
                mp.get_active_downloads.return_value = []
                mp.get_download_history.return_value = []
                mp.get_transfer_history.return_value = []
                await reconcile_gap_fill_records(
                    db=db, task_id=rec.task_id, season_record_ids=["gap"],
                    force_release_missing=True, mp_client=mp, cd2_client=disk,
                )
                mp.get_media_info.return_value = {"original_title":"Show"}
                mp.search_resources.side_effect = [[], []]
                result = await execute_gap_fill_download(["gap"], db)
                self.assertTrue(result["skipped"], result)
                self.assertIn("未返回资源", result["message"])
                mp.search_resources.side_effect = TimeoutError()
                result = await execute_gap_fill_download(["gap"], db)
                self.assertFalse(result["skipped"])
                self.assertFalse(result["ok"])
                disk.list_files.return_value.append(self.file("unidentified.Part.02.1080p.mkv"))
                mp.search_resources.reset_mock()
                result = await execute_gap_fill_download(["gap"], db)
                self.assertTrue(result["skipped"])
                self.assertIn("unidentified.Part.02.1080p.mkv", result["message"])
                mp.search_resources.assert_not_awaited()

    def clients(self):
        sub = {"id": 7, "name": "示例", "season": 2, "tmdbid": 1, "total_episode": 2,
               "completed_episode": 1, "lack_episode": 1, "state": "P"}
        mp = AsyncMock()
        mp.list_subscriptions.return_value = [sub]
        mp.search_resources.return_value = []
        mp.get_active_downloads.return_value = []
        mp.get_transfer_queue.return_value = []
        disk = AsyncMock()
        disk.list_files.return_value = [self.file(), self.file("season.nfo")]
        disk.get_file_info.side_effect = [self.file(), None]
        disk.delete_file.return_value = {"success": True}
        return mp, disk

    async def test_split_coverage_and_cleanup_veto(self):
        eps = [{"name": f"第{n}期{p}", "episode_number": (n-1)*2+i}
               for n in range(1, 13) for i, p in enumerate(("上", "下"), 1)]
        mapping = episode_segments(eps)
        files = [self.file(f"X.S02E{n:02}.Part{p}.mp4") for n in range(1, 13) for p in ("A", "B")]
        self.assertIn("全部正片分段", segment_report(files, 2, mapping))
        self.assertIn("第12期第2段", segment_report(files[:-1], 2, mapping))
        self.assertFalse(generate_review_clean_plan(files, 2, 24)["can_clean"])
        self.assertFalse(parse_episode("Hot Ones： Extra Heat - S01E01 - Hot.Ones.Extra.Heat.S01E01.mkv").is_extra)

    async def test_preview_scope_and_execution_idempotence(self):
        mp, disk = self.clients()
        # Test fixture uses a string TMDB ID to identify the intended season.
        from app.models.season import SeasonRecord
        async with async_session() as db:
            (await db.get(SeasonRecord, "gap")).tmdb_id = "1"
            await db.commit()
            plan = await preview(db, mp, disk, 7, True)
            self.assertEqual(len(plan["files"]), 1)
            result = await execute(db, mp, disk, plan["operation_id"])
            self.assertTrue(result["ok"])
            self.assertTrue((await execute(db, mp, disk, plan["operation_id"]))["ok"])
        disk.delete_file.assert_awaited_once_with(self.file()["path"])
        mp.delete_subscription.assert_awaited_once_with(7)

    async def test_failed_unsubscribe_never_deletes(self):
        mp, disk = self.clients()
        from app.models.season import SeasonRecord
        async with async_session() as db:
            (await db.get(SeasonRecord, "gap")).tmdb_id = "1"
            await db.commit()
            plan = await preview(db, mp, disk, 7, True)
            mp.delete_subscription.side_effect = TimeoutError()
            self.assertFalse((await execute(db, mp, disk, plan["operation_id"]))["ok"])
        disk.delete_file.assert_not_awaited()

    async def test_partial_delete_can_resume_after_response_loss(self):
        mp, disk = self.clients()
        from app.models.season import SeasonRecord
        async with async_session() as db:
            (await db.get(SeasonRecord, "gap")).tmdb_id = "1"
            await db.commit()
            plan = await preview(db, mp, disk, 7, True)
            disk.delete_file.side_effect = TimeoutError()
            self.assertFalse((await execute(db, mp, disk, plan["operation_id"]))["ok"])
            mp.list_subscriptions.return_value = []
            disk.list_files.return_value = [self.file("season.nfo")]
            disk.get_file_info.side_effect = [None]
            self.assertTrue((await execute(db, mp, disk, plan["operation_id"]))["ok"])
            mp.delete_subscription.assert_awaited_once()

    async def test_directory_changes_prevent_retirement(self):
        mp, disk = self.clients()
        from app.models.season import SeasonRecord
        async with async_session() as db:
            (await db.get(SeasonRecord, "gap")).tmdb_id = "1"
            await db.commit()
            plan = await preview(db, mp, disk, 7, True)
            disk.list_files.return_value.append(self.file("X.S02E02.mp4"))
            self.assertFalse((await execute(db, mp, disk, plan["operation_id"]))["ok"])
        mp.delete_subscription.assert_not_awaited()
        disk.delete_file.assert_not_awaited()

    async def test_invalid_grpc_and_resource_timeout_are_unknown(self):
        disk = CloudDrive2Client()
        await disk.close()
        disk.origin = "https://fixture"
        disk._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>login</html>")))
        with self.assertRaises(RuntimeError):
            await disk.delete_file("/qa/src/a")
        await disk.close()
        mp, _ = self.clients()
        mp.search_resources.side_effect = TimeoutError()
        result = await resource_check(mp, {"tmdbid": 1, "season": 2, "name": "x"})
        self.assertEqual(result["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
