"""MediaFlow v2.5.45 五模块处理流程重构与智能清理方案 — G01–G28 验收测试套件"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.archive import (
    DeleteSourceSeasonPreviewRequest,
    DeleteSourceSeasonRequest,
    RecommendedExecuteRequest,
    RecommendedPlanRequest,
    build_recommended_batch_plan,
    delete_source_season,
    execute_recommended_batch_plan,
    preview_delete_source_season,
)
from app.core.directory_merge import (
    build_canonical_show_dir_name,
    build_directory_merge_plan,
    detect_explicit_file_tmdb_id,
    execute_directory_merge,
    normalize_dir_path,
)
from app.core.episode_parser import extract_douban_subject_id
from app.core.inspector import _merge_season_sources
from app.core.manual_confirmation import (
    CATEGORY_LABELS,
    build_manual_correction_plan,
    execute_manual_correction_plan,
    update_nfo_identity_structured,
)
from app.core.review_engine import (
    build_file_fingerprints,
    compare_plan_fingerprints,
    generate_review_clean_plan,
)
from app.core.season_classifier import (
    CATEGORY_PRIORITY,
    MODULE_DISPLAY_ORDER,
    _predict_post_review_category,
    classify_single_record,
    consolidate_seasons_to_work_records,
    detect_category_from_path,
    resolve_record_gap_category,
)
from app.core.segments import diagnose_periods, diagnose_periods_structured
from app.database import Base
from app.models.directory_group import DirectoryGroup
from app.models.season import SeasonRecord
from app.models.task import Task


class TestV2545FiveModuleRefactor(unittest.IsolatedAsyncioTestCase):
    """覆盖方案文档 G01–G28 全部验收标准的自动化测试"""

    # ------------------------------------------------------------------
    # G01 / G02 / G21 / G25 / G26: 模块优先级、展示顺序、防回流循环
    # ------------------------------------------------------------------
    def test_g01_g21_priority_and_display_order(self):
        # G21: 展示顺序与模块命名
        self.assertEqual(MODULE_DISPLAY_ORDER, ("review", "anomaly", "download", "airing", "archive"))
        self.assertEqual(CATEGORY_LABELS["download"], "缺集待补")
        self.assertNotIn("真缺集", CATEGORY_LABELS["download"])
        # G01: 优先级 review(1) > anomaly(2) > airing(3) > download(4) > archive(5)
        self.assertLess(CATEGORY_PRIORITY["review"], CATEGORY_PRIORITY["anomaly"])
        self.assertLess(CATEGORY_PRIORITY["anomaly"], CATEGORY_PRIORITY["airing"])
        self.assertLess(CATEGORY_PRIORITY["airing"], CATEGORY_PRIORITY["download"])
        self.assertLess(CATEGORY_PRIORITY["download"], CATEGORY_PRIORITY["archive"])

        # G01: 既缺集又有重复版本 -> 先入待复核清理，清理后去向预判为缺集待补
        rec_missing_and_dup = SimpleNamespace(
            status="download_needed",
            total_episodes=10,
            recognized_episodes=[1, 2, 3, 4],
            missing_episodes=[5, 6, 7, 8, 9, 10],
            duplicate_episodes=[2],
            evidence={"meta_status": "ok"},
            conflict={},
        )
        cat, action = classify_single_record(rec_missing_and_dup)
        self.assertEqual(cat, "review")
        self.assertEqual(action, "clean")
        self.assertEqual(_predict_post_review_category(rec_missing_and_dup, {"meta_status": "ok"}), "download")

    def test_g02_airing_with_stable_duplicates_goes_review_then_airing(self):
        # G02: 连载中 + 稳定同内容重复版本 -> 先入待复核清理，清理后回到连载留源
        rec_airing_dup = SimpleNamespace(
            status="airing",
            total_episodes=12,
            recognized_episodes=[1, 2, 3],
            missing_episodes=[4, 5, 6],
            duplicate_episodes=[2],
            evidence={"meta_status": "ok", "broadcast_evidence": {"status": "airing"}},
            conflict={},
        )
        cat, action = classify_single_record(rec_airing_dup)
        self.assertEqual(cat, "review")
        self.assertEqual(action, "clean")
        self.assertEqual(
            _predict_post_review_category(rec_airing_dup, {"meta_status": "ok", "broadcast_evidence": {"status": "airing"}}),
            "airing",
        )

        # 清理时活跃下载文件仅延迟该组，不影响其他稳定重复组
        files = [
            {"name": "Show.S01E01.1080p.H264.mkv", "path": "/src/Show/Season 1/Show.S01E01.1080p.H264.mkv", "size": 1_000_000_000},
            {"name": "Show.S01E01.2160p.HEVC.mkv", "path": "/src/Show/Season 1/Show.S01E01.2160p.HEVC.mkv", "size": 3_000_000_000},
            {"name": "Show.S01E02.1080p.H264.mkv", "path": "/src/Show/Season 1/Show.S01E02.1080p.H264.mkv", "size": 1_000_000_000},
            {"name": "Show.S01E02.2160p.HEVC.mkv", "path": "/src/Show/Season 1/Show.S01E02.2160p.HEVC.mkv", "size": 2_500_000_000},
        ]
        plan = generate_review_clean_plan(
            files=files,
            season_number=1,
            total_episodes=12,
            active_download_paths={"/src/Show/Season 1/Show.S01E02.1080p.H264.mkv"},
        )
        self.assertTrue(plan["can_clean"])
        deleted_names = {f["name"] for f in plan["to_delete"]}
        self.assertEqual(deleted_names, {"Show.S01E01.1080p.H264.mkv"})
        self.assertTrue(any("E02" in d["name"] for d in plan["deferred_files"]))

    def test_g26_distinct_retained_prevents_review_loop(self):
        # G26: 重复全为合理差异版本（如正片 + 加长版 / 纯享 / Vlog）-> 零删留存，直接放行下游不再回流
        files = [
            {"name": "你好时光.S01E01.1080p.mp4", "size": 2_000_000_000},
            {"name": "你好时光.S01E01.加长版.1080p.mp4", "size": 2_500_000_000},
        ]
        plan = generate_review_clean_plan(files=files, season_number=1, total_episodes=10)
        self.assertTrue(plan["all_distinct_retained"])
        self.assertEqual(len(plan["to_delete"]), 0)

        rec = SimpleNamespace(
            status="download_needed",
            total_episodes=10,
            recognized_episodes=[1],
            missing_episodes=[2, 3, 4, 5, 6, 7, 8, 9, 10],
            duplicate_episodes=[1],
            evidence={
                "meta_status": "ok",
                "review_resolution": {
                    "all_distinct_retained": True,
                    "rules_version": "v2.5.45",
                    "distinct_retention_note": plan["distinct_retention_note"],
                },
            },
            conflict={},
        )
        cat, _ = classify_single_record(rec)
        self.assertEqual(cat, "download")

    # ------------------------------------------------------------------
    # G03 / G04 / G05: 统一内容等价与版本识别模型
    # ------------------------------------------------------------------
    def test_g03_chinese_restaurant_codec_dedup_and_vlog_extra_preserved(self):
        # G03: 中餐厅 S10E01 同时有 1080p H.264 / H.265（同内容清理低版），且同集有 Vlog、加更（独立保留）
        files = [
            {"name": "中餐厅 - S10E01 - 第1期.1080p.H264.mp4", "size": 2_100_000_000},
            {"name": "中餐厅 - S10E01 - 第1期.1080p.HEVC.H265.mp4", "size": 2_100_000_000},
            {"name": "中餐厅 - S10E01 - 合伙人Vlog.1080p.mp4", "size": 450_000_000},
            {"name": "中餐厅 - S10E01 - 加更版.1080p.mp4", "size": 900_000_000},
        ]
        plan = generate_review_clean_plan(files=files, season_number=10, total_episodes=12)
        self.assertTrue(plan["can_clean"])
        deleted_names = {f["name"] for f in plan["to_delete"]}
        kept_names = {f["name"] for f in plan["to_keep"]}
        self.assertEqual(deleted_names, {"中餐厅 - S10E01 - 第1期.1080p.H264.mp4"})
        self.assertIn("中餐厅 - S10E01 - 第1期.1080p.HEVC.H265.mp4", kept_names)
        self.assertIn("中餐厅 - S10E01 - 合伙人Vlog.1080p.mp4", kept_names)
        self.assertIn("中餐厅 - S10E01 - 加更版.1080p.mp4", kept_names)
        self.assertTrue(plan["to_delete"][0].get("replaced_by"))
        self.assertTrue(plan["to_delete"][0].get("equivalence_basis"))

    def test_g04_hello_time_standard_vs_extended_independent_dedup(self):
        # G04: 你好时光 S01 同时有正片（标准版）1080p/2160p 与加长版 1080p/2160p -> 各自独立择优保留
        files = [
            {"name": "你好时光 - S01E01 - 第1期.1080p.mp4", "size": 1_800_000_000},
            {"name": "你好时光 - S01E01 - 第1期.2160p.4K.mp4", "size": 4_200_000_000},
            {"name": "你好时光 - S01E01 - 第1期 加长版.1080p.mp4", "size": 2_400_000_000},
            {"name": "你好时光 - S01E01 - 第1期 加长版.2160p.4K.mp4", "size": 5_500_000_000},
        ]
        plan = generate_review_clean_plan(files=files, season_number=1, total_episodes=10)
        self.assertTrue(plan["can_clean"])
        kept = {f["name"] for f in plan["to_keep"]}
        deleted = {f["name"] for f in plan["to_delete"]}
        self.assertEqual(
            kept,
            {
                "你好时光 - S01E01 - 第1期.2160p.4K.mp4",
                "你好时光 - S01E01 - 第1期 加长版.2160p.4K.mp4",
            },
        )
        self.assertEqual(
            deleted,
            {
                "你好时光 - S01E01 - 第1期.1080p.mp4",
                "你好时光 - S01E01 - 第1期 加长版.1080p.mp4",
            },
        )

    def test_g05_segments_and_whole_non_elimination_non_blocking(self):
        # G05: 第 1 期有 上/下 与完整版，互不淘汰，且不阻断第 2 期确定重复的清理
        files = [
            {"name": "综艺 - S01E01 - 第1期上.1080p.mp4", "size": 1_200_000_000},
            {"name": "综艺 - S01E01 - 第1期下.1080p.mp4", "size": 1_300_000_000},
            {"name": "综艺 - S01E01 - 第1期完整版.1080p.mp4", "size": 2_500_000_000},
            {"name": "综艺 - S01E02 - 第2期.720p.mp4", "size": 900_000_000},
            {"name": "综艺 - S01E02 - 第2期.2160p.mp4", "size": 3_800_000_000},
        ]
        plan = generate_review_clean_plan(files=files, season_number=1, total_episodes=10)
        self.assertTrue(plan["can_clean"])
        kept = {f["name"] for f in plan["to_keep"]}
        deleted = {f["name"] for f in plan["to_delete"]}
        self.assertIn("综艺 - S01E01 - 第1期上.1080p.mp4", kept)
        self.assertIn("综艺 - S01E01 - 第1期下.1080p.mp4", kept)
        self.assertIn("综艺 - S01E01 - 第1期完整版.1080p.mp4", kept)
        self.assertEqual(deleted, {"综艺 - S01E02 - 第2期.720p.mp4"})

    # ------------------------------------------------------------------
    # G06 / G07 / G08 / G09: 同 TMDB ID 规范目录名、跨作品隔离、多季合并、碰撞处理
    # ------------------------------------------------------------------
    async def test_g06_g07_g08_g09_canonical_merge_multi_season_isolation_and_collision(self):
        self.assertEqual(
            build_canonical_show_dir_name(canonical_title="乘风", first_air_year="2020-06-12", tmdb_id="104716")[0],
            "乘风 (2020) [tmdbid=104716]",
        )
        self.assertEqual(detect_explicit_file_tmdb_id("别的作品 [tmdbid=999999] S01E01.mkv"), "999999")

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dir_a = root / "乘风破浪的姐姐"
            dir_b = root / "乘风2023"
            (dir_a / "Season 1").mkdir(parents=True)
            (dir_a / "Season 2").mkdir(parents=True)
            (dir_b / "Season 1").mkdir(parents=True)
            (dir_b / "Season 2").mkdir(parents=True)

            # 根目录海报/NFO 资产
            (dir_a / "poster.jpg").write_bytes(b"poster-data")
            (dir_a / "tvshow.nfo").write_text("<tvshow><tmdbid>104716</tmdbid></tvshow>", encoding="utf-8")

            # S01: dir_a 有 1080p，dir_b 有 2160p + 加长版（差异保留，目录归并不丢任何有效文件）
            (dir_a / "Season 1" / "乘风 - S01E01.1080p.mp4").write_bytes(b"a" * 1000)
            (dir_b / "Season 1" / "乘风 - S01E01.2160p.mp4").write_bytes(b"b" * 3000)
            (dir_b / "Season 1" / "乘风 - S01E01 - 加长版.1080p.mp4").write_bytes(b"c" * 1500)

            # S02: dir_a 有正常 S02E01 + 跨作品显式 tmdbid=999999 冲突文件（G07: 仅隔离冲突文件，不阻断干净季）
            (dir_a / "Season 2" / "乘风 - S02E01.1080p.mp4").write_bytes(b"d" * 1200)
            (dir_b / "Season 2" / "OtherShow [tmdbid=999999] S02E02.1080p.mp4").write_bytes(b"x" * 1000)
            # 英文/拼音释放名不算跨作品冲突 (G07)
            (dir_b / "Season 2" / "Cheng.Feng.S02E03.1080p.mp4").write_bytes(b"e" * 1100)

            norm_a = normalize_dir_path(str(dir_a))
            norm_b = normalize_dir_path(str(dir_b))

            async with SessionLocal() as db:
                task = Task(id="task-dg", name="目录归并测试", source_path=normalize_dir_path(str(root)), status="completed")
                grp = DirectoryGroup(
                    id="grp-104716",
                    task_id=task.id,
                    tmdb_id="104716",
                    category_key="cat:综艺",
                    title="乘风",
                    paths_json=json.dumps(
                        [
                            {"side": "source", "norm_path": norm_a, "seasons": [1, 2]},
                            {"side": "source", "norm_path": norm_b, "seasons": [1, 2]},
                        ],
                        ensure_ascii=False,
                    ),
                    status="pending",
                )
                db.add_all([task, grp])
                await db.commit()

                plan = await build_directory_merge_plan(
                    db=db,
                    task_id=task.id,
                    group_id=grp.id,
                    canonical_title="乘风",
                    first_air_year="2020",
                    auto_canonical_target=True,
                    all_seasons=True,
                    smart_collision=True,
                    cd2_client=None,
                )
                # G06: 两个现有目录都不规范 -> 自动创建受控规范目录
                self.assertTrue(plan["source_target_will_create"])
                self.assertTrue(plan["source_target_show_path"].endswith("乘风 (2020) [tmdbid=104716]"))
                # G08: 单组多季一次性纳入计划
                self.assertEqual(sorted(plan["affected_seasons"]), [1, 2])
                # G07: 仅隔离显式跨作品文件，不阻断其他干净文件，英文名 Cheng.Feng 正常归并
                self.assertEqual(len(plan["isolated_conflicts"]), 1)
                self.assertIn("999999", plan["isolated_conflicts"][0]["filename"])
                self.assertTrue(plan["can_execute"])
                # G09: 根目录资产保留计划
                self.assertTrue(len(plan["assets_to_preserve"]) >= 1)

                exec_res = await execute_directory_merge(
                    db=db,
                    task_id=task.id,
                    group_id=grp.id,
                    plan_id=plan["plan_id"],
                    cd2_client=None,
                )
                self.assertTrue(exec_res["ok"])
                canon_dir = Path(plan["source_target_show_path"])
                self.assertTrue((canon_dir / "Season 1").exists())
                self.assertTrue((canon_dir / "Season 2").exists())
                s1_files = {p.name for p in (canon_dir / "Season 1").iterdir() if p.is_file()}
                s2_files = {p.name for p in (canon_dir / "Season 2").iterdir() if p.is_file()}
                # S01 2160p 与加长版均已安全迁入规范目录且零覆盖，再用统一清理引擎验证仅淘汰低版 1080p
                self.assertTrue(any("2160p" in n for n in s1_files))
                self.assertTrue(any("加长版" in n for n in s1_files))
                s1_plan = generate_review_clean_plan(
                    files=[{"name": p.name, "path": str(p), "size": p.stat().st_size} for p in (canon_dir / "Season 1").iterdir() if p.is_file()],
                    season_number=1,
                    total_episodes=10,
                )
                self.assertTrue(s1_plan["can_clean"])
                self.assertEqual({d["name"] for d in s1_plan["to_delete"]}, {"乘风 - S01E01.1080p.mp4"})
                self.assertTrue(any("加长版" in k["name"] for k in s1_plan["to_keep"]))
                # S02 正常文件 + 英文名都在，跨作品 999999 留在原位隔离未被搬入
                self.assertTrue(any("S02E01" in n for n in s2_files))
                self.assertTrue(any("S02E03" in n for n in s2_files))
                self.assertFalse(any("999999" in n for n in s2_files))
                self.assertTrue((dir_b / "Season 2" / "OtherShow [tmdbid=999999] S02E02.1080p.mp4").exists())
                # 根目录资产已保底迁入规范目录
                self.assertTrue((canon_dir / "poster.jpg").exists())

        await engine.dispose()

    # ------------------------------------------------------------------
    # G11 / G12 / G13 / G14 / G15 / G16: 豆瓣+TMDB 多源融合与精准分流
    # ------------------------------------------------------------------
    def test_g11_g12_douban_tmdb_fusion_and_e42_guard(self):
        # G11 & G12: 豆瓣常规期数已完结 (10期)，TMDB 多出一条未播/加更条目 E42 不得推翻常规完结
        primary = {
            "total": 11,
            "finished": False,
            "broadcast_evidence": {
                "status": "evidence_pending",
                "aired_episodes": list(range(1, 11)),
                "upcoming_episodes": [],
                "today_scheduled_episodes": [],
                "undated_episodes": [42],
            },
        }
        secondary = {
            "total": 10,
            "finished": True,
            "bound": True,
            "douban_id": "37937537",
            "title": "喜剧之王单口季 第三季",
        }
        merged = _merge_season_sources(primary, secondary)
        self.assertEqual(merged["douban_total"], 10)
        self.assertTrue(merged["finished"])
        self.assertEqual(merged["broadcast_evidence"]["status"], "finished")
        self.assertEqual(merged["broadcast_evidence"]["undated_extra_ignored"], [42])
        self.assertIn("source_fusion", merged)

        # 验证豆瓣 URL ID 提取器
        self.assertEqual(extract_douban_subject_id("https://movie.douban.com/subject/36533816/"), "36533816")
        self.assertEqual(extract_douban_subject_id("36823911"), "36823911")

    def test_g13_g14_g15_g16_period_diagnosis_and_attachments_only(self):
        # G13: 喜剧之王单口季 S01 整期 1-10 全齐 + whole_period_verified -> period_ready
        whole_files = [{"name": f"喜剧之王单口季.S01E{i:02d}.mp4", "size": 2_000_000_000} for i in range(1, 11)]
        split_mapping = {i: [1, 2] for i in range(1, 11)}
        diag_g13 = diagnose_periods_structured(
            files=whole_files,
            season=1,
            mapping=split_mapping,
            ordinals={},
            total=10,
            finished=True,
            archive_present=False,
            douban_total=10,
            whole_period_verified=True,
        )
        self.assertEqual(diag_g13["result"][0], "period_ready")
        self.assertNotIn("系统跟踪", diag_g13["detail"])

        # G14: 非正式会谈 S08 播期不明（air_status=unknown）不得归类为 airing
        rec_g14 = SimpleNamespace(
            status="unknown",
            total_episodes=10,
            recognized_episodes=[1, 2, 3, 4, 5],
            missing_episodes=[],
            duplicate_episodes=[],
            evidence={"meta_status": "metadata_unverified", "broadcast_evidence": {"status": "unknown"}},
            conflict={},
        )
        cat_g14, _ = classify_single_record(rec_g14)
        self.assertEqual(cat_g14, "anomaly")
        self.assertNotEqual(cat_g14, "airing")

        # G15: 非正式会谈 S09 仅附件无视频 -> attachments_only -> download_needed (缺失全部正片期)
        att_files = [
            {"name": "tvshow.nfo", "isDirectory": False},
            {"name": "season09-poster.jpg", "isDirectory": False},
        ]
        diag_g15 = diagnose_periods_structured(
            files=att_files,
            season=9,
            mapping={i: [1, 2] for i in range(1, 13)},
            ordinals={},
            total=12,
            finished=True,
            archive_present=False,
            douban_total=12,
        )
        self.assertEqual(diag_g15["outcome_kind"], "attachments_only")
        self.assertEqual(diag_g15["result"][0], "period_missing")
        self.assertEqual(diag_g15["result"][2], list(range(1, 13)))

        # G16: 子目录 / 无法解析命名视频 / .strm 不得误判为“源端 0 视频 -> 缺集待补”
        strm_files = [{"name": "非正式会谈.S09E01.strm", "isDirectory": False}]
        diag_strm = diagnose_periods_structured(
            files=strm_files, season=9, mapping={i: [1, 2] for i in range(1, 13)}, ordinals={}, total=12, finished=True, archive_present=False
        )
        self.assertEqual(diag_strm["outcome_kind"], "unrecognized_content")
        self.assertIsNone(diag_strm["result"])
        self.assertIn("STRM", diag_strm["detail"])

        subdir_files = [{"name": "花絮合集", "isDirectory": True}]
        diag_sub = diagnose_periods_structured(
            files=subdir_files, season=9, mapping={i: [1, 2] for i in range(1, 13)}, ordinals={}, total=12, finished=True, archive_present=False
        )
        self.assertEqual(diag_sub["outcome_kind"], "unrecognized_content")
        self.assertIsNone(diag_sub["result"])
        self.assertIn("子目录", diag_sub["detail"])

        unparsed_video = [{"name": "神秘未命名视频文件.mp4", "isDirectory": False, "size": 1_000_000_000}]
        diag_unparsed = diagnose_periods_structured(
            files=unparsed_video, season=9, mapping={i: [1, 2] for i in range(1, 13)}, ordinals={}, total=12, finished=True, archive_present=False
        )
        self.assertEqual(diag_unparsed["outcome_kind"], "unrecognized_content")
        self.assertIsNone(diag_unparsed["result"])
        self.assertIn("未识别", diag_unparsed["detail"])

        # G25: 彻底清除“系统跟踪中无需操作”
        st, detail, _, _ = diagnose_periods(
            whole_files, 1, split_mapping, {}, 10, True, False, douban_total=10, whole_period_verified=True
        )
        self.assertEqual(st, "period_ready")
        self.assertNotIn("系统跟踪", detail)

    # ------------------------------------------------------------------
    # G17 / G18 / G19: 人工确认物理修正（身份归属 / 季号修复 / 仅资料确认）
    # ------------------------------------------------------------------
    async def test_g17_g18_g19_manual_confirmation_physical_and_metadata_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # G17: 修正目标作品 -> 创建/迁移至新作品目录 + 更新 NFO
            wrong_show = root / "错误剧名 (2024) [tmdbid=111]"
            (wrong_show / "Season 1").mkdir(parents=True)
            (wrong_show / "Season 1" / "错误剧名 - S01E01.1080p.mkv").write_bytes(b"video1")
            (wrong_show / "Season 1" / "错误剧名 - S01E01.zh.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nhi", encoding="utf-8"
            )
            (wrong_show / "tvshow.nfo").write_text(
                "<tvshow><title>错误剧名</title><tmdbid>111</tmdbid></tvshow>", encoding="utf-8"
            )

            s1_files = [
                {"name": "错误剧名 - S01E01.1080p.mkv", "is_dir": False},
                {"name": "错误剧名 - S01E01.zh.srt", "is_dir": False},
            ]
            plan_g17 = build_manual_correction_plan(
                show_name="错误剧名",
                show_path=str(wrong_show),
                season_path=str(wrong_show / "Season 1"),
                current_tmdb_id="111",
                current_season=1,
                target_tmdb_id=222,
                target_title="正确剧名",
                target_first_air_year="2025",
                target_season=1,
                season_files=s1_files,
            )
            self.assertTrue(plan_g17["has_file_changes"])
            exec_g17 = await execute_manual_correction_plan(plan_g17, cd2_client=None)
            self.assertTrue(exec_g17["ok"])
            new_show = root / "正确剧名 (2025) [tmdbid=222]"
            self.assertTrue((new_show / "Season 1").exists())
            moved_files = {p.name for p in (new_show / "Season 1").iterdir()}
            self.assertIn("正确剧名 - S01E01.1080p.mkv", moved_files)
            self.assertIn("正确剧名 - S01E01.zh.srt", moved_files)
            # NFO 结构化更新
            nfo_file = new_show / "tvshow.nfo"
            nfo_file.write_text("<tvshow><title>错误剧名</title><tmdbid>111</tmdbid></tvshow>", encoding="utf-8")
            updated = update_nfo_identity_structured(str(nfo_file), tmdb_id="222", title="正确剧名")
            self.assertTrue(updated)
            self.assertIn("222", nfo_file.read_text(encoding="utf-8"))

            # G18: 仅修正季号 (Season 1 -> Season 2)，保留同剧 Season 3 不受影响
            show_g18 = root / "多季剧 (2023) [tmdbid=333]"
            (show_g18 / "Season 1").mkdir(parents=True)
            (show_g18 / "Season 3").mkdir(parents=True)
            (show_g18 / "Season 1" / "多季剧 - S01E01.mkv").write_bytes(b"s2e1")
            (show_g18 / "Season 3" / "多季剧 - S03E01.mkv").write_bytes(b"s3e1")
            plan_g18 = build_manual_correction_plan(
                show_name="多季剧",
                show_path=str(show_g18),
                season_path=str(show_g18 / "Season 1"),
                current_tmdb_id="333",
                current_season=1,
                target_tmdb_id=333,
                target_title="多季剧",
                target_season=2,
                season_files=[{"name": "多季剧 - S01E01.mkv", "is_dir": False}],
                sibling_seasons=["Season 3"],
            )
            self.assertTrue(plan_g18["has_file_changes"])
            exec_g18 = await execute_manual_correction_plan(plan_g18, cd2_client=None)
            self.assertTrue(exec_g18["ok"])
            self.assertTrue((show_g18 / "Season 2" / "多季剧 - S02E01.mkv").exists())
            self.assertTrue((show_g18 / "Season 3" / "多季剧 - S03E01.mkv").exists())

            # G19: 仅确认总集数 / 播毕状态 / 豆瓣条目，不改文件与目录（零文件改动）
            plan_g19 = build_manual_correction_plan(
                show_name="多季剧",
                show_path=str(show_g18),
                season_path=str(show_g18 / "Season 2"),
                current_tmdb_id="333",
                current_season=2,
                target_tmdb_id=None,
                target_title="",
                target_season=2,
                season_files=[{"name": "多季剧 - S02E01.mkv", "is_dir": False}],
            )
            self.assertFalse(plan_g19["has_file_changes"])

    # ------------------------------------------------------------------
    # G20: 动漫 / 非动漫路径自动识别（绝不返回 pending）
    # ------------------------------------------------------------------
    async def test_g20_auto_anime_non_anime_resolution(self):
        # 跳过宽泛目录（电视剧/媒体库/nas），命中“国漫/日漫/动漫” -> anime
        cat, reason = detect_category_from_path("/115/Library_incs/nas/电视剧/国漫/凡人修仙传/Season 1")
        self.assertEqual(cat, "anime")
        self.assertIn("国漫", reason)

        # 命中“综艺/国产剧/美剧” -> non_anime
        cat2, reason2 = detect_category_from_path("/115/Library_incs/nas/电视剧/综艺/中餐厅/Season 10")
        self.assertEqual(cat2, "non_anime")
        self.assertIn("综艺", reason2)

        # 仅宽泛目录时按元数据兜底，绝不返回 pending
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with SessionLocal() as db:
            rec_anime_meta = SimpleNamespace(
                id="r-anime",
                tmdb_id="88801",
                show_name="某番剧",
                show_path="/nas/电视剧/某番剧",
                season_path="/nas/电视剧/某番剧/Season 1",
                evidence={"matched_work": {"genre_ids": [16], "origin_country": ["JP"]}},
            )
            res_a = await resolve_record_gap_category(db, rec_anime_meta)
            self.assertEqual(res_a["category"], "anime")
            self.assertNotEqual(res_a["category"], "pending")

            rec_default = SimpleNamespace(
                id="r-def",
                tmdb_id="88802",
                show_name="普通剧",
                show_path="/nas/电视剧/普通剧",
                season_path="/nas/电视剧/普通剧/Season 1",
                evidence={},
            )
            res_d = await resolve_record_gap_category(db, rec_default)
            self.assertEqual(res_d["category"], "non_anime")
            self.assertNotEqual(res_d["category"], "pending")
        await engine.dispose()

    # ------------------------------------------------------------------
    # G10 / G22 / G23 / G24 / G27 / G28: 归档已齐源端清理、G22禁删待补源端、
    # 指纹漂移保护、推荐批量执行与四类去向统计
    # ------------------------------------------------------------------
    async def test_g10_g22_g24_g28_api_guards_source_cleanup_and_recommended_batch(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_root = root / "src"
            dst_root = root / "dst"

            # 1) G22 记录：download_needed（绝不允许 delete-source-season）
            dl_show = src_root / "缺集剧 (2025) [tmdbid=501]"
            (dl_show / "Season 1").mkdir(parents=True)
            (dl_show / "Season 1" / "缺集剧 - S01E01.mp4").write_bytes(b"ep1")

            # 2) G10 记录：already_archived，源端除已入库 E01 外还有独有加更版 -> 必须先保护归档再清源
            arc_src_show = src_root / "已归档剧 (2025) [tmdbid=502]"
            arc_dst_show = dst_root / "已归档剧 (2025) [tmdbid=502]"
            (arc_src_show / "Season 1").mkdir(parents=True)
            (arc_dst_show / "Season 1").mkdir(parents=True)
            (arc_src_show / "Season 1" / "已归档剧 - S01E01.1080p.mp4").write_bytes(b"same-ep1")
            (arc_src_show / "Season 1" / "已归档剧 - S01E01 - 加更.1080p.mp4").write_bytes(b"unique-extra")
            (arc_dst_show / "Season 1" / "已归档剧 - S01E01.2160p.mp4").write_bytes(b"dst-ep1")

            # 3) G28 重复版本记录：清理后转入缺集待补
            dup_show = src_root / "混合剧 (2025) [tmdbid=503]"
            (dup_show / "Season 1").mkdir(parents=True)
            (dup_show / "Season 1" / "混合剧 - S01E01.720p.mp4").write_bytes(b"low" * 100)
            (dup_show / "Season 1" / "混合剧 - S01E01.2160p.mp4").write_bytes(b"high" * 400)

            async with SessionLocal() as db:
                task = Task(
                    id="task-v2545",
                    name="测试任务",
                    source_path=src_root.as_posix(),
                    status="completed",
                )
                db.add(task)
                rec_dl = SeasonRecord(
                    id="rec-dl",
                    task_id=task.id,
                    show_name="缺集剧",
                    show_path=dl_show.as_posix(),
                    season_number=1,
                    season_path=(dl_show / "Season 1").as_posix(),
                    tmdb_id="501",
                    status="download_needed",
                    total_episodes=5,
                    local_episode_count=1,
                    recognized_episodes_json=json.dumps([1]),
                    missing_episodes_json=json.dumps([2, 3, 4, 5]),
                    duplicate_episodes_json=json.dumps([]),
                    season_finished=True,
                    evidence_json=json.dumps({"meta_status": "ok"}),
                )
                rec_arc = SeasonRecord(
                    id="rec-arc",
                    task_id=task.id,
                    show_name="已归档剧",
                    show_path=arc_src_show.as_posix(),
                    season_number=1,
                    season_path=(arc_src_show / "Season 1").as_posix(),
                    archive_target_path=(arc_dst_show / "Season 1").as_posix(),
                    tmdb_id="502",
                    status="already_archived",
                    total_episodes=1,
                    local_episode_count=2,
                    recognized_episodes_json=json.dumps([1]),
                    missing_episodes_json=json.dumps([]),
                    duplicate_episodes_json=json.dumps([]),
                    season_finished=True,
                    evidence_json=json.dumps({"meta_status": "ok"}),
                )
                rec_dup = SeasonRecord(
                    id="rec-dup",
                    task_id=task.id,
                    show_name="混合剧",
                    show_path=dup_show.as_posix(),
                    season_number=1,
                    season_path=(dup_show / "Season 1").as_posix(),
                    tmdb_id="503",
                    status="download_needed",
                    total_episodes=4,
                    local_episode_count=2,
                    recognized_episodes_json=json.dumps([1]),
                    missing_episodes_json=json.dumps([2, 3, 4]),
                    duplicate_episodes_json=json.dumps([1]),
                    season_finished=True,
                    evidence_json=json.dumps({"meta_status": "ok"}),
                )
                db.add_all([rec_dl, rec_arc, rec_dup])
                await db.commit()

                # G22: 严禁对缺集待补执行 delete-source-season preview / execute（必须返回 HTTP 409）
                with self.assertRaises(HTTPException) as ctx_prev:
                    await preview_delete_source_season(DeleteSourceSeasonPreviewRequest(season_record_id="rec-dl"), db=db)
                self.assertEqual(ctx_prev.exception.status_code, 409)

                with self.assertRaises(HTTPException) as ctx_exec:
                    await delete_source_season(DeleteSourceSeasonRequest(season_record_id="rec-dl"), db=db)
                self.assertEqual(ctx_exec.exception.status_code, 409)

                # G24: 文件指纹漂移检测
                fp_before = build_file_fingerprints([{"name": "a.mkv", "path": "/src/a.mkv", "size": 100}])
                drift = compare_plan_fingerprints(
                    fp_before,
                    [{"name": "a.mkv", "path": "/src/a.mkv", "size": 200}],
                    {"/src/a.mkv"},
                )
                self.assertTrue(drift["changed"])
                self.assertTrue(drift["needs_repreview"])

                # G28: 统一批量「执行推荐处理」预览 -> 执行 -> 四类去向 + G10 独有加更保底归档
                import shutil

                class _LocalFakeCD2:
                    async def list_files(self, path: str, force_refresh: bool = False):
                        p = Path(path)
                        if not p.exists() or not p.is_dir():
                            return []
                        out = []
                        for child in sorted(p.iterdir(), key=lambda x: x.name):
                            st = child.stat()
                            out.append({
                                "name": child.name,
                                "path": child.as_posix(),
                                "isDirectory": child.is_dir(),
                                "size": st.st_size if child.is_file() else 0,
                                "modified": str(int(st.st_mtime)),
                            })
                        return out

                    async def delete_file(self, path: str):
                        return await self.delete_files([path])

                    async def delete_files(self, paths: list[str]):
                        for fp in paths:
                            p = Path(fp)
                            if p.is_dir():
                                shutil.rmtree(p, ignore_errors=True)
                            elif p.exists():
                                p.unlink()
                        return {"success": True}

                    async def move_file(self, src: str, dst_dir: str, skip_existing: bool = False):
                        Path(dst_dir).mkdir(parents=True, exist_ok=True)
                        shutil.move(src, str(Path(dst_dir) / Path(src).name))
                        return {"success": True}

                    async def mkdir(self, path: str):
                        Path(path).mkdir(parents=True, exist_ok=True)
                        return {"success": True}

                    async def close(self):
                        return None

                from app.config import get_settings as _real_get_settings
                _real_s = _real_get_settings()
                fake_settings = SimpleNamespace(
                    source_root=src_root.as_posix(),
                    archive_root=dst_root.as_posix(),
                    cd2_url=getattr(_real_s, "cd2_url", ""),
                    cd2_username=getattr(_real_s, "cd2_username", ""),
                    cd2_password=getattr(_real_s, "cd2_password", ""),
                    cd2_token=getattr(_real_s, "cd2_token", ""),
                )

                with patch("app.api.archive.CloudDrive2Client", return_value=_LocalFakeCD2()), \
                     patch("app.api.archive.get_settings", return_value=fake_settings), \
                     patch("app.api.archive.MoviePilotClient") as mock_mp_cls:
                    mock_mp_inst = mock_mp_cls.return_value
                    mock_mp_inst.get_active_downloads = AsyncMock(return_value=[])
                    mock_mp_inst.close = AsyncMock(return_value=None)

                    plan_resp = await build_recommended_batch_plan(RecommendedPlanRequest(task_id=task.id), db=db)
                    self.assertTrue(plan_resp["ok"])
                    self.assertEqual(plan_resp["counts"]["clean_count"], 1)
                    self.assertEqual(plan_resp["counts"]["redundant_source_count"], 1)

                    exec_resp = await execute_recommended_batch_plan(
                        RecommendedExecuteRequest(
                            task_id=task.id,
                            plan_id=plan_resp["plan_id"],
                        ),
                        db=db,
                    )
                    self.assertTrue(exec_resp["ok"])
                    outcomes = exec_resp["outcomes"]
                    # G28: 混合剧清掉 720p 后进入 to_download，已归档剧进入 source_cleaned
                    self.assertTrue(any(x["season_record_id"] == "rec-dup" for x in outcomes["to_download"]))
                    self.assertTrue(any(x["season_record_id"] == "rec-arc" for x in outcomes["source_cleaned"]))

                    # G10: 源端独有加更已先保留至归档库，原源端已清理
                    dst_files = {p.name for p in (arc_dst_show / "Season 1").iterdir()}
                    self.assertIn("已归档剧 - S01E01 - 加更.1080p.mp4", dst_files)
                    self.assertFalse((arc_src_show / "Season 1").exists())

                    # G27: 旧任务历史记录无损重算不会报错
                    rec_dup_db = (await db.execute(select(SeasonRecord).where(SeasonRecord.id == "rec-dup"))).scalar_one()
                    self.assertEqual(json.loads(rec_dup_db.duplicate_episodes_json or "[]"), [])
                    self.assertEqual(rec_dup_db.status, "download_needed")

        await engine.dispose()


if __name__ == "__main__":
    unittest.main()
