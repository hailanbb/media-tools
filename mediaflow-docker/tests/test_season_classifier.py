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

    def test_l01_l02_l03_l04_l05_l07_l08_l09_airing_rules(self):
        """验证连载留源 L01-L09 核心分流、三组进度、多目录去重与失效转异常."""
        # L01: 已播 8/12，八集收齐，后四集有未来排期 -> 留源 1 季，显示已播 8 集，已收齐；4 集待播，无缺集提示
        rec_l01 = {
            "id": "l01-s2",
            "show_name": "沿海列车",
            "tmdb_id": "8001",
            "season_number": 2,
            "season_path": "/src/沿海列车/Season 2",
            "status": "airing",
            "season_finished": False,
            "total_episodes": 12,
            "recognized_episodes": list(range(1, 9)),
            "missing_episodes": [9, 10, 11, 12],
            "duplicate_episodes": [],
            "evidence": {
                "broadcast_evidence": {
                    "status": "airing",
                    "schedule_verified": True,
                    "aired_episodes": list(range(1, 9)),
                    "upcoming_episodes": [9, 10, 11, 12],
                    "next_episode_number": 9,
                    "next_air_date": "2099-05-01",
                }
            },
        }
        w_l01, c_l01 = consolidate_seasons_to_work_records([rec_l01])
        self.assertEqual(c_l01["airing"], 1)
        self.assertEqual(w_l01[0]["missing_episodes"], [], "未来待播集不得作为缺集暴露")
        summary_l01 = w_l01[0]["airing_view"]["card_summary"]
        self.assertIn("已播 8 集，已收齐", summary_l01)
        self.assertIn("4 集待播", summary_l01)

        # L02: 源端 E01-E05,E07，归档端 E08，E06 在途 -> 先显示已收 7、在途 1；E06 入库后 -> 已收 8、在途消失
        rec_l02_before = {
            "id": "l02-s2",
            "show_name": "沿海列车",
            "tmdb_id": "8002",
            "season_number": 2,
            "season_path": "/src/沿海列车/Season 2",
            "status": "airing",
            "season_finished": False,
            "total_episodes": 12,
            "recognized_episodes": [1, 2, 3, 4, 5, 7],
            "gap_fill_state": {"pushed_episodes": [6]},
            "evidence": {
                "dual_archive_info": {
                    "source_episodes": [1, 2, 3, 4, 5, 7],
                    "archived_episodes": [8],
                },
                "broadcast_evidence": {
                    "status": "airing",
                    "schedule_verified": True,
                    "aired_episodes": list(range(1, 9)),
                    "upcoming_episodes": [9, 10, 11, 12],
                },
            },
        }
        w_l02_b, _ = consolidate_seasons_to_work_records([rec_l02_before])
        av_b = w_l02_b[0]["airing_view"]
        self.assertEqual(len(av_b["collected_aired_episodes"]), 7)
        self.assertEqual(av_b["in_transit_episodes"], [6])
        self.assertIn("已播 8 集，已收 7 集", av_b["card_summary"])
        self.assertIn("E06 已提交下载，等待入库", av_b["card_summary"])

        rec_l02_after = {
            **rec_l02_before,
            "recognized_episodes": [1, 2, 3, 4, 5, 6, 7],
            "evidence": {
                **rec_l02_before["evidence"],
                "dual_archive_info": {
                    "source_episodes": [1, 2, 3, 4, 5, 6, 7],
                    "archived_episodes": [8],
                },
            },
        }
        w_l02_a, _ = consolidate_seasons_to_work_records([rec_l02_after])
        av_a = w_l02_a[0]["airing_view"]
        self.assertEqual(len(av_a["collected_aired_episodes"]), 8)
        self.assertEqual(av_a["in_transit_episodes"], [])
        self.assertNotIn("已提交下载", av_a["card_summary"])

        # L03: 同剧 S01 已播毕齐全，S02 在播 -> S01 归入 archive，S02 归入 airing
        rec_s01_done = {
            "id": "l03-s1",
            "show_name": "跨季剧",
            "tmdb_id": "8003",
            "season_number": 1,
            "status": "archive_ready",
            "season_finished": True,
            "total_episodes": 10,
            "recognized_episodes": list(range(1, 11)),
        }
        rec_s02_air = {
            "id": "l03-s2",
            "show_name": "跨季剧",
            "tmdb_id": "8003",
            "season_number": 2,
            "status": "airing",
            "season_finished": False,
            "total_episodes": 10,
            "recognized_episodes": [1, 2, 3],
        }
        _, c_l03 = consolidate_seasons_to_work_records([rec_s01_done, rec_s02_air])
        self.assertEqual(c_l03["archive"], 1)
        self.assertEqual(c_l03["airing"], 1)

        # L04: 旧记录仅有 season_finished=False（无状态/无排期证据），或 heuristic_180d_blocked -> 归入异常剧集 system_tracking
        rec_legacy_false = {
            "id": "l04-legacy",
            "show_name": "旧布尔剧",
            "tmdb_id": "8004",
            "season_number": 1,
            "season_finished": False,
            "total_episodes": 12,
            "recognized_episodes": [1, 2],
        }
        cat_l04a, act_l04a = classify_single_record(rec_legacy_false)
        self.assertEqual((cat_l04a, act_l04a), ("anomaly", "system_tracking"))

        rec_181d = {
            "id": "l04-181d",
            "show_name": "缺明细181天剧",
            "tmdb_id": "8004",
            "season_number": 2,
            "status": "airing",
            "season_finished": False,
            "evidence": {
                "broadcast_evidence": {
                    "status": "evidence_pending",
                    "heuristic_180d_blocked": True,
                    "missing_evidence_reason": "季明细缺失，仅凭首播距今 181 天无法确认播毕",
                }
            },
        }
        cat_l04b, act_l04b = classify_single_record(rec_181d)
        self.assertEqual((cat_l04b, act_l04b), ("anomaly", "system_tracking"))

        # L05: 连载季 E05 有两份，底层 review_needed；同时关联历史物理记录带 download_needed -> 主卡仅 1 张且为 airing
        rec_l05_dup = {
            "id": "l05-dup",
            "show_name": "连载双版本剧",
            "tmdb_id": "8005",
            "season_number": 1,
            "season_path": "/src/A/Season 1",
            "status": "review_needed",
            "status_detail": "连载中存在重复版本 (第 5 集)，需清理低质冗余",
            "season_finished": False,
            "total_episodes": 12,
            "recognized_episodes": [1, 2, 3, 4, 5],
            "duplicate_episodes": [5],
        }
        rec_l05_hist_gap = {
            "id": "l05-gap",
            "show_name": "连载双版本剧",
            "tmdb_id": "8005",
            "season_number": 1,
            "season_path": "/src/B/Season 1",
            "status": "download_needed",
            "season_finished": True,
            "total_episodes": 12,
            "recognized_episodes": [1, 2, 3],
            "missing_episodes": list(range(4, 13)),
        }
        w_l05, c_l05 = consolidate_seasons_to_work_records([rec_l05_dup, rec_l05_hist_gap])
        self.assertEqual(len(w_l05), 1)
        self.assertEqual(c_l05["airing"], 1)
        self.assertEqual(c_l05["download"], 0)
        self.assertEqual(c_l05["review"], 0)
        self.assertEqual(w_l05[0]["airing_view"]["duplicate_episodes"], [5])

        # L07: 同季两个已绑定物理目录，正片重叠 (E03,E04 重叠) -> 确认有效时去重计数(6集)、只占 1 季；确认失效后移到异常剧集
        rec_l07_a = {
            "id": "l07-a",
            "show_name": "双目录连载剧",
            "tmdb_id": "8007",
            "season_number": 1,
            "season_path": "/src/DirA/Season 1",
            "status": "airing",
            "season_finished": False,
            "total_episodes": 12,
            "recognized_episodes": [1, 2, 3, 4],
            "evidence": {"directory_group": {"id": "grp-8007", "requires_confirm": False, "status": "confirmed"}},
        }
        rec_l07_b = {
            "id": "l07-b",
            "show_name": "双目录连载剧",
            "tmdb_id": "8007",
            "season_number": 1,
            "season_path": "/src/DirB/Season 1",
            "status": "airing",
            "season_finished": False,
            "total_episodes": 12,
            "recognized_episodes": [3, 4, 5, 6],
            "evidence": {"directory_group": {"id": "grp-8007", "requires_confirm": False, "status": "confirmed"}},
        }
        w_l07_ok, c_l07_ok = consolidate_seasons_to_work_records([rec_l07_a, rec_l07_b])
        self.assertEqual(len(w_l07_ok), 1)
        self.assertEqual(c_l07_ok["airing"], 1)
        self.assertEqual(w_l07_ok[0]["recognized_episodes"], [1, 2, 3, 4, 5, 6], "重叠目录必须按集号去重计数")
        self.assertEqual(w_l07_ok[0]["airing_view"]["cross_dir_overlap_episodes"], [3, 4])

        # 当目录组确认失效 (grp-8007 进入 pending_dir_group_ids) -> 唯一主卡移到异常剧集
        w_l07_inv, c_l07_inv = consolidate_seasons_to_work_records([rec_l07_a, rec_l07_b], pending_dir_group_ids={"grp-8007"})
        self.assertEqual(len(w_l07_inv), 1)
        self.assertEqual(c_l07_inv["airing"], 0)
        self.assertEqual(c_l07_inv["anomaly"], 1)
        self.assertEqual(w_l07_inv[0]["main_category"], "anomaly")


if __name__ == "__main__":
    unittest.main()
