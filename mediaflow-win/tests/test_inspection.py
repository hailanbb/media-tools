"""初检引擎与异常剧集治理全矩阵单元测试用例.

覆盖场景：
1. 剧集集号解析 (parse_episode) 与多集/花絮识别；
2. 剧季状态诊断 (diagnose_season)：
   - archive_ready (全集齐全无重复)
   - review_needed (含重复版本)
   - download_needed (已完结但缺集)
   - airing (连载中未完结)
   - unknown (TMDB无元数据)
3. 异常剧集分类规则验证 (拆分冲突、归档已有、元数据丢失)；
4. 集数人工校准流转验证。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding='utf-8')

from app.core.inspector import diagnose_season, parse_episode


class TestInspectionEngine(unittest.TestCase):

    def test_01_parse_episode_variants(self):
        """测试各种标准与复杂命名的单集解析."""
        # 标准 S01E05
        ep1 = parse_episode("Show.Name.S01E05.1080p.mkv", "/path/Show/Season 1/Show.Name.S01E05.1080p.mkv", 2000000000)
        self.assertEqual(ep1.episode_numbers, [5])
        self.assertFalse(ep1.is_extra)

        # 中文 第08集
        ep2 = parse_episode("国产动漫 第08集 1080p.mp4", "/path/Season 1/国产动漫 第08集 1080p.mp4", 1000000000)
        self.assertEqual(ep2.episode_numbers, [8])
        self.assertFalse(ep2.is_extra)

        # 多集打包 E01-E02
        ep3 = parse_episode("Show.S01E01-E02.1080p.mkv", "/path/Show/Season 1/Show.S01E01-E02.1080p.mkv", 4000000000)
        self.assertEqual(ep3.episode_numbers, [1, 2])
        self.assertFalse(ep3.is_extra)

        # 衍生花絮 / 日记
        ep4 = parse_episode("Show.S01.特辑.花絮.mkv", "/path/Show/Season 1/Show.S01.特辑.花絮.mkv", 500000000)
        self.assertTrue(ep4.is_extra)

    def test_02_diagnose_archive_ready(self):
        """测试齐全可归档：全 12 集，无重复，完结."""
        status, detail = diagnose_season(
            recognized=list(range(1, 13)),
            total_episodes=12,
            season_finished=True,
            has_duplicates=False,
        )
        self.assertEqual(status, "archive_ready")
        self.assertIn("可归档", detail)

    def test_03_diagnose_review_needed(self):
        """测试含重复集：无论是否完结，优先进入待复核清理."""
        status, detail = diagnose_season(
            recognized=list(range(1, 13)),
            total_episodes=12,
            season_finished=True,
            has_duplicates=True,  # 有重复副本
        )
        self.assertEqual(status, "review_needed")
        self.assertIn("重复", detail)

    def test_03b_diagnose_download_needed_takes_precedence_over_review(self):
        """测试缺集属实优先进待补缺：即使有重复集，已完结剧集仍优先归入 download_needed."""
        status, detail = diagnose_season(
            recognized=[1, 2, 3, 5],
            total_episodes=5,
            season_finished=True,
            has_duplicates=True,
            duplicate_episodes=[5],
        )
        self.assertEqual(status, "download_needed")
        self.assertIn("缺失", detail)
        self.assertIn("4", detail)
        self.assertIn("重复", detail)
        self.assertIn("5", detail)

    def test_04_diagnose_download_needed(self):
        """测试缺集：全剧已完结（12集），但本地只有 10 集，缺 11、12."""
        status, detail = diagnose_season(
            recognized=list(range(1, 11)),
            total_episodes=12,
            season_finished=True,
            has_duplicates=False,
        )
        self.assertEqual(status, "download_needed")
        self.assertIn("11", detail)
        self.assertIn("12", detail)

    def test_05_diagnose_airing(self):
        """测试连载中：官方未完结，本地当前 8/24 集."""
        status, detail = diagnose_season(
            recognized=list(range(1, 9)),
            total_episodes=24,
            season_finished=False,
            has_duplicates=False,
        )
        self.assertEqual(status, "airing")
        self.assertIn("尚未确认本季播毕", detail)

    def test_06_diagnose_unknown(self):
        """测试 TMDB 无总集数信息."""
        status, detail = diagnose_season(
            recognized=[1, 2, 3],
            total_episodes=None,  # 无法获取总集数
            season_finished=False,
            has_duplicates=False,
        )
        self.assertEqual(status, "unknown")
        self.assertIn("无法获取", detail)

    def test_07_filter_non_regular_episodes_and_historical_finish(self):
        """测试 TMDB 混入预告片时被剔除，且历史完结剧（首播距今超半年）正确识别已完结与真实总集数."""
        import asyncio
        from unittest.mock import AsyncMock
        from app.core.inspector import _fetch_seasons_by_tmdb_id

        mock_mp = AsyncMock()
        # 模拟 TMDB 返回包含预告片（如山河新疆 S01，TMDB 宣称 4 集，但第 4 集是预告片且无 air_date）
        mock_mp.get_tmdb_seasons.return_value = [
            {"season_number": 1, "episode_count": 4, "air_date": "2020-03-24"}
        ]
        mock_mp.get_tmdb_episodes.return_value = [
            {"episode_number": 1, "name": "天山", "air_date": "2020-03-24"},
            {"episode_number": 2, "name": "昆仑山", "air_date": "2020-03-31"},
            {"episode_number": 3, "name": "阿尔泰山", "air_date": "2020-04-07"},
            {"episode_number": 4, "name": "预告片 - 走进探秘《山河新疆》", "air_date": None},
        ]

        async def run():
            data = await _fetch_seasons_by_tmdb_id(mock_mp, 201303, None)
            self.assertIn(1, data)
            # 必须成功过滤掉第 4 集预告片，真实总集数为 3
            self.assertEqual(data[1]["total"], 3)
            # 首播于 2020 年且 3 集均已播出，必须判定为已完结 True，绝不能误判为连载追更中
            self.assertTrue(data[1]["finished"])

        asyncio.run(run())

    def test_08_auto_recalibrate_season_on_overflow(self):
        """测试超范围异常自愈校准：本地 18 集但元数据误判为 10 集时，自动校准为 18 集可归档."""
        import asyncio
        from unittest.mock import AsyncMock
        from app.core.inspector import auto_recalibrate_season

        mock_tmdb = AsyncMock()
        mock_mp = AsyncMock()

        # 模拟 TMDB 搜索返回 2 个候选：渔村篇 (2015, S2=10集) 与 旌善篇 (2014, S2=18集)
        mock_tmdb.search_tv.return_value = [
            {"id": 68157, "name": "三时三餐 渔村篇", "first_air_date": "2015-01-23"},
            {"id": 68156, "name": "三时三餐 旌善篇", "first_air_date": "2014-10-17"},
        ]

        async def get_tv_detail_mock(tv_id):
            if tv_id == 68157:
                return {"id": 68157, "name": "三时三餐 渔村篇", "seasons": [{"season_number": 2, "episode_count": 10, "air_date": "2015-10-09"}]}
            elif tv_id == 68156:
                return {"id": 68156, "name": "三时三餐 旌善篇", "seasons": [{"season_number": 2, "episode_count": 18, "air_date": "2015-05-15"}]}
            return {}

        mock_tmdb.get_tv_detail.side_effect = get_tv_detail_mock

        async def run():
            # 本地实测 18 集，原元数据为 10 集 (触发超范围异常)
            new_tot, is_fin, new_tid, src = await auto_recalibrate_season(
                show_name="三时三餐 (2014) [tmdbid=211502]",
                season_num=2,
                recognized=list(range(1, 19)),
                current_total=10,
                current_tmdb_id=211502,
                tmdb_client=mock_tmdb,
                mp_client=mock_mp,
            )
            # 必须精准选拔出 18 集的 68156
            self.assertEqual(new_tot, 18)
            self.assertEqual(new_tid, 68156)
            self.assertTrue(is_fin)
            self.assertIn("68156", src)

            # 诊断应直接变为 archive_ready
            diag_status, diag_detail = diagnose_season(
                recognized=list(range(1, 19)),
                total_episodes=new_tot,
                season_finished=is_fin,
                has_duplicates=False,
            )
            self.assertEqual(diag_status, "archive_ready")
            self.assertIn("可归档", diag_detail)

        asyncio.run(run())

    def test_09_resolve_show_metadata_candidate_scoring(self):
        """测试元数据解析引擎的候选打分竞选：年份吻合的条目必须优先胜出."""
        import asyncio
        from unittest.mock import AsyncMock
        from app.core.inspector import _resolve_show_metadata

        mock_tmdb = AsyncMock()
        mock_mp = AsyncMock()

        # 模拟 MP 对无效 ID 211502 返回空
        mock_mp.get_tmdb_seasons.return_value = []

        # 模拟 TMDB 返回候选（2015 年条目排在前面，2014 年条目排在后面）
        mock_tmdb.search_tv.return_value = [
            {"id": 68157, "name": "三时三餐 渔村篇", "first_air_date": "2015-01-23"},
            {"id": 68156, "name": "三时三餐 旌善篇", "first_air_date": "2014-10-17"},
        ]

        async def get_tv_detail_mock(tv_id):
            if tv_id == 68157:
                return {"id": 68157, "name": "三时三餐 渔村篇", "seasons": [{"season_number": 1, "episode_count": 9, "air_date": "2015-01-23"}]}
            elif tv_id == 68156:
                return {"id": 68156, "name": "三时三餐 旌善篇", "seasons": [{"season_number": 1, "episode_count": 11, "air_date": "2014-10-17"}]}
            return {}

        mock_tmdb.get_tv_detail.side_effect = get_tv_detail_mock

        async def run():
            resolved_id, seasons = await _resolve_show_metadata(
                show_name="三时三餐 (2014) [tmdbid=211502]",
                needed_seasons=[1],
                tmdb_id_num=211502,
                mp_client=mock_mp,
                tmdb_client=mock_tmdb,
            )
            # 因为 2014 年份匹配，68156 得分更高，必须优先被选中
            self.assertEqual(resolved_id, 68156)
            self.assertEqual(seasons[1]["total"], 11)

        asyncio.run(run())

    def test_10_tape_cut_extra_detection(self):
        """测试 Tape/Cut/母带/切片等花絮被精准识别为 is_extra."""
        files = [
            "Sister.Take.Over.S01E20.4k.Tape.01.2160p.WEB-DL.H265.AAC-OurTV.mp4",
            "Sister.Take.Over.S01E25.4k.Cut.01.2160p.WEB-DL.H265.AAC-OurTV.mp4",
            "Show.S02E01.切片.1080p.mp4",
            "Show.S01E05.母带版.mkv",
            "Show.S01E11.加更版.mp4",
            "Show.S01E12.纯享版.mp4",
        ]
        for f in files:
            ep = parse_episode(f, f"/path/{f}", 10000000)
            self.assertTrue(ep.is_extra, f"文件 {f} 应当被识别为花絮 (is_extra=True)")

    def test_11_separate_outliers(self):
        """测试断崖式孤立集号剥离."""
        from app.core.inspector import separate_outliers

        # 案例1: 盒子里的猫 [1..12, 56]，expected_max=10
        eps = list(range(1, 13)) + [56]
        regular, outliers = separate_outliers(eps, expected_max=10)
        self.assertEqual(regular, list(range(1, 13)))
        self.assertEqual(outliers, [56])

        # 案例2: 正常连续剧集 [1..12]，expected_max=12
        regular2, outliers2 = separate_outliers(list(range(1, 13)), expected_max=12)
        self.assertEqual(regular2, list(range(1, 13)))
        self.assertEqual(outliers2, [])

        # 案例3: 缺集剧集 [1, 24]，expected_max=24（官方正片内的跳号，绝不误剥离）
        regular3, outliers3 = separate_outliers([1, 24], expected_max=24)
        self.assertEqual(regular3, [1, 24])
        self.assertEqual(outliers3, [])

    def test_12_diagnose_season_bonus_specials_tolerance(self):
        """测试正片齐全外加 1~4 集收官特辑/加更期直接判定为 archive_ready."""
        # 官方 10 集，本地 1..10 齐全，附带 11, 12 两集加更特辑
        status, detail = diagnose_season(
            recognized=list(range(1, 13)),
            total_episodes=10,
            season_finished=True,
            has_duplicates=False,
            allow_bonus_specials=True,
        )
        self.assertEqual(status, "archive_ready")
        self.assertIn("全 10 集齐全", detail)
        self.assertIn("加更/特辑", detail)
        self.assertIn("可归档", detail)


class TestSeasonSourceReconciliation(unittest.IsolatedAsyncioTestCase):
    async def scan(self, files, *, bound=True, details=None, candidates=(), primary=10,
                   series="小小少年", category="/src", year=None, season_entry=False):
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.core.inspector import run_inspection
        tid = 256080 if bound else 999
        show = f"{category}/{series} (2024) [tmdbid={tid}]"
        subject = {"douban_id": "37116072", "title": "小小少年 第二季", "season": 2,
                   "seasons": {"2": list(range(1, 13))}, "year": "2025"}
        disk = AsyncMock()
        disk.list_files.side_effect = lambda path: (
            [{"name": "Season 2", "path": show + "/Season 2", "isDirectory": True}] if path == show
            else [{"name": f, "path": path + "/" + f, "size": 100, "isDirectory": False} for f in files])
        mp = AsyncMock()
        mp.search_media.return_value = list(candidates)
        lookup = details if details is not None else {"37116072": subject}
        mp.get_douban_media_info.side_effect = lambda did, **kwargs: lookup.get(did, {})
        http = MagicMock()
        http.__aenter__.return_value.get = AsyncMock(return_value=MagicMock(status_code=503))
        with patch("app.core.inspector._fetch_seasons_by_tmdb_id", new=AsyncMock(
                return_value={2: {"total": primary, "finished": True, "season_year": year}})), patch(
                "app.core.inspector.httpx.AsyncClient", return_value=http):
            out = await run_inspection("case", show + "/Season 2/" if season_entry else show, cd2_client=disk, mp_client=mp, tmdb_client=AsyncMock())
        if bound:
            mp.search_media.assert_not_awaited()
        return out.seasons[0]

    async def test_category_with_loose_trailer_and_season_in_show_title(self):
        from app.core.inspector import _discover_shows
        from unittest.mock import AsyncMock
        root = "/收藏/其他分类"
        subdirs = ["Special Forces (2022) [tmdbid=12]", "Another Season 2 (2020) [tmdbid=34]"]
        disk = AsyncMock()
        async def read(path):
            if path == root:
                return [{"path": root + "/" + n, "name": n, "isDirectory": True} for n in subdirs] + [
                    {"path": root + "/预告.mkv", "name": "预告.mkv", "isDirectory": False}]
            return [{"path": path + "/Season 1", "name": "Season 1", "isDirectory": True}]
        disk.list_files.side_effect = read
        found = []
        await _discover_shows(disk, root, found)
        self.assertEqual([x["name"] for x in found], subdirs)

    async def test_direct_season_entry_keeps_parent_identity(self):
        files = ["Example.S02E01-E12.mkv"]
        from_show = await self.scan(files, category="/其他库/节目")
        from_season = await self.scan(files, category="/其他库/节目", season_entry=True)
        self.assertEqual(from_show, from_season)
        self.assertEqual(from_season.season_number, 2)

    async def test_short_english_series_outside_variety_directory(self):
        detail = {"douban_id": "91", "title": "Autumn Coast Season 2", "season": 2,
                  "number_of_episodes": 8, "year": "2025"}
        d = await self.scan(["Autumn.Coast.S02E01-E04.mkv"], bound=False, series="Autumn Coast",
                            category="/任意盘/海外短剧", primary=7, year="2025",
                            details={"91": detail}, candidates=[{"id": "91", "title": detail["title"]}])
        self.assertEqual((d.total_episodes, d.missing_episodes), (8, [5, 6, 7, 8]))

    async def test_fallback_keeps_douban_and_tmdb_ids_separate(self):
        import logging
        from unittest.mock import AsyncMock
        from app.core.inspector import _fetch_tmdb_via_mp
        mp = AsyncMock()
        mp.search_media.side_effect = [[{"title": "示例", "year": "2024", "id": "777"}],
                                       [{"title": "同名误匹配", "year": "2024", "id": "33"},
                                        {"title": "示例", "year": "2024", "id": "88"}]]
        tid, _ = await _fetch_tmdb_via_mp(mp, "示例 (2024)", logging.getLogger(__name__))
        self.assertEqual(tid, 88)
        mp.get_tmdb_seasons.assert_awaited_once_with(88)

    async def test_numeric_sequel_requires_matching_season_year(self):
        detail = {"douban_id": "92", "title": "海岸来信2", "year": "2026",
                  "seasons": {"1": list(range(1, 13))}}
        for year, total in [("2026", 12), ("2023", 10)]:
            d = await self.scan(["Show.S02E01-E08.mkv"], bound=False, series="海岸来信", category="/收藏/自定义",
                                primary=10, year=year, details={"92": detail},
                                candidates=[{"id": "92", "title": detail["title"]}])
            self.assertEqual(d.total_episodes, total)

    async def test_partial_and_future_schedules_do_not_imply_finished(self):
        from app.core.inspector import _fetch_seasons_by_tmdb_id
        from unittest.mock import AsyncMock
        mp = AsyncMock()
        mp.get_tmdb_seasons.return_value = [{"season_number": 1, "air_date": "2019-01-01", "episode_count": 8}]
        mp.get_tmdb_episodes.return_value = [{"episode_number": n, "name": f"Ep{n}", "air_date": "2020-01-01"}
                                             for n in range(1, 5)]
        d = (await _fetch_seasons_by_tmdb_id(mp, 123, None))[1]
        self.assertEqual((d["total"], d["finished"], d["partial_listing"]), (8, False, True))
        mp.get_tmdb_episodes.return_value *= 2
        mp.get_tmdb_episodes.return_value[-1] = {"episode_number": 8, "name": "Final", "air_date": "2099-01-01"}
        self.assertFalse((await _fetch_seasons_by_tmdb_id(mp, 123, None))[1]["finished"])

    async def test_verified_link_with_empty_search_and_duplicate(self):
        files = [f"Show.S02E{i:02}.H264.mp4" for i in range(1, 13)] + ["Show.S02E02.H265.mp4"]
        d = await self.scan(files)
        self.assertEqual((d.status, d.total_episodes, d.local_episode_count), ("review_needed", 12, 13))
        self.assertEqual((d.duplicate_episodes, d.missing_episodes), ([2], []))
        self.assertIn("37116072", d.status_detail)

    async def test_missing_episode_and_later_total_are_read_from_detail(self):
        d = await self.scan(["X.S02E01-E10.mkv", "X.S02E12.mkv"], details={"37116072": {
            "season": 2, "seasons": {"2": list(range(1, 14))}}})
        self.assertEqual((d.total_episodes, d.missing_episodes), (13, [11, 13]))

    async def test_unbound_correct_season_found_despite_small_difference(self):
        d = await self.scan(["X.S02E01-E12.mkv"], bound=False,
                            candidates=[{"id": "37116072", "title": "小小少年 第二季"}])
        self.assertEqual((d.status, d.total_episodes), ("archive_ready", 12))

    async def test_larger_source_supported_in_either_direction(self):
        d = await self.scan(["X.S02E01-E12.mkv"], primary=8)
        self.assertEqual((d.total_episodes, d.status), (12, "archive_ready"))
        d = await self.scan(["X.S02E01-E20.mkv"], primary=20)
        self.assertEqual((d.total_episodes, d.status), (20, "archive_ready"))

    async def test_unverified_overflow_never_becomes_bonus(self):
        for details in ({}, {"37116072": {"season": 1, "seasons": {"1": list(range(1, 13))}}}):
            d = await self.scan(["X.S02E01-E12.mkv"], details=details)
            self.assertEqual((d.status, d.total_episodes), ("review_needed", 10))
            self.assertIn("[11, 12]", d.status_detail)
        d = await self.scan(["X.S02E01-E10.mkv", "X.S02E11.加更.mp4"], details={})
        self.assertEqual((d.status, d.recognized_episodes), ("archive_ready", list(range(1, 11))))

    async def test_wrong_title_or_ambiguous_same_season_is_not_adopted(self):
        files = ["X.S02E01-E12.mkv"]
        for titles in [("另一节目 第二季",), ("小小少年 第二季", "小小少年 第二季")]:
            details = {str(i+1): {"title": name, "season": 2, "number_of_episodes": 12}
                       for i, name in enumerate(titles)}
            candidates = [{"id": did, "title": item["title"]} for did, item in details.items()]
            d = await self.scan(files, bound=False, details=details, candidates=candidates)
            self.assertEqual((d.status, d.total_episodes), ("review_needed", 10))


class TestArchiveEvidence(unittest.IsolatedAsyncioTestCase):
    async def inspect(self, source, target, *, total=13, alias=False, ambiguous=False, fail=False):
        from unittest.mock import AsyncMock, patch
        from app.core.inspector import run_inspection
        show = "高度潜力 (2024) [tmdbid=226637]"
        src = "/src/tv/" + show
        archived_name = "潜能探案组 (2024) [tmdbid=226637]" if alias else show
        dst = "/dst/tv/" + archived_name
        def entries(parent, names, directory=False):
            return [dict(name=n, path=f"{parent}/{n}", size=size, isDirectory=directory) for n, size in names]
        catalogue = [(archived_name, 0)]
        if ambiguous:
            catalogue.append(("High Potential [tmdbid=226637]", 0))
        dirs = {
            src: entries(src, [("Season 1", 0)], True),
            src + "/Season 1": entries(src + "/Season 1", source),
            "/dst/tv": entries("/dst/tv", catalogue, True),
            dst: entries(dst, [("S01", 0)], True),
            dst + "/S01": entries(dst + "/S01", target),
        }
        async def read(path):
            if fail and path == dst + "/S01":
                raise TimeoutError("fixture timeout")
            return dirs[path]
        disk = AsyncMock()
        disk.list_files.side_effect = read
        with patch("app.core.inspector._resolve_show_metadata", new=AsyncMock(
                return_value=(226637, {1: {"total": total, "finished": True}}))):
            result = await run_inspection("case", src, cd2_client=disk, tmdb_client=AsyncMock(),
                                          archive_root="/dst", global_source_root="/src")
        self.assertEqual(len(result.seasons), 1)
        disk.delete_file.assert_not_awaited()
        disk.move_file.assert_not_awaited()
        return result.seasons[0]

    async def test_sidecars_only_full_archive(self):
        files = [(f"High.Potential.S01E{i:02}.mkv", 100) for i in range(1, 14)]
        d = await self.inspect([("season.nfo", 8), ("S01E01.jpg", 9), ("S01E01.nfo", 4)], files)
        self.assertEqual((d.status, d.total_episodes, d.local_episode_count), ("verified_complete", 13, 0))
        self.assertEqual((d.recognized_episodes, d.missing_episodes), ([], []))
        self.assertIn("已完成", d.status_detail)
        self.assertIn("只留元数据附件", d.status_detail)

    async def test_incomplete_archive_does_not_count_wrong_season_empty_or_extra(self):
        files = [("S01E01.mkv", 10), ("S02E02.mkv", 10), ("S01E03.mkv", 0), ("Clip.S01E04.mkv", 10)]
        d = await self.inspect([("mystery.mkv", 10)], files, total=4)
        self.assertEqual(d.missing_episodes, [2, 3, 4])
        self.assertIn("1/4", d.status_detail)
        self.assertIn("未能从文件名解析出集号（如 mystery.mkv）", d.status_detail)

    async def test_alias_same_id_and_season_is_read_only(self):
        d = await self.inspect([("S01E01.mkv", 100)], [("S01E01.mkv", 100)], total=1, alias=True)
        self.assertEqual(d.status, "already_archived")
        self.assertIn("别名", d.status_detail)
        self.assertIn("潜能探案组", d.archive_target_path)
        self.assertEqual(d.missing_episodes, [])

    async def test_ambiguous_alias_and_failed_read(self):
        for kwargs, message in [({"ambiguous": True}, "多个"), ({"fail": True}, "未完成")]:
            d = await self.inspect([], [], **kwargs)
            self.assertIn(message, d.status_detail)
            self.assertNotIn("已覆盖全部", d.status_detail)

    async def test_missing_metadata_does_not_imply_complete(self):
        d = await self.inspect([], [("S01E01.mkv", 5)], total=None)
        self.assertIn("资料总集数未知", d.status_detail)
        self.assertNotIn("已覆盖全部", d.status_detail)


if __name__ == "__main__":
    unittest.main()
