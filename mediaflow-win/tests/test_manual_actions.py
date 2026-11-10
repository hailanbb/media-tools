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
        self.assertTrue(r.json()["ok"])
        disk.delete_file.assert_awaited_once_with("/qa/src/Show")
        self.assertEqual([s["id"] for s in (await self.http.get("/api/tasks/t/seasons")).json()], ["ready"])


if __name__ == "__main__":
    unittest.main()
