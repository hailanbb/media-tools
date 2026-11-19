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
        # 遵循安全红线：缺集待补剧集严禁删除源端整剧
        self.assertFalse(r.json()["ok"])
        self.assertIn("缺集待补", r.json()["message"])

    async def test_gap_delete_source_season_preview_and_execute(self):
        # G22 (v2.5.45): 缺集待补取消删除源端本季入口，preview 与 execute 均必须返回 409 拦截
        prev_resp = await self.http.post("/api/archive/delete-source-season/preview", json={"season_record_id": "gap"})
        self.assertEqual(prev_resp.status_code, 409)
        self.assertIn("已取消删除源端入口", prev_resp.json().get("detail", ""))

        del_resp = await self.http.post("/api/archive/delete-source-season", json={"season_record_id": "gap"})
        self.assertEqual(del_resp.status_code, 409)
        self.assertIn("已取消删除源端入口", del_resp.json().get("detail", ""))

    async def test_gap_delete_source_season_rejected_for_non_gap(self):
        # ready 状态的剧集禁止通过单季清源删源（仅限 already_archived）
        r = await self.http.post("/api/archive/delete-source-season", json={"season_record_id": "ready"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("仅限", r.json().get("detail", r.json().get("message", "")))

    async def test_l05_airing_work_season_blocks_all_write_endpoints_with_zero_external_writes(self):
        """L05: 连载季 E05 有两份(review_needed)且关联物理目录带 download_needed -> 直接调用删源、清理、归档、手动完成时外部写入为零."""
        async with async_session() as db:
            db.add_all([
                SeasonRecord(
                    id="l05-air-dup",
                    task_id="t",
                    show_name="连载双目录剧",
                    show_path="/qa/src/AiringShowA",
                    tmdb_id="88005",
                    season_number=1,
                    season_path="/qa/src/AiringShowA/Season 1",
                    total_episodes=12,
                    local_episode_count=6,
                    recognized_episodes_json="[1,2,3,4,5]",
                    duplicate_episodes_json="[5]",
                    status="review_needed",
                    status_detail="连载中存在重复版本 (第 5 集)，需清理低质冗余",
                    season_finished=False,
                ),
                SeasonRecord(
                    id="l05-hist-gap",
                    task_id="t",
                    show_name="连载双目录剧",
                    show_path="/qa/src/AiringShowB",
                    tmdb_id="88005",
                    season_number=1,
                    season_path="/qa/src/AiringShowB/Season 1",
                    total_episodes=12,
                    local_episode_count=3,
                    recognized_episodes_json="[1,2,3]",
                    missing_episodes_json="[4,5,6,7,8,9,10,11,12]",
                    status="download_needed",
                    status_detail="历史缺集记录",
                    season_finished=True,
                ),
            ])
            await db.commit()

        disk = AsyncMock()
        with patch("app.api.archive.CloudDrive2Client", return_value=disk):
            # 1) 直接对关联缺集记录调用单季删源预检与执行 -> 409 拒绝，0 删
            r_del_prev = await self.http.post("/api/archive/delete-source-season/preview", json={"season_record_id": "l05-hist-gap"})
            self.assertEqual(r_del_prev.status_code, 409)
            r_del = await self.http.post("/api/archive/delete-source-season", json={"season_record_id": "l05-hist-gap"})
            self.assertEqual(r_del.status_code, 409)

            # 2) 直接对连载重复记录调用重复版本清理 -> 拒绝，0 删
            r_clean = await self.http.post("/api/archive/clean", json={"season_record_ids": ["l05-air-dup"]})
            self.assertFalse(r_clean.json()["ok"])

            # 3) 直接调用归档执行 -> 拒绝，0 移
            r_arc = await self.http.post("/api/archive/execute", json={"season_record_ids": ["l05-air-dup", "l05-hist-gap"]})
            self.assertFalse(r_arc.json()["ok"])

            # 4) 直接调用标记手动处理 -> 409 拒绝
            r_h = await self.http.post("/api/tasks/t/seasons/l05-air-dup/handled")
            self.assertEqual(r_h.status_code, 409)

            disk.delete_file.assert_not_awaited()
            disk.move_file.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
