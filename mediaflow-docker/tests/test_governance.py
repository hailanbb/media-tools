"""分段覆盖、联合操作失败恢复和源目录隔离回归。"""
import json
from unittest.mock import AsyncMock
from test_manual_actions import ManualActionsTest, async_session, OperationLog
from app.core.segments import segment_report, episode_segments
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
