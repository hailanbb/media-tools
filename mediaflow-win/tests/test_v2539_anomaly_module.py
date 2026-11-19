"""v2.5.39 异常剧集模块全场景验收测试（人工确认、冲突与撤销、本次保留、目录确认重算、失败重试、外部修改刷新与跨轮指纹差分）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(
    MF_DATA_DIR=tempfile.mkdtemp(prefix="anomaly-v2539-"),
    MF_SOURCE_ROOT="/qa/src",
    MF_ARCHIVE_ROOT="/qa/dst",
    MF_AUTH_TOKEN="fixture",
)

import httpx
from sqlalchemy import select

from app.core.inspector import InspectionResult, SeasonDiagnosis, run_inspection
from app.database import Base, async_session, engine, init_db
from app.main import app
from app.models.directory_group import DirectoryGroup
from app.models.metadata_cache import MetadataCache
from app.models.season import SeasonRecord
from app.models.task import Task
from app.services.task_service import execute_inspection


class TestV2539AnomalyModule(unittest.IsolatedAsyncioTestCase):
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

    async def test_01_manual_confirmation_lifecycle_reuse_contradiction_and_revoke(self):
        """《性爱治疗师》人工确认闭环：全未选拦截、仅确认身份、确认4集已播毕转可归档、跨轮复用、新发现E05矛盾暂停、修改与撤销、防误继承。"""
        show = "性爱治疗师 (2025) [tmdbid=315775]"
        show_path = f"/qa/src/{show}"
        season_path = f"{show_path}/Season 1"

        async with async_session() as db:
            db.add(Task(id="t_therapy", name="韩国剧集", source_path="/qa/src", status="completed"))
            rec = SeasonRecord(
                id="rec_therapy",
                task_id="t_therapy",
                show_name=show,
                show_path=show_path,
                season_path=season_path,
                tmdb_id="315775",
                season_number=1,
                total_episodes=None,
                local_episode_count=4,
                recognized_episodes_json="[1, 2, 3, 4]",
                missing_episodes_json="[]",
                duplicate_episodes_json="[]",
                season_finished=False,
                status="unknown",
                status_detail="已识别 E01–E04，共 4 集；暂不确定全季总集数。",
                evidence_json=json.dumps({
                    "meta_status": "metadata_unverified",
                    "candidates": [{"tmdb_id": 315653, "season_number": 1, "episode_count": 4, "result": "reference_only"}],
                }),
            )
            db.add(rec)
            await db.commit()

        # 1. 全未选择提交 -> 400 拒绝
        r_empty = await self.http.post(
            "/api/tasks/t_therapy/seasons/rec_therapy/manual-confirm",
            json={"confirm_current_identity": False, "broadcast_status": "unknown"},
        )
        self.assertEqual(r_empty.status_code, 400)

        # 2. 仅确认身份 -> 解除 metadata_unverified，显示本季总集数待补充，留在异常
        r_id_only = await self.http.post(
            "/api/tasks/t_therapy/seasons/rec_therapy/manual-confirm",
            json={"confirm_current_identity": True, "broadcast_status": "unknown"},
        )
        self.assertEqual(r_id_only.status_code, 200, r_id_only.text)
        data_id = r_id_only.json()
        self.assertEqual(data_id["target_category"], "anomaly")
        self.assertIn("本季总集数待补充", data_id["season"]["status_detail"])

        # 3. 确认 315775 S01 共 4 集且已播毕 -> 转入齐全可归档，且不写入官方 MetadataCache
        r_prev = await self.http.post(
            "/api/tasks/t_therapy/seasons/rec_therapy/manual-confirm/preview",
            json={"confirm_current_identity": True, "total_episodes": 4, "broadcast_status": "finished"},
        )
        self.assertEqual(r_prev.json()["predicted_category"], "archive")

        r_full = await self.http.post(
            "/api/tasks/t_therapy/seasons/rec_therapy/manual-confirm",
            json={"confirm_current_identity": True, "total_episodes": 4, "broadcast_status": "finished", "note": "已核对片头"},
        )
        self.assertEqual(r_full.status_code, 200, r_full.text)
        data_full = r_full.json()
        self.assertEqual(data_full["target_category"], "archive")
        self.assertEqual(data_full["season"]["status"], "archive_ready")
        self.assertEqual(data_full["summary"]["archive_ready"], 1)
        self.assertEqual(data_full["summary"]["archive_check"], 0)

        async with async_session() as db:
            cached = await db.scalar(select(MetadataCache).where(MetadataCache.cache_key == "tmdb:315775:S1"))
            self.assertIsNone(cached)

        # 4. 离线重复重新核算：相同候选 315653 与无官方资料绝不推翻人工确认
        r_recalc = await self.http.post("/api/tasks/t_therapy/recalculate", json={"fetch_remote": False})
        self.assertEqual(r_recalc.status_code, 200)
        seasons_after = (await self.http.get("/api/tasks/t_therapy/seasons")).json()
        self.assertEqual(seasons_after[0]["main_category"], "archive")
        self.assertEqual(seasons_after[0]["status"], "archive_ready")

        # 5. 新实质矛盾：发现正片 E05 超出已确认总集数 4 -> 暂停基于总集数的准入，回退异常并保留身份确认
        async with async_session() as db:
            rec_db = await db.get(SeasonRecord, "rec_therapy")
            rec_db.recognized_episodes_json = "[1, 2, 3, 4, 5]"
            rec_db.local_episode_count = 5
            await db.commit()

        await self.http.post("/api/tasks/t_therapy/recalculate", json={"fetch_remote": False})
        seasons_contra = (await self.http.get("/api/tasks/t_therapy/seasons")).json()
        self.assertEqual(seasons_contra[0]["main_category"], "anomaly")
        self.assertEqual(seasons_contra[0]["anomaly_sub_category"], "decision")
        mc_ev = seasons_contra[0]["evidence"]["manual_confirmation"]
        self.assertTrue(mc_ev["identity_confirmed"])
        self.assertTrue(mc_ev["contradiction"]["has_contradiction"])
        self.assertIn("E05", seasons_contra[0]["status_detail"])

        # 6. 修改确认为共 6 集且已播毕 -> 转入真缺集待补（缺 E06）
        r_mod = await self.http.post(
            "/api/tasks/t_therapy/seasons/rec_therapy/manual-confirm",
            json={"confirm_current_identity": True, "total_episodes": 6, "broadcast_status": "finished"},
        )
        self.assertEqual(r_mod.json()["target_category"], "download")
        self.assertEqual(r_mod.json()["season"]["missing_episodes"], [6])

        # 7. 撤销确认 -> 恢复 unknown (metadata_unverified)
        r_rev = await self.http.delete("/api/tasks/t_therapy/seasons/rec_therapy/manual-confirm")
        self.assertEqual(r_rev.status_code, 200)
        self.assertEqual(r_rev.json()["season"]["main_category"], "anomaly")
        self.assertEqual(r_rev.json()["season"]["status"], "unknown")

    async def test_02_hold_for_task_counts_restore_and_next_scan_reset(self):
        """本次保留，不再提示：移出五模块待办、已扫描总量不变、刷新与重算保持保留、支持恢复、下一轮初检正常评估。"""
        async with async_session() as db:
            db.add(Task(id="t_hold", name="综艺", source_path="/qa/src", status="completed"))
            db.add_all([
                SeasonRecord(
                    id="rec_asian_voice",
                    task_id="t_hold",
                    show_name="亚洲新声 (2025) [tmdbid=290001]",
                    show_path="/qa/src/亚洲新声",
                    season_path="/qa/src/亚洲新声/Season 1",
                    tmdb_id="290001",
                    season_number=1,
                    total_episodes=10,
                    local_episode_count=10,
                    recognized_episodes_json="[1,2,3,4,5,6,7,8,9,10]",
                    status="period_review",
                    status_detail="已识别第 1–10 期编号，暂缺能确认整期版的发行资料。文件已保留。",
                    evidence_json=json.dumps({"period_evidence": {"is_segmented": True}}),
                ),
                SeasonRecord(
                    id="rec_ready_one",
                    task_id="t_hold",
                    show_name="正常剧 (2024) [tmdbid=290002]",
                    show_path="/qa/src/正常剧",
                    season_path="/qa/src/正常剧/Season 1",
                    tmdb_id="290002",
                    season_number=1,
                    total_episodes=4,
                    local_episode_count=4,
                    recognized_episodes_json="[1,2,3,4]",
                    season_finished=True,
                    status="archive_ready",
                    status_detail="全 4 集已齐",
                ),
            ])
            await db.commit()

        # 1. 执行本次保留
        r_hold = await self.http.post("/api/tasks/t_hold/seasons/rec_asian_voice/hold", json={"reason": "本轮暂不处理"})
        self.assertEqual(r_hold.status_code, 200)
        summary = r_hold.json()["summary"]
        self.assertEqual(summary["archive_check"], 0)
        self.assertEqual(summary["archive_ready"], 1)
        self.assertEqual(summary["held_seasons"], 1)
        self.assertEqual(summary["total_seasons"], 2)  # 已扫描作品季总量不变

        # 2. 离线重算不会重新弹出
        await self.http.post("/api/tasks/t_hold/recalculate", json={"fetch_remote": False})
        anom_list = (await self.http.get("/api/tasks/t_hold/seasons?status=anomaly")).json()
        held_list = (await self.http.get("/api/tasks/t_hold/seasons?status=held")).json()
        self.assertEqual(len(anom_list), 0)
        self.assertEqual(len(held_list), 1)
        self.assertEqual(held_list[0]["id"], "rec_asian_voice")

        # 3. 恢复处理 -> 回到异常待办且不产生重复主卡
        r_restore = await self.http.delete("/api/tasks/t_hold/seasons/rec_asian_voice/hold")
        self.assertEqual(r_restore.status_code, 200)
        self.assertEqual(r_restore.json()["summary"]["archive_check"], 1)
        self.assertEqual(r_restore.json()["summary"]["held_seasons"], 0)
        all_seasons = (await self.http.get("/api/tasks/t_hold/seasons")).json()
        self.assertEqual(len(all_seasons), 2)

        # 4. 再次设为本次保留后执行下一轮初检 -> 下一轮初检正常评估，不继承永久忽略，且未变指纹标记 unchanged_shelved
        await self.http.post("/api/tasks/t_hold/seasons/rec_asian_voice/hold")
        diag_next = SeasonDiagnosis(
            show_name="亚洲新声 (2025) [tmdbid=290001]",
            show_path="/qa/src/亚洲新声",
            season_path="/qa/src/亚洲新声/Season 1",
            tmdb_id=290001,
            season_number=1,
            total_episodes=10,
            local_episode_count=10,
            recognized_episodes=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            status="period_review",
            status_detail="已识别第 1–10 期编号",
            season_finished=False,
            evidence={"period_evidence": {"is_segmented": True}},
        )
        with (
            patch("app.services.task_service.run_inspection", new=AsyncMock(return_value=InspectionResult(
                task_id="t_hold", source_path="/qa/src", seasons=[diag_next]
            ))),
            patch("app.clients.clouddrive2.CloudDrive2Client", return_value=AsyncMock()),
            patch("app.clients.moviepilot.MoviePilotClient", return_value=AsyncMock()),
            patch("app.clients.tmdb.TMDBClient", return_value=AsyncMock()),
        ):
            async with async_session() as db:
                await execute_inspection("t_hold", db)

        seasons_rescan = (await self.http.get("/api/tasks/t_hold/seasons")).json()
        self.assertEqual(len(seasons_rescan), 1)
        self.assertEqual(seasons_rescan[0]["main_category"], "anomaly")
        self.assertFalse(seasons_rescan[0]["is_held_for_task"])
        self.assertEqual(seasons_rescan[0]["evidence"].get("delta_state"), "unchanged_shelved")

    async def test_03_boss_directory_group_confirm_returns_to_airing_and_retry_failed_scope(self):
        """《BOSS的甜蜜诱惑》确认目录后自动回到连载留源；重试失败项仅补查失败记录。"""
        async with async_session() as db:
            db.add(Task(id="t_boss", name="韩剧", source_path="/qa/src", status="completed"))
            grp = DirectoryGroup(
                id="grp_boss",
                task_id="t_boss",
                tmdb_id="326975",
                category_key="韩剧",
                group_kind="multi_source_same_tmdb",
                title="同 ID 多源目录待确认",
                paths_signature="sig1",
                paths_json=json.dumps([
                    {"side": "source", "norm_path": "/qa/src/BossA", "seasons": [1], "episodes_by_season": {"1": [1]}},
                    {"side": "source", "norm_path": "/qa/src/BossB", "seasons": [1], "episodes_by_season": {"1": [2]}},
                ]),
                suggested_target_path="/qa/dst/BOSS的甜蜜诱惑 (2026) [tmdbid=326975]",
                status="pending_confirm",
                season_count=1,
            )
            db.add(grp)
            for rid, spath, eps in (("boss_1", "/qa/src/BossA", "[1]"), ("boss_2", "/qa/src/BossB", "[2]")):
                db.add(SeasonRecord(
                    id=rid,
                    task_id="t_boss",
                    show_name="BOSS的甜蜜诱惑 (2026) [tmdbid=326975]",
                    show_path=spath,
                    season_path=f"{spath}/Season 1",
                    tmdb_id="326975",
                    season_number=1,
                    total_episodes=12,
                    local_episode_count=1,
                    recognized_episodes_json=eps,
                    season_finished=False,
                    status="airing",
                    status_detail="连载中",
                    evidence_json=json.dumps({
                        "directory_group": {"id": "grp_boss", "requires_confirm": True},
                        "broadcast_evidence": {"status": "airing", "schedule_verified": True, "aired_episodes": [1, 2], "upcoming_episodes": [3, 4]},
                    }),
                ))
            db.add(SeasonRecord(
                id="rec_failed_one",
                task_id="t_boss",
                show_name="超时剧 [tmdbid=399999]",
                show_path="/qa/src/TimeoutShow",
                season_path="/qa/src/TimeoutShow/Season 1",
                tmdb_id="399999",
                season_number=1,
                local_episode_count=2,
                recognized_episodes_json="[1, 2]",
                status="unknown",
                status_detail="查询超时",
                evidence_json=json.dumps({"meta_status": "fetch_failed", "meta_error": "timeout"}),
            ))
            await db.commit()

        # 确认目录前：v2.5.45 G06 同 TMDB 多目录归并先收口至 review (merge_directory)，合并覆盖 [1, 2]
        before_list = (await self.http.get("/api/tasks/t_boss/seasons")).json()
        boss_card = next(x for x in before_list if x["tmdb_id"] == "326975")
        self.assertEqual(boss_card["main_category"], "review")
        self.assertEqual(boss_card["review_action_kind"], "merge_directory")
        self.assertEqual(boss_card["airing_view"]["source_episodes"], [1, 2])

        # 确认目录后：无需全量重扫，自动回到 airing（连载留源）
        r_conf = await self.http.post(
            "/api/tasks/t_boss/directory-groups/grp_boss/confirm",
            json={"mode": "use_archive_target", "target_path": "/qa/dst/BOSS的甜蜜诱惑 (2026) [tmdbid=326975]"},
        )
        self.assertEqual(r_conf.status_code, 200, r_conf.text)
        self.assertEqual(r_conf.json()["summary"]["airing"], 1)

        after_list = (await self.http.get("/api/tasks/t_boss/seasons")).json()
        boss_after = next(x for x in after_list if x["tmdb_id"] == "326975")
        self.assertEqual(boss_after["main_category"], "airing")

        # 重试失败项：仅针对 rec_failed_one 发起远程核查，不重跑 boss_1 / boss_2
        rechecked_paths: list[str] = []

        async def _fake_run_inspection(task_id, source_path, **kwargs):
            rechecked_paths.append(kwargs.get("only_season_path") or source_path)
            return InspectionResult(
                task_id=task_id,
                source_path=source_path,
                seasons=[
                    SeasonDiagnosis(
                        show_name="超时剧 [tmdbid=399999]",
                        show_path="/qa/src/TimeoutShow",
                        season_path="/qa/src/TimeoutShow/Season 1",
                        tmdb_id=399999,
                        season_number=1,
                        total_episodes=2,
                        local_episode_count=2,
                        recognized_episodes=[1, 2],
                        season_finished=True,
                        status="archive_ready",
                        status_detail="全 2 集已齐",
                        evidence={"meta_status": "ok", "metadata_binding_verified": True},
                    )
                ],
            )

        with (
            patch("app.core.inspector.run_inspection", side_effect=_fake_run_inspection),
            patch("app.clients.clouddrive2.CloudDrive2Client", return_value=AsyncMock()),
            patch("app.clients.moviepilot.MoviePilotClient", return_value=AsyncMock()),
            patch("app.clients.tmdb.TMDBClient", return_value=AsyncMock()),
        ):
            r_retry = await self.http.post("/api/tasks/t_boss/retry-failed")

        self.assertEqual(r_retry.status_code, 200, r_retry.text)
        self.assertEqual(r_retry.json()["retried_ids"], ["rec_failed_one"])
        self.assertEqual(rechecked_paths, ["/qa/src/TimeoutShow/Season 1"])


if __name__ == "__main__":
    unittest.main()
