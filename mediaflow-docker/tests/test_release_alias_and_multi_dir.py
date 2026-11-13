"""验收测试套件: 外文发行名误报消除、真错配保留、只读归属重评估与同 TMDB 多目录组级确认门禁。"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="release-alias-mdir-")
atexit.register(lambda: shutil.rmtree(_TMP_DATA_DIR, ignore_errors=True))
os.environ.update(
    MF_DATA_DIR=_TMP_DATA_DIR,
    MF_SOURCE_ROOT="/qa/src",
    MF_ARCHIVE_ROOT="/qa/dst",
    MF_LIBRARY_ROOT="/qa/dst",
    MF_AUTH_TOKEN="fixture",
    MF_CD2_URL="http://cd2.mock",
    MF_CD2_TOKEN="cd2tok",
    MF_MP_URL="http://mp.mock",
    MF_MP_TOKEN="mptok",
    MF_TMDB_API_KEY="tmdbtok",
    MF_ENABLE_MULTI_DIR_GOVERNANCE="true",
)

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.clients import clouddrive2 as cd2_mod  # noqa: E402
from app.clients import moviepilot as mp_mod  # noqa: E402
from app.core.directory_governance import (  # noqa: E402
    build_physical_dir_entry,
    classify_tmdb_directory_group,
    is_substantive_alias_dir,
)
from app.core.identity_resolver import (  # noqa: E402
    analyze_season_release_signals,
    reevaluate_offline_conflict,
)
from app.core.inspector import run_inspection  # noqa: E402
from app.database import Base, async_session, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.directory_group import DirectoryGroup  # noqa: E402
from app.models.gap_fill import GapFillEpisode  # noqa: E402
from app.models.metadata_cache import MetadataCache  # noqa: E402
from app.models.season import SeasonRecord  # noqa: E402
from app.models.season_mapping import SeasonMapping  # noqa: E402
from app.models.task import Task  # noqa: E402


class MockCD2:
    def __init__(self, tree: dict[str, list[dict]]):
        self.tree = tree
        self.move_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []

    async def list_files(self, path: str, force_refresh: bool = False):
        norm = path.rstrip("/") or "/"
        if norm in self.tree:
            return list(self.tree[norm])
        raise FileNotFoundError(norm)

    async def mkdir(self, path: str):
        self.tree.setdefault(path.rstrip("/"), [])
        return True

    async def move_file(self, src: str, dst: str, skip_existing: bool = True):
        self.move_calls.append((src, dst))
        return {"ok": True}

    async def delete_file(self, path: str):
        self.delete_calls.append(path)
        return True

    async def close(self):
        return None


class MockMP:
    def __init__(self, seasons_by_tid=None, eps_by_key=None, subs=None, ms_remote_by_tid=None):
        self.seasons_by_tid = seasons_by_tid or {}
        self.eps_by_key = eps_by_key or {}
        self.subs = subs or []
        self.ms_remote_by_tid = ms_remote_by_tid
        self.deleted_subs: list[int] = []
        self.ms_calls: list[int] = []

    async def check_mediaserver_exists_remote(self, tmdb_id: int, title: str = "", year: str = "", media_type: str = "电视剧"):
        self.ms_calls.append(int(tmdb_id))
        if self.ms_remote_by_tid is None:
            return {"checked": False, "season_episodes": {}, "error": "mock_unconfigured"}
        return self.ms_remote_by_tid.get(int(tmdb_id), {"checked": True, "season_episodes": {}, "error": ""})

    async def get_tmdb_seasons(self, tmdb_id: int):
        return self.seasons_by_tid.get(int(tmdb_id))

    async def get_tmdb_episodes(self, tmdb_id: int, season: int):
        return self.eps_by_key.get((int(tmdb_id), int(season)))

    async def list_subscriptions(self):
        return list(self.subs)

    async def delete_subscription(self, sub_id: int):
        self.deleted_subs.append(int(sub_id))
        return True

    async def get_downloading(self):
        return []

    async def get_active_downloads(self):
        return []

    async def get_transfer_queue(self):
        return []

    async def search_resources(self, tmdb_id: int | None = None, season: int | None = None, title: str = ""):
        return []

    async def list_transfer_history(self, title: str = ""):
        return []

    async def search_media(self, keyword: str):
        return []

    async def close(self):
        return None


class MockTMDB:
    def __init__(self, details: dict[int, dict], season_eps=None, searches=None, aliases=None, fail_aliases: bool = False):
        self.details = details
        self.season_eps = season_eps or {}
        self.searches = searches or {}
        self.aliases = aliases or {}
        self.fail_aliases = fail_aliases

    async def fetch_tv_detail(self, tv_id: int, language: str | None = None):
        if int(tv_id) in self.details:
            return dict(self.details[int(tv_id)]), "ok", None
        return {}, "not_found", "404"

    async def fetch_tv_aliases(self, tv_id: int):
        if self.fail_aliases:
            raise TimeoutError("TMDB translations timeout")
        return dict(self.aliases.get(int(tv_id), {"aliases": [], "en_name": "", "translations_ok": False}))

    async def fetch_season_detail(self, tv_id: int, season_number: int, language: str | None = None):
        eps = self.season_eps.get((int(tv_id), int(season_number)), [])
        return ({"episodes": eps}, "ok", None) if eps else ({}, "not_found", "404")

    async def fetch_search_tv(self, query: str, language: str | None = None):
        for k, v in self.searches.items():
            if k.casefold() in query.casefold() or query.casefold() in k.casefold():
                return list(v), "ok", None
        return [], "ok", None

    async def get_tv_detail(self, tv_id: int):
        d, _, _ = await self.fetch_tv_detail(tv_id)
        return d

    async def search_tv(self, query: str):
        r, _, _ = await self.fetch_search_tv(query)
        return r

    async def close(self):
        return None


def _file(name: str, parent: str, size: int = 1024 * 1024):
    return {"name": name, "path": f"{parent}/{name}", "isDirectory": False, "size": size}


def _dir(name: str, parent: str, stable_id: str = ""):
    d = {"name": name, "path": f"{parent}/{name}", "isDirectory": True, "size": 0}
    if stable_id:
        d["id"] = stable_id
    return d


class ReleaseAliasAndMultiDirTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await init_db()

    # ── Batch 1: 外文发行名、别名与真错配测试 ──────────────────────────

    async def test_korean_and_arbitrary_foreign_release_names_do_not_false_positive_even_when_aliases_fail(self):
        src_cat = "/qa/src/韩国剧集"
        cd2 = MockCD2(
            {
                src_cat: [
                    _dir("Boss的甜蜜诱惑 (2026) [tmdbid=326975]", src_cat),
                    _dir("不是你的恋爱 (2026) [tmdbid=314939]", src_cat),
                    _dir("不着边际的你 (2026) [tmdbid=334517]", src_cat),
                    _dir("最后的餐桌 (2025) [tmdbid=316702]", src_cat),
                    _dir("未预设测试韩剧 (2026) [tmdbid=999888]", src_cat),
                ],
                f"{src_cat}/Boss的甜蜜诱惑 (2026) [tmdbid=326975]": [_dir("Season 1", f"{src_cat}/Boss的甜蜜诱惑 (2026) [tmdbid=326975]")],
                f"{src_cat}/Boss的甜蜜诱惑 (2026) [tmdbid=326975]/Season 1": [
                    _file("What.a.Boss.Wants.S01E01.1080p.WEB-DL.mkv", f"{src_cat}/Boss的甜蜜诱惑 (2026) [tmdbid=326975]/Season 1"),
                    _file("What.a.Boss.Wants.S01E02.1080p.WEB-DL.mkv", f"{src_cat}/Boss的甜蜜诱惑 (2026) [tmdbid=326975]/Season 1"),
                ],
                f"{src_cat}/不是你的恋爱 (2026) [tmdbid=314939]": [_dir("Season 1", f"{src_cat}/不是你的恋爱 (2026) [tmdbid=314939]")],
                f"{src_cat}/不是你的恋爱 (2026) [tmdbid=314939]/Season 1": [
                    _file("A.Love.Other.Than.Yours.S01E01.1080p.WEB-DL.mkv", f"{src_cat}/不是你的恋爱 (2026) [tmdbid=314939]/Season 1"),
                    _file("A.Love.Other.Than.Yours.S01E02.1080p.WEB-DL.mkv", f"{src_cat}/不是你的恋爱 (2026) [tmdbid=314939]/Season 1"),
                ],
                f"{src_cat}/不着边际的你 (2026) [tmdbid=334517]": [_dir("Season 1", f"{src_cat}/不着边际的你 (2026) [tmdbid=334517]")],
                f"{src_cat}/不着边际的你 (2026) [tmdbid=334517]/Season 1": [
                    _file("Oh.My.Guard.S01E01.1080p.WEB-DL.mkv", f"{src_cat}/不着边际的你 (2026) [tmdbid=334517]/Season 1"),
                    _file("Oh.My.Guard.S01E02.1080p.WEB-DL.mkv", f"{src_cat}/不着边际的你 (2026) [tmdbid=334517]/Season 1"),
                ],
                f"{src_cat}/最后的餐桌 (2025) [tmdbid=316702]": [_dir("Season 1", f"{src_cat}/最后的餐桌 (2025) [tmdbid=316702]")],
                f"{src_cat}/最后的餐桌 (2025) [tmdbid=316702]/Season 1": [
                    _file("[最后一桌] Final Table.S01E01.1080p.WEB-DL.mkv", f"{src_cat}/最后的餐桌 (2025) [tmdbid=316702]/Season 1"),
                    _file("国际名厨争霸赛.The.Final.Table.S01E02.1080p.WEB-DL.mkv", f"{src_cat}/最后的餐桌 (2025) [tmdbid=316702]/Season 1"),
                ],
                f"{src_cat}/未预设测试韩剧 (2026) [tmdbid=999888]": [_dir("Season 1", f"{src_cat}/未预设测试韩剧 (2026) [tmdbid=999888]")],
                f"{src_cat}/未预设测试韩剧 (2026) [tmdbid=999888]/Season 1": [
                    _file("Arbitrary.English.Alias.S01E01.1080p.WEB-DL.mkv", f"{src_cat}/未预设测试韩剧 (2026) [tmdbid=999888]/Season 1"),
                    _file("Arbitrary.English.Alias.S01E02.1080p.WEB-DL.mkv", f"{src_cat}/未预设测试韩剧 (2026) [tmdbid=999888]/Season 1"),
                ],
                "/qa/dst/韩国剧集": [],
            }
        )
        details = {
            326975: {"id": 326975, "name": "Boss的甜蜜诱惑", "original_name": "보스의 유혹", "status": "Ended", "seasons": [{"season_number": 1, "episode_count": 2, "air_date": "2025-01-01"}]},
            314939: {"id": 314939, "name": "不是你的恋爱", "original_name": "너의 연애가 아니야", "status": "Ended", "seasons": [{"season_number": 1, "episode_count": 2, "air_date": "2025-01-01"}]},
            334517: {"id": 334517, "name": "不着边际的你", "original_name": "종잡을 수 없는 너", "status": "Ended", "seasons": [{"season_number": 1, "episode_count": 2, "air_date": "2025-01-01"}]},
            316702: {"id": 316702, "name": "最后的餐桌", "original_name": "최후의 식탁", "status": "Ended", "seasons": [{"season_number": 1, "episode_count": 2, "air_date": "2025-01-01"}]},
            999888: {"id": 999888, "name": "未预设测试韩剧", "original_name": "미정 제목", "status": "Ended", "seasons": [{"season_number": 1, "episode_count": 2, "air_date": "2025-01-01"}]},
        }
        # 故意模拟 translations/aliases 接口超时失败：依然不能误报 identity_review 或 mixed_works
        tmdb = MockTMDB(details=details, fail_aliases=True)
        mp = MockMP(seasons_by_tid={k: v["seasons"] for k, v in details.items()})

        async with async_session() as db:
            res = await run_inspection(
                task_id="t-kr-1",
                source_path=src_cat,
                cd2_client=cd2,
                mp_client=mp,
                tmdb_client=tmdb,
                archive_root="/qa/dst",
                global_source_root="/qa/src",
                db=db,
            )
            await db.commit()

        by_name = {s.show_name: s for s in res.seasons}
        self.assertEqual(len(res.seasons), 5)
        for sname, sdiag in by_name.items():
            self.assertEqual(sdiag.status, "archive_ready", f"{sname} 应正常 archive_ready，却得到 {sdiag.status}: {sdiag.status_detail}")
            self.assertFalse((sdiag.conflict or {}).get("blocking"), f"{sname} 不应有阻塞冲突")

    async def test_true_misplacement_gravesend_kingdom_and_explicit_file_tmdbid_preserved(self):
        src_cat = "/qa/src/欧美剧集"
        cd2 = MockCD2(
            {
                src_cat: [
                    _dir("Gravesend (2021) [tmdbid=97125]", src_cat),
                    _dir("王国 (2014) [tmdbid=61427]", src_cat),
                    _dir("Boss的甜蜜诱惑 (2026) [tmdbid=326975]", src_cat),
                ],
                f"{src_cat}/Gravesend (2021) [tmdbid=97125]": [_dir("Season 8", f"{src_cat}/Gravesend (2021) [tmdbid=97125]")],
                f"{src_cat}/Gravesend (2021) [tmdbid=97125]/Season 8": [
                    _file("Gravesend - S08E01 - The.Neighborhood.S08E01.Welcome.to.the.Neighborhood.1080p.mkv", f"{src_cat}/Gravesend (2021) [tmdbid=97125]/Season 8"),
                ],
                f"{src_cat}/王国 (2014) [tmdbid=61427]": [_dir("Season 6", f"{src_cat}/王国 (2014) [tmdbid=61427]")],
                f"{src_cat}/王国 (2014) [tmdbid=61427]/Season 6": [
                    _file("Kingdom.S06E01.2025.1080p.Baha.WEB-DL.mkv", f"{src_cat}/王国 (2014) [tmdbid=61427]/Season 6"),
                ],
                f"{src_cat}/Boss的甜蜜诱惑 (2026) [tmdbid=326975]": [_dir("Season 1", f"{src_cat}/Boss的甜蜜诱惑 (2026) [tmdbid=326975]")],
                f"{src_cat}/Boss的甜蜜诱惑 (2026) [tmdbid=326975]/Season 1": [
                    _file("Wrong.Show.[tmdbid=888888].S01E01.1080p.mkv", f"{src_cat}/Boss的甜蜜诱惑 (2026) [tmdbid=326975]/Season 1"),
                ],
                "/qa/dst/欧美剧集": [],
            }
        )
        details = {
            97125: {"id": 97125, "name": "Gravesend", "original_name": "Gravesend", "status": "Ended", "seasons": [{"season_number": 1, "episode_count": 8}, {"season_number": 2, "episode_count": 8}, {"season_number": 3, "episode_count": 8}]},
            61427: {"id": 61427, "name": "搏击王国", "original_name": "Kingdom", "status": "Ended", "seasons": [{"season_number": 1, "episode_count": 10}, {"season_number": 2, "episode_count": 20}, {"season_number": 3, "episode_count": 10}]},
            79001: {"id": 79001, "name": "左邻右舍", "original_name": "The Neighborhood", "first_air_date": "2018-10-01", "status": "Returning Series", "seasons": [{"season_number": 8, "episode_count": 20}]},
            210006: {"id": 210006, "name": "王者天下", "original_name": "킹덤", "first_air_date": "2025-10-04", "status": "Returning Series", "seasons": [{"season_number": 6, "episode_count": 13}]},
            326975: {"id": 326975, "name": "Boss的甜蜜诱惑", "original_name": "보스의 유혹", "status": "Ended", "seasons": [{"season_number": 1, "episode_count": 12}]},
        }
        tmdb = MockTMDB(
            details=details,
            season_eps={(79001, 8): [{"episode_number": 1, "name": "Welcome to the Neighborhood"}]},
            searches={
                "The Neighborhood": [details[79001]],
                "Kingdom": [details[210006]],
            },
        )
        mp = MockMP(seasons_by_tid={k: v["seasons"] for k, v in details.items()})

        async with async_session() as db:
            res = await run_inspection(
                task_id="t-real-conflict",
                source_path=src_cat,
                cd2_client=cd2,
                mp_client=mp,
                tmdb_client=tmdb,
                archive_root="/qa/dst",
                global_source_root="/qa/src",
                db=db,
            )

        by_name = {s.show_name: s for s in res.seasons}
        self.assertEqual(by_name["Gravesend (2021) [tmdbid=97125]"].status, "identity_review")
        self.assertEqual(by_name["Gravesend (2021) [tmdbid=97125]"].conflict.get("kind"), "identity_conflict")
        self.assertEqual(by_name["王国 (2014) [tmdbid=61427]"].status, "identity_review")
        self.assertEqual(by_name["王国 (2014) [tmdbid=61427]"].conflict.get("kind"), "ambiguous_title")
        # 显式文件内 [tmdbid=888888] 与目录 [326975] 冲突依然必须拦截
        self.assertEqual(by_name["Boss的甜蜜诱惑 (2026) [tmdbid=326975]"].status, "identity_review")

    async def test_revoke_mapping_and_readonly_reevaluate_clear_stale_false_positive_without_mutating_handled_or_gapfill(self):
        async with async_session() as db:
            task = Task(id="t-reeval", name="韩国剧集", source_path="/qa/src/韩国剧集", media_type="tv", status="completed")
            db.add(task)
            db.add(
                MetadataCache(
                    cache_key="tmdb_tv:326975",
                    source="tmdb_tv",
                    media_id="326975",
                    payload_json=json.dumps({"tmdb_id": 326975, "name": "Boss的甜蜜诱惑", "original_name": "보스의 유혹", "seasons": [1]}),
                    verified=True,
                    status="ok",
                )
            )
            db.add(
                MetadataCache(
                    cache_key="tmdb_season:326975:S1",
                    source="tmdb_season",
                    media_id="326975",
                    season_number=1,
                    payload_json=json.dumps({"total": 12, "finished": True}),
                    verified=True,
                    status="ok",
                )
            )
            # 1) 旧误报 + 用户映射（含 stale previous_conflict）：撤销时应按新规则清空误报，绝不恢复假冲突
            r_mapped = SeasonRecord(
                id="r-mapped",
                task_id="t-reeval",
                show_name="Boss的甜蜜诱惑 (2026) [tmdbid=326975]",
                show_path="/qa/src/韩国剧集/Boss的甜蜜诱惑 (2026) [tmdbid=326975]",
                tmdb_id="326975",
                season_number=1,
                season_path="/qa/src/韩国剧集/Boss的甜蜜诱惑 (2026) [tmdbid=326975]/Season 1",
                total_episodes=12,
                local_episode_count=12,
                recognized_episodes_json=json.dumps(list(range(1, 13))),
                missing_episodes_json="[]",
                duplicate_episodes_json="[]",
                status="archive_ready",
                status_detail="已按单季映射重算",
                season_finished=True,
                evidence_json=json.dumps({
                    "matched_work": {"tmdb_id": 326975, "name": "Boss的甜蜜诱惑", "original_name": "보스의 유혹", "seasons_present": [1]},
                    "release_evidence": {"dominant_release_title": "What a Boss Wants", "sample_files": ["What.a.Boss.Wants.S01E01.1080p.mkv"]},
                }),
                conflict_json=json.dumps({
                    "blocking": False,
                    "resolved_by_mapping": True,
                    "previous_conflict": {"kind": "identity_conflict", "blocking": True, "reason": "旧版误报 What a Boss Wants", "sample_files": ["What.a.Boss.Wants.S01E01.1080p.mkv"]},
                }),
                mapping_json=json.dumps({"target_tmdb_id": 326975, "target_season": 1}),
            )
            db.add(SeasonMapping(scope_key=r_mapped.season_path, source_tmdb_id=326975, source_season=1, show_path=r_mapped.show_path, season_path=r_mapped.season_path, target_tmdb_id=326975, target_season=1, is_active=True))

            # 2) 旧双语前缀混装误报（[最后一桌] Final Table vs 国际名厨争霸赛 The Final Table）：重评估应自动洗掉误报
            r_mixed_fp = SeasonRecord(
                id="r-mixed-fp",
                task_id="t-reeval",
                show_name="最后的餐桌 (2025) [tmdbid=316702]",
                show_path="/qa/src/韩国剧集/最后的餐桌 (2025) [tmdbid=316702]",
                tmdb_id="316702",
                season_number=1,
                season_path="/qa/src/韩国剧集/最后的餐桌 (2025) [tmdbid=316702]/Season 1",
                total_episodes=2,
                local_episode_count=2,
                recognized_episodes_json="[1,2]",
                missing_episodes_json="[]",
                duplicate_episodes_json="[]",
                status="identity_review",
                status_detail="旧版误报 mixed_works",
                season_finished=True,
                evidence_json=json.dumps({
                    "matched_work": {"tmdb_id": 316702, "name": "最后的餐桌", "original_name": "최후의 식탁", "seasons_present": [1]},
                    "release_evidence": {"distinct_release_titles": ["[最后一桌] Final Table", "国际名厨争霸赛 The Final Table"], "sample_files": ["[最后一桌] Final Table S01E01 1080p.mkv", "国际名厨争霸赛.The.Final.Table.S01E02.1080p.mkv"]},
                }),
                conflict_json=json.dumps({"kind": "mixed_works", "blocking": True, "sample_files": ["[最后一桌] Final Table S01E01 1080p.mkv", "国际名厨争霸赛.The.Final.Table.S01E02.1080p.mkv"]}),
            )

            # 3) 真实错配 Gravesend S08：重评估时应继续保留 identity_review
            r_true_bad = SeasonRecord(
                id="r-true-bad",
                task_id="t-reeval",
                show_name="Gravesend (2021) [tmdbid=97125]",
                show_path="/qa/src/韩国剧集/Gravesend (2021) [tmdbid=97125]",
                tmdb_id="97125",
                season_number=8,
                season_path="/qa/src/韩国剧集/Gravesend (2021) [tmdbid=97125]/Season 8",
                total_episodes=20,
                local_episode_count=1,
                recognized_episodes_json="[1]",
                missing_episodes_json="[]",
                duplicate_episodes_json="[]",
                status="identity_review",
                status_detail="Gravesend 内混入 The Neighborhood",
                season_finished=True,
                evidence_json=json.dumps({"matched_work": {"tmdb_id": 97125, "name": "Gravesend", "original_name": "Gravesend", "seasons_present": [1, 2, 3]}}),
                conflict_json=json.dumps({"kind": "identity_conflict", "blocking": True, "sample_files": ["Gravesend - S08E01 - The.Neighborhood.S08E01.Welcome.to.the.Neighborhood.1080p.mkv"], "episode_title_matches": [{"episode": 1}]}),
            )

            # 4) manually_handled 与 GapFillEpisode 回执均应原样保留
            r_handled = SeasonRecord(
                id="r-handled",
                task_id="t-reeval",
                show_name="人工已完成剧",
                show_path="/qa/src/韩国剧集/人工已完成剧",
                tmdb_id="1001",
                season_number=1,
                season_path="/qa/src/韩国剧集/人工已完成剧/Season 1",
                total_episodes=0,
                local_episode_count=0,
                recognized_episodes_json="[]",
                missing_episodes_json="[]",
                duplicate_episodes_json="[]",
                status="manually_handled",
                status_detail="已人工确认完成",
            )
            g_ep = GapFillEpisode(
                task_id="t-reeval",
                season_record_id="r-mixed-fp",
                season_path=r_mixed_fp.season_path,
                tmdb_id=316702,
                season_number=1,
                episode_number=2,
                state="accepted",
                outcome_code="submitted",
                identity_key="tmdb:316702|S1|st:316702:S1:nm:standard:0",
            )
            db.add_all([r_mapped, r_mixed_fp, r_true_bad, r_handled, g_ep])
            await db.commit()

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            headers = {"Authorization": "Bearer fixture"}
            # 1) 撤销 r-mapped 映射：不能复活误报冲突，状态应为 archive_ready
            rev_resp = await client.delete("/api/tasks/t-reeval/seasons/r-mapped/season-mapping", headers=headers)
            self.assertEqual(rev_resp.status_code, 200, rev_resp.text)
            rev_data = rev_resp.json()
            self.assertEqual(rev_data["season"]["status"], "archive_ready")
            self.assertFalse((rev_data["season"]["conflict"] or {}).get("blocking"))

            # 2) 一键只读重评估本任务归属提示
            re_resp = await client.post("/api/tasks/t-reeval/reevaluate", json={}, headers=headers)
            self.assertEqual(re_resp.status_code, 200, re_resp.text)
            re_data = re_resp.json()
            self.assertTrue(re_data["ok"])
            self.assertEqual(re_data["preserved_handled"], 1)

        async with async_session() as db:
            m_row = await db.get(SeasonRecord, "r-mixed-fp")
            b_row = await db.get(SeasonRecord, "r-true-bad")
            h_row = await db.get(SeasonRecord, "r-handled")
            ep_row = (await db.scalars(select(GapFillEpisode).where(GapFillEpisode.task_id == "t-reeval"))).first()
            self.assertEqual(m_row.status, "archive_ready")
            self.assertEqual(b_row.status, "identity_review")
            self.assertEqual(h_row.status, "manually_handled")
            self.assertIsNotNone(ep_row)
            self.assertEqual(ep_row.state, "accepted")

    # ── Batch 2: 同一 TMDB 多目录索引、6 大场景与写入/删除/退订门禁 ───────────

    async def test_all_six_multi_directory_scenarios_and_write_gates(self):
        # 规则单元验证：标点/大小写/全半角差异不算异名；同剧不同 TMDB 不合并；计算路径不进入物理索引
        self.assertFalse(is_substantive_alias_dir("Oh My Guard (2025) [tmdbid=120847]", "Oh! My Guard [tmdbid=120847]"))
        self.assertTrue(is_substantive_alias_dir("完美的谎言 (2026) [tmdbid=307478]", "谎言 (2026) [tmdbid=307478]"))

        single_src_only = classify_tmdb_directory_group([
            build_physical_dir_entry(side="source", category="cat:韩剧", show_path="/qa/src/韩剧/A [tmdbid=10]", tmdb_id=10, seasons=[1])
        ])
        self.assertIsNone(single_src_only)

        src_cat = "/qa/src/韩国剧集"
        dst_cat = "/qa/dst/韩国剧集"
        tree = {
            src_cat: [
                # 场景 1: 单源端 + 不存在的归档预期目录 -> 不报警
                _dir("单源预期归档 (2026) [tmdbid=700001]", src_cat),
                # 场景 2: 源端+归档同 ID 同名（含标点差异）-> 正常双库比对，不进入多目录确认
                _dir("Oh My Guard (2025) [tmdbid=700002]", src_cat),
                # 场景 3: 同侧 >=2 个物理目录同一 TMDB (307478)，且跨越 2 季 -> 仅合并 1 张组级卡
                _dir("完美的谎言 (2026) [tmdbid=307478]", src_cat),
                _dir("谎言 (2026) [tmdbid=307478]", src_cat),
                # 场景 4: 同 ID 异名双库目录 (src A vs dst B) -> 触发组级确认并拦截写入/退订
                _dir("旧名目录A (2025) [tmdbid=700004]", src_cat),
                # 场景 5: 同剧名不同 TMDB ID -> 各自独立，不串组
                _dir("重名作品 (2024) [tmdbid=700005]", src_cat),
                _dir("重名作品 (2024) [tmdbid=700006]", src_cat),
            ],
            f"{src_cat}/单源预期归档 (2026) [tmdbid=700001]": [_dir("Season 1", f"{src_cat}/单源预期归档 (2026) [tmdbid=700001]")],
            f"{src_cat}/单源预期归档 (2026) [tmdbid=700001]/Season 1": [_file("S01E01.mkv", f"{src_cat}/单源预期归档 (2026) [tmdbid=700001]/Season 1")],

            f"{src_cat}/Oh My Guard (2025) [tmdbid=700002]": [_dir("Season 1", f"{src_cat}/Oh My Guard (2025) [tmdbid=700002]")],
            f"{src_cat}/Oh My Guard (2025) [tmdbid=700002]/Season 1": [_file("Oh.My.Guard.S01E01.mkv", f"{src_cat}/Oh My Guard (2025) [tmdbid=700002]/Season 1")],

            f"{src_cat}/完美的谎言 (2026) [tmdbid=307478]": [
                _dir("Season 1", f"{src_cat}/完美的谎言 (2026) [tmdbid=307478]"),
                _dir("Season 2", f"{src_cat}/完美的谎言 (2026) [tmdbid=307478]"),
            ],
            f"{src_cat}/完美的谎言 (2026) [tmdbid=307478]/Season 1": [_file("S01E01.mkv", f"{src_cat}/完美的谎言 (2026) [tmdbid=307478]/Season 1")],
            f"{src_cat}/完美的谎言 (2026) [tmdbid=307478]/Season 2": [_file("S02E01.mkv", f"{src_cat}/完美的谎言 (2026) [tmdbid=307478]/Season 2")],

            f"{src_cat}/谎言 (2026) [tmdbid=307478]": [_dir("Season 1", f"{src_cat}/谎言 (2026) [tmdbid=307478]")],
            f"{src_cat}/谎言 (2026) [tmdbid=307478]/Season 1": [_file("S01E02.mkv", f"{src_cat}/谎言 (2026) [tmdbid=307478]/Season 1")],

            f"{src_cat}/旧名目录A (2025) [tmdbid=700004]": [_dir("Season 1", f"{src_cat}/旧名目录A (2025) [tmdbid=700004]")],
            f"{src_cat}/旧名目录A (2025) [tmdbid=700004]/Season 1": [_file("AliasShow.S01E01.1080p.mkv", f"{src_cat}/旧名目录A (2025) [tmdbid=700004]/Season 1")],

            f"{src_cat}/重名作品 (2024) [tmdbid=700005]": [_dir("Season 1", f"{src_cat}/重名作品 (2024) [tmdbid=700005]")],
            f"{src_cat}/重名作品 (2024) [tmdbid=700005]/Season 1": [_file("S01E01.mkv", f"{src_cat}/重名作品 (2024) [tmdbid=700005]/Season 1")],
            f"{src_cat}/重名作品 (2024) [tmdbid=700006]": [_dir("Season 1", f"{src_cat}/重名作品 (2024) [tmdbid=700006]")],
            f"{src_cat}/重名作品 (2024) [tmdbid=700006]/Season 1": [_file("S01E01.mkv", f"{src_cat}/重名作品 (2024) [tmdbid=700006]/Season 1")],

            dst_cat: [
                _dir("Oh! My Guard [tmdbid=700002]", dst_cat),
                _dir("正名归档B (2025) [tmdbid=700004]", dst_cat),
            ],
            f"{dst_cat}/Oh! My Guard [tmdbid=700002]": [_dir("Season 1", f"{dst_cat}/Oh! My Guard [tmdbid=700002]")],
            f"{dst_cat}/Oh! My Guard [tmdbid=700002]/Season 1": [_file("Oh.My.Guard.S01E01.mkv", f"{dst_cat}/Oh! My Guard [tmdbid=700002]/Season 1")],
            f"{dst_cat}/正名归档B (2025) [tmdbid=700004]": [_dir("Season 1", f"{dst_cat}/正名归档B (2025) [tmdbid=700004]")],
            f"{dst_cat}/正名归档B (2025) [tmdbid=700004]/Season 1": [_file("AliasShow.S01E01.1080p.mkv", f"{dst_cat}/正名归档B (2025) [tmdbid=700004]/Season 1")],
        }
        cd2 = MockCD2(tree)
        details = {
            700001: {"id": 700001, "name": "单源预期归档", "status": "Ended", "seasons": [{"season_number": 1, "episode_count": 1, "air_date": "2024-01-01"}]},
            700002: {"id": 700002, "name": "Oh My Guard", "original_name": "Oh My Guard", "status": "Ended", "seasons": [{"season_number": 1, "episode_count": 1, "air_date": "2024-01-01"}]},
            307478: {"id": 307478, "name": "谎言", "original_name": "라이어", "first_air_date": "2026-10-30", "status": "Ended", "seasons": [{"season_number": 1, "episode_count": 2, "air_date": "2026-10-30"}, {"season_number": 2, "episode_count": 1, "air_date": "2026-10-30"}]},
            700004: {"id": 700004, "name": "正名归档B", "original_name": "AliasShow", "status": "Ended", "seasons": [{"season_number": 1, "episode_count": 1, "air_date": "2024-01-01"}]},
            700005: {"id": 700005, "name": "重名作品A", "status": "Ended", "seasons": [{"season_number": 1, "episode_count": 1, "air_date": "2024-01-01"}]},
            700006: {"id": 700006, "name": "重名作品B", "status": "Ended", "seasons": [{"season_number": 1, "episode_count": 1, "air_date": "2024-01-01"}]},
        }
        tmdb = MockTMDB(details=details)
        mp = MockMP(
            seasons_by_tid={k: v["seasons"] for k, v in details.items()},
            subs=[{"id": 88, "name": "正名归档B", "tmdbid": 700004, "season": 1, "state": "N", "completed_episode": 1, "total_episode": 1, "lack_episode": 0}],
        )

        async with async_session() as db:
            db.add(Task(id="t-mdir", name="韩国剧集多目录", source_path=src_cat, media_type="tv", status="completed"))
            res = await run_inspection(
                task_id="t-mdir",
                source_path=src_cat,
                cd2_client=cd2,
                mp_client=mp,
                tmdb_client=tmdb,
                archive_root="/qa/dst",
                global_source_root="/qa/src",
                db=db,
            )
            # 持久化所有诊断季以联调归档/清理/退订门禁
            for d in res.seasons:
                db.add(
                    SeasonRecord(
                        task_id="t-mdir",
                        show_name=d.show_name,
                        show_path=d.show_path,
                        tmdb_id=str(d.tmdb_id or ""),
                        season_number=d.season_number,
                        season_path=d.season_path,
                        total_episodes=d.total_episodes,
                        local_episode_count=d.local_episode_count,
                        recognized_episodes_json=json.dumps(d.recognized_episodes),
                        missing_episodes_json=json.dumps(d.missing_episodes),
                        duplicate_episodes_json=json.dumps(d.duplicate_episodes),
                        status=d.status,
                        status_detail=d.status_detail,
                        archive_target_path=d.archive_target_path,
                        season_finished=d.season_finished,
                        evidence_json=json.dumps(d.evidence, ensure_ascii=False),
                        conflict_json=json.dumps(d.conflict, ensure_ascii=False),
                        mapping_json=json.dumps(d.mapping, ensure_ascii=False),
                    )
                )
            await db.commit()

        # 精确验证生成的组级复核卡：仅有 307478 (multi_source_dirs) 和 700004 (alias_dir_confirm) 两组！
        group_tids = {str(g["tmdb_id"]): g for g in res.directory_groups}
        self.assertEqual(set(group_tids.keys()), {"307478", "700004"})
        self.assertEqual(group_tids["307478"]["group_kind"], "multi_source_dirs")
        self.assertEqual(len(group_tids["307478"]["entries"]), 2)
        self.assertEqual(group_tids["700004"]["group_kind"], "alias_dir_confirm")
        self.assertEqual(group_tids["700004"]["suggested_target_path"], f"{dst_cat}/正名归档B (2025) [tmdbid=700004]")
        # 700001 / 700002(仅标点差异) / 700005&700006(同剧不同ID) 均无多目录告警
        # 同作品多季仅第一张标注 is_primary_group_card=True
        primary_cards_307478 = [s for s in res.seasons if str(s.tmdb_id) == "307478" and (s.evidence.get("directory_group") or {}).get("is_primary_group_card")]
        self.assertEqual(len(primary_cards_307478), 1)
        g307478 = group_tids["307478"]
        self.assertEqual(g307478["canonical_tmdb_title"], "谎言")
        self.assertEqual(g307478["suggested_target_path"], f"{dst_cat}/谎言 (2026) [tmdbid=307478]")
        cand_targets = {c["target_path"]: c for c in g307478["candidate_targets"]}
        self.assertIn(f"{dst_cat}/完美的谎言 (2026) [tmdbid=307478]", cand_targets)
        self.assertIn(f"{dst_cat}/谎言 (2026) [tmdbid=307478]", cand_targets)
        self.assertTrue(cand_targets[f"{dst_cat}/谎言 (2026) [tmdbid=307478]"]["matches_tmdb_page"])
        self.assertTrue(cand_targets[f"{dst_cat}/谎言 (2026) [tmdbid=307478]"]["is_recommended"])
        self.assertFalse(cand_targets[f"{dst_cat}/完美的谎言 (2026) [tmdbid=307478]"]["is_recommended"])

        # 验证写入/删除/退订全部被组级未确认门禁安全拦截，且 CD2 未收到任何写操作
        async with async_session() as db:
            rec_700004 = (await db.scalars(select(SeasonRecord).where(SeasonRecord.task_id == "t-mdir", SeasonRecord.tmdb_id == "700004"))).first()
            rec_307478 = (await db.scalars(select(SeasonRecord).where(SeasonRecord.task_id == "t-mdir", SeasonRecord.tmdb_id == "307478"))).first()
            rec_700004_id = rec_700004.id
            rec_307478_id = rec_307478.id
            grp_700004_id = group_tids["700004"]["id"]

        transport = httpx.ASGITransport(app=app)
        headers = {"Authorization": "Bearer fixture"}
        with patch("app.api.archive.CloudDrive2Client", return_value=cd2), patch("app.clients.clouddrive2.CloudDrive2Client", return_value=cd2), patch("app.api.moviepilot.MoviePilotClient", return_value=mp):
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                # 验证 directory-groups 列表接口返回 TMDB 页面名与单选候选列表（含历史任务按 SeasonRecord 自动推导兜底）
                r_list = await client.get("/api/tasks/t-mdir/directory-groups", headers=headers)
                self.assertEqual(r_list.status_code, 200)
                list_groups = {str(g["tmdb_id"]): g for g in r_list.json()["groups"]}
                self.assertEqual(list_groups["307478"]["canonical_tmdb_title"], "谎言")
                self.assertEqual(list_groups["307478"]["suggested_target_path"], f"{dst_cat}/谎言 (2026) [tmdbid=307478]")
                self.assertGreaterEqual(len(list_groups["307478"]["candidate_targets"]), 2)

                # 未确认前拦截归档、删除源端（本季）、删除源端整剧、退订联动清理
                r_arc = await client.post("/api/archive/execute", json={"season_record_ids": [rec_307478_id]}, headers=headers)
                self.assertEqual(r_arc.status_code, 200)
                self.assertFalse(r_arc.json()["ok"])
                self.assertEqual(cd2.move_calls, [])

                r_red = await client.post("/api/archive/clean-source-redundant", json={"season_record_ids": [rec_700004_id]}, headers=headers)
                self.assertEqual(r_red.status_code, 200)
                self.assertFalse(r_red.json()["ok"])
                self.assertEqual(cd2.delete_calls, [])

                r_sub = await client.post("/api/moviepilot/subscriptions/88/operation-preview", json={"cleanup": True, "season_record_id": rec_700004_id}, headers=headers)
                self.assertEqual(r_sub.status_code, 409)

                # 用户确认 700004 目标归档目录 B -> 确认操作本身绝不移动或删除文件！
                r_conf = await client.post(
                    f"/api/tasks/t-mdir/directory-groups/{grp_700004_id}/confirm",
                    json={"mode": "use_archive_target", "target_path": f"{dst_cat}/正名归档B (2025) [tmdbid=700004]"},
                    headers=headers,
                )
                self.assertEqual(r_conf.status_code, 200, r_conf.text)
                self.assertEqual(cd2.move_calls, [])
                self.assertEqual(cd2.delete_calls, [])

                # 场景 6a: 网盘突然出现第 3 个同 ID 目录 -> 执行清理时实时检测到漂移并自动失效原确认，安全拦截！
                tree[dst_cat].append(_dir("第三别名B2 [tmdbid=700004]", dst_cat))
                r_red2 = await client.post("/api/archive/clean-source-redundant", json={"season_record_ids": [rec_700004_id]}, headers=headers)
                self.assertEqual(r_red2.status_code, 200)
                self.assertFalse(r_red2.json()["ok"])
                self.assertEqual(cd2.delete_calls, [])

                async with async_session() as db:
                    g_reloaded = await db.get(DirectoryGroup, grp_700004_id)
                    self.assertEqual(g_reloaded.status, "pending_confirm")
                    self.assertTrue(g_reloaded.invalidated_reason)

                # 恢复双目录并再次确认后，真实清理冗余即可放行并仍执行文件级覆盖核查
                tree[dst_cat] = [d for d in tree[dst_cat] if "第三别名" not in d["name"]]
                r_conf2 = await client.post(
                    f"/api/tasks/t-mdir/directory-groups/{grp_700004_id}/confirm",
                    json={"mode": "use_archive_target", "target_path": f"{dst_cat}/正名归档B (2025) [tmdbid=700004]"},
                    headers=headers,
                )
                self.assertEqual(r_conf2.status_code, 200)
                r_red3 = await client.post("/api/archive/clean-source-redundant", json={"season_record_ids": [rec_700004_id]}, headers=headers)
                self.assertEqual(r_red3.status_code, 200)
                self.assertTrue(r_red3.json()["ok"], r_red3.json())
                self.assertEqual(len(cd2.delete_calls), 1)

    async def test_zero_video_shell_checks_emby_and_promotes_to_download_needed(self):
        """0 视频仅剩 NFO 且归档无片时：Emby 无集直接进 download_needed；Emby 已有或查询失败则留 archive_check。"""
        src_cat = "/qa/src/香港剧集"
        show_path = f"{src_cat}/抗战 (2025) [tmdbid=303491]"
        s1_path = f"{show_path}/Season 1"
        tree = {
            src_cat: [_dir("抗战 (2025) [tmdbid=303491]", src_cat)],
            show_path: [_dir("Season 1", show_path)],
            s1_path: [
                _file("season.nfo", s1_path, size=1412),
                _file("抗战 - S01E01.nfo", s1_path, size=1127),
                _file("抗战 - S01E02.nfo", s1_path, size=1079),
            ],
            "/qa/dst/香港剧集": [],
        }
        cd2 = MockCD2(tree)
        details = {
            303491: {
                "id": 303491,
                "name": "抗战",
                "original_name": "抗戰",
                "first_air_date": "2025-10-17",
                "status": "Ended",
                "seasons": [{"season_number": 1, "episode_count": 5, "air_date": "2025-10-17"}],
            }
        }
        tmdb = MockTMDB(details=details)

        # 1) Emby 实时确认 0 集 -> 直接进入 download_needed (缺失 1..5 集)
        mp_empty = MockMP(
            seasons_by_tid={303491: details[303491]["seasons"]},
            ms_remote_by_tid={303491: {"checked": True, "season_episodes": {}, "error": ""}},
        )
        res_empty = await run_inspection(
            task_id="t-nfo-empty",
            source_path=src_cat,
            cd2_client=cd2,
            mp_client=mp_empty,
            tmdb_client=tmdb,
            archive_root="/qa/dst",
            global_source_root="/qa/src",
        )
        self.assertEqual(len(res_empty.seasons), 1)
        s_empty = res_empty.seasons[0]
        self.assertEqual(s_empty.status, "download_needed")
        self.assertEqual(s_empty.missing_episodes, [1, 2, 3, 4, 5])
        self.assertEqual(mp_empty.ms_calls, [303491])
        self.assertTrue(s_empty.evidence["mediaserver_check"]["checked"])
        self.assertEqual(s_empty.evidence["mediaserver_check"]["season_episodes"], [])

        # 2) Emby 实时查到已有 S01E01..E05 -> 安全保留在 archive_check，严禁转补缺
        mp_present = MockMP(
            seasons_by_tid={303491: details[303491]["seasons"]},
            ms_remote_by_tid={303491: {"checked": True, "season_episodes": {1: [1, 2, 3, 4, 5]}, "error": ""}},
        )
        res_present = await run_inspection(
            task_id="t-nfo-present",
            source_path=src_cat,
            cd2_client=cd2,
            mp_client=mp_present,
            tmdb_client=tmdb,
            archive_root="/qa/dst",
            global_source_root="/qa/src",
        )
        s_present = res_present.seasons[0]
        self.assertEqual(s_present.status, "archive_check")
        self.assertIn("Emby", s_present.status_detail)
        self.assertEqual(s_present.evidence["mediaserver_check"]["season_episodes"], [1, 2, 3, 4, 5])

        # 3) Emby 接口查询失败/超时 -> 安全保留在 archive_check，不盲推补缺
        mp_err = MockMP(
            seasons_by_tid={303491: details[303491]["seasons"]},
            ms_remote_by_tid={303491: {"checked": False, "season_episodes": {}, "error": "ConnectTimeout"}},
        )
        res_err = await run_inspection(
            task_id="t-nfo-err",
            source_path=src_cat,
            cd2_client=cd2,
            mp_client=mp_err,
            tmdb_client=tmdb,
            archive_root="/qa/dst",
            global_source_root="/qa/src",
        )
        s_err = res_err.seasons[0]
        self.assertEqual(s_err.status, "archive_check")
        self.assertFalse(s_err.evidence["mediaserver_check"]["checked"])

    async def test_emby_partial_coverage_only_promotes_missing_diff(self):
        """Emby 分集部分覆盖时：仅将实际缺号列入待补（如全 12 集已有 1-8 则仅补 9-12）；并集齐全则留存移出待补。"""
        src_cat = "/qa/src/国产剧"
        show_path = f"{src_cat}/长风渡 (2025) [tmdbid=123456]"
        s1_path = f"{show_path}/Season 1"
        tree = {
            src_cat: [_dir("长风渡 (2025) [tmdbid=123456]", src_cat)],
            show_path: [_dir("Season 1", show_path)],
            s1_path: [
                _file("season.nfo", s1_path, size=1412),
            ],
            "/qa/dst/国产剧": [],
        }
        cd2 = MockCD2(tree)
        details = {
            123456: {
                "id": 123456,
                "name": "长风渡",
                "original_name": "长风渡",
                "first_air_date": "2025-06-18",
                "status": "Ended",
                "seasons": [{"season_number": 1, "episode_count": 12, "air_date": "2025-06-18"}],
            }
        }
        tmdb = MockTMDB(details=details)

        # 1) Emby 拥有 1..8 集 -> 进 download_needed，但 missing_episodes 仅为 9..12
        mp_partial = MockMP(
            seasons_by_tid={123456: details[123456]["seasons"]},
            ms_remote_by_tid={123456: {"checked": True, "season_episodes": {1: list(range(1, 9))}, "error": ""}},
        )
        res_partial = await run_inspection(
            task_id="t-emby-partial",
            source_path=src_cat,
            cd2_client=cd2,
            mp_client=mp_partial,
            tmdb_client=tmdb,
            archive_root="/qa/dst",
            global_source_root="/qa/src",
        )
        self.assertEqual(len(res_partial.seasons), 1)
        s_partial = res_partial.seasons[0]
        self.assertEqual(s_partial.status, "download_needed")
        self.assertEqual(s_partial.missing_episodes, [9, 10, 11, 12])
        self.assertIn("E09–E12", s_partial.status_detail)

        # 2) Emby 拥有全 1..12 集 -> 并集齐全，留存并移出待补
        mp_full = MockMP(
            seasons_by_tid={123456: details[123456]["seasons"]},
            ms_remote_by_tid={123456: {"checked": True, "season_episodes": {1: list(range(1, 13))}, "error": ""}},
        )
        res_full = await run_inspection(
            task_id="t-emby-full",
            source_path=src_cat,
            cd2_client=cd2,
            mp_client=mp_full,
            tmdb_client=tmdb,
            archive_root="/qa/dst",
            global_source_root="/qa/src",
        )
        self.assertEqual(len(res_full.seasons), 1)
        s_full = res_full.seasons[0]
        self.assertEqual(s_full.status, "archive_check")
        self.assertIn("已移出补缺待办", s_full.status_detail)

    async def test_clean_directory_group_source_season_safety_gates_and_execution(self):
        """测试目录组针对指定源端单季的覆盖核验与安全删除门禁及执行。"""
        src_cat = "/qa/src/测试区"
        dst_cat = "/qa/dst/测试区"
        show_src1 = f"{src_cat}/双目录剧集 (2025) [tmdbid=800001]"
        s1_src1 = f"{show_src1}/Season 1"
        show_src2 = f"{src_cat}/双目录剧集 别名目录 [tmdbid=800001]"
        s1_src2 = f"{show_src2}/Season 1"
        show_dst = f"{dst_cat}/双目录剧集 (2025) [tmdbid=800001]"
        s1_dst = f"{show_dst}/Season 1"

        tree = {
            src_cat: [
                _dir("双目录剧集 (2025) [tmdbid=800001]", src_cat),
                _dir("双目录剧集 别名目录 [tmdbid=800001]", src_cat),
            ],
            show_src1: [_dir("Season 1", show_src1)],
            s1_src1: [
                _file("双目录剧集 - S01E01.mp4", s1_src1, size=500_000_000),
                _file("双目录剧集 - S01E01.zh.srt", s1_src1, size=20_000),
            ],
            show_src2: [_dir("Season 1", show_src2)],
            s1_src2: [
                _file("双目录剧集 - S01E01.mp4", s1_src2, size=500_000_000),
            ],
            dst_cat: [_dir("双目录剧集 (2025) [tmdbid=800001]", dst_cat)],
            show_dst: [_dir("Season 1", show_dst)],
            s1_dst: [
                _file("双目录剧集 - S01E01.mkv", s1_dst, size=600_000_000),
                _file("双目录剧集 - S01E02.mkv", s1_dst, size=600_000_000),
            ],
        }
        cd2 = MockCD2(tree)
        details = {
            800001: {
                "id": 800001,
                "name": "双目录剧集",
                "original_name": "双目录剧集",
                "first_air_date": "2025-01-01",
                "status": "Ended",
                "seasons": [{"season_number": 1, "episode_count": 2, "air_date": "2025-01-01"}],
            }
        }
        tmdb = MockTMDB(details=details)
        mp = MockMP(seasons_by_tid={800001: details[800001]["seasons"]})

        async with async_session() as db:
            db.add(Task(id="t-clean-season", name="双目录单季清理测试", source_path=src_cat, media_type="tv", status="completed"))
            res = await run_inspection(
                task_id="t-clean-season",
                source_path=src_cat,
                cd2_client=cd2,
                mp_client=mp,
                tmdb_client=tmdb,
                archive_root="/qa/dst",
                global_source_root="/qa/src",
                db=db,
            )
            for d in res.seasons:
                db.add(
                    SeasonRecord(
                        task_id="t-clean-season",
                        show_name=d.show_name,
                        show_path=d.show_path,
                        tmdb_id=str(d.tmdb_id or ""),
                        season_number=d.season_number,
                        season_path=d.season_path,
                        total_episodes=d.total_episodes,
                        local_episode_count=d.local_episode_count,
                        recognized_episodes_json=json.dumps(d.recognized_episodes),
                        missing_episodes_json=json.dumps(d.missing_episodes),
                        duplicate_episodes_json=json.dumps(d.duplicate_episodes),
                        status=d.status,
                        status_detail=d.status_detail,
                        archive_target_path=d.archive_target_path,
                        season_finished=d.season_finished,
                        evidence_json=json.dumps(d.evidence, ensure_ascii=False),
                        conflict_json=json.dumps(d.conflict, ensure_ascii=False),
                        mapping_json=json.dumps(d.mapping, ensure_ascii=False),
                    )
                )
            await db.commit()

        self.assertEqual(len(res.seasons), 2)

        async with async_session() as db:
            rec = (await db.scalars(select(SeasonRecord).where(SeasonRecord.task_id == "t-clean-season", SeasonRecord.show_path == show_src1))).first()
            self.assertIsNotNone(rec)
            rec_id = rec.id
            groups = list((await db.scalars(select(DirectoryGroup).where(DirectoryGroup.task_id == "t-clean-season"))).all())
            self.assertEqual(len(groups), 1)
            grp = groups[0]
            grp_id = grp.id

        transport = httpx.ASGITransport(app=app)
        headers = {"Authorization": "Bearer fixture"}
        with patch("app.clients.clouddrive2.CloudDrive2Client", return_value=cd2):
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                # 1) 未确认目标 -> 400
                r1 = await client.post(f"/api/tasks/t-clean-season/directory-groups/{grp_id}/clean-source-season", json={"season_record_id": rec_id}, headers=headers)
                self.assertEqual(r1.status_code, 400)
                self.assertIn("尚未确认", r1.text)

                # 用户确认归档目标为 show_dst
                r_conf = await client.post(
                    f"/api/tasks/t-clean-season/directory-groups/{grp_id}/confirm",
                    json={"mode": "use_archive_target", "target_path": show_dst},
                    headers=headers,
                )
                self.assertEqual(r_conf.status_code, 200)

                # 2) 归档目标单独全季齐全 (E01, E02 共 2 集 == total 2 集) 且完全覆盖源端 (E01 in target) -> 成功执行清理与字幕迁移
                r2 = await client.post(f"/api/tasks/t-clean-season/directory-groups/{grp_id}/clean-source-season", json={"season_record_id": rec_id}, headers=headers)
                self.assertEqual(r2.status_code, 200, r2.text)
                self.assertTrue(r2.json()["success"])
                # 独有字幕应被迁移至目标目录
                self.assertEqual(len(cd2.move_calls), 1)
                self.assertEqual(cd2.move_calls[0][1], s1_dst)
                # 源端单季目录被安全删除
                self.assertEqual(len(cd2.delete_calls), 1)
                self.assertEqual(cd2.delete_calls[0], s1_src1)

                # 验证记录状态变为 cleaned_redundant
                async with async_session() as db:
                    rec_after = await db.get(SeasonRecord, rec_id)
                    self.assertEqual(rec_after.status, "cleaned_redundant")
                    self.assertIn("已通过多目录归档覆盖核验清理源端残留", rec_after.status_detail)


if __name__ == "__main__":
    unittest.main()
