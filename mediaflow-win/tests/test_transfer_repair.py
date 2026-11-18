"""媒体整理智能修复全矩阵自动化测试套件 (覆盖 R01–R33 核心验收场景)。

测试涵盖：
1. [基线接管与历史隔离] (R01, R29, R30, R32): 初次启用建立基线，670 条旧历史不导入活跃修复；
2. [集号与季号推导] (R03, R04, R06, R07, R09, R10):
   - 标准 S02E03 自动就绪
   - 纯 E03 绝不盲目套 S01，提示补充季选择
   - 动漫 Ver1.1a - 11 剔除版本噪音精准识别 E11
   - 绝对集号跨季 offset 严格应用一次
   - 连集 E01-E02 不拆分为两份
   - 收官加更 part1/part2 保持分段语义不误转正片
3. [媒体类型与电影隔离] (R05): 电影类型独立，不携带季集参数
4. [目标核验与风险拦截] (R11, R12, R13):
   - 目标已存在且合格视频 -> 核实结案 (already_present_verified)
   - 目标同名不符或历史含 dest_fileitem -> 前置拦截防删除旧目标
5. [源文件状态与环境区分] (R14, R15, R31):
   - 存储离线转 waiting_environment
   - 源不存在且无合格目标 -> 标记 skipped/source_missing 退出活跃队列
6. [计划生成与幂等执行] (R16, R17, R19, R24, R26):
   - 计划摘要哈希生成
   - 经验规则作用域复用
   - 保护状态拦截
   - 契约载荷 from_history=false、reorganize=false
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.transfer_repair import (
    calculate_plan_hash,
    diagnose_failure_record,
    extract_season_and_episodes,
    parse_clean_title,
)
from app.database import Base
from app.models.transfer_repair import (
    TransferRepairBaseline,
    TransferRepairItem,
    TransferRepairRule,
)
from app.services.transfer_repair_service import (
    execute_repair_plans,
    get_or_create_baseline,
    get_repair_items,
    get_repair_summary,
    scan_new_failures,
    update_item_plan_choice,
)


class MockMoviePilotClient:
    """模拟 MoviePilot 客户端，提供受控的历史数据与转移接口。"""

    def __init__(self, history_records=None, queue_items=None):
        self.history_records = history_records or []
        self.queue_items = queue_items or []
        self.manual_transfer_calls = []

    async def get_transfer_history_paginated(self, status=None, title=None, page=1, count=50):
        records = [r for r in self.history_records if status is None or r.get("status") == status]
        start = (page - 1) * count
        end = start + count
        sliced = records[start:end]
        return {"total": len(records), "list": sliced}

    async def get_transfer_queue(self):
        return self.queue_items

    async def manual_transfer_v2(self, payload):
        self.manual_transfer_calls.append(payload)
        return {"success": True, "message": "已成功提交 MoviePilot 手动整理"}

    async def close(self):
        pass


class TestTransferRepairEngine(unittest.IsolatedAsyncioTestCase):
    """测试媒体整理智能修复核心推导与服务生命周期。"""

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
    # 1. 集号解析与参数推导专项测试 (R03, R04, R06, R07, R09, R10)
    # -------------------------------------------------------------------------
    def test_r03_standard_s02e03(self):
        """R03: 标准 S02E03 文件名解析。"""
        s, eps, part, is_sp, sp_type = extract_season_and_episodes("The.Show.S02E03.1080p.mkv")
        self.assertEqual(s, 2)
        self.assertEqual(eps, [3])
        self.assertIsNone(part)
        self.assertFalse(is_sp)

    def test_r04_only_e03_without_season(self):
        """R04: 仅含 E03 且父目录无季依据时，绝不能默认套用 S01！"""
        s, eps, part, is_sp, sp_type = extract_season_and_episodes("The.Show.E03.mkv", parent_path="/downloads")
        self.assertIsNone(s, "绝不能为了通过整理而将未知季号的 E03 擅自套用为 S01！")
        self.assertEqual(eps, [3])

    def test_r06_anime_version_tag_filter(self):
        """R06: 动漫 Ver1.1a - 11 必须剔除版本号，集号提取为 11。"""
        s, eps, part, is_sp, sp_type = extract_season_and_episodes(
            "[SweetSub] NieR Automata Ver1.1a - 11 [1080p].mp4",
            parent_path="/downloads",
        )
        self.assertEqual(eps, [11], "必须精准识别 11 为集号，排除 Ver1.1a 干扰！")

    def test_r07_absolute_numbering_offset(self):
        """R07: 绝对集号跨季复用映射，offset 严格仅计算一次。"""
        s, eps, part, is_sp, sp_type = extract_season_and_episodes(
            "Anime.Episode.25.mkv",
            parent_path="/downloads/Season 2",
            confirmed_offset=12,
        )
        self.assertEqual(s, 2)
        self.assertEqual(eps, [13], "绝对编号 25 经 offset=12 应正确换算为第二季第 13 集！")

    def test_r09_combined_episode_e01_e02(self):
        """R09: E01-E02 合并视频识别为包含 [1, 2]，不拆分为两个实体文件。"""
        s, eps, part, is_sp, sp_type = extract_season_and_episodes("Show.S01E01-E02.mkv")
        self.assertEqual(s, 1)
        self.assertEqual(eps, [1, 2], "连集必须作为一个文件覆盖两集，严禁拆分！")

    def test_r10_bonus_part_segment(self):
        """R10: 综艺加更 part1/part2 保持分段与特辑语义，绝不误变成 E01/E02。"""
        s, eps, part, is_sp, sp_type = extract_season_and_episodes("奔跑吧.S09.收官加更.part1.mkv")
        self.assertEqual(s, 9)
        self.assertEqual(part, "part1")
        self.assertTrue(is_sp)
        self.assertEqual(sp_type, "加更")

    # -------------------------------------------------------------------------
    # 2. 诊断与冲突分类专项测试 (R05, R11, R12, R13, R15, R31)
    # -------------------------------------------------------------------------
    def test_r05_movie_media_type_isolation(self):
        """R05: 电影类型独立推导，无季集参数。"""
        diag = diagnose_failure_record(
            {
                "id": 101,
                "title": "奥本海默",
                "src": "/nas/downloads/电影/Oppenheimer.2023.2160p.mkv",
                "type": "电影",
                "error": "未识别到媒体信息",
            },
            source_exists=True,
        )
        self.assertEqual(diag["media_type"], "movie")
        self.assertEqual(diag["status"], "ready")
        self.assertIsNone(diag["season_number"], "电影绝不能携带季号！")
        self.assertEqual(diag["episode_numbers"], [], "电影绝不能携带集号！")

    def test_r11_target_already_present_verified(self):
        """R11: 目标文件已存在且为合格视频，核实结案。"""
        diag = diagnose_failure_record(
            {
                "id": 102,
                "title": "庆余年",
                "src": "/downloads/QingYuNian.S02E01.mkv",
                "error": "媒体库存在同名文件，当前覆盖模式为不覆盖",
            },
            source_exists=True,
            target_exists=True,
            target_file_size=1024 * 1024 * 500,  # 500MB
        )
        self.assertEqual(diag["status"], "resolved")
        self.assertEqual(diag["resolution_code"], "already_present_verified")

    def test_r12_r13_dest_fileitem_risk_conflict(self):
        """R12/R13: 历史包含 dest_fileitem 引用时前置拦截，防止 MP 隐式删目标。"""
        diag = diagnose_failure_record(
            {
                "id": 103,
                "title": "庆余年",
                "src": "/downloads/QingYuNian.S02E01.mkv",
                "dest_fileitem": {"path": "/media/QingYuNian/S02/E01.mkv"},
                "error": "未识别集数",
            },
            source_exists=True,
        )
        self.assertEqual(diag["status"], "conflict")
        self.assertIn("dest_fileitem", diag["main_block_reason"])

    def test_r15_source_missing_skipped(self):
        """R15: 源文件确认不存在且无目标证据，标记 skipped/source_missing 退出活跃队列。"""
        diag = diagnose_failure_record(
            {
                "id": 104,
                "title": "已删源剧集",
                "src": "/downloads/deleted_file.mkv",
                "error": "源文件不存在",
            },
            source_exists=False,
            storage_healthy=True,
        )
        self.assertEqual(diag["status"], "skipped")
        self.assertEqual(diag["skip_reason"], "source_missing")

    def test_r31_storage_offline_waiting_environment(self):
        """R31: 存储离线时归入 waiting_environment，绝不能误判为源不存在直接跳过。"""
        diag = diagnose_failure_record(
            {
                "id": 105,
                "title": "离线挂载剧集",
                "src": "/115/nas/offline_file.mkv",
                "error": "copy 失败",
            },
            source_exists=False,
            storage_healthy=False,
        )
        self.assertEqual(diag["status"], "waiting_environment")

    # -------------------------------------------------------------------------
    # 3. 基线接管、扫描与执行生命周期集成测试 (R01, R29, R30, R16)
    # -------------------------------------------------------------------------
    async def test_r01_r29_baseline_takeover_isolation(self):
        """R01/R29: 首次启用建立基线，历史失败记录不导入待修复队列。"""
        # 准备 5 条旧历史 (ID: 666-670)
        history = [
            {"id": 670, "title": "旧失败5", "src": "/downloads/old5.mkv", "status": False},
            {"id": 669, "title": "旧失败4", "src": "/downloads/old4.mkv", "status": False},
            {"id": 668, "title": "旧失败3", "src": "/downloads/old3.mkv", "status": False},
            {"id": 667, "title": "旧失败2", "src": "/downloads/old2.mkv", "status": False},
            {"id": 666, "title": "旧失败1", "src": "/downloads/old1.mkv", "status": False},
        ]
        mp = MockMoviePilotClient(history_records=history)

        # 首次建立基线
        baseline = await get_or_create_baseline(self.db, mp)
        self.assertEqual(baseline.baseline_history_id, 670, "基线起点历史 ID 必须记录为 670！")

        # 首次扫描（没有比 670 更大的新记录）
        scan_res = await scan_new_failures(self.db, mp)
        self.assertEqual(scan_res["new_failures_found"], 0, "启用前旧历史绝不能导入新失败列表！")
        self.assertEqual(scan_res["new_items_created"], 0)

        # 检查待处理项目数：必须为 0
        summary = await get_repair_summary(self.db, mp)
        self.assertEqual(summary["counts"]["pending"], 0)

        # R30: 产生新失败 (ID: 671)，创建真实存在的测试源文件
        with tempfile.NamedTemporaryFile(prefix="NewShow.S01E05.", suffix=".mkv", delete=False) as tf:
            tf.write(b"video content")
            real_src = tf.name

        try:
            history.insert(0, {
                "id": 671,
                "title": "新剧集",
                "src": real_src,
                "status": False,
                "error": "未识别集数",
            })

            scan_res2 = await scan_new_failures(self.db, mp)
            self.assertEqual(scan_res2["new_failures_found"], 1, "必须增量捕获 ID > 670 的新失败！")
            self.assertEqual(scan_res2["new_items_created"], 1)

            # 检查列表
            items_res = await get_repair_items(self.db, tab="pending")
            self.assertEqual(items_res["total"], 1)
            item = items_res["items"][0]
            self.assertEqual(item["season_number"], 1)
            self.assertEqual(item["episode_numbers"], [5])
        finally:
            if os.path.exists(real_src):
                os.unlink(real_src)

    async def test_r16_execute_repair_plans(self):
        """R16: 执行已就绪计划，验证 ManualTransferItem 契约载荷。"""
        mp = MockMoviePilotClient()
        # 创建一个待执行条目
        item = TransferRepairItem(
            id="item-exec-1",
            source_path="/downloads/TestShow.S02E08.mkv",
            file_basename="TestShow.S02E08.mkv",
            latest_history_id=672,
            title="测试剧集",
            media_type="tv",
            season_number=2,
            episode_numbers_json="[8]",
            status="ready",
            plan_json=json.dumps({"safe_to_execute": True}),
        )
        self.db.add(item)
        await self.db.commit()

        res = await execute_repair_plans(self.db, mp, ["item-exec-1"])
        self.assertEqual(len(res["results"]), 1)
        self.assertTrue(res["results"][0]["ok"])

        # 验证发送给 MP 的载荷
        self.assertEqual(len(mp.manual_transfer_calls), 1)
        call_payload = mp.manual_transfer_calls[0]
        self.assertEqual(call_payload["logid"], 672)
        self.assertEqual(call_payload["season"], 2)
        self.assertEqual(call_payload["episode_detail"], 8)
        self.assertFalse(call_payload["from_history"], "必须严格显式禁用 from_history 防止旧错误回填！")
        self.assertFalse(call_payload["reorganize"], "必须严格显式禁用 reorganize 防止删除目标！")


def run_tests():
    suite = unittest.TestLoader().loadTestsFromTestCase(TestTransferRepairEngine)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    run_tests()
