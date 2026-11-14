import unittest
from app.core.review_engine import episodes_from_name, generate_review_clean_plan
from app.core.inspector import diagnose_season


class TestCmctvAndSpecials(unittest.TestCase):
    def test_cmctv_release_group_not_misidentified_as_cm_special(self):
        """测试 CMCTV、CMCT 等发布组标签绝对不被误判为特典广告 CM。"""
        f1 = "百万秘宝攻防战 - S02E01 - [百万秘宝攻防战].Million.Dollar.Secret.S02E01.2026.1080p.NF.WEB-DL.x264.DDP.5.1-CMCTV.mkv"
        eps1 = episodes_from_name(f1, season=2)
        self.assertEqual(eps1, [1])

        f7 = "百万秘宝攻防战 - S02E07 - [百万秘宝攻防战].Million.Dollar.Secret.S02E07.2026.1080p.NF.WEB-DL.x264.DDP.5.1-CMCTV.mkv"
        eps7 = episodes_from_name(f7, season=2)
        self.assertEqual(eps7, [7])

        f11 = "青春环游记 - S05E11 - [青春环游记 第5季].Youth.Periplous.2024.S05E11.WEB-DL.4K.HEVC.AAC-CMCTV.mp4"
        eps11 = episodes_from_name(f11, season=5)
        self.assertEqual(eps11, [11])

        f12 = "青春环游记 - S05E12 - [青春环游记 第5季].Youth.Periplous.2024.S05E12.WEB-DL.4K.HEVC.AAC-CMCTV.mp4"
        eps12 = episodes_from_name(f12, season=5)
        self.assertEqual(eps12, [12])

    def test_pure_specials_still_filtered(self):
        """纯特典（如 PV01、CM01）且无正片编号者，仍正确识别并过滤。"""
        self.assertEqual(episodes_from_name("[VCB-Studio] Evangelion 3.0+1.0 [PV01].mkv"), [])
        self.assertEqual(episodes_from_name("[Moozzi2] Fate Zero - CM01.mkv"), [])

    def test_e00_pilot_diagnose_season(self):
        """测试正片齐全且附带 E00 先导特辑时，自动就绪可归档并提示智能移入 Season 0。"""
        # 森林进化论 S02：官方 23 集，本地有 0..23（共 24 集）
        status, detail = diagnose_season(
            recognized=list(range(0, 24)),
            total_episodes=23,
            season_finished=True,
            has_duplicates=False,
        )
        self.assertEqual(status, "archive_ready")
        self.assertIn("Season 0", detail)
        self.assertIn("第 0 集先导特辑", detail)

    def test_e00_with_duplicates_review_needed(self):
        """测试有 E00 但正片存在重复集时，仍进入复核清理。"""
        status, detail = diagnose_season(
            recognized=list(range(0, 24)),
            total_episodes=23,
            season_finished=True,
            has_duplicates=True,
            duplicate_episodes=[1],
        )
        self.assertEqual(status, "review_needed")

    def test_multiple_e00_specials_without_regular_duplicates_archive_ready(self):
        """测试仅第 0 集（先导片与特别企划）有多份副本、正片齐全且无正片重复时，直接判定可归档。"""
        # 中餐厅 S06：官方 12 集，本地有 0..12，但仅 0 号集有两个版本（先导片与特别企划）
        status, detail = diagnose_season(
            recognized=list(range(0, 13)),
            total_episodes=12,
            season_finished=True,
            has_duplicates=True,
            duplicate_episodes=[0],
        )
        self.assertEqual(status, "archive_ready")
        self.assertIn("Season 0", detail)
        self.assertIn("第 0 集先导特辑/特别篇", detail)

    def test_multiple_e00_specials_classified_to_archive_category(self):
        """测试仅有第 0 集重复的记录在五模块分流中直接归入 archive (齐全可归档)。"""
        from app.core.season_classifier import classify_single_record, consolidate_seasons_to_work_records
        rec = {
            "show_name": "中餐厅 (2017) [tmdbid=91914]",
            "season_number": 6,
            "tmdb_id": 91914,
            "total_episodes": 12,
            "status": "archive_ready",
            "status_detail": "全 12 集齐全（附带第 0 集先导特辑/特别篇，归档时智能移入 Season 0），可归档",
            "recognized_episodes_json": "[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]",
            "duplicate_episodes_json": "[0]",
            "missing_episodes_json": "[]",
        }
        cat, action = classify_single_record(rec)
        self.assertEqual(cat, "archive")
        self.assertEqual(action, "archive")
        works, counts = consolidate_seasons_to_work_records([rec])
        self.assertEqual(counts["archive"], 1)
        self.assertEqual(counts["review"], 0)
        self.assertEqual(works[0]["main_category"], "archive")

    def test_review_clean_engine_safely_protects_all_specials(self):
        """测试清理引擎对第 0 集先导片与各类特辑绝对保护不删，只清理正片冗余。"""
        files = [
            {"name": "中餐厅 - S10E00 - 中餐厅.Chinese.Restaurant.2026.S10E00.1080p.WEB-DL.H264.AAC-PTerWEB.mp4", "path": "/src/S10/e00_1.mp4", "size": 1000},
            {"name": "中餐厅 - S10E00 - 中餐厅特别企划.Chinese.Restaurant.Plus.2026.S10SP01.1080p.WEB-DL.H264.AAC-PTerWEB.mp4", "path": "/src/S10/e00_2.mp4", "size": 1200},
        ]
        for i in range(1, 13):
            files.append({"name": f"中餐厅 - S10E{i:02d} - 1080p.mp4", "path": f"/src/S10/e{i:02d}_1080.mp4", "size": 2000})
            files.append({"name": f"中餐厅 - S10E{i:02d} - 720p.mp4", "path": f"/src/S10/e{i:02d}_720.mp4", "size": 1000})

        plan = generate_review_clean_plan(files, season_number=10, total_episodes=12)
        self.assertTrue(plan["can_clean"])
        kept_names = [k["name"] for k in plan["to_keep"]]
        del_names = [d["name"] for d in plan["to_delete"]]
        # 两个 E00 特辑均必须 100% 保护在 to_keep 中
        self.assertIn("中餐厅 - S10E00 - 中餐厅.Chinese.Restaurant.2026.S10E00.1080p.WEB-DL.H264.AAC-PTerWEB.mp4", kept_names)
        self.assertIn("中餐厅 - S10E00 - 中餐厅特别企划.Chinese.Restaurant.Plus.2026.S10SP01.1080p.WEB-DL.H264.AAC-PTerWEB.mp4", kept_names)
        self.assertNotIn("中餐厅 - S10E00 - 中餐厅.Chinese.Restaurant.2026.S10E00.1080p.WEB-DL.H264.AAC-PTerWEB.mp4", del_names)
        self.assertNotIn("中餐厅 - S10E00 - 中餐厅特别企划.Chinese.Restaurant.Plus.2026.S10SP01.1080p.WEB-DL.H264.AAC-PTerWEB.mp4", del_names)
        # 12 个 720p 劣质正片副本被加入待删除清单
        self.assertEqual(len(plan["to_delete"]), 12)

    def test_clean_plan_with_cmctv_complete_version(self):
        """测试当存在 CMCTV 完整版本与劣质残卷时，能够正确生成清理计划淘汰残卷。"""
        files = [
            {"name": "剧集 - S01E01 - Show.S01E01.720p-GroupA.mkv", "path": "/src/S01/ep1.mkv", "size": 1000},
            {"name": "剧集 - S01E01 - Show.S01E01.1080p-CMCTV.mkv", "path": "/src/S01/ep1_cmctv.mkv", "size": 2000},
            {"name": "剧集 - S01E02 - Show.S01E02.1080p-CMCTV.mkv", "path": "/src/S01/ep2_cmctv.mkv", "size": 2000},
        ]
        plan = generate_review_clean_plan(files, season_number=1, total_episodes=2)
        self.assertTrue(plan["can_clean"])
        self.assertEqual(len(plan["to_keep"]), 2)
        self.assertEqual(len(plan["to_delete"]), 1)
        self.assertEqual(plan["to_delete"][0]["name"], "剧集 - S01E01 - Show.S01E01.720p-GroupA.mkv")


if __name__ == "__main__":
    unittest.main()
