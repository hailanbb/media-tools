"""v2.5.32 重点功能自动化回归测试:
1. 缺集数量大于 3 集优先整季包下载，<= 3 集优先省容量散集；
2. 删除源端（本季）时，无其他有效季直接删除 show_path 并在初检目录强制刷新，有其他有效季仅删除 season_path。
"""

from __future__ import annotations

import os
import posixpath
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MF_DATA_DIR", tempfile.mkdtemp(prefix="gap-fill-v2532-"))

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.season import SeasonRecord
from app.models.task import Task
from app.models.gap_fill import GapFillEpisode
from app.services.download_service import select_best_candidates
from app.api.archive import (
    DeleteSourceSeasonPreviewRequest,
    DeleteSourceSeasonRequest,
    preview_delete_source_season,
    delete_source_season,
)


def _cand(
    title: str,
    *,
    seeders: int,
    size: int,
    eps: list[int] | None = None,
    season: int = 1,
    site: int = 1,
    site_name: str = "TestSite",
    files: list[str] | None = None,
):
    torrent = {
        "title": title,
        "seeders": seeders,
        "size": size,
        "site": site,
        "site_name": site_name,
    }
    if files:
        torrent["files"] = [{"name": f, "size": size // len(files)} for f in files]
    media = {
        "tmdb_id": 303491,
        "type": "tv",
    }
    meta = {
        "title": title,
        "season": season,
        "episodes": eps or [],
        "episode_list": eps or [],
    }
    return {
        "torrent_info": torrent,
        "media_info": media,
        "meta_info": meta,
    }


class TestGapFillV2532(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def asyncTearDown(self):
        await self.engine.dispose()

    def test_candidate_selection_prioritizes_season_pack_when_missing_greater_than_3(self):
        """缺集数量 > 3 集时，优先选择整季包；<= 3 集时优先散集省容量方案。"""
        # 散集候选 E01-E05
        standalone_candidates = [
            _cand("Show.S01E01.1080p", seeders=10, size=1_000_000_000, eps=[1]),
            _cand("Show.S01E02.1080p", seeders=10, size=1_000_000_000, eps=[2]),
            _cand("Show.S01E03.1080p", seeders=10, size=1_000_000_000, eps=[3]),
            _cand("Show.S01E04.1080p", seeders=10, size=1_000_000_000, eps=[4]),
            _cand("Show.S01E05.1080p", seeders=10, size=1_000_000_000, eps=[5]),
        ]
        # 整季包候选（覆盖全 10 集，带文件清单）
        season_pack_candidate = _cand(
            "Show.S01.Complete.1080p",
            seeders=15,
            size=8_000_000_000,
            eps=list(range(1, 11)),
            files=[f"Show.S01E{i:02d}.1080p.mkv" for i in range(1, 11)],
        )

        all_candidates = standalone_candidates + [season_pack_candidate]

        # 场景 1: 缺 4 集 (缺集数 > 3) -> 必须优先整季包
        missing_4_eps = [1, 2, 3, 4]
        plan_4 = select_best_candidates(
            all_candidates,
            needed=missing_4_eps,
            total=10,
            season=1,
            allow_season_pack=True,
        )
        self.assertEqual(len(plan_4), 1)
        cand, eps, is_pack = plan_4[0]
        self.assertTrue(is_pack, "缺 4 集 (>3集) 时应优先返回整季包")
        self.assertEqual(cand["torrent_info"]["title"], "Show.S01.Complete.1080p")

        # 场景 2: 缺 2 集 (缺集数 <= 3) -> 必须优先散集组合
        missing_2_eps = [1, 2]
        plan_2 = select_best_candidates(
            all_candidates,
            needed=missing_2_eps,
            total=10,
            season=1,
            allow_season_pack=True,
        )
        self.assertEqual(len(plan_2), 2)
        for _, _, is_pack in plan_2:
            self.assertFalse(is_pack, "缺 2 集 (<=3集) 时应优先返回省容量散集")

        # 场景 3: 缺 4 集但无整季包候选 -> 回退散集组合
        plan_fallback = select_best_candidates(
            standalone_candidates,
            needed=missing_4_eps,
            total=10,
            season=1,
            allow_season_pack=True,
        )
        self.assertEqual(len(plan_fallback), 4)
        for _, _, is_pack in plan_fallback:
            self.assertFalse(is_pack, "无整季包时应平滑回退至散集组合")

    @patch("app.api.archive.get_settings")
    @patch("app.api.archive.CloudDrive2Client")
    async def test_delete_source_season_cascades_to_show_dir_when_no_other_seasons(self, mock_cd2_cls, mock_get_settings):
        """当剧集无其他有效剧季时，删除源端本季应直接删除 show_path 并在 task.source_path 强制刷新。"""
        # Mock settings
        settings = MagicMock()
        settings.source_root = "/115/Library_incs/nas/电视剧"
        settings.archive_root = "/115/Media/电视剧"
        settings.mp_url = ""
        mock_get_settings.return_value = settings

        mock_cd2 = MagicMock()
        mock_cd2.list_files = AsyncMock(return_value=[])
        mock_cd2.delete_file = AsyncMock(return_value={"success": True})
        mock_cd2.close = AsyncMock()
        mock_cd2_cls.return_value = mock_cd2

        async with self.session_factory() as db:
            task = Task(
                id="task-test-01",
                name="香港剧集",
                source_path="/115/Library_incs/nas/电视剧/香港剧集",
            )
            db.add(task)
            rec = SeasonRecord(
                id="season-rec-01",
                task_id=task.id,
                show_name="抗战 (2025) [tmdbid=303491]",
                show_path="/115/Library_incs/nas/电视剧/香港剧集/抗战 (2025) [tmdbid=303491]",
                season_number=1,
                season_path="/115/Library_incs/nas/电视剧/香港剧集/抗战 (2025) [tmdbid=303491]/Season 1",
                status="download_needed",
                local_episode_count=0,
                missing_episodes_json="[1, 2, 3]",
            )
            db.add(rec)
            await db.commit()

            # 1. 测试预检 preview_delete_source_season
            preview_res = await preview_delete_source_season(
                DeleteSourceSeasonPreviewRequest(season_record_id=rec.id),
                db=db,
            )
            self.assertTrue(preview_res.get("ok"))
            self.assertTrue(preview_res.get("will_delete_show_dir"), "无其他季时应识别 will_delete_show_dir=True")
            self.assertEqual(preview_res.get("target_delete_path"), rec.show_path)
            self.assertEqual(preview_res.get("task_source_path"), "/115/Library_incs/nas/电视剧/香港剧集")

            # 2. 测试正式删除 delete_source_season
            del_res = await delete_source_season(
                DeleteSourceSeasonRequest(season_record_id=rec.id, confirm_abandon=True),
                db=db,
            )
            self.assertTrue(del_res.ok)
            # 断言删除的是整剧根目录 show_path
            mock_cd2.delete_file.assert_awaited_with(rec.show_path)
            # 断言在初检目录执行了 force_refresh 刷新
            mock_cd2.list_files.assert_awaited_with(task.source_path, force_refresh=True)

            # 检查记录状态被置为 source_absent
            await db.refresh(rec)
            self.assertEqual(rec.status, "source_absent")

    @patch("app.api.archive.get_settings")
    @patch("app.api.archive.CloudDrive2Client")
    async def test_delete_source_season_keeps_show_dir_when_other_active_seasons_exist(self, mock_cd2_cls, mock_get_settings):
        """当剧集仍有其他有效剧季时，删除源端本季仅删除 season_path，保护整剧根目录。"""
        settings = MagicMock()
        settings.source_root = "/115/Library_incs/nas/电视剧"
        settings.archive_root = "/115/Media/电视剧"
        settings.mp_url = ""
        mock_get_settings.return_value = settings

        mock_cd2 = MagicMock()
        mock_cd2.list_files = AsyncMock(return_value=[])
        mock_cd2.delete_file = AsyncMock(return_value={"success": True})
        mock_cd2.close = AsyncMock()
        mock_cd2_cls.return_value = mock_cd2

        async with self.session_factory() as db:
            task = Task(
                id="task-test-02",
                name="香港剧集",
                source_path="/115/Library_incs/nas/电视剧/香港剧集",
            )
            db.add(task)
            rec1 = SeasonRecord(
                id="season-rec-s01",
                task_id=task.id,
                show_name="大剧 (2025) [tmdbid=12345]",
                show_path="/115/Library_incs/nas/电视剧/香港剧集/大剧 (2025) [tmdbid=12345]",
                season_number=1,
                season_path="/115/Library_incs/nas/电视剧/香港剧集/大剧 (2025) [tmdbid=12345]/Season 1",
                status="download_needed",
                local_episode_count=0,
                missing_episodes_json="[1, 2]",
            )
            rec2 = SeasonRecord(
                id="season-rec-s02",
                task_id=task.id,
                show_name="大剧 (2025) [tmdbid=12345]",
                show_path="/115/Library_incs/nas/电视剧/香港剧集/大剧 (2025) [tmdbid=12345]",
                season_number=2,
                season_path="/115/Library_incs/nas/电视剧/香港剧集/大剧 (2025) [tmdbid=12345]/Season 2",
                status="download_needed",
                local_episode_count=0,
                missing_episodes_json="[5]",
            )
            db.add_all([rec1, rec2])
            await db.commit()

            # 1. 预检删除 S01
            preview_res = await preview_delete_source_season(
                DeleteSourceSeasonPreviewRequest(season_record_id=rec1.id),
                db=db,
            )
            self.assertTrue(preview_res.get("ok"))
            self.assertFalse(preview_res.get("will_delete_show_dir"), "存在活跃S02时，不应删除show_path")
            self.assertEqual(preview_res.get("target_delete_path"), rec1.season_path)

            # 2. 执行删除 S01
            del_res = await delete_source_season(
                DeleteSourceSeasonRequest(season_record_id=rec1.id, confirm_abandon=True),
                db=db,
            )
            self.assertTrue(del_res.ok)
            # 断言仅删除了本季目录 season_path，绝不误伤 show_path
            mock_cd2.delete_file.assert_awaited_with(rec1.season_path)
            # 并在初检目录刷新
            mock_cd2.list_files.assert_awaited_with(task.source_path, force_refresh=True)


if __name__ == "__main__":
    unittest.main()
