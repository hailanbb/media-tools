"""阶段三领域算法专项测试 (F07, F08, P0-2)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from app.core.review_engine import generate_review_clean_plan, episodes_from_name

class TestPhase3Domain(unittest.TestCase):
    def test_f07_multiepisode_range_recognition(self):
        """F07: 验证合集范围集（如 S01E01-E02）多集提取正确，不被误判为残卷。"""
        eps = episodes_from_name("MyShow.S01E01-E02.1080p.mkv", 1)
        self.assertEqual(eps, [1, 2])

        eps_cn = episodes_from_name("我的剧集.第01-03集.4K.mp4", 1)
        self.assertEqual(eps_cn, [1, 2, 3])

        files = [
            {"name": "MyShow.S01E01-E02.1080p.mkv", "path": "/path/MyShow.S01E01-E02.1080p.mkv", "size": 2000, "isDirectory": False},
        ]
        plan = generate_review_clean_plan(files, season_number=1, total_episodes=2)
        self.assertEqual(len(plan["to_delete"]), 0)
        self.assertEqual(len(plan["to_keep"]), 1)
        self.assertEqual(plan["to_keep"][0]["name"], "MyShow.S01E01-E02.1080p.mkv")

    def test_p02_overlap_multiepisode_total2(self):
        """P0-2: 总集数=2，单集 X.S01E01 (大) + 合集 X.S01E01-E02 (小)，合集是 E02 的唯一文件，绝不能删！"""
        files = [
            {"name": "X.S01E01.1080p.mkv", "path": "/path/X.S01E01.1080p.mkv", "size": 2000, "isDirectory": False},
            {"name": "X.S01E01-E02.1080p.mkv", "path": "/path/X.S01E01-E02.1080p.mkv", "size": 1000, "isDirectory": False},
        ]
        plan = generate_review_clean_plan(files, season_number=1, total_episodes=2)
        to_keep_paths = {k["path"] for k in plan["to_keep"]}
        to_delete_paths = {d["path"] for d in plan["to_delete"]}

        # 核心不变量 1: 保留与删除路径绝对无交集
        self.assertEqual(to_keep_paths & to_delete_paths, set(), "to_keep 和 to_delete 存在交集！")

        # 核心不变量 2: 合集包含唯一 E02，绝对不能在删除清单中！
        self.assertNotIn("/path/X.S01E01-E02.1080p.mkv", to_delete_paths)
        self.assertIn("/path/X.S01E01-E02.1080p.mkv", to_keep_paths)

    def test_p02_overlap_multiepisode_total3_mixed(self):
        """P0-2: 总集数=3，混版分支：单集 X.S01E01 (大) + 合集 X.S01E01-E02 (小)，合集绝不能删！"""
        files = [
            {"name": "X.S01E01.1080p.mkv", "path": "/path/X.S01E01.1080p.mkv", "size": 2000, "isDirectory": False},
            {"name": "X.S01E01-E02.1080p.mkv", "path": "/path/X.S01E01-E02.1080p.mkv", "size": 1000, "isDirectory": False},
        ]
        plan = generate_review_clean_plan(files, season_number=1, total_episodes=3)
        to_keep_paths = {k["path"] for k in plan["to_keep"]}
        to_delete_paths = {d["path"] for d in plan["to_delete"]}

        # 核心不变量 1: 保留与删除路径绝对无交集
        self.assertEqual(to_keep_paths & to_delete_paths, set(), "to_keep 和 to_delete 存在交集！")

        # 核心不变量 2: 合集包含唯一 E02，绝对不能在删除清单中！
        self.assertNotIn("/path/X.S01E01-E02.1080p.mkv", to_delete_paths)
        self.assertIn("/path/X.S01E01-E02.1080p.mkv", to_keep_paths)

    def test_f07_multiepisode_duplicate_arbitration(self):
        """F07: 多完整版仲裁：4K完整版 (E01-E02) 与 720p完整版 (E01-E02)，优选 4K，淘汰 720p。"""
        files = [
            {"name": "MyShow.S01E01-E02.4k.mkv", "path": "/path/MyShow.S01E01-E02.4k.mkv", "size": 4000, "isDirectory": False},
            {"name": "MyShow.S01E01-E02.720p.mkv", "path": "/path/MyShow.S01E01-E02.720p.mkv", "size": 800, "isDirectory": False},
        ]
        plan = generate_review_clean_plan(files, season_number=1, total_episodes=2)
        self.assertTrue(plan["can_clean"])
        to_keep = [k["name"] for k in plan["to_keep"]]
        to_delete = [d["name"] for d in plan["to_delete"]]
        self.assertIn("MyShow.S01E01-E02.4k.mkv", to_keep)
        self.assertIn("MyShow.S01E01-E02.720p.mkv", to_delete)

if __name__ == "__main__":
    unittest.main()
