"""MediaFlow 五模块互斥分流与作品季归并单元测试."""

import unittest
from app.core.season_classifier import (
    classify_single_record,
    consolidate_seasons_to_work_records,
    build_work_key,
)


class TestSeasonClassifier(unittest.TestCase):
    def test_single_record_priorities(self):
        # 1. 目录组待确认 -> anomaly
        rec_dir = {
            "status": "review_needed",
            "evidence": {"directory_group": {"requires_confirm": True, "id": "grp-1"}},
        }
        cat, act = classify_single_record(rec_dir)
        self.assertEqual(cat, "anomaly")

        # 2. 身份冲突 -> anomaly
        rec_id_conf = {"status": "identity_review"}
        cat, _ = classify_single_record(rec_id_conf)
        self.assertEqual(cat, "anomaly")

        # 3. 分段存疑 -> anomaly
        rec_period = {"status": "period_review"}
        cat, _ = classify_single_record(rec_period)
        self.assertEqual(cat, "anomaly")

        # 4. 源端已有残留 -> anomaly
        rec_already = {"status": "already_archived"}
        cat, act = classify_single_record(rec_already)
        self.assertEqual(cat, "anomaly")
        self.assertEqual(act, "clean_redundant")

        # 5. 在播连载 -> airing
        rec_airing = {
            "status": "airing",
            "season_finished": False,
            "total_episodes": 16,
            "recognized_episodes": [1, 2, 3],
        }
        cat, act = classify_single_record(rec_airing)
        self.assertEqual(cat, "airing")
        self.assertEqual(act, "skip")

        # 6. 缺集兼重复 -> download (真缺集优先，齐集后再清理)
        rec_gap_dup = {
            "status": "download_needed",
            "missing_episodes": [14],
            "duplicate_episodes": [15],
            "total_episodes": 20,
            "recognized_episodes": [1, 2, 3, 15],
            "season_finished": True,
        }
        cat, act = classify_single_record(rec_gap_dup)
        self.assertEqual(cat, "download")
        self.assertEqual(act, "download")

        # 7. 纯重复无缺集 -> review
        rec_dup_only = {
            "status": "review_needed",
            "missing_episodes": [],
            "duplicate_episodes": [2],
            "total_episodes": 12,
            "recognized_episodes": list(range(1, 13)),
            "season_finished": True,
        }
        cat, act = classify_single_record(rec_dup_only)
        self.assertEqual(cat, "review")
        self.assertEqual(act, "clean")

        # 8. 齐全且无重复 -> archive
        rec_clean_archive = {
            "status": "archive_ready",
            "missing_episodes": [],
            "duplicate_episodes": [],
            "total_episodes": 12,
            "recognized_episodes": list(range(1, 13)),
            "season_finished": True,
        }
        cat, act = classify_single_record(rec_clean_archive)
        self.assertEqual(cat, "archive")
        self.assertEqual(act, "archive")

    def test_multi_dir_consolidation_and_invariants(self):
        # 模拟乘风破浪 S06：中文目录（review）与英文目录（download）
        records = [
            {
                "id": "rec-chinese",
                "show_name": "乘风2025",
                "tmdb_id": "223344",
                "season_number": 6,
                "season_path": "/src/乘风2025/Season 6",
                "status": "review_needed",
                "missing_episodes": [],
                "duplicate_episodes": [1],
                "total_episodes": 12,
                "recognized_episodes": list(range(1, 13)),
            },
            {
                "id": "rec-english",
                "show_name": "Ride The Wind 2025",
                "tmdb_id": "223344",
                "season_number": 6,
                "season_path": "/src/Ride The Wind 2025/Season 6",
                "status": "download_needed",
                "missing_episodes": [5],
                "duplicate_episodes": [],
                "total_episodes": 12,
                "recognized_episodes": [1, 2, 3, 4],
            },
            # 另一个剧集：已齐可归档
            {
                "id": "rec-show2",
                "show_name": "完全完结剧",
                "tmdb_id": "999888",
                "season_number": 1,
                "season_path": "/src/Show2/Season 1",
                "status": "archive_ready",
                "missing_episodes": [],
                "duplicate_episodes": [],
                "total_episodes": 10,
                "recognized_episodes": list(range(1, 11)),
            },
            # 第三个剧集：连载中
            {
                "id": "rec-show3",
                "show_name": "连载剧",
                "tmdb_id": "777666",
                "season_number": 1,
                "season_path": "/src/Show3/Season 1",
                "status": "airing",
                "season_finished": False,
                "missing_episodes": [5, 6],
                "duplicate_episodes": [],
                "total_episodes": 16,
                "recognized_episodes": [1, 2, 3, 4],
            },
        ]

        work_records, counts = consolidate_seasons_to_work_records(records)

        # 乘风破浪两个目录必须被归并为 1 个作品季，且因为缺集优先级 (download: 3) 高于 review (4)，
        # 该作品季主分类必须是 download！
        self.assertEqual(len(work_records), 3, "4 个物理目录应归并为 3 个作品季")
        cf_rec = next(w for w in work_records if str(w.get("tmdb_id")) == "223344")
        self.assertEqual(cf_rec["main_category"], "download")
        self.assertEqual(cf_rec["total_dir_count"], 2)
        self.assertEqual(len(cf_rec["associated_records"]), 1)

        # 数学不变量检验
        self.assertEqual(counts["total_work_seasons"], 3)
        self.assertEqual(
            counts["archive"] + counts["review"] + counts["download"] + counts["airing"] + counts["anomaly"],
            counts["total_work_seasons"],
            "各分类作品季数之和必须严格等于作品季总数！"
        )
        self.assertEqual(counts["download"], 1)
        self.assertEqual(counts["archive"], 1)
        self.assertEqual(counts["airing"], 1)
        self.assertEqual(counts["review"], 0)
        self.assertEqual(counts["anomaly"], 0)


if __name__ == "__main__":
    unittest.main()
