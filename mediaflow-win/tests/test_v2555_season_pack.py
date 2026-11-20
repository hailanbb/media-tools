"""离线验证整季回退、综艺完整声明与覆盖冲突；不提交下载。"""
import copy
import unittest

from test_gap_fill_optimization import _cand
from app.services.download_service import (
    _classify_reject_outcome, _evaluate_raw_candidates,
    select_best_candidates, verify_season_pack_candidate,
)


class SeasonPackFallbackTests(unittest.TestCase):
    def pack(self, description="全10期", **kwargs):
        return _cand("Show.S01.1080p", seeders=8, size=1000,
                     season=1, total_episode=10, description=description, **kwargs)

    def verify(self, candidate, **kwargs):
        return verify_season_pack_candidate(candidate, 1, 10, by_period=True, **kwargs)

    def test_explicit_complete_variety_without_file_list(self):
        for declaration in ("全10期", "全10集", "10期全", "Complete"):
            with self.subTest(declaration=declaration):
                ok, episodes, _ = self.verify(self.pack(declaration))
                self.assertTrue(ok)
                self.assertEqual(episodes, set(range(1, 11)))

    def test_single_missing_episode_falls_back_to_whole_season(self):
        pack = self.pack()
        plan = select_best_candidates([pack], [10], total=10, season=1, by_period=True)
        self.assertEqual(plan, [(pack, list(range(1, 11)), True)])

    def test_incomplete_standalones_fall_back_to_whole_season(self):
        one = _cand("Show.S01E02", seeders=10, size=50, season=1, eps=[2])
        pack = self.pack("全10集")
        plan = select_best_candidates([one, pack], [2, 9], total=10, season=1)
        self.assertEqual(plan, [(pack, list(range(1, 11)), True)])

    def test_production_evaluator_accepts_pack_independently(self):
        standalone, packs, reasons = _evaluate_raw_candidates(
            [self.pack()], tmdb_id_num=42, season_num=1, total_eps=10,
            missing_eps=[9], by_period=True, parts_mode=False,
            part_episode_mapping=None, keyword_fallback=False,
            min_seeders=2, min_resolution=None, require_chinese=False,
        )
        self.assertEqual(len(packs), 1)
        self.assertEqual(packs[0].covered_missing, (9,))
        self.assertFalse(standalone)
        self.assertFalse(reasons)

    def test_known_split_requires_segment_evidence(self):
        self.assertFalse(self.verify(self.pack(), part_episode_mapping={1: [1, 2]})[0])
        self.assertFalse(self.verify(self.pack(), parts_mode=True)[0])

    def test_unknown_or_conflicting_pack_is_not_complete(self):
        bad = [self.pack(""), self.pack("更新至第10期"), self.pack("全10期 花絮")]
        conflict = self.pack()
        conflict["meta_info"]["total_episode"] = "5"
        bad.append(conflict)
        partial = self.pack(eps=[1, 2])
        bad.append(partial)
        wrong_season = self.pack()
        wrong_season["meta_info"]["season_list"] = [2]
        bad.append(wrong_season)
        for cand in bad:
            self.assertFalse(self.verify(cand)[0], cand)

    def test_file_evidence_overrides_full_count_advertisement(self):
        cand = self.pack(files=["Show.S01E01.mkv"])
        self.assertFalse(self.verify(cand)[0])

    def test_seed_threshold_and_pack_disabled_gate(self):
        cand = self.pack()
        low = copy.deepcopy(cand)
        low["torrent_info"]["seeders"] = 1
        self.assertFalse(select_best_candidates([low], [9], total=10, season=1, by_period=True))
        self.assertFalse(select_best_candidates([cand], [9], total=10, season=1,
                                               by_period=True, allow_season_pack=False))

    def test_log_separates_seeders_from_coverage(self):
        code, message = _classify_reject_outcome(291, ["覆盖口径冲突", "做种不足"], "", 223, 2)
        self.assertEqual(code, "no_qualified_candidate")
        self.assertIn("不代表覆盖合格", message)
        self.assertIn("部分资源做种低于 2 人", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
