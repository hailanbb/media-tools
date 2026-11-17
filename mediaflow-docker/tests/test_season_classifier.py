"""MediaFlow 五模块互斥分流与作品季归并单元测试."""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath("."))

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

    def test_a13_failed_conflict_and_missing_evidence_never_fallback_to_archive(self):
        """A13: 旧记录为 failed/conflict、缺证据或已 archived；无依据记录严禁兜底落入 archive."""
        rec_failed = {
            "id": "r-failed",
            "show_name": "失败剧",
            "tmdb_id": "5001",
            "season_number": 1,
            "status": "failed",
            "total_episodes": 10,
            "recognized_episodes": list(range(1, 11)),
        }
        cat_f, act_f = classify_single_record(rec_failed)
        self.assertEqual(cat_f, "anomaly")
        self.assertEqual(act_f, "inspect_info")

        rec_no_ev = {
            "id": "r-no-ev",
            "show_name": "无证据剧",
            "tmdb_id": "5002",
            "season_number": 1,
            "status": "archive_ready",
            "total_episodes": None,
            "recognized_episodes": [],
            "local_episode_count": 0,
        }
        cat_ne, _ = classify_single_record(rec_no_ev)
        self.assertEqual(cat_ne, "anomaly", "缺少总集数与覆盖证据的记录不得进入 archive")

        # 同一作品季：一条已 archived，另一条活跃物理记录为 download_needed
        rec_done = {
            "id": "r-done",
            "show_name": "双目录剧",
            "tmdb_id": "5003",
            "season_number": 1,
            "season_path": "/src/A/Season 1",
            "status": "archived",
            "total_episodes": 12,
            "recognized_episodes": list(range(1, 13)),
        }
        rec_active = {
            "id": "r-active",
            "show_name": "双目录剧",
            "tmdb_id": "5003",
            "season_number": 1,
            "season_path": "/src/B/Season 1",
            "status": "download_needed",
            "total_episodes": 12,
            "recognized_episodes": [1, 2, 3],
            "missing_episodes": list(range(4, 13)),
        }
        work_recs, counts = consolidate_seasons_to_work_records([rec_failed, rec_no_ev, rec_done, rec_active])
        self.assertEqual(counts["archive"], 0)
        self.assertEqual(counts["anomaly"], 2)
        self.assertEqual(counts["download"], 1)
        self.assertEqual(
            counts["archive"] + counts["review"] + counts["download"] + counts["airing"] + counts["anomaly"],
            counts["total_work_seasons"],
        )

    def test_a06_complementary_group_vs_intentional_coexist(self):
        """A06: 同作品季两互补源目录归并为 1 张 archive 卡；有意共存收藏保持各自独立卡片不合并."""
        comp_a = {
            "id": "comp-a",
            "show_name": "互补剧",
            "tmdb_id": "6001",
            "season_number": 1,
            "show_path": "/src/互补剧_A",
            "season_path": "/src/互补剧_A/Season 1",
            "archive_target_path": "/dst/互补剧/Season 1",
            "status": "download_needed",
            "total_episodes": 6,
            "recognized_episodes": [1, 2, 3],
            "missing_episodes": [4, 5, 6],
            "duplicate_episodes": [],
            "season_finished": True,
        }
        comp_b = {
            "id": "comp-b",
            "show_name": "互补剧",
            "tmdb_id": "6001",
            "season_number": 1,
            "show_path": "/src/互补剧_B",
            "season_path": "/src/互补剧_B/Season 1",
            "archive_target_path": "/dst/互补剧/Season 1",
            "status": "download_needed",
            "total_episodes": 6,
            "recognized_episodes": [4, 5, 6],
            "missing_episodes": [1, 2, 3],
            "duplicate_episodes": [],
            "season_finished": True,
        }
        coexist_1 = {
            "id": "coex-1",
            "show_name": "共存收藏_国语",
            "tmdb_id": "6002",
            "season_number": 1,
            "show_path": "/src/共存收藏_国语",
            "season_path": "/src/共存收藏_国语/Season 1",
            "archive_target_path": "/dst/共存收藏_国语/Season 1",
            "status": "archive_ready",
            "total_episodes": 4,
            "recognized_episodes": [1, 2, 3, 4],
            "evidence": {"directory_group": {"status": "coexist", "confirmation_mode": "intentional_coexist"}},
        }
        coexist_2 = {
            "id": "coex-2",
            "show_name": "共存收藏_粤语",
            "tmdb_id": "6002",
            "season_number": 1,
            "show_path": "/src/共存收藏_粤语",
            "season_path": "/src/共存收藏_粤语/Season 1",
            "archive_target_path": "/dst/共存收藏_粤语/Season 1",
            "status": "archive_ready",
            "total_episodes": 4,
            "recognized_episodes": [1, 2, 3, 4],
            "evidence": {"directory_group": {"status": "coexist", "confirmation_mode": "intentional_coexist"}},
        }
        work_recs, counts = consolidate_seasons_to_work_records([comp_a, comp_b, coexist_1, coexist_2])
        self.assertEqual(len(work_recs), 3, "互补 2 目录归并为 1 卡 + 有意共存 2 目录保持 2 卡 = 共 3 卡")
        self.assertEqual(counts["archive"], 3)
        comp_card = next(w for w in work_recs if str(w.get("tmdb_id")) == "6001")
        self.assertTrue(comp_card.get("is_complementary_group"))
        self.assertEqual(comp_card.get("recognized_episodes"), [1, 2, 3, 4, 5, 6])


if __name__ == "__main__":
    unittest.main()
