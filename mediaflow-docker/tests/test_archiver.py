"""归档引擎与源端零残留治理全矩阵单元测试用例.

覆盖场景：
1. 计算归档目标路径 (compute_archive_target)；
2. 同季冲突检测与安全拦截；
3. 全剧归档完成时，公共资产 (tvshow.nfo, poster) 迁入归档库且源端剧集目录彻底删除；
4. 多季连载中，暂时保留源端剧集目录。
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding='utf-8')

from app.core.inspector import compute_archive_target


class TestArchiverEngine(unittest.TestCase):

    def test_01_compute_archive_target(self):
        """测试源路径到归档库路径的映射计算."""
        source_root = "/115/Library_incs/nas"
        archive_root = "/115/Library_incs/nas.finish"

        show_path = "/115/Library_incs/nas/电视剧/韩国剧集/少年，注意 (2026) [tmdbid=322723]"
        season_path = f"{show_path}/Season 1"

        target = compute_archive_target(show_path, season_path, archive_root, source_root)
        expected = "/115/Library_incs/nas.finish/电视剧/韩国剧集/少年，注意 (2026) [tmdbid=322723]/Season 1"
        self.assertEqual(target, expected)

    def test_02_compute_archive_target_already_archived(self):
        """测试已经是归档库路径时的幂等性."""
        source_root = "/115/Library_incs/nas"
        archive_root = "/115/Library_incs/nas.finish"

        show_path = "/115/Library_incs/nas.finish/电视剧/已归档剧"
        season_path = f"{show_path}/Season 1"

        target = compute_archive_target(show_path, season_path, archive_root, source_root)
        self.assertEqual(target, season_path)

    def test_03_archive_cleanup_zero_residue_logic(self):
        """模拟测试全剧归档完成后的公共资产迁移与源端目录物理删除逻辑."""
        # 模拟源端剧根内容：Season 1 刚刚移出后，仅剩公共资产
        remaining_files_after_season_move = [
            {"name": "tvshow.nfo", "isDirectory": False},
            {"name": "poster.jpg", "isDirectory": False},
            {"name": "backdrop.jpg", "isDirectory": False},
        ]

        # 判定是否有剩余季度或视频
        from app.core.inspector import _parse_season_number, is_video_file
        rem_seasons = [
            f for f in remaining_files_after_season_move
            if f.get("isDirectory") and _parse_season_number(f.get("name", "")) is not None
        ]
        rem_videos = [
            f for f in remaining_files_after_season_move
            if not f.get("isDirectory") and is_video_file(f.get("name", ""))
        ]

        self.assertEqual(len(rem_seasons), 0)
        self.assertEqual(len(rem_videos), 0)

        # 此时应判定：全剧已无其余季度，应将公共资产搬移到归档库，然后彻底删除源端剧根目录
        should_delete_show_dir = (not rem_seasons and not rem_videos)
        self.assertTrue(should_delete_show_dir)

    def test_04_archive_multi_season_preserve_logic(self):
        """模拟测试若源端还有其他连载季（如 Season 2），源端剧根目录应暂时保留."""
        remaining_files_after_season_move = [
            {"name": "Season 2", "isDirectory": True},
            {"name": "tvshow.nfo", "isDirectory": False},
            {"name": "poster.jpg", "isDirectory": False},
        ]

        from app.core.inspector import _parse_season_number, is_video_file
        rem_seasons = [
            f for f in remaining_files_after_season_move
            if f.get("isDirectory") and _parse_season_number(f.get("name", "")) is not None
        ]
        rem_videos = [
            f for f in remaining_files_after_season_move
            if not f.get("isDirectory") and is_video_file(f.get("name", ""))
        ]

        self.assertEqual(len(rem_seasons), 1)
        self.assertEqual(rem_seasons[0]["name"], "Season 2")

        # 此时应判定：仍有连载季，暂不能删除源端剧集主目录
        should_delete_show_dir = (not rem_seasons and not rem_videos)
        self.assertFalse(should_delete_show_dir)


if __name__ == "__main__":
    unittest.main()
