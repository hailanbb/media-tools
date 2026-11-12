"""Doc 2 acceptance suite: pre-submit intent + sanitized receipt persistence,
partial coverage conservation, tout/unverified & reconcile resolution,
mid-batch abort marking, re-inspect record inheritance & superseding,
legacy OperationLog fallback, and secret non-leakage.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="gap-persist-")
atexit.register(lambda: shutil.rmtree(_TMP_DATA_DIR, ignore_errors=True))
os.environ.update(
    MF_DATA_DIR=_TMP_DATA_DIR,
    MF_SOURCE_ROOT="/qa/src",
    MF_ARCHIVE_ROOT="/qa/dst",
    MF_AUTH_TOKEN="fixture",
    MF_CD2_URL="http://cd2.mock",
    MF_CD2_TOKEN="cd2tok",
    MF_MP_URL="http://mp.mock",
    MF_MP_TOKEN="mptok",
    MF_TMDB_API_KEY="",
)

import httpx  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from app.clients import clouddrive2 as cd2_mod  # noqa: E402
from app.clients import moviepilot as mp_mod  # noqa: E402
from app.core.inspector import InspectionResult, SeasonDiagnosis  # noqa: E402
from app.database import Base, async_session, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.gap_fill import GapFillEpisode, GapFillSeasonRun  # noqa: E402
from app.models.operation import OperationLog  # noqa: E402
from app.models.season import SeasonRecord  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.services import download_service  # noqa: E402
from app.services.gap_fill_tracker import compute_season_gap_fill_view, refresh_task_summary  # noqa: E402
from app.services.task_service import execute_inspection  # noqa: E402


def _cand(
    title: str,
    *,
    seeders: int,
    size: int,
    eps: list[int],
    season: int = 1,
    tmdb_id: int = 1001,
    site: int = 4,
    site_name: str = "彩虹岛",
    enclosure: str = "",
):
    return {
        "torrent_info": {
            "title": title,
            "seeders": seeders,
            "size": size,
            "site": site,
            "site_name": site_name,
            "enclosure": enclosure,
        },
        "media_info": {"tmdb_id": tmdb_id},
        "meta_info": {"season_list": [season], "episode_list": list(eps)},
    }


def _ep_files(season_dir: str, prefix: str, season: int, eps: list[int]) -> list[dict]:
    return [
        {
            "name": f"{prefix}.S{season:02d}E{e:02d}.1080p.mkv",
            "path": f"{season_dir}/{prefix}.S{season:02d}E{e:02d}.1080p.mkv",
            "fullPathName": f"{season_dir}/{prefix}.S{season:02d}E{e:02d}.1080p.mkv",
            "isDirectory": False,
            "size": 1_500_000_000,
        }
        for e in eps
    ]


def _show_tree(show_path: str, prefix: str, season: int, eps: list[int]) -> dict[str, list[dict]]:
    season_path = f"{show_path}/Season {season}"
    return {
        show_path: [{"name": f"Season {season}", "path": season_path, "fullPathName": season_path, "isDirectory": True, "size": 0}],
        season_path: _ep_files(season_path, prefix, season, eps),
    }


class StubCD2:
    def __init__(self, tree: dict[str, list[dict]] | None = None):
        self.tree = tree or {}

    async def list_files(self, path: str, force_refresh: bool = False):
        return list(self.tree.get(path, []))

    async def close(self):
        return None


class StubMP:
    def __init__(
        self,
        *,
        search_by_tmdb: dict[int, list[dict]] | None = None,
        timeout_titles: set[str] | None = None,
        secret_echo: bool = False,
    ):
        self.search_by_tmdb = search_by_tmdb or {}
        self.timeout_titles = set(timeout_titles or set())
        self.secret_echo = secret_echo
        self.sites = [{"id": 4, "name": "彩虹岛", "is_active": True}]
        self.active_downloads: list[dict] = []
        self.transfer_queue: list[dict] = []
        self.download_history: list[dict] = []
        self.transfer_history: list[dict] = []
        self.download_calls: list[dict] = []
        self.search_calls: list[dict] = []

    async def get_sites(self):
        return list(self.sites)

    async def get_indexer_sites(self):
        return {"ok": True, "value": [int(s["id"]) for s in self.sites], "error": None}

    async def get_active_downloads(self):
        return list(self.active_downloads)

    async def get_transfer_queue(self):
        return list(self.transfer_queue)

    async def get_download_history(self, page: int = 1, count: int = 100):
        return list(self.download_history)

    async def get_transfer_history(self, title: str | None = None, page: int = 1, count: int = 100):
        return list(self.transfer_history)

    async def clear_transfer_history_for_episodes(self, **kwargs):
        return {"cleared_count": 0, "records": [], "protected_count": 0}

    async def get_transfer_overwrite_policy(self):
        return {"value": "never", "label": "存在即跳过"}

    async def get_media_info(self, tmdb_id: int):
        counts = {1001: 4, 1002: 10, 1003: 6, 1009: 4, 2001: 5, 3001: 4, 3002: 4}
        tot = counts.get(int(tmdb_id), 4)
        return {
            "id": int(tmdb_id),
            "tmdb_id": int(tmdb_id),
            "title": f"Show{tmdb_id}",
            "original_title": f"Show{tmdb_id}",
            "status": "Ended",
            "seasons": [{"season_number": 1, "episode_count": tot, "air_date": "2024-01-01"}],
        }

    async def get_tmdb_seasons(self, tmdb_id: int):
        info = await self.get_media_info(tmdb_id)
        return info["seasons"]

    async def get_tmdb_episodes(self, tmdb_id: int, season: int):
        info = await self.get_media_info(tmdb_id)
        tot = info["seasons"][0]["episode_count"]
        return [{"episode_number": i, "air_date": "2024-01-01"} for i in range(1, tot + 1)]

    async def search_media(self, title: str, source: str = "douban"):
        return []

    async def search_resources(
        self,
        tmdb_id: int | None = None,
        season: int | None = None,
        title: str | None = None,
        sites: list[int] | tuple[int, ...] | set[int] | str | None = None,
    ):
        self.search_calls.append({"keyword": title, "tmdb_id": tmdb_id, "season": season})
        return list(self.search_by_tmdb.get(int(tmdb_id or 0), []))

    async def submit_download(self, media_info: dict, torrent_info: dict, **kwargs):
        title = str(torrent_info.get("title") or "")
        self.download_calls.append({"title": title, "tmdb_id": media_info.get("tmdb_id")})
        if any(marker in title for marker in self.timeout_titles):
            raise httpx.ReadTimeout("MP timed out while replying; passkey=TOP_SECRET_PK&authkey=SECRET_AK")
        msg = (
            "queued via https://pt.example/download.php?passkey=LEAK_PASSKEY_123&authkey=LEAK_AUTH_456 token=LEAK_TOK"
            if self.secret_echo
            else "submitted"
        )
        return {"success": True, "download_id": f"dl_{len(self.download_calls)}", "message": msg}

    async def close(self):
        return None


class GapFillPersistenceAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await init_db()
        download_service._active_downloads_cache = None
        download_service._active_downloads_cache_time = 0.0
        download_service._recently_submitted_tasks.clear()
        download_service._search_sites_cache.clear()
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fixture",
            headers={"Authorization": "Bearer fixture"},
        )

    async def asyncTearDown(self):
        await self.http.aclose()
        await engine.dispose()

    async def test_01_three_season_batch_partial_deduction_and_restart_survival(self):
        """3-season batch: A full accepted (-> pushed_pending), B E02+E03 accepted & E09 low_seeders (-> stays todo w/ actionable=[9]), C zero_hits (-> stays todo); survives reload + re-run deduplication."""
        async with async_session() as db:
            db.add(Task(id="t_batch", name="3季批处理", source_path="/qa/src", status="completed"))
            db.add(Task(id="t_other", name="其他任务", source_path="/qa/src_other", status="completed"))
            db.add_all([
                SeasonRecord(
                    id="rec_A", task_id="t_batch", show_name="剧集A [tmdbid=1001]", show_path="/qa/src/ShowA [tmdbid=1001]",
                    tmdb_id="1001", season_number=1, season_path="/qa/src/ShowA [tmdbid=1001]/Season 1",
                    total_episodes=4, local_episode_count=2,
                    recognized_episodes_json="[3, 4]", missing_episodes_json="[1, 2]",
                    status="download_needed", status_detail="缺 1, 2 集", season_finished=True,
                ),
                SeasonRecord(
                    id="rec_B", task_id="t_batch", show_name="剧集B [tmdbid=1002]", show_path="/qa/src/ShowB [tmdbid=1002]",
                    tmdb_id="1002", season_number=1, season_path="/qa/src/ShowB [tmdbid=1002]/Season 1",
                    total_episodes=10, local_episode_count=7,
                    recognized_episodes_json="[1, 4, 5, 6, 7, 8, 10]", missing_episodes_json="[2, 3, 9]",
                    status="download_needed", status_detail="缺 2, 3, 9 集", season_finished=True,
                ),
                SeasonRecord(
                    id="rec_C", task_id="t_batch", show_name="剧集C [tmdbid=1003]", show_path="/qa/src/ShowC [tmdbid=1003]",
                    tmdb_id="1003", season_number=1, season_path="/qa/src/ShowC [tmdbid=1003]/Season 1",
                    total_episodes=6, local_episode_count=4,
                    recognized_episodes_json="[1, 2, 3, 6]", missing_episodes_json="[4, 5]",
                    status="download_needed", status_detail="缺 4, 5 集", season_finished=True,
                ),
                SeasonRecord(
                    id="rec_Other", task_id="t_other", show_name="隔离剧", show_path="/qa/src_other/Iso",
                    tmdb_id="1009", season_number=1, season_path="/qa/src_other/Iso/Season 1",
                    total_episodes=4, local_episode_count=2,
                    recognized_episodes_json="[1, 2]", missing_episodes_json="[3, 4]",
                    status="download_needed", status_detail="缺 3, 4 集", season_finished=True,
                ),
            ])
            await refresh_task_summary("t_batch", db)
            await db.commit()

        mp = StubMP(
            search_by_tmdb={
                1001: [
                    _cand("ShowA.S01E01.1080p.mkv", seeders=10, size=1_500_000_000, eps=[1], tmdb_id=1001),
                    _cand("ShowA.S01E02.1080p.mkv", seeders=10, size=1_500_000_000, eps=[2], tmdb_id=1001),
                ],
                1002: [
                    _cand("ShowB.S01E02.1080p.mkv", seeders=10, size=1_500_000_000, eps=[2], tmdb_id=1002),
                    _cand("ShowB.S01E03.1080p.mkv", seeders=10, size=1_500_000_000, eps=[3], tmdb_id=1002),
                    _cand("ShowB.S01E09.1080p.mkv", seeders=1, size=1_500_000_000, eps=[9], tmdb_id=1002),
                ],
                1003: [],
            },
            secret_echo=True,
        )
        cd2 = StubCD2({
            **_show_tree("/qa/src/ShowA [tmdbid=1001]", "ShowA", 1, [3, 4]),
            **_show_tree("/qa/src/ShowB [tmdbid=1002]", "ShowB", 1, [1, 4, 5, 6, 7, 8, 10]),
            **_show_tree("/qa/src/ShowC [tmdbid=1003]", "ShowC", 1, [1, 2, 3, 6]),
        })

        with patch.object(download_service, "MoviePilotClient", return_value=mp), \
             patch.object(cd2_mod, "CloudDrive2Client", return_value=cd2):
            async with async_session() as db:
                await download_service.execute_gap_fill_download(
                    ["rec_A", "rec_B", "rec_C"], db, batch_run_id="batch_1"
                )

        # Verify task summary and serialized seasons via HTTP (simulates fresh page load / restart)
        download_service._recently_submitted_tasks.clear()
        task_resp = (await self.http.get("/api/tasks/t_batch")).json()
        self.assertEqual(task_resp["summary"]["download_needed"], 2)  # B + C
        self.assertEqual(task_resp["summary"]["download_pushed"], 1)  # A

        seasons = {s["id"]: s for s in (await self.http.get("/api/tasks/t_batch/seasons")).json()}
        self.assertEqual(seasons["rec_A"]["gap_fill_state"]["view_bucket"], "pushed_pending")
        self.assertEqual(seasons["rec_A"]["gap_fill_state"]["actionable_missing_episodes"], [])
        self.assertEqual(seasons["rec_A"]["gap_fill_state"]["pushed_episodes"], [1, 2])

        self.assertEqual(seasons["rec_B"]["gap_fill_state"]["view_bucket"], "todo")
        self.assertEqual(seasons["rec_B"]["gap_fill_state"]["actionable_missing_episodes"], [9])
        self.assertEqual(seasons["rec_B"]["gap_fill_state"]["pushed_episodes"], [2, 3])
        ep9_rec = [e for e in seasons["rec_B"]["gap_fill_state"]["episodes"] if e["episode"] == 9][0]
        self.assertEqual(ep9_rec["outcome_code"], "low_seeders")

        self.assertEqual(seasons["rec_C"]["gap_fill_state"]["view_bucket"], "todo")
        self.assertEqual(seasons["rec_C"]["gap_fill_state"]["actionable_missing_episodes"], [4, 5])
        self.assertEqual(seasons["rec_C"]["gap_fill_state"]["last_run"]["outcome_status"], "zero_hits")

        # Other task untouched (cross-task isolation)
        other_seasons = (await self.http.get("/api/tasks/t_other/seasons")).json()
        self.assertEqual(other_seasons[0]["gap_fill_state"]["view_bucket"], "todo")
        self.assertEqual(other_seasons[0]["gap_fill_state"]["actionable_missing_episodes"], [3, 4])

        # Verify NO secret leakage anywhere in HTTP payload OR DB
        seasons_text = json.dumps(seasons, ensure_ascii=False)
        for leak in ("LEAK_PASSKEY_123", "LEAK_AUTH_456", "LEAK_TOK"):
            self.assertNotIn(leak, seasons_text)
        async with async_session() as db:
            db_eps = list((await db.execute(select(GapFillEpisode))).scalars().all())
            for row in db_eps:
                blob = f"{row.reason} {row.torrent_title}"
                for leak in ("LEAK_PASSKEY_123", "LEAK_AUTH_456", "LEAK_TOK"):
                    self.assertNotIn(leak, blob)

        # Re-running Show B when E09 still has low_seeders must ONLY attempt E09 (never E02 or E03)
        mp.download_calls.clear()
        mp.search_calls.clear()
        mp.search_by_tmdb[1002] = [
            _cand("ShowB.S01E02.1080p.mkv", seeders=10, size=1_500_000_000, eps=[2], tmdb_id=1002),
            _cand("ShowB.S01E03.1080p.mkv", seeders=10, size=1_500_000_000, eps=[3], tmdb_id=1002),
            _cand("ShowB.S01E09.Fixed.1080p.mkv", seeders=9, size=1_500_000_000, eps=[9], tmdb_id=1002),
        ]
        with patch.object(download_service, "MoviePilotClient", return_value=mp), \
             patch.object(cd2_mod, "CloudDrive2Client", return_value=cd2):
            async with async_session() as db:
                await download_service.execute_gap_fill_download(["rec_B"], db, batch_run_id="batch_2")
        self.assertEqual([c["title"] for c in mp.download_calls], ["ShowB.S01E09.Fixed.1080p.mkv"])

    async def test_02_season_pack_dedup_timeout_unverified_and_reconcile_cycles(self):
        """Timeout -> submitted_unverified -> reconcile confirms MP queue vs MP-cancelled w/ no library file frees back to todo vs library presence -> in_library."""
        async with async_session() as db:
            db.add(Task(id="t_rec", name="对账验证", source_path="/qa/src", status="completed"))
            db.add(SeasonRecord(
                id="rec_to", task_id="t_rec", show_name="TimeoutShow [tmdbid=2001]", show_path="/qa/src/TimeoutShow [tmdbid=2001]",
                tmdb_id="2001", season_number=1, season_path="/qa/src/TimeoutShow [tmdbid=2001]/Season 1",
                archive_target_path="/qa/dst/TimeoutShow/Season 1",
                total_episodes=5, local_episode_count=3,
                recognized_episodes_json="[1, 2, 3]", missing_episodes_json="[4, 5]",
                status="download_needed", status_detail="缺 4, 5 集", season_finished=True,
            ))
            await db.commit()

        # Simulate whole-season/multi-ep E04-E05 pack that times out on HTTP response
        mp = StubMP(
            search_by_tmdb={
                2001: [_cand("TimeoutShow.S01E04-E05.Pack.1080p.mkv", seeders=10, size=3_000_000_000, eps=[4, 5], tmdb_id=2001)],
            },
            timeout_titles={"Pack"},
        )
        cd2 = StubCD2({
            **_show_tree("/qa/src/TimeoutShow [tmdbid=2001]", "TimeoutShow", 1, [1, 2, 3]),
            "/qa/dst/TimeoutShow/Season 1": [],
        })

        with patch.object(download_service, "MoviePilotClient", return_value=mp), \
             patch.object(cd2_mod, "CloudDrive2Client", return_value=cd2):
            async with async_session() as db:
                out = await download_service.execute_gap_fill_download(["rec_to"], db, batch_run_id="b_to")

        self.assertEqual(len(out["submitted"]), 1)
        self.assertEqual(out["submitted"][0]["status"], "unverified")
        self.assertTrue(out["details"][0]["submitted_unverified"])

        seasons = {s["id"]: s for s in (await self.http.get("/api/tasks/t_rec/seasons")).json()}
        gstate = seasons["rec_to"]["gap_fill_state"]
        self.assertEqual(gstate["view_bucket"], "pushed_pending")
        self.assertEqual(sorted(gstate["unverified_episodes"]), [4, 5])
        self.assertEqual(sorted(gstate["pushed_episodes"]), [4, 5])
        # Secret scrubbed even in exception message
        self.assertNotIn("TOP_SECRET_PK", json.dumps(seasons))
        self.assertNotIn("SECRET_AK", json.dumps(seasons))

        # Cycle A: Reconcile finds torrent in MP active downloads -> unverified becomes accepted
        mp.active_downloads = [{"title": "TimeoutShow.S01E04-E05.Pack.1080p.mkv", "tmdb_id": 2001}]
        with patch.object(mp_mod, "MoviePilotClient", return_value=mp), \
             patch.object(cd2_mod, "CloudDrive2Client", return_value=cd2):
            rec_a = (await self.http.post("/api/tasks/t_rec/reconcile-gap-fill", json={})).json()
        self.assertTrue(rec_a["ok"])
        self.assertEqual(rec_a["reconcile"]["still_pending"], 2)

        # Cycle B: User cancels MP task -> MP queue/history empty AND dual library lacks E04/E05 -> releases back to todo!
        mp.active_downloads = []
        with patch.object(mp_mod, "MoviePilotClient", return_value=mp), \
             patch.object(cd2_mod, "CloudDrive2Client", return_value=cd2):
            rec_b = (await self.http.post("/api/tasks/t_rec/reconcile-gap-fill", json={"force_release_missing": True})).json()
        self.assertEqual(rec_b["reconcile"]["released_to_todo"], 2)
        seasons_b = {s["id"]: s for s in (await self.http.get("/api/tasks/t_rec/seasons")).json()}
        self.assertEqual(seasons_b["rec_to"]["gap_fill_state"]["view_bucket"], "todo")
        self.assertEqual(sorted(seasons_b["rec_to"]["gap_fill_state"]["actionable_missing_episodes"]), [4, 5])

        # Re-push cleanly & verify Cycle C: file appears in archive_target_path -> reconciled_in_library!
        mp.timeout_titles.clear()
        with patch.object(download_service, "MoviePilotClient", return_value=mp), \
             patch.object(cd2_mod, "CloudDrive2Client", return_value=cd2):
            async with async_session() as db:
                await download_service.execute_gap_fill_download(["rec_to"], db, batch_run_id="b_to2")

        cd2.tree["/qa/dst"] = [{"name": "TimeoutShow", "path": "/qa/dst/TimeoutShow", "isDirectory": True, "size": 0}]
        cd2.tree["/qa/dst/TimeoutShow"] = [{"name": "Season 1", "path": "/qa/dst/TimeoutShow/Season 1", "isDirectory": True, "size": 0}]
        cd2.tree["/qa/dst/TimeoutShow/Season 1"] = [
            {"name": "TimeoutShow.S01E04.mkv", "path": "/qa/dst/TimeoutShow/Season 1/TimeoutShow.S01E04.mkv", "isDirectory": False, "size": 1_000_000_000},
            {"name": "TimeoutShow.S01E05.mkv", "path": "/qa/dst/TimeoutShow/Season 1/TimeoutShow.S01E05.mkv", "isDirectory": False, "size": 1_000_000_000},
        ]
        with patch.object(mp_mod, "MoviePilotClient", return_value=mp), \
             patch.object(cd2_mod, "CloudDrive2Client", return_value=cd2):
            rec_c = (await self.http.post("/api/tasks/t_rec/reconcile-gap-fill", json={})).json()
        self.assertEqual(rec_c["reconcile"]["reconciled_in_library"], 2)
        seasons_c = {s["id"]: s for s in (await self.http.get("/api/tasks/t_rec/seasons")).json()}
        self.assertEqual(seasons_c["rec_to"]["gap_fill_state"]["pushed_episodes"], [])

    async def test_03_abort_marking_reinspect_inheritance_superseding_and_legacy_fallback(self):
        """Mid-batch mark-aborted, new SeasonRecord.id inheritance across re-inspect, identity-change superseding, and legacy OperationLog fallback."""
        async with async_session() as db:
            db.add(Task(id="t_reinspect", name="重检迁移", source_path="/qa/src", status="completed"))
            db.add_all([
                SeasonRecord(
                    id="r_old_1", task_id="t_reinspect", show_name="剧一 [tmdbid=3001]", show_path="/qa/src/ShowOne [tmdbid=3001]",
                    tmdb_id="3001", season_number=1, season_path="/qa/src/ShowOne [tmdbid=3001]/Season 1",
                    total_episodes=4, local_episode_count=2,
                    recognized_episodes_json="[1, 2]", missing_episodes_json="[3, 4]",
                    status="download_needed", status_detail="缺 3, 4 集", season_finished=True,
                ),
                SeasonRecord(
                    id="r_old_2", task_id="t_reinspect", show_name="剧二 [tmdbid=3002]", show_path="/qa/src/ShowTwo [tmdbid=3002]",
                    tmdb_id="3002", season_number=1, season_path="/qa/src/ShowTwo [tmdbid=3002]/Season 1",
                    total_episodes=4, local_episode_count=2,
                    recognized_episodes_json="[1, 2]", missing_episodes_json="[3, 4]",
                    status="download_needed", status_detail="缺 3, 4 集", season_finished=True,
                ),
            ])
            await db.commit()

        # Submit ShowOne E03+E04, abort ShowTwo via API
        mp = StubMP(search_by_tmdb={3001: [
            _cand("ShowOne.S01E03.1080p.mkv", seeders=10, size=1_500_000_000, eps=[3], tmdb_id=3001),
            _cand("ShowOne.S01E04.1080p.mkv", seeders=10, size=1_500_000_000, eps=[4], tmdb_id=3001),
        ]})
        cd2 = StubCD2({
            **_show_tree("/qa/src/ShowOne [tmdbid=3001]", "ShowOne", 1, [1, 2]),
            **_show_tree("/qa/src/ShowTwo [tmdbid=3002]", "ShowTwo", 1, [1, 2]),
        })
        with patch.object(download_service, "MoviePilotClient", return_value=mp), \
             patch.object(cd2_mod, "CloudDrive2Client", return_value=cd2):
            async with async_session() as db:
                await download_service.execute_gap_fill_download(["r_old_1"], db, batch_run_id="b_abort")

        r_ab = await self.http.post("/api/moviepilot/gap-fill/mark-aborted", json={
            "task_id": "t_reinspect",
            "season_record_ids": ["r_old_2"],
            "batch_run_id": "b_abort",
        })
        self.assertEqual(r_ab.status_code, 200)
        self.assertEqual(r_ab.json()["marked_count"], 1)

        seasons_ab = {s["id"]: s for s in (await self.http.get("/api/tasks/t_reinspect/seasons")).json()}
        self.assertEqual(seasons_ab["r_old_1"]["gap_fill_state"]["view_bucket"], "pushed_pending")
        self.assertEqual(seasons_ab["r_old_2"]["gap_fill_state"]["view_bucket"], "todo")
        self.assertEqual(seasons_ab["r_old_2"]["gap_fill_state"]["last_run"]["outcome_status"], "aborted_unstarted")

        # Re-inspect creates new SeasonRecord.id rows: ShowOne keeps same identity -> MUST inherit pushed [3,4]!
        # ShowTwo switches identity (mapping to TMDB 3999 S02) -> previous identity records get superseded cleanly!
        fake_res = InspectionResult(task_id="t_reinspect", source_path="/qa/src")
        fake_res.seasons = [
            SeasonDiagnosis(
                show_name="剧一 [tmdbid=3001]", show_path="/qa/src/ShowOne [tmdbid=3001]", tmdb_id="3001", season_number=1,
                season_path="/qa/src/ShowOne [tmdbid=3001]/Season 1", total_episodes=4, local_episode_count=2,
                recognized_episodes=[1, 2], missing_episodes=[3, 4], duplicate_episodes=[],
                status="download_needed", status_detail="缺 3, 4 集", season_finished=True,
            ),
            SeasonDiagnosis(
                show_name="剧二 [tmdbid=3999]", show_path="/qa/src/ShowTwo [tmdbid=3002]", tmdb_id="3999", season_number=1,
                season_path="/qa/src/ShowTwo [tmdbid=3002]/Season 1", total_episodes=4, local_episode_count=2,
                recognized_episodes=[1, 2], missing_episodes=[3, 4], duplicate_episodes=[],
                status="download_needed", status_detail="缺 3, 4 集（已改绑新身份）", season_finished=True,
                mapping={"target_tmdb_id": 3999, "target_season": 2, "numbering_mode": "independent"},
            ),
        ]
        mock_cd2 = StubCD2()
        with patch.object(cd2_mod, "CloudDrive2Client", return_value=mock_cd2), \
             patch.object(mp_mod, "MoviePilotClient", return_value=mp), \
             patch("app.clients.tmdb.TMDBClient", return_value=StubCD2()), \
             patch("app.services.task_service.run_inspection", return_value=fake_res):
            async with async_session() as db:
                await execute_inspection("t_reinspect", db)

        new_seasons = {s["show_name"]: s for s in (await self.http.get("/api/tasks/t_reinspect/seasons")).json()}
        self.assertNotIn(new_seasons["剧一 [tmdbid=3001]"]["id"], {"r_old_1"})
        self.assertEqual(new_seasons["剧一 [tmdbid=3001]"]["gap_fill_state"]["view_bucket"], "pushed_pending")
        self.assertEqual(new_seasons["剧一 [tmdbid=3001]"]["gap_fill_state"]["pushed_episodes"], [3, 4])

        # Verify old ShowTwo identity rows became superseded while new identity shows fresh todo
        self.assertEqual(new_seasons["剧二 [tmdbid=3999]"]["gap_fill_state"]["view_bucket"], "todo")
        self.assertEqual(new_seasons["剧二 [tmdbid=3999]"]["gap_fill_state"]["actionable_missing_episodes"], [3, 4])

        # Legacy OperationLog fallback when GapFillEpisode rows are removed
        async with async_session() as db:
            await db.execute(delete(GapFillEpisode).where(GapFillEpisode.show_name == "剧一 [tmdbid=3001]"))
            db.add(OperationLog(
                task_id="t_reinspect",
                season_record_id=new_seasons["剧一 [tmdbid=3001]"]["id"],
                action="download_gap_fill",
                detail_json=json.dumps({
                    "show": "剧一",
                    "season": 1,
                    "search_tmdb_id": 3001,
                    "covered": [3, 4],
                    "mp_accepted": True,
                    "title": "ShowOne.S01E03-E04",
                    "site": "彩虹岛",
                }, ensure_ascii=False),
                status="completed",
            ))
            await db.commit()
            rec_one = await db.get(SeasonRecord, new_seasons["剧一 [tmdbid=3001]"]["id"])
            view_legacy = await compute_season_gap_fill_view(db, rec_one)
            self.assertEqual(sorted(view_legacy["pushed_episodes"]), [3, 4])
            self.assertEqual(view_legacy["view_bucket"], "pushed_pending")


if __name__ == "__main__":
    unittest.main()
