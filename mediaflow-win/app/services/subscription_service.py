"""订阅资源核查、状态管理与已确认源文件的可恢复清理。

严格遵循：
- 《MediaFlow剧集订阅智能清理需求与交互方案-v2.5.65.md》
- 《MediaFlow电影订阅智能清理需求与交互方案-v2.5.65.md》
"""

from __future__ import annotations

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
from app.core.directory_governance import find_blocking_group_for_record
from app.core.episode_parser import is_video_file, parse_episode
from app.core.subscription_cleaner import diagnose_subscription
from app.models.operation import OperationLog
from app.models.season import SeasonRecord

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
        items = await disk.list_files(path, force_refresh=True)
        if any(f.get("isDirectory") for f in items):
            raise HTTPException(409, "季内含子目录，请先核对目录归属")
        data.update(path=path, inventory=[fingerprint(f) for f in items])
        for item in items:
            if not is_video_file(item["name"]):
                continue
            parsed = parse_episode(item["name"], item["path"], item.get("size", 0))
            if parsed.season_number != season or not parsed.episode_numbers or parsed.is_extra or parsed.is_special:
                raise HTTPException(409, "源季含归属未确认的视频，请先核对")
            if posixpath.dirname(item["path"]) != path:
                raise HTTPException(409, "返回的文件路径超出源季目录")
            data["files"].append(fingerprint(item))
        if not data["files"]:
            raise HTTPException(409, "该季没有可确认的源视频，可选择仅退订")

    op = OperationLog(action="subscription_cleanup", status="pending", detail_json=json.dumps(data, ensure_ascii=False))
    db.add(op)
    await db.commit()
    return {
        "operation_id": op.id,
        **data,
        "bytes": sum(f.get("size") or 0 for f in data["files"]),
        "notice": "这些源视频可能是唯一副本；字幕、NFO、海报保留在源端" if cleanup else "仅退订订阅记录，源端和媒体库所有文件均完整保留",
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

            records = list(await db.scalars(select(SeasonRecord).where(SeasonRecord.season_path == data["path"])))
            for record in records:
                if record.status != "manually_handled":
                    record.status = "archive_check"
                    record.local_episode_count = 0
                    record.recognized_episodes_json = "[]"
                    record.missing_episodes_json = "[]"
                    record.duplicate_episodes_json = "[]"
                    record.status_detail = "已按退订确认清理源视频，字幕、NFO 和海报留在源目录。"
            from app.api.archive import _refresh_task_summary
            for task_id in {r.task_id for r in records}:
                await _refresh_task_summary(task_id, db)

        await save("completed")
        return {
            "ok": True,
            "message": f"已退订，已清理 {len(data['deleted'])} 个确认的源视频" if data["cleanup"] else "已成功退订，媒体与做种任务完整保留",
            "operation_id": op.id,
        }
    except Exception as exc:
        await save("failed")
        message = exc.detail if isinstance(exc, HTTPException) else type(exc).__name__
        return {
            "ok": False,
            "message": f"{'已退订' if data['unsubscribed'] else '退订未确认'}；{message}，可使用同一操作重试",
            "operation_id": op.id,
        }
