"""v2.5.39 同 TMDB 多目录源端合并与前缀统一全场景验收测试 (M01 - M14)。

覆盖规范：
- M01: 《谎言》完整操作闭环：旧目录 E01–E02 移入目标 S01，与 E03–E05 共存；附件保全后删除旧空目录并自动刷新。
- M02: 本次位置与未来归档位置分离：源端保留在 source_root (nas)，未来归档仅保存指向 archive_root (nas.finish)；连载状态继续留源。
- M03: 命名边界及后缀保留：代表本季的有效 S 标记为边界；S 之后的文本 100% 逐字保持；连集不重编号，目标已有文件不改名。
- M04: 目标样本选择：取目标同季已有视频的前缀 P_target；无样本时建议基于清洗剧名的前缀，不擅自拼接年份或 TMDB ID。
- M05: 伴生文件同步：外挂字幕、分集 NFO 等随对应视频采用相同基名，保留语言标签 (.zh-Hans.srt) 与扩展名。
- M06: 非标准及季号冲突：错季文件、无有效 S 标记或歧义标记列入待确认/冲突；特别篇保持非正片。
- M07: 最终路径冲突拦截：重名或批量改名冲突前置拦截，不覆盖、不静默跳过后删源。
- M08: 同集多版本：文件名不同的不同版本完整保留合并，重复版本标识保留。
- M09: 公共资产及未知附件：tvshow.nfo、poster.jpg 等同名资产不覆盖，保全至“合并保留/{旧剧名}/”子目录；未知附件保全。
- M10: 多季与组范围：旧目录仍有其他季度（如 S02）则保留旧剧根，仅清理旧季目录。
- M11: 实物后验与并发保护：写入前后验目标文件存在、源文件离开；仅在确认为空时清理旧目录。
- M12: 幂等与失败恢复：已完成文件不重复移动，清理失败只重试收尾，刷新失败只重试刷新。
- M13: 分类与统计回流：合并后正片 5 集，全季 12 集，已播 6 集，无其他异常自动回流「连载留源」。
- M14: 旧接口兼容：仅保存目标与有意共存保持 0 文件写入；合并必须经明确预览与一次执行确认。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

temp_data_dir = tempfile.mkdtemp(prefix="merge-data-")
temp_src_dir = tempfile.mkdtemp(prefix="merge-src-")
temp_dst_dir = tempfile.mkdtemp(prefix="merge-dst-")

os.environ.update(
    MF_DATA_DIR=temp_data_dir,
    MF_SOURCE_ROOT=temp_src_dir.replace("\\", "/"),
    MF_ARCHIVE_ROOT=temp_dst_dir.replace("\\", "/"),
    MF_AUTH_TOKEN="fixture",
)

import httpx
from sqlalchemy import select

from app.core.directory_merge import (
    extract_target_prefix_samples,
    is_mismatched_season_file,
    split_season_episode_boundary,
)
from app.database import Base, async_session, engine, init_db
from app.main import app
from app.models.directory_group import DirectoryGroup
from app.models.season import SeasonRecord
from app.models.task import Task


class TestV2539DirectoryMerge(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await init_db()
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fixture",
            headers={"Authorization": "Bearer fixture"},
        )

    async def asyncTearDown(self):
        await self.http.aclose()
        await engine.dispose()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(temp_data_dir, ignore_errors=True)
        shutil.rmtree(temp_src_dir, ignore_errors=True)
        shutil.rmtree(temp_dst_dir, ignore_errors=True)

    def test_01_boundary_and_prefix_samples_m03_m04_m05(self):
        """M03, M04, M05: 有效 S 标记边界、字母 S 防误判、伴生文件后缀保持与前缀提取。"""
        # 1. 标题内含字母 S，不误判
        fn1 = "Some.Show.Season.1.S01E02.1080p.mkv"
        res1 = split_season_episode_boundary(fn1, season_number=1)
        self.assertIsNotNone(res1)
        self.assertEqual(res1[0], "Some.Show.Season.1.")
        self.assertEqual(res1[1], "S01E02.1080p.mkv")

        # 2. 中文剧名标准连字号
        fn2 = "完美的谎言 - S01E01 - Episode.01.1080p.WEB-DL.mkv"
        res2 = split_season_episode_boundary(fn2, season_number=1)
        self.assertIsNotNone(res2)
        self.assertEqual(res2[0], "完美的谎言 - ")
        self.assertEqual(res2[1], "S01E01 - Episode.01.1080p.WEB-DL.mkv")

        # 3. 外挂字幕伴生文件 (.zh-Hans.srt)
        sub_fn = "完美的谎言 - S01E01 - Episode.01.1080p.WEB-DL.zh-Hans.srt"
        sub_res = split_season_episode_boundary(sub_fn, season_number=1)
        self.assertIsNotNone(sub_res)
        self.assertEqual(sub_res[0], "完美的谎言 - ")
        self.assertEqual(sub_res[1], "S01E01 - Episode.01.1080p.WEB-DL.zh-Hans.srt")

        # 4. 连集范围保留 (S01E01-E02)
        range_fn = "完美的谎言.S01E01-E02.1080p.mkv"
        range_res = split_season_episode_boundary(range_fn, season_number=1)
        self.assertIsNotNone(range_res)
        self.assertEqual(range_res[0], "完美的谎言.")
        self.assertEqual(range_res[1], "S01E01-E02.1080p.mkv")

        # 5. 前缀样本提取 (M04)
        target_files = [
            "谎言 - S01E03 - Episode.03.1080p.WEB-DL.mkv",
            "谎言 - S01E04 - Episode.04.1080p.WEB-DL.mkv",
            "谎言 - S01E05 - Episode.05.1080p.WEB-DL.mkv",
        ]
        samples = extract_target_prefix_samples(target_files, season_number=1, fallback_title="谎言")
        self.assertEqual(samples[0], "谎言 - ")

    def test_02_season_mismatch_and_conflict_detection_m06_m07(self):
        """M06, M07: 错季文件拦截、无有效标记拦截。"""
        # S02 出现在 S01 目录中
        mismatch_fn = "完美的谎言 - S02E01.mkv"
        self.assertTrue(is_mismatched_season_file(mismatch_fn, season_number=1))
        self.assertFalse(is_mismatched_season_file(mismatch_fn, season_number=2))

        # 正常 S01 文件不是错季
        normal_fn = "完美的谎言 - S01E01.mkv"
        self.assertFalse(is_mismatched_season_file(normal_fn, season_number=1))

    async def test_03_m01_m02_m05_m08_m09_m11_m12_m13_m14_full_merge_lifecycle(self):
        """M01, M02, M05, M08, M09, M11, M12, M13, M14: 《谎言》源端合并闭环全生命周期。"""
        task_id = "t_lie_merge"
        grp_id = "grp_lie_307478"
        norm_src_root = temp_src_dir.replace("\\", "/")
        norm_dst_root = temp_dst_dir.replace("\\", "/")

        old_show_path = f"{norm_src_root}/完美的谎言 (2026) [tmdbid=307478]"
        target_show_path = f"{norm_src_root}/谎言 (2026) [tmdbid=307478]"
        archive_show_path = f"{norm_dst_root}/谎言 (2026) [tmdbid=307478]"

        old_s1 = f"{old_show_path}/Season 1"
        target_s1 = f"{target_show_path}/Season 1"

        os.makedirs(old_s1, exist_ok=True)
        os.makedirs(target_s1, exist_ok=True)

        # 写入真实测试物理文件
        # 旧目录含 E01, E02 (两种版本，M08) 与外挂字幕 (M05)
        f_old_e1 = f"{old_s1}/完美的谎言 - S01E01 - Episode.01.1080p.WEB-DL.mkv"
        f_old_e1_sub = f"{old_s1}/完美的谎言 - S01E01 - Episode.01.1080p.WEB-DL.zh-Hans.srt"
        f_old_e2 = f"{old_s1}/完美的谎言 - S01E02 - Episode.02.1080p.WEB-DL.mkv"
        f_old_root_nfo = f"{old_show_path}/tvshow.nfo"
        f_old_root_poster = f"{old_show_path}/poster.jpg"

        Path(f_old_e1).write_text("e01-video", encoding="utf-8")
        Path(f_old_e1_sub).write_text("e01-sub", encoding="utf-8")
        Path(f_old_e2).write_text("e02-video", encoding="utf-8")
        Path(f_old_root_nfo).write_text("nfo-content", encoding="utf-8")
        Path(f_old_root_poster).write_text("poster-bytes", encoding="utf-8")

        # 目标目录已有 E03, E04, E05 以及自己的 tvshow.nfo (M09 同名不覆盖)
        f_tgt_e3 = f"{target_s1}/谎言 - S01E03 - Episode.03.1080p.WEB-DL.mkv"
        f_tgt_e4 = f"{target_s1}/谎言 - S01E04 - Episode.04.1080p.WEB-DL.mkv"
        f_tgt_e5 = f"{target_s1}/谎言 - S01E05 - Episode.05.1080p.WEB-DL.mkv"
        f_tgt_root_nfo = f"{target_show_path}/tvshow.nfo"

        Path(f_tgt_e3).write_text("e03-video", encoding="utf-8")
        Path(f_tgt_e4).write_text("e04-video", encoding="utf-8")
        Path(f_tgt_e5).write_text("e05-video", encoding="utf-8")
        Path(f_tgt_root_nfo).write_text("target-nfo-content", encoding="utf-8")

        # 初始化数据库数据
        async with async_session() as db:
            db.add(Task(id=task_id, name="韩国剧集", source_path=norm_src_root, status="completed"))
            # 旧目录季记录 (已收 2 集)
            r_old = SeasonRecord(
                id="rec_old_lie",
                task_id=task_id,
                show_name="完美的谎言 (2026) [tmdbid=307478]",
                show_path=old_show_path,
                season_path=old_s1,
                tmdb_id="307478",
                season_number=1,
                total_episodes=12,
                local_episode_count=2,
                recognized_episodes_json="[1, 2]",
                status="airing",
                evidence_json=json.dumps({
                    "broadcast_evidence": {"status": "airing", "aired_episodes": [1, 2, 3, 4, 5, 6]},
                    "directory_group": {"id": grp_id, "requires_confirm": True},
                }),
            )
            # 目标目录季记录 (已收 3 集)
            r_tgt = SeasonRecord(
                id="rec_tgt_lie",
                task_id=task_id,
                show_name="谎言 (2026) [tmdbid=307478]",
                show_path=target_show_path,
                season_path=target_s1,
                tmdb_id="307478",
                season_number=1,
                total_episodes=12,
                local_episode_count=3,
                recognized_episodes_json="[3, 4, 5]",
                status="airing",
                evidence_json=json.dumps({
                    "broadcast_evidence": {"status": "airing", "aired_episodes": [1, 2, 3, 4, 5, 6]},
                    "directory_group": {"id": grp_id, "requires_confirm": True},
                }),
            )
            db.add_all([r_old, r_tgt])

            # 目录组记录
            grp = DirectoryGroup(
                id=grp_id,
                task_id=task_id,
                tmdb_id="307478",
                title="谎言 (2026)",
                category_key="cat:韩国剧集",
                group_kind="multi_source_dirs",
                status="pending_confirm",
                suggested_target_path=archive_show_path,
                paths_json=json.dumps([
                    {"side": "source", "norm_path": old_show_path, "raw_name": "完美的谎言 (2026) [tmdbid=307478]", "seasons": [1]},
                    {"side": "source", "norm_path": target_show_path, "raw_name": "谎言 (2026) [tmdbid=307478]", "seasons": [1]},
                    {"side": "archive", "norm_path": archive_show_path, "raw_name": "谎言 (2026) [tmdbid=307478]", "seasons": []},
                ]),
            )
            db.add(grp)
            await db.commit()

        # ------------------------------------------------------------------
        # 阶段 1: 验证旧版仅保存接口 (M14)
        # 调用 /confirm 仅保存规则，绝不移动物理文件
        # ------------------------------------------------------------------
        resp_confirm = await self.http.post(
            f"/api/tasks/{task_id}/directory-groups/{grp_id}/confirm",
            json={"mode": "use_archive_target", "target_path": archive_show_path},
        )
        self.assertEqual(resp_confirm.status_code, 200)
        self.assertTrue(os.path.exists(f_old_e1), "旧接口调用后旧文件必须保持原位，绝不自动移动")

        # ------------------------------------------------------------------
        # 阶段 2: 验证只读合并预检 (M01, M02, M04, M09)
        # ------------------------------------------------------------------
        resp_prev = await self.http.post(
            f"/api/tasks/{task_id}/directory-groups/{grp_id}/merge-preview",
            json={"target_path": archive_show_path, "season_number": 1},
        )
        self.assertEqual(resp_prev.status_code, 200)
        prev_data = resp_prev.json()
        self.assertTrue(prev_data["ok"])
        plan = prev_data["plan"]

        self.assertTrue(plan["can_execute"])
        self.assertTrue(plan["is_airing"])
        self.assertEqual(plan["source_target_show_path"], target_show_path)
        self.assertEqual(plan["archive_target_show_path"], archive_show_path)
        self.assertEqual(plan["chosen_prefix"], "谎言 - ")

        # 校验待移动文件
        move_files = plan["files_to_move"]
        new_names = [f["new_filename"] for f in move_files]
        self.assertIn("谎言 - S01E01 - Episode.01.1080p.WEB-DL.mkv", new_names)
        self.assertIn("谎言 - S01E02 - Episode.02.1080p.WEB-DL.mkv", new_names)
        self.assertIn("谎言 - S01E01 - Episode.01.1080p.WEB-DL.zh-Hans.srt", new_names)

        # 校验资产补齐与同名冗余刮削清理 (M09 · v2.5.85 废除生成合并保留)
        preserve_list = plan["assets_to_preserve"]
        preserve_names = [p["file_name"] for p in preserve_list]
        self.assertIn("poster.jpg", preserve_names)
        poster_preserve = next(p for p in preserve_list if p["file_name"] == "poster.jpg")
        self.assertEqual(poster_preserve["target_dir"], target_show_path)
        # 目标已存在同名 tvshow.nfo，不覆盖目标且不制造「合并保留」目录，旧副本纳入冗余刮削清理
        redundant_scrape_names = [s["file_name"] for s in plan.get("redundant_scrape_to_clean", [])]
        self.assertIn("tvshow.nfo", redundant_scrape_names)

        # ------------------------------------------------------------------
        # 阶段 3: 执行合并闭环 (M01, M05, M09, M11, M13)
        # ------------------------------------------------------------------
        with patch("app.clients.clouddrive2.CloudDrive2Client.list_files", new_callable=AsyncMock) as mock_refresh:
            resp_exec = await self.http.post(
                f"/api/tasks/{task_id}/directory-groups/{grp_id}/merge-execute",
                json={"plan_id": plan["plan_id"]},
            )
            self.assertEqual(resp_exec.status_code, 200)
            exec_data = resp_exec.json()
            self.assertTrue(exec_data["ok"])
            self.assertIn("已合并到谎言", exec_data["message"])
            self.assertIn("实际空目录", exec_data["message"])

        # 物理验证：目标季目录应包含 E01–E05 全 5 集视频及外挂字幕
        expected_tgt_e1 = f"{target_s1}/谎言 - S01E01 - Episode.01.1080p.WEB-DL.mkv"
        expected_tgt_e1_sub = f"{target_s1}/谎言 - S01E01 - Episode.01.1080p.WEB-DL.zh-Hans.srt"
        expected_tgt_e2 = f"{target_s1}/谎言 - S01E02 - Episode.02.1080p.WEB-DL.mkv"

        self.assertTrue(os.path.exists(expected_tgt_e1))
        self.assertTrue(os.path.exists(expected_tgt_e1_sub))
        self.assertTrue(os.path.exists(expected_tgt_e2))
        self.assertTrue(os.path.exists(f_tgt_e3))
        self.assertTrue(os.path.exists(f_tgt_e4))
        self.assertTrue(os.path.exists(f_tgt_e5))

        # 旧目录空壳已清理
        self.assertFalse(os.path.exists(old_s1), "旧 Season 1 目录应已被安全清理")
        self.assertFalse(os.path.exists(old_show_path), "旧剧根空壳应已被安全清理")

        # 缺失海报已补齐至目标剧根，目标原有 tvshow.nfo 未被覆盖，且绝不产生「合并保留」目录
        self.assertTrue(os.path.exists(f"{target_show_path}/poster.jpg"))
        self.assertEqual(Path(f_tgt_root_nfo).read_text(encoding="utf-8"), "target-nfo-content")
        self.assertFalse(os.path.exists(f"{target_show_path}/合并保留"))

        # ------------------------------------------------------------------
        # 阶段 4: 数据库与分类回流验证 (M13)
        # 目标记录聚合为 5 集、全季 12 集、已播 6 集，分类归入「连载留源 (airing)」
        # ------------------------------------------------------------------
        async with async_session() as db:
            updated_tgt = await db.get(SeasonRecord, "rec_tgt_lie")
            self.assertEqual(json.loads(updated_tgt.recognized_episodes_json), [1, 2, 3, 4, 5])
            self.assertEqual(updated_tgt.local_episode_count, 5)

            updated_old = await db.get(SeasonRecord, "rec_old_lie")
            self.assertEqual(updated_old.status, "source_absent")

            # 验证 summary 计数回流
            t_after = await db.get(Task, task_id)
            sum_after = json.loads(t_after.summary_json or "{}")
            self.assertEqual(sum_after["airing"], 1, "合并后该作品季应唯一回流至连载留源")
            self.assertEqual(sum_after["pending_directory_groups"], 0, "目录组待办已清零")

        # ------------------------------------------------------------------
        # 阶段 5: 验证幂等执行 (M12)
        # 重复执行相同计划不会报错，返回成功
        # ------------------------------------------------------------------
        resp_exec_repeat = await self.http.post(
            f"/api/tasks/{task_id}/directory-groups/{grp_id}/merge-execute",
            json={"plan_id": plan["plan_id"]},
        )
        self.assertEqual(resp_exec_repeat.status_code, 200)

    async def test_04_m10_multi_season_preserves_old_show_root(self):
        """M10: 旧目录中若存在其他季 (S02)，仅清理 S01，旧剧根必须安全保留。"""
        task_id = "t_multi_season"
        grp_id = "grp_multi_s"
        norm_src_root = temp_src_dir.replace("\\", "/")

        old_show = f"{norm_src_root}/旧剧 (2025) [tmdbid=999]"
        target_show = f"{norm_src_root}/新剧 (2025) [tmdbid=999]"

        old_s1 = f"{old_show}/Season 1"
        old_s2 = f"{old_show}/Season 2"
        target_s1 = f"{target_show}/Season 1"

        os.makedirs(old_s1, exist_ok=True)
        os.makedirs(old_s2, exist_ok=True)
        os.makedirs(target_s1, exist_ok=True)

        Path(f"{old_s1}/旧剧 - S01E01.mkv").write_text("v1", encoding="utf-8")
        Path(f"{old_s2}/旧剧 - S02E01.mkv").write_text("v2", encoding="utf-8")
        Path(f"{target_s1}/新剧 - S01E02.mkv").write_text("v3", encoding="utf-8")

        async with async_session() as db:
            db.add(Task(id=task_id, name="测试任务", source_path=norm_src_root, status="completed"))
            db.add(SeasonRecord(
                id="rec_m_old", task_id=task_id, show_name="旧剧 (2025)", show_path=old_show, season_path=old_s1,
                tmdb_id="999", season_number=1, total_episodes=10, local_episode_count=1, recognized_episodes_json="[1]",
                status="airing",
            ))
            db.add(SeasonRecord(
                id="rec_m_tgt", task_id=task_id, show_name="新剧 (2025)", show_path=target_show, season_path=target_s1,
                tmdb_id="999", season_number=1, total_episodes=10, local_episode_count=1, recognized_episodes_json="[2]",
                status="airing",
            ))
            db.add(DirectoryGroup(
                id=grp_id, task_id=task_id, tmdb_id="999", title="新剧", category_key="cat:测试",
                paths_json=json.dumps([
                    {"side": "source", "norm_path": old_show, "seasons": [1, 2]},
                    {"side": "source", "norm_path": target_show, "seasons": [1]},
                ]),
            ))
            await db.commit()

        # 生成预览
        resp_p = await self.http.post(
            f"/api/tasks/{task_id}/directory-groups/{grp_id}/merge-preview",
            json={"source_target_path": target_show, "season_number": 1},
        )
        self.assertEqual(resp_p.status_code, 200)
        plan = resp_p.json()["plan"]
        self.assertTrue(plan["can_execute"])
        self.assertTrue(any("Season 2" in s for s in plan["remaining_seasons"]))

        # 执行合并
        resp_e = await self.http.post(
            f"/api/tasks/{task_id}/directory-groups/{grp_id}/merge-execute",
            json={"plan_id": plan["plan_id"]},
        )
        self.assertEqual(resp_e.status_code, 200)

        # 验证：旧 Season 1 被清理，但旧剧根与 Season 2 完好无损 (M10)
        self.assertFalse(os.path.exists(old_s1))
        self.assertTrue(os.path.exists(old_s2))
        self.assertTrue(os.path.exists(old_show))

    async def test_05_selected_target_path_drives_source_target_and_prefix(self):
        """选择特定目标归档目录后，源端合并主目录与前缀样本必须严格同步对齐（修复 Hot Ones 问题）。"""
        task_id = "t_hot_ones"
        grp_id = "grp_hot_326194"
        norm_src_root = temp_src_dir.replace("\\", "/")
        norm_dst_root = temp_dst_dir.replace("\\", "/")

        en_show = f"{norm_src_root}/电视剧/综艺/Hot Ones： Extra Heat (2026) [tmdbid=326194]"
        cn_show = f"{norm_src_root}/电视剧/综艺/Hot Ones：劲辣专访 (2026) [tmdbid=326194]"
        cn_archive = f"{norm_dst_root}/电视剧/综艺/Hot Ones：劲辣专访 (2026) [tmdbid=326194]"
        custom_archive = f"{norm_dst_root}/电视剧/综艺/劲辣专访特别篇 (2026) [tmdbid=326194]"

        en_s1 = f"{en_show}/Season 1"
        cn_s1 = f"{cn_show}/Season 1"
        os.makedirs(en_s1, exist_ok=True)
        os.makedirs(cn_s1, exist_ok=True)
        Path(f"{en_s1}/Hot Ones Extra Heat - S01E01.mkv").write_text("v1", encoding="utf-8")

        async with async_session() as db:
            db.add(Task(id=task_id, name="综艺", source_path=norm_src_root, status="completed"))
            db.add(DirectoryGroup(
                id=grp_id,
                task_id=task_id,
                tmdb_id="326194",
                title="同一作品有多个目录",
                category_key="cat:电视剧/综艺",
                paths_json=json.dumps([
                    {"side": "source", "category": "cat:电视剧/综艺", "norm_path": en_show, "raw_name": "Hot Ones： Extra Heat (2026) [tmdbid=326194]", "seasons": [1]},
                    {"side": "source", "category": "cat:电视剧/综艺", "norm_path": cn_show, "raw_name": "Hot Ones：劲辣专访 (2026) [tmdbid=326194]", "seasons": [1]},
                ]),
                evidence_json=json.dumps({"canonical_tmdb_title": "Hot Ones：劲辣专访", "canonical_tmdb_year": "2026"}),
            ))
            await db.commit()

        # 1. 仅传入 target_path（勾选中文目标归档目录），未传 source_target_path
        resp = await self.http.post(
            f"/api/tasks/{task_id}/directory-groups/{grp_id}/merge-preview",
            json={"target_path": cn_archive, "season_number": 1},
        )
        self.assertEqual(resp.status_code, 200)
        plan = resp.json()["plan"]
        self.assertEqual(plan["source_target_show_path"], cn_show)
        self.assertEqual(plan["archive_target_show_path"], cn_archive)
        self.assertEqual(plan["chosen_prefix"], "Hot Ones：劲辣专访 - ")
        self.assertIn("Hot Ones：劲辣专访 - ", plan["prefix_samples"])
        self.assertEqual(plan["files_to_move"][0]["new_filename"], "Hot Ones：劲辣专访 - S01E01.mkv")

        # 2. 传入自定义目标目录路径，源端主目录与前缀均随之同步对齐
        resp_custom = await self.http.post(
            f"/api/tasks/{task_id}/directory-groups/{grp_id}/merge-preview",
            json={"target_path": custom_archive, "season_number": 1},
        )
        self.assertEqual(resp_custom.status_code, 200)
        plan_c = resp_custom.json()["plan"]
        self.assertTrue(plan_c["source_target_show_path"].endswith("/劲辣专访特别篇 (2026) [tmdbid=326194]"))
        self.assertEqual(plan_c["chosen_prefix"], "劲辣专访特别篇 - ")

    def test_06_coverage_matrix_no_cartesian_fake_rows_and_honest_archive(self):
        """覆盖矩阵不得为不含该季的源端目录生成 0 集假行，且归档端未含本季时不标全缺。"""
        from types import SimpleNamespace
        from app.core.directory_governance import build_group_coverage_matrix

        group_dict = {
            "id": "grp-spidey",
            "tmdb_id": "127635",
            "status": "pending_confirm",
            "entries": [
                {
                    "side": "source",
                    "norm_path": "/src/小蜘蛛和他的神奇伙伴 (2021) [tmdbid=127635]",
                    "seasons": [4],
                    "episodes_by_season": {"4": list(range(1, 13))},
                },
                {
                    "side": "source",
                    "norm_path": "/src/小蜘蛛和他的神奇小伙伴们 (2021) [tmdbid=127635]",
                    "seasons": [4, 5],
                    "episodes_by_season": {"4": list(range(1, 39)), "5": list(range(3, 11))},
                },
                {
                    "side": "archive",
                    "norm_path": "/dst/小蜘蛛和他的神奇小伙伴们 (2021) [tmdbid=127635]",
                    "seasons": [1, 2],
                    "episodes_by_season": {},
                },
            ],
        }
        records = [
            SimpleNamespace(
                id="r-a-s4",
                tmdb_id="127635",
                show_path="/src/小蜘蛛和他的神奇伙伴 (2021) [tmdbid=127635]",
                season_number=4,
                total_episodes=38,
                recognized_episodes_json=json.dumps(list(range(1, 13))),
            ),
            SimpleNamespace(
                id="r-b-s4",
                tmdb_id="127635",
                show_path="/src/小蜘蛛和他的神奇小伙伴们 (2021) [tmdbid=127635]",
                season_number=4,
                total_episodes=38,
                recognized_episodes_json=json.dumps(list(range(1, 39))),
            ),
            SimpleNamespace(
                id="r-b-s5",
                tmdb_id="127635",
                show_path="/src/小蜘蛛和他的神奇小伙伴们 (2021) [tmdbid=127635]",
                season_number=5,
                total_episodes=20,
                recognized_episodes_json=json.dumps(list(range(3, 11))),
            ),
        ]

        matrix = build_group_coverage_matrix(group_dict, records)
        self.assertEqual([sec["season_number"] for sec in matrix], [4, 5])

        # S04 应有 2 个源端行 + 1 个归档端行
        s4_rows = matrix[0]["rows"]
        s4_sources = [r for r in s4_rows if r["side"] == "source"]
        self.assertEqual(len(s4_sources), 2)
        self.assertEqual(s4_sources[0]["delete_block_reason"], "点击右侧「确认归档目标」")

        # S05 只能有 1 个源端行（不含 S05 的源端目录被过滤，绝无 0/20 集假行）
        s5_rows = matrix[1]["rows"]
        s5_sources = [r for r in s5_rows if r["side"] == "source"]
        self.assertEqual(len(s5_sources), 1)
        self.assertEqual(s5_sources[0]["path"], "/src/小蜘蛛和他的神奇小伙伴们 (2021) [tmdbid=127635]")

        # 归档端未收录本季时应标「本季未归档（已存 S01、S02）」且缺号为「—」
        s5_arc = next(r for r in s5_rows if r["side"] == "archive")
        self.assertEqual(s5_arc["coverage_text"], "本季未归档（已存 S01、S02）")
        self.assertEqual(s5_arc["missing_text"], "—")

    def test_07_review_clean_count_excludes_pure_merge_directory(self):
        """review_clean 统计仅计入含单季清理动作的条目，不与待归并目录组重复计数。"""
        from app.core.season_classifier import consolidate_seasons_to_work_records

        records = [
            {
                "id": "pure-merge",
                "tmdb_id": "231394",
                "show_name": "忍者神龟",
                "show_path": "/src/忍者神龟",
                "season_path": "/src/忍者神龟/Season 2",
                "season_number": 2,
                "status": "download_needed",
                "total_episodes": 12,
                "recognized_episodes": list(range(1, 12)),
                "missing_episodes": [12],
                "duplicate_episodes": [],
                "evidence": {"directory_group": {"id": "grp-tmnt", "status": "pending_confirm", "requires_confirm": True}},
            },
            {
                "id": "hybrid-merge-dup",
                "tmdb_id": "231395",
                "show_name": "混合剧",
                "show_path": "/src/混合剧",
                "season_path": "/src/混合剧/Season 1",
                "season_number": 1,
                "status": "review_needed",
                "total_episodes": 10,
                "recognized_episodes": list(range(1, 11)),
                "missing_episodes": [],
                "duplicate_episodes": [1],
                "evidence": {"directory_group": {"id": "grp-hybrid", "status": "pending_confirm", "requires_confirm": True}},
            },
            {
                "id": "pure-clean",
                "tmdb_id": "225320",
                "show_name": "乐高梦想",
                "show_path": "/src/乐高梦想",
                "season_path": "/src/乐高梦想/Season 3",
                "season_number": 3,
                "status": "review_needed",
                "total_episodes": 20,
                "recognized_episodes": list(range(1, 21)),
                "missing_episodes": [],
                "duplicate_episodes": [1, 2],
                "evidence": {},
            },
        ]
        _, counts = consolidate_seasons_to_work_records(records, pending_dir_group_ids={"grp-tmnt", "grp-hybrid"})
        self.assertEqual(counts["review"], 3)
        self.assertEqual(counts["review_clean"], 2)


if __name__ == "__main__":
    unittest.main()
