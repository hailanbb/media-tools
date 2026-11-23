"""订阅治理专属 CD2 实盘库存索引与实时核验服务（与既有初检/补缺/归档模块完全隔离）。

职责：
1. 扫描 CloudDrive2 受控源根 (source_root) 与归档根 (archive_root) 下的电视剧、动漫、电影目录；
2. 以 [tmdbid=XXX] 为主键（规范化标题+年份兜底），为电视剧精确统计 CD2 源+归档合并去重集数与集号列表，
   为电影精确核验 CD2 中是否存在有效正片视频文件；
3. 将精简索引快照持久化到现有 metadata_cache 表 (cache_key="sub_cd2_inventory_v1")，不修改任何数据库表结构；
4. 提供只读注入函数 enrich_subscriptions_with_cd2_and_cache()，零阻塞叠加：
   - CD2 持久化实盘库存快照；
   - 本地已有 SeasonRecord（初检已扫出的 CD2 剧季实盘数据）；
   - 本地已有 MetadataCache 中的 TMDB 剧集缓存（提供单集时长、类型 ID 等短剧识别金标准，零额外外部请求）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.clouddrive2 import CloudDrive2Client, GrpcError
from app.clients.moviepilot import MoviePilotClient
from app.config import get_settings
from app.core.episode_parser import is_video_file, parse_episode
from app.core.identity_resolver import normalize_title_token
from app.models.metadata_cache import MetadataCache
from app.models.season import SeasonRecord

log = logging.getLogger("mediaflow.subscription_cd2_index")

CD2_SNAPSHOT_CACHE_KEY = "sub_cd2_inventory_v1"
CD2_SNAPSHOT_SOURCE = "cd2_sub_index"

_TMDB_TAG_RE = re.compile(r"\[tmdbid[=:](\d+)\]", re.IGNORECASE)
_YEAR_RE = re.compile(r"\((\d{4})\)")
_SEASON_DIR_RE = re.compile(r"^(?:season\s*|s|第)(\d+)(?:季)?$", re.IGNORECASE)
_TRAILER_RE = re.compile(r"(?:trailer|sample|teaser|预告|花絮|片段)", re.IGNORECASE)

# 进程级内存热缓存 & 异步同步任务状态
_memory_snapshot: dict[str, Any] | None = None
_sync_job: dict[str, Any] = {
    "job_id": "",
    "status": "idle",  # idle | running | completed | failed
    "progress_pct": 0,
    "stage": "",
    "message": "尚未执行 CD2 全盘实盘同步",
    "scanned_categories": 0,
    "total_categories": 0,
    "matched_tv": 0,
    "matched_movies": 0,
    "synced_at": None,
    "errors": [],
}
_sync_task: asyncio.Task | None = None


def _parse_season_dirname(name: str) -> int | None:
    m = _SEASON_DIR_RE.match((name or "").strip())
    if m:
        return int(m.group(1))
    if (name or "").strip().lower() in ("specials", "special", "特别篇", "番外"):
        return 0
    return None


def _extract_dir_identity(dirname: str) -> tuple[str | None, str, str]:
    """从目录名提取 (tmdb_id, normalized_title, year)。"""
    tid_m = _TMDB_TAG_RE.search(dirname or "")
    tid = tid_m.group(1) if tid_m else None

    year_m = _YEAR_RE.search(dirname or "")
    year = year_m.group(1) if year_m else ""

    cleaned = _TMDB_TAG_RE.sub("", dirname or "")
    cleaned = _YEAR_RE.sub("", cleaned).strip(" -_·.")
    norm_title = normalize_title_token(cleaned)
    return tid, norm_title, year


def _title_year_key(title: str, year: str = "") -> str:
    nt = normalize_title_token(title or "")
    if not nt:
        return ""
    return f"{nt}|{year.strip()}" if year and year.strip() else nt


def _is_valid_movie_video(entry: dict[str, Any]) -> bool:
    """判断电影目录下的文件是否为有效正片视频（过滤极小 sample/trailer）。"""
    if entry.get("isDirectory"):
        return False
    fname = str(entry.get("name") or "")
    if not is_video_file(fname):
        return False
    if _TRAILER_RE.search(fname):
        return False
    size = int(entry.get("size") or 0)
    # .strm 文件体积极小但有效；实体视频要求 >= 5MB 或 CD2 未报告大小(size==0)
    if fname.lower().endswith(".strm"):
        return True
    return size == 0 or size >= 5 * 1024 * 1024


async def load_cd2_snapshot(db: AsyncSession) -> dict[str, Any] | None:
    """读取 CD2 实盘库存快照（优先内存缓存，冷启动从 SQLite metadata_cache 加载）。"""
    global _memory_snapshot
    if _memory_snapshot is not None:
        return _memory_snapshot
    try:
        stmt = select(MetadataCache).where(MetadataCache.cache_key == CD2_SNAPSHOT_CACHE_KEY)
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row and row.payload_json:
            data = json.loads(row.payload_json)
            if isinstance(data, dict):
                _memory_snapshot = data
                return data
    except Exception as exc:
        log.warning("读取 CD2 订阅索引快照失败: %s", exc)
    return None


async def save_cd2_snapshot(db: AsyncSession, snapshot: dict[str, Any]) -> None:
    """持久化 CD2 实盘库存快照到 metadata_cache 表。"""
    global _memory_snapshot
    _memory_snapshot = snapshot
    payload_str = json.dumps(snapshot, ensure_ascii=False)
    try:
        stmt = select(MetadataCache).where(MetadataCache.cache_key == CD2_SNAPSHOT_CACHE_KEY)
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = MetadataCache(
                cache_key=CD2_SNAPSHOT_CACHE_KEY,
                source=CD2_SNAPSHOT_SOURCE,
                media_id="global",
                season_number=0,
                payload_json=payload_str,
                verified=bool(snapshot.get("tv_scanned_ok") or snapshot.get("movie_scanned_ok")),
                status="ok",
            )
            db.add(row)
        else:
            row.payload_json = payload_str
            row.verified = bool(snapshot.get("tv_scanned_ok") or snapshot.get("movie_scanned_ok"))
            row.status = "ok"
            row.fetched_at = datetime.now(timezone.utc)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        log.warning("保存 CD2 订阅索引快照失败: %s", exc)


def get_cd2_sync_status() -> dict[str, Any]:
    """获取当前 CD2 异步同步任务状态及已有快照概要。"""
    snap = _memory_snapshot
    return {
        **_sync_job,
        "has_snapshot": snap is not None,
        "snapshot_synced_at": snap.get("synced_at") if snap else None,
        "snapshot_tv_count": len((snap or {}).get("tv_by_key") or {}),
        "snapshot_movie_count": len((snap or {}).get("movie_by_tmdb") or {}),
        "snapshot_tv_ok": bool((snap or {}).get("tv_scanned_ok")),
        "snapshot_movie_ok": bool((snap or {}).get("movie_scanned_ok")),
    }


async def _safe_list_files(cd2: CloudDrive2Client, path: str, timeout: float = 55.0) -> tuple[list[dict[str, Any]], str | None]:
    """带超时与 NOT_FOUND 区分的安全 CD2 列目录。"""
    try:
        items = await asyncio.wait_for(cd2.list_files(path), timeout=timeout)
        return items, None
    except FileNotFoundError:
        return [], None
    except GrpcError as ge:
        if ge.status == 5:  # NOT_FOUND
            return [], None
        return [], f"{path}: gRPC[{ge.status}] {ge.message}"
    except asyncio.TimeoutError:
        return [], f"{path}: 读取超时({int(timeout)}s)"
    except Exception as exc:
        return [], f"{path}: {type(exc).__name__}: {exc}"


async def _scan_season_episodes(
    cd2: CloudDrive2Client,
    show_dir: str,
    target_season: int,
    sem: asyncio.Semaphore,
) -> tuple[set[int], list[str], list[int]]:
    """扫描单个剧集目录下指定季的 CD2 实存正片集号集合与非 strm 文件体积列表。"""
    eps: set[int] = set()
    matched_paths: list[str] = []
    video_sizes: list[int] = []

    async with sem:
        children, err = await _safe_list_files(cd2, show_dir, timeout=35.0)
    if err:
        return eps, matched_paths, video_sizes

    season_dirs = [
        c for c in children
        if c.get("isDirectory") and _parse_season_dirname(c.get("name", "")) == target_season
    ]

    if season_dirs:
        for sd in season_dirs:
            sd_path = sd["path"]
            async with sem:
                files, f_err = await _safe_list_files(cd2, sd_path, timeout=40.0)
            if f_err:
                continue
            found_any = False
            for f in files:
                if f.get("isDirectory"):
                    continue
                fname = str(f.get("name") or "")
                if not is_video_file(fname):
                    continue
                parsed = parse_episode(fname, str(f.get("path") or ""), int(f.get("size") or 0))
                if parsed.is_special or parsed.is_extra or not parsed.episode_numbers:
                    continue
                if parsed.season_number is not None and parsed.season_number != target_season:
                    continue
                valid_eps = [ep for ep in parsed.episode_numbers if ep > 0]
                if not valid_eps:
                    continue
                eps.update(valid_eps)
                found_any = True
                if not fname.lower().endswith(".strm") and int(f.get("size") or 0) > 0:
                    video_sizes.append(int(f.get("size") or 0))
            if found_any:
                matched_paths.append(sd_path)
    elif target_season == 1:
        # 兜底：部分剧集未建 Season 1 子目录，视频直接放在剧根目录下
        found_any = False
        for f in children:
            if f.get("isDirectory"):
                continue
            fname = str(f.get("name") or "")
            if not is_video_file(fname):
                continue
            parsed = parse_episode(fname, str(f.get("path") or ""), int(f.get("size") or 0))
            if parsed.is_special or parsed.is_extra or not parsed.episode_numbers:
                continue
            if parsed.season_number is not None and parsed.season_number != 1:
                continue
            valid_eps = [ep for ep in parsed.episode_numbers if ep > 0]
            if not valid_eps:
                continue
            eps.update(valid_eps)
            found_any = True
            if not fname.lower().endswith(".strm") and int(f.get("size") or 0) > 0:
                video_sizes.append(int(f.get("size") or 0))
        if found_any:
            matched_paths.append(show_dir)

    return eps, matched_paths, video_sizes


async def build_cd2_subscription_inventory(
    db_session_factory,
    *,
    scope: str = "all",  # "all" | "tv" | "movie"
    job_id: str = "",
    raw_subs: list[dict[str, Any]] | None = None,
    progress_cb=None,
) -> dict[str, Any]:
    """执行 CD2 全盘实盘库存扫描并与当前 MoviePilot 订阅对齐，结果持久化到 SQLite。"""
    global _sync_job
    settings = get_settings()
    source_root = (settings.source_root or "").rstrip("/")
    archive_root = (settings.archive_root or settings.library_root or "").rstrip("/")

    roots: list[tuple[str, str]] = []
    if source_root:
        roots.append(("source", source_root))
    if archive_root and archive_root != source_root:
        roots.append(("archive", archive_root))

    if not roots:
        raise RuntimeError("未配置 CD2 源目录或归档目录 (MF_SOURCE_ROOT / MF_ARCHIVE_ROOT)")

    def _notify_progress() -> None:
        if callable(progress_cb):
            try:
                progress_cb(
                    int(_sync_job.get("progress_pct") or 0),
                    str(_sync_job.get("stage") or ""),
                    str(_sync_job.get("message") or ""),
                    int(_sync_job.get("scanned_categories") or 0),
                    int(_sync_job.get("total_categories") or 0),
                    int(_sync_job.get("matched_tv") or 0),
                    int(_sync_job.get("matched_movies") or 0),
                )
            except Exception:
                pass

    _sync_job.update(
        job_id=job_id or uuid.uuid4().hex[:12],
        status="running",
        progress_pct=5,
        stage="fetch_subscriptions",
        message="正在准备 CD2 实盘目录扫描目标...",
        errors=[],
    )
    _notify_progress()

    cd2 = CloudDrive2Client()
    sem = asyncio.Semaphore(4)

    if raw_subs is not None:
        subs = raw_subs
    else:
        mp = MoviePilotClient()
        try:
            subs = await mp.list_subscriptions()
        finally:
            await mp.close()

    # 汇总当前订阅需要重点钻取的 TMDB ID 集合
    tv_sub_targets: dict[str, set[int]] = {}  # tmdb_id -> {season1, season2}
    tv_title_targets: dict[str, set[int]] = {}  # norm_title -> {seasons}
    movie_sub_tids: set[str] = set()
    movie_sub_titles: set[str] = set()

    for s in subs:
        raw_type = str(s.get("type") or s.get("mtype") or "").strip().lower()
        is_mov = raw_type in ("movie", "电影") or "电影" in raw_type or "movie" in raw_type
        tid = str(s.get("tmdbid") or s.get("tmdb_id") or "").strip()
        name = str(s.get("name") or s.get("title") or "").strip()
        year = str(s.get("year") or "").strip()
        if is_mov:
            if tid.isdigit():
                movie_sub_tids.add(tid)
            tk = _title_year_key(name, year)
            if tk:
                movie_sub_titles.add(tk)
            tk0 = _title_year_key(name, "")
            if tk0:
                movie_sub_titles.add(tk0)
        else:
            sn = int(s.get("season") or 1)
            if tid.isdigit():
                tv_sub_targets.setdefault(tid, set()).add(sn)
            nt = normalize_title_token(name)
            if nt:
                tv_title_targets.setdefault(nt, set()).add(sn)

    # 加载已有快照，支持局部 scope ("tv" 或 "movie") 增量刷新而不丢弃另一侧数据
    async with db_session_factory() as db:
        prev_snap = await load_cd2_snapshot(db) or {}

    tv_by_key: dict[str, Any] = dict(prev_snap.get("tv_by_key") or {}) if scope == "movie" else {}
    tv_by_title: dict[str, Any] = dict(prev_snap.get("tv_by_title") or {}) if scope == "movie" else {}
    movie_by_tmdb: dict[str, Any] = dict(prev_snap.get("movie_by_tmdb") or {}) if scope == "tv" else {}
    movie_by_title: dict[str, Any] = dict(prev_snap.get("movie_by_title") or {}) if scope == "tv" else {}
    tv_scanned_ok = bool(prev_snap.get("tv_scanned_ok")) if scope == "movie" else True
    movie_scanned_ok = bool(prev_snap.get("movie_scanned_ok")) if scope == "tv" else True
    category_errors: list[str] = []

    try:
        # 1. 发现所有二级分类目录
        tv_cat_dirs: list[tuple[str, str]] = []     # (side, path)
        movie_cat_dirs: list[tuple[str, str]] = []  # (side, path)

        for side, root in roots:
            if scope in ("all", "tv"):
                for top in ("电视剧", "动漫"):
                    entries, err = await _safe_list_files(cd2, f"{root}/{top}", timeout=30.0)
                    if err:
                        category_errors.append(err)
                        tv_scanned_ok = False
                    for e in entries:
                        if e.get("isDirectory"):
                            tv_cat_dirs.append((side, e["path"]))
            if scope in ("all", "movie"):
                entries, err = await _safe_list_files(cd2, f"{root}/电影", timeout=30.0)
                if err:
                    category_errors.append(err)
                    movie_scanned_ok = False
                for e in entries:
                    if e.get("isDirectory"):
                        movie_cat_dirs.append((side, e["path"]))

        total_cats = len(tv_cat_dirs) + len(movie_cat_dirs)
        _sync_job.update(
            progress_pct=15,
            stage="scan_categories",
            total_categories=total_cats,
            scanned_categories=0,
            message=f"正在扫描 CD2 共 {total_cats} 个分类目录...",
        )
        _notify_progress()

        # 2. 并发扫描所有分类目录（获得作品文件夹列表）
        tv_show_index_tid: dict[str, list[tuple[str, str]]] = {}     # tid -> [(side, show_path)]
        tv_show_index_title: dict[str, list[tuple[str, str]]] = {}   # norm_title -> [(side, show_path)]
        movie_dir_index_tid: dict[str, list[tuple[str, str]]] = {}   # tid -> [(side, movie_path)]
        movie_dir_index_title: dict[str, list[tuple[str, str]]] = {} # norm_title|year -> [(side, movie_path)]

        scanned_cats = 0

        async def _scan_cat(kind: str, side: str, cpath: str):
            nonlocal scanned_cats, tv_scanned_ok, movie_scanned_ok
            async with sem:
                items, err = await _safe_list_files(cd2, cpath, timeout=60.0)
            scanned_cats += 1
            _sync_job["scanned_categories"] = scanned_cats
            _sync_job["progress_pct"] = 15 + int(40 * scanned_cats / max(1, total_cats))
            _sync_job["message"] = f"已扫描 CD2 分类目录 {scanned_cats}/{total_cats} ({cpath.rsplit('/', 1)[-1]})"
            _notify_progress()
            if err:
                category_errors.append(err)
                if kind == "tv":
                    tv_scanned_ok = False
                else:
                    movie_scanned_ok = False
                return
            for item in items:
                if not item.get("isDirectory"):
                    continue
                tid, nt, yr = _extract_dir_identity(item.get("name", ""))
                ipath = item["path"]
                if kind == "tv":
                    if tid:
                        tv_show_index_tid.setdefault(tid, []).append((side, ipath))
                    elif nt:
                        # 仅当目录本身不含 [tmdbid=...] 时才建立纯标题兜底索引，防止跨作品误匹配
                        tv_show_index_title.setdefault(nt, []).append((side, ipath))
                else:
                    if tid:
                        movie_dir_index_tid.setdefault(tid, []).append((side, ipath))
                    else:
                        # 仅当电影目录不含 [tmdbid=...] 时才建立标题+年份兜底索引（无年份目录才索引纯标题）
                        tk_yr = _title_year_key(nt, yr)
                        if tk_yr:
                            movie_dir_index_title.setdefault(tk_yr, []).append((side, ipath))

        cat_tasks = [
            *(_scan_cat("tv", side, p) for side, p in tv_cat_dirs),
            *(_scan_cat("movie", side, p) for side, p in movie_cat_dirs),
        ]
        if cat_tasks:
            await asyncio.gather(*cat_tasks)

        # 3. 针对命中订阅的电视剧，钻取对应季并合并源+归档去重集数
        if scope in ("all", "tv"):
            tv_drill_jobs: list[tuple[str, str, int, list[tuple[str, str]]]] = []
            for tid, seasons in tv_sub_targets.items():
                dirs = tv_show_index_tid.get(tid)
                if dirs:
                    for sn in seasons:
                        tv_drill_jobs.append(("tid", tid, sn, dirs))
            for nt, seasons in tv_title_targets.items():
                dirs = tv_show_index_title.get(nt)
                if dirs:
                    for sn in seasons:
                        tv_drill_jobs.append(("title", nt, sn, dirs))

            total_tv_jobs = len(tv_drill_jobs)
            drilled_tv_count = 0
            _sync_job.update(
                progress_pct=58,
                stage="drill_tv_seasons",
                message=f"正在核验 {total_tv_jobs} 个命中剧季的 CD2 实存集数 (0/{total_tv_jobs})...",
            )
            _notify_progress()

            async def _drill_one_tv(key_kind: str, ident: str, sn: int, dirs: list[tuple[str, str]]):
                nonlocal drilled_tv_count
                src_eps: set[int] = set()
                arch_eps: set[int] = set()
                all_paths: list[str] = []
                all_sizes: list[int] = []
                for side, show_dir in dirs:
                    eps, paths, sizes = await _scan_season_episodes(cd2, show_dir, sn, sem)
                    if side == "source":
                        src_eps.update(eps)
                    else:
                        arch_eps.update(eps)
                    all_paths.extend(paths)
                    all_sizes.extend(sizes)
                merged = sorted(src_eps | arch_eps)
                avg_mb = round((sum(all_sizes) / len(all_sizes)) / (1024 * 1024), 1) if all_sizes else None
                entry = {
                    "season": sn,
                    "source_count": len(src_eps),
                    "archive_count": len(arch_eps),
                    "total_count": len(merged),
                    "merged_episodes": merged,
                    "avg_video_mb": avg_mb,
                    "paths": list(dict.fromkeys(all_paths)),
                }
                if key_kind == "tid":
                    tv_by_key[f"{ident}:{sn}"] = entry
                else:
                    tv_by_title[f"{ident}:{sn}"] = entry
                drilled_tv_count += 1
                _sync_job["matched_tv"] = len(tv_by_key)
                _sync_job["progress_pct"] = 58 + int(25 * drilled_tv_count / max(1, total_tv_jobs))
                _sync_job["message"] = f"正在核验剧季 CD2 实存集数 ({drilled_tv_count}/{total_tv_jobs})"
                _notify_progress()

            if tv_drill_jobs:
                await asyncio.gather(*(_drill_one_tv(*job) for job in tv_drill_jobs))
            _sync_job["matched_tv"] = len(tv_by_key)

        # 4. 针对命中订阅的电影，核实目录内存在有效正片视频文件
        if scope in ("all", "movie"):
            mov_check_jobs: list[tuple[str, str, list[tuple[str, str]]]] = []
            for tid in movie_sub_tids:
                dirs = movie_dir_index_tid.get(tid)
                if dirs:
                    mov_check_jobs.append(("tid", tid, dirs))
            for tk in movie_sub_titles:
                dirs = movie_dir_index_title.get(tk)
                if dirs:
                    mov_check_jobs.append(("title", tk, dirs))

            total_mov_jobs = len(mov_check_jobs)
            verified_mov_count = 0
            _sync_job.update(
                progress_pct=85,
                stage="verify_movies",
                message=f"正在核验 {total_mov_jobs} 部命中电影的 CD2 实存视频文件 (0/{total_mov_jobs})...",
            )
            _notify_progress()

            async def _verify_one_movie(key_kind: str, ident: str, dirs: list[tuple[str, str]]):
                nonlocal verified_mov_count
                valid_paths: list[str] = []
                video_count = 0
                max_mb = 0.0
                for _side, mdir in dirs:
                    async with sem:
                        files, f_err = await _safe_list_files(cd2, mdir, timeout=30.0)
                    if f_err:
                        continue
                    vids = [f for f in files if _is_valid_movie_video(f)]
                    if vids:
                        valid_paths.append(mdir)
                        video_count += len(vids)
                        for v in vids:
                            mb = int(v.get("size") or 0) / (1024 * 1024)
                            if mb > max_mb:
                                max_mb = mb
                entry = {
                    "in_library": video_count > 0,
                    "video_count": video_count,
                    "max_video_mb": round(max_mb, 1),
                    "paths": list(dict.fromkeys(valid_paths)),
                }
                if key_kind == "tid":
                    movie_by_tmdb[ident] = entry
                else:
                    movie_by_title[ident] = entry
                verified_mov_count += 1
                _sync_job["matched_movies"] = sum(1 for v in movie_by_tmdb.values() if v.get("in_library"))
                _sync_job["progress_pct"] = 85 + int(14 * verified_mov_count / max(1, total_mov_jobs))
                _sync_job["message"] = f"正在核验电影 CD2 实存视频文件 ({verified_mov_count}/{total_mov_jobs})"
                _notify_progress()

            if mov_check_jobs:
                await asyncio.gather(*(_verify_one_movie(*job) for job in mov_check_jobs))
            _sync_job["matched_movies"] = sum(1 for v in movie_by_tmdb.values() if v.get("in_library"))

        synced_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        snapshot = {
            "version": 1,
            "synced_at": synced_at,
            "source_root": source_root,
            "archive_root": archive_root,
            "tv_scanned_ok": tv_scanned_ok,
            "movie_scanned_ok": movie_scanned_ok,
            "tv_by_key": tv_by_key,
            "tv_by_title": tv_by_title,
            "movie_by_tmdb": movie_by_tmdb,
            "movie_by_title": movie_by_title,
            "tv_dirs_indexed": len(tv_show_index_tid) if scope != "movie" else int(prev_snap.get("tv_dirs_indexed") or 0),
            "movie_dirs_indexed": len(movie_dir_index_tid) if scope != "tv" else int(prev_snap.get("movie_dirs_indexed") or 0),
            "category_errors": category_errors[:20],
        }

        async with db_session_factory() as db:
            await save_cd2_snapshot(db, snapshot)

        _sync_job.update(
            status="completed",
            progress_pct=100,
            stage="done",
            synced_at=synced_at,
            errors=category_errors[:10],
            message=(
                f"CD2 实盘同步完成：核实电视剧 {len(tv_by_key)} 季、已入库电影 "
                f"{sum(1 for v in movie_by_tmdb.values() if v.get('in_library'))} 部"
                + (f"（{len(category_errors)} 个目录读取超时）" if category_errors else "")
            ),
        )
        _notify_progress()
        return snapshot
    except Exception as exc:
        log.exception("CD2 实盘库存同步异常")
        _sync_job.update(
            status="failed",
            stage="error",
            message=f"CD2 实盘同步失败: {exc}",
            errors=[str(exc)],
        )
        raise
    finally:
        await cd2.close()


def trigger_cd2_sync_background(db_session_factory, scope: str = "all") -> dict[str, Any]:
    """触发后台异步 CD2 全盘库存同步（防重复并发启动）。"""
    global _sync_task, _sync_job
    if _sync_task is not None and not _sync_task.done():
        return {"started": False, "already_running": True, **get_cd2_sync_status()}

    job_id = uuid.uuid4().hex[:12]
    _sync_job.update(
        job_id=job_id,
        status="running",
        progress_pct=2,
        stage="starting",
        message="正在启动 CD2 实盘库存异步同步...",
        errors=[],
    )
    _sync_task = asyncio.create_task(
        build_cd2_subscription_inventory(db_session_factory, scope=scope, job_id=job_id)
    )
    return {"started": True, "already_running": False, **get_cd2_sync_status()}


async def check_single_subscription_cd2(
    db: AsyncSession,
    sub: dict[str, Any],
) -> dict[str, Any]:
    """对单个订阅执行即时 CD2 实盘核验（优先利用缓存路径或扫描对应类型目录），并更新快照。"""
    settings = get_settings()
    source_root = (settings.source_root or "").rstrip("/")
    archive_root = (settings.archive_root or settings.library_root or "").rstrip("/")
    roots = [(side, r) for side, r in (("source", source_root), ("archive", archive_root)) if r]
    if len(roots) == 2 and roots[0][1] == roots[1][1]:
        roots = [roots[0]]

    raw_type = str(sub.get("type") or sub.get("mtype") or sub.get("media_type") or "").strip().lower()
    is_movie = raw_type in ("movie", "电影") or "电影" in raw_type or "movie" in raw_type
    tid = str(sub.get("tmdbid") or sub.get("tmdb_id") or "").strip()
    name = str(sub.get("name") or sub.get("title") or "").strip()
    year = str(sub.get("year") or "").strip()
    season = int(sub.get("season") or 1)
    norm_name = normalize_title_token(name)

    if not roots:
        return {"ok": False, "checked": False, "message": "未配置 CD2 源/归档目录"}

    cd2 = CloudDrive2Client()
    sem = asyncio.Semaphore(4)
    try:
        top_names = ("电影",) if is_movie else ("电视剧", "动漫")
        cat_dirs: list[tuple[str, str]] = []
        for side, root in roots:
            for top in top_names:
                entries, _ = await _safe_list_files(cd2, f"{root}/{top}", timeout=25.0)
                for e in entries:
                    if e.get("isDirectory"):
                        cat_dirs.append((side, e["path"]))

        matched_dirs: list[tuple[str, str]] = []

        async def _search_cat(side: str, cpath: str):
            async with sem:
                entries, _ = await _safe_list_files(cd2, cpath, timeout=45.0)
            for e in entries:
                if not e.get("isDirectory"):
                    continue
                e_tid, e_nt, e_yr = _extract_dir_identity(e.get("name", ""))
                if tid and e_tid and e_tid == tid:
                    matched_dirs.append((side, e["path"]))
                elif not e_tid and norm_name and e_nt == norm_name and (not year or not e_yr or year == e_yr):
                    matched_dirs.append((side, e["path"]))

        if cat_dirs:
            await asyncio.gather(*(_search_cat(side, p) for side, p in cat_dirs))

        if is_movie:
            valid_paths: list[str] = []
            video_files: list[str] = []
            for _side, mdir in matched_dirs:
                files, _ = await _safe_list_files(cd2, mdir, timeout=25.0)
                vids = [f for f in files if _is_valid_movie_video(f)]
                if vids:
                    valid_paths.append(mdir)
                    video_files.extend(str(v.get("name") or "") for v in vids)

            in_lib = len(video_files) > 0
            # 回写内存与持久化快照中的电影项
            snap = await load_cd2_snapshot(db)
            if snap is not None and tid:
                snap.setdefault("movie_by_tmdb", {})[tid] = {
                    "in_library": in_lib,
                    "video_count": len(video_files),
                    "paths": list(dict.fromkeys(valid_paths)),
                }
                await save_cd2_snapshot(db, snap)

            return {
                "ok": True,
                "checked": True,
                "source": "cd2",
                "media_type": "movie",
                "in_library": in_lib,
                "paths": list(dict.fromkeys(valid_paths)),
                "video_files": video_files[:5],
                "message": (
                    f"CD2 实盘已核实入库（{len(video_files)} 个视频文件）：{valid_paths[0]}"
                    if in_lib
                    else "CD2 源目录与归档库均未检测到该电影视频文件"
                ),
            }
        else:
            src_eps: set[int] = set()
            arch_eps: set[int] = set()
            paths: list[str] = []
            sizes: list[int] = []
            for side, sdir in matched_dirs:
                eps, p_list, sz_list = await _scan_season_episodes(cd2, sdir, season, sem)
                if side == "source":
                    src_eps.update(eps)
                else:
                    arch_eps.update(eps)
                paths.extend(p_list)
                sizes.extend(sz_list)

            merged = sorted(src_eps | arch_eps)
            avg_mb = round((sum(sizes) / len(sizes)) / (1024 * 1024), 1) if sizes else None
            snap = await load_cd2_snapshot(db)
            if snap is not None and tid:
                snap.setdefault("tv_by_key", {})[f"{tid}:{season}"] = {
                    "season": season,
                    "source_count": len(src_eps),
                    "archive_count": len(arch_eps),
                    "total_count": len(merged),
                    "merged_episodes": merged,
                    "avg_video_mb": avg_mb,
                    "paths": list(dict.fromkeys(paths)),
                }
                await save_cd2_snapshot(db, snap)

            return {
                "ok": True,
                "checked": True,
                "source": "cd2",
                "media_type": "tv",
                "season": season,
                "in_library": len(merged) > 0,
                "in_library_episodes": merged,
                "source_count": len(src_eps),
                "archive_count": len(arch_eps),
                "count": len(merged),
                "paths": list(dict.fromkeys(paths)),
                "message": (
                    f"CD2 实盘 S{season:02d} 共实存 {len(merged)} 集（源目录 {len(src_eps)} · 归档库 {len(arch_eps)}）"
                    if merged
                    else f"CD2 源目录与归档库均未检测到 S{season:02d} 正片文件"
                ),
            }
    finally:
        await cd2.close()


async def enrich_subscriptions_with_cd2_and_cache(
    db: AsyncSession,
    subs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """零阻塞将 CD2 实盘快照 + 本地初检 SeasonRecord + 本地 TMDB MetadataCache 叠加注入到订阅字典副本中。

    返回: (enriched_subs, meta_info)
    """
    snapshot = await load_cd2_snapshot(db)

    # 1. 只读加载本地最新 SeasonRecord（已通过 CD2 初检扫出的剧季作为即时补充）
    season_rec_map: dict[str, dict[str, Any]] = {}
    try:
        rows = (
            await db.execute(
                select(SeasonRecord).order_by(SeasonRecord.updated_at.desc())
            )
        ).scalars().all()
        for r in rows:
            if not r.tmdb_id or r.status == "source_absent":
                continue
            key = f"{str(r.tmdb_id).strip()}:{int(r.season_number)}"
            if key in season_rec_map:
                continue
            try:
                rec_eps = json.loads(r.recognized_episodes_json or "[]")
            except Exception:
                rec_eps = []
            # 同时吸收归档证据里的集号（若有）
            arch_eps: list[int] = []
            try:
                ev = json.loads(r.evidence_json or "{}")
                if isinstance(ev.get("archive_episodes"), list):
                    arch_eps = [int(x) for x in ev["archive_episodes"] if isinstance(x, (int, float))]
            except Exception:
                pass
            merged = sorted({int(x) for x in rec_eps if isinstance(x, (int, float))} | set(arch_eps))
            cnt = max(len(merged), int(r.local_episode_count or 0))
            season_rec_map[key] = {
                "source_count": len(rec_eps) or int(r.local_episode_count or 0),
                "archive_count": len(arch_eps),
                "total_count": cnt,
                "merged_episodes": merged,
                "paths": [p for p in (r.season_path, r.archive_target_path) if p],
                "season_finished": bool(r.season_finished),
                "total_episodes": r.total_episodes,
            }
    except Exception as exc:
        log.warning("读取本地 SeasonRecord 辅助 CD2 索引失败: %s", exc)

    # 2. 只读加载本地 MetadataCache 中已有 TMDB 剧集与季缓存（用于短剧时长/类型及 99 集占位纠偏，零外部请求）
    tmdb_cache_map: dict[str, dict[str, Any]] = {}
    tmdb_season_cache_map: dict[str, dict[str, Any]] = {}
    try:
        cache_keys: set[str] = set()
        for s in subs:
            tid_s = str(s.get("tmdbid") or s.get("tmdb_id") or "").strip()
            if not tid_s.isdigit():
                continue
            cache_keys.add(f"tmdb_tv:{tid_s}")
            sn_s = int(s.get("season") or 1)
            cache_keys.add(f"tmdb_tv_season:{tid_s}:{sn_s}")
        if cache_keys:
            # SQLite IN 子句分块查询，防止超 999 参数上限
            key_list = list(cache_keys)
            for i in range(0, len(key_list), 400):
                chunk = key_list[i:i + 400]
                mc_rows = (
                    await db.execute(select(MetadataCache).where(MetadataCache.cache_key.in_(chunk)))
                ).scalars().all()
                for mr in mc_rows:
                    try:
                        payload = json.loads(mr.payload_json or "{}")
                        if not (isinstance(payload, dict) and payload):
                            continue
                        if str(mr.cache_key).startswith("tmdb_tv_season:"):
                            tmdb_season_cache_map[f"{mr.media_id}:{mr.season_number}"] = payload
                        else:
                            tmdb_cache_map[str(mr.media_id)] = payload
                    except Exception:
                        pass
    except Exception as exc:
        log.warning("读取 MetadataCache 辅助订阅诊断失败: %s", exc)

    settings = get_settings()
    cd2_configured = bool(settings.cd2_url and (settings.source_root or settings.archive_root or settings.library_root))

    tv_by_key = (snapshot or {}).get("tv_by_key") or {}
    tv_by_title = (snapshot or {}).get("tv_by_title") or {}
    movie_by_tmdb = (snapshot or {}).get("movie_by_tmdb") or {}
    movie_by_title = (snapshot or {}).get("movie_by_title") or {}
    tv_scanned_ok = bool((snapshot or {}).get("tv_scanned_ok"))
    movie_scanned_ok = bool((snapshot or {}).get("movie_scanned_ok"))

    enriched: list[dict[str, Any]] = []
    cd2_matched_count = 0

    for raw in subs:
        s = dict(raw)
        if cd2_configured:
            s["strict_cd2_only"] = True
        raw_type = str(s.get("type") or s.get("mtype") or s.get("media_type") or "").strip().lower()
        is_mov = (
            raw_type in ("movie", "电影")
            or "电影" in raw_type
            or "movie" in raw_type
            or (s.get("season") is None and not s.get("total_episode") and bool(s.get("year")))
        )
        tid = str(s.get("tmdbid") or s.get("tmdb_id") or "").strip()
        name = str(s.get("name") or s.get("title") or "").strip()
        year = str(s.get("year") or "").strip()

        if is_mov:
            hit = movie_by_tmdb.get(tid) if tid else None
            if hit is None:
                hit = movie_by_title.get(_title_year_key(name, year))
            if hit is not None:
                s["cd2_verified"] = True
                s["cd2_in_library"] = bool(hit.get("in_library"))
                s["cd2_paths"] = list(hit.get("paths") or [])
                cd2_matched_count += 1
            elif snapshot is not None and movie_scanned_ok:
                # 全盘电影分类完整扫描通过且未命中 → 明确标记 CD2 未入库
                s["cd2_verified"] = True
                s["cd2_in_library"] = False
                s["cd2_paths"] = []
        else:
            sn = int(s.get("season") or 1)
            key = f"{tid}:{sn}" if tid else ""
            nt_key = f"{normalize_title_token(name)}:{sn}" if name else ""
            hit = tv_by_key.get(key) if key else None
            if hit is None and nt_key:
                hit = tv_by_title.get(nt_key)
            if hit is None and key and (snapshot is None or not tv_scanned_ok):
                hit = season_rec_map.get(key)

            if hit is not None:
                s["cd2_verified"] = True
                s["cd2_source_episodes"] = int(hit.get("source_count") or 0)
                s["cd2_archive_episodes"] = int(hit.get("archive_count") or 0)
                s["cd2_total_episodes"] = int(hit.get("total_count") or 0)
                s["cd2_episode_numbers"] = list(hit.get("merged_episodes") or [])
                s["cd2_paths"] = list(hit.get("paths") or [])
                if hit.get("avg_video_mb") is not None:
                    s["cd2_avg_video_mb"] = hit.get("avg_video_mb")
                if hit.get("season_finished") is True:
                    s["season_finished"] = True
                if hit.get("total_episodes"):
                    s["tmdb_season_episodes"] = hit.get("total_episodes")
                cd2_matched_count += 1
            elif snapshot is not None and tv_scanned_ok:
                # 全盘剧集/动漫分类完整扫描通过且未命中 → CD2 实存严格为 0 集
                s["cd2_verified"] = True
                s["cd2_source_episodes"] = 0
                s["cd2_archive_episodes"] = 0
                s["cd2_total_episodes"] = 0
                s["cd2_episode_numbers"] = []
                s["cd2_paths"] = []

            # 注入已有 TMDB 缓存的时长、类型与季集数（用于短剧高精度判定与 99 集纠偏，零额外网络调用）
            cd2_ep_val = int(s.get("cd2_total_episodes") or 0)
            if tid and tid in tmdb_cache_map:
                td = tmdb_cache_map[tid]
                if isinstance(td.get("episode_run_time"), list):
                    s["tmdb_episode_run_time"] = td["episode_run_time"]
                if isinstance(td.get("genres"), list):
                    s["tmdb_genre_ids"] = [g.get("id") for g in td["genres"] if isinstance(g, dict) and g.get("id")]
                elif isinstance(td.get("genre_ids"), list):
                    s["tmdb_genre_ids"] = td["genre_ids"]
                if isinstance(td.get("origin_country"), list):
                    s["tmdb_origin_country"] = td["origin_country"]
                if isinstance(td.get("seasons"), list):
                    for ts in td["seasons"]:
                        if isinstance(ts, dict) and int(ts.get("season_number") or -1) == sn:
                            ep_cnt = int(ts.get("episode_count") or 0)
                            if ep_cnt >= 2 and ep_cnt >= cd2_ep_val and not s.get("tmdb_season_episodes"):
                                s["tmdb_season_episodes"] = ep_cnt
                            break

            if key and key in tmdb_season_cache_map:
                sd = tmdb_season_cache_map[key]
                ep_cnt = int(sd.get("episode_count") or sd.get("aired_count") or 0)
                if ep_cnt >= 2 and ep_cnt >= cd2_ep_val:
                    s["tmdb_season_episodes"] = ep_cnt
                    if sd.get("season_finished") is True:
                        s["season_finished"] = True

        enriched.append(s)

    meta_info = {
        "cd2_synced": snapshot is not None,
        "cd2_synced_at": (snapshot or {}).get("synced_at"),
        "cd2_tv_ok": tv_scanned_ok,
        "cd2_movie_ok": movie_scanned_ok,
        "cd2_matched_count": cd2_matched_count,
        "sync_status": get_cd2_sync_status(),
    }
    return enriched, meta_info
