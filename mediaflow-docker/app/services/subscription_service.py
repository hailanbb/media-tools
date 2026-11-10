"""订阅资源核查与已确认源文件的可恢复清理。"""
import json
import posixpath
import time
from datetime import datetime, timezone
from sqlalchemy import select, update
from fastapi import HTTPException
from app.models.operation import OperationLog
from app.models.season import SeasonRecord
from app.config import get_settings
from app.clients.clouddrive2 import CloudDrive2Client
from app.clients.moviepilot import MoviePilotClient
from app.core.episode_parser import parse_episode, is_video_file
from app.core.subscription_cleaner import diagnose_subscription


def identity(sub):
    return str(sub.get("tmdbid") or sub.get("tmdb_id") or ""), int(sub.get("season") or 1)


def stamp(sub):
    keys = ("id", "name", "tmdbid", "tmdb_id", "season", "state", "completed_episode", "lack_episode",
            "total_episode", "best_version", "best_version_full", "custom_words")
    return {k: sub.get(k) for k in keys}


async def subscription(mp, sub_id):
    subs = await mp.list_subscriptions()
    return next((s for s in subs if int(s["id"]) == sub_id), None)


async def resource_check(mp, sub):
    tid, season = identity(sub)
    title = sub.get("name") or sub.get("title") or ""
    if not tid.isdigit():
        return {"status": "unknown", "message": "作品 ID 未确认，请先核对作品身份", "resources": []}
    try:
        rows = await mp.search_resources(tmdb_id=int(tid), season=season, title=title)
        if not rows:
            rows = await mp.search_resources(title=title, season=season)
        resources = []
        for row in rows:
            media, meta, torrent = (row.get(k) or {} for k in ("media_info", "meta_info", "torrent_info"))
            if str(media.get("tmdb_id") or "") != tid:
                continue
            seasons = meta.get("season_list") or [meta.get("begin_season")]
            if season not in seasons:
                continue
            resources.append({"title": torrent.get("title"), "seeders": torrent.get("seeders"),
                              "episodes": meta.get("episode_list") or [], "total": meta.get("total_episode")})
        return {"status": "found" if resources else "no_match",
                "message": "找到同季资源，需核对是否覆盖缺集" if resources else "本次搜索未命中；站点完整性未知",
                "resources": resources, "checked_at": datetime.now(timezone.utc).isoformat(),
                "site_coverage": "unknown"}
    except Exception as exc:
        return {"status": "unknown", "message": "资源查询未完成：" + type(exc).__name__, "resources": []}


async def check_queues(mp, sub):
    tid, _ = identity(sub)
    for queue in (await mp.get_active_downloads(), await mp.get_transfer_queue()):
        if not isinstance(queue, list):
            raise HTTPException(409, "下载或整理队列状态未知，请稍后重试")
        for item in queue:
            name = str(item.get("title") or item.get("name") or "")
            item_id = str(item.get("tmdbid") or item.get("tmdb_id") or (item.get("media_info") or {}).get("tmdb_id") or "")
            if not item_id:
                raise HTTPException(409, "队列中有作品 ID 未确认的任务，请先核对")
            if (tid and item_id == tid) or (sub.get("name") and sub["name"] in name):
                raise HTTPException(409, "本剧还有下载或整理任务，请先处理队列")


def fingerprint(item):
    return {k: item.get(k) for k in ("name", "path", "size", "modified", "id", "isDirectory")}


def source_path(path):
    settings = get_settings()
    normalized = posixpath.normpath(path)
    root = posixpath.normpath(settings.source_root) if settings.source_root else ""
    protected = [posixpath.normpath(p) for p in (settings.archive_root, settings.library_root) if p]
    if not root or not normalized.startswith(root.rstrip("/") + "/") or normalized != path:
        raise HTTPException(409, "源季路径不属于受控源根的严格子目录")
    if any(normalized == p or normalized.startswith(p + "/") or p.startswith(normalized + "/") for p in protected):
        raise HTTPException(409, "源季路径与归档或媒体库重叠")
    return normalized


async def preview(db, mp, disk, sub_id, cleanup, record_id=None):
    sub = await subscription(mp, sub_id)
    if not sub:
        raise HTTPException(404, "订阅不存在")
    if diagnose_subscription(sub).is_protected:
        raise HTTPException(409, "订阅处于保护状态，请先核对保护设置")
    evidence = await resource_check(mp, sub)
    await check_queues(mp, sub)
    data = {"sub_id": sub_id, "subscription": stamp(sub), "cleanup": cleanup,
            "evidence": evidence, "deleted": [], "unsubscribed": False, "files": [], "inventory": []}
    if cleanup:
        tid, season = identity(sub)
        if not tid.isdigit():
            raise HTTPException(409, "作品 ID 未确认")
        records = list(await db.scalars(select(SeasonRecord).where(
            SeasonRecord.tmdb_id == tid, SeasonRecord.season_number == season)))
        paths = {r.season_path for r in records if record_id is None or r.id == record_id}
        if len(paths) != 1:
            raise HTTPException(409, "未定位到唯一源季目录，请先初检或明确选择该季记录")
        path = source_path(paths.pop())
        # 裸剧目录可能包含多季，联合操作只处理已识别的独立季目录。
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
    return {"operation_id": op.id, **data, "bytes": sum(f.get("size") or 0 for f in data["files"]),
            "notice": "这些源视频可能是唯一副本；字幕、NFO、海报保留在源端"}


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
    claimed = await db.execute(update(OperationLog).where(OperationLog.id == op.id,
        OperationLog.detail_json == original).values(status="executing", detail_json=json.dumps(data, ensure_ascii=False)))
    await db.commit()
    if not claimed.rowcount:
        raise HTTPException(409, "操作状态已变化，请重试")
    others = await db.scalars(select(OperationLog).where(
        OperationLog.action == "subscription_cleanup", OperationLog.status == "executing", OperationLog.id != op.id))
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
            # 订阅可能在上一轮超时前已删除，先核对文件再恢复。
            data["unsubscribed"] = True
        await check_queues(mp, current or data["subscription"])
        if data["cleanup"]:
            path = source_path(data["path"])
            items = [fingerprint(f) for f in await disk.list_files(path, force_refresh=True)]
            expected = [f for f in data["inventory"] if f["path"] not in data["deleted"]]
            # 删除请求响应丢失时，仅允许已确认待删文件消失。
            actual = {f["path"]: f for f in items}
            removable = {f["path"] for f in data["files"]}
            if any(actual.get(f["path"]) != f and (f["path"] in actual or f["path"] not in removable) for f in expected) or any(f["path"] not in {e["path"] for e in expected} for f in items):
                raise HTTPException(409, "源目录内容已变化，请重新预览剩余文件")
        if current:
            await mp.delete_subscription(data["sub_id"])
            data["unsubscribed"] = True
            await save("executing")
        if data["cleanup"]:
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
        if data["cleanup"]:
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
        return {"ok": True, "message": f"已退订，已清理 {len(data['deleted'])} 个确认的源视频", "operation_id": op.id}
    except Exception as exc:
        await save("failed")
        message = exc.detail if isinstance(exc, HTTPException) else type(exc).__name__
        return {"ok": False, "message": f"{'已退订' if data['unsubscribed'] else '退订未确认'}；{message}，可使用同一操作重试", "operation_id": op.id}
