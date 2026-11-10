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
