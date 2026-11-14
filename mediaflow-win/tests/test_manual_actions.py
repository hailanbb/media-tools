"""持久隐藏、任务隔离与缺集条目的整剧删除接口回归。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(MF_DATA_DIR=tempfile.mkdtemp(prefix="manual-actions-", dir=Path.cwd()),
                  MF_SOURCE_ROOT="/qa/src", MF_ARCHIVE_ROOT="/qa/dst", MF_AUTH_TOKEN="fixture",
                  MF_CD2_URL="", MF_CD2_TOKEN="", MF_MP_URL="", MF_MP_TOKEN="", MF_TMDB_API_KEY="")

import httpx
from sqlalchemy import select
from app.main import app
from app.database import Base, async_session, engine, init_db
from app.models.task import Task
from app.models.season import SeasonRecord
from app.models.operation import OperationLog
from app.core.inspector import InspectionResult, SeasonDiagnosis
from app.services.task_service import execute_inspection


class ManualActionsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await init_db()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture",
                                      headers={"Authorization": "Bearer fixture"})
        async with async_session() as db:
            db.add_all([Task(id="t", name="任务", source_path="/qa/src", status="completed"),
                        Task(id="other", name="另一任务", source_path="/qa/src/other", status="completed")])
            await db.flush()
            for rid, status, season in [("anomaly", "unknown", 1), ("gap", "download_needed", 2),
                                         ("ready", "archive_ready", 1)]:
                show = "/qa/src/Show" if rid != "ready" else "/qa/src/Other"
                db.add(SeasonRecord(id=rid, task_id="t", show_name="示例", show_path=show,
                                    season_path=f"{show}/Season {season}", season_number=season,
                                    total_episodes=None if rid == "anomaly" else 2,
                                    local_episode_count=1, recognized_episodes_json="[1]", status=status))
            await db.commit()

    async def asyncTearDown(self):
        await self.http.aclose()
        await engine.dispose()

    async def test_handled_is_hidden_idempotent_and_scoped(self):
        for _ in range(2):
            r = await self.http.post("/api/tasks/t/seasons/anomaly/handled")
            self.assertTrue(r.json()["ok"])
        rows = (await self.http.get("/api/tasks/t/seasons")).json()
        self.assertEqual({r["id"] for r in rows}, {"gap", "ready"})
        self.assertEqual((await self.http.get("/api/tasks/t/seasons?status=manually_handled")).json(), [])
        task = (await self.http.get("/api/tasks/t")).json()
        self.assertEqual(task["summary"]["total_seasons"], 2)
        async with async_session() as db:
            logs = list(await db.scalars(select(OperationLog).where(OperationLog.action == "manually_handled")))
            self.assertEqual(len(logs), 1)
            self.assertEqual(json.loads(logs[0].detail_json)["season_path"], "/qa/src/Show/Season 1")

    async def test_legacy_file_check_can_be_completed_manually(self):
        async with async_session() as db:
            rec = await db.get(SeasonRecord, "anomaly")
            rec.status = "archive_check"
            rec.total_episodes = 12
            await db.commit()
        r = await self.http.post("/api/tasks/t/seasons/anomaly/handled")
        self.assertTrue(r.json()["ok"])
        rows = (await self.http.get("/api/tasks/t/seasons")).json()
        self.assertNotIn("anomaly", [r["id"] for r in rows])

    async def test_normal_record_and_other_task_rejected(self):
        self.assertEqual((await self.http.post("/api/tasks/other/seasons/anomaly/handled")).status_code, 404)
        for rid in ("gap", "ready"):
            self.assertEqual((await self.http.post(f"/api/tasks/t/seasons/{rid}/handled")).status_code, 409)

    async def test_marker_survives_rescan_and_temporary_absence(self):
        await self.http.post("/api/tasks/t/seasons/anomaly/handled")
        for seasons in ([], [SeasonDiagnosis(show_name="示例", show_path="/qa/src/Show", season_number=1,
                                             season_path="/qa/src/Show/Season 1", status="unknown")]):
            with patch("app.services.task_service.run_inspection", new=AsyncMock(return_value=InspectionResult(
                    task_id="t", source_path="/qa/src", seasons=seasons))), patch(
                    "app.clients.clouddrive2.CloudDrive2Client", return_value=AsyncMock()), patch(
                    "app.clients.moviepilot.MoviePilotClient", return_value=AsyncMock()), patch(
                    "app.clients.tmdb.TMDBClient", return_value=AsyncMock()):
                async with async_session() as db:
                    await execute_inspection("t", db)
            self.assertEqual((await self.http.get("/api/tasks/t/seasons")).json(), [])
            self.assertEqual((await self.http.get("/api/tasks/t")).json()["summary"]["total_seasons"], 0)

    async def test_gap_delete_removes_only_selected_show(self):
        disk = AsyncMock()
        disk.delete_file.return_value = {"success": True}
        disk.list_files.return_value = []
        with patch("app.api.archive.CloudDrive2Client", return_value=disk):
            r = await self.http.post("/api/archive/delete-source-show", json={"season_record_id": "gap"})
        # 遵循 v2.5.24 安全红线：真缺集待补剧集严禁删除源端整剧
        self.assertFalse(r.json()["ok"])
        self.assertIn("真缺集", r.json()["message"])

    async def test_gap_delete_source_season_preview_and_execute(self):
        disk = AsyncMock()
        disk.list_files.return_value = [
            {"name": "Show.S02E01.mp4", "size": 1024 * 1024 * 500, "is_dir": False},
            {"name": "Show.S02E01.zh.srt", "size": 1024 * 50, "is_dir": False},
        ]
        disk.delete_file.return_value = {"success": True}
        disk.refresh_path.return_value = {"success": True}

        mp = AsyncMock()
        mp.get_active_downloads.return_value = []

        with patch("app.api.archive.CloudDrive2Client", return_value=disk), \
             patch("app.api.archive.MoviePilotClient", return_value=mp):
            # 1. 预检接口
            prev_resp = await self.http.post("/api/archive/delete-source-season/preview", json={"season_record_id": "gap"})
            self.assertEqual(prev_resp.status_code, 200)
            prev_data = prev_resp.json()
            self.assertTrue(prev_data["ok"])
            self.assertEqual(prev_data["data"]["season_path"], "/qa/src/Show/Season 2")
            self.assertEqual(prev_data["data"]["file_stats"]["video_count"], 1)
            self.assertEqual(prev_data["data"]["file_stats"]["sub_count"], 1)
            self.assertEqual(prev_data["data"]["episodes"], ["S02E01"])
            self.assertFalse(prev_data["data"]["has_active_mp_downloads"])

            # 2. 执行单季删除
            del_resp = await self.http.post("/api/archive/delete-source-season", json={"season_record_id": "gap"})
            self.assertEqual(del_resp.status_code, 200)
            del_data = del_resp.json()
            self.assertTrue(del_data["ok"])
            self.assertIn("已成功删除", del_data["message"])

            # 验证 CD2 删除与父级刷新
            disk.delete_file.assert_called_once_with("/qa/src/Show/Season 2")
            disk.list_files.assert_called_with("/qa/src/Show", force_refresh=True)

        # 验证数据库状态已转为 source_absent，不出现在待补
        async with async_session() as db:
            rec = await db.get(SeasonRecord, "gap")
            self.assertEqual(rec.status, "source_absent")

            # 验证审计日志
            logs = list(await db.scalars(select(OperationLog).where(OperationLog.action == "delete_source_season")))
            self.assertEqual(len(logs), 1)
            detail = json.loads(logs[0].detail_json)
            self.assertEqual(detail["season_path"], "/qa/src/Show/Season 2")
            self.assertEqual(detail["reason"], "user_abandoned_deleted")

    async def test_gap_delete_source_season_rejected_for_non_gap(self):
        # ready 状态的剧集禁止通过单季放弃补缺删源
        r = await self.http.post("/api/archive/delete-source-season", json={"season_record_id": "ready"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("仅允许", r.json().get("detail", r.json().get("message", "")))


if __name__ == "__main__":
    unittest.main()
