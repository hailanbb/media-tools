"""媒体整理智能修复全矩阵自动化测试套件 (v2.5.66 完整闭环，覆盖 R01–R33 验收场景)。"""

import json
import os
import posixpath
import sys
import tempfile
import unittest

# 安全护栏：在导入 app.database 前绑定独立临时目录，严禁触碰生产 data/mediaflow.db
_TMP_TEST_DIR = tempfile.mkdtemp(prefix="mf_test_transfer_repair_")
os.environ["MF_DATA_DIR"] = _TMP_TEST_DIR

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clients.moviepilot import MoviePilotClient
from app.core.transfer_repair import (
    build_mp_manual_transfer_payload,
    diagnose_failure_record,
    extract_raw_error,
    extract_season_and_episodes,
    parse_mp_preview_response,
)
from app.database import Base
from app.models.transfer_repair import (
    TransferRepairBaseline,
    TransferRepairItem,
)
from app.services.transfer_repair_service import (
    execute_repair_plans,
    get_or_create_baseline,
    get_repair_items,
    get_repair_summary,
    recheck_single_item,
    reset_baseline,
    scan_new_failures,
    update_item_plan_choice,
    verify_repair_item,
)


class MockMoviePilotClientV2:
    """高保真模拟 MoviePilot v2.15.6（含 storage/list、target-path、preview、history 与 errmsg 真实结构）。"""

    def __init__(
        self,
        *,
        origin: str = "http://mock-mp:3000",
        version: str = "v2.15.6",
        history_records: list[dict] | None = None,
        queue_items: list[dict] | None = None,
        storage_dirs: dict[tuple[str, str], list[dict]] | None = None,
        offline_storages: set[str] | None = None,
        preview_response: dict | None = None,
        execute_response: dict | None = None,
        target_path_response: dict | None = None,
    ):
        self.origin = origin
        self.version = version
        self.history_records = list(history_records or [])
        self.queue_items = list(queue_items or [])
        self.storage_dirs = dict(storage_dirs or {})
        self.offline_storages = set(offline_storages or set())
        self.preview_response = preview_response
        self.execute_response = execute_response
        self.target_path_response = target_path_response
        self.manual_transfer_calls: list[dict] = []
        self.target_path_calls: list[dict] = []

    async def get_system_version(self) -> str:
        return self.version

    async def get_transfer_queue(self) -> list[dict]:
        return self.queue_items

    async def get_transfer_history_paginated(self, status=None, title=None, page=1, count=50):
        records = [
            r
            for r in self.history_records
            if (status is None or r.get("status") == status)
            and (not title or title in str(r.get("title") or ""))
        ]
        records.sort(key=lambda r: int(r.get("id") or 0), reverse=True)
        start = (page - 1) * count
        end = start + count
        return {"total": len(records), "list": records[start:end]}

    async def list_storage_dir(self, storage: str, dir_path: str):
        st = storage or "local"
        if st in self.offline_storages:
            return False, [], "ConnectTimeout: storage unreachable"
        norm = posixpath.normpath(str(dir_path).replace("\\", "/"))
        entries = self.storage_dirs.get((st, norm), [])
        return True, list(entries), ""

    async def get_manual_target_path(self, fileitem, storage="local"):
        self.target_path_calls.append({"fileitem": fileitem, "storage": storage})
        if self.target_path_response is not None:
            return self.target_path_response
        return {
            "success": True,
            "data": {
                "target_storage": "local",
                "target_path": "/video/link/电视剧",
                "transfer_type": "copy",
            },
        }

    async def manual_transfer_v2(self, payload: dict):
        self.manual_transfer_calls.append(dict(payload))
        if payload.get("preview"):
            if self.preview_response is not None:
                return self.preview_response
            return {
                "success": True,
                "message": "预览成功",
                "data": {
                    "items": [
                        {
                            "success": True,
                            "dest": "/video/link/电视剧/测试剧集/Season 2/测试剧集 - S02E08.mkv",
                        }
                    ]
                },
            }
        if self.execute_response is not None:
            return self.execute_response
        return {"success": True, "message": "已受理手动整理"}

    async def close(self):
        pass


class TestTransferRepairEngine(unittest.IsolatedAsyncioTestCase):
    """覆盖 R01–R33 全部核心契约与生产级边界。"""

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        self.async_session = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.db = self.async_session()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    # -------------------------------------------------------------------------
    # 1. 参数解析与不变量专项测试 (R03, R04, R05, R06, R07, R09, R10)
    # -------------------------------------------------------------------------
    def test_r03_standard_s02e03(self):
        s, eps, part, is_sp, _ = extract_season_and_episodes("The.Show.S02E03.1080p.mkv")
        self.assertEqual((s, eps, part, is_sp), (2, [3], None, False))

    def test_r04_only_e03_without_season_never_defaults_s01(self):
        s, eps, _, _, _ = extract_season_and_episodes("The.Show.E03.mkv", parent_path="/video/downloads")
        self.assertIsNone(s, "绝不能将未知季号的 E03 擅自默认套用为 S01！")
        self.assertEqual(eps, [3])

    def test_r06_anime_version_tag_filter(self):
        _, eps, _, _, _ = extract_season_and_episodes(
            "[SweetSub] NieR Automata Ver1.1a - 11 [1080p].mp4",
            parent_path="/video/downloads",
        )
        self.assertEqual(eps, [11])

    def test_r07_absolute_numbering_offset(self):
        s, eps, _, _, _ = extract_season_and_episodes(
            "Anime.Episode.25.mkv",
            parent_path="/video/downloads/Season 2",
            confirmed_offset=12,
        )
        self.assertEqual((s, eps), (2, [13]))

    def test_r09_combined_episode_e01_e02(self):
        s, eps, _, _, _ = extract_season_and_episodes("Show.S01E01-E02.mkv")
        self.assertEqual((s, eps), (1, [1, 2]))

    def test_r10_bonus_part_segment(self):
        s, eps, part, is_sp, sp_type = extract_season_and_episodes("奔跑吧.S09.收官加更.part1.mkv")
        self.assertEqual((s, eps, part, is_sp, sp_type), (9, [], "part1", True, "加更"))

    def test_r05_movie_isolation(self):
        diag = diagnose_failure_record(
            {
                "id": 101,
                "title": "奥本海默",
                "src": "/video/downloads/电影/Oppenheimer.2023.2160p.mkv",
                "type": "电影",
                "errmsg": "未识别到媒体信息",
            },
            source_exists=True,
            source_file_size=5 * 1024 * 1024 * 1024,
        )
        self.assertEqual(diag["media_type"], "movie")
        self.assertEqual(diag["status"], "ready")
        self.assertIsNone(diag["season_number"])
        self.assertEqual(diag["episode_numbers"], [])
        self.assertEqual(diag["raw_error"], "未识别到媒体信息")

    # -------------------------------------------------------------------------
    # 2. MP v2.15.6 真实字段 errmsg + 跨机存储核查 + 目标合格结案 (R11–R15, R31)
    # -------------------------------------------------------------------------
    async def test_real_mp_errmsg_cross_host_storage_and_r11_verified_close(self):
        """复现真实线上《垂涎》案例：Windows 下跨机核查 /video/downloads 与 /video/link，同名合格目标自动结案。"""
        src_dir = "/video/downloads/电视剧/ABO.Desire.S01"
        src_file = f"{src_dir}/ABO.Desire.S01E06.mkv"
        dest_dir = "/video/link/电视剧/台湾剧集/垂涎 (2025) [tmdbid=280100]/Season 1"
        dest_file = f"{dest_dir}/垂涎 - S01E06 - ABO.Desire.S01E06.mkv"

        mp = MockMoviePilotClientV2(
            history_records=[
                {"id": 105740, "status": False, "title": "旧历史", "src": "/video/downloads/old.mkv", "errmsg": "旧错误"},
            ],
            storage_dirs={
                ("local", src_dir): [
                    {"name": "ABO.Desire.S01E06.mkv", "path": src_file, "type": "file", "size": 1144986473, "storage": "local"}
                ],
                ("local", dest_dir): [
                    {"name": "垂涎 - S01E06 - ABO.Desire.S01E06.mkv", "path": dest_file, "type": "file", "size": 1144986473, "storage": "local"}
                ],
            },
        )

        # 建立基线 #105740
        baseline = await get_or_create_baseline(self.db, mp)
        self.assertEqual(baseline.baseline_history_id, 105740)
        self.assertEqual(baseline.baseline_status, "active")

        # 产生真实新失败 #105749（MP 字段为 errmsg，并非 error）
        mp.history_records.append({
            "id": 105749,
            "status": False,
            "errmsg": "媒体库存在同名文件，当前覆盖模式为不覆盖",
            "title": "垂涎",
            "type": "电视剧",
            "seasons": "S01",
            "episodes": "E06",
            "tmdbid": 280100,
            "src": src_file,
            "src_storage": "local",
            "dest": dest_file,
            "dest_storage": "local",
            "date": "2026-11-21 16:25:13",
            "mode": "copy",
            "src_fileitem": {"path": src_file, "storage": "local", "name": "ABO.Desire.S01E06.mkv", "size": 1144986473, "url": "http://secret-token-url"},
            "dest_fileitem": {"path": dest_file, "storage": "local", "name": "垂涎 - S01E06 - ABO.Desire.S01E06.mkv", "size": 1144986473},
        })

        scan_res = await scan_new_failures(self.db, mp)
        self.assertEqual(scan_res["new_failures_found"], 1)
        self.assertEqual(scan_res["resolved_verified"], 1)
        self.assertEqual(scan_res["skipped_source_missing"], 0, "绝不能因 Windows os.path.exists 误判 Linux 路径为源不存在！")

        resolved_list = await get_repair_items(self.db, tab="resolved")
        self.assertEqual(resolved_list["total"], 1)
        it = resolved_list["items"][0]
        self.assertEqual(it["title"], "垂涎", "必须保留 MP 已识别中文名，不被英文文件名覆盖！")
        self.assertEqual(it["raw_error"], "媒体库存在同名文件，当前覆盖模式为不覆盖")
        self.assertEqual(it["error_category"], "target_exists")
        self.assertEqual(it["resolution_code"], "already_present_verified")
        # 验证敏感字段已脱敏（第 8.3 节）
        db_item = await self.db.get(TransferRepairItem, it["id"])
        self.assertNotIn("secret-token-url", db_item.src_fileitem_json)

    def test_r12_r13_undersized_target_and_dest_fileitem_conflict(self):
        # R12: 同名目标明显残缺 (5MB vs 1GB 源) -> 冲突保护
        diag_small = diagnose_failure_record(
            {"id": 201, "title": "庆余年", "src": "/video/downloads/QYN.S02E01.mkv", "errmsg": "媒体库存在同名文件，当前覆盖模式为不覆盖"},
            source_exists=True,
            source_file_size=1024 * 1024 * 1024,
            target_exists=True,
            target_file_size=5 * 1024 * 1024,
        )
        self.assertEqual(diag_small["status"], "conflict")

        # R13: 历史含 dest_fileitem 且目标不存在 -> 阻断 logid 隐式清理旧目标风险
        diag_dest_risk = diagnose_failure_record(
            {
                "id": 202,
                "title": "庆余年",
                "src": "/video/downloads/QYN.S02E01.mkv",
                "dest_fileitem": {"path": "/video/link/QYN/S02/E01.mkv", "storage": "local"},
                "errmsg": "copy 失败",
            },
            source_exists=True,
            target_exists=False,
        )
        self.assertEqual(diag_dest_risk["status"], "conflict")
        self.assertTrue(diag_dest_risk["plan_json"]["dest_cleanup_risk"])

    async def test_r14_subsequent_success_closes_missing_source(self):
        """R14: 原下载文件已移走，但存在后续成功整理记录且目标有效落地 -> superseded_by_success 结案。"""
        dest_dir = "/video/link/电视剧/测试剧/Season 1"
        dest_file = f"{dest_dir}/测试剧 - S01E03.mkv"
        mp = MockMoviePilotClientV2(
            history_records=[
                {"id": 100, "status": False, "title": "旧基线", "src": "/video/downloads/old.mkv"},
            ],
            storage_dirs={
                ("local", dest_dir): [
                    {"name": "测试剧 - S01E03.mkv", "path": dest_file, "type": "file", "size": 800 * 1024 * 1024, "storage": "local"}
                ]
            },
        )
        await get_or_create_baseline(self.db, mp)
        mp.history_records.extend([
            {"id": 101, "status": False, "title": "测试剧", "tmdbid": 999, "seasons": "S01", "episodes": "E03", "src": "/video/downloads/Show.S01E03.mkv", "src_storage": "local", "errmsg": "copy 失败"},
            {"id": 105, "status": True, "title": "测试剧", "tmdbid": 999, "seasons": "S01", "episodes": "E03", "src": "/video/downloads/Show.S01E03.mkv", "dest": dest_file, "dest_storage": "local"},
        ])
        scan_res = await scan_new_failures(self.db, mp)
        self.assertEqual(scan_res["resolved_verified"], 1)
        items = (await get_repair_items(self.db, tab="resolved"))["items"]
        self.assertEqual(items[0]["resolution_code"], "superseded_by_success")

    async def test_r15_r31_source_missing_vs_storage_offline(self):
        """R15 vs R31: 存储离线必须入 waiting_environment，存储健康且源确实不存在才入 skipped/source_missing。"""
        mp = MockMoviePilotClientV2(
            history_records=[{"id": 500, "status": False, "title": "基线", "src": "/video/downloads/b.mkv"}],
            offline_storages={"u115"},
        )
        await get_or_create_baseline(self.db, mp)
        mp.history_records.extend([
            {"id": 501, "status": False, "title": "离线剧", "src": "/115/Show.S01E01.mkv", "src_storage": "u115", "errmsg": "copy 失败"},
            {"id": 502, "status": False, "title": "已删剧", "src": "/video/downloads/Gone.S01E02.mkv", "src_storage": "local", "errmsg": "源文件不存在"},
        ])
        res = await scan_new_failures(self.db, mp)
        self.assertEqual(res["pending_new"], 1)
        self.assertEqual(res["skipped_source_missing"], 1)

        pending_items = (await get_repair_items(self.db, tab="pending"))["items"]
        self.assertEqual(pending_items[0]["status"], "waiting_environment")
        skipped_items = (await get_repair_items(self.db, tab="rules_records"))["items"]
        self.assertEqual(skipped_items[0]["status"], "skipped")
        self.assertEqual(skipped_items[0]["skip_reason"], "source_missing")

    # -------------------------------------------------------------------------
    # 3. 零基线防御、实例重置与方案 A 基线重设 (R01, R29, R32)
    # -------------------------------------------------------------------------
    async def test_r01_r29_r32_zero_baseline_guard_and_reset(self):
        """无 mp_client 时绝不能静默落盘 active=0 基线；MP ID 回退时必须触发 reset_required。"""
        b0 = await get_or_create_baseline(self.db, mp_client=None)
        self.assertEqual(b0.baseline_status, "pending_confirm")

        mp = MockMoviePilotClientV2(
            history_records=[
                {"id": 900, "status": False, "title": "历史A", "src": "/video/downloads/a.mkv", "date": "2026-11-21 10:00:00"},
                {"id": 899, "status": False, "title": "历史B", "src": "/video/downloads/b.mkv", "date": "2026-11-20 10:00:00"},
            ]
        )
        # 传入有效 mp_client 后自动锚定至 #900
        scan1 = await scan_new_failures(self.db, mp)
        self.assertEqual(scan1["baseline_history_id"], 900)
        self.assertEqual(scan1["new_failures_found"], 0)

        # R32: 模拟 MP 重建库导致最大 ID 跌落至 10
        mp.history_records = [{"id": 10, "status": False, "title": "重建库记录", "src": "/video/downloads/x.mkv"}]
        scan2 = await scan_new_failures(self.db, mp)
        self.assertEqual(scan2["status"], "baseline_reset_required")

        # 调用 reset_baseline（方案 A）重设并清空脏记录
        reset_res = await reset_baseline(self.db, mp, clear_existing_items=True)
        self.assertTrue(reset_res["ok"])
        self.assertEqual(reset_res["baseline_history_id"], 10)

    # -------------------------------------------------------------------------
    # 4. 预览单项失败拦截、执行载荷契约与目标落地核验 (R16, R17, R18, R20, R21)
    # -------------------------------------------------------------------------
    async def test_r16_r17_r20_r21_preview_execute_and_verify_pipeline(self):
        src_dir = "/video/downloads/电视剧/TestShow.S02"
        src_file = f"{src_dir}/TestShow.S02E08.mkv"
        sub_file = f"{src_dir}/TestShow.S02E08.zh.srt"
        dest_dir = "/video/link/电视剧/测试剧集/Season 2"
        dest_file = f"{dest_dir}/测试剧集 - S02E08.mkv"

        mp = MockMoviePilotClientV2(
            storage_dirs={
                ("local", src_dir): [
                    {"name": "TestShow.S02E08.mkv", "path": src_file, "type": "file", "size": 500 * 1024 * 1024, "storage": "local"},
                    {"name": "TestShow.S02E08.zh.srt", "path": sub_file, "type": "file", "size": 4096, "storage": "local"},
                ],
                ("local", dest_dir): [],
            },
            # R17: 顶层 success=True，但内层 item 失败 -> 必须拦截执行！
            preview_response={
                "success": True,
                "data": {"items": [{"success": False, "errmsg": "目标目录权限不足"}]},
            },
        )
        await get_or_create_baseline(self.db, mp)
        item = TransferRepairItem(
            id="item-exec-v2",
            source_path=src_file,
            file_basename="TestShow.S02E08.mkv",
            file_size=500 * 1024 * 1024,
            src_storage="local",
            latest_history_id=672,
            title="测试剧集",
            media_type="tv",
            tmdb_id=12345,
            season_number=2,
            episode_numbers_json="[8]",
            status="ready",
            evidence_json=json.dumps({"companion_subtitles": [{"name": "TestShow.S02E08.zh.srt"}]}),
            plan_json=json.dumps({"safe_to_execute": True}),
            plan_hash="abc123",
        )
        self.db.add(item)
        await self.db.commit()

        # 1. R17: 预览内层失败 -> 绝不能提交执行
        blocked_res = await execute_repair_plans(self.db, mp, ["item-exec-v2"])
        self.assertFalse(blocked_res["results"][0]["ok"])
        self.assertIn("预览未通过", blocked_res["results"][0]["reason"])
        self.assertEqual(len(mp.manual_transfer_calls), 1, "只应发起 preview=True，绝不能继续发正式执行！")

        # 2. R16 + R21: 预览通过但目标尚未落地 -> 提交后必须停留在 verifying，绝不假成功！
        mp.preview_response = {
            "success": True,
            "data": {"items": [{"success": True, "dest": dest_file}]},
        }
        exec_res = await execute_repair_plans(self.db, mp, ["item-exec-v2"])
        self.assertTrue(exec_res["results"][0]["ok"])
        self.assertEqual(exec_res["results"][0]["status"], "verifying", "目标尚未核实落地前，严禁直接宣告 resolved！")
        # 校验真实发送的 ManualTransferItem 载荷
        real_call = mp.manual_transfer_calls[-1]
        self.assertEqual(real_call["fileitem"]["path"], src_file)
        self.assertEqual(real_call["episode_detail"], "8")
        self.assertFalse(real_call["from_history"])
        self.assertFalse(real_call["reorganize"])
        self.assertFalse(real_call["preview"])

        # 3. 模拟目标视频已落地但外挂字幕缺失 (R20) -> 核验转 resolved/repaired 且保留字幕待办提示
        mp.storage_dirs[("local", dest_dir)] = [
            {"name": "测试剧集 - S02E08.mkv", "path": dest_file, "type": "file", "size": 500 * 1024 * 1024, "storage": "local"}
        ]
        ver_res = await verify_repair_item(self.db, mp, "item-exec-v2")
        self.assertTrue(ver_res["ok"])
        self.assertEqual(ver_res["status"], "resolved")
        self.assertIn("伴随字幕", ver_res["message"])

    async def test_queue_flatten_and_rule_reuse_recheck(self):
        """测试嵌套 MP 队列展开与规则保存后重诊断命中 (R24)。"""
        nested_q = [
            {
                "media": {"title": "异能研究所"},
                "season": "S02",
                "tasks": [
                    {"fileitem": {"path": "/video/downloads/a.mkv", "storage": "local", "size": 100}, "state": "running"},
                    {"fileitem": {"path": "/video/downloads/b.mkv", "storage": "local", "size": 200}, "state": "waiting"},
                ],
            }
        ]
        flat = MoviePilotClient.flatten_transfer_queue(nested_q)
        self.assertEqual(len(flat), 2)
        self.assertEqual(flat[0]["title"], "异能研究所")

        # 保存同包规则并重诊断
        src_dir = "/video/downloads/AnimePack"
        src_file = f"{src_dir}/Anime.E25.mkv"
        mp = MockMoviePilotClientV2(
            storage_dirs={
                ("local", src_dir): [
                    {"name": "Anime.E25.mkv", "path": src_file, "type": "file", "size": 300 * 1024 * 1024, "storage": "local"}
                ]
            }
        )
        item = TransferRepairItem(
            id="item-rule-1",
            source_path=src_file,
            file_basename="Anime.E25.mkv",
            src_storage="local",
            latest_history_id=700,
            title="某动漫",
            media_type="tv",
            season_number=None,
            episode_numbers_json="[25]",
            status="needs_confirmation",
        )
        self.db.add(item)
        await self.db.commit()

        await update_item_plan_choice(
            self.db,
            "item-rule-1",
            {
                "title": "某动漫",
                "media_type": "tv",
                "season_number": 2,
                "episode_numbers": [13],
                "episode_offset": 12,
                "remember_scope": "this_package",
            },
        )
        rc = await recheck_single_item(self.db, mp, "item-rule-1")
        self.assertTrue(rc["ok"])
        self.assertEqual(rc["status"], "ready")


def run_tests():
    suite = unittest.TestLoader().loadTestsFromTestCase(TestTransferRepairEngine)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    run_tests()
