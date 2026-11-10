"""MediaFlow 业务领域核心不变量与用户心智回归测试套件.

专门覆盖并永久固化近期用户反馈的核心痛点与边界条件：
1. 【缺集属实优先进待补缺】：已完结剧集只要有缺集，即使有同集多画质版本，必须优先进入 download_needed；
2. 【全集齐备方可进待清理】：只有当全剧集数无缺失且存在重复副本时，才进入 review_needed；
3. 【连载热播剧绝不误判拆分冲突】：连载剧集即使已播与总集数相差甚远，绝对严禁判定为 split_conflict；
4. 【混版全集保全第一定律】：混版剧集（多压制组拼凑）清理冗余时，每个已有集数必须 100% 保留一份最佳版本，绝不误伤任何孤本集；
5. 【人性化集数文案无歧义】：绝对杜绝“缺 14 集”这类容易被理解为数量的歧义文案，必须输出“缺失第 14 集”；
6. 【清理后状态机流转闭环】：从包含重复的缺集状态，清理后自愈为干净缺集状态，集齐后直达可归档状态。
"""

import os
import sys
import unittest
from collections import Counter

sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding='utf-8')

from app.core.inspector import diagnose_season, parse_episode
from app.core.review_engine import (
    generate_review_clean_plan,
    format_missing_episodes_text,
)


class TestDomainInvariants(unittest.TestCase):
    """业务核心不变量回归测试."""

    def test_01_invariant_missing_takes_precedence_over_review(self):
        """【不变量 1】缺集属实优先进入【真缺集待补】清单.
        
        用户痛点复现：香港探秘地图 (2026) S01 共 20 集，已有 19 集（缺 E14），且 E15 有两个版本。
        要求：绝对不能因为 E15 重复而扣留在 review_needed，必须优先归入 download_needed！
        """
        status, detail = diagnose_season(
            recognized=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19, 20],
            total_episodes=20,
            season_finished=True,
            has_duplicates=True,
            duplicate_episodes=[15],
        )
        # 核心断言：优先归入待补缺
        self.assertEqual(status, "download_needed", "已完结缺集剧集必须优先归入 download_needed，不能被 review_needed 遮蔽！")
        self.assertIn("缺失", detail)
        self.assertIn("14", detail)
        self.assertIn("重复", detail)
        self.assertIn("15", detail)

    def test_02_invariant_review_only_when_fully_intact_or_airing(self):
        """【不变量 2】只有无缺集全齐或连载中，才进入【处需清理】.
        
        场景 A：已完结剧集 12 集已全部集齐，但 E01 有 2 个版本 -> review_needed
        场景 B：连载中剧集播到第 5 集，但 E01 有 2 个版本 -> review_needed
        """
        # 场景 A
        status_a, detail_a = diagnose_season(
            recognized=list(range(1, 13)),
            total_episodes=12,
            season_finished=True,
            has_duplicates=True,
            duplicate_episodes=[1],
        )
        self.assertEqual(status_a, "review_needed")
        self.assertIn("已齐", detail_a)
        self.assertIn("重复", detail_a)

        # 场景 B
        status_b, detail_b = diagnose_season(
            recognized=[1, 2, 3, 4, 5],
            total_episodes=16,
            season_finished=False,
            has_duplicates=True,
            duplicate_episodes=[1],
        )
        self.assertEqual(status_b, "review_needed")
        self.assertIn("连载中存在重复版本", detail_b)

    def test_03_invariant_airing_show_never_split_conflict(self):
        """【不变量 3】连载热播剧绝不误判为拆分冲突.
        
        用户痛点复现：不着边际的你 (2026) S01 共 16 集，当前播至第 5 集（差 11 集 >= 5）。
        要求：连载剧集集数差是更新规律常态，绝不能被启发式规则误标为 split_conflict 踢入异常剧集！
        """
        status, detail = diagnose_season(
            recognized=[1, 2, 3, 4, 5],
            total_episodes=16,
            season_finished=False,
            has_duplicates=False,
        )
        self.assertEqual(status, "airing")
        self.assertIn("尚未确认本季播毕", detail)
        self.assertNotIn("拆分", detail)
        self.assertNotIn("失真", detail)

    def test_04_invariant_mixed_groups_zero_loss_guarantee(self):
        """【不变量 4】混版场景全集保全第一定律（保全孤本集 > 画质整理）.
        
        用户痛点复现：剧集由多压制组散装拼凑（混版），缺 E14，且 E15 有两份画质。
        要求：
        1. 每一个已有的孤本集数（E01~E13, E16~E20，哪怕来自零散不同组）100% 坚决保全入 to_keep！
        2. 仅淘汰同集重复的较低画质版本（E15 1080p）；
        3. can_clean 必须为 True，绝不因缺集产生“暂缓清理”死锁！
        """
        mixed_files = [
            {"name": f"Show.S01E{i:02d}.GroupA.2160p.mkv", "path": f"/p/E{i:02d}.mkv", "size": 1500000000, "isDirectory": False}
            for i in range(1, 14)
        ]
        # E15 混版双版本
        mixed_files.append({"name": "Show.S01E15.GroupB.1080p.mkv", "path": "/p/E15_1080.mkv", "size": 400000000, "isDirectory": False})
        mixed_files.append({"name": "Show.S01E15.GroupC.2160p.mkv", "path": "/p/E15_4k.mkv", "size": 2500000000, "isDirectory": False})
        # E16~E20 来自不同组
        for i in range(16, 21):
            mixed_files.append({"name": f"Show.S01E{i:02d}.GroupD.2160p.mkv", "path": f"/p/E{i:02d}.mkv", "size": 1600000000, "isDirectory": False})

        # 总集数 20，已有 19 集，缺 E14
        plan = generate_review_clean_plan(mixed_files, season_number=1, total_episodes=20, force_clean=False)

        # 1. 允许清理同集多余版本
        self.assertTrue(plan["can_clean"], "包含缺失集的混版剧集只要存在同集重复，必须允许执行冗余清理！")
        # 2. 保留集数必须恰好为 19 项（E01~E13, E15, E16~E20 各 1 份最优）
        self.assertEqual(len(plan["to_keep"]), 19)
        kept_paths = [f["path"] for f in plan["to_keep"]]
        # 孤本集数严密核验
        for i in list(range(1, 14)) + list(range(16, 21)):
            self.assertIn(f"/p/E{i:02d}.mkv", kept_paths, f"孤本第 {i} 集绝对不允许被误删！")
        # E15 优选 4K
        self.assertIn("/p/E15_4k.mkv", kept_paths)

        # 3. 待删列表必须且仅有 E15 1080p
        self.assertEqual(len(plan["to_delete"]), 1)
        self.assertEqual(plan["to_delete"][0]["path"], "/p/E15_1080.mkv")

    def test_05_invariant_humanized_wording_no_ambiguity(self):
        """【不变量 5】人性化集数文案绝无数量歧义.
        
        用户痛点复现：“缺 14 集”被误读为“缺少 14 集数量”。
        要求：格式化输出必须带“第”，清晰无歧义。
        """
        # 单集缺失
        w1 = format_missing_episodes_text([14])
        self.assertEqual(w1, "缺失第 14 集")
        self.assertNotIn("缺 14 集", w1)

        # 少量多集缺失
        w2 = format_missing_episodes_text([11, 12])
        self.assertEqual(w2, "缺失第 11、12 集 (共 2 集)")

        # 大量缺失
        w3 = format_missing_episodes_text([1, 2, 3, 4, 5, 6, 7, 8])
        self.assertIn("共 8 集", w3)
        self.assertIn("缺失第 1、2、3 等", w3)

    def test_06_invariant_state_machine_self_healing_transition(self):
        """【不变量 6】状态机自愈流转闭环.
        
        模拟端到端生命周期：
        1. 初始状态：19/20 集，E15 重复，E14 缺失 -> 诊断为 download_needed；
        2. 用户执行清理：E15 1080p 被删，底层文件重新诊断 -> 状态保持 download_needed，无重复标记；
        3. 模拟补全第 14 集 -> 底层文件再次重诊 -> 100% 自动跃迁至 archive_ready！
        """
        # 步骤 1：初检诊断
        s1, d1 = diagnose_season(
            recognized=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19, 20],
            total_episodes=20,
            season_finished=True,
            has_duplicates=True,
            duplicate_episodes=[15],
        )
        self.assertEqual(s1, "download_needed")
        self.assertIn("同时存在重复集", d1)

        # 步骤 2：清理重复后重诊
        s2, d2 = diagnose_season(
            recognized=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19, 20],
            total_episodes=20,
            season_finished=True,
            has_duplicates=False,  # 重复已消解
            duplicate_episodes=[],
        )
        self.assertEqual(s2, "download_needed")
        self.assertNotIn("重复", d2)
        self.assertIn("14", d2)

        # 步骤 3：补齐缺失第 14 集后重诊
        s3, d3 = diagnose_season(
            recognized=list(range(1, 21)),  # 1~20 全齐
            total_episodes=20,
            season_finished=True,
            has_duplicates=False,
            duplicate_episodes=[],
        )
        self.assertEqual(s3, "archive_ready")
        self.assertIn("全 20 集齐全，可归档", d3)


if __name__ == "__main__":
    unittest.main()
