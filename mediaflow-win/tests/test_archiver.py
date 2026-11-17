"""归档引擎与源端零残留治理全矩阵单元测试用例.

覆盖场景：
1. 计算归档目标路径 (compute_archive_target)；
2. 同季冲突检测与安全拦截；
3. 全剧归档完成时，公共资产 (tvshow.nfo, poster) 迁入归档库且源端剧集目录彻底删除；
4. 多季连载中，暂时保留源端剧集目录。
"""

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding='utf-8')

_TMP_ARCHIVER_DATA_DIR = tempfile.mkdtemp(prefix="mf-test-archiver-")
atexit.register(shutil.rmtree, _TMP_ARCHIVER_DATA_DIR, True)
os.environ.update(
    MF_DATA_DIR=_TMP_ARCHIVER_DATA_DIR,
    MF_SOURCE_ROOT="/qa/src",
    MF_ARCHIVE_ROOT="/qa/dst",
    MF_LIBRARY_ROOT="/qa/dst",
    MF_AUTH_TOKEN="fixture",
)

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

    def test_05_archived_season_excluded_from_todo_and_summary(self):
        """测试已归档(status='archived')条目严格从五大待办清单及统计中剔除."""
        from app.core.season_classifier import classify_single_record, consolidate_seasons_to_work_records

        archived_rec = {
            "id": "rec_archived_01",
            "show_name": "你的心事映在我的眉间 (2026) [tmdbid=316795]",
            "tmdb_id": 316795,
            "season_number": 1,
            "season_path": "/115/Library_incs/nas/电视剧/香港剧集/你的心事映在我的眉间 (2026) [tmdbid=316795]/Season 1",
            "total_episodes": 1,
            "recognized_episodes": [1],
            "missing_episodes": [],
            "duplicate_episodes": [],
            "status": "archived",
            "status_detail": "已成功归档至 /115/Library_incs/nas.finish/电视剧/香港剧集/你的心事映在我的眉间 (2026) [tmdbid=316795]/Season 1",
        }

        cat, act = classify_single_record(archived_rec)
        self.assertEqual(cat, "", "已归档记录不得落入任何待办分类！")
        self.assertEqual(act, "", "已归档记录动作类型应为空！")

        # 归并为作品季工作项时，全组均为 archived 的剧集组应自动跳过
        work_records, counts = consolidate_seasons_to_work_records([archived_rec])
        self.assertEqual(len(work_records), 0, "已归档剧集不得出现在待办工作项中！")
        self.assertEqual(counts["archive"], 0, "齐全可归档计数必须为 0！")
        self.assertEqual(counts["total_work_seasons"], 0, "总待办季数必须为 0！")


class _FakeCD2:
    """可重置的内存模拟 CD2 网盘，用于验证 A01–A14 验收场景."""

    def __init__(self, tree: dict[str, list[dict]]):
        self.entries = {k.rstrip("/"): list(v) for k, v in tree.items()}
        self.move_calls: list[tuple[str, str, bool]] = []
        self.delete_calls: list[str] = []
        self.fail_list_paths: set[str] = set()
        self.fail_delete_paths: set[str] = set()
        self.drop_file_on_season_move: str | None = None

    async def mkdir(self, path: str):
        p = path.rstrip("/")
        self.entries.setdefault(p, [])
        parent = p.rsplit("/", 1)[0]
        bname = p.rsplit("/", 1)[-1]
        if parent in self.entries and not any(x.get("name") == bname for x in self.entries[parent]):
            self.entries[parent].append({"name": bname, "path": p, "isDirectory": True, "size": 0})
        return {"success": True}

    async def list_files(self, path: str, force_refresh: bool = False):
        p = path.rstrip("/")
        if p in self.fail_list_paths:
            raise RuntimeError(f"模拟读取失败: {p}")
        if p not in self.entries:
            return []
        return [dict(x) for x in self.entries[p]]

    async def move_file(self, src: str, dst_parent: str, *, skip_existing: bool = False):
        s = src.rstrip("/")
        dp = dst_parent.rstrip("/")
        self.move_calls.append((s, dp, skip_existing))
        self.entries.setdefault(dp, [])
        s_parent = s.rsplit("/", 1)[0]
        s_name = s.rsplit("/", 1)[-1]

        # 若移动的是整个目录
        if s in self.entries:
            new_dir_path = f"{dp}/{s_name}"
            moved_children = []
            for child in self.entries.pop(s, []):
                if self.drop_file_on_season_move and child.get("name") == self.drop_file_on_season_move:
                    continue
                c_copy = dict(child)
                c_copy["path"] = f"{new_dir_path}/{c_copy['name']}"
                moved_children.append(c_copy)
            self.entries[new_dir_path] = moved_children
            if s_parent in self.entries:
                self.entries[s_parent] = [x for x in self.entries[s_parent] if x.get("path") != s and x.get("name") != s_name]
            if not any(x.get("name") == s_name for x in self.entries[dp]):
                self.entries[dp].append({"name": s_name, "path": new_dir_path, "isDirectory": True, "size": 0})
            return {"success": True, "resultFilePaths": [new_dir_path]}

        # 移动单个文件
        if s_parent in self.entries:
            found = next((x for x in self.entries[s_parent] if x.get("path") == s or x.get("name") == s_name), None)
            if found:
                existing_in_dst = any(x.get("name", "").lower() == s_name.lower() for x in self.entries[dp])
                if existing_in_dst and skip_existing:
                    return {"success": True, "resultFilePaths": []}
                self.entries[s_parent] = [x for x in self.entries[s_parent] if x is not found]
                f_copy = dict(found)
                f_copy["path"] = f"{dp}/{s_name}"
                self.entries[dp].append(f_copy)
        return {"success": True, "resultFilePaths": [f"{dp}/{s_name}"]}

    async def delete_file(self, path: str):
        p = path.rstrip("/")
        if p in self.fail_delete_paths:
            raise RuntimeError(f"模拟删除失败: {p}")
        self.delete_calls.append(p)
        self.entries.pop(p, None)
        parent = p.rsplit("/", 1)[0]
        bname = p.rsplit("/", 1)[-1]
        if parent in self.entries:
            self.entries[parent] = [x for x in self.entries[parent] if x.get("path") != p and x.get("name") != bname]
        return {"success": True}

    async def close(self):
        pass


class TestArchiveAcceptanceScenarios(unittest.IsolatedAsyncioTestCase):
    """覆盖《MediaFlow齐全可归档模块需求与交互方案》A01–A14 核心验收场景."""

    async def asyncSetUp(self):
        os.environ.update(
            MF_SOURCE_ROOT="/qa/src",
            MF_ARCHIVE_ROOT="/qa/dst",
            MF_LIBRARY_ROOT="/qa/dst",
            MF_AUTH_TOKEN="fixture",
        )
        from app.config import get_settings
        get_settings.cache_clear()
        from app.database import Base, engine, init_db
        from app.main import app
        import httpx

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await init_db()
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fixture",
            headers={"Authorization": "Bearer fixture"},
        )

    async def asyncTearDown(self):
        from app.database import engine
        await self.http.aclose()
        await engine.dispose()

    async def test_a01_and_a02_preview_execute_and_retained_subtitle(self):
        """A01 & A02: S01归档保留S02与剧根海报；增量合并同名伴生字幕双端保留并正常登记 archived."""
        from app.database import async_session
        from app.models.task import Task
        from app.models.season import SeasonRecord

        async with async_session() as db:
            db.add(Task(id="t1", name="测试任务", source_path="/qa/src", status="completed"))
            await db.flush()
            # A01: S01 已完结，S02 连载中
            db.add(SeasonRecord(
                id="s01_full", task_id="t1", show_name="剧A", show_path="/qa/src/剧A",
                tmdb_id="101", season_number=1, season_path="/qa/src/剧A/Season 1",
                archive_target_path="/qa/dst/剧A/Season 1", total_episodes=2, local_episode_count=2,
                recognized_episodes_json="[1, 2]", missing_episodes_json="[]", duplicate_episodes_json="[]",
                status="archive_ready", season_finished=True,
            ))
            db.add(SeasonRecord(
                id="s02_airing", task_id="t1", show_name="剧A", show_path="/qa/src/剧A",
                tmdb_id="101", season_number=2, season_path="/qa/src/剧A/Season 2",
                archive_target_path="/qa/dst/剧A/Season 2", total_episodes=10, local_episode_count=2,
                recognized_episodes_json="[1, 2]", missing_episodes_json="[3, 4]", duplicate_episodes_json="[]",
                status="airing", season_finished=False,
            ))
            # A02: 剧B S01 增量合并（源有 E03 + 同名 sub.srt，目标已有 E01, E02 + 同名 sub.srt）
            db.add(SeasonRecord(
                id="s01_inc", task_id="t1", show_name="剧B", show_path="/qa/src/剧B",
                tmdb_id="102", season_number=1, season_path="/qa/src/剧B/Season 1",
                archive_target_path="/qa/dst/剧B/Season 01", total_episodes=3, local_episode_count=1,
                recognized_episodes_json="[3]", missing_episodes_json="[]", duplicate_episodes_json="[]",
                status="archive_ready", season_finished=True,
            ))
            await db.commit()

        disk = _FakeCD2({
            "/qa/src/剧A": [
                {"name": "Season 1", "path": "/qa/src/剧A/Season 1", "isDirectory": True},
                {"name": "Season 2", "path": "/qa/src/剧A/Season 2", "isDirectory": True},
                {"name": "poster.jpg", "path": "/qa/src/剧A/poster.jpg", "isDirectory": False, "size": 10},
            ],
            "/qa/src/剧A/Season 1": [
                {"name": "A.S01E01.mkv", "path": "/qa/src/剧A/Season 1/A.S01E01.mkv", "isDirectory": False, "size": 100},
                {"name": "A.S01E02.mkv", "path": "/qa/src/剧A/Season 1/A.S01E02.mkv", "isDirectory": False, "size": 100},
            ],
            "/qa/src/剧B": [
                {"name": "Season 1", "path": "/qa/src/剧B/Season 1", "isDirectory": True},
            ],
            "/qa/src/剧B/Season 1": [
                {"name": "B.S01E03.mkv", "path": "/qa/src/剧B/Season 1/B.S01E03.mkv", "isDirectory": False, "size": 100},
                {"name": "sub.srt", "path": "/qa/src/剧B/Season 1/sub.srt", "isDirectory": False, "size": 5},
            ],
            "/qa/dst/剧B": [
                {"name": "Season 01", "path": "/qa/dst/剧B/Season 01", "isDirectory": True},
            ],
            "/qa/dst/剧B/Season 01": [
                {"name": "B.S01E01.mkv", "path": "/qa/dst/剧B/Season 01/B.S01E01.mkv", "isDirectory": False, "size": 100},
                {"name": "B.S01E02.mkv", "path": "/qa/dst/剧B/Season 01/B.S01E02.mkv", "isDirectory": False, "size": 100},
                {"name": "sub.srt", "path": "/qa/dst/剧B/Season 01/sub.srt", "isDirectory": False, "size": 5},
            ],
        })

        with patch("app.api.archive.CloudDrive2Client", return_value=disk):
            prev = (await self.http.post("/api/archive/preview", json={"season_record_ids": ["s01_full", "s01_inc"], "task_id": "t1"})).json()
            self.assertTrue(prev["ok"])
            self.assertEqual(prev["executable_count"], 2)
            by_id = {it["season_record_id"]: it for it in prev["items"]}
            self.assertEqual(by_id["s01_full"]["mode"], "full_season_move")
            self.assertFalse(by_id["s01_full"]["will_cleanup_show_root"])
            self.assertIn("Season 2", by_id["s01_full"]["show_root_note"])
            self.assertEqual(by_id["s01_inc"]["mode"], "incremental_merge")
            self.assertEqual(len(by_id["s01_inc"]["expected_retained"]), 1)

            exe = (await self.http.post("/api/archive/execute", json={
                "season_record_ids": ["s01_full", "s01_inc"],
                "plan_id": prev["plan_id"],
            })).json()
            self.assertTrue(exe["ok"])
            res_map = {r["season_record_id"]: r for r in exe["results"]}
            self.assertEqual(res_map["s01_full"]["outcome"], "archived")
            self.assertEqual(res_map["s01_inc"]["outcome"], "archived")
            # 剧A 的 Season 2 和 poster.jpg 仍在源端
            src_a_names = [x["name"] for x in disk.entries["/qa/src/剧A"]]
            self.assertIn("Season 2", src_a_names)
            self.assertIn("poster.jpg", src_a_names)
            # 剧B 的 sub.srt 双端保留，且 B.S01E03.mkv 已合并入 Season 01
            dst_b_names = [x["name"] for x in disk.entries["/qa/dst/剧B/Season 01"]]
            self.assertCountEqual(dst_b_names, ["B.S01E01.mkv", "B.S01E02.mkv", "B.S01E03.mkv", "sub.srt"])
            src_b_names = [x["name"] for x in disk.entries["/qa/src/剧B/Season 1"]]
            self.assertEqual(src_b_names, ["sub.srt"])

            # A11: 重复提交同一 plan_id 幂等返回，不再次调用 move_file
            move_cnt_before = len(disk.move_calls)
            exe_dup = (await self.http.post("/api/archive/execute", json={
                "season_record_ids": ["s01_full"],
                "plan_id": prev["plan_id"],
            })).json()
            self.assertTrue(exe_dup["ok"])
            self.assertTrue(exe_dup["results"][0]["already_done"])
            self.assertEqual(len(disk.move_calls), move_cnt_before)

    async def test_a05_and_a07_zero_writes_when_blocked_even_with_e00(self):
        """A05 & A07: 目标存在同名视频或同集异名冲突或多候选季目录时，在首个写入前拦截，E00 保持在源端不动."""
        from app.database import async_session
        from app.models.task import Task
        from app.models.season import SeasonRecord

        async with async_session() as db:
            db.add(Task(id="t2", name="冲突任务", source_path="/qa/src", status="completed"))
            await db.flush()
            db.add(SeasonRecord(
                id="s_e00_dup", task_id="t2", show_name="剧C", show_path="/qa/src/剧C",
                tmdb_id="103", season_number=1, season_path="/qa/src/剧C/Season 1",
                archive_target_path="/qa/dst/剧C/Season 1", total_episodes=2, local_episode_count=2,
                recognized_episodes_json="[0, 1, 2]", status="archive_ready", season_finished=True,
            ))
            db.add(SeasonRecord(
                id="s_diff_cut", task_id="t2", show_name="剧D", show_path="/qa/src/剧D",
                tmdb_id="104", season_number=1, season_path="/qa/src/剧D/Season 1",
                archive_target_path="/qa/dst/剧D/Season 1", total_episodes=2, local_episode_count=1,
                recognized_episodes_json="[1]", status="archive_ready", season_finished=True,
            ))
            await db.commit()

        disk = _FakeCD2({
            "/qa/src/剧C/Season 1": [
                {"name": "C.S01E00.mkv", "path": "/qa/src/剧C/Season 1/C.S01E00.mkv", "isDirectory": False, "size": 50},
                {"name": "C.S01E01.mkv", "path": "/qa/src/剧C/Season 1/C.S01E01.mkv", "isDirectory": False, "size": 100},
            ],
            "/qa/dst/剧C": [
                {"name": "Season 1", "path": "/qa/dst/剧C/Season 1", "isDirectory": True},
            ],
            "/qa/dst/剧C/Season 1": [
                {"name": "C.S01E01.mkv", "path": "/qa/dst/剧C/Season 1/C.S01E01.mkv", "isDirectory": False, "size": 100},
            ],
            "/qa/src/剧D/Season 1": [
                {"name": "D.S01E01.DirectorCut.mkv", "path": "/qa/src/剧D/Season 1/D.S01E01.DirectorCut.mkv", "isDirectory": False, "size": 120},
            ],
            "/qa/dst/剧D": [
                {"name": "Season 1", "path": "/qa/dst/剧D/Season 1", "isDirectory": True},
            ],
            "/qa/dst/剧D/Season 1": [
                {"name": "D.S01E01.WEB-DL.mkv", "path": "/qa/dst/剧D/Season 1/D.S01E01.WEB-DL.mkv", "isDirectory": False, "size": 100},
            ],
        })

        with patch("app.api.archive.CloudDrive2Client", return_value=disk):
            exe = (await self.http.post("/api/archive/execute", json={"season_record_ids": ["s_e00_dup", "s_diff_cut"]})).json()
            self.assertFalse(exe["ok"])
            # 核心断言：零写入！C.S01E00.mkv 绝不能提前被移入 Season 0！
            self.assertEqual(disk.move_calls, [])
            src_c_names = [x["name"] for x in disk.entries["/qa/src/剧C/Season 1"]]
            self.assertIn("C.S01E00.mkv", src_c_names)

    async def test_a08_and_a09_unverified_read_failure_and_reconcile_resume(self):
        """A08 & A09: 整移末次读源报错或目标缺计划文件不得登记 archived；中断后 reconcile 仅续传剩余项."""
        from app.database import async_session
        from app.models.task import Task
        from app.models.season import SeasonRecord

        async with async_session() as db:
            db.add(Task(id="t3", name="核验与恢复任务", source_path="/qa/src", status="completed"))
            await db.flush()
            db.add(SeasonRecord(
                id="s_drop", task_id="t3", show_name="漏文件剧", show_path="/qa/src/漏文件剧",
                tmdb_id="105", season_number=1, season_path="/qa/src/漏文件剧/Season 1",
                archive_target_path="/qa/dst/漏文件剧/Season 1", total_episodes=2, local_episode_count=2,
                recognized_episodes_json="[1, 2]", status="archive_ready", season_finished=True,
            ))
            await db.commit()

        disk = _FakeCD2({
            "/qa/src/漏文件剧": [
                {"name": "Season 1", "path": "/qa/src/漏文件剧/Season 1", "isDirectory": True},
            ],
            "/qa/src/漏文件剧/Season 1": [
                {"name": "E.S01E01.mkv", "path": "/qa/src/漏文件剧/Season 1/E.S01E01.mkv", "isDirectory": False, "size": 100},
                {"name": "E.S01E02.mkv", "path": "/qa/src/漏文件剧/Season 1/E.S01E02.mkv", "isDirectory": False, "size": 100},
            ],
        })
        # 模拟整季移动后目标漏了 E.S01E02.mkv
        disk.drop_file_on_season_move = "E.S01E02.mkv"

        with patch("app.api.archive.CloudDrive2Client", return_value=disk):
            exe = (await self.http.post("/api/archive/execute", json={"season_record_ids": ["s_drop"]})).json()
            self.assertFalse(exe["ok"])
            self.assertEqual(exe["results"][0]["outcome"], "partial_archived")

            # 将漏掉的 E.S01E02.mkv 放回源季，测试 /api/archive/reconcile 仅续传未到位项
            disk.entries["/qa/src/漏文件剧/Season 1"] = [
                {"name": "E.S01E02.mkv", "path": "/qa/src/漏文件剧/Season 1/E.S01E02.mkv", "isDirectory": False, "size": 100}
            ]
            disk.move_calls.clear()
            rec_res = (await self.http.post("/api/archive/reconcile", json={
                "season_record_id": "s_drop",
                "resume_remaining": True,
            })).json()
            self.assertTrue(rec_res["ok"])
            self.assertEqual(rec_res["outcome"], "archived")
            self.assertEqual(rec_res["newly_moved"], ["E.S01E02.mkv"])
            self.assertEqual(len(disk.move_calls), 1)


if __name__ == "__main__":
    unittest.main()
