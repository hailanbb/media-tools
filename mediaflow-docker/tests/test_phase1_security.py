"""阶段一安全底线专项测试 (F01, F02, F06, F12, P0-1, P0-3)"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from app.core.review_engine import generate_review_clean_plan
from app.api.archive import _safe_cleanup_source_show_dir, delete_source_show, DeleteSourceShowRequest, clean_source_redundant, _ArchiveRequest
from app.models.operation import OperationLog
from app.models.season import SeasonRecord

class TestPhase1Security(unittest.TestCase):
    def test_f06_subtitle_and_thumb_protection(self):
        """F06: 验证外挂字幕绝不被误删，且保留视频的 -thumb 缩略图不被误当孤儿删除。"""
        files = [
            {"name": "Show.S01E01.1080p.mkv", "path": "/path/Show.S01E01.1080p.mkv", "size": 1000, "isDirectory": False},
            {"name": "Show.S01E01.720p.mkv", "path": "/path/Show.S01E01.720p.mkv", "size": 500, "isDirectory": False},
            {"name": "Show.S01E01.1080p.chs.srt", "path": "/path/Show.S01E01.1080p.chs.srt", "size": 10, "isDirectory": False},
            {"name": "Show.S01E01.720p.ass", "path": "/path/Show.S01E01.720p.ass", "size": 15, "isDirectory": False},
            {"name": "Show.S01E01.1080p-thumb.jpg", "path": "/path/Show.S01E01.1080p-thumb.jpg", "size": 50, "isDirectory": False},
            {"name": "Show.S01E01.720p-thumb.jpg", "path": "/path/Show.S01E01.720p-thumb.jpg", "size": 40, "isDirectory": False},
        ]
        
        plan = generate_review_clean_plan(files, season_number=1, total_episodes=1)
        self.assertTrue(plan["can_clean"])
        
        to_delete_names = [d["name"] for d in plan["to_delete"]]
        to_keep_names = [k["name"] for k in plan["to_keep"]]
        
        self.assertIn("Show.S01E01.1080p.mkv", to_keep_names)
        self.assertIn("Show.S01E01.720p.mkv", to_delete_names)
        self.assertNotIn("Show.S01E01.1080p-thumb.jpg", to_delete_names)
        self.assertNotIn("Show.S01E01.1080p.chs.srt", to_delete_names)
        self.assertNotIn("Show.S01E01.720p.ass", to_delete_names)

    def test_f01_safe_cleanup_source_show_dir_extras_protection(self):
        """F01: 当源端含有 Extras 或自定义子目录时，绝对不能删除剧集目录。"""
        cd2_mock = MagicMock()
        cd2_mock.list_files = AsyncMock(return_value=[
            {"name": "Extras", "path": "/src/Show/Extras", "isDirectory": True},
            {"name": "tvshow.nfo", "path": "/src/Show/tvshow.nfo", "isDirectory": False},
        ])
        cd2_mock.delete_file = AsyncMock()
        
        cleaned = asyncio.run(_safe_cleanup_source_show_dir(cd2_mock, "/src/Show", "/target/Show"))
        self.assertFalse(cleaned)
        cd2_mock.delete_file.assert_not_called()

    def test_p03_delete_source_show_rejects_source_root_itself(self):
        """P0-3: 绝对禁止将受控源根目录本身作为删除对象。"""
        db_mock = MagicMock()
        fake_rec = SeasonRecord(
            id="rec_root",
            task_id="task_1",
            show_name="SourceRootFakeShow",
            show_path="/115/Library_incs/nas",
            season_number=1,
            season_path="/115/Library_incs/nas/Season 1",
        )
        db_mock.get = AsyncMock(return_value=fake_rec)
        
        with patch("app.api.archive.get_settings") as mock_settings:
            s = MagicMock()
            s.source_root = "/115/Library_incs/nas"
            s.archive_root = "/115/Media_Archive"
            mock_settings.return_value = s
            
            with patch("app.api.archive.CloudDrive2Client") as mock_cd2_cls:
                mock_cd2 = MagicMock()
                mock_cd2_cls.return_value = mock_cd2
                
                resp = asyncio.run(delete_source_show(DeleteSourceShowRequest(season_record_id="rec_root"), db_mock))
                self.assertFalse(resp.ok)
                self.assertIn("禁止删除受控源根目录本身", resp.message)
                mock_cd2.delete_file.assert_not_called()

    def test_p03_delete_source_show_rejects_archive_root(self):
        """P0-3: 绝对禁止将归档库路径作为源端删除对象。"""
        db_mock = MagicMock()
        fake_rec = SeasonRecord(
            id="rec_arch",
            task_id="task_1",
            show_name="ArchiveShow",
            show_path="/115/Media_Archive/Show",
            season_number=1,
            season_path="/115/Media_Archive/Show/Season 1",
        )
        db_mock.get = AsyncMock(return_value=fake_rec)
        
        with patch("app.api.archive.get_settings") as mock_settings:
            s = MagicMock()
            s.source_root = "/115/Library_incs/nas"
            s.archive_root = "/115/Media_Archive"
            mock_settings.return_value = s
            
            with patch("app.api.archive.CloudDrive2Client") as mock_cd2_cls:
                mock_cd2 = MagicMock()
                mock_cd2_cls.return_value = mock_cd2
                
                resp = asyncio.run(delete_source_show(DeleteSourceShowRequest(season_record_id="rec_arch"), db_mock))
                self.assertFalse(resp.ok)
                mock_cd2.delete_file.assert_not_called()

    def test_p01_clean_source_redundant_rejects_missing_eps_on_target(self):
        """P0-1: 源端有 E01, E02，目标端只有 E01，坚决拦截不删源端！断言零删除！"""
        fake_rec = SeasonRecord(
            id="rec_1",
            task_id="task_1",
            show_name="TestShow",
            season_number=1,
            season_path="/115/Library_incs/nas/TestShow/Season 1",
            show_path="/115/Library_incs/nas/TestShow",
            archive_target_path="/115/Media_Archive/TestShow/Season 1",
            status="already_archived",
        )
        db_mock = MagicMock()
        exec_mock = MagicMock()
        exec_mock.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[fake_rec])))
        db_mock.execute = AsyncMock(return_value=exec_mock)
        db_mock.get = AsyncMock(return_value=None)
        db_mock.commit = AsyncMock()

        with patch("app.api.archive.get_settings") as mock_settings:
            s = MagicMock()
            s.source_root = "/115/Library_incs/nas"
            s.archive_root = "/115/Media_Archive"
            mock_settings.return_value = s

            with patch("app.api.archive.CloudDrive2Client") as mock_cd2_cls:
                mock_cd2 = MagicMock()
                mock_cd2_cls.return_value = mock_cd2
                src_files = [
                    {"name": "TestShow.S01E01.mkv", "path": "/src/E01.mkv", "isDirectory": False, "size": 100},
                    {"name": "TestShow.S01E02.mkv", "path": "/src/E02.mkv", "isDirectory": False, "size": 100},
                ]
                target_files = [
                    {"name": "TestShow.S01E01.mkv", "path": "/target/E01.mkv", "isDirectory": False, "size": 100},
                ]
                async def mock_list(p, force_refresh=False):
                    if "Media_Archive" in p:
                        return target_files
                    return src_files
                mock_cd2.list_files = AsyncMock(side_effect=mock_list)
                mock_cd2.delete_file = AsyncMock()
                mock_cd2.close = AsyncMock()

                resp = asyncio.run(clean_source_redundant(_ArchiveRequest(season_record_ids=["rec_1"]), db_mock))
                self.assertFalse(resp.ok)
                self.assertEqual(fake_rec.status, "already_archived")
                mock_cd2.delete_file.assert_not_called()

    def test_p01_clean_source_redundant_rejects_identical_paths(self):
        """P0-1: 源路径与目标路径相同时，坚决拦截不删归档！断言零删除！"""
        fake_rec = SeasonRecord(
            id="rec_ident",
            task_id="task_1",
            show_name="TestShow",
            season_number=1,
            season_path="/115/Media_Archive/TestShow/Season 1",
            show_path="/115/Media_Archive/TestShow",
            archive_target_path="/115/Media_Archive/TestShow/Season 1",
            status="already_archived",
        )
        db_mock = MagicMock()
        exec_mock = MagicMock()
        exec_mock.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[fake_rec])))
        db_mock.execute = AsyncMock(return_value=exec_mock)
        db_mock.get = AsyncMock(return_value=None)
        db_mock.commit = AsyncMock()

        with patch("app.api.archive.get_settings") as mock_settings:
            s = MagicMock()
            s.source_root = "/115/Library_incs/nas"
            s.archive_root = "/115/Media_Archive"
            mock_settings.return_value = s

            with patch("app.api.archive.CloudDrive2Client") as mock_cd2_cls:
                mock_cd2 = MagicMock()
                mock_cd2_cls.return_value = mock_cd2
                mock_cd2.delete_file = AsyncMock()
                mock_cd2.close = AsyncMock()

                resp = asyncio.run(clean_source_redundant(_ArchiveRequest(season_record_ids=["rec_ident"]), db_mock))
                self.assertFalse(resp.ok)
                mock_cd2.delete_file.assert_not_called()

    def test_f04_operation_log_contract(self):
        """F04: 验证 OperationLog 模型字段名一致性，杜绝 TypeError。"""
        log = OperationLog(
            task_id="task1",
            season_record_id="rec1",
            action="delete_source_show",
            status="completed",
            detail_json='{"deleted": true}',
        )
        self.assertEqual(log.action, "delete_source_show")
        self.assertEqual(log.status, "completed")
        self.assertEqual(log.detail_json, '{"deleted": true}')

    def test_p01_clean_source_redundant_rejects_unrecognized_video(self):
        """P0-1: 源端含有无法解析集号的正片视频时，证据不足坚决拦截并转入复核！断言零删除！"""
        fake_rec = SeasonRecord(
            id="rec_unrec",
            task_id="task_1",
            show_name="TestShow",
            season_number=1,
            season_path="/115/Library_incs/nas/TestShow/Season 1",
            show_path="/115/Library_incs/nas/TestShow",
            archive_target_path="/115/Media_Archive/TestShow/Season 1",
            status="already_archived",
        )
        db_mock = MagicMock()
        exec_mock = MagicMock()
        exec_mock.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[fake_rec])))
        db_mock.execute = AsyncMock(return_value=exec_mock)
        db_mock.get = AsyncMock(return_value=None)
        db_mock.commit = AsyncMock()

        with patch("app.api.archive.get_settings") as mock_settings:
            s = MagicMock()
            s.source_root = "/115/Library_incs/nas"
            s.archive_root = "/115/Media_Archive"
            mock_settings.return_value = s

            with patch("app.api.archive.CloudDrive2Client") as mock_cd2_cls:
                mock_cd2 = MagicMock()
                mock_cd2_cls.return_value = mock_cd2
                src_files = [
                    {"name": "TestShow.S01E01.mkv", "path": "/src/E01.mkv", "isDirectory": False, "size": 100},
                    {"name": "TestShow.UnknownSpecial.mkv", "path": "/src/UnknownSpecial.mkv", "isDirectory": False, "size": 100},
                ]
                target_files = [
                    {"name": "TestShow.S01E01.mkv", "path": "/target/E01.mkv", "isDirectory": False, "size": 100},
                ]
                async def mock_list(p, force_refresh=False):
                    if "Media_Archive" in p:
                        return target_files
                    return src_files
                mock_cd2.list_files = AsyncMock(side_effect=mock_list)
                mock_cd2.delete_file = AsyncMock()
                mock_cd2.close = AsyncMock()

                resp = asyncio.run(clean_source_redundant(_ArchiveRequest(season_record_ids=["rec_unrec"]), db_mock))
                self.assertFalse(resp.ok)
                self.assertEqual(fake_rec.status, "already_archived")
                self.assertIn("未识别集号的正片视频", resp.message)
                mock_cd2.delete_file.assert_not_called()

if __name__ == "__main__":
    unittest.main()
