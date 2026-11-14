"""单元测试：综艺节目编号自动核验与免人工处理 (v2.5.26).

依据《MediaFlow综艺编号自动核验与免人工处理方案-v2.5.25-2026-11-14.md》：
1. 跨清晰度/编码支持（亚洲新声 S01）：支持跨 1080p/2160p 正片序列，证实整期前后台系统跟踪，不盲推 17-20；
2. 衍生版本隔离（偏爱之恋 S01）：加码/投递/纯享/陪看/日记等作为衍生版隔离，不污染正片，不因衍生文件宣布正片已齐；
3. 双重解释缺期判定（令人心动的offer S07）：整期与流水号双重解释均确认缺失时，判定为 period_missing，准确输出缺失期号；
4. 正片重复副本检出（爸爸当家 S03）：正片齐全但存在同集多份正片副本时，判定为待复核清理；
5. 免人工打扰验证：彻底杜绝“请打开同一版本的样片，确认一集包含整期还是其中一段”骚扰提示。
"""

import unittest
from app.core.segments import diagnose_periods, segment_number, episode_segments, DERIVATIVE_PATTERN


class VarietySegmentsAutoVerifyTests(unittest.TestCase):
    def make_file(self, name: str, size: int = 1000000000) -> dict:
        return {
            "name": name,
            "path": f"/media/shows/Variety/Season 1/{name}",
            "size": size,
            "isDirectory": False,
        }

    def test_cross_resolution_tracking_sing_asia(self):
        """亚洲新声 S01：跨 1080p/2160p 混存，整期编号 1-10 齐全，系统跟踪，不盲推 17-20."""
        # 10 期，每期在 TMDB 分为 2 段 (共 20 段)
        mapping = {n: [1, 2] for n in range(1, 11)}
        ordinals = {}
        for n in range(1, 21):
            ordinals[n] = (n + 1) // 2

        # 本地有 10 期整期版文件，但 E01 为 1080p，E02-E10 为 2160p
        files = [
            self.make_file("Sing.Asia.S01E01.1080p.iQIYI.WEB-DL.AAC2.0.H.264-MWeb.mkv"),
        ] + [
            self.make_file(f"Sing.Asia.S01E{n:02d}.2160p.iQIYI.WEB-DL.AAC2.0.H.265-MWeb.mkv")
            for n in range(2, 11)
        ]

        status, detail, missing, recognized = diagnose_periods(
            files=files,
            season=1,
            mapping=mapping,
            ordinals=ordinals,
            total=10,
            finished=True,
            archive_present=False,
        )

        # 状态应为系统跟踪 (period_review)，绝不盲推 17-20，且缺失期数为空
        self.assertEqual(status, "period_review")
        self.assertEqual(missing, [])
        self.assertEqual(recognized, list(range(1, 11)))
        # 包含系统跟踪说明，不含打开样片骚扰
        self.assertIn("系统跟踪中无需操作", detail)
        self.assertNotIn("请打开同一版本的样片", detail)
        self.assertNotIn("17", detail)

    def test_derivative_isolation_pianai(self):
        """偏爱之恋 S01：混入偏爱投递、加码、日记、纯享、陪看衍生版，隔离处理."""
        mapping = {n: [1, 2] for n in range(1, 13)}
        ordinals = {n: (n + 1) // 2 for n in range(1, 25)}

        # E01 只有衍生版本，无正片；E02-E12 有正片
        derivative_names = [
            "偏爱之恋.偏爱投递.Love.Wins.All.S01E01.Deliver.2025.2160p.WEB-DL.H265.AAC-ADWeb.mp4",
            "偏爱之恋.加码.Love.Wins.All.S01E01.Plus.2025.2160p.WEB-DL.H265.AAC-ADWeb.mp4",
            "偏爱之恋.日记.Love.Wins.All.S01E01.Diary.2025.2160p.WEB-DL.H265.AAC-ADWeb.mp4",
            "偏爱之恋.纯享.Love.Wins.All.S01E01.Pure.2025.2160p.WEB-DL.H265.AAC-ADWeb.mp4",
            "偏爱之恋.陪看.Love.Wins.All.S01E01.Watch.2025.2160p.WEB-DL.H265.AAC-ADWeb.mp4",
        ]
        regular_names = [
            f"偏爱之恋.Love.Wins.All.S01E{n:02d}.2025.2160p.WEB-DL.H265.AAC-ADWeb.mp4"
            for n in range(2, 13)
        ]
        files = [self.make_file(name) for name in derivative_names + regular_names]

        status, detail, missing, recognized = diagnose_periods(
            files=files,
            season=1,
            mapping=mapping,
            ordinals=ordinals,
            total=12,
            finished=True,
            archive_present=False,
        )

        # 衍生版不能替代正片 E01，正片 E01 尚未确认，保持系统跟踪
        self.assertEqual(status, "period_review")
        self.assertIn("衍生版本", detail)
        self.assertIn("系统跟踪中无需操作", detail)
        self.assertNotIn("请打开同一版本的样片", detail)

    def test_dual_interpretation_missing_offer(self):
        """令人心动的offer S07：整期缺 8-10，流水号也缺 8-10，双重确认缺失 -> period_missing."""
        mapping = {
            1: [1, 2, 3], 2: [1, 2, 3], 3: [1, 2, 3], 4: [1, 2, 3],
            5: [1, 2], 6: [1, 2, 3], 7: [1, 2, 3], 8: [1, 2], 9: [1, 2], 10: [1, 2, 3]
        }
        ordinals = {
            1: 1, 2: 1, 3: 1, 4: 2, 5: 2, 6: 2, 7: 3, 8: 3, 9: 3, 10: 4, 11: 4, 12: 4,
            13: 5, 14: 5, 15: 6, 16: 6, 17: 6, 18: 7, 19: 7, 20: 7, 21: 8, 22: 8,
            23: 9, 24: 9, 25: 10, 26: 10, 27: 10
        }

        # 仅有 1-7 期整期文件
        files = [
            self.make_file(f"令人心动的offer.S07E{n:02d}.2025.2160p.WEB-DL.H265.AAC-ADWeb.mp4")
            for n in range(1, 8)
        ]

        status, detail, missing, recognized = diagnose_periods(
            files=files,
            season=7,
            mapping=mapping,
            ordinals=ordinals,
            total=10,
            finished=True,
            archive_present=False,
        )

        # 准确判定为 period_missing，仅推送确实缺失的 8, 9, 10
        self.assertEqual(status, "period_missing")
        self.assertEqual(missing, [8, 9, 10])
        self.assertEqual(recognized, list(range(1, 8)))
        self.assertIn("按期号和分段集号分别核对", detail)
        self.assertIn("缺第 8, 9, 10 期", detail)
        self.assertNotIn("请打开同一版本的样片", detail)

    def test_explicit_segments_complete(self):
        """显式上中下分段齐全 -> period_ready."""
        mapping = {1: [1, 2], 2: [1, 2], 3: [1, 2]}
        ordinals = {1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 3}
        files = [
            self.make_file("Show.S01E01.第1期上.mkv"),
            self.make_file("Show.S01E01.第1期下.mkv"),
            self.make_file("Show.S01E02.第2期上.mkv"),
            self.make_file("Show.S01E02.第2期下.mkv"),
            self.make_file("Show.S01E03.第3期上.mkv"),
            self.make_file("Show.S01E03.第3期下.mkv"),
        ]

        status, detail, missing, recognized = diagnose_periods(
            files=files,
            season=1,
            mapping=mapping,
            ordinals=ordinals,
            total=3,
            finished=True,
            archive_present=False,
        )

        self.assertEqual(status, "period_ready")
        self.assertEqual(missing, [])
        self.assertEqual(recognized, [1, 2, 3])
        self.assertIn("逐期核实了全部上下段", detail)

    def test_no_harassment_text_across_all_branches(self):
        """验证所有逻辑分支均不出现骚扰提示."""
        mapping = {1: [1, 2], 2: [1, 2]}
        ordinals = {1: 1, 2: 1, 3: 2, 4: 2}

        # 分支 1: 未完成播映
        _, d1, _, _ = diagnose_periods(
            [self.make_file("Show.S01E01.mkv")], 1, mapping, ordinals, 2, False, False
        )
        self.assertNotIn("请打开同一版本的样片", d1)

        # 分支 2: 双重缺失
        _, d2, _, _ = diagnose_periods(
            [self.make_file("Show.S01E01.mkv")], 1, mapping, ordinals, 2, True, False
        )
        self.assertNotIn("请打开同一版本的样片", d2)

        # 分支 3: 歧义跟踪
        _, d3, _, _ = diagnose_periods(
            [self.make_file("Show.S01E01.mkv"), self.make_file("Show.S01E02.mkv")],
            1, mapping, ordinals, 2, True, False
        )
        self.assertNotIn("请打开同一版本的样片", d3)
        self.assertIn("系统跟踪中无需操作", d3)


if __name__ == "__main__":
    unittest.main()
