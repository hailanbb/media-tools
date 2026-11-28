"""v2.5.85 主动放弃补缺删源：无其他需保留季时一并删除剧集目录并刷新 CD2 专项回归测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("MF_DATA_DIR", tempfile.mkdtemp(prefix="mf-v2585-"))

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import source_discard
from app.database import Base
from app.models.gap_fill import GapFillSeasonRun
from app.models.season import SeasonRecord
from app.models.task import Task
from app.services.gap_fill_tracker import build_identity_key


def _file(path: str, size: int = 1024) -> dict:
    name = path.rsplit("/", 1)[-1]
    return {
        "name": name,
        "path": path,
        "id": path,
        "size": size,
        "modified": "2026-11-28T12:00:00",
        "isDirectory": False,
    }


def _folder(path: str) -> dict:
    name = path.rsplit("/", 1)[-1]
    return {
        "name": name,
        "path": path,
        "id": path,
        "size": 0,
        "modified": "2026-11-28T12:00:00",
        "isDirectory": True,
    }


class FakeCD2:
    def __init__(self, tree: dict[str, list[dict]]) -> None:
        self.tree = {k: [dict(x) for x in v] for k, v in tree.items()}
        self.deleted: list[str] = []
        self.refreshed: list[str] = []

    async def list_files(self, path: str, force_refresh: bool = False) -> list[dict]:
        if force_refresh:
            self.refreshed.append(path)
        if path not in self.tree:
            raise FileNotFoundError(f"not found: {path}")
        return [dict(x) for x in self.tree[path]]

    async def delete_file(self, path: str) -> dict:
        self.deleted.append(path)
        for k in list(self.tree.keys()):
            if k == path or k.startswith(path + "/"):
                self.tree.pop(k, None)
        for k in self.tree:
            self.tree[k] = [x for x in self.tree[k] if x["path"] != path]
        return {"success": True}

    async def close(self) -> None:
        pass


class TestV2585DiscardShowDir(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.settings = SimpleNamespace(
            source_root="/115/Library_incs/nas",
            archive_root="/115/Library_incs/nas.finish",
            mp_url="",
            mp_token="",
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_01_south_park_single_season_deletes_show_dir_and_refreshes_cd2(self) -> None:
        """复现南方公园 [tmdbid=2190] S27：无其他季且剧根仅剩刮削海报/NFO时，一并删除剧集目录并刷新初检目录。"""
        cat_dir = "/115/Library_incs/nas/动漫/欧美动漫"
        show_dir = f"{cat_dir}/南方公园 (1997) [tmdbid=2190]"
        season_dir = f"{show_dir}/Season 27"
        cd2 = FakeCD2({
            "/115/Library_incs/nas": [_folder("/115/Library_incs/nas/动漫")],
            "/115/Library_incs/nas/动漫": [_folder(cat_dir)],
            cat_dir: [_folder(show_dir)],
            show_dir: [
                _folder(season_dir),
                _file(f"{show_dir}/backdrop.jpg", 350245),
                _file(f"{show_dir}/poster.jpg", 611465),
                _file(f"{show_dir}/season27-poster.jpg", 608644),
                _file(f"{show_dir}/tvshow.nfo", 2520),
            ],
            season_dir: [
                _file(f"{season_dir}/South.Park.S27E01.1080p.mkv", 500_000_000),
                _file(f"{season_dir}/season.nfo", 400),
            ],
        })

        async with self.Session() as db:
            db.add(Task(id="t_sp", name="动漫总目录", source_path="/115/Library_incs/nas/动漫", media_type="anime", status="completed"))
            rec = SeasonRecord(
                id="rec_sp27",
                task_id="t_sp",
                show_name="南方公园 (1997) [tmdbid=2190]",
                show_path=show_dir,
                tmdb_id="2190",
                season_number=27,
                season_path=season_dir,
                total_episodes=6,
                local_episode_count=1,
                recognized_episodes_json="[1]",
                missing_episodes_json="[2,3,4,5,6]",
                status="download_needed",
                status_detail="缺 5 集",
            )
            db.add(rec)
            await db.commit()

            with (
                patch.object(source_discard, "CloudDrive2Client", return_value=cd2),
                patch.object(source_discard, "get_settings", return_value=self.settings),
            ):
                prev = (await source_discard.preview(source_discard.PreviewRequest(season_record_id="rec_sp27"), db))["data"]
                self.assertTrue(prev["will_delete_show_dir"])
                self.assertEqual(prev["target_delete_path"], show_dir)
                self.assertEqual(prev["file_stats"]["video_count"], 1)
                self.assertEqual(prev["file_stats"]["other_count"], 5)  # season.nfo + 4 个剧根刮削文件

                cd2.refreshed.clear()
                res = await source_discard.execute(
                    source_discard.ExecuteRequest(
                        season_record_id="rec_sp27",
                        preview_token=prev["preview_token"],
                        confirm_abandon=True,
                    ),
                    db,
                )
                self.assertTrue(res["ok"])
                self.assertTrue(res["deleted_show_dir"])
                self.assertEqual(cd2.deleted, [season_dir, show_dir])
                self.assertNotIn(show_dir, cd2.tree)
                # 既刷新了剧集父目录，也刷新了任务的初检目录
                self.assertIn(cat_dir, cd2.refreshed)
                self.assertIn("/115/Library_incs/nas/动漫", cd2.refreshed)
                ev = json.loads(rec.evidence_json)
                self.assertTrue(ev["source_presence"]["show_absent"])

    async def test_02_preserves_show_dir_when_other_season_exists_in_db_or_disk(self) -> None:
        """存在其他有效季（DB 或物理目录）时，仅删除本季目录，保留剧集外层目录。"""
        cat_dir = "/115/Library_incs/nas/动漫/欧美动漫"
        show_dir = f"{cat_dir}/多季番 [tmdbid=3000]"
        s1_dir = f"{show_dir}/Season 1"
        s2_dir = f"{show_dir}/Season 2"
        cd2 = FakeCD2({
            cat_dir: [_folder(show_dir)],
            show_dir: [_folder(s1_dir), _folder(s2_dir), _file(f"{show_dir}/tvshow.nfo", 300)],
            s1_dir: [_file(f"{s1_dir}/S01E01.mkv", 1000)],
            s2_dir: [_file(f"{s2_dir}/S02E01.mkv", 1000)],
        })

        async with self.Session() as db:
            db.add(Task(id="t_ms", name="欧美动漫", source_path=cat_dir, media_type="anime", status="completed"))
            db.add(
                SeasonRecord(
                    id="rec_s1",
                    task_id="t_ms",
                    show_name="多季番 [tmdbid=3000]",
                    show_path=show_dir,
                    tmdb_id="3000",
                    season_number=1,
                    season_path=s1_dir,
                    total_episodes=2,
                    local_episode_count=1,
                    recognized_episodes_json="[1]",
                    missing_episodes_json="[2]",
                    status="download_needed",
                )
            )
            await db.commit()

            with (
                patch.object(source_discard, "CloudDrive2Client", return_value=cd2),
                patch.object(source_discard, "get_settings", return_value=self.settings),
            ):
                prev = (await source_discard.preview(source_discard.PreviewRequest(season_record_id="rec_s1"), db))["data"]
                self.assertFalse(prev["will_delete_show_dir"])
                res = await source_discard.execute(
                    source_discard.ExecuteRequest(
                        season_record_id="rec_s1",
                        preview_token=prev["preview_token"],
                        confirm_abandon=True,
                    ),
                    db,
                )
                self.assertTrue(res["ok"])
                self.assertFalse(res["deleted_show_dir"])
                self.assertEqual(cd2.deleted, [s1_dir])
                self.assertIn(show_dir, cd2.tree)
                self.assertIn(s2_dir, cd2.tree)

    async def test_03_show_root_drift_after_preview_blocks_delete(self) -> None:
        """预览判定连删剧根后，若执行前剧根新增了新季度或视频，立即拦截拒绝误删。"""
        cat_dir = "/115/Library_incs/nas/动漫/欧美动漫"
        show_dir = f"{cat_dir}/防漂移番 [tmdbid=4000]"
        s1_dir = f"{show_dir}/Season 1"
        cd2 = FakeCD2({
            cat_dir: [_folder(show_dir)],
            show_dir: [_folder(s1_dir), _file(f"{show_dir}/poster.jpg", 500)],
            s1_dir: [_file(f"{s1_dir}/S01E01.mkv", 1000)],
        })

        async with self.Session() as db:
            db.add(Task(id="t_dr", name="欧美动漫", source_path=cat_dir, media_type="anime", status="completed"))
            db.add(
                SeasonRecord(
                    id="rec_dr1",
                    task_id="t_dr",
                    show_name="防漂移番 [tmdbid=4000]",
                    show_path=show_dir,
                    tmdb_id="4000",
                    season_number=1,
                    season_path=s1_dir,
                    total_episodes=2,
                    local_episode_count=1,
                    recognized_episodes_json="[1]",
                    missing_episodes_json="[2]",
                    status="download_needed",
                )
            )
            await db.commit()

            with (
                patch.object(source_discard, "CloudDrive2Client", return_value=cd2),
                patch.object(source_discard, "get_settings", return_value=self.settings),
            ):
                prev = (await source_discard.preview(source_discard.PreviewRequest(season_record_id="rec_dr1"), db))["data"]
                self.assertTrue(prev["will_delete_show_dir"])
                # 模拟预览后外部向剧根写入了 Season 2
                cd2.tree[show_dir].append(_folder(f"{show_dir}/Season 2"))
                cd2.tree[f"{show_dir}/Season 2"] = [_file(f"{show_dir}/Season 2/S02E01.mkv", 1000)]

                with self.assertRaises(HTTPException) as ctx:
                    await source_discard.execute(
                        source_discard.ExecuteRequest(
                            season_record_id="rec_dr1",
                            preview_token=prev["preview_token"],
                            confirm_abandon=True,
                        ),
                        db,
                    )
                self.assertEqual(ctx.exception.status_code, 409)
                self.assertIn("剧集目录在预览后发生变化", ctx.exception.detail)
                self.assertEqual(cd2.deleted, [])

    async def test_04_batch_discard_all_seasons_cleans_show_dir_on_last_season(self) -> None:
        """同剧 S01、S02 在无资源页被同时批量放弃删源时，删完最后一季后自动连带清理剧集目录并刷新 CD2。"""
        cat_dir = "/115/Library_incs/nas/动漫/欧美动漫"
        show_dir = f"{cat_dir}/双季放弃番 [tmdbid=5000]"
        s1_dir = f"{show_dir}/Season 1"
        s2_dir = f"{show_dir}/Season 2"
        cd2 = FakeCD2({
            cat_dir: [_folder(show_dir)],
            show_dir: [_folder(s1_dir), _folder(s2_dir), _file(f"{show_dir}/tvshow.nfo", 300)],
            s1_dir: [_file(f"{s1_dir}/S01E01.mkv", 1000)],
            s2_dir: [_file(f"{s2_dir}/S02E01.mkv", 1000)],
        })

        async with self.Session() as db:
            db.add(Task(id="t_b", name="欧美动漫", source_path=cat_dir, media_type="anime", status="completed"))
            for sn, sdir in ((1, s1_dir), (2, s2_dir)):
                show_name = "双季放弃番 [tmdbid=5000]"
                rec = SeasonRecord(
                    id=f"rec_b{sn}",
                    task_id="t_b",
                    show_name=show_name,
                    show_path=show_dir,
                    tmdb_id="5000",
                    season_number=sn,
                    season_path=sdir,
                    total_episodes=2,
                    local_episode_count=1,
                    recognized_episodes_json="[1]",
                    missing_episodes_json="[2]",
                    status="download_needed",
                )
                db.add(rec)
                db.add(
                    GapFillSeasonRun(
                        task_id="t_b",
                        season_record_id=f"rec_b{sn}",
                        season_path=sdir,
                        show_name=show_name,
                        season_number=sn,
                        identity_key=build_identity_key(show_name, "5000", sn),
                        outcome_status="zero_hits",
                        target_episodes_json="[2]",
                    )
                )
            await db.commit()

            with (
                patch.object(source_discard, "CloudDrive2Client", return_value=cd2),
                patch.object(source_discard, "get_settings", return_value=self.settings),
            ):
                b_prev = await source_discard.preview_batch(
                    source_discard.BatchPreviewRequest(task_id="t_b", season_record_ids=["rec_b1", "rec_b2"]),
                    db,
                )
                self.assertEqual(len(b_prev["items"]), 2)
                b_res = await source_discard.execute_batch(
                    source_discard.BatchExecuteRequest(preview_token=b_prev["preview_token"], confirm_abandon=True),
                    db,
                )
                self.assertTrue(b_res["ok"])
                self.assertEqual(b_res["completed"], 2)
                self.assertEqual(cd2.deleted, [s1_dir, s2_dir, show_dir])
                self.assertNotIn(show_dir, cd2.tree)
                self.assertIn(cat_dir, cd2.refreshed)


if __name__ == "__main__":
    unittest.main()
