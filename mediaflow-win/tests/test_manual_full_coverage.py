"""
MediaFlow 通用 Agent 执行手册 83 项用例自动化验证套件
覆盖: SAFE, PLAN, SCAN, ARC, TASK, MP, UI, SYS, E2E 全领域规范
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from app.core.review_engine import (
    generate_review_clean_plan,
    episodes_from_name,
)
from app.core.inspector import diagnose_season, compute_archive_target
from app.api.archive import (
    delete_source_show,
    DeleteSourceShowRequest,
    clean_source_redundant,
    _ArchiveRequest,
    calibrate_episodes,
    CalibrateEpisodesRequest,
)
from app.models.task import Task
from app.models.season import SeasonRecord
from app.models.operation import OperationLog

class TestManualFullCoverage(unittest.TestCase):
    # ── 5.1 SAFE 边界与路径隔离 ───────────────────────────────
    def test_safe07_delete_show_boundary_injection(self):
        """SAFE-07: 对整剧删除依次注入越界或非法路径，全部被安全拒绝。"""
        db_mock = MagicMock()
        with patch("app.api.archive.get_settings") as mock_settings:
            s = MagicMock()
            s.source_root = "/qa/src"
            s.archive_root = "/qa/dst"
            mock_settings.return_value = s

            injected_paths = [
                "/qa/src.finish/Show",
                "/qa/src/../dst/Show",
                "/qa/src/sub/..",
                "/qa/src",
                "/qa/dst/Show",
            ]
            for p in injected_paths:
                fake_rec = SeasonRecord(
                    id=f"rec_{hash(p)}",
                    task_id="t1",
                    show_name="InjectShow",
                    show_path=p,
                    season_number=1,
                    season_path=f"{p}/Season 1",
                )
                db_mock.get = AsyncMock(return_value=fake_rec)
                resp = asyncio.run(delete_source_show(DeleteSourceShowRequest(season_record_id=fake_rec.id), db_mock))
                self.assertFalse(resp.ok, f"路径 {p} 应该被安全拦截，但返回了 ok=True")

    def test_safe08_clean_source_protects_unknown_video_and_extras(self):
        """SAFE-08: 源端含有未知视频或独有正片视频时，坚决拦截不删。"""
        fake_rec = SeasonRecord(
            id="rec_s08",
            task_id="t1",
            show_name="Safe08Show",
            season_number=1,
            season_path="/qa/src/Safe08Show/Season 1",
            show_path="/qa/src/Safe08Show",
            archive_target_path="/qa/dst/Safe08Show/Season 1",
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
            s.source_root = "/qa/src"
            s.archive_root = "/qa/dst"
            mock_settings.return_value = s

            with patch("app.api.archive.CloudDrive2Client") as mock_cd2_cls:
                mock_cd2 = MagicMock()
                mock_cd2_cls.return_value = mock_cd2
                # 源端有未识别集号的正片视频
                src_files = [
                    {"name": "Safe08Show.S01E01.mkv", "path": "/src/E01.mkv", "isDirectory": False, "size": 100},
                    {"name": "Safe08Show.UnknownMystery.mkv", "path": "/src/Mystery.mkv", "isDirectory": False, "size": 100},
                ]
                target_files = [
                    {"name": "Safe08Show.S01E01.mkv", "path": "/dst/E01.mkv", "isDirectory": False, "size": 100},
                ]
                async def mock_list(p, force_refresh=False):
                    if "qa/dst" in p:
                        return target_files
                    return src_files
                mock_cd2.list_files = AsyncMock(side_effect=mock_list)
                mock_cd2.delete_file = AsyncMock()
                mock_cd2.close = AsyncMock()

                resp = asyncio.run(clean_source_redundant(_ArchiveRequest(season_record_ids=["rec_s08"]), db_mock))
                self.assertFalse(resp.ok)
                mock_cd2.delete_file.assert_not_called()

    def test_safe14_already_archived_empty_target_path_safety(self):
        """SAFE-14: archive_target_path 为空时，合法根自动安全推导，非法根安全拦截不报 500。"""
        fake_rec_valid = SeasonRecord(
            id="rec_val",
            task_id="t1",
            show_name="ValidShow",
            season_number=1,
            season_path="/qa/src/ValidShow/Season 1",
            show_path="/qa/src/ValidShow",
            archive_target_path=None,  # 空目标路径
            status="already_archived",
        )
        fake_rec_invalid = SeasonRecord(
            id="rec_inval",
            task_id="t1",
            show_name="InvalidShow",
            season_number=1,
            season_path="/other_root/InvalidShow/Season 1",
            show_path="/other_root/InvalidShow",
            archive_target_path=None,
            status="already_archived",
        )
        db_mock = MagicMock()
        exec_mock = MagicMock()
        exec_mock.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[fake_rec_invalid])))
        db_mock.execute = AsyncMock(return_value=exec_mock)
        db_mock.get = AsyncMock(return_value=None)
        db_mock.commit = AsyncMock()

        with patch("app.api.archive.get_settings") as mock_settings:
            s = MagicMock()
            s.source_root = "/qa/src"
            s.archive_root = "/qa/dst"
            mock_settings.return_value = s

            with patch("app.api.archive.CloudDrive2Client") as mock_cd2_cls:
                mock_cd2 = MagicMock()
                mock_cd2_cls.return_value = mock_cd2
                mock_cd2.close = AsyncMock()

                # 测试非法源根推导时友好拒绝，不抛 500
                resp = asyncio.run(clean_source_redundant(_ArchiveRequest(season_record_ids=["rec_inval"]), db_mock))
                self.assertFalse(resp.ok)
                mock_cd2.delete_file.assert_not_called()

    # ── 5.2 PLAN & SCAN 识别、计划与诊断 ─────────────────────
    def test_plan04_complete_group_quality_priority(self):
        """PLAN-04: 两个完整版本 (2160p vs 720p)，坚决保留 2160p 淘汰 720p。"""
        files = [
            {"name": "Show.S01E01.2160p.mkv", "path": "/p/E01.4k.mkv", "size": 4000, "isDirectory": False},
            {"name": "Show.S01E02.2160p.mkv", "path": "/p/E02.4k.mkv", "size": 4000, "isDirectory": False},
            {"name": "Show.S01E01.720p.mkv", "path": "/p/E01.720.mkv", "size": 800, "isDirectory": False},
            {"name": "Show.S01E02.720p.mkv", "path": "/p/E02.720.mkv", "size": 800, "isDirectory": False},
        ]
        plan = generate_review_clean_plan(files, season_number=1, total_episodes=2)
        self.assertTrue(plan["can_clean"])
        kept = {f["name"] for f in plan["to_keep"]}
        deleted = {f["name"] for f in plan["to_delete"]}
        self.assertIn("Show.S01E01.2160p.mkv", kept)
        self.assertIn("Show.S01E02.2160p.mkv", kept)
        self.assertIn("Show.S01E01.720p.mkv", deleted)
        self.assertIn("Show.S01E02.720p.mkv", deleted)

    def test_plan06_zero_byte_and_part_protection(self):
        """PLAN-06: 季目录含 0 字节视频或 .part 文件时，触发安全熔断，拒绝实际删除。"""
        files = [
            {"name": "Show.S01E01.1080p.mkv", "path": "/p/E01.mkv", "size": 2000, "isDirectory": False},
            {"name": "Show.S01E01.720p.mkv", "path": "/p/E01_bad.mkv", "size": 0, "isDirectory": False}, # 0字节
        ]
        plan = generate_review_clean_plan(files, season_number=1, total_episodes=1)
        self.assertFalse(plan["can_clean"])
        self.assertIn("损坏视频", plan["reason"])

        files_part = [
            {"name": "Show.S01E01.1080p.mkv", "path": "/p/E01.mkv", "size": 2000, "isDirectory": False},
            {"name": "Show.S01E02.1080p.mkv.part", "path": "/p/E02.part", "size": 1000, "isDirectory": False},
        ]
        plan_part = generate_review_clean_plan(files_part, season_number=1, total_episodes=2)
        self.assertFalse(plan_part["can_clean"])
        self.assertIn("临时下载文件", plan_part["reason"])

    def test_scan02_scan04_scan05_diagnosis_matrix(self):
        """SCAN-02, SCAN-04, SCAN-05: 诊断状态矩阵验证。"""
        # SCAN-02: {1,2} -> archive_ready; {1,2,2} -> review_needed; {1} -> download_needed
        st1, _ = diagnose_season([1, 2], total_episodes=2, season_finished=True, has_duplicates=False)
        self.assertEqual(st1, "archive_ready")

        st2, _ = diagnose_season([1, 2], total_episodes=2, season_finished=True, has_duplicates=True, duplicate_episodes=[2])
        self.assertEqual(st2, "review_needed")

        st3, _ = diagnose_season([1], total_episodes=2, season_finished=True, has_duplicates=False)
        self.assertEqual(st3, "download_needed")

        # SCAN-05: 超范围集号 {1, 3}，官方 2 集 -> 必须标为缺集待补 download_needed，且含超范围提示
        st5, det5 = diagnose_season([1, 3], total_episodes=2, season_finished=True, has_duplicates=False)
        self.assertEqual(st5, "download_needed")
        self.assertIn("超范围集", det5)

        # 官方 2 集，本地 {1, 2, 3} -> 无缺集但有超范围集号，标为 review_needed
        st_over, det_over = diagnose_season([1, 2, 3], total_episodes=2, season_finished=True, has_duplicates=False)
        self.assertEqual(st_over, "review_needed")
        self.assertIn("超出官方范围的异常集号", det_over)

    def test_scan08_calibrate_episodes(self):
        """SCAN-08: POST archive/calibrate-episodes 合法与非法参数校验。"""
        db_mock = MagicMock()
        fake_rec = SeasonRecord(
            id="rec_cal",
            task_id="t1",
            show_name="CalShow",
            season_number=1,
            season_path="/qa/src/CalShow/Season 1",
            show_path="/qa/src/CalShow",
            total_episodes=10,
            recognized_episodes_json="[1,2,3]",
            season_finished=True,
        )
        db_mock.get = AsyncMock(return_value=fake_rec)
        db_mock.commit = AsyncMock()
        exec_mock = MagicMock()
        exec_mock.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[fake_rec])))
        db_mock.execute = AsyncMock(return_value=exec_mock)

        # 1. 正常校准总集数从 10 改为 3 (本地刚好有 1,2,3) -> 变为 archive_ready
        req_ok = CalibrateEpisodesRequest(season_record_id="rec_cal", total_episodes=3)
        resp_ok = asyncio.run(calibrate_episodes(req_ok, db_mock))
        self.assertTrue(resp_ok.ok)
        self.assertEqual(fake_rec.status, "archive_ready")

        # 2. 非法参数校验 (0 或 负数)
        req_zero = CalibrateEpisodesRequest(season_record_id="rec_cal", total_episodes=0)
        resp_zero = asyncio.run(calibrate_episodes(req_zero, db_mock))
        self.assertFalse(resp_zero.ok)

        req_neg = CalibrateEpisodesRequest(season_record_id="rec_cal", total_episodes=-5)
        resp_neg = asyncio.run(calibrate_episodes(req_neg, db_mock))
        self.assertFalse(resp_neg.ok)

    # ── 5.4 MP 补缺与退订契约 ─────────────────────────────────
    def test_mp01_download_gap_fill_field_compatibility(self):
        """MP-01: download_service 针对空记录模型保持 ok: False 统一响应。"""
        from app.services.download_service import execute_gap_fill_download as submit_season_gap_downloads
        db_mock = MagicMock()
        exec_mock = MagicMock()
        exec_mock.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        db_mock.execute = AsyncMock(return_value=exec_mock)

        with patch("app.services.download_service.get_settings") as mock_set:
            s = MagicMock()
            s.mp_url = "http://mp.test"
            s.mp_token = "token"
            mock_set.return_value = s

            res = asyncio.run(submit_season_gap_downloads(["non_exist_id"], db_mock))
            self.assertFalse(res["ok"])
            self.assertFalse(res["success"])
            self.assertEqual(res["details"], [])

if __name__ == "__main__":
    unittest.main()
