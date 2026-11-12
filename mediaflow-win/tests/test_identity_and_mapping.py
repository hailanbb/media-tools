"""Doc 1 acceptance suite: identity conflict, ambiguous short titles, cross-entry mapping,
distinct metadata failure states, stale verified cache preservation, and read-only recalculate.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="identity-map-")
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
    MF_TMDB_API_KEY="tmdbtok",
)

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.clients import clouddrive2 as cd2_mod  # noqa: E402
from app.clients import tmdb as tmdb_mod  # noqa: E402
from app.core.inspector import run_inspection  # noqa: E402
from app.database import Base, async_session, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.gap_fill import GapFillEpisode  # noqa: E402
from app.models.metadata_cache import MetadataCache  # noqa: E402
from app.models.season import SeasonRecord  # noqa: E402
from app.models.season_mapping import SeasonMapping  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.services import download_service  # noqa: E402


class FakeCD2:
    def __init__(self, tree: dict[str, list[dict]]):
        self.tree = tree

    async def list_files(self, path: str, force_refresh: bool = False):
        return list(self.tree.get(path, []))

    async def file_exists(self, path: str) -> bool:
        return path in self.tree

    async def close(self):
        return None


def _dir(name: str, path: str) -> dict:
    return {"name": name, "path": path, "fullPathName": path, "isDirectory": True, "isFile": False, "size": 0}


def _file(name: str, path: str, size: int = 1_500_000_000) -> dict:
    return {"name": name, "path": path, "fullPathName": path, "isDirectory": False, "isFile": True, "size": size}


class FakeTMDB:
    def __init__(
        self,
        shows: dict[int, dict] | None = None,
        search_map: dict[str, list[dict]] | None = None,
        season_details: dict[tuple[int, int], dict] | None = None,
        fail_queries: set[str] | None = None,
        fail_shows: set[int] | None = None,
    ):
        self.shows = shows or {}
        self.search_map = search_map or {}
        self.season_details = season_details or {}
        self.fail_queries = {q.casefold() for q in (fail_queries or set())}
        self.fail_shows = set(fail_shows or set())

    async def fetch_search_tv(self, query: str, language: str | None = None):
        q = str(query or "").strip()
        if any(fq in q.casefold() for fq in self.fail_queries):
            return [], "error", f"TMDB timeout for {q}"
        for key, vals in self.search_map.items():
            if key.casefold() in q.casefold() or q.casefold() in key.casefold():
                return list(vals), "ok", None
        return [], "ok", None

    async def search_tv(self, query: str, year: int | None = None, first_air_year: int | None = None):
        res, status, _ = await self.fetch_search_tv(query)
        return res

    async def fetch_tv_detail(self, tv_id: int, language: str | None = None):
        if int(tv_id) in self.fail_shows:
            return {}, "error", f"TMDB details timeout for {tv_id}"
        if int(tv_id) in self.shows:
            return dict(self.shows[int(tv_id)]), "ok", None
        return {}, "not_found", "TMDB 返回 404"

    async def get_tv_detail(self, tv_id: int):
        data, _, _ = await self.fetch_tv_detail(tv_id)
        return data

    async def fetch_season_detail(self, tv_id: int, season_number: int, language: str | None = None):
        if int(tv_id) in self.fail_shows:
            return {}, "error", f"TMDB season timeout for {tv_id}"
        key = (int(tv_id), int(season_number))
        if key in self.season_details:
            return dict(self.season_details[key]), "ok", None
        return {}, "not_found", "TMDB 季不存在"

    async def get_season_detail(self, tv_id: int, season_number: int):
        data, _, _ = await self.fetch_season_detail(tv_id, season_number)
        return data

    async def find_by_imdb_id(self, imdb_id: str):
        return None

    async def close(self):
        return None


class FakeMP:
    def __init__(
        self,
        douban_search: dict[str, list[dict]] | None = None,
        douban_details: dict[str, dict] | None = None,
        media_details: dict[int, dict] | None = None,
        tmdb_seasons: dict[tuple[int, int], list[dict]] | None = None,
        torrents: dict[str, list[dict]] | None = None,
        sites: list[dict] | None = None,
    ):
        self.douban_search = douban_search or {}
        self.douban_details = douban_details or {}
        self.media_details = media_details or {}
        self.tmdb_seasons = tmdb_seasons or {}
        self.torrents = torrents or {}
        self.sites = sites if sites is not None else [{"id": 4, "name": "彩虹岛", "is_active": True}]
        self.search_calls: list[dict] = []
        self.submitted_torrents: list[dict] = []
        self.active_downloads: list[dict] = []

    async def search_media(self, title: str, source: str = "douban"):
        q = str(title or "").strip()
        for key, vals in self.douban_search.items():
            if key.casefold() in q.casefold() or q.casefold() in key.casefold():
                return list(vals)
        return []

    async def get_douban_detail(self, douban_id: str):
        return dict(self.douban_details.get(str(douban_id), {}))

    async def get_douban_media_info(self, douban_id: str, type_name: str = "电视剧"):
        return dict(self.douban_details.get(str(douban_id), {}))

    async def get_media_info(self, tmdb_id: int):
        return dict(self.media_details.get(int(tmdb_id), {}))

    async def get_tmdb_seasons(self, tmdb_id: int):
        raw = self.media_details.get(int(tmdb_id))
        if raw and "seasons" in raw:
            return raw["seasons"]
        return []

    async def get_tmdb_episodes(self, tmdb_id: int, season: int):
        return list(self.tmdb_seasons.get((int(tmdb_id), int(season)), []))

    async def search_resources(
        self,
        tmdb_id: int | None = None,
        season: int | None = None,
        title: str | None = None,
        sites: list[int] | tuple[int, ...] | set[int] | str | None = None,
    ):
        self.search_calls.append({
            "keyword": title,
            "tmdb_id": tmdb_id,
            "season": season,
            "sites": sites,
        })
        res: list[dict] = []
        for k, vals in self.torrents.items():
            if title and k.casefold() in title.casefold():
                res.extend(vals)
        if not res and tmdb_id and str(tmdb_id) in self.torrents:
            res.extend(self.torrents[str(tmdb_id)])
        return res

    async def get_sites(self):
        return list(self.sites)

    async def get_indexer_sites(self):
        return {"ok": True, "value": [int(s["id"]) for s in self.sites], "error": None}

    async def get_active_downloads(self):
        return list(self.active_downloads)

    async def get_transfer_overwrite_policy(self):
        return {"value": "never", "label": "存在即跳过"}

    async def clear_transfer_history_for_episodes(self, **kwargs):
        return {"cleared_count": 0, "records": [], "protected_count": 0}

    async def submit_download(self, media_info: dict, torrent_info: dict, **kwargs):
        self.submitted_torrents.append({"torrent_info": torrent_info, "media_info": media_info})
        return {"success": True, "download_id": "dl_mapped_1", "message": "ok"}

    async def close(self):
        return None


class IdentityAndMappingAcceptanceTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_01_gravesend_s08_release_conflict_vs_stripped_unlisted(self):
        """Gravesend S08: release clues trigger identity_review w/ ep overlap; stripped files -> not_listed unknown, rejecting Douban 36913489 S01=4."""
        show_path_conflict = "/qa/src/Gravesend (2020) [tmdbid=103737]"
        s8_conflict = f"{show_path_conflict}/Season 8"
        show_path_stripped = "/qa/src/Gravesend Stripped (2020) [tmdbid=103737]"
        s8_stripped = f"{show_path_stripped}/Season 8"

        cd2 = FakeCD2({
            "/qa/src": [_dir("Gravesend (2020) [tmdbid=103737]", show_path_conflict)],
            show_path_conflict: [_dir("Season 8", s8_conflict)],
            s8_conflict: [
                _file(
                    "Gravesend - S08E01 - The.Neighborhood.S08E01.Welcome.to.the.Neighborhood.1080p.mkv",
                    f"{s8_conflict}/Gravesend - S08E01 - The.Neighborhood.S08E01.Welcome.to.the.Neighborhood.1080p.mkv",
                ),
            ],
            "/qa/dst": [],
        })
        tmdb = FakeTMDB(
            shows={
                103737: {
                    "id": 103737,
                    "name": "Gravesend",
                    "original_name": "Gravesend",
                    "status": "Ended",
                    "seasons": [
                        {"season_number": 1, "episode_count": 4, "air_date": "2020-01-01"},
                        {"season_number": 2, "episode_count": 4, "air_date": "2022-01-01"},
                    ],
                },
                80350: {
                    "id": 80350,
                    "name": "The Neighborhood",
                    "original_name": "The Neighborhood",
                    "status": "Returning Series",
                    "seasons": [{"season_number": 8, "episode_count": 20, "air_date": "2025-10-13"}],
                },
            },
            search_map={
                "The Neighborhood": [{"id": 80350, "name": "The Neighborhood", "original_name": "The Neighborhood", "first_air_date": "2018-10-01"}],
            },
            season_details={
                (80350, 8): {
                    "season_number": 8,
                    "air_date": "2025-10-13",
                    "episodes": [
                        {"episode_number": 1, "name": "Welcome to the Neighborhood", "air_date": "2025-10-13"},
                        {"episode_number": 2, "name": "Second Ep", "air_date": "2025-10-20"},
                    ],
                },
            },
        )
        mp = FakeMP(
            douban_search={
                "Gravesend": [{"douban_id": "36913489", "title": "Gravesend 第一季", "season": 1, "type": "电视剧"}],
            },
            douban_details={
                "36913489": {"id": "36913489", "title": "Gravesend 第一季", "episodes_count": 4, "season": 1, "tmdb_id": 103737},
            },
        )

        async with async_session() as db:
            res_conflict = await run_inspection(
                task_id="t_grav_1",
                source_path="/qa/src",
                cd2_client=cd2,
                mp_client=mp,
                tmdb_client=tmdb,
                archive_root="/qa/dst",
                db=db,
            )
            self.assertEqual(len(res_conflict.seasons), 1)
            d1 = res_conflict.seasons[0]
            self.assertEqual(d1.status, "identity_review")
            self.assertTrue(d1.conflict.get("blocking"))
            self.assertEqual(d1.conflict.get("kind"), "identity_conflict")
            self.assertEqual(d1.conflict.get("detected_work", {}).get("tmdb_id"), 80350)
            self.assertTrue(d1.conflict.get("episode_title_matches"))
            self.assertIn("Welcome to the Neighborhood", json.dumps(d1.conflict, ensure_ascii=False))

            # Part B: Stripped filenames with zero release clues -> must become unknown (not_listed), rejecting Douban 36913489 S01=4
            cd2_stripped = FakeCD2({
                "/qa/src": [_dir("Gravesend Stripped (2020) [tmdbid=103737]", show_path_stripped)],
                show_path_stripped: [_dir("Season 8", s8_stripped)],
                s8_stripped: [_file("Gravesend - S08E01.mkv", f"{s8_stripped}/Gravesend - S08E01.mkv")],
                "/qa/dst": [],
            })
            res_stripped = await run_inspection(
                task_id="t_grav_2",
                source_path="/qa/src",
                cd2_client=cd2_stripped,
                mp_client=mp,
                tmdb_client=tmdb,
                archive_root="/qa/dst",
                db=db,
            )
            self.assertEqual(len(res_stripped.seasons), 1)
            d2 = res_stripped.seasons[0]
            self.assertEqual(d2.status, "unknown")
            self.assertIsNone(d2.total_episodes)
            self.assertEqual(d2.evidence.get("meta_status"), "not_listed")
            self.assertNotIn("36913489", d2.status_detail)

    async def test_02_kingdom_ambiguous_short_title_and_reversible_season_mapping(self):
        """搏击王国 S06 (61427 vs 46437 Kingdom 2025 Baha): ambiguous_title never auto-rebinds; single-season confirm & revoke leaves S01 untouched."""
        show_path = "/qa/src/搏击王国 (2014) [tmdbid=61427]"
        s1_path = f"{show_path}/Season 1"
        s6_path = f"{show_path}/Season 6"

        cd2 = FakeCD2({
            "/qa/src": [_dir("搏击王国 (2014) [tmdbid=61427]", show_path)],
            show_path: [_dir("Season 1", s1_path), _dir("Season 6", s6_path)],
            s1_path: [_file(f"Kingdom.S01E{i:02d}.mkv", f"{s1_path}/Kingdom.S01E{i:02d}.mkv") for i in range(1, 11)],
            s6_path: [_file("Kingdom.S06E01.2025.1080p.Baha.WEB-DL.mkv", f"{s6_path}/Kingdom.S06E01.2025.1080p.Baha.WEB-DL.mkv")],
            "/qa/dst": [],
        })
        tmdb = FakeTMDB(
            shows={
                61427: {
                    "id": 61427,
                    "name": "搏击王国",
                    "original_name": "Kingdom",
                    "status": "Ended",
                    "seasons": [{"season_number": 1, "episode_count": 10, "air_date": "2014-10-08"}],
                },
                46437: {
                    "id": 46437,
                    "name": "王者天下",
                    "original_name": "Kingdom",
                    "status": "Ended",
                    "seasons": [{"season_number": 6, "episode_count": 13, "air_date": "2025-10-01"}],
                },
            },
            search_map={
                "Kingdom": [
                    {"id": 61427, "name": "搏击王国", "original_name": "Kingdom", "first_air_date": "2014-10-08"},
                    {"id": 46437, "name": "王者天下", "original_name": "Kingdom", "first_air_date": "2012-06-04"},
                ],
            },
            season_details={
                (61427, 1): {"season_number": 1, "air_date": "2014-10-08", "episodes": [{"episode_number": i, "air_date": "2014-10-08"} for i in range(1, 11)]},
                (46437, 6): {"season_number": 6, "air_date": "2025-10-01", "episodes": [{"episode_number": i, "air_date": "2025-10-01"} for i in range(1, 14)]},
            },
        )
        mp = FakeMP()

        async with async_session() as db:
            res = await run_inspection(
                task_id="t_kingdom",
                source_path="/qa/src",
                cd2_client=cd2,
                mp_client=mp,
                tmdb_client=tmdb,
                archive_root="/qa/dst",
                db=db,
            )
            by_sn = {s.season_number: s for s in res.seasons}
            self.assertEqual(by_sn[1].status, "archive_ready")
            self.assertEqual(by_sn[6].status, "identity_review")
            self.assertEqual(by_sn[6].conflict.get("kind"), "ambiguous_title")
            self.assertTrue(by_sn[6].conflict.get("ambiguous"))
            self.assertTrue(by_sn[6].conflict.get("blocking"))
            self.assertEqual(by_sn[6].tmdb_id, 61427)  # Never auto-rebound!

            # Persist Task + SeasonRecords to test API conflict guards + single-season mapping & revocation
            db.add(Task(id="t_kingdom", name="Kingdom Test", source_path="/qa/src", status="completed"))
            rec_s1 = SeasonRecord(
                id="rec_k_s1",
                task_id="t_kingdom",
                show_name=by_sn[1].show_name,
                show_path=show_path,
                tmdb_id="61427",
                season_number=1,
                season_path=s1_path,
                total_episodes=10,
                local_episode_count=10,
                recognized_episodes_json=json.dumps(list(range(1, 11))),
                missing_episodes_json="[]",
                status=by_sn[1].status,
                status_detail=by_sn[1].status_detail,
                season_finished=True,
                evidence_json=json.dumps(by_sn[1].evidence, ensure_ascii=False),
                conflict_json="{}",
                mapping_json="{}",
            )
            rec_s6 = SeasonRecord(
                id="rec_k_s6",
                task_id="t_kingdom",
                show_name=by_sn[6].show_name,
                show_path=show_path,
                tmdb_id="61427",
                season_number=6,
                season_path=s6_path,
                total_episodes=None,
                local_episode_count=1,
                recognized_episodes_json="[1]",
                missing_episodes_json="[]",
                status=by_sn[6].status,
                status_detail=by_sn[6].status_detail,
                evidence_json=json.dumps(by_sn[6].evidence, ensure_ascii=False),
                conflict_json=json.dumps(by_sn[6].conflict, ensure_ascii=False),
                mapping_json="{}",
            )
            db.add_all([rec_s1, rec_s6])
            await db.commit()

        # Blocking conflict must reject clean/archive/download actions cleanly (ok=False + 安全拦截)
        r_arch = await self.http.post("/api/archive/execute", json={"season_record_ids": ["rec_k_s6"]})
        self.assertEqual(r_arch.status_code, 200, r_arch.text)
        self.assertFalse(r_arch.json()["ok"])
        self.assertIn("安全拦截", r_arch.json()["message"])
        r_clean = await self.http.post("/api/archive/clean", json={"season_record_ids": ["rec_k_s6"]})
        self.assertEqual(r_clean.status_code, 200, r_clean.text)
        self.assertFalse(r_clean.json()["ok"])
        self.assertTrue("安全拦截" in r_clean.json()["message"] or "不适用" in r_clean.json()["message"])
        r_dl = await self.http.post("/api/moviepilot/download-gap-fill", json={"season_record_ids": ["rec_k_s6"]})
        self.assertEqual(r_dl.status_code, 200, r_dl.text)
        self.assertFalse(r_dl.json()["ok"])
        self.assertTrue(r_dl.json().get("skipped"))

        # User confirms single-season mapping for S06 ONLY (target 46437, season 6, 13 eps)
        r_map = await self.http.post(
            "/api/tasks/t_kingdom/seasons/rec_k_s6/season-mapping",
            json={
                "target_tmdb_id": 46437,
                "target_season": 6,
                "target_title": "王者天下 第六季",
                "expected_total_episodes": 13,
                "mapping_kind": "work_rebind",
                "evidence_note": "确认 S06 为 2025 巴哈版《王者天下》",
            },
        )
        self.assertEqual(r_map.status_code, 200, r_map.text)
        data_map = r_map.json()
        self.assertTrue(data_map["ok"])
        self.assertEqual(data_map["season"]["status"], "download_needed")
        self.assertEqual(data_map["season"]["total_episodes"], 13)
        self.assertEqual(data_map["season"]["mapping"]["target_tmdb_id"], 46437)
        self.assertFalse(data_map["season"]["conflict"].get("blocking"))

        # Confirm S01 is completely untouched!
        seasons_after_map = {s["id"]: s for s in (await self.http.get("/api/tasks/t_kingdom/seasons")).json()}
        self.assertEqual(seasons_after_map["rec_k_s1"]["status"], "archive_ready")
        self.assertEqual(seasons_after_map["rec_k_s1"]["tmdb_id"], "61427")
        self.assertEqual(seasons_after_map["rec_k_s1"]["mapping"], {})

        # Revoke S06 single-season mapping -> restores identity_review, S01 still untouched
        r_del = await self.http.delete("/api/tasks/t_kingdom/seasons/rec_k_s6/season-mapping")
        self.assertEqual(r_del.status_code, 200, r_del.text)
        self.assertTrue(r_del.json()["ok"])
        self.assertEqual(r_del.json()["season"]["status"], "identity_review")
        self.assertEqual(r_del.json()["season"]["mapping"], {})
        seasons_after_rev = {s["id"]: s for s in (await self.http.get("/api/tasks/t_kingdom/seasons")).json()}
        self.assertEqual(seasons_after_rev["rec_k_s1"]["status"], "archive_ready")

    async def test_03_the_terror_s03_seed_mapping_dual_lib_and_mapped_gap_fill(self):
        """极地恶灵 S03 (75191/S03 -> 323903/S01, douban 36768839, Ended 6 eps): dual-archive E02 coverage & mapped gap-fill rejects wrong shows."""
        show_src = "/qa/src/极地恶灵 (2018) [tmdbid=75191]"
        s3_src = f"{show_src}/Season 3"
        archive_alias_dir = "/qa/dst/银色恶魔 (2025) [tmdbid=323903]"
        archive_s1 = f"{archive_alias_dir}/Season 1"

        cd2 = FakeCD2({
            "/qa/src": [_dir("极地恶灵 (2018) [tmdbid=75191]", show_src)],
            show_src: [_dir("Season 3", s3_src)],
            s3_src: [_file("极地恶灵 - S03E01.1080p.mkv", f"{s3_src}/极地恶灵 - S03E01.1080p.mkv")],
            "/qa/dst": [_dir("银色恶魔 (2025) [tmdbid=323903]", archive_alias_dir)],
            archive_alias_dir: [_dir("Season 1", archive_s1)],
            archive_s1: [_file("银色恶魔 - S01E02.1080p.mkv", f"{archive_s1}/银色恶魔 - S01E02.1080p.mkv")],
        })
        tmdb = FakeTMDB(
            shows={
                75191: {
                    "id": 75191,
                    "name": "极地恶灵",
                    "original_name": "The Terror",
                    "status": "Ended",
                    "seasons": [{"season_number": 1, "episode_count": 10}, {"season_number": 2, "episode_count": 10}],
                },
                323903: {
                    "id": 323903,
                    "name": "极地恶灵：银色恶魔",
                    "original_name": "The Terror: Devil in Silver",
                    "status": "Ended",
                    "seasons": [{"season_number": 1, "episode_count": 6, "air_date": "2025-05-01"}],
                },
            },
            season_details={
                (323903, 1): {
                    "season_number": 1,
                    "air_date": "2025-05-01",
                    "episodes": [
                        {"episode_number": i, "name": ("Starry Night" if i == 6 else f"Ep {i}"), "air_date": "2025-05-01"}
                        for i in range(1, 7)
                    ],
                },
            },
        )
        # Seeded torrents contain wrong The Terror S01 (75191), unrelated Kingdom, AND true mapped Devil in Silver (323903 S01E03)
        wrong_terror_s1 = {
            "torrent_info": {"title": "The.Terror.S01E03.1080p.BluRay.mkv", "seeders": 25, "size": 2_000_000_000, "site": 4, "site_name": "彩虹岛"},
            "media_info": {"tmdb_id": 75191},
            "meta_info": {"season_list": [1], "episode_list": [3]},
        }
        wrong_kingdom = {
            "torrent_info": {"title": "Kingdom.S01E03.1080p.WEB-DL.mkv", "seeders": 30, "size": 2_000_000_000, "site": 4, "site_name": "彩虹岛"},
            "media_info": {"tmdb_id": 46437},
            "meta_info": {"season_list": [1], "episode_list": [3]},
        }
        valid_silver_s1 = {
            "torrent_info": {"title": "The.Terror.Devil.in.Silver.S01E03.1080p.WEB-DL.mkv", "seeders": 8, "size": 2_000_000_000, "site": 4, "site_name": "彩虹岛"},
            "media_info": {"tmdb_id": 323903},
            "meta_info": {"season_list": [1], "episode_list": [3]},
        }
        mp = FakeMP(
            douban_details={"36768839": {"id": "36768839", "title": "极地恶灵：银色恶魔", "episodes_count": 6}},
            torrents={"323903": [wrong_terror_s1, wrong_kingdom, valid_silver_s1], "极地恶灵": [wrong_terror_s1, wrong_kingdom, valid_silver_s1], "Devil in Silver": [valid_silver_s1]},
        )

        async with async_session() as db:
            res = await run_inspection(
                task_id="t_terror",
                source_path="/qa/src",
                cd2_client=cd2,
                mp_client=mp,
                tmdb_client=tmdb,
                archive_root="/qa/dst",
                db=db,
            )
            self.assertEqual(len(res.seasons), 1)
            d = res.seasons[0]
            self.assertEqual(d.mapping.get("target_tmdb_id"), 323903)
            self.assertEqual(d.mapping.get("target_season"), 1)
            self.assertEqual(d.total_episodes, 6)
            self.assertTrue(d.season_finished)
            self.assertEqual(sorted(d.recognized_episodes), [1, 2])
            self.assertEqual(d.missing_episodes, [3, 4, 5, 6])
            self.assertEqual(d.status, "download_needed")

            db.add(Task(id="t_terror", name="Terror", source_path="/qa/src", status="completed"))
            db.add(SeasonRecord(
                id="rec_terror_s3",
                task_id="t_terror",
                show_name=d.show_name,
                show_path=show_src,
                tmdb_id="75191",
                season_number=3,
                season_path=s3_src,
                total_episodes=6,
                local_episode_count=1,
                recognized_episodes_json=json.dumps(d.recognized_episodes),
                missing_episodes_json=json.dumps(d.missing_episodes),
                status="download_needed",
                status_detail=d.status_detail,
                archive_target_path=d.archive_target_path,
                season_finished=True,
                evidence_json=json.dumps(d.evidence, ensure_ascii=False),
                conflict_json="{}",
                mapping_json=json.dumps(d.mapping, ensure_ascii=False),
            ))
            await db.commit()

            with patch.object(download_service, "MoviePilotClient", return_value=mp), \
                 patch.object(cd2_mod, "CloudDrive2Client", return_value=cd2), \
                 patch.object(tmdb_mod, "TMDBClient", return_value=tmdb):
                out = await download_service.execute_gap_fill_download(["rec_terror_s3"], db, batch_run_id="b_terror")

        # Must have searched using mapped coordinates (323903, S01), NEVER blindly pushed The.Terror.S01 or Kingdom!
        self.assertTrue(any(c.get("tmdb_id") == 323903 and c.get("season") == 1 for c in mp.search_calls))
        submitted_titles = [s["torrent_info"]["title"] for s in mp.submitted_torrents]
        self.assertEqual(submitted_titles, ["The.Terror.Devil.in.Silver.S01E03.1080p.WEB-DL.mkv"])
        self.assertNotIn("The.Terror.S01E03.1080p.BluRay.mkv", submitted_titles)
        self.assertNotIn("Kingdom.S01E03.1080p.WEB-DL.mkv", submitted_titles)
        self.assertEqual(len(out["submitted"]), 1)

    async def test_04_metadata_failure_distinction_stale_cache_mixed_works_and_readonly_recalc(self):
        """Distinct meta_status (fetch_failed / no_candidate / episodes_tba), stale cache reuse on timeout, mixed works conflict, and readonly /recalculate preserving manually_handled."""
        p_fail = "/qa/src/TimeoutShow (2024)"
        p_none = "/qa/src/NonExistXYZ (2024)"
        p_tba = "/qa/src/UnannouncedShow (2025) [tmdbid=88880]"
        p_cached = "/qa/src/CachedShow (2023) [tmdbid=77770]"
        p_mixed = "/qa/src/MixedShow (2023) [tmdbid=66660]"

        cd2 = FakeCD2({
            "/qa/src": [
                _dir("TimeoutShow (2024)", p_fail),
                _dir("NonExistXYZ (2024)", p_none),
                _dir("UnannouncedShow (2025) [tmdbid=88880]", p_tba),
                _dir("CachedShow (2023) [tmdbid=77770]", p_cached),
                _dir("MixedShow (2023) [tmdbid=66660]", p_mixed),
            ],
            p_fail: [_dir("Season 1", f"{p_fail}/Season 1")],
            f"{p_fail}/Season 1": [_file("TimeoutShow.S01E01.mkv", f"{p_fail}/Season 1/TimeoutShow.S01E01.mkv")],
            p_none: [_dir("Season 1", f"{p_none}/Season 1")],
            f"{p_none}/Season 1": [_file("NonExistXYZ.S01E01.mkv", f"{p_none}/Season 1/NonExistXYZ.S01E01.mkv")],
            p_tba: [_dir("Season 1", f"{p_tba}/Season 1")],
            f"{p_tba}/Season 1": [_file("UnannouncedShow.S01E01.mkv", f"{p_tba}/Season 1/UnannouncedShow.S01E01.mkv")],
            p_cached: [_dir("Season 1", f"{p_cached}/Season 1")],
            f"{p_cached}/Season 1": [_file("CachedShow.S01E01.mkv", f"{p_cached}/Season 1/CachedShow.S01E01.mkv")],
            p_mixed: [_dir("Season 1", f"{p_mixed}/Season 1")],
            f"{p_mixed}/Season 1": [
                _file("MixedShow.S01E01.1080p.mkv", f"{p_mixed}/Season 1/MixedShow.S01E01.1080p.mkv"),
                _file("Totally.Other.Drama.S01E02.1080p.mkv", f"{p_mixed}/Season 1/Totally.Other.Drama.S01E02.1080p.mkv"),
            ],
            "/qa/dst": [],
        })
        tmdb = FakeTMDB(
            shows={
                88880: {"id": 88880, "name": "UnannouncedShow", "original_name": "UnannouncedShow", "status": "In Production", "seasons": [{"season_number": 1, "episode_count": 0}]},
                66660: {"id": 66660, "name": "MixedShow", "original_name": "MixedShow", "status": "Ended", "seasons": [{"season_number": 1, "episode_count": 4}]},
            },
            fail_queries={"TimeoutShow"},
            fail_shows={77770},
        )
        mp = FakeMP()

        async with async_session() as db:
            # Pre-seed verified MetadataCache snapshot for CachedShow (77770 S01 = 2 eps, finished)
            db.add(MetadataCache(
                cache_key="tmdb:77770:S1",
                source="tmdb_season",
                media_id="77770",
                season_number=1,
                payload_json=json.dumps({
                    "total": 2,
                    "episode_count": 2,
                    "finished": True,
                    "air_date": "2023-01-01",
                    "status": "Ended",
                    "episodes": [{"episode_number": 1}, {"episode_number": 2}],
                }),
                verified=True,
                status="ok",
                fetched_at=datetime.now(timezone.utc),
            ))
            await db.commit()

            res = await run_inspection(
                task_id="t_meta",
                source_path="/qa/src",
                cd2_client=cd2,
                mp_client=mp,
                tmdb_client=tmdb,
                archive_root="/qa/dst",
                db=db,
            )
            by_name = {s.show_name: s for s in res.seasons}

            self.assertEqual(by_name["TimeoutShow (2024)"].status, "unknown")
            self.assertEqual(by_name["TimeoutShow (2024)"].evidence.get("meta_status"), "fetch_failed")

            self.assertEqual(by_name["NonExistXYZ (2024)"].status, "unknown")
            self.assertEqual(by_name["NonExistXYZ (2024)"].evidence.get("meta_status"), "no_candidate")

            self.assertEqual(by_name["UnannouncedShow (2025) [tmdbid=88880]"].status, "unknown")
            self.assertEqual(by_name["UnannouncedShow (2025) [tmdbid=88880]"].evidence.get("meta_status"), "episodes_tba")

            # Timeout on 77770 reused stale verified cache rather than degrading to unknown!
            self.assertTrue(by_name["CachedShow (2023) [tmdbid=77770]"].evidence.get("stale_cache_reused"))
            self.assertEqual(by_name["CachedShow (2023) [tmdbid=77770]"].status, "download_needed")
            self.assertEqual(by_name["CachedShow (2023) [tmdbid=77770]"].missing_episodes, [2])

            # Mixed works in same directory blocked as identity_review / mixed_works
            self.assertEqual(by_name["MixedShow (2023) [tmdbid=66660]"].status, "identity_review")
            self.assertEqual(by_name["MixedShow (2023) [tmdbid=66660]"].conflict.get("kind"), "mixed_works")

            # Verify read-only /recalculate preserves manually_handled without modifying files
            db.add(Task(id="t_recalc", name="Recalc", source_path="/qa/src", status="completed"))
            db.add_all([
                SeasonRecord(
                    id="r_handled",
                    task_id="t_recalc",
                    show_name="HandledShow",
                    show_path="/qa/src/HandledShow",
                    tmdb_id="1",
                    season_number=1,
                    season_path="/qa/src/HandledShow/Season 1",
                    total_episodes=10,
                    local_episode_count=10,
                    recognized_episodes_json="[1]",
                    status="manually_handled",
                    status_detail="已人工确认",
                ),
                SeasonRecord(
                    id="r_cached",
                    task_id="t_recalc",
                    show_name="CachedShow",
                    show_path=p_cached,
                    tmdb_id="77770",
                    season_number=1,
                    season_path=f"{p_cached}/Season 1",
                    total_episodes=2,
                    local_episode_count=2,
                    recognized_episodes_json="[1, 2]",
                    status="download_needed",
                    status_detail="stale",
                    season_finished=True,
                ),
            ])
            await db.commit()

        r_rec = await self.http.post("/api/tasks/t_recalc/recalculate", json={"fetch_remote": False})
        self.assertEqual(r_rec.status_code, 200, r_rec.text)
        body_rec = r_rec.json()
        self.assertTrue(body_rec["ok"])
        self.assertEqual(body_rec["preserved_handled"], 1)
        self.assertEqual(body_rec["updated_count"], 1)
        async with async_session() as db:
            r_h = await db.get(SeasonRecord, "r_handled")
            r_c = await db.get(SeasonRecord, "r_cached")
            self.assertEqual(r_h.status, "manually_handled")
            self.assertEqual(r_c.status, "archive_ready")


if __name__ == "__main__":
    unittest.main()
