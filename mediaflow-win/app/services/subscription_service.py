"""订阅资源核查、状态管理与已确认源文件的可恢复清理。

严格遵循：
- 《MediaFlow剧集订阅智能清理需求与交互方案-v2.5.65.md》
- 《MediaFlow电影订阅智能清理需求与交互方案-v2.5.65.md》
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, update

from app.clients.clouddrive2 import CloudDrive2Client
from app.clients.moviepilot import MoviePilotClient, parse_season_str
from app.config import get_settings
from app.core.directory_governance import (
    find_blocking_group_for_record,
    normalize_dir_path,
    resolve_category_key,
)
from app.core.episode_parser import is_video_file, parse_episode
from app.core.identity_resolver import normalize_title_token
from app.core.subscription_cleaner import diagnose_subscription
from app.models.directory_group import DirectoryGroup
from app.models.operation import OperationLog
from app.models.season import SeasonRecord
from app.services.subscription_cd2_index import (
    _extract_dir_identity,
    _parse_season_dirname,
    _safe_list_files,
    load_cd2_snapshot,
)

log = logging.getLogger("mediaflow.subscription_service")


def is_movie_sub(sub: dict[str, Any]) -> bool:
    """精准判断订阅是否为电影类型。"""
    raw_type = str(sub.get("type") or sub.get("mtype") or sub.get("media_type") or "").strip().lower()
    return (
        raw_type in ("movie", "电影")
        or "电影" in raw_type
        or "movie" in raw_type
        or (sub.get("season") is None and not sub.get("total_episode") and bool(sub.get("year")))
    )


def identity(sub: dict[str, Any]) -> tuple[str, int | None]:
    """提取作品 TMDB ID 与季号（电影季号为 None，剧集默认 1）。"""
    tid = str(sub.get("tmdbid") or sub.get("tmdb_id") or "")
    if is_movie_sub(sub):
        return tid, None
    return tid, int(sub.get("season") or 1)


def stamp(sub: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id", "name", "tmdbid", "tmdb_id", "season", "state", "completed_episode", "lack_episode",
        "total_episode", "best_version", "best_version_full", "custom_words", "type", "mtype"
    )
    return {k: sub.get(k) for k in keys}


async def subscription(mp: MoviePilotClient, sub_id: int) -> dict[str, Any] | None:
    subs = await mp.list_subscriptions()
    return next((s for s in subs if int(s.get("id") or s.get("sub_id") or 0) == sub_id), None)


async def resource_check(mp: MoviePilotClient, sub: dict[str, Any]) -> dict[str, Any]:
    """对订阅开展多站点可用资源检索与缺口诊断（剧集/电影严格类型隔离）。"""
    tid, season = identity(sub)
    title = str(sub.get("name") or sub.get("title") or "")
    is_movie = is_movie_sub(sub)

    if not tid.isdigit():
        return {"status": "unknown", "message": "作品 ID 未确认，请先核对作品身份", "resources": []}

    try:
        if is_movie:
            # 🎬 电影模式：取消季号与集号要求，关键词不追加 S01，以电影 mtype 检索
            rows = await mp.search_resources(tmdb_id=int(tid), title=title, media_type="movie")
            if not rows and title:
                rows = await mp.search_resources(title=title, media_type="movie")

            resources = []
            for row in rows:
                media, meta, torrent = (row.get(k) or {} for k in ("media_info", "meta_info", "torrent_info"))
                row_tid = str(media.get("tmdb_id") or "")
                if row_tid and row_tid != tid:
                    continue
                resources.append({
                    "title": torrent.get("title"),
                    "seeders": torrent.get("seeders") or 0,
                    "size": torrent.get("size") or 0,
                    "site_name": torrent.get("site_name") or "",
                })
            return {
                "status": "found" if resources else "no_match",
                "message": f"找到 {len(resources)} 个电影候选资源" if resources else "本次全网搜索未命中可用电影资源",
                "resources": resources,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "site_coverage": "checked",
                "media_type": "movie",
            }
        else:
            # 📺 电视剧模式：严格指定季号，匹配同季与集数缺口
            season_num = season if season is not None else 1
            rows = await mp.search_resources(tmdb_id=int(tid), season=season_num, title=title, media_type="tv")
            if not rows:
                rows = await mp.search_resources(title=title, season=season_num, media_type="tv")
            resources = []
            for row in rows:
                media, meta, torrent = (row.get(k) or {} for k in ("media_info", "meta_info", "torrent_info"))
                if str(media.get("tmdb_id") or "") != tid:
                    continue
                seasons = meta.get("season_list") or [meta.get("begin_season")]
                if season_num not in seasons:
                    continue
                resources.append({
                    "title": torrent.get("title"),
                    "seeders": torrent.get("seeders") or 0,
                    "episodes": meta.get("episode_list") or [],
                    "total": meta.get("total_episode"),
                    "site_name": torrent.get("site_name") or "",
                })
            return {
                "status": "found" if resources else "no_match",
                "message": f"找到 {len(resources)} 个同季资源，需核对是否覆盖缺集" if resources else "本次搜索未命中；站点完整性未知",
                "resources": resources,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "site_coverage": "checked",
                "media_type": "tv",
            }
    except Exception as exc:
        return {"status": "unknown", "message": "资源查询未完成：" + type(exc).__name__, "resources": []}


async def check_queues(mp: MoviePilotClient, sub: dict[str, Any]) -> None:
    """检查活动下载与整理队列，严格按作品 ID、媒体类型与季度隔离任务阻塞（正常做种不阻塞退订）。"""
    tid, season = identity(sub)
    is_movie = is_movie_sub(sub)
    sub_name = str(sub.get("name") or sub.get("title") or "").strip()

    active_downloads = await mp.get_active_downloads()
    transfer_queue = await mp.get_transfer_queue()

    for queue, q_name in ((active_downloads, "下载"), (transfer_queue, "整理")):
        if not isinstance(queue, list):
            raise HTTPException(409, f"{q_name}队列状态未知，请稍后重试")

        for item in queue:
            # 状态检查：正常做种任务 (seeding / finished / upload) 不持续阻挡完成退订 (F10, T09)
            item_state = str(item.get("state") or item.get("status") or "").lower()
            if item_state in ("seeding", "uploading", "completed", "finished", "done", "paused"):
                continue

            name = str(item.get("title") or item.get("name") or "")
            item_id = str(
                item.get("tmdbid")
                or item.get("tmdb_id")
                or (item.get("media_info") or {}).get("tmdb_id")
                or ""
            )

            # 如果任务没有 ID 且标题完全不含当前作品名，说明属于无关作品，不阻断当前订阅
            if not item_id and sub_name and sub_name not in name:
                continue

            # 身份匹配核验
            matched_id = bool(tid and item_id and item_id == tid)
            matched_name = bool(sub_name and sub_name in name)

            if matched_id or matched_name:
                if is_movie:
                    # 电影若仍在活跃下载或整理中，阻塞退订
                    raise HTTPException(409, f"本片尚在{q_name}队列中处理，请先等待入库或在下载器中处理")
                else:
                    # 电视剧：核对季号，不同季的下载不误阻塞当前季
                    if season is not None:
                        item_seasons = parse_season_str(
                            item.get("seasons")
                            or item.get("season")
                            or (item.get("meta_info") or {}).get("season")
                        )
                        if item_seasons and season not in item_seasons:
                            # 属于同部剧的其他季度，不阻塞本季
                            continue
                    raise HTTPException(409, f"本剧该季尚在{q_name}队列中处理，请先处理队列")


def fingerprint(item: dict[str, Any]) -> dict[str, Any]:
    return {k: item.get(k) for k in ("name", "path", "size", "modified", "id", "isDirectory")}


def source_path(path: str) -> str:
    settings = get_settings()
    normalized = posixpath.normpath(path)
    root = posixpath.normpath(settings.source_root) if settings.source_root else ""
    protected = [posixpath.normpath(p) for p in (settings.archive_root, settings.library_root) if p]
    if not root or not normalized.startswith(root.rstrip("/") + "/") or normalized != path:
        raise HTTPException(409, "源季路径不属于受控源根的严格子目录")
    if any(normalized == p or normalized.startswith(p + "/") or p.startswith(normalized + "/") for p in protected):
        raise HTTPException(409, "源季路径与归档或媒体库重叠")
    return normalized


async def pause_subscription(mp: MoviePilotClient, sub_id: int) -> dict[str, Any]:
    """将订阅置为暂停观察状态 (S) 并返回更新结果。"""
    res = await mp.update_subscription_status(sub_id, "S")
    return {"ok": True, "sub_id": sub_id, "state": "S", "message": "已将订阅置为暂停观察状态 (S)", "raw": res}


async def resume_subscription(mp: MoviePilotClient, sub_id: int) -> dict[str, Any]:
    """将订阅恢复为活跃追更状态 (R) 并返回更新结果。"""
    res = await mp.update_subscription_status(sub_id, "R")
    return {"ok": True, "sub_id": sub_id, "state": "R", "message": "已将订阅恢复为活跃追更状态 (R)", "raw": res}


async def discover_source_season_entries(
    db,
    disk: CloudDrive2Client | None,
    sub: dict[str, Any],
) -> list[dict[str, Any]]:
    """只读定位剧集订阅对应的受控源季目录（优先 SeasonRecord；无初检记录时回退 CD2 快照与源根目录实盘扫描）。

    安全边界：
    - 电影订阅直接返回空列表；
    - 仅接受严格位于 source_root 下且基名明确为目标季目录（_parse_season_dirname == season）的路径；
    - 绝不返回归档/媒体库目录或整剧根目录。
    """
    if is_movie_sub(sub):
        return []
    tid, season = identity(sub)
    if season is None:
        return []

    unique: dict[str, dict[str, Any]] = {}
    if tid.isdigit():
        rows = await db.scalars(select(SeasonRecord).where(
            SeasonRecord.tmdb_id == tid,
            SeasonRecord.season_number == season,
            SeasonRecord.status != "manually_handled",
        ))
        for row in rows:
            if row.season_path:
                unique[row.season_path] = {
                    "id": row.id,
                    "path": row.season_path,
                    "origin": "season_record",
                }
    if unique:
        return list(unique.values())

    settings = get_settings()
    src_root = posixpath.normpath(settings.source_root) if settings.source_root else ""
    if not src_root:
        return []

    title = str(sub.get("name") or sub.get("title") or "").strip()
    norm_title = normalize_title_token(title)

    def _accept_cd2_season_path(raw_path: str) -> str | None:
        if not raw_path:
            return None
        norm_p = posixpath.normpath(str(raw_path))
        if _parse_season_dirname(posixpath.basename(norm_p)) != season:
            return None
        try:
            return source_path(norm_p)
        except HTTPException:
            return None

    # 1. 优先尝试已落盘的 CD2 订阅实盘快照（零额外远端扫描耗时）
    snap = await load_cd2_snapshot(db)
    if isinstance(snap, dict):
        tv_by_key = snap.get("tv_by_key") or {}
        tv_by_title = snap.get("tv_by_title") or {}
        hit = tv_by_key.get(f"{tid}:{season}") if tid.isdigit() else None
        if hit is None and norm_title:
            hit = tv_by_title.get(f"{norm_title}:{season}")
        if isinstance(hit, dict):
            for p in hit.get("paths") or []:
                accepted = _accept_cd2_season_path(p)
                if accepted:
                    unique[accepted] = {"id": f"cd2:{accepted}", "path": accepted, "origin": "cd2"}
        if unique:
            return list(unique.values())

    # 2. 快照未命中或尚未同步时，按 source_root/{电视剧,动漫}/* 实时只读扫描源季目录
    if disk is None:
        return []

    sem = asyncio.Semaphore(4)
    cat_dirs: list[str] = []
    for top in ("电视剧", "动漫"):
        entries, _ = await _safe_list_files(disk, f"{src_root.rstrip('/')}/{top}", timeout=25.0)
        for e in entries:
            if e.get("isDirectory") and e.get("path"):
                cat_dirs.append(str(e["path"]))

    matched_show_dirs: list[str] = []

    async def _scan_cat_dir(cpath: str) -> None:
        async with sem:
            items, _ = await _safe_list_files(disk, cpath, timeout=35.0)
        for it in items:
            if not it.get("isDirectory") or not it.get("path"):
                continue
            e_tid, e_nt, _ = _extract_dir_identity(str(it.get("name") or ""))
            if tid.isdigit() and e_tid and e_tid == tid:
                matched_show_dirs.append(str(it["path"]))
            elif not e_tid and norm_title and e_nt == norm_title:
                matched_show_dirs.append(str(it["path"]))

    if cat_dirs:
        await asyncio.gather(*(_scan_cat_dir(cp) for cp in cat_dirs))

    for show_dir in dict.fromkeys(matched_show_dirs):
        async with sem:
            children, _ = await _safe_list_files(disk, show_dir, timeout=30.0)
        for child in children:
            if not child.get("isDirectory") or not child.get("path"):
                continue
            accepted = _accept_cd2_season_path(str(child["path"]))
            if accepted:
                unique[accepted] = {"id": f"cd2:{accepted}", "path": accepted, "origin": "cd2"}

    return list(unique.values())


async def preview(db, mp, disk, sub_id, cleanup, record_id=None):
    sub = await subscription(mp, sub_id)
    if not sub:
        raise HTTPException(404, "订阅不存在")

    is_movie = is_movie_sub(sub)
    if is_movie and cleanup:
        # 严格隔离规范（v2.5.40 第 7 节）：电影自动操作严禁联动删除源视频！
        raise HTTPException(400, "电影暂不支持联动删除源视频文件，仅支持取消订阅！电影操作与剧季源目录严格隔离。")

    if diagnose_subscription(sub).is_protected:
        raise HTTPException(409, "订阅处于保护状态，请先核对保护设置")

    evidence = await resource_check(mp, sub)
    await check_queues(mp, sub)

    data = {
        "sub_id": sub_id,
        "subscription": stamp(sub),
        "cleanup": cleanup,
        "media_type": "movie" if is_movie else "tv",
        "evidence": evidence,
        "deleted": [],
        "unsubscribed": False,
        "files": [],
        "inventory": [],
    }

    if cleanup and not is_movie:
        tid, season = identity(sub)
        if not tid.isdigit():
            raise HTTPException(409, "作品 ID 未确认")
        records = list(await db.scalars(select(SeasonRecord).where(
            SeasonRecord.tmdb_id == tid, SeasonRecord.season_number == season
        )))
        use_cd2_fallback = (not records) or (isinstance(record_id, str) and record_id.startswith("cd2:"))
        if not use_cd2_fallback:
            target_recs = [r for r in records if record_id is None or r.id == record_id]
            for tr in target_recs:
                blocked_group = await find_blocking_group_for_record(
                    db, tr, cd2_client=disk, operation="subscription_cleanup"
                )
                if blocked_group is not None:
                    await db.commit()
                    raise HTTPException(
                        409,
                        f"存在未确认的{blocked_group.title or '同 ID 多目录组'}（TMDB {blocked_group.tmdb_id}），"
                        f"已暂停退订联动清理：{blocked_group.invalidated_reason or '请先确认归档目标或标记有意共存'}",
                    )
            paths = {r.season_path for r in target_recs}
            if len(paths) != 1:
                raise HTTPException(409, "未定位到唯一源季目录，请先初检或明确选择该季记录")
            path = source_path(paths.pop())
            data["record_ids"] = [r.id for r in target_recs]
            if any(r.show_path == path for r in records):
                raise HTTPException(409, "该目录与整剧目录相同，请先人工确认季目录")
        else:
            if isinstance(record_id, str) and record_id.startswith("cd2:"):
                candidate_paths = [posixpath.normpath(record_id[4:])]
            else:
                discovered = await discover_source_season_entries(db, disk, sub)
                candidate_paths = [str(e["path"]) for e in discovered if e.get("path")]

            if len(candidate_paths) != 1:
                raise HTTPException(409, "CD2 源目录中未定位到唯一源季目录（可能已归档或存在多目录），可选择仅退订")

            path = source_path(candidate_paths[0])
            if _parse_season_dirname(posixpath.basename(path)) != season:
                raise HTTPException(409, "目标目录非明确的季子目录（拒绝定位整剧根目录），请先人工核对")

            settings = get_settings()
            if settings.enable_multi_dir_governance:
                norm_show = normalize_dir_path(posixpath.dirname(path))
                cat_key = resolve_category_key(norm_show, settings.source_root, settings.archive_root)
                groups = list(await db.scalars(select(DirectoryGroup).where(
                    DirectoryGroup.tmdb_id == tid, DirectoryGroup.status == "pending_confirm"
                )))
                for g in groups:
                    try:
                        entries = json.loads(g.paths_json or "[]")
                    except Exception:
                        entries = []
                    paths_in_group = {normalize_dir_path(e.get("norm_path") or "") for e in entries if isinstance(e, dict)}
                    if norm_show in paths_in_group or (cat_key and g.category_key == cat_key):
                        raise HTTPException(
                            409,
                            f"存在未确认的{g.title or '同 ID 多目录组'}（TMDB {g.tmdb_id}），"
                            f"已暂停退订联动清理：{g.invalidated_reason or '请先确认归档目标或标记有意共存'}",
                        )
            data["record_ids"] = []
        items = await disk.list_files(path, force_refresh=True)
        if any(f.get("isDirectory") for f in items):
            raise HTTPException(409, "季内含子目录，请先核对目录归属")
        data.update(path=path, inventory=[fingerprint(f) for f in items])
        has_verified_video = False
        for item in items:
            if is_video_file(item["name"]):
                parsed = parse_episode(item["name"], item["path"], item.get("size", 0))
                if parsed.season_number != season or not parsed.episode_numbers or parsed.is_extra or parsed.is_special:
                    raise HTTPException(409, "源季含归属未确认的视频，请先核对")
                has_verified_video = True
            if posixpath.dirname(item["path"]) != path:
                raise HTTPException(409, "返回的文件路径超出源季目录")
            data["files"].append(fingerprint(item))
        if not has_verified_video and not data["files"]:
            raise HTTPException(409, "该季没有可确认的源文件，可选择仅退订")

    op = OperationLog(action="subscription_cleanup", status="pending", detail_json=json.dumps(data, ensure_ascii=False))
    db.add(op)
    await db.commit()
    return {
        "operation_id": op.id,
        **data,
        "bytes": sum(f.get("size") or 0 for f in data["files"]),
        "notice": (
            "退订并彻底删除该季源目录（包含视频、字幕、NFO与元数据）；"
            "若该剧无其他季度或文件，将一并清理剧集空目录并通过 CD2 刷新"
            if cleanup
            else "仅退订订阅记录，源端和媒体库所有文件均完整保留"
        ),
    }


async def execute(db, mp, disk, operation_id):
    op = await db.get(OperationLog, operation_id)
    if not op or op.action != "subscription_cleanup":
        raise HTTPException(404, "操作不存在")
    if op.status == "completed":
        return {"ok": True, "message": "操作已完成", "operation_id": op.id}
    original = op.detail_json
    data = json.loads(original)
    if op.status == "executing" and time.time() - data.get("lease", 0) < 300:
        raise HTTPException(409, "操作正在执行，请稍后查看")
    data["lease"] = time.time()
    claimed = await db.execute(update(OperationLog).where(
        OperationLog.id == op.id, OperationLog.detail_json == original
    ).values(status="executing", detail_json=json.dumps(data, ensure_ascii=False)))
    await db.commit()
    if not claimed.rowcount:
        raise HTTPException(409, "操作状态已变化，请重试")
    others = await db.scalars(select(OperationLog).where(
        OperationLog.action == "subscription_cleanup", OperationLog.status == "executing", OperationLog.id != op.id
    ))
    if any(json.loads(other.detail_json).get("sub_id") == data["sub_id"] for other in others):
        op.status = "failed"
        await db.commit()
        raise HTTPException(409, "同一订阅已有执行中的操作，请先处理该操作")

    async def save(status):
        op.status = status
        op.detail_json = json.dumps(data, ensure_ascii=False)
        await db.commit()

    try:
        current = await subscription(mp, data["sub_id"])
        if current:
            if stamp(current) != data["subscription"] or diagnose_subscription(current).is_protected:
                raise HTTPException(409, "订阅已变化，请重新预览")
        elif not data["unsubscribed"]:
            data["unsubscribed"] = True

        await check_queues(mp, current or data["subscription"])

        if data["cleanup"] and data.get("media_type") != "movie":
            path = source_path(data["path"])
            check_recs = list(await db.scalars(select(SeasonRecord).where(SeasonRecord.season_path == path)))
            for cr in check_recs:
                blocked_group = await find_blocking_group_for_record(
                    db, cr, cd2_client=disk, operation="subscription_cleanup"
                )
                if blocked_group is not None:
                    op.status = "failed"
                    await db.commit()
                    raise HTTPException(
                        409,
                        f"存在未确认或已失效的{blocked_group.title or '同 ID 多目录组'}（TMDB {blocked_group.tmdb_id}），已中止退订清理",
                    )
            items = [fingerprint(f) for f in await disk.list_files(path, force_refresh=True)]
            expected = [f for f in data["inventory"] if f["path"] not in data["deleted"]]
            actual = {f["path"]: f for f in items}
            removable = {f["path"] for f in data["files"]}
            if any(actual.get(f["path"]) != f and (f["path"] in actual or f["path"] not in removable) for f in expected) or any(f["path"] not in {e["path"] for e in expected} for f in items):
                raise HTTPException(409, "源目录内容已变化，请重新预览剩余文件")

        if current:
            await mp.delete_subscription(data["sub_id"])
            data["unsubscribed"] = True
            await save("executing")

        show_dir_deleted = False
        season_dir_deleted = False
        if data["cleanup"] and data.get("media_type") != "movie":
            for f in data["files"]:
                if f["path"] in data["deleted"]:
                    continue
                existing = await disk.get_file_info(f["path"])
                if existing:
                    if fingerprint(existing) != f:
                        raise HTTPException(409, "文件已变化，保留待核对")
                    result = await disk.delete_file(f["path"])
                    if result.get("success") is not True:
                        raise RuntimeError("网盘未确认删除成功")
                    if await disk.get_file_info(f["path"]):
                        raise RuntimeError("删除后文件仍可见，待复核")
                data["deleted"].append(f["path"])
                await save("executing")

            # 彻底删除该季源目录本身，并通过 CD2 刷新父级
            season_path = data["path"]
            show_path = posixpath.dirname(season_path)
            try:
                del_season_res = await disk.delete_file(season_path)
                if del_season_res.get("success") is True:
                    season_dir_deleted = True
            except Exception as s_err:
                log.warning("清理季目录异常: %s", s_err)

            # 检查剧集根目录：若该剧集没有其它季或文件在目录中，则删除整个剧集目录并刷新 CD2
            try:
                parent_items = await disk.list_files(show_path, force_refresh=True)
                meaningful_items = [
                    it for it in parent_items
                    if it.get("path") != season_path and it.get("name") not in (".DS_Store", "Thumbs.db")
                ]
                settings = get_settings()
                src_root_norm = posixpath.normpath(settings.source_root) if settings.source_root else ""
                is_safe_show_dir = (
                    src_root_norm
                    and show_path.startswith(src_root_norm.rstrip("/") + "/")
                    and show_path != src_root_norm
                    and len(show_path.strip("/").split("/")) > len(src_root_norm.strip("/").split("/")) + 1
                )
                if not meaningful_items and is_safe_show_dir:
                    del_show_res = await disk.delete_file(show_path)
                    if del_show_res.get("success") is True:
                        show_dir_deleted = True
                        cat_dir = posixpath.dirname(show_path)
                        await disk.list_files(cat_dir, force_refresh=True)
            except Exception as show_err:
                log.warning("清理剧集空目录异常: %s", show_err)

            records = list(await db.scalars(select(SeasonRecord).where(SeasonRecord.season_path == data["path"])))
            for record in records:
                if record.status != "manually_handled":
                    record.status = "archive_check"
                    record.local_episode_count = 0
                    record.recognized_episodes_json = "[]"
                    record.missing_episodes_json = "[]"
                    record.duplicate_episodes_json = "[]"
                    record.status_detail = "已彻底清理该季源目录（含视频、字幕与元数据）" + ("，并清理剧集空目录" if show_dir_deleted else "")
            from app.api.archive import _refresh_task_summary
            for task_id in {r.task_id for r in records}:
                await _refresh_task_summary(task_id, db)

        await save("completed")
        try:
            from app.services.subscription_governance_service import mutate_unified_snapshot_on_delete
            await mutate_unified_snapshot_on_delete(db, [int(data["sub_id"])])
        except Exception as snap_err:
            log.warning("刷新单条退订本地快照失败: %s", snap_err)
        del_msg = (
            f"已退订，已彻底删除该季源目录及 {len(data['deleted'])} 个文件"
            + ("（该剧无其它季，已一并清理剧集空目录并刷新 CD2）" if show_dir_deleted else "（已刷新 CD2 剧集目录）")
            if data["cleanup"]
            else "已成功退订，媒体与做种任务完整保留"
        )
        return {
            "ok": True,
            "message": del_msg,
            "operation_id": op.id,
            "season_dir_deleted": season_dir_deleted,
            "show_dir_deleted": show_dir_deleted,
        }
    except Exception as exc:
        await save("failed")
        if data.get("unsubscribed"):
            try:
                from app.services.subscription_governance_service import mutate_unified_snapshot_on_delete
                await mutate_unified_snapshot_on_delete(db, [int(data["sub_id"])])
            except Exception:
                pass
        message = exc.detail if isinstance(exc, HTTPException) else type(exc).__name__
        return {
            "ok": False,
            "message": f"{'已退订' if data['unsubscribed'] else '退订未确认'}；{message}，可使用同一操作重试",
            "operation_id": op.id,
        }
