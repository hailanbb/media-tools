"""媒体整理修复服务层 — 安全基线、跨机存储核验、智能诊断、预览执行、落地核验与后台调度。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import posixpath
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.moviepilot import MoviePilotClient
from app.core.transfer_repair import (
    MIN_QUALIFIED_VIDEO_BYTES,
    build_idempotency_key,
    build_mp_manual_transfer_payload,
    calculate_plan_hash,
    diagnose_failure_record,
    extract_raw_error,
    find_companion_subtitles,
    normalize_file_path,
    parse_mp_preview_response,
    sanitize_fileitem,
)
from app.models.transfer_repair import (
    TransferRepairBaseline,
    TransferRepairItem,
    TransferRepairRule,
)

log = logging.getLogger("mediaflow.transfer_repair_service")

# 进程内路径互斥锁（与同文件并发提交、归档及目录合并防冲突，第 7.4 节 R27）
_ACTIVE_REPAIR_PATHS: set[str] = set()

# 重试退避阶梯（分钟）：10 分钟、30 分钟、2 小时（第 7.4 节）
_RETRY_BACKOFF_MINUTES = [10, 30, 120]

# 待处理状态集合
PENDING_STATUSES = (
    "discovered",
    "diagnosing",
    "ready",
    "needs_confirmation",
    "waiting_source",
    "waiting_environment",
    "conflict",
)


def is_repair_path_locked(path: str) -> bool:
    """检查指定路径是否正在被整理修复流程锁定（供归档/目录合并共享互斥，R27）。"""
    norm = normalize_file_path(path)
    return bool(norm and norm in _ACTIVE_REPAIR_PATHS)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _today_start_utc() -> datetime:
    """按本地自然日零点换算为 UTC 时间，确保‘今日修复/今日结案’统计口径准确。"""
    local_now = datetime.now().astimezone()
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(timezone.utc)


class _StorageProbeCache:
    """单轮扫描/核验内的远程目录级缓存，避免同目录 16 个文件重复调用 MP storage/list。"""

    def __init__(self, mp_client: MoviePilotClient | None) -> None:
        self.mp = mp_client
        self._dir_cache: dict[tuple[str, str], tuple[bool, list[dict[str, Any]], str]] = {}
        self._success_history_cache: dict[str, list[dict[str, Any]]] = {}

    async def list_dir(self, storage: str, dir_path: str) -> tuple[bool, list[dict[str, Any]], str]:
        norm_dir = normalize_file_path(dir_path)
        if not norm_dir:
            return False, [], "空目录路径"
        key = (storage or "local", norm_dir)
        if key in self._dir_cache:
            return self._dir_cache[key]

        # 优先使用 MoviePilot 原生 storage/list（支持跨机 NAS/Docker/115/SMB 等一切后端）
        if self.mp and hasattr(self.mp, "list_storage_dir"):
            res = await self.mp.list_storage_dir(storage or "local", norm_dir)
            self._dir_cache[key] = res
            return res

        # 仅在离线单元测试（MockClient 未提供 list_storage_dir）时回退到本地文件系统检查
        if os.path.isdir(norm_dir):
            entries: list[dict[str, Any]] = []
            try:
                for name in os.listdir(norm_dir):
                    full = posixpath.join(norm_dir, name)
                    st_path = os.path.join(norm_dir, name)
                    is_dir = os.path.isdir(st_path)
                    entries.append({
                        "name": name,
                        "path": normalize_file_path(full),
                        "type": "dir" if is_dir else "file",
                        "size": 0 if is_dir else os.path.getsize(st_path),
                        "modify_time": os.path.getmtime(st_path),
                        "storage": storage or "local",
                    })
                res = (True, entries, "")
            except Exception as exc:
                res = (False, [], str(exc))
        else:
            res = (True, [], "")
        self._dir_cache[key] = res
        return res

    async def probe_file(
        self,
        storage: str,
        file_path: str,
    ) -> dict[str, Any]:
        """核查单个远程文件的真实存在性、体积、mtime 与同目录清单（用于清点伴随字幕）。"""
        norm = normalize_file_path(file_path)
        if not norm:
            return {
                "healthy": True,
                "exists": False,
                "size": 0,
                "mtime": None,
                "entry": None,
                "siblings": [],
                "error": "",
            }
        parent = posixpath.dirname(norm)
        basename = posixpath.basename(norm)
        ok, entries, err = await self.list_dir(storage, parent)
        if not ok:
            return {
                "healthy": False,
                "exists": False,
                "size": 0,
                "mtime": None,
                "entry": None,
                "siblings": [],
                "error": err,
            }

        hit = None
        for entry in entries:
            e_path = normalize_file_path(entry.get("path") or "")
            e_name = str(entry.get("name") or "")
            if (e_path and e_path == norm) or (e_name and e_name == basename):
                hit = entry
                break

        # 若当前父目录返回空，再向上探查一级确认存储本身未掉线（R31）
        if hit is None and not entries:
            grandparent = posixpath.dirname(parent)
            if grandparent and grandparent != parent:
                gp_ok, _, gp_err = await self.list_dir(storage, grandparent)
                if not gp_ok:
                    return {
                        "healthy": False,
                        "exists": False,
                        "size": 0,
                        "mtime": None,
                        "entry": None,
                        "siblings": [],
                        "error": gp_err,
                    }

        mtime_dt = None
        if hit and hit.get("modify_time"):
            try:
                mtime_dt = datetime.fromtimestamp(float(hit["modify_time"]), tz=timezone.utc)
            except Exception:
                mtime_dt = None

        return {
            "healthy": True,
            "exists": hit is not None,
            "size": int(hit.get("size") or 0) if hit else 0,
            "mtime": mtime_dt,
            "entry": sanitize_fileitem(hit) if hit else None,
            "siblings": entries,
            "error": "",
        }

    async def find_subsequent_success(
        self,
        title: str,
        src_path: str,
        min_history_id: int,
        tmdb_id: int | None = None,
        seasons: str = "",
        episodes: str = "",
    ) -> dict[str, Any]:
        """有边界地查询同作品后续成功整理记录，并经存储核实目标真实落地（R14）。"""
        if not self.mp or not title or not hasattr(self.mp, "get_transfer_history_paginated"):
            return {}
        cache_key = title.strip()
        if cache_key not in self._success_history_cache:
            try:
                res = await self.mp.get_transfer_history_paginated(status=True, title=cache_key, page=1, count=50)
                self._success_history_cache[cache_key] = res.get("list", [])
            except Exception as exc:
                log.debug("查询后续成功历史异常 (%s): %s", cache_key, exc)
                self._success_history_cache[cache_key] = []

        norm_src = normalize_file_path(src_path)
        for rec in self._success_history_cache[cache_key]:
            rec_id = int(rec.get("id") or 0)
            if rec_id <= min_history_id:
                continue
            rec_src = normalize_file_path(rec.get("src") or (rec.get("src_fileitem") or {}).get("path") or "")
            same_src = bool(norm_src and rec_src == norm_src)
            same_ep = bool(
                tmdb_id
                and int(rec.get("tmdbid") or 0) == int(tmdb_id)
                and seasons
                and str(rec.get("seasons") or "") == seasons
                and episodes
                and str(rec.get("episodes") or "") == episodes
            )
            if not (same_src or same_ep):
                continue
            dest_p = normalize_file_path(rec.get("dest") or (rec.get("dest_fileitem") or {}).get("path") or "")
            dest_st = str(rec.get("dest_storage") or (rec.get("dest_fileitem") or {}).get("storage") or "local")
            if dest_p:
                dest_probe = await self.probe_file(dest_st, dest_p)
                if dest_probe["healthy"] and dest_probe["exists"] and dest_probe["size"] > MIN_QUALIFIED_VIDEO_BYTES:
                    return {
                        "verified": True,
                        "history_id": rec_id,
                        "dest_path": dest_p,
                        "dest_storage": dest_st,
                        "dest_size": dest_probe["size"],
                        "date": str(rec.get("date") or ""),
                    }
        return {}


async def _fetch_mp_baseline_anchor(mp_client: MoviePilotClient) -> tuple[bool, int, str, str, str]:
    """实时探测 MP 实例，获取当前最新失败历史高水位与版本信息。

    返回 (ok, max_history_id, baseline_date, mp_version, error_msg)。
    """
    try:
        page_data = await mp_client.get_transfer_history_paginated(status=False, page=1, count=1)
        records = page_data.get("list", [])
        max_id = 0
        b_date = ""
        if records and isinstance(records[0], dict):
            max_id = int(records[0].get("id") or 0)
            b_date = str(records[0].get("date") or "")
        mp_ver = ""
        if hasattr(mp_client, "get_system_version"):
            try:
                mp_ver = await mp_client.get_system_version()
            except Exception:
                mp_ver = ""
        return True, max_id, b_date, mp_ver, ""
    except Exception as exc:
        return False, 0, "", "", f"{type(exc).__name__}: {exc}"


async def get_or_create_baseline(
    db: AsyncSession,
    mp_client: MoviePilotClient | None = None,
) -> TransferRepairBaseline:
    """获取或初始化接管基线（R01, R29, R32）。

    安全不变量：
    - 严禁在未成功读取 MP 历史时将 baseline_history_id=0 静默固化为 active；
    - 无 mp_client 或 MP 探测失败时标记为 pending_confirm，阻止全库旧历史误导入。
    """
    res = await db.execute(select(TransferRepairBaseline).order_by(TransferRepairBaseline.id.asc()).limit(1))
    baseline = res.scalar_one_or_none()
    mp_origin = str(getattr(mp_client, "origin", "") or "").rstrip("/")

    if baseline:
        # 若已有基线处于 pending_confirm 且本次传入了有效 mp_client，尝试自动完成首次锚定
        if baseline.baseline_status == "pending_confirm" and mp_client is not None:
            ok, max_id, b_date, mp_ver, err = await _fetch_mp_baseline_anchor(mp_client)
            if ok:
                baseline.baseline_history_id = max_id
                baseline.last_scanned_history_id = max_id
                baseline.baseline_date = b_date
                baseline.mp_origin = mp_origin or baseline.mp_origin
                baseline.mp_version = mp_ver or baseline.mp_version
                baseline.baseline_status = "active"
                baseline.takeover_at = _utc_now()
                await db.commit()
                await db.refresh(baseline)
                log.info("媒体整理修复接管基线已完成锚定: ID=%d, date=%s", max_id, b_date)
        elif baseline.baseline_history_id == 0 and baseline.baseline_status == "active" and baseline.last_scanned_history_id > 0:
            # 检测到历史遗留的 0 基线缺陷，标记为待确认/需重设，避免继续拉取旧历史
            baseline.baseline_status = "reset_required"
            await db.commit()
            await db.refresh(baseline)
        elif mp_client and mp_origin and baseline.mp_origin and baseline.mp_origin != mp_origin:
            # R32: 检测到 MP 实例更换，必须重新确认接管基线
            baseline.baseline_status = "reset_required"
            await db.commit()
            await db.refresh(baseline)
        return baseline

    # 初次建立基线
    max_history_id = 0
    b_date = ""
    mp_ver = ""
    b_status = "pending_confirm"

    if mp_client is not None:
        ok, max_id, b_date, mp_ver, err = await _fetch_mp_baseline_anchor(mp_client)
        if ok:
            max_history_id = max_id
            b_status = "active"
        else:
            log.warning("初次建立接管基线读取 MP 历史失败，保持 pending_confirm 阻止导入旧历史: %s", err)

    baseline = TransferRepairBaseline(
        instance_id="default",
        mp_origin=mp_origin,
        mp_version=mp_ver,
        baseline_status=b_status,
        takeover_at=_utc_now(),
        baseline_history_id=max_history_id,
        baseline_date=b_date,
        last_scanned_history_id=max_history_id,
        is_enabled=True,
        auto_repair_enabled=False,
    )
    db.add(baseline)
    await db.commit()
    await db.refresh(baseline)
    log.info("已初始化媒体整理修复基线: status=%s, baseline_id=%d", b_status, max_history_id)
    return baseline


async def reset_baseline(
    db: AsyncSession,
    mp_client: MoviePilotClient,
    clear_existing_items: bool = True,
    custom_baseline_id: int | None = None,
) -> dict[str, Any]:
    """重新锚定接管基线至 MP 当前最新失败 ID（或指定 ID），并可清理此前误导入的脏记录（方案 A）。"""
    ok, latest_id, b_date, mp_ver, err = await _fetch_mp_baseline_anchor(mp_client)
    if not ok:
        return {"ok": False, "message": f"无法连接 MoviePilot 锚定基线: {err}"}

    target_baseline_id = int(custom_baseline_id) if custom_baseline_id is not None else latest_id
    mp_origin = str(getattr(mp_client, "origin", "") or "").rstrip("/")

    res = await db.execute(select(TransferRepairBaseline).order_by(TransferRepairBaseline.id.asc()).limit(1))
    baseline = res.scalar_one_or_none()

    cleared_items = 0
    if clear_existing_items:
        cnt_res = await db.execute(select(func.count(TransferRepairItem.id)))
        cleared_items = int(cnt_res.scalar() or 0)
        await db.execute(delete(TransferRepairItem))

    now = _utc_now()
    if baseline is None:
        baseline = TransferRepairBaseline(
            instance_id="default",
            mp_origin=mp_origin,
            mp_version=mp_ver,
            baseline_status="active",
            takeover_at=now,
            baseline_history_id=target_baseline_id,
            baseline_date=b_date,
            last_scanned_history_id=target_baseline_id,
            last_scanned_at=now,
            last_scan_truncated=False,
            last_scan_summary_json="{}",
            is_enabled=True,
            auto_repair_enabled=False,
        )
        db.add(baseline)
    else:
        baseline.mp_origin = mp_origin or baseline.mp_origin
        baseline.mp_version = mp_ver or baseline.mp_version
        baseline.baseline_status = "active"
        baseline.takeover_at = now
        baseline.baseline_history_id = target_baseline_id
        baseline.baseline_date = b_date
        baseline.last_scanned_history_id = target_baseline_id
        baseline.last_scanned_at = now
        baseline.last_scan_truncated = False
        baseline.last_scan_summary_json = "{}"
        baseline.updated_at = now

    await db.commit()
    return {
        "ok": True,
        "baseline_history_id": target_baseline_id,
        "baseline_date": b_date,
        "mp_version": mp_ver,
        "cleared_items": cleared_items,
        "message": f"已重设接管基线至 #{target_baseline_id}（清理旧记录 {cleared_items} 条），后续仅接管此起点之后的新失败。",
    }


async def _load_active_rules(db: AsyncSession) -> list[dict[str, Any]]:
    rules_res = await db.execute(
        select(TransferRepairRule).where(TransferRepairRule.is_active == True)  # noqa: E712
    )
    return [
        {
            "id": r.id,
            "scope_kind": r.scope_kind,
            "scope_key": r.scope_key,
            "show_title": r.show_title,
            "media_type": r.media_type,
            "tmdb_id": r.tmdb_id,
            "season_number": r.season_number,
            "episode_offset": r.episode_offset,
        }
        for r in rules_res.scalars().all()
    ]


async def _diagnose_record_with_probe(
    rec: dict[str, Any],
    probe_cache: _StorageProbeCache,
    active_rules: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """结合 MP 远程存储探测、目标核验、伴随字幕与后续成功历史，输出完整诊断结果。"""
    src_fi = sanitize_fileitem(rec.get("src_fileitem"))
    dest_fi = sanitize_fileitem(rec.get("dest_fileitem"))
    src_path = normalize_file_path(
        rec.get("src") or src_fi.get("path") or rec.get("source_path") or rec.get("file_path") or rec.get("path") or ""
    )
    dest_path = normalize_file_path(rec.get("dest") or dest_fi.get("path") or "")
    src_storage = str(rec.get("src_storage") or src_fi.get("storage") or "local")
    dest_storage = str(rec.get("dest_storage") or dest_fi.get("storage") or src_storage)

    # 1. 核查源文件（严格走 MP storage/list，不再在 Windows 上用 os.path.exists）
    src_probe = await probe_cache.probe_file(src_storage, src_path)

    # 2. 若存在目标引用（dest / dest_fileitem），核查目标文件真实存在性与大小
    dest_probe = (
        await probe_cache.probe_file(dest_storage, dest_path)
        if dest_path
        else {"healthy": True, "exists": False, "size": 0, "mtime": None, "entry": None, "siblings": [], "error": ""}
    )

    storage_healthy = bool(src_probe["healthy"] and dest_probe["healthy"])
    storage_error = src_probe["error"] or dest_probe["error"]

    # 3. 源不存在或目标待查时，查一次有边界的后续成功历史（R14）
    subsequent_success: dict[str, Any] = {}
    if storage_healthy and (not src_probe["exists"] or not dest_probe["exists"]):
        rec_id = int(rec.get("id") or 0)
        mp_title = str(rec.get("title") or "").strip()
        mp_tmdbid = int(rec.get("tmdbid") or 0) or None
        subsequent_success = await probe_cache.find_subsequent_success(
            title=mp_title,
            src_path=src_path,
            min_history_id=rec_id,
            tmdb_id=mp_tmdbid,
            seasons=str(rec.get("seasons") or ""),
            episodes=str(rec.get("episodes") or ""),
        )

    # 4. 清点同目录伴随外挂字幕（零额外请求，直接复用 src_probe.siblings）
    subs = find_companion_subtitles(posixpath.basename(src_path), src_probe.get("siblings"))

    diag = diagnose_failure_record(
        rec,
        source_exists=bool(src_probe["exists"]),
        target_exists=bool(dest_probe["exists"]),
        target_file_size=int(dest_probe["size"] or 0),
        source_file_size=int(src_probe["size"] or 0),
        storage_healthy=storage_healthy,
        storage_error=storage_error,
        active_rules=active_rules,
        dest_dir_sample=posixpath.dirname(dest_path) if dest_path else None,
        subsequent_success=subsequent_success,
        companion_subtitles=subs,
    )
    return diag, src_probe


def _apply_diag_to_item(
    item: TransferRepairItem,
    diag: dict[str, Any],
    src_probe: dict[str, Any],
) -> None:
    """将最新诊断结论写入持久化修复项（保护项与进行中项不强行覆盖）。"""
    item.file_size = int(src_probe.get("size") or item.file_size or 0)
    if src_probe.get("mtime"):
        item.file_mtime = src_probe["mtime"]
    item.storage = diag["src_storage"]
    item.src_storage = diag["src_storage"]
    item.dest_storage = diag["dest_storage"]
    item.raw_error = diag["raw_error"] or item.raw_error
    item.error_category = diag["error_category"]
    item.main_block_reason = diag["main_block_reason"]
    item.failure_group_key = diag["failure_group_key"]
    item.mp_title = diag["mp_title"] or item.mp_title
    item.mp_seasons = diag["mp_seasons"] or item.mp_seasons
    item.mp_episodes = diag["mp_episodes"] or item.mp_episodes
    item.mp_mode = diag["mp_mode"] or item.mp_mode
    item.mp_date = diag["mp_date"] or item.mp_date
    if diag["src_fileitem"]:
        item.src_fileitem_json = json.dumps(diag["src_fileitem"], ensure_ascii=False)
    elif src_probe.get("entry"):
        item.src_fileitem_json = json.dumps(src_probe["entry"], ensure_ascii=False)
    if diag["dest_fileitem"]:
        item.dest_fileitem_json = json.dumps(diag["dest_fileitem"], ensure_ascii=False)
    item.title = diag["title"]
    item.media_type = diag["media_type"]
    item.tmdb_id = diag["tmdb_id"]
    item.season_number = diag["season_number"]
    item.episode_numbers_json = json.dumps(diag["episode_numbers"], ensure_ascii=False)
    item.part = diag["part"]
    item.is_special = diag["is_special"]
    item.special_type = diag["special_type"]
    item.target_storage = diag["dest_storage"] or diag["src_storage"]
    item.target_path = diag["target_path"] or item.target_path
    item.evidence_json = json.dumps(diag["evidence_json"], ensure_ascii=False)
    item.target_verify_json = json.dumps(diag["target_verify_json"], ensure_ascii=False)
    item.plan_json = json.dumps(diag["plan_json"], ensure_ascii=False)
    item.plan_hash = diag["plan_hash"]
    item.status = diag["status"]
    item.resolution_code = diag["resolution_code"]
    item.skip_reason = diag["skip_reason"]
    if diag["status"] == "resolved" and item.resolved_at is None:
        item.resolved_at = _utc_now()
    item.updated_at = _utc_now()


async def scan_new_failures(
    db: AsyncSession,
    mp_client: MoviePilotClient,
    max_pages: int = 5,
    page_size: int = 50,
) -> dict[str, Any]:
    """增量扫描 MoviePilot 接管基线之后的新失败并执行真实存储核查与诊断（R01, R02, R29, R30, R32）。"""
    baseline = await get_or_create_baseline(db, mp_client)
    if not baseline.is_enabled:
        return {"status": "disabled", "message": "自动发现扫描已暂停"}

    if baseline.baseline_status != "active":
        return {
            "status": "baseline_required",
            "baseline_status": baseline.baseline_status,
            "baseline_history_id": baseline.baseline_history_id,
            "message": "接管范围待确认或基线需重设（防止误导入旧历史），请先点击「重设基线」确认起点。",
            "new_failures_found": 0,
            "new_items_created": 0,
            "pending_new": 0,
            "resolved_verified": 0,
            "skipped_source_missing": 0,
            "items_updated": 0,
        }

    # 更新 MP 版本信息
    if hasattr(mp_client, "get_system_version"):
        try:
            ver = await mp_client.get_system_version()
            if ver:
                baseline.mp_version = ver
        except Exception:
            pass

    active_rules = await _load_active_rules(db)
    probe_cache = _StorageProbeCache(mp_client)

    new_failures: list[dict[str, Any]] = []
    seen_rec_ids: set[int] = set()
    scanned_total = 0
    scanned_pages = 0
    truncated = False
    highest_seen_id = baseline.last_scanned_history_id or baseline.baseline_history_id
    first_page_max_id: int | None = None

    for page in range(1, max_pages + 1):
        try:
            data = await mp_client.get_transfer_history_paginated(status=False, page=page, count=page_size)
        except Exception as exc:
            log.error("扫描 MP 失败历史异常 (page %d): %s", page, exc)
            return {"status": "error", "message": f"读取 MoviePilot 失败历史失败: {exc}"}

        records = data.get("list", [])
        if not records:
            break

        scanned_pages += 1
        scanned_total += len(records)
        reached_baseline = False

        if page == 1:
            page1_ids = [int(r.get("id") or 0) for r in records if isinstance(r, dict)]
            first_page_max_id = max(page1_ids) if page1_ids else 0
            # R32: 若 MP 当前最大失败 ID 远小于已记录基线 ID，判定 MP 重建库或换实例，暂停接管
            if first_page_max_id > 0 and baseline.baseline_history_id > 0 and first_page_max_id < baseline.baseline_history_id:
                baseline.baseline_status = "reset_required"
                await db.commit()
                return {
                    "status": "baseline_reset_required",
                    "baseline_status": "reset_required",
                    "message": (
                        f"检测到 MoviePilot 当前最新历史 ID (#{first_page_max_id}) 小于已存基线 "
                        f"(#{baseline.baseline_history_id})，疑似更换实例或重建库；已暂停导入，请重新确认基线。"
                    ),
                    "new_failures_found": 0,
                    "new_items_created": 0,
                }

        for rec in records:
            if not isinstance(rec, dict):
                continue
            rec_id = int(rec.get("id") or 0)
            if rec_id <= 0 or rec_id in seen_rec_ids:
                continue
            seen_rec_ids.add(rec_id)
            if rec_id > highest_seen_id:
                highest_seen_id = rec_id

            # R29/R30: 严格隔离基线及之前的旧历史
            if rec_id <= baseline.baseline_history_id:
                reached_baseline = True
                continue

            new_failures.append(rec)

        if reached_baseline or len(records) < page_size:
            break
        if page == max_pages:
            truncated = True

    # 按源文件归并并执行真实存储核查与诊断（按时间顺序从旧到新处理，确保最新错误覆盖旧错误）
    new_failures.sort(key=lambda r: int(r.get("id") or 0))

    created_count = 0
    updated_count = 0
    pending_new = 0
    resolved_verified = 0
    skipped_missing = 0

    for rec in new_failures:
        rec_id = int(rec.get("id") or 0)
        src_path = normalize_file_path(
            rec.get("src")
            or (rec.get("src_fileitem") or {}).get("path")
            or rec.get("source_path")
            or rec.get("file_path")
            or rec.get("path")
            or ""
        )
        if not src_path:
            continue

        item_res = await db.execute(
            select(TransferRepairItem).where(TransferRepairItem.source_path == src_path)
        )
        item = item_res.scalar_one_or_none()

        # 已保护或正在提交/核验中的条目只追加历史 ID，不覆盖状态
        if item and (item.is_protected or item.status in ("submitted", "verifying")):
            existing_ids = json.loads(item.mp_history_ids_json or "[]")
            if rec_id not in existing_ids:
                existing_ids.append(rec_id)
                item.mp_history_ids_json = json.dumps(existing_ids, ensure_ascii=False)
                item.latest_history_id = max(item.latest_history_id, rec_id)
                item.raw_error = extract_raw_error(rec) or item.raw_error
                item.updated_at = _utc_now()
                updated_count += 1
            continue

        # 已结案且未产生更新的失败事件，无需重复诊断
        if item and item.status == "resolved" and rec_id <= item.latest_history_id:
            continue

        diag, src_probe = await _diagnose_record_with_probe(rec, probe_cache, active_rules)

        if item:
            existing_ids = json.loads(item.mp_history_ids_json or "[]")
            if rec_id not in existing_ids:
                existing_ids.append(rec_id)
            item.mp_history_ids_json = json.dumps(existing_ids, ensure_ascii=False)
            item.latest_history_id = max(item.latest_history_id, rec_id)
            _apply_diag_to_item(item, diag, src_probe)
            updated_count += 1
        else:
            item = TransferRepairItem(
                source_path=src_path,
                file_basename=posixpath.basename(src_path),
                mp_history_ids_json=json.dumps([rec_id], ensure_ascii=False),
                latest_history_id=rec_id,
            )
            _apply_diag_to_item(item, diag, src_probe)
            db.add(item)
            created_count += 1
            if diag["status"] in PENDING_STATUSES:
                pending_new += 1
            elif diag["status"] == "resolved":
                resolved_verified += 1
            elif diag["status"] == "skipped":
                skipped_missing += 1

    scan_summary = {
        "scanned_at": _utc_now().isoformat(),
        "scanned_pages": scanned_pages,
        "scanned_records": scanned_total,
        "truncated": truncated,
        "new_failures_found": len(new_failures),
        "new_items_created": created_count,
        "pending_new": pending_new,
        "resolved_verified": resolved_verified,
        "skipped_source_missing": skipped_missing,
        "items_updated": updated_count,
    }
    baseline.last_scanned_history_id = highest_seen_id
    baseline.last_scanned_at = _utc_now()
    baseline.last_scan_truncated = truncated
    baseline.last_scan_summary_json = json.dumps(scan_summary, ensure_ascii=False)
    await db.commit()

    return {
        "status": "success",
        "baseline_status": baseline.baseline_status,
        "baseline_history_id": baseline.baseline_history_id,
        **scan_summary,
    }


async def recheck_single_item(
    db: AsyncSession,
    mp_client: MoviePilotClient,
    item_id: str,
) -> dict[str, Any]:
    """对单条修复项执行只读重诊断（不触发整理，第 10.3 节）。"""
    item = await db.get(TransferRepairItem, item_id)
    if not item:
        return {"ok": False, "message": "修复项不存在"}
    if item.status in ("submitted", "verifying"):
        return {"ok": False, "message": "当前条目正在执行或核验中，请等待核验结果"}

    active_rules = await _load_active_rules(db)
    probe_cache = _StorageProbeCache(mp_client)

    rec = {
        "id": item.latest_history_id,
        "src": item.source_path,
        "dest": item.target_path,
        "src_storage": item.src_storage or item.storage or "local",
        "dest_storage": item.dest_storage or item.target_storage or "",
        "src_fileitem": json.loads(item.src_fileitem_json or "{}") or None,
        "dest_fileitem": json.loads(item.dest_fileitem_json or "{}") or None,
        "title": item.mp_title or item.title,
        "type": "电影" if item.media_type == "movie" else "电视剧",
        "tmdbid": item.tmdb_id,
        "seasons": item.mp_seasons,
        "episodes": item.mp_episodes,
        "mode": item.mp_mode,
        "date": item.mp_date,
        "errmsg": item.raw_error,
    }
    diag, src_probe = await _diagnose_record_with_probe(rec, probe_cache, active_rules)
    if not item.is_protected:
        _apply_diag_to_item(item, diag, src_probe)
    else:
        item.evidence_json = json.dumps(diag["evidence_json"], ensure_ascii=False)
        item.target_verify_json = json.dumps(diag["target_verify_json"], ensure_ascii=False)
        item.updated_at = _utc_now()
    await db.commit()
    return {
        "ok": True,
        "id": item.id,
        "status": item.status,
        "main_block_reason": item.main_block_reason,
        "plan_hash": item.plan_hash,
        "message": f"重诊断完成，当前状态：{item.status}",
    }


async def preview_repair_item(
    db: AsyncSession,
    mp_client: MoviePilotClient,
    item_id: str,
) -> dict[str, Any]:
    """实时刷新源 FileItem、匹配目标目录并调用 MoviePilot preview=true 验证计划一致性（R17, R18）。"""
    item = await db.get(TransferRepairItem, item_id)
    if not item:
        return {"ok": False, "message": "修复项不存在"}

    probe_cache = _StorageProbeCache(mp_client)
    src_storage = item.src_storage or item.storage or "local"
    src_probe = await probe_cache.probe_file(src_storage, item.source_path)
    if not src_probe["healthy"]:
        item.status = "waiting_environment"
        item.main_block_reason = f"预览前存储探测失败: {src_probe['error']}"
        item.updated_at = _utc_now()
        await db.commit()
        return {"ok": False, "message": item.main_block_reason}
    if not src_probe["exists"]:
        item.status = "skipped"
        item.skip_reason = "source_missing"
        item.main_block_reason = "预览前核实源文件已不存在，跳过修复"
        item.updated_at = _utc_now()
        await db.commit()
        return {"ok": False, "message": item.main_block_reason}

    fresh_fi = src_probe["entry"] or json.loads(item.src_fileitem_json or "{}")
    if fresh_fi:
        item.src_fileitem_json = json.dumps(fresh_fi, ensure_ascii=False)
    item.file_size = int(src_probe["size"] or item.file_size or 0)

    # 调用 MP target-path 匹配推荐目标配置
    target_storage = item.target_storage or item.dest_storage or src_storage
    target_dir = ""
    transfer_type = item.mp_mode or None
    if hasattr(mp_client, "get_manual_target_path"):
        tp_res = await mp_client.get_manual_target_path(fresh_fi or item.source_path, storage=src_storage)
        tp_data = tp_res.get("data") if isinstance(tp_res, dict) else None
        if isinstance(tp_data, dict):
            target_storage = tp_data.get("target_storage") or target_storage
            target_dir = tp_data.get("target_path") or ""
            transfer_type = tp_data.get("transfer_type") or transfer_type

    plan = json.loads(item.plan_json or "{}")
    episodes = json.loads(item.episode_numbers_json or "[]")
    dest_risk = bool(plan.get("dest_cleanup_risk") or json.loads(item.dest_fileitem_json or "{}"))

    payload = build_mp_manual_transfer_payload(
        logid=item.latest_history_id,
        fileitem=fresh_fi,
        source_path=item.source_path,
        src_storage=src_storage,
        media_type=item.media_type,
        tmdb_id=item.tmdb_id,
        season_number=item.season_number,
        episode_numbers=episodes,
        part=item.part,
        episode_offset=int(plan.get("episode_offset") or 0),
        target_storage=target_storage,
        target_path=target_dir or None,
        transfer_type=transfer_type,
        preview=True,
        allow_logid=not dest_risk,
    )

    try:
        raw_preview = await mp_client.manual_transfer_v2(payload)
    except Exception as exc:
        return {"ok": False, "message": f"调用 MoviePilot 预览失败: {exc}"}

    parsed = parse_mp_preview_response(raw_preview, expected_source_path=item.source_path)
    preview_record = {
        "checked_at": _utc_now().isoformat(),
        "ok": parsed["ok"],
        "reason": parsed["reason"],
        "target_path": parsed["target_path"] or target_dir,
        "target_storage": target_storage,
        "transfer_type": transfer_type or "",
        "items": parsed["items"][:5],
        "source_size_snapshot": item.file_size,
        "plan_hash": item.plan_hash,
    }
    item.preview_json = json.dumps(preview_record, ensure_ascii=False)
    if parsed["target_path"]:
        item.target_path = parsed["target_path"]
        item.target_storage = target_storage
        plan["target_path"] = parsed["target_path"]
        plan["target_storage"] = target_storage
        plan["transfer_type"] = transfer_type or ""
        item.plan_json = json.dumps(plan, ensure_ascii=False)
        item.plan_hash = calculate_plan_hash(plan)
    item.updated_at = _utc_now()
    await db.commit()

    return {
        "ok": parsed["ok"],
        "preview": preview_record,
        "plan_hash": item.plan_hash,
        "message": parsed["reason"],
    }


async def verify_repair_item(
    db: AsyncSession,
    mp_client: MoviePilotClient,
    item_id: str,
) -> dict[str, Any]:
    """对已提交整理的项目执行真实落地核验（第 7.3 节：绝不以接口返回成功冒充落地成功）。"""
    item = await db.get(TransferRepairItem, item_id)
    if not item:
        return {"ok": False, "message": "修复项不存在"}

    probe_cache = _StorageProbeCache(mp_client)
    evidence = json.loads(item.evidence_json or "{}")
    preview = json.loads(item.preview_json or "{}")
    plan = json.loads(item.plan_json or "{}")
    ver = json.loads(item.verification_json or "{}")

    stages = {
        "submitted": True,
        "mp_history_matched": False,
        "target_landed": False,
        "subtitles_verified": True,
        "mediaserver_confirmed": False,
        "checked_at": _utc_now().isoformat(),
    }

    # 1. 优先查询 MP 后续成功历史定位真实落地目标路径
    dest_path = normalize_file_path(item.target_path or preview.get("target_path") or "")
    dest_storage = item.target_storage or item.dest_storage or item.src_storage or "local"

    sub_ok = await probe_cache.find_subsequent_success(
        title=item.title or item.mp_title,
        src_path=item.source_path,
        min_history_id=max(0, int(item.latest_history_id or 0)),
        tmdb_id=item.tmdb_id,
        seasons=f"S{item.season_number:02d}" if item.season_number is not None else "",
        episodes=plan.get("episode_detail") or "",
    )
    if sub_ok.get("verified"):
        stages["mp_history_matched"] = True
        stages["target_landed"] = True
        dest_path = sub_ok["dest_path"]
        dest_storage = sub_ok["dest_storage"]
        ver["landed_size"] = sub_ok["dest_size"]
        ver["matched_history_id"] = sub_ok["history_id"]
    elif dest_path:
        # 2. 直接通过 MP storage/list 核验预期目标文件是否已真实落地
        dest_probe = await probe_cache.probe_file(dest_storage, dest_path)
        if not dest_probe["healthy"]:
            ver["stages"] = stages
            ver["note"] = f"核验存储暂时不可达: {dest_probe['error']}"
            item.verification_json = json.dumps(ver, ensure_ascii=False)
            item.status = "verifying"
            item.updated_at = _utc_now()
            await db.commit()
            return {"ok": False, "status": "verifying", "message": ver["note"]}
        if dest_probe["exists"] and dest_probe["size"] > MIN_QUALIFIED_VIDEO_BYTES:
            stages["target_landed"] = True
            ver["landed_size"] = dest_probe["size"]
            # 检查伴随字幕是否同步到位（R20）
            expected_subs = evidence.get("companion_subtitles") or []
            if expected_subs:
                dest_subs = find_companion_subtitles(posixpath.basename(dest_path), dest_probe.get("siblings"))
                if not dest_subs:
                    stages["subtitles_verified"] = False
                    ver["subtitle_warning"] = f"主视频已落地，但 {len(expected_subs)} 个伴随字幕尚未同步到位（独立待办，不重跑视频）"

    ver["stages"] = stages
    ver["dest_path"] = dest_path
    item.verification_json = json.dumps(ver, ensure_ascii=False)

    if stages["target_landed"]:
        item.target_path = dest_path or item.target_path
        item.status = "resolved"
        item.resolution_code = "repaired"
        item.resolved_at = _utc_now()
        item.main_block_reason = (
            ver.get("subtitle_warning")
            or f"已完成整理并经存储实查核验目标视频落地 ({ver.get('landed_size', 0)} B)"
        )
        item.updated_at = _utc_now()
        await db.commit()
        return {"ok": True, "status": "resolved", "message": item.main_block_reason, "verification": ver}

    # 目标尚未核实落地 -> 留在 verifying，超过 15 分钟标注“结果待确认”防止盲目重提（R19, R21）
    elapsed = (_utc_now() - item.submitted_at).total_seconds() if item.submitted_at else 0
    item.status = "verifying"
    if elapsed > 900:
        item.main_block_reason = "已提交超过 15 分钟仍未核实目标落地（结果待确认），请人工核查或点立即核验，暂不重复提交"
    else:
        item.main_block_reason = "请求已提交 MoviePilot，正在等待处理并核验目标文件真实落地"
    item.updated_at = _utc_now()
    await db.commit()
    return {"ok": False, "status": "verifying", "message": item.main_block_reason, "verification": ver}


async def execute_repair_plans(
    db: AsyncSession,
    mp_client: MoviePilotClient,
    item_ids: list[str],
    skip_preview: bool = False,
) -> dict[str, Any]:
    """批量或单项执行已就绪的整理计划：加锁 → 防漂移 → 预览核对 → 落盘幂等键 → 提交 → 核验（第 7 节）。"""
    baseline = await get_or_create_baseline(db, mp_client)
    res = await db.execute(select(TransferRepairItem).where(TransferRepairItem.id.in_(item_ids)))
    items = res.scalars().all()

    results: list[dict[str, Any]] = []
    probe_cache = _StorageProbeCache(mp_client)

    for item in items:
        if item.is_protected:
            results.append({"id": item.id, "ok": False, "reason": "条目已被保护，跳过执行"})
            continue

        if item.status not in ("ready", "needs_confirmation"):
            results.append({"id": item.id, "ok": False, "reason": f"当前状态 [{item.status}] 不支持直接执行"})
            continue

        plan = json.loads(item.plan_json or "{}")
        if plan.get("dest_cleanup_risk") or json.loads(item.dest_fileitem_json or "{}"):
            item.status = "conflict"
            item.main_block_reason = "检测到旧目标引用 (dest_fileitem)，阻断自动执行以防隐式删除旧目标"
            results.append({"id": item.id, "ok": False, "reason": item.main_block_reason})
            continue

        norm_src = normalize_file_path(item.source_path)
        if norm_src in _ACTIVE_REPAIR_PATHS:
            results.append({"id": item.id, "ok": False, "reason": "同一源文件正在被其他任务操作（并发互斥保护）"})
            continue

        _ACTIVE_REPAIR_PATHS.add(norm_src)
        try:
            src_storage = item.src_storage or item.storage or "local"

            # 1. 提交前重查源文件防漂移（R18）
            src_probe = await probe_cache.probe_file(src_storage, norm_src)
            if not src_probe["healthy"]:
                item.status = "waiting_environment"
                item.main_block_reason = f"提交前存储探测异常: {src_probe['error']}"
                results.append({"id": item.id, "ok": False, "reason": item.main_block_reason})
                continue
            if not src_probe["exists"]:
                item.status = "skipped"
                item.skip_reason = "source_missing"
                item.main_block_reason = "提交前复核发现源文件已不存在，计划失效并跳过"
                results.append({"id": item.id, "ok": False, "reason": item.main_block_reason})
                continue

            if item.file_size > 0 and src_probe["size"] > 0 and src_probe["size"] != item.file_size:
                item.file_size = src_probe["size"]
                item.status = "diagnosing"
                item.main_block_reason = "提交前检测到源文件体积变化（仍在写入或被替换），旧计划已失效，请重新诊断"
                results.append({"id": item.id, "ok": False, "reason": item.main_block_reason})
                continue

            fresh_fi = src_probe["entry"] or json.loads(item.src_fileitem_json or "{}")
            episodes = json.loads(item.episode_numbers_json or "[]")

            # 2. 获取目标目录配置
            target_storage = item.target_storage or item.dest_storage or src_storage
            target_dir = plan.get("target_dir") or ""
            transfer_type = plan.get("transfer_type") or item.mp_mode or None
            if hasattr(mp_client, "get_manual_target_path"):
                tp_res = await mp_client.get_manual_target_path(fresh_fi or norm_src, storage=src_storage)
                tp_data = tp_res.get("data") if isinstance(tp_res, dict) else None
                if isinstance(tp_data, dict):
                    target_storage = tp_data.get("target_storage") or target_storage
                    target_dir = tp_data.get("target_path") or target_dir
                    transfer_type = tp_data.get("transfer_type") or transfer_type

            # 3. 强制预览一致性校验（R17）
            if not skip_preview:
                preview_payload = build_mp_manual_transfer_payload(
                    logid=item.latest_history_id,
                    fileitem=fresh_fi,
                    source_path=norm_src,
                    src_storage=src_storage,
                    media_type=item.media_type,
                    tmdb_id=item.tmdb_id,
                    season_number=item.season_number,
                    episode_numbers=episodes,
                    part=item.part,
                    episode_offset=int(plan.get("episode_offset") or 0),
                    target_storage=target_storage,
                    target_path=target_dir or None,
                    transfer_type=transfer_type,
                    preview=True,
                    allow_logid=True,
                )
                pv_raw = await mp_client.manual_transfer_v2(preview_payload)
                pv_parsed = parse_mp_preview_response(pv_raw, expected_source_path=norm_src)
                item.preview_json = json.dumps(
                    {
                        "checked_at": _utc_now().isoformat(),
                        "ok": pv_parsed["ok"],
                        "reason": pv_parsed["reason"],
                        "target_path": pv_parsed["target_path"] or target_dir,
                        "items": pv_parsed["items"][:5],
                    },
                    ensure_ascii=False,
                )
                if not pv_parsed["ok"]:
                    item.main_block_reason = f"执行前预览未通过: {pv_parsed['reason']}"
                    results.append({"id": item.id, "ok": False, "reason": item.main_block_reason})
                    continue
                if pv_parsed["target_path"]:
                    item.target_path = pv_parsed["target_path"]

            # 4. 落盘幂等键后再发出写请求（第 10.2 节）
            idem_key = build_idempotency_key(
                source_path=norm_src,
                file_size=int(src_probe["size"] or item.file_size or 0),
                plan_hash=item.plan_hash or calculate_plan_hash(plan),
                target_path=item.target_path or target_dir,
            )
            exec_payload = build_mp_manual_transfer_payload(
                logid=item.latest_history_id,
                fileitem=fresh_fi,
                source_path=norm_src,
                src_storage=src_storage,
                media_type=item.media_type,
                tmdb_id=item.tmdb_id,
                season_number=item.season_number,
                episode_numbers=episodes,
                part=item.part,
                episode_offset=int(plan.get("episode_offset") or 0),
                target_storage=target_storage,
                target_path=target_dir or None,
                transfer_type=transfer_type,
                preview=False,
                allow_logid=True,
            )
            item.idempotency_key = idem_key
            item.status = "submitted"
            item.submitted_at = _utc_now()
            item.main_block_reason = "正在向 MoviePilot 提交手动整理请求..."
            item.last_execution_json = json.dumps(
                {
                    "idempotency_key": idem_key,
                    "submitted_at": item.submitted_at.isoformat(),
                    "payload": {k: v for k, v in exec_payload.items() if k != "fileitem"},
                },
                ensure_ascii=False,
            )
            await db.commit()

            # 5. 正式调用 MoviePilot 执行整理
            mp_res = await mp_client.manual_transfer_v2(exec_payload)
            if not isinstance(mp_res, dict) or mp_res.get("success") is not True:
                msg = (
                    str(mp_res.get("message") or "MoviePilot 返回未明确成功状态")
                    if isinstance(mp_res, dict)
                    else f"MoviePilot 返回未知结构 ({type(mp_res).__name__})"
                )
                item.retry_count += 1
                if item.retry_count >= baseline.retry_budget:
                    item.status = "exhausted"
                    item.main_block_reason = f"重试已达上限 ({baseline.retry_budget} 次): {msg}"
                else:
                    delay_min = _RETRY_BACKOFF_MINUTES[min(item.retry_count - 1, len(_RETRY_BACKOFF_MINUTES) - 1)]
                    item.status = "waiting_environment"
                    item.next_retry_at = _utc_now() + timedelta(minutes=delay_min)
                    item.main_block_reason = f"提交失败（第 {item.retry_count}/{baseline.retry_budget} 次，{delay_min} 分钟后退避重试）: {msg}"
                item.last_execution_json = json.dumps(
                    {
                        "idempotency_key": idem_key,
                        "submitted_at": item.submitted_at.isoformat() if item.submitted_at else None,
                        "response": mp_res if isinstance(mp_res, dict) else str(mp_res),
                    },
                    ensure_ascii=False,
                )
                results.append({"id": item.id, "ok": False, "reason": msg})
                continue

            # 6. 接口接受后进入核验阶段（绝不直接伪造成功！）
            item.last_execution_json = json.dumps(
                {
                    "idempotency_key": idem_key,
                    "submitted_at": item.submitted_at.isoformat() if item.submitted_at else None,
                    "response": mp_res,
                },
                ensure_ascii=False,
            )
            item.status = "verifying"
            await db.commit()

            verify_res = await verify_repair_item(db, mp_client, item.id)
            results.append({
                "id": item.id,
                "ok": True,
                "status": verify_res.get("status", "verifying"),
                "verified": bool(verify_res.get("ok")),
                "message": verify_res.get("message") or "已提交 MoviePilot，进入目标落地核验",
            })

        except Exception as exc:
            # R19: 请求超时或中断后严禁立即认定失败重提，保留在 verifying 等待核验
            if item.status == "submitted":
                item.status = "verifying"
                item.main_block_reason = f"提交阶段发生网络异常 ({exc})，已转入核验避免重复提交"
                results.append({"id": item.id, "ok": False, "status": "verifying", "reason": item.main_block_reason})
            else:
                item.retry_count += 1
                results.append({"id": item.id, "ok": False, "reason": f"执行异常: {exc}"})
        finally:
            _ACTIVE_REPAIR_PATHS.discard(norm_src)
            item.updated_at = _utc_now()

    await db.commit()
    return {"results": results, "total": len(items)}


async def get_repair_summary(
    db: AsyncSession,
    mp_client: MoviePilotClient | None = None,
) -> dict[str, Any]:
    """获取媒体整理修复仪表盘统计数据（第 9.1 节，严格按自然日区分‘今日修复/今日结案’）。"""
    baseline = await get_or_create_baseline(db, mp_client)

    mp_queue_tasks: list[dict[str, Any]] = []
    mp_version = baseline.mp_version or ""
    if mp_client:
        try:
            raw_q = await mp_client.get_transfer_queue()
            mp_queue_tasks = MoviePilotClient.flatten_transfer_queue(raw_q)
        except Exception:
            pass
        if not mp_version and hasattr(mp_client, "get_system_version"):
            try:
                mp_version = await mp_client.get_system_version()
                if mp_version:
                    baseline.mp_version = mp_version
                    await db.commit()
            except Exception:
                pass

    today_start = _today_start_utc()

    res = await db.execute(
        select(
            TransferRepairItem.status,
            TransferRepairItem.resolution_code,
            TransferRepairItem.resolved_at,
            TransferRepairItem.updated_at,
        )
    )
    rows = res.all()

    counts: dict[str, int] = {
        "pending": 0,
        "ready": 0,
        "waiting_env": 0,
        "needs_confirmation": 0,
        "conflict": 0,
        "executing": 0,
        "resolved_today": 0,
        "resolved_total": 0,
        "already_present_verified": 0,
        "repaired_today": 0,
        "skipped": 0,
        "protected": 0,
        "exhausted": 0,
    }

    for st, res_code, resolved_at, updated_at in rows:
        if st in PENDING_STATUSES:
            counts["pending"] += 1
        if st == "ready":
            counts["ready"] += 1
        elif st == "waiting_environment":
            counts["waiting_env"] += 1
        elif st == "needs_confirmation":
            counts["needs_confirmation"] += 1
        elif st == "conflict":
            counts["conflict"] += 1
        elif st in ("submitted", "verifying"):
            counts["executing"] += 1
        elif st == "resolved":
            counts["resolved_total"] += 1
            ts = resolved_at or updated_at
            if ts is not None:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                is_today = ts >= today_start
            else:
                is_today = False
            if is_today:
                counts["resolved_today"] += 1
                if res_code in ("already_present_verified", "superseded_by_success"):
                    counts["already_present_verified"] += 1
                elif res_code == "repaired":
                    counts["repaired_today"] += 1
        elif st == "skipped":
            counts["skipped"] += 1
        elif st == "protected":
            counts["protected"] += 1
        elif st == "exhausted":
            counts["exhausted"] += 1

    return {
        "baseline_status": baseline.baseline_status,
        "baseline_history_id": baseline.baseline_history_id,
        "baseline_date": baseline.baseline_date,
        "mp_version": mp_version,
        "takeover_at": baseline.takeover_at.isoformat() if baseline.takeover_at else None,
        "last_scanned_at": baseline.last_scanned_at.isoformat() if baseline.last_scanned_at else None,
        "last_scan_truncated": bool(baseline.last_scan_truncated),
        "last_scan_summary": json.loads(baseline.last_scan_summary_json or "{}"),
        "is_enabled": baseline.is_enabled,
        "auto_repair_enabled": baseline.auto_repair_enabled,
        "max_concurrency": baseline.max_concurrency,
        "batch_quota": baseline.batch_quota,
        "retry_budget": baseline.retry_budget,
        "scan_interval_minutes": baseline.scan_interval_minutes,
        "mp_queue_count": len(mp_queue_tasks),
        "mp_queue_tasks": mp_queue_tasks[:20],
        "counts": counts,
    }


async def get_repair_items(
    db: AsyncSession,
    tab: str = "pending",
    category: str | None = None,
    keyword: str | None = None,
    group_by: str = "work",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """查询媒体整理修复项列表，支持按标签页分流与按作品/季/发布包或故障组聚合（第 9.2 节）。"""
    query = select(TransferRepairItem)

    if tab == "pending":
        query = query.where(TransferRepairItem.status.in_(list(PENDING_STATUSES) + ["exhausted"]))
    elif tab == "executing":
        query = query.where(TransferRepairItem.status.in_(["submitted", "verifying"]))
    elif tab == "resolved":
        query = query.where(TransferRepairItem.status == "resolved")
    elif tab == "rules_records":
        query = query.where(TransferRepairItem.status.in_(["skipped", "protected"]))

    if category:
        query = query.where(TransferRepairItem.error_category == category)
    if keyword:
        query = query.where(
            (TransferRepairItem.title.ilike(f"%{keyword}%"))
            | (TransferRepairItem.mp_title.ilike(f"%{keyword}%"))
            | (TransferRepairItem.file_basename.ilike(f"%{keyword}%"))
        )

    total_res = await db.execute(select(func.count()).select_from(query.subquery()))
    total = int(total_res.scalar() or 0)

    query = query.order_by(TransferRepairItem.latest_history_id.desc(), TransferRepairItem.updated_at.desc()).offset(offset).limit(limit)
    items = (await db.execute(query)).scalars().all()

    items_data: list[dict[str, Any]] = []
    for it in items:
        items_data.append({
            "id": it.id,
            "source_path": it.source_path,
            "file_basename": it.file_basename,
            "file_size": it.file_size,
            "src_storage": it.src_storage or it.storage or "local",
            "dest_storage": it.dest_storage or it.target_storage or "",
            "latest_history_id": it.latest_history_id,
            "history_ids": json.loads(it.mp_history_ids_json or "[]"),
            "mp_title": it.mp_title,
            "mp_seasons": it.mp_seasons,
            "mp_episodes": it.mp_episodes,
            "mp_mode": it.mp_mode,
            "mp_date": it.mp_date,
            "title": it.title,
            "media_type": it.media_type,
            "tmdb_id": it.tmdb_id,
            "season_number": it.season_number,
            "episode_numbers": json.loads(it.episode_numbers_json or "[]"),
            "part": it.part,
            "is_special": it.is_special,
            "special_type": it.special_type,
            "target_path": it.target_path,
            "status": it.status,
            "error_category": it.error_category,
            "main_block_reason": it.main_block_reason,
            "failure_group_key": it.failure_group_key,
            "raw_error": it.raw_error,
            "resolution_code": it.resolution_code,
            "skip_reason": it.skip_reason,
            "plan_hash": it.plan_hash,
            "plan": json.loads(it.plan_json or "{}"),
            "evidence": json.loads(it.evidence_json or "{}"),
            "target_verify": json.loads(it.target_verify_json or "{}"),
            "preview": json.loads(it.preview_json or "{}"),
            "verification": json.loads(it.verification_json or "{}"),
            "is_protected": it.is_protected,
            "retry_count": it.retry_count,
            "updated_at": it.updated_at.isoformat() if it.updated_at else None,
        })

    # 构建分组聚合视图（默认按作品/季/发布包；可选按故障组）
    groups_map: dict[str, dict[str, Any]] = {}
    if group_by != "none":
        for d in items_data:
            if group_by == "failure":
                g_key = f"{d['error_category']}:{d['status']}:{d['src_storage']}"
                g_label = f"{d['error_category']} ({d['status']} · {d['src_storage']})"
            else:
                g_key = d["failure_group_key"] or f"{d['title'] or '未识别'}:S{d['season_number'] or 0}"
                season_s = f"S{int(d['season_number']):02d}" if d["season_number"] is not None else ("电影" if d["media_type"] == "movie" else "季待定")
                g_label = f"{d['title'] or d['mp_title'] or '未识别作品'} · {season_s}"

            if g_key not in groups_map:
                groups_map[g_key] = {
                    "group_key": g_key,
                    "label": g_label,
                    "title": d["title"] or d["mp_title"] or "未识别作品",
                    "tmdb_id": d["tmdb_id"],
                    "media_type": d["media_type"],
                    "season_number": d["season_number"],
                    "primary_status": d["status"],
                    "primary_reason": d["main_block_reason"],
                    "ready_count": 0,
                    "items": [],
                }
            g = groups_map[g_key]
            g["items"].append(d)
            if d["status"] == "ready":
                g["ready_count"] += 1

    return {
        "total": total,
        "items": items_data,
        "groups": list(groups_map.values()),
    }


async def update_item_plan_choice(
    db: AsyncSession,
    item_id: str,
    choice_data: dict[str, Any],
) -> dict[str, Any]:
    """用户补充一次选择并重新生成核验计划（第 9.4 节，支持同包/同目录规则持久化与复用）。"""
    item = await db.get(TransferRepairItem, item_id)
    if not item:
        return {"ok": False, "message": "修复项不存在"}

    title = str(choice_data.get("title") or item.title or "").strip()
    media_type = str(choice_data.get("media_type") or item.media_type or "tv")
    tmdb_id = choice_data.get("tmdb_id")
    if tmdb_id is not None and str(tmdb_id).strip():
        tmdb_id = int(tmdb_id)
    else:
        tmdb_id = item.tmdb_id

    if media_type == "movie":
        season_number = None
        episodes: list[int] = []
        part_str = None
        is_special = False
        special_type = None
    else:
        season_number = choice_data.get("season_number")
        if season_number is not None:
            season_number = int(season_number)
        episodes_in = choice_data.get("episode_numbers")
        if episodes_in is not None and isinstance(episodes_in, list):
            episodes = [int(e) for e in episodes_in]
        else:
            episodes = json.loads(item.episode_numbers_json or "[]")
        part_str = choice_data.get("part") if "part" in choice_data else item.part
        is_special = bool(choice_data.get("is_special", item.is_special))
        special_type = choice_data.get("special_type") or item.special_type

    episode_offset = int(choice_data.get("episode_offset") or 0)
    special_handling = str(choice_data.get("special_handling") or "standard")

    plan = json.loads(item.plan_json or "{}")
    if special_handling == "hold":
        item.status = "resolved"
        item.resolution_code = "user_disposition"
        item.resolved_at = _utc_now()
        item.main_block_reason = "用户选择保留源文件暂不整理（已结案，不计入自动修复成功）"
        plan["safe_to_execute"] = False
    else:
        safe_exec = bool(
            title
            and not plan.get("dest_cleanup_risk")
            and (media_type == "movie" or (season_number is not None and (episodes or is_special)))
        )
        plan.update({
            "title": title,
            "media_type": media_type,
            "tmdb_id": tmdb_id,
            "season_number": season_number,
            "episode_numbers": episodes,
            "part": part_str,
            "episode_offset": episode_offset,
            "is_special": is_special,
            "special_type": special_type,
            "special_handling": special_handling,
            "user_confirmed": True,
            "safe_to_execute": safe_exec,
            "updated_at": _utc_now().isoformat(),
        })
        if safe_exec:
            item.status = "ready"
            item.main_block_reason = f"用户已确认《{title}》参数，可生成预览并执行整理"
        else:
            item.status = "needs_confirmation"

    p_hash = calculate_plan_hash(plan)
    item.title = title
    item.media_type = media_type
    item.tmdb_id = tmdb_id
    item.season_number = season_number
    item.episode_numbers_json = json.dumps(episodes, ensure_ascii=False)
    item.part = part_str
    item.is_special = is_special
    item.special_type = special_type
    item.plan_json = json.dumps(plan, ensure_ascii=False)
    item.plan_hash = p_hash
    item.updated_at = _utc_now()

    remember_scope = choice_data.get("remember_scope")
    if remember_scope in ("this_package", "this_dir_template"):
        parent_dir = posixpath.dirname(item.source_path)
        scope_key = posixpath.basename(parent_dir) if remember_scope == "this_package" else parent_dir
        rule = TransferRepairRule(
            scope_kind=remember_scope,
            scope_key=scope_key,
            show_title=title,
            media_type=media_type,
            tmdb_id=tmdb_id,
            season_number=season_number if season_number is not None else 1,
            numbering_mode=str(choice_data.get("numbering_mode") or ("offset" if episode_offset else "standard")),
            episode_offset=episode_offset,
            special_handling=special_handling,
            note=f"由修复项 #{item.id[:8]} 确认保存",
        )
        db.add(rule)

    await db.commit()
    return {"ok": True, "plan_hash": p_hash, "status": item.status, "message": "选择已更新并生成计划"}


async def run_transfer_repair_tick(db: AsyncSession, mp_client: MoviePilotClient) -> dict[str, Any]:
    """后台调度器单次心跳：核验在途项 → 定时增量扫描 → 自动修复 ready 项（受并发与额度约束）。"""
    baseline = await get_or_create_baseline(db, mp_client)
    tick_info: dict[str, Any] = {"verified": 0, "scanned": False, "auto_executed": 0}

    # 1. 恢复并核验所有 submitted / verifying 项（R19）
    ver_res = await db.execute(
        select(TransferRepairItem.id).where(TransferRepairItem.status.in_(["submitted", "verifying"])).limit(20)
    )
    for (item_id,) in ver_res.all():
        await verify_repair_item(db, mp_client, item_id)
        tick_info["verified"] += 1

    # 2. 达到扫描周期则执行增量扫描
    if baseline.is_enabled and baseline.baseline_status == "active":
        interval_sec = max(60, int(baseline.scan_interval_minutes or 10) * 60)
        due = (
            baseline.last_scanned_at is None
            or (_utc_now() - (baseline.last_scanned_at.replace(tzinfo=timezone.utc) if baseline.last_scanned_at.tzinfo is None else baseline.last_scanned_at)).total_seconds()
            >= interval_sec
        )
        if due:
            await scan_new_failures(db, mp_client)
            tick_info["scanned"] = True

    # 3. 若用户开启自动修复，按 batch_quota 执行高确定性 ready 项及到期退避重试项
    if baseline.auto_repair_enabled and baseline.baseline_status == "active":
        now = _utc_now()
        quota = max(1, min(int(baseline.batch_quota or 20), 20))
        ready_res = await db.execute(
            select(TransferRepairItem.id)
            .where(
                TransferRepairItem.is_protected == False,  # noqa: E712
                (
                    (TransferRepairItem.status == "ready")
                    | (
                        (TransferRepairItem.status == "waiting_environment")
                        & (TransferRepairItem.next_retry_at != None)  # noqa: E711
                        & (TransferRepairItem.next_retry_at <= now)
                        & (TransferRepairItem.retry_count < baseline.retry_budget)
                    )
                ),
            )
            .limit(quota)
        )
        ids = [row[0] for row in ready_res.all()]
        if ids:
            exec_res = await execute_repair_plans(db, mp_client, ids)
            tick_info["auto_executed"] = len(exec_res.get("results", []))

    return tick_info


async def transfer_repair_scheduler_loop(poll_seconds: int = 60) -> None:
    """挂入 FastAPI lifespan 的轻量后台调度循环，使自动发现、核验与自动修复真正闭环运行。"""
    from app.config import get_settings
    from app.database import async_session

    await asyncio.sleep(15)
    while True:
        try:
            settings = get_settings()
            if settings.mp_url and settings.mp_token:
                mp = MoviePilotClient()
                try:
                    async with async_session() as db:
                        await run_transfer_repair_tick(db, mp)
                finally:
                    await mp.close()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.warning("媒体整理修复后台调度心跳异常: %s", exc)
        await asyncio.sleep(poll_seconds)
