"""v2.5.84 专项验收与回归测试：
1. 「齐全可归档」分类与归档接口准入一致性（复现《奇迹少女 (2015) [tmdbid=65334]》S06 含扩展集 E27）；
2. 受阻条目精准模块导航（current_category / next_step 严禁误导至不存在该剧的「异常剧集」）；
3. 人工复核无误后强制归档（force_archive=True 放行状态/分段/同集异名多版本，保留审计日志）；
4. 强制归档下的物理安全硬红线（同名视频防覆盖、多目标季歧义、路径越界、空视频源季 100% 拒止且零写入）。
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding="utf-8")

_TMP_V2584_DATA_DIR = tempfile.mkdtemp(prefix="mf-test-v2584-")
atexit.register(shutil.rmtree, _TMP_V2584_DATA_DIR, True)
os.environ.update(
    MF_DATA_DIR=_TMP_V2584_DATA_DIR,
    MF_SOURCE_ROOT="/qa/src",
    MF_ARCHIVE_ROOT="/qa/dst",
    MF_LIBRARY_ROOT="/qa/dst",
    MF_AUTH_TOKEN="fixture",
)

from tests.test_archiver import _FakeCD2


class TestV2584ArchiveAlignmentAndForce(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        os.environ.update(
            MF_DATA_DIR=_TMP_V2584_DATA_DIR,
            MF_SOURCE_ROOT="/qa/src",
            MF_ARCHIVE_ROOT="/qa/dst",
            MF_LIBRARY_ROOT="/qa/dst",
            MF_AUTH_TOKEN="fixture",
        )
        from app.config import get_settings

        get_settings.cache_clear()
        import httpx
        from app.database import Base, engine, init_db
        from app.main import app
        from app.api.archive import _ARCHIVE_PREVIEW_PLANS, _COMPLETED_PLAN_SEASONS

        _ARCHIVE_PREVIEW_PLANS.clear()
        _COMPLETED_PLAN_SEASONS.clear()

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await init_db()
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fixture",
            headers={"Authorization": "Bearer fixture"},
        )

    async def asyncTearDown(self):
        from app.database import engine

        await self.http.aclose()
        await engine.dispose()

    async def test_01_miraculous_s06_supplemental_episode_normal_archive_succeeds(self):
        """复现《奇迹少女 (2015) [tmdbid=65334]》S06：官方 26 集已完结，本地 1..27 集且位于齐全可归档，常规归档直接通过."""
        from app.core.season_classifier import get_record_work_main_category
        from app.database import async_session
        from app.models.season import SeasonRecord
        from app.models.task import Task

        show_dir = "/qa/src/奇迹少女 (2015) [tmdbid=65334]"
        season_dir = f"{show_dir}/Season 6"
        dst_season_dir = "/qa/dst/奇迹少女 (2015) [tmdbid=65334]/Season 6"
        eps_1_to_27 = list(range(1, 28))

        async with async_session() as db:
            db.add(Task(id="t_miraculous", name="动画巡检", source_path="/qa/src", status="completed"))
            await db.flush()
            rec = SeasonRecord(
                id="rec_miraculous_s06",
                task_id="t_miraculous",
                show_name="奇迹少女 (2015) [tmdbid=65334]",
                show_path=show_dir,
                tmdb_id="65334",
                season_number=6,
                season_path=season_dir,
                archive_target_path=dst_season_dir,
                total_episodes=26,
                local_episode_count=27,
                recognized_episodes_json=json.dumps(eps_1_to_27),
                missing_episodes_json="[]",
                duplicate_episodes_json="[]",
                status="review_needed",
                status_detail="当前资料收录 26 集，本地另有集号 [27] 待核实。",
                season_finished=True,
                evidence_json=json.dumps({
                    "metadata_binding_verified": True,
                    "broadcast_evidence": {"status": "finished", "schedule_verified": True},
                    "review_plan_preview": {"can_clean": False, "reason": "全剧已就绪，无需清理任何文件"},
                }, ensure_ascii=False),
            )
            db.add(rec)
            await db.commit()
            work_cat = await get_record_work_main_category(db, rec)
            self.assertEqual(work_cat, "archive", "奇迹少女 S06 应稳定归入齐全可归档模块")

        season_files = [
            {
                "name": f"奇迹少女 - S06E{ep:02d}.mp4",
                "path": f"{season_dir}/奇迹少女 - S06E{ep:02d}.mp4",
                "isDirectory": False,
                "size": 100,
            }
            for ep in eps_1_to_27
        ]
        disk = _FakeCD2({
            "/qa/src": [{"name": "奇迹少女 (2015) [tmdbid=65334]", "path": show_dir, "isDirectory": True}],
            show_dir: [
                {"name": "Season 6", "path": season_dir, "isDirectory": True},
                {"name": "tvshow.nfo", "path": f"{show_dir}/tvshow.nfo", "isDirectory": False, "size": 10},
            ],
            season_dir: season_files,
        })

        with patch("app.api.archive.CloudDrive2Client", return_value=disk):
            prev = (await self.http.post("/api/archive/preview", json={
                "season_record_ids": ["rec_miraculous_s06"],
                "task_id": "t_miraculous",
            })).json()
            self.assertTrue(prev["ok"], f"齐全可归档内的扩展集条目常规预检不应受阻: {prev}")
            self.assertEqual(prev["executable_count"], 1)
            self.assertEqual(prev["blocked_count"], 0)
            item0 = prev["items"][0]
            self.assertTrue(item0["executable"])
            self.assertFalse(item0["force_archive"])
            self.assertTrue(
                any("附随扩展集一并归档" in ln and "[27]" in ln for ln in item0["summary_lines"]),
                f"预检摘要应提示附随扩展集 [27] 一并归档: {item0['summary_lines']}",
            )

            exe = (await self.http.post("/api/archive/execute", json={
                "season_record_ids": ["rec_miraculous_s06"],
                "plan_id": prev["plan_id"],
            })).json()
            self.assertTrue(exe["ok"], f"常规归档执行应成功: {exe}")
            self.assertEqual(exe["results"][0]["outcome"], "archived")
            self.assertEqual(len(disk.entries[dst_season_dir]), 27)

    async def test_02_blocked_navigation_accurately_reflects_actual_module(self):
        """受阻条目的 current_category 与 next_step 必须严格对应真实所在模块，绝不盲目导向 anomaly."""
        from app.database import async_session
        from app.models.season import SeasonRecord
        from app.models.task import Task

        async with async_session() as db:
            db.add(Task(id="t_nav", name="导航校准", source_path="/qa/src", status="completed"))
            await db.flush()
            # 1) 位于 review（待复核清理）的重复副本季
            db.add(SeasonRecord(
                id="rec_in_review",
                task_id="t_nav",
                show_name="待清理剧",
                show_path="/qa/src/待清理剧",
                tmdb_id="2001",
                season_number=1,
                season_path="/qa/src/待清理剧/Season 1",
                archive_target_path="/qa/dst/待清理剧/Season 1",
                total_episodes=2,
                local_episode_count=3,
                recognized_episodes_json="[1, 2]",
                duplicate_episodes_json="[1]",
                status="review_needed",
                season_finished=True,
                evidence_json=json.dumps({
                    "metadata_binding_verified": True,
                    "broadcast_evidence": {"status": "finished"},
                    "review_plan_preview": {"can_clean": True, "to_delete_count": 1},
                }),
            ))
            # 2) 位于 archive（齐全可归档），但目标库存在同名视频硬冲突
            db.add(SeasonRecord(
                id="rec_in_archive_dup_dst",
                task_id="t_nav",
                show_name="同季同名冲突剧",
                show_path="/qa/src/同季同名冲突剧",
                tmdb_id="2002",
                season_number=1,
                season_path="/qa/src/同季同名冲突剧/Season 1",
                archive_target_path="/qa/dst/同季同名冲突剧/Season 1",
                total_episodes=2,
                local_episode_count=2,
                recognized_episodes_json="[1, 2]",
                status="archive_ready",
                season_finished=True,
            ))
            # 3) 位于 anomaly（异常剧集）的未知集数条目
            db.add(SeasonRecord(
                id="rec_in_anomaly",
                task_id="t_nav",
                show_name="资料未知剧",
                show_path="/qa/src/资料未知剧",
                tmdb_id="2003",
                season_number=1,
                season_path="/qa/src/资料未知剧/Season 1",
                archive_target_path="/qa/dst/资料未知剧/Season 1",
                total_episodes=None,
                local_episode_count=4,
                recognized_episodes_json="[1, 2, 3, 4]",
                status="unknown",
                season_finished=False,
            ))
            await db.commit()

        disk = _FakeCD2({
            "/qa/src/同季同名冲突剧/Season 1": [
                {"name": "S01E01.mkv", "path": "/qa/src/同季同名冲突剧/Season 1/S01E01.mkv", "isDirectory": False, "size": 100},
                {"name": "S01E02.mkv", "path": "/qa/src/同季同名冲突剧/Season 1/S01E02.mkv", "isDirectory": False, "size": 100},
            ],
            "/qa/dst/同季同名冲突剧": [
                {"name": "Season 1", "path": "/qa/dst/同季同名冲突剧/Season 1", "isDirectory": True},
            ],
            "/qa/dst/同季同名冲突剧/Season 1": [
                {"name": "S01E01.mkv", "path": "/qa/dst/同季同名冲突剧/Season 1/S01E01.mkv", "isDirectory": False, "size": 100},
            ],
        })

        with patch("app.api.archive.CloudDrive2Client", return_value=disk):
            prev = (await self.http.post("/api/archive/preview", json={
                "season_record_ids": ["rec_in_review", "rec_in_archive_dup_dst", "rec_in_anomaly"],
                "task_id": "t_nav",
            })).json()
            by_id = {it["season_record_id"]: it for it in prev["items"]}

            self.assertEqual(by_id["rec_in_review"]["current_category"], "review")
            self.assertEqual(by_id["rec_in_review"]["next_step"], "goto_review")
            self.assertTrue(by_id["rec_in_review"]["can_force_archive"])

            self.assertEqual(by_id["rec_in_archive_dup_dst"]["current_category"], "archive")
            self.assertEqual(by_id["rec_in_archive_dup_dst"]["next_step"], "stay_in_archive")
            self.assertFalse(by_id["rec_in_archive_dup_dst"]["can_force_archive"])

            self.assertEqual(by_id["rec_in_anomaly"]["current_category"], "anomaly")
            self.assertEqual(by_id["rec_in_anomaly"]["next_step"], "goto_anomaly")
            self.assertTrue(by_id["rec_in_anomaly"]["can_force_archive"])

    async def test_03_force_archive_allows_reviewed_seasons_and_logs_audit(self):
        """人工复核无误后强制归档：放行状态限制、分段文件与同集异名双版本共存，并写入审计标记."""
        from sqlalchemy import select
        from app.database import async_session
        from app.models.operation import OperationLog
        from app.models.season import SeasonRecord
        from app.models.task import Task

        async with async_session() as db:
            db.add(Task(id="t_force", name="强制归档任务", source_path="/qa/src", status="completed"))
            await db.flush()
            # A: 资料未核实但用户人工核对齐全的季（含上下段命名）
            db.add(SeasonRecord(
                id="rec_force_unknown",
                task_id="t_force",
                show_name="冷门纪录片",
                show_path="/qa/src/冷门纪录片",
                tmdb_id="3001",
                season_number=1,
                season_path="/qa/src/冷门纪录片/Season 1",
                archive_target_path="/qa/dst/冷门纪录片/Season 1",
                total_episodes=None,
                local_episode_count=2,
                recognized_episodes_json="[1, 2]",
                status="unknown",
                season_finished=False,
            ))
            # B: 同集异名多剪辑版汇入已有目标季（源端 4K 导演剪辑版，目标端已有 1080p 版）
            db.add(SeasonRecord(
                id="rec_force_dual_cut",
                task_id="t_force",
                show_name="双版本剧",
                show_path="/qa/src/双版本剧",
                tmdb_id="3002",
                season_number=1,
                season_path="/qa/src/双版本剧/Season 1",
                archive_target_path="/qa/dst/双版本剧/Season 1",
                total_episodes=1,
                local_episode_count=1,
                recognized_episodes_json="[1]",
                status="review_needed",
                season_finished=True,
            ))
            await db.commit()

        disk = _FakeCD2({
            "/qa/src": [
                {"name": "冷门纪录片", "path": "/qa/src/冷门纪录片", "isDirectory": True},
                {"name": "双版本剧", "path": "/qa/src/双版本剧", "isDirectory": True},
            ],
            "/qa/src/冷门纪录片": [
                {"name": "Season 1", "path": "/qa/src/冷门纪录片/Season 1", "isDirectory": True},
            ],
            "/qa/src/冷门纪录片/Season 1": [
                {"name": "Doc.S01E01.上.mp4", "path": "/qa/src/冷门纪录片/Season 1/Doc.S01E01.上.mp4", "isDirectory": False, "size": 100},
                {"name": "Doc.S01E01.下.mp4", "path": "/qa/src/冷门纪录片/Season 1/Doc.S01E01.下.mp4", "isDirectory": False, "size": 100},
            ],
            "/qa/src/双版本剧": [
                {"name": "Season 1", "path": "/qa/src/双版本剧/Season 1", "isDirectory": True},
            ],
            "/qa/src/双版本剧/Season 1": [
                {"name": "Dual.S01E01.4K.DirectorCut.mkv", "path": "/qa/src/双版本剧/Season 1/Dual.S01E01.4K.DirectorCut.mkv", "isDirectory": False, "size": 200},
            ],
            "/qa/dst/双版本剧": [
                {"name": "Season 1", "path": "/qa/dst/双版本剧/Season 1", "isDirectory": True},
            ],
            "/qa/dst/双版本剧/Season 1": [
                {"name": "Dual.S01E01.1080p.WEB-DL.mkv", "path": "/qa/dst/双版本剧/Season 1/Dual.S01E01.1080p.WEB-DL.mkv", "isDirectory": False, "size": 100},
            ],
        })

        with patch("app.api.archive.CloudDrive2Client", return_value=disk):
            # 常规预检应阻断，但均标记 can_force_archive=True
            normal_prev = (await self.http.post("/api/archive/preview", json={
                "season_record_ids": ["rec_force_unknown", "rec_force_dual_cut"],
                "task_id": "t_force",
                "force_archive": False,
            })).json()
            self.assertFalse(normal_prev["ok"])
            for it in normal_prev["items"]:
                self.assertTrue(it["can_force_archive"], f"软性阻断应允许人工切换强制归档: {it}")

            # 切换为强制归档预检
            force_prev = (await self.http.post("/api/archive/preview", json={
                "season_record_ids": ["rec_force_unknown", "rec_force_dual_cut"],
                "task_id": "t_force",
                "force_archive": True,
            })).json()
            self.assertTrue(force_prev["ok"], f"强制归档预检应通过: {force_prev}")
            self.assertTrue(force_prev["force_archive"])
            self.assertEqual(force_prev["executable_count"], 2)
            for it in force_prev["items"]:
                self.assertTrue(it["mode_label"].startswith("强制归档 · "))
                self.assertTrue(any("人工复核强制归档" in ln for ln in it["summary_lines"]))

            # 按 plan_id 执行（即使请求体省略 force_archive，也应从 saved_plan 继承）
            exe = (await self.http.post("/api/archive/execute", json={
                "season_record_ids": ["rec_force_unknown", "rec_force_dual_cut"],
                "plan_id": force_prev["plan_id"],
            })).json()
            self.assertTrue(exe["ok"], f"强制归档执行应成功: {exe}")
            res_by_id = {r["season_record_id"]: r for r in exe["results"]}
            self.assertEqual(res_by_id["rec_force_unknown"]["outcome"], "archived")
            self.assertTrue(res_by_id["rec_force_unknown"]["force_archive"])
            self.assertEqual(res_by_id["rec_force_dual_cut"]["outcome"], "archived")

            # 目标季两个不同文件名版本同时存在，零覆盖
            dst_dual_names = [x["name"] for x in disk.entries["/qa/dst/双版本剧/Season 1"]]
            self.assertCountEqual(
                dst_dual_names,
                ["Dual.S01E01.1080p.WEB-DL.mkv", "Dual.S01E01.4K.DirectorCut.mkv"],
            )

        async with async_session() as db:
            logs = list((await db.execute(select(OperationLog).where(OperationLog.task_id == "t_force"))).scalars().all())
            self.assertEqual(len(logs), 2)
            for lg in logs:
                detail = json.loads(lg.detail_json or "{}")
                self.assertTrue(detail.get("force_archive"), f"OperationLog 必须记录 force_archive=True: {detail}")
                self.assertEqual(lg.status, "completed")

    async def test_04_force_archive_never_bypasses_hard_physical_redlines(self):
        """即使显式传入 force_archive=True，同名视频冲突、多候选目标季、越界路径、无视频空目录依然 100% 拦截且零写入."""
        from app.database import async_session
        from app.models.season import SeasonRecord
        from app.models.task import Task

        async with async_session() as db:
            db.add(Task(id="t_hard", name="硬红线测试", source_path="/qa/src", status="completed"))
            await db.flush()
            db.add_all([
                SeasonRecord(
                    id="hard_same_name",
                    task_id="t_hard",
                    show_name="同名冲突剧",
                    show_path="/qa/src/同名冲突剧",
                    tmdb_id="4001",
                    season_number=1,
                    season_path="/qa/src/同名冲突剧/Season 1",
                    archive_target_path="/qa/dst/同名冲突剧/Season 1",
                    total_episodes=1,
                    local_episode_count=1,
                    recognized_episodes_json="[1]",
                    status="review_needed",
                    season_finished=True,
                ),
                SeasonRecord(
                    id="hard_multi_cand",
                    task_id="t_hard",
                    show_name="多候选剧",
                    show_path="/qa/src/多候选剧",
                    tmdb_id="4002",
                    season_number=1,
                    season_path="/qa/src/多候选剧/Season 1",
                    archive_target_path="/qa/dst/多候选剧/Season 1",
                    total_episodes=1,
                    local_episode_count=1,
                    recognized_episodes_json="[1]",
                    status="unknown",
                    season_finished=False,
                ),
                SeasonRecord(
                    id="hard_out_of_root",
                    task_id="t_hard",
                    show_name="越界路径剧",
                    show_path="/other/越界路径剧",
                    tmdb_id="4003",
                    season_number=1,
                    season_path="/other/越界路径剧/Season 1",
                    archive_target_path="/qa/dst/越界路径剧/Season 1",
                    total_episodes=1,
                    local_episode_count=1,
                    recognized_episodes_json="[1]",
                    status="review_needed",
                    season_finished=True,
                ),
                SeasonRecord(
                    id="hard_no_video",
                    task_id="t_hard",
                    show_name="纯字幕空壳剧",
                    show_path="/qa/src/纯字幕空壳剧",
                    tmdb_id="4004",
                    season_number=1,
                    season_path="/qa/src/纯字幕空壳剧/Season 1",
                    archive_target_path="/qa/dst/纯字幕空壳剧/Season 1",
                    total_episodes=1,
                    local_episode_count=0,
                    recognized_episodes_json="[]",
                    status="unknown",
                    season_finished=False,
                ),
            ])
            await db.commit()

        disk = _FakeCD2({
            "/qa/src/同名冲突剧/Season 1": [
                {"name": "Same.S01E01.mkv", "path": "/qa/src/同名冲突剧/Season 1/Same.S01E01.mkv", "isDirectory": False, "size": 100},
            ],
            "/qa/dst/同名冲突剧": [
                {"name": "Season 1", "path": "/qa/dst/同名冲突剧/Season 1", "isDirectory": True},
            ],
            "/qa/dst/同名冲突剧/Season 1": [
                {"name": "Same.S01E01.mkv", "path": "/qa/dst/同名冲突剧/Season 1/Same.S01E01.mkv", "isDirectory": False, "size": 100},
            ],
            "/qa/src/多候选剧/Season 1": [
                {"name": "Multi.S01E01.mkv", "path": "/qa/src/多候选剧/Season 1/Multi.S01E01.mkv", "isDirectory": False, "size": 100},
            ],
            "/qa/dst/多候选剧": [
                {"name": "Season 1", "path": "/qa/dst/多候选剧/Season 1", "isDirectory": True},
                {"name": "Season 01", "path": "/qa/dst/多候选剧/Season 01", "isDirectory": True},
            ],
            "/qa/src/纯字幕空壳剧/Season 1": [
                {"name": "only.nfo", "path": "/qa/src/纯字幕空壳剧/Season 1/only.nfo", "isDirectory": False, "size": 5},
            ],
        })

        ids = ["hard_same_name", "hard_multi_cand", "hard_out_of_root", "hard_no_video"]
        with patch("app.api.archive.CloudDrive2Client", return_value=disk):
            prev = (await self.http.post("/api/archive/preview", json={
                "season_record_ids": ids,
                "task_id": "t_hard",
                "force_archive": True,
            })).json()
            self.assertFalse(prev["ok"])
            self.assertEqual(prev["executable_count"], 0)
            self.assertEqual(prev["blocked_count"], 4)
            for it in prev["items"]:
                self.assertFalse(it["executable"])
                self.assertFalse(it["can_force_archive"])

            exe = (await self.http.post("/api/archive/execute", json={
                "season_record_ids": ids,
                "force_archive": True,
            })).json()
            self.assertFalse(exe["ok"])
            self.assertEqual(disk.move_calls, [], "硬红线阻断时不得发生任何网盘移动操作")
            self.assertEqual(disk.delete_calls, [], "硬红线阻断时不得发生任何网盘删除操作")


if __name__ == "__main__":
    unittest.main()
