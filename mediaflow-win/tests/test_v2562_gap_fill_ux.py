"""v2.5.62 缺集待补交互与核对入库优化专项测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

os.environ.setdefault("MF_DATA_DIR", tempfile.mkdtemp(prefix="mf-v2562-"))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clients.moviepilot import MoviePilotClient
from app.database import Base
from app.models.gap_fill import GapFillEpisode
from app.models.season import SeasonRecord
from app.models.task import Task
from app.services.gap_fill_tracker import build_identity_key, reconcile_gap_fill_records


class TestV2562GapFillUX(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_01_reconcile_emits_progress_and_handles_concurrent_reads(self) -> None:
        async with self.Session() as db:
            db.add(Task(id="t1", name="Test", source_path="/src", media_type="tv", status="completed"))
            for idx in (1, 2):
                rec = SeasonRecord(
                    id=f"r{idx}",
                    task_id="t1",
                    show_name=f"Show {idx} [tmdbid={1000 + idx}]",
                    show_path=f"/src/Show {idx}",
                    tmdb_id=str(1000 + idx),
                    season_number=1,
                    season_path=f"/src/Show {idx}/Season 1",
                    total_episodes=2,
                    local_episode_count=1,
                    recognized_episodes_json="[1]",
                    missing_episodes_json="[2]",
                    status="download_needed",
                    status_detail="缺第 2 集",
                )
                db.add(rec)
                db.add(
                    GapFillEpisode(
                        task_id="t1",
                        season_record_id=f"r{idx}",
                        season_path=f"/src/Show {idx}/Season 1",
                        show_name=rec.show_name,
                        tmdb_id=1000 + idx,
                        search_tmdb_id=1000 + idx,
                        season_number=1,
                        search_season_number=1,
                        numbering_mode="standard",
                        identity_key=build_identity_key(rec.show_name, rec.tmdb_id, rec.season_number, None),
                        episode_number=2,
                        state="queued",
                        outcome_code="accepted",
                    )
                )
            await db.commit()

            class FakeMP:
                async def get_active_downloads(self):
                    return []

                async def get_transfer_queue(self):
                    return []

                async def get_download_history(self, **kw):
                    return []

                async def get_transfer_history(self, **kw):
                    return []

            class FakeCD2:
                async def list_files(self, path, **kw):
                    return [
                        {"name": "S01E01.mkv", "path": f"{path}/S01E01.mkv", "size": 500000, "isDirectory": False},
                    ]

            events: list[dict] = []
            res = await reconcile_gap_fill_records(
                "t1",
                db,
                mp_client=FakeMP(),
                cd2_client=FakeCD2(),
                progress_cb=events.append,
            )

            self.assertTrue(res["ok"])
            self.assertEqual(res["checked_seasons"], 2)
            self.assertGreaterEqual(len(events), 4)
            self.assertEqual(events[-1]["percent"], 100)
            self.assertEqual(events[-1]["stage"], "核对入库完成")

    async def test_02_clear_transfer_history_aliases_and_tmdb_filter_delete_record_only(self) -> None:
        mp = MoviePilotClient()
        mp.get_all_transfer_history_for_title = AsyncMock(
            side_effect=[
                [{"id": 11, "title": "English Title", "tmdbid": 888, "seasons": "S01", "episodes": "E01-E05"}],
                [
                    {"id": 11, "title": "English Title", "tmdbid": 888, "seasons": "S01", "episodes": "E01-E05"},
                    {"id": 12, "title": "中文剧名", "tmdbid": 888, "seasons": "S01", "episodes": ""},
                    {"id": 13, "title": "中文剧名", "tmdbid": 999, "seasons": "S01", "episodes": "E02"},  # 异剧防误删
                    {"id": 14, "title": "中文剧名", "tmdbid": 888, "seasons": "S02", "episodes": "E01"},  # 异季保护
                ],
            ]
        )
        deleted: list[tuple[int, bool, bool]] = []

        async def _fake_del(rid, delete_src=False, delete_dest=False):
            deleted.append((rid, delete_src, delete_dest))
            return True

        mp.delete_transfer_history = _fake_del  # type: ignore[method-assign]

        res = await mp.clear_transfer_history_for_episodes(
            title="English Title",
            season=1,
            episodes=None,
            explicit_only=False,
            tmdb_id=888,
            title_aliases=["中文剧名"],
        )
        await mp.close()

        self.assertEqual(res["cleared_count"], 2)
        self.assertEqual(deleted, [(11, False, False), (12, False, False)])

    def test_03_frontend_ux_button_order_progress_and_rename(self) -> None:
        html = Path("frontend/index.html").read_text(encoding="utf-8")
        idx_reconcile = html.find('id="reconcileGapFillBtn"')
        idx_sites = html.find('onclick="openGapFillSitesModal()"')
        self.assertGreater(idx_reconcile, 0)
        self.assertGreater(idx_sites, idx_reconcile, "核对入库按钮应排在补缺站点设置之前")
        self.assertIn('id="gapFillReconcileProgress"', html)
        self.assertIn('/reconcile-gap-fill-start', html)
        self.assertIn('/reconcile-gap-fill-status/', html)
        self.assertIn('>确认源已移删</button>', html)
        self.assertNotIn('>核对源目录</button>', html)

    async def test_04_reconcile_start_and_status_endpoints(self) -> None:
        import asyncio
        from unittest.mock import patch
        from app.api.tasks import start_reconcile_gap_fill, get_reconcile_gap_fill_status

        async with self.Session() as db:
            db.add(Task(id="t_api", name="API Test", source_path="/src", media_type="tv", status="completed"))
            await db.commit()

            fake_summary = {
                "ok": True,
                "checked_seasons": 1,
                "reconciled_in_library": 1,
                "still_pending": 0,
                "released_to_todo": 0,
                "post_review_completed": 1,
                "post_review_pending": 0,
            }

            async def _fake_reconcile(*, progress_cb=None, **kwargs):
                if progress_cb:
                    progress_cb({"stage": "并发核对中", "current": 1, "total": 1, "current_show": "S1", "percent": 80})
                return fake_summary

            with (
                patch("app.database.async_session", self.Session),
                patch("app.api.tasks.reconcile_gap_fill_records", side_effect=_fake_reconcile),
                patch("app.api.tasks.refresh_task_summary", return_value={"total": 1}),
            ):
                start_res = await start_reconcile_gap_fill("t_api", body=None, db=db)
                self.assertTrue(start_res["ok"])
                job_id = start_res["job_id"]
                await asyncio.sleep(0.05)
                status_res = await get_reconcile_gap_fill_status("t_api", job_id)
                self.assertEqual(status_res["status"], "completed")
                self.assertEqual(status_res["percent"], 100)
                self.assertIn("核对入库完成", status_res["message"])


if __name__ == "__main__":
    unittest.main()
