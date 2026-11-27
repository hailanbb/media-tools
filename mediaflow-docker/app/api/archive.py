"""归档与清理端点 — 生产级 CD2 文件操作与状态级联流转。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import posixpath
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.clouddrive2 import CloudDrive2Client
from app.clients.moviepilot import MoviePilotClient
from app.config import get_settings
from app.core.directory_governance import find_blocking_group_for_record, normalize_dir_path
from app.core.episode_parser import companion_matches_video, classify_content_unit, is_subtitle_file, is_video_file, parse_episode
from app.core.inspector import _parse_season_number, _verified_video, diagnose_season
from app.core.segments import segment_number
from app.core.source_cleanup_assets import (
    checked_listing, read_auxiliary_tree, preserve_auxiliary_folder, verify_preserved_folder,
    preserve_source_show_shell,
)
from app.services.source_presence import source_entry, record_source_absent
from app.core import batch_journal
from app.core.review_engine import _strip_cd2_dup_suffix, compare_plan_fingerprints, generate_review_clean_plan
from app.database import get_db
from app.models.operation import OperationLog
from app.models.season import SeasonRecord
from app.models.task import Task
from app.schemas.common import StatusResponse

router = APIRouter()
log = logging.getLogger("mediaflow.archive")

# 全局受保护的文件主干名（不区分大小写，绝对不能作为伴生文件删除）
GLOBAL_PROTECTED_STEMS = {
    "poster", "season", "tvshow", "folder", "banner",
    "fanart", "clearart", "clearlogo", "logo", "disc",
    "landscape", "backdrop", "characterart", "keyart", "thumb", "theme"
}

KNOWN_PUBLIC_ASSET_EXTS = {
    ".nfo", ".jpg", ".jpeg", ".png", ".webp", ".tbn", ".svg", ".xml"
}


class _ArchiveRequest(BaseModel):
    season_record_ids: list[str]
    force_clean: bool = False
    plan_id: str | None = None
    batch_id: str | None = None
    expected_fingerprints: dict[str, dict[str, Any]] | None = None


class ArchivePreviewRequest(BaseModel):
    season_record_ids: list[str]
    task_id: str | None = None


class ArchiveReconcileRequest(BaseModel):
    season_record_id: str
    resume_remaining: bool = False


class ArchiveExecuteResponse(StatusResponse):
    batch_id: str = ""
    plan_id: str = ""
    results: list[dict] = []
    missing_ids: list[str] = []
    summary: dict[str, int] = {}


class CalibrateEpisodesRequest(BaseModel):
    season_record_id: str
    total_episodes: int


# 剧目级归档互斥锁字典与防护锁
_SHOW_ARCHIVE_LOCKS: dict[str, asyncio.Lock] = {}
_SHOW_ARCHIVE_LOCKS_GUARD = asyncio.Lock()

# 预检计划快照与批次结果缓存
_ARCHIVE_PREVIEW_PLANS: dict[str, dict] = {}
_COMPLETED_PLAN_SEASONS: dict[tuple[str, str], dict] = {}
_LAST_ARCHIVE_BATCH_BY_TASK: dict[str, dict] = {}


def _is_known_show_public_asset(filename: str) -> bool:
    """判定剧集根目录下的文件是否为已知的公共刮削资产（如 tvshow.nfo, poster.jpg, backdrop.jpg 等）。"""
    if not filename:
        return False
    lower_name = filename.strip().lower()
    stem, ext = os.path.splitext(lower_name)
    if ext not in KNOWN_PUBLIC_ASSET_EXTS:
        return False
    if stem in GLOBAL_PROTECTED_STEMS or stem.startswith("season"):
        return True
    for suffix in ("-poster", "-fanart", "-banner", "-logo", "-clearart", "-clearlogo", "-thumb", "-landscape", "-backdrop"):
        if stem.endswith(suffix):
            return True
    return False


async def _acquire_show_locks(paths: list[str]) -> list[asyncio.Lock]:
    """按规范化排序顺序认领路径锁，防止并发死锁。"""
    sorted_paths = sorted(set(p for p in paths if p))
    locks: list[asyncio.Lock] = []
    async with _SHOW_ARCHIVE_LOCKS_GUARD:
        for p in sorted_paths:
            if p not in _SHOW_ARCHIVE_LOCKS:
                _SHOW_ARCHIVE_LOCKS[p] = asyncio.Lock()
            locks.append(_SHOW_ARCHIVE_LOCKS[p])
    for lock in locks:
        await lock.acquire()
    return locks


def _release_show_locks(locks: list[asyncio.Lock]) -> None:
    for lock in reversed(locks):
        if lock.locked():
            lock.release()


async def _inspect_and_cleanup_source_show_dir(
    cd2: CloudDrive2Client,
    show_path: str,
    target_parent: str | None = None,
    *,
    before_move=None,
    previous_receipt: dict | None = None,
) -> dict:
    """检查并尝试回收源剧根目录，区分已知公共资产与未知附件，返回结构化收尾结论。"""
    if not show_path or len(show_path.strip("/").split("/")) < 2:
        return {
            "cleaned": False,
            "status": "retained_expected",
            "reason": "路径层级过浅，安全保留",
            "retained_items": [],
            "migrated_assets": [],
        }

    norm_show = posixpath.normpath(show_path)
    settings = get_settings()
    norm_source_root = posixpath.normpath(settings.source_root.rstrip("/")) if settings.source_root else ""
    if not norm_source_root or norm_show != show_path or norm_show == norm_source_root or not norm_show.startswith(norm_source_root + "/"):
        log.warning("剧集目录超出或等于源库根目录，严禁删除: %s", show_path)
        return {
            "cleaned": False,
            "status": "retained_expected",
            "reason": "剧集目录超出或等于源库根目录，安全保留",
            "retained_items": [],
            "migrated_assets": [],
        }

    try:
        parent = posixpath.dirname(norm_show)
        siblings = checked_listing(await cd2.list_files(parent, force_refresh=True), parent)
        if not any(f["path"] == norm_show for f in siblings):
            if previous_receipt:
                expected_shelf = str(target_parent or "") + "/合并保留/"
                receipt_target = previous_receipt.get("target_path", "")
                if (previous_receipt.get("source_path") != norm_show
                        or not receipt_target.startswith(expected_shelf)
                        or posixpath.normpath(receipt_target) != receipt_target):
                    raise ValueError("剧根保全回执与当前路径不一致")
                await verify_preserved_folder(cd2, previous_receipt)
            return dict(cleaned=True, status="cleaned", reason="已核实源剧集外层目录不存在",
                        retained_items=[], migrated_assets=[])
        source_items = checked_listing(await cd2.list_files(show_path, force_refresh=True), show_path)
    except Exception as e:
        log.warning("检查源剧集目录失败: %s - %s", show_path, e)
        return {
            "cleaned": False,
            "status": "cleanup_failed",
            "reason": f"读取源剧集目录失败: {e}",
            "retained_items": [],
            "migrated_assets": [],
        }

    subdirs = [f for f in source_items if f.get("isDirectory")]
    if any(f.get("name") != "合并保留" for f in subdirs):
        subdir_names = ", ".join(f.get("name", "") for f in subdirs[:3])
        all_retained = [f.get("name", "") for f in source_items if f.get("name")]
        log.info("源剧集目录仍有子目录 (%s 等)，安全保留源剧集主目录: %s", subdir_names, show_path)
        return {
            "cleaned": False,
            "status": "retained_expected",
            "reason": f"源剧集目录仍有子目录 ({subdir_names})，剧根及公共资产留源",
            "retained_items": all_retained,
            "migrated_assets": [],
        }

    videos = [f for f in source_items if not f.get("isDirectory") and is_video_file(f.get("name", ""))]
    if videos:
        all_retained = [f.get("name", "") for f in source_items if f.get("name")]
        log.info("源剧集目录仍有视频文件，安全保留源剧集主目录: %s", show_path)
        return {
            "cleaned": False,
            "status": "retained_expected",
            "reason": "源剧集目录仍有视频文件，安全保留源剧根",
            "retained_items": all_retained,
            "migrated_assets": [],
        }

    non_dir_files = [f for f in source_items if not f.get("isDirectory")]
    known_assets = [f for f in non_dir_files if _is_known_show_public_asset(f.get("name", ""))]
    unknown_attachments = [f for f in non_dir_files if not _is_known_show_public_asset(f.get("name", ""))]

    migrated_assets: list[str] = []
    if (subdirs or known_assets) and target_parent and not unknown_attachments:
        try:
            norm_archive = posixpath.normpath(settings.archive_root.rstrip("/")) if settings.archive_root else ""
            if (not norm_source_root or not norm_archive
                    or norm_show != show_path or posixpath.normpath(target_parent) != target_parent
                    or not target_parent.startswith(norm_archive + "/")
                    or norm_show == target_parent or norm_show.startswith(target_parent + "/")
                    or target_parent.startswith(norm_show + "/")):
                raise ValueError("剧根保全路径不在隔离的受控源库与归档库内")
            receipt = await preserve_source_show_shell(
                cd2, show_path, target_parent,
                extensions=KNOWN_PUBLIC_ASSET_EXTS | {".txt"}, before_move=before_move)
            return dict(cleaned=True, status="cleaned", reason="剧根附件已保全，源剧集外层目录已移除",
                        retained_items=[], migrated_assets=list(receipt["inventory"]),
                        preserved_root={k: v for k, v in receipt.items() if k != "inventory"})
        except Exception as e:
            return dict(cleaned=False, status="cleanup_failed", reason=f"剧根附件保全未完成：{e}",
                        retained_items=[f["name"] for f in source_items], migrated_assets=[])
    if unknown_attachments:
        unk_names = [u.get("name", "") for u in unknown_attachments if u.get("name")]
        log.info("源剧集目录存在未知附件 (%s)，保留源剧集目录: %s", ", ".join(unk_names[:3]), show_path)
        return {
            "cleaned": False,
            "status": "retained_unknown_attachments",
            "reason": f"源剧集目录存在未知附件 ({', '.join(unk_names[:3])})，已安全留源",
            "retained_items": [f.get("name", "") for f in non_dir_files if f.get("name")],
            "migrated_assets": migrated_assets,
        }

    try:
        final_check = checked_listing(await cd2.list_files(show_path, force_refresh=True), show_path)
        if final_check:
            rem_names = [f.get("name", "") for f in final_check if f.get("name")]
            log.info("源剧集目录尚有残留项目 (%d 个)，保留主目录: %s", len(final_check), show_path)
            return {
                "cleaned": False,
                "status": "retained_expected",
                "reason": f"源剧集目录仍有同名或留存资产 ({', '.join(rem_names[:3])})，安全留源",
                "retained_items": rem_names,
                "migrated_assets": migrated_assets,
            }

        result = await cd2.delete_file(show_path)
        if not isinstance(result, dict) or result.get("success") is not True:
            raise ValueError("剧根删除回执未确认成功")
        siblings = checked_listing(await cd2.list_files(parent, force_refresh=True), parent)
        if any(f["path"] == norm_show for f in siblings):
            raise ValueError("剧根删除后仍可见")
        log.info("源端全剧已无其余季度与任何子目录/文件，已彻底安全清理源端空壳目录: %s", show_path)
        return {
            "cleaned": True,
            "status": "cleaned",
            "reason": "源端全剧已无其余季度，已同步公共资产并清理空壳剧根",
            "retained_items": [],
            "migrated_assets": migrated_assets,
        }
    except Exception as del_err:
        log.warning("清理源剧集空壳目录失败: %s - %s", show_path, del_err)
        return {
            "cleaned": False,
            "status": "cleanup_failed",
            "reason": f"源剧集空壳目录回收失败 ({del_err})",
            "retained_items": [],
            "migrated_assets": migrated_assets,
        }


async def _safe_cleanup_source_show_dir(
    cd2: CloudDrive2Client,
    show_path: str,
    target_parent: str | None = None,
) -> bool:
    """源端剧集目录空壳安全清理门禁（兼容布尔返回签名）。"""
    res = await _inspect_and_cleanup_source_show_dir(cd2, show_path, target_parent)
    return bool(res.get("cleaned"))


def _identify_specials_in_season(
    contents: list[dict],
    target_parent: str,
    target_parent_files: list[dict],
) -> dict:
    """只读识别季内先导特辑 (E00/SP01) 及伴生文件，不执行任何写入。"""
    special_videos = []
    for f in contents:
        if f.get("isDirectory"):
            continue
        fname = f.get("name", "")
        if not is_video_file(fname):
            continue
        parsed = parse_episode(fname)
        is_e00 = (parsed.episode_numbers == [0]) or bool(
            re.search(r"(?:^|[^a-z0-9])[Ss]0*\d+[Ee]00(?!\d)|(?:^|[^a-z0-9])[Ee]00(?!\d)|SP0*1", fname, re.I)
        )
        if is_e00:
            special_videos.append(f)

    if not special_videos:
        return {
            "special_videos": [],
            "special_files_to_move": [],
            "regular_contents": list(contents),
            "s0_dir_name": "Season 0",
            "target_s0_path": posixpath.join(target_parent, "Season 0") if target_parent else "",
            "candidate_s0_dirs": [],
        }

    special_stems = {re.sub(r"\.[^.]+$", "", v.get("name", "")).lower() for v in special_videos}
    special_files_to_move = []
    regular_contents = []

    for f in contents:
        fname = f.get("name", "")
        stem = re.sub(r"\.[^.]+$", "", fname).lower()
        clean_stem = re.sub(r"-thumb$", "", stem)
        if stem in special_stems or clean_stem in special_stems or f in special_videos:
            special_files_to_move.append(f)
        else:
            regular_contents.append(f)

    candidate_s0_dirs = [
        tf for tf in (target_parent_files or [])
        if tf.get("isDirectory") and tf.get("name", "").lower() in ("season 0", "season 00", "specials", "特别篇")
    ]
    s0_dir_name = candidate_s0_dirs[0].get("name") if candidate_s0_dirs else "Season 0"
    target_s0_path = posixpath.join(target_parent, s0_dir_name) if target_parent else ""

    return {
        "special_videos": special_videos,
        "special_files_to_move": special_files_to_move,
        "regular_contents": regular_contents,
        "s0_dir_name": s0_dir_name,
        "target_s0_path": target_s0_path,
        "candidate_s0_dirs": candidate_s0_dirs,
    }


async def _migrate_specials_to_season_0(
    cd2: CloudDrive2Client,
    contents: list[dict],
    target_parent: str,
    target_parent_files: list[dict],
    season_number: int,
) -> tuple[list[dict], list[str]]:
    """检测并迁移季内先导特辑 (E00/SP01) 至目标剧集的 Season 0 (特别篇) 目录。"""
    spec_info = _identify_specials_in_season(contents, target_parent, target_parent_files)
    special_files_to_move = spec_info["special_files_to_move"]
    remaining_contents = list(spec_info["regular_contents"])
    if not special_files_to_move:
        return contents, []

    target_s0_path = spec_info["target_s0_path"]
    try:
        await cd2.mkdir(target_s0_path)
    except Exception as e:
        log.warning("创建目标特别篇目录 %s 失败或已存在: %s", target_s0_path, e)

    moved_names = []
    for sf in special_files_to_move:
        src_path = sf.get("path")
        if not src_path:
            continue
        try:
            mv_res = await cd2.move_file(src_path, target_s0_path, skip_existing=True)
            if mv_res.get("success", True):
                moved_names.append(sf.get("name", ""))
                log.info("已智能将先导特辑移入特别篇目录: %s -> %s", sf.get("name"), target_s0_path)
            else:
                log.warning("迁移先导特辑 %s 失败: %s", sf.get("name"), mv_res.get("errorMessage"))
                remaining_contents.append(sf)
        except Exception as mv_err:
            log.warning("迁移先导特辑 %s 异常: %s", sf.get("name"), mv_err)
            remaining_contents.append(sf)

    return remaining_contents, moved_names


def _find_cross_season_or_unisolated_items(
    regular_contents: list[dict],
    season_number: int,
    is_flat_show: bool,
) -> list[str]:
    """检查源目录是否存在跨季文件或平铺剧根未隔离的子目录 (A07)。"""
    offending: list[str] = []
    for f in regular_contents:
        fname = f.get("name", "")
        if f.get("isDirectory"):
            if is_flat_show:
                offending.append(f"子目录:{fname}")
            continue
        if is_video_file(fname):
            parsed = parse_episode(fname)
            if (
                parsed.season_number is not None
                and parsed.season_number > 0
                and parsed.season_number != season_number
            ):
                offending.append(fname)
    return offending


def _find_same_episode_conflicts(
    src_season_files: list[dict],
    dest_season_files: list[dict],
    season_number: int,
) -> tuple[list[str], list[dict]]:
    """检查增量合并时目标季是否存在同名视频或同集异名冲突视频 (A05, A07)。"""
    dest_video_names_lower = {
        f.get("name", "").lower(): f.get("name", "")
        for f in dest_season_files
        if not f.get("isDirectory") and is_video_file(f.get("name", ""))
    }
    duplicate_videos = [
        f.get("name", "")
        for f in src_season_files
        if not f.get("isDirectory")
        and is_video_file(f.get("name", ""))
        and f.get("name", "").lower() in dest_video_names_lower
    ]

    dest_ep_map: dict[int, str] = {}
    for df in dest_season_files:
        if df.get("isDirectory"):
            continue
        dname = df.get("name", "")
        if not is_video_file(dname):
            continue
        parsed_d = parse_episode(dname)
        for ep in (parsed_d.episode_numbers or []):
            if isinstance(ep, int) and ep > 0:
                dest_ep_map.setdefault(ep, dname)

    same_ep_diff_name_conflicts: list[dict] = []
    for sf in src_season_files:
        if sf.get("isDirectory"):
            continue
        sname = sf.get("name", "")
        if not is_video_file(sname) or sname.lower() in dest_video_names_lower:
            continue
        parsed_s = parse_episode(sname)
        conflict_eps = [
            ep for ep in (parsed_s.episode_numbers or [])
            if isinstance(ep, int) and ep > 0 and ep in dest_ep_map
        ]
        if conflict_eps:
            same_ep_diff_name_conflicts.append({
                "episodes": conflict_eps,
                "source_file": sname,
                "target_file": dest_ep_map[conflict_eps[0]],
            })

    return duplicate_videos, same_ep_diff_name_conflicts


async def _expand_complementary_records(
    db: AsyncSession,
    records: list[SeasonRecord],
) -> tuple[list[SeasonRecord], dict[str, list[SeasonRecord]]]:
    """针对同一作品季、已绑定同一归档目标的互补源目录，自动扩展纳入同一执行/预检计划 (A06)。"""
    from app.core.season_classifier import TERMINAL_STATUSES, build_work_key

    task_ids = {r.task_id for r in records if r.task_id}
    if not task_ids:
        return records, {}

    stmt = select(SeasonRecord).where(
        SeasonRecord.task_id.in_(task_ids),
        SeasonRecord.status.not_in(tuple(TERMINAL_STATUSES)),
    )
    all_task_recs = list((await db.execute(stmt)).scalars().all())

    by_work_key: dict[tuple[str, str], list[SeasonRecord]] = {}
    for tr in all_task_recs:
        wk = build_work_key(tr)
        by_work_key.setdefault((tr.task_id, wk), []).append(tr)

    expanded: list[SeasonRecord] = []
    seen_ids: set[str] = set()
    complementary_map: dict[str, list[SeasonRecord]] = {}

    for rec in records:
        if rec.id not in seen_ids:
            expanded.append(rec)
            seen_ids.add(rec.id)
        wk = build_work_key(rec)
        if not wk.startswith("tmdb:"):
            continue
        siblings = by_work_key.get((rec.task_id, wk), [])
        if len(siblings) <= 1:
            continue

        # 检查是否所有 sibling 均指向同一归档目标且集数互补无重叠
        tot = rec.total_episodes or 0
        if tot <= 0:
            continue
        target_set = {
            posixpath.normpath(s.archive_target_path.rstrip("/"))
            for s in siblings
            if s.archive_target_path
        }
        if len(target_set) != 1:
            continue
        union_eps: set[int] = set()
        overlap = False
        for s in siblings:
            try:
                s_eps = {int(e) for e in json.loads(s.recognized_episodes_json or "[]") if str(e).isdigit() and int(e) > 0}
            except Exception:
                s_eps = set()
            if union_eps & s_eps:
                overlap = True
                break
            union_eps |= s_eps
        if not overlap and set(range(1, tot + 1)) <= union_eps:
            complementary_map[rec.id] = [s for s in siblings if s.id != rec.id]
            for s in siblings:
                if s.id not in seen_ids:
                    if s.status not in ("archive_ready", "period_ready"):
                        s.status = "archive_ready"
                    expanded.append(s)
                    seen_ids.add(s.id)

    return expanded, complementary_map


async def _refresh_task_summary(task_id: str, db: AsyncSession) -> dict[str, Any]:
    """级联刷新关联 Task 的汇总统计 summary_json（含真缺集待补与已推送待入库）。"""
    from app.services.gap_fill_tracker import refresh_task_summary

    return await refresh_task_summary(task_id, db)


@router.post("/preview")
async def preview_archive(
    body: ArchivePreviewRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """只读归档预检 — 生成搬移计划快照，明示整移/增量合并/保留版本模式、特别篇去向、预期留源与阻断原因。"""
    from uuid import uuid4
    from app.core.season_classifier import build_work_key

    if not body.season_record_ids:
        return {
            "ok": False,
            "message": "未选择任何项目",
            "plan_id": "",
            "total_selected": 0,
            "executable_count": 0,
            "blocked_count": 0,
            "executable_ids": [],
            "blocked_ids": [],
            "missing_ids": [],
            "items": [],
        }

    settings = get_settings()
    if not settings.archive_root:
        return {
            "ok": False,
            "message": "未配置归档目录 (MF_ARCHIVE_ROOT)",
            "plan_id": "",
            "total_selected": len(body.season_record_ids),
            "executable_count": 0,
            "blocked_count": len(body.season_record_ids),
            "executable_ids": [],
            "blocked_ids": list(body.season_record_ids),
            "missing_ids": [],
            "items": [],
        }

    stmt = select(SeasonRecord).where(SeasonRecord.id.in_(body.season_record_ids))
    fetched = list((await db.execute(stmt)).scalars().all())
    by_id = {r.id: r for r in fetched}
    missing_ids = [rid for rid in body.season_record_ids if rid not in by_id]
    ordered_records = [by_id[rid] for rid in body.season_record_ids if rid in by_id]

    expanded_records, complementary_map = await _expand_complementary_records(db, ordered_records)

    norm_source_root = posixpath.normpath(settings.source_root.rstrip("/")) if settings.source_root else ""
    norm_archive_root = posixpath.normpath(settings.archive_root.rstrip("/")) if settings.archive_root else ""

    cd2 = CloudDrive2Client()
    plan_id = f"plan_{uuid4().hex[:16]}"
    items: list[dict] = []
    executable_ids: list[str] = []
    blocked_ids: list[str] = []

    try:
        for rec in expanded_records:
            work_key = build_work_key(rec)
            source_season_path = rec.season_path or ""
            norm_src_season = posixpath.normpath(source_season_path) if source_season_path else ""
            norm_src_show = posixpath.normpath(rec.show_path or posixpath.dirname(norm_src_season)) if norm_src_season else ""
            is_flat_show = bool(norm_src_show and norm_src_season and norm_src_show == norm_src_season)

            archive_target = rec.archive_target_path
            if not archive_target and norm_source_root and norm_src_season.startswith(norm_source_root + "/"):
                relative = norm_src_season[len(norm_source_root):]
                archive_target = norm_archive_root + relative
            norm_archive_target = posixpath.normpath(archive_target) if archive_target else ""
            norm_target_parent = posixpath.dirname(norm_archive_target) if norm_archive_target else ""
            season_dir_name = posixpath.basename(norm_src_season) if norm_src_season else f"Season {rec.season_number}"

            comp_siblings = complementary_map.get(rec.id, [])
            assoc_dirs = [
                {
                    "id": s.id,
                    "season_path": s.season_path,
                    "show_path": s.show_path,
                    "recognized_episodes": json.loads(s.recognized_episodes_json or "[]"),
                }
                for s in comp_siblings
            ]

            item_plan: dict = {
                "season_record_id": rec.id,
                "task_id": rec.task_id,
                "work_key": work_key,
                "show_name": rec.show_name,
                "season_number": rec.season_number,
                "executable": False,
                "state": "blocked",
                "mode": "blocked",
                "mode_label": "受阻暂停",
                "source_path": source_season_path,
                "show_path": norm_src_show,
                "target_path": norm_archive_target,
                "target_parent": norm_target_parent,
                "regular_files_to_move": [],
                "special_files_to_move": [],
                "specials_target_path": "",
                "target_existing_files": [],
                "expected_retained": [],
                "public_assets_to_migrate": [],
                "will_cleanup_show_root": False,
                "show_root_note": "",
                "associated_directories": assoc_dirs,
                "block_reason": "",
                "candidate_paths": [],
                "conflict_files": [],
                "next_step": "goto_anomaly",
                "summary_lines": [],
                "snapshot": {},
            }

            # 1. 冲突与状态门禁
            conflict_info = json.loads(rec.conflict_json or "{}") if getattr(rec, "conflict_json", None) else {}
            if isinstance(conflict_info, dict) and conflict_info.get("blocking"):
                item_plan["block_reason"] = (
                    f"存在身份/归属冲突 ({conflict_info.get('reason') or conflict_info.get('type')})，安全拦截归档"
                )
                item_plan["summary_lines"] = [f"✗ {item_plan['block_reason']}"]
                blocked_ids.append(rec.id)
                items.append(item_plan)
                continue

            from app.core.season_classifier import get_record_work_main_category
            work_cat = await get_record_work_main_category(db, rec)
            if work_cat == "airing" or rec.status == "airing":
                item_plan["block_reason"] = "当前作品季处于「连载留源」状态，连载期暂不开放归档"
                item_plan["summary_lines"] = [f"✗ {item_plan['block_reason']}"]
                blocked_ids.append(rec.id)
                items.append(item_plan)
                continue

            if rec.status not in ("archive_ready", "period_ready"):
                item_plan["block_reason"] = f"当前状态为 {rec.status}，不满足归档条件"
                item_plan["summary_lines"] = [f"✗ {item_plan['block_reason']}"]
                blocked_ids.append(rec.id)
                items.append(item_plan)
                continue

            if not archive_target:
                item_plan["block_reason"] = "无法自动计算归档目标路径"
                item_plan["summary_lines"] = [f"✗ {item_plan['block_reason']}"]
                blocked_ids.append(rec.id)
                items.append(item_plan)
                continue

            # 2. 路径边界核验
            if norm_source_root and not norm_src_season.startswith(norm_source_root + "/"):
                item_plan["block_reason"] = "源路径不在受控源根目录下"
            elif norm_archive_root and not norm_archive_target.startswith(norm_archive_root + "/"):
                item_plan["block_reason"] = "归档目标不在受控归档根目录下"
            elif norm_src_season == norm_archive_target:
                item_plan["block_reason"] = "源目录与目标目录相同，安全拦截"
            elif norm_archive_target.startswith(norm_src_season + "/") or norm_src_season.startswith(norm_archive_target + "/"):
                item_plan["block_reason"] = "源目录与目标目录互相包含，安全拦截"
            elif norm_src_show == norm_target_parent:
                item_plan["block_reason"] = "源剧集与目标剧集目录相同，安全拦截"

            if item_plan["block_reason"]:
                item_plan["summary_lines"] = [f"✗ {item_plan['block_reason']}"]
                blocked_ids.append(rec.id)
                items.append(item_plan)
                continue

            # 3. 多目录组门禁
            blocked_group = await find_blocking_group_for_record(
                db, rec, cd2_client=cd2, operation="archive_preview"
            )
            if blocked_group is not None:
                msg_reason = blocked_group.invalidated_reason or "请先查看目录内容并确认目标归档目录或标记有意共存"
                item_plan["block_reason"] = (
                    f"存在未确认的{blocked_group.title or '同 ID 多目录组'}（TMDB {blocked_group.tmdb_id}）— {msg_reason}"
                )
                item_plan["next_step"] = "goto_anomaly_dir_group"
                item_plan["summary_lines"] = [f"✗ {item_plan['block_reason']}"]
                blocked_ids.append(rec.id)
                items.append(item_plan)
                continue

            # 4. 读取源季目录
            try:
                contents = await cd2.list_files(source_season_path, force_refresh=True)
            except Exception as list_src_err:
                item_plan["block_reason"] = f"读取源季目录失败 ({list_src_err})"
                item_plan["state"] = "needs_recheck"
                item_plan["next_step"] = "retry_preview"
                item_plan["summary_lines"] = [f"✗ {item_plan['block_reason']}"]
                blocked_ids.append(rec.id)
                items.append(item_plan)
                continue

            preserve_versions = rec.status == "period_ready"
            if not preserve_versions and any(segment_number(f.get("name", "")) is not None for f in contents):
                item_plan["block_reason"] = "源季含分段编号文件，请先核对上下段和整期版"
                item_plan["next_step"] = "goto_anomaly"
                item_plan["summary_lines"] = [f"✗ {item_plan['block_reason']}"]
                blocked_ids.append(rec.id)
                items.append(item_plan)
                continue

            # 5. 只读检查目标剧集目录（若尚不存在则视为空列表，不创建目录）
            try:
                target_parent_files = await cd2.list_files(norm_target_parent, force_refresh=True)
            except Exception:
                target_parent_files = []

            spec_info = _identify_specials_in_season(contents, norm_target_parent, target_parent_files)
            regular_contents = spec_info["regular_contents"]
            special_files_to_move = spec_info["special_files_to_move"]
            target_s0_path = spec_info["target_s0_path"]
            candidate_s0_dirs = spec_info["candidate_s0_dirs"]

            # 跨季与平铺未隔离检查 (A07)
            cross_items = _find_cross_season_or_unisolated_items(regular_contents, rec.season_number, is_flat_show)
            if cross_items:
                item_plan["block_reason"] = f"源目录包含跨季或未隔离内容 ({', '.join(cross_items[:3])})，安全暂停"
                item_plan["conflict_files"] = cross_items
                item_plan["next_step"] = "goto_anomaly"
                item_plan["summary_lines"] = [f"✗ {item_plan['block_reason']}"]
                blocked_ids.append(rec.id)
                items.append(item_plan)
                continue

            # 特别篇目标候选与冲突检查 (A05)
            if special_files_to_move:
                if len(candidate_s0_dirs) > 1:
                    s0_paths = [d.get("path") or f"{norm_target_parent}/{d.get('name')}" for d in candidate_s0_dirs]
                    item_plan["block_reason"] = f"目标存在多个特别篇候选目录 ({', '.join(s0_paths)})，安全停止"
                    item_plan["candidate_paths"] = s0_paths
                    item_plan["summary_lines"] = [f"✗ {item_plan['block_reason']}"]
                    blocked_ids.append(rec.id)
                    items.append(item_plan)
                    continue
                if len(candidate_s0_dirs) == 1:
                    try:
                        s0_existing = await cd2.list_files(target_s0_path, force_refresh=True)
                    except Exception:
                        s0_existing = []
                    s0_video_names = {
                        f.get("name", "").lower()
                        for f in s0_existing
                        if not f.get("isDirectory") and is_video_file(f.get("name", ""))
                    }
                    dup_s0 = [
                        sv.get("name", "")
                        for sv in spec_info["special_videos"]
                        if sv.get("name", "").lower() in s0_video_names
                    ]
                    if dup_s0:
                        item_plan["block_reason"] = f"目标特别篇目录已存在同名视频 ({dup_s0[0]})，安全拦截"
                        item_plan["conflict_files"] = dup_s0
                        item_plan["summary_lines"] = [f"✗ {item_plan['block_reason']}"]
                        blocked_ids.append(rec.id)
                        items.append(item_plan)
                        continue

            # 目标同季候选目录检查 (A05)
            candidate_seasons = [
                f for f in target_parent_files if f.get("isDirectory") and (
                    _parse_season_number(f.get("name", "")) == rec.season_number or
                    f.get("name", "").lower() == season_dir_name.lower()
                )
            ]
            if len(candidate_seasons) > 1:
                candidate_paths = [f.get("path") or f"{norm_target_parent}/{f.get('name')}" for f in candidate_seasons]
                item_plan["block_reason"] = f"发现 {len(candidate_seasons)} 个可能的 S{rec.season_number:02d} 目标目录 ({', '.join(candidate_paths)})"
                item_plan["candidate_paths"] = candidate_paths
                item_plan["next_step"] = "goto_anomaly"
                item_plan["summary_lines"] = [f"✗ {item_plan['block_reason']}"]
                blocked_ids.append(rec.id)
                items.append(item_plan)
                continue

            existing_season = candidate_seasons[0] if candidate_seasons else None
            if existing_season and preserve_versions:
                item_plan["block_reason"] = "目标季已存在，保留版本归档已停止，请重新核对"
                item_plan["state"] = "needs_recheck"
                item_plan["next_step"] = "recheck"
                item_plan["summary_lines"] = [f"✗ {item_plan['block_reason']}"]
                blocked_ids.append(rec.id)
                items.append(item_plan)
                continue

            dest_season_files: list[dict] = []
            if existing_season:
                existing_season_path = existing_season.get("path") or f"{norm_target_parent}/{existing_season.get('name')}"
                item_plan["target_path"] = existing_season_path
                item_plan["mode"] = "incremental_merge"
                item_plan["mode_label"] = "增量合并至已有季"
                try:
                    dest_season_files = await cd2.list_files(existing_season_path, force_refresh=True)
                except Exception as dest_err:
                    item_plan["block_reason"] = f"读取已有目标季目录失败 ({dest_err})"
                    item_plan["state"] = "needs_recheck"
                    item_plan["next_step"] = "retry_preview"
                    item_plan["summary_lines"] = [f"✗ {item_plan['block_reason']}"]
                    blocked_ids.append(rec.id)
                    items.append(item_plan)
                    continue

                dup_videos, ep_conflicts = _find_same_episode_conflicts(
                    regular_contents, dest_season_files, rec.season_number
                )
                if dup_videos:
                    item_plan["block_reason"] = f"同季冲突：归档库已存在同名视频文件 ({dup_videos[0]})，已安全拦截"
                    item_plan["conflict_files"] = dup_videos
                    item_plan["summary_lines"] = [f"✗ {item_plan['block_reason']}"]
                    blocked_ids.append(rec.id)
                    items.append(item_plan)
                    continue
                if ep_conflicts:
                    c0 = ep_conflicts[0]
                    item_plan["block_reason"] = (
                        f"同集异名冲突：第 {c0['episodes']} 集在目标季已有不同版本 ({c0['target_file']})，已安全拦截"
                    )
                    item_plan["conflict_files"] = [c0["source_file"], c0["target_file"]]
                    item_plan["summary_lines"] = [f"✗ {item_plan['block_reason']}"]
                    blocked_ids.append(rec.id)
                    items.append(item_plan)
                    continue

                dest_file_names = {f.get("name", "").lower() for f in dest_season_files if not f.get("isDirectory")}
                item_plan["target_existing_files"] = [
                    f.get("name", "") for f in dest_season_files if not f.get("isDirectory") and f.get("name")
                ]
                for sf in regular_contents:
                    if sf.get("isDirectory"):
                        continue
                    sf_name = sf.get("name", "")
                    if not sf_name:
                        continue
                    if sf_name.lower() in dest_file_names:
                        item_plan["expected_retained"].append({
                            "name": sf_name,
                            "path": sf.get("path") or f"{source_season_path}/{sf_name}",
                            "scope": "season",
                            "reason": "目标季已有同名非视频伴生文件，默认双端保留",
                        })
                    else:
                        item_plan["regular_files_to_move"].append({
                            "name": sf_name,
                            "path": sf.get("path") or f"{source_season_path}/{sf_name}",
                            "target_path": f"{existing_season_path}/{sf_name}",
                            "size": sf.get("size", 0),
                            "is_video": is_video_file(sf_name),
                        })
            else:
                actual_target_season = f"{norm_target_parent}/{season_dir_name}"
                item_plan["target_path"] = actual_target_season
                item_plan["mode"] = "preserve_versions" if preserve_versions else "full_season_move"
                item_plan["mode_label"] = "保留全部版本归档" if preserve_versions else "整季移入"
                for sf in regular_contents:
                    if sf.get("isDirectory"):
                        continue
                    sf_name = sf.get("name", "")
                    if not sf_name:
                        continue
                    item_plan["regular_files_to_move"].append({
                        "name": sf_name,
                        "path": sf.get("path") or f"{source_season_path}/{sf_name}",
                        "target_path": f"{actual_target_season}/{sf_name}",
                        "size": sf.get("size", 0),
                        "is_video": is_video_file(sf_name),
                    })

            if special_files_to_move:
                item_plan["specials_target_path"] = target_s0_path
                for sp in special_files_to_move:
                    sp_name = sp.get("name", "")
                    item_plan["special_files_to_move"].append({
                        "name": sp_name,
                        "path": sp.get("path") or f"{source_season_path}/{sp_name}",
                        "target_path": f"{target_s0_path}/{sp_name}",
                        "size": sp.get("size", 0),
                        "is_video": is_video_file(sp_name),
                    })

            # 检查源剧根目录预期留源与公共资产 (A01, A12)
            if not is_flat_show and norm_src_show:
                try:
                    show_items = await cd2.list_files(norm_src_show, force_refresh=True)
                except Exception:
                    show_items = []
                other_subdirs = [
                    d.get("name", "")
                    for d in show_items
                    if d.get("isDirectory")
                    and d.get("name", "").lower() != season_dir_name.lower()
                ]
                root_files = [f for f in show_items if not f.get("isDirectory") and f.get("name")]
                if preserve_versions:
                    item_plan["will_cleanup_show_root"] = False
                    item_plan["show_root_note"] = "保留版本分支跳过源剧根回收，剧根资产留源"
                elif other_subdirs:
                    item_plan["will_cleanup_show_root"] = False
                    item_plan["show_root_note"] = f"源剧根还有 {', '.join(other_subdirs[:3])}，剧根及公共海报将留源"
                    for rf in root_files:
                        item_plan["expected_retained"].append({
                            "name": rf.get("name", ""),
                            "path": rf.get("path") or f"{norm_src_show}/{rf.get('name', '')}",
                            "scope": "show_root",
                            "reason": f"源剧根仍有其他目录 ({', '.join(other_subdirs[:2])})，公共资料留源",
                        })
                else:
                    known_root = [rf.get("name", "") for rf in root_files if _is_known_show_public_asset(rf.get("name", ""))]
                    unknown_root = [rf for rf in root_files if not _is_known_show_public_asset(rf.get("name", ""))]
                    if unknown_root:
                        item_plan["will_cleanup_show_root"] = False
                        item_plan["show_root_note"] = f"源剧根含未知附件 ({', '.join(u.get('name','') for u in unknown_root[:2])})，将安全留源"
                        for u in unknown_root:
                            item_plan["expected_retained"].append({
                                "name": u.get("name", ""),
                                "path": u.get("path") or f"{norm_src_show}/{u.get('name', '')}",
                                "scope": "show_root",
                                "reason": "未知剧根附件安全留源",
                            })
                    else:
                        item_plan["will_cleanup_show_root"] = True
                        item_plan["public_assets_to_migrate"] = known_root
                        item_plan["show_root_note"] = (
                            f"本季为源剧根最后一季，将同步公共资产 ({', '.join(known_root[:3]) or '无'}) 并回收空剧根"
                        )

            # 生成确认窗摘要说明
            reg_vid_count = sum(1 for x in item_plan["regular_files_to_move"] if x.get("is_video"))
            reg_total_count = len(item_plan["regular_files_to_move"])
            summary_lines = []
            if item_plan["mode"] == "incremental_merge":
                summary_lines.append(
                    f"✓ 【{item_plan['mode_label']}】{reg_vid_count} 个正片视频（共 {reg_total_count} 个文件）将移入已有 {posixpath.basename(item_plan['target_path'])}；目标原有 {len(item_plan['target_existing_files'])} 个文件保留"
                )
            else:
                summary_lines.append(
                    f"✓ 【{item_plan['mode_label']}】{reg_vid_count} 个正片视频（共 {reg_total_count} 个文件）将整季移至 {item_plan['target_path']}"
                )
            if item_plan["special_files_to_move"]:
                sp_vid_count = sum(1 for x in item_plan["special_files_to_move"] if x.get("is_video"))
                summary_lines.append(
                    f"• 先导特辑分流：{sp_vid_count} 部特辑（共 {len(item_plan['special_files_to_move'])} 个文件）将移入 {item_plan['specials_target_path']}"
                )
            if item_plan["expected_retained"]:
                ret_names = ", ".join(x["name"] for x in item_plan["expected_retained"][:3])
                summary_lines.append(
                    f"• 预期留源：{len(item_plan['expected_retained'])} 项保留在源端 ({ret_names})"
                )
            if item_plan["show_root_note"]:
                summary_lines.append(f"• 源剧根处理：{item_plan['show_root_note']}")
            if assoc_dirs:
                summary_lines.append(f"• 关联互补源目录：共 {len(assoc_dirs) + 1} 个源目录共同参与本季归档")

            item_plan["executable"] = True
            item_plan["state"] = "executable"
            item_plan["next_step"] = "confirm_archive"
            item_plan["summary_lines"] = summary_lines
            item_plan["snapshot"] = {
                "source_file_names": sorted(f.get("name", "") for f in contents if not f.get("isDirectory") and f.get("name")),
                "target_season_exists": bool(existing_season),
                "target_file_names": sorted(f.get("name", "") for f in dest_season_files if not f.get("isDirectory") and f.get("name")),
                "candidate_count": len(candidate_seasons),
            }
            executable_ids.append(rec.id)
            items.append(item_plan)

    finally:
        await cd2.close()

    _ARCHIVE_PREVIEW_PLANS[plan_id] = {
        "plan_id": plan_id,
        "task_id": body.task_id or (expanded_records[0].task_id if expanded_records else ""),
        "created_at": datetime.utcnow().isoformat(),
        "items_by_id": {it["season_record_id"]: it for it in items},
    }

    return {
        "ok": len(executable_ids) > 0,
        "message": f"预检完成：{len(executable_ids)} 季可执行，{len(blocked_ids)} 季受阻",
        "plan_id": plan_id,
        "task_id": body.task_id or (expanded_records[0].task_id if expanded_records else ""),
        "total_selected": len(items),
        "executable_count": len(executable_ids),
        "blocked_count": len(blocked_ids),
        "executable_ids": executable_ids,
        "blocked_ids": blocked_ids,
        "missing_ids": missing_ids,
        "items": items,
    }


@router.post("/execute", response_model=ArchiveExecuteResponse)
async def execute_archive(
    body: _ArchiveRequest,
    db: AsyncSession = Depends(get_db),
) -> ArchiveExecuteResponse:
    """执行归档 — 将选中的剧季按季移动到归档目录，具备前置零写入冲突拦截、同季增量合并、先导片分流与逐季结构化回执。"""
    from uuid import uuid4
    from app.core.season_classifier import build_work_key

    if not body.season_record_ids:
        return ArchiveExecuteResponse(ok=False, message="未选择任何项目")

    settings = get_settings()
    if not settings.archive_root:
        return ArchiveExecuteResponse(ok=False, message="未配置归档目录 (MF_ARCHIVE_ROOT)")

    batch_id = body.batch_id or f"batch_{uuid4().hex[:12]}"
    plan_id = body.plan_id or ""
    saved_plan = _ARCHIVE_PREVIEW_PLANS.get(plan_id) if plan_id else None

    stmt = select(SeasonRecord).where(SeasonRecord.id.in_(body.season_record_ids))
    result = await db.execute(stmt)
    fetched = list(result.scalars().all())
    by_id = {r.id: r for r in fetched}
    missing_ids = [rid for rid in body.season_record_ids if rid not in by_id]
    ordered_records = [by_id[rid] for rid in body.season_record_ids if rid in by_id]

    if not ordered_records:
        return ArchiveExecuteResponse(
            ok=False,
            message="未找到匹配的季度记录",
            batch_id=batch_id,
            plan_id=plan_id,
            missing_ids=missing_ids,
        )

    records, _ = await _expand_complementary_records(db, ordered_records)

    norm_source_root = posixpath.normpath(settings.source_root.rstrip("/")) if settings.source_root else ""
    norm_archive_root = posixpath.normpath(settings.archive_root.rstrip("/")) if settings.archive_root else ""

    cd2 = CloudDrive2Client()
    success_count = 0
    errors: list[str] = []
    affected_tasks: set[str] = set()
    season_results: list[dict] = []

    def _make_receipt(
        rec: SeasonRecord,
        outcome: str,
        message: str,
        *,
        mode: str = "full_season_move",
        target_path: str = "",
        moved_files: list[str] | None = None,
        moved_specials: list[str] | None = None,
        specials_target_path: str = "",
        retained_items: list[dict] | None = None,
        unverified_items: list[str] | None = None,
        cleanup_result: dict | None = None,
        next_step: str = "done",
        already_done: bool = False,
    ) -> dict:
        return {
            "season_record_id": rec.id,
            "task_id": rec.task_id,
            "work_key": build_work_key(rec),
            "show_name": rec.show_name,
            "season_number": rec.season_number,
            "outcome": outcome,
            "mode": mode,
            "source_path": rec.season_path,
            "target_path": target_path or (rec.archive_target_path or ""),
            "moved_files": moved_files or [],
            "moved_specials": moved_specials or [],
            "specials_target_path": specials_target_path,
            "retained_items": retained_items or [],
            "unverified_items": unverified_items or [],
            "cleanup_result": cleanup_result or {},
            "message": message,
            "next_step": next_step,
            "already_done": already_done,
        }

    # 按 (norm_src_show, norm_target_parent) 分组，落实同剧串行、跨剧受控并发
    show_groups: dict[tuple[str, str], list[SeasonRecord]] = {}
    for rec in records:
        # 幂等防重：若同一确认计划重复提交或该季已完成归档 (A11)
        if plan_id and (plan_id, rec.id) in _COMPLETED_PLAN_SEASONS:
            prev_receipt = dict(_COMPLETED_PLAN_SEASONS[(plan_id, rec.id)])
            prev_receipt["already_done"] = True
            season_results.append(prev_receipt)
            success_count += 1
            continue
        if rec.status == "archived":
            receipt = _make_receipt(
                rec,
                "archived",
                f"{rec.show_name} S{rec.season_number:02d}: 已完成归档（重复提交幂等返回）",
                target_path=rec.archive_target_path or "",
                already_done=True,
            )
            season_results.append(receipt)
            success_count += 1
            continue

        conflict_info = json.loads(rec.conflict_json or "{}") if getattr(rec, "conflict_json", None) else {}
        if isinstance(conflict_info, dict) and conflict_info.get("blocking"):
            err_msg = (
                f"{rec.show_name} S{rec.season_number:02d}: 存在身份/归属冲突 ({conflict_info.get('reason') or conflict_info.get('type')})，安全拦截归档"
            )
            errors.append(err_msg)
            season_results.append(_make_receipt(rec, "blocked", err_msg, next_step="goto_anomaly"))
            continue

        from app.core.season_classifier import get_record_work_main_category
        work_cat = await get_record_work_main_category(db, rec)
        if work_cat == "airing" or rec.status == "airing":
            err_msg = f"{rec.show_name} S{rec.season_number:02d}: 当前作品季处于「连载留源」状态，安全拦截归档"
            errors.append(err_msg)
            season_results.append(_make_receipt(rec, "blocked", err_msg, next_step="stay_in_airing"))
            continue

        if rec.status not in ("archive_ready", "period_ready"):
            err_msg = f"{rec.show_name} S{rec.season_number:02d}: 当前状态为 {rec.status}，跳过"
            errors.append(err_msg)
            season_results.append(_make_receipt(rec, "blocked", err_msg, next_step="goto_anomaly"))
            continue

        source_season_path = rec.season_path
        norm_src_season = posixpath.normpath(source_season_path)
        norm_src_show = posixpath.normpath(rec.show_path or posixpath.dirname(norm_src_season))

        archive_target = rec.archive_target_path
        if not archive_target:
            if norm_source_root and norm_src_season.startswith(norm_source_root + "/"):
                relative = norm_src_season[len(norm_source_root):]
                archive_target = norm_archive_root + relative
            else:
                archive_target = ""

        if not archive_target:
            err_msg = f"{rec.show_name} S{rec.season_number:02d}: 无法自动计算归档路径"
            errors.append(err_msg)
            season_results.append(_make_receipt(rec, "blocked", err_msg, next_step="goto_anomaly"))
            continue

        norm_archive_target = posixpath.normpath(archive_target)
        norm_target_parent = posixpath.dirname(norm_archive_target)

        # 路径安全红线核验
        path_err = ""
        if norm_source_root and not norm_src_season.startswith(norm_source_root + "/"):
            path_err = f"{rec.show_name} S{rec.season_number:02d}: 源路径不在受控源根目录下"
        elif norm_archive_root and not norm_archive_target.startswith(norm_archive_root + "/"):
            path_err = f"{rec.show_name} S{rec.season_number:02d}: 归档目标不在受控归档根目录下"
        elif norm_src_season == norm_archive_target:
            path_err = f"{rec.show_name} S{rec.season_number:02d}: 源目录与目标目录相同，安全拦截"
        elif norm_archive_target.startswith(norm_src_season + "/") or norm_src_season.startswith(norm_archive_target + "/"):
            path_err = f"{rec.show_name} S{rec.season_number:02d}: 源目录与目标目录互相包含，安全拦截"
        elif norm_src_show == norm_target_parent:
            path_err = f"{rec.show_name}: 源剧集与目标剧集目录相同，安全拦截"

        if path_err:
            errors.append(path_err)
            season_results.append(_make_receipt(rec, "blocked", path_err, next_step="goto_anomaly"))
            continue

        key = (norm_src_show, norm_target_parent)
        show_groups.setdefault(key, []).append(rec)

    try:
        for (norm_src_show, norm_target_parent), show_seasons in show_groups.items():
            # 多目录/异名目录组级安全门禁（含实时漂移/目标失效检测）
            blocked_group = None
            for rec_probe in show_seasons:
                blocked_group = await find_blocking_group_for_record(
                    db, rec_probe, cd2_client=cd2, operation="execute_archive"
                )
                if blocked_group is not None:
                    break
            if blocked_group is not None:
                msg_reason = blocked_group.invalidated_reason or "请先查看目录内容并确认目标归档目录或标记有意共存"
                err_msg = (
                    f"{show_seasons[0].show_name}: 存在未确认的{blocked_group.title or '同 ID 多目录组'}（TMDB {blocked_group.tmdb_id}），已暂停自动归档 — {msg_reason}"
                )
                errors.append(err_msg)
                for r_blk in show_seasons:
                    season_results.append(
                        _make_receipt(r_blk, "blocked", err_msg, next_step="goto_anomaly_dir_group")
                    )
                await db.commit()
                continue

            # 认领规范化的源剧根路径和实际目标剧根路径锁
            locks = await _acquire_show_locks([norm_src_show, norm_target_parent])
            target_parent_files_cache: list[dict] | None = None
            has_successful_regular_archive = False
            is_flat_show = False
            show_archived_receipts: list[dict] = []

            try:
                for rec in show_seasons:
                    # 锁内二次检查状态（防并发重复执行）
                    if rec.status == "archived":
                        receipt = _make_receipt(
                            rec,
                            "archived",
                            f"{rec.show_name} S{rec.season_number:02d}: 已完成归档（并发幂等返回）",
                            target_path=rec.archive_target_path or "",
                            already_done=True,
                        )
                        season_results.append(receipt)
                        success_count += 1
                        continue

                    source_season_path = rec.season_path
                    norm_src_season = posixpath.normpath(source_season_path)
                    is_flat_show = (norm_src_show == norm_src_season)

                    archive_target = rec.archive_target_path
                    if not archive_target:
                        relative = norm_src_season[len(norm_source_root):]
                        archive_target = norm_archive_root + relative
                    norm_archive_target = posixpath.normpath(archive_target)
                    target_parent = norm_target_parent
                    season_dir_name = posixpath.basename(norm_src_season)

                    try:
                        contents = await cd2.list_files(source_season_path, force_refresh=True)
                    except Exception as list_src_err:
                        err_msg = f"{rec.show_name} S{rec.season_number:02d}: 列出源季目录失败 ({list_src_err})"
                        errors.append(err_msg)
                        season_results.append(_make_receipt(rec, "blocked", err_msg, next_step="retry_preview"))
                        continue

                    preserve_versions = rec.status == "period_ready"
                    if preserve_versions:
                        from app.clients.moviepilot import MoviePilotClient
                        from app.core.inspector import run_inspection
                        mp = MoviePilotClient()
                        try:
                            verified = await run_inspection(rec.task_id, rec.show_path, cd2, mp,
                                                            archive_root=settings.archive_root,
                                                            global_source_root=settings.source_root)
                            if not any(s.season_path == source_season_path and s.status == "period_ready"
                                       and s.archive_target_path == rec.archive_target_path for s in verified.seasons):
                                err_msg = f"{rec.show_name}：全季分段或归档位置已变化，请重新初检"
                                errors.append(err_msg)
                                season_results.append(_make_receipt(rec, "blocked", err_msg, mode="preserve_versions", next_step="recheck"))
                                continue
                        finally:
                            await mp.close()

                    if not preserve_versions and any(segment_number(f.get("name", "")) is not None for f in contents):
                        err_msg = f"{rec.show_name}: 请先核对上下段和整期版"
                        errors.append(err_msg)
                        season_results.append(_make_receipt(rec, "blocked", err_msg, next_step="goto_anomaly"))
                        continue

                    try:
                        # 确保目标父目录存在并初始化/复用快照
                        if target_parent_files_cache is None:
                            await cd2.mkdir(target_parent)
                            try:
                                target_parent_files_cache = await cd2.list_files(target_parent, force_refresh=True)
                            except Exception as list_err:
                                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 读取目标目录失败 ({list_err})，安全拦截归档"
                                log.error(err_msg)
                                errors.append(err_msg)
                                season_results.append(_make_receipt(rec, "blocked", err_msg, next_step="retry_preview"))
                                continue

                        # 1. 只读识别先导特辑与常规内容（严禁在冲突检查前写入！）
                        spec_info = _identify_specials_in_season(
                            contents, target_parent, target_parent_files_cache or []
                        )
                        regular_contents = spec_info["regular_contents"]
                        special_files_to_move = spec_info["special_files_to_move"]
                        target_s0_path = spec_info["target_s0_path"]
                        candidate_s0_dirs = spec_info["candidate_s0_dirs"]

                        # 2. 跨季与平铺未隔离检查 (A07)
                        cross_items = _find_cross_season_or_unisolated_items(
                            regular_contents, rec.season_number, is_flat_show
                        )
                        if cross_items:
                            err_msg = f"{rec.show_name} S{rec.season_number:02d}: 源目录存在跨季或未隔离内容 ({', '.join(cross_items[:3])})，安全拦截"
                            log.warning(err_msg)
                            errors.append(err_msg)
                            rec.status = "review_needed"
                            rec.status_detail = err_msg
                            affected_tasks.add(rec.task_id)
                            season_results.append(_make_receipt(rec, "blocked", err_msg, next_step="goto_anomaly"))
                            continue

                        # 3. 【P0: 目标季匹配多个候选时停止，展示候选路径，此时 E00 尚未移动 (A05)】
                        candidate_seasons = [
                            f for f in target_parent_files_cache if f.get("isDirectory") and (
                                _parse_season_number(f.get("name", "")) == rec.season_number or
                                f.get("name", "").lower() == season_dir_name.lower()
                            )
                        ]
                        if len(candidate_seasons) > 1:
                            candidate_paths = [f.get("path") or f"{target_parent}/{f.get('name')}" for f in candidate_seasons]
                            err_msg = f"{rec.show_name} S{rec.season_number:02d}: 目标匹配到多个候选季目录 ({', '.join(candidate_paths)})，安全停止"
                            log.warning(err_msg)
                            errors.append(err_msg)
                            rec.status = "review_needed"
                            rec.status_detail = err_msg
                            affected_tasks.add(rec.task_id)
                            season_results.append(_make_receipt(rec, "blocked", err_msg, next_step="goto_anomaly"))
                            continue

                        existing_season = candidate_seasons[0] if candidate_seasons else None
                        if existing_season and preserve_versions:
                            err_msg = f"{rec.show_name}：目标季已存在，保留版本归档已停止"
                            errors.append(err_msg)
                            season_results.append(_make_receipt(rec, "blocked", err_msg, mode="preserve_versions", next_step="recheck"))
                            continue

                        # 4. 若绑定了预检计划，校验实物快照是否发生外部漂移 (A04, A11)
                        if saved_plan and rec.id in saved_plan.get("items_by_id", {}):
                            plan_snap = saved_plan["items_by_id"][rec.id].get("snapshot") or {}
                            curr_src_names = sorted(
                                f.get("name", "") for f in contents if not f.get("isDirectory") and f.get("name")
                            )
                            if plan_snap.get("source_file_names") and curr_src_names != plan_snap.get("source_file_names"):
                                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 预检后源季目录文件已变化，请重新预检"
                                errors.append(err_msg)
                                season_results.append(_make_receipt(rec, "blocked", err_msg, next_step="retry_preview"))
                                continue
                            if plan_snap.get("target_season_exists") is False and existing_season is not None:
                                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 预检后目标库新出现同季目录，请重新预检"
                                errors.append(err_msg)
                                season_results.append(_make_receipt(rec, "blocked", err_msg, next_step="retry_preview"))
                                continue

                        # 5. 若存在已有目标季，先读取目标季并完成同名视频与同集异名冲突检查（在移动 E00 之前！A05, A07）
                        dest_season_files: list[dict] = []
                        existing_season_path = ""
                        if existing_season:
                            existing_season_path = existing_season.get("path") or f"{target_parent}/{existing_season.get('name')}"
                            try:
                                dest_season_files = await cd2.list_files(existing_season_path, force_refresh=True)
                            except Exception as dest_read_err:
                                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 读取目标同季目录失败 ({dest_read_err})"
                                errors.append(err_msg)
                                season_results.append(_make_receipt(rec, "blocked", err_msg, mode="incremental_merge", next_step="retry_preview"))
                                continue

                            duplicate_videos, same_ep_conflicts = _find_same_episode_conflicts(
                                regular_contents, dest_season_files, rec.season_number
                            )
                            if duplicate_videos:
                                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 同季冲突，归档库已存在同名视频文件 ({duplicate_videos[0]})，已安全拦截"
                                log.warning(err_msg)
                                errors.append(err_msg)
                                rec.status = "conflict"
                                rec.status_detail = f"同季文件冲突：目标目录已存在同名视频 {duplicate_videos[0]}，整季暂缓合并"
                                affected_tasks.add(rec.task_id)
                                season_results.append(_make_receipt(rec, "blocked", err_msg, mode="incremental_merge", target_path=existing_season_path, next_step="goto_anomaly"))
                                continue

                            if same_ep_conflicts:
                                c0 = same_ep_conflicts[0]
                                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 同集异名冲突，目标已存在第 {c0['episodes']} 集的不同文件 ({c0['target_file']})，已安全拦截"
                                log.warning(err_msg)
                                errors.append(err_msg)
                                rec.status = "conflict"
                                rec.status_detail = err_msg
                                affected_tasks.add(rec.task_id)
                                season_results.append(_make_receipt(rec, "blocked", err_msg, mode="incremental_merge", target_path=existing_season_path, next_step="goto_anomaly"))
                                continue

                        # 6. 若含先导特辑，先完成特别篇目录歧义与同名冲突检查 (A05)
                        if special_files_to_move:
                            if len(candidate_s0_dirs) > 1:
                                s0_paths = [d.get("path") or f"{target_parent}/{d.get('name')}" for d in candidate_s0_dirs]
                                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 目标存在多个特别篇候选目录 ({', '.join(s0_paths)})，安全停止"
                                errors.append(err_msg)
                                rec.status = "review_needed"
                                rec.status_detail = err_msg
                                affected_tasks.add(rec.task_id)
                                season_results.append(_make_receipt(rec, "blocked", err_msg, next_step="goto_anomaly"))
                                continue
                            if len(candidate_s0_dirs) == 1:
                                try:
                                    s0_existing = await cd2.list_files(target_s0_path, force_refresh=True)
                                except Exception:
                                    s0_existing = []
                                s0_vnames = {
                                    f.get("name", "").lower()
                                    for f in s0_existing
                                    if not f.get("isDirectory") and is_video_file(f.get("name", ""))
                                }
                                dup_s0 = [
                                    sv.get("name", "")
                                    for sv in spec_info["special_videos"]
                                    if sv.get("name", "").lower() in s0_vnames
                                ]
                                if dup_s0:
                                    err_msg = f"{rec.show_name} S{rec.season_number:02d}: 目标特别篇目录已存在同名视频 ({dup_s0[0]})，安全停止"
                                    errors.append(err_msg)
                                    rec.status = "conflict"
                                    rec.status_detail = err_msg
                                    affected_tasks.add(rec.task_id)
                                    season_results.append(_make_receipt(rec, "blocked", err_msg, next_step="goto_anomaly"))
                                    continue

                        # 7. 所有前置门禁 100% 通过后，开始执行写入：先智能分流先导特辑至 Season 0
                        moved_specials: list[str] = []
                        if special_files_to_move:
                            contents, moved_specials = await _migrate_specials_to_season_0(
                                cd2=cd2,
                                contents=contents,
                                target_parent=target_parent,
                                target_parent_files=target_parent_files_cache or [],
                                season_number=rec.season_number,
                            )
                            if moved_specials:
                                log.info("%s S%02d: 成功智能分流 %d 个先导特辑/伴生文件至 Season 0", rec.show_name, rec.season_number, len(moved_specials))

                        # 8A. 分支一：目标同季已存在 -> 季内文件级增量合并通道 (F08)
                        if existing_season:
                            log.info("发现目标归档库已存在同季目录: %s，启动季内文件级增量合并通道 (F08)", existing_season_path)
                            try:
                                src_season_files = contents
                                dest_file_names = {f.get("name", "").lower() for f in dest_season_files if not f.get("isDirectory")}

                                planned_merge_names = [
                                    sf.get("name")
                                    for sf in src_season_files
                                    if not sf.get("isDirectory")
                                    and sf.get("name")
                                    and sf.get("name", "").lower() not in dest_file_names
                                ]
                                expected_retained_season = [
                                    {
                                        "name": sf.get("name", ""),
                                        "path": sf.get("path") or f"{source_season_path}/{sf.get('name', '')}",
                                        "scope": "season",
                                        "reason": "目标季已存在同名非视频伴生文件，双端保留",
                                    }
                                    for sf in src_season_files
                                    if not sf.get("isDirectory")
                                    and sf.get("name")
                                    and sf.get("name", "").lower() in dest_file_names
                                ]

                                # 首个写入前落盘计划日志 (A09)
                                op_log = OperationLog(
                                    task_id=rec.task_id,
                                    season_record_id=rec.id,
                                    action="archive_incremental_merge",
                                    status="in_progress",
                                    detail_json=json.dumps({
                                        "batch_id": batch_id,
                                        "plan_id": plan_id,
                                        "show": rec.show_name,
                                        "season": rec.season_number,
                                        "mode": "incremental_merge",
                                        "source": source_season_path,
                                        "target": existing_season_path,
                                        "planned_files": planned_merge_names,
                                        "moved_specials": moved_specials,
                                    }, ensure_ascii=False),
                                )
                                db.add(op_log)

                                merged_files: list[str] = []
                                for sf in src_season_files:
                                    if sf.get("isDirectory"):
                                        continue
                                    sf_name = sf.get("name")
                                    if not sf_name:
                                        continue
                                    if sf_name.lower() in dest_file_names:
                                        log.info("目标库已存在同名非视频文件 %s，保留两端，源端暂留存", sf_name)
                                        continue

                                    mv_res = await cd2.move_file(sf.get("path"), existing_season_path, skip_existing=True)
                                    if mv_res.get("success", True):
                                        merged_files.append(sf_name)
                                    else:
                                        log.warning("增量迁移文件 %s 失败: %s", sf_name, mv_res.get("errorMessage"))

                                # 检查源端剩余文件：P0 核心防线：显式区分“读取成功且为空”和“读取失败”
                                list_src_failed = False
                                rem_in_src = []
                                try:
                                    rem_in_src = await cd2.list_files(source_season_path, force_refresh=True)
                                except Exception as rem_err:
                                    list_src_failed = True
                                    log.warning("增量合并后读取源季目录失败: %s - %s", source_season_path, rem_err)

                                if list_src_failed:
                                    err_msg = f"{rec.show_name} S{rec.season_number:02d}: 增量合并已移动 {len(merged_files)} 个文件，但末次读取源季失败，已保留源季待核实"
                                    log.warning(err_msg)
                                    errors.append(err_msg)
                                    rec.status = "review_needed"
                                    rec.status_detail = err_msg
                                    op_log.status = "archive_unverified"
                                    op_log.detail_json = json.dumps({
                                        "batch_id": batch_id,
                                        "plan_id": plan_id,
                                        "show": rec.show_name,
                                        "season": rec.season_number,
                                        "mode": "incremental_merge",
                                        "source": source_season_path,
                                        "target": existing_season_path,
                                        "planned_files": planned_merge_names,
                                        "merged_files": merged_files,
                                        "outcome": "archive_unverified",
                                    }, ensure_ascii=False)
                                    affected_tasks.add(rec.task_id)
                                    season_results.append(
                                        _make_receipt(
                                            rec,
                                            "archive_unverified",
                                            err_msg,
                                            mode="incremental_merge",
                                            target_path=existing_season_path,
                                            moved_files=merged_files,
                                            moved_specials=moved_specials,
                                            specials_target_path=target_s0_path if moved_specials else "",
                                            unverified_items=planned_merge_names,
                                            next_step="verify_both_sides",
                                        )
                                    )
                                    continue

                                src_valid_files = [f for f in src_season_files if not f.get("isDirectory")]
                                if not merged_files and src_valid_files and not expected_retained_season:
                                    err_msg = f"{rec.show_name} S{rec.season_number:02d}: 增量合并失败，未能成功移动任何文件"
                                    log.error(err_msg)
                                    errors.append(err_msg)
                                    rec.status = "failed"
                                    rec.status_detail = err_msg
                                    op_log.status = "failed"
                                    affected_tasks.add(rec.task_id)
                                    season_results.append(
                                        _make_receipt(rec, "blocked", err_msg, mode="incremental_merge", target_path=existing_season_path, next_step="goto_anomaly")
                                    )
                                    continue

                                rem_videos = [
                                    f.get("name", "")
                                    for f in rem_in_src
                                    if not f.get("isDirectory") and is_video_file(f.get("name", ""))
                                ]
                                if rem_videos:
                                    err_msg = f"{rec.show_name} S{rec.season_number:02d}: 增量合并部分成功，已移动 {len(merged_files)} 个文件，源端仍残留 {len(rem_videos)} 个视频未移出"
                                    log.warning(err_msg)
                                    errors.append(err_msg)
                                    rec.status = "review_needed"
                                    rec.status_detail = err_msg
                                    op_log.status = "partial_archived"
                                    op_log.detail_json = json.dumps({
                                        "batch_id": batch_id,
                                        "plan_id": plan_id,
                                        "show": rec.show_name,
                                        "season": rec.season_number,
                                        "mode": "incremental_merge",
                                        "source": source_season_path,
                                        "target": existing_season_path,
                                        "planned_files": planned_merge_names,
                                        "merged_files": merged_files,
                                        "remaining_videos": rem_videos,
                                        "outcome": "partial_archived",
                                    }, ensure_ascii=False)
                                    affected_tasks.add(rec.task_id)
                                    season_results.append(
                                        _make_receipt(
                                            rec,
                                            "partial_archived",
                                            err_msg,
                                            mode="incremental_merge",
                                            target_path=existing_season_path,
                                            moved_files=merged_files,
                                            moved_specials=moved_specials,
                                            specials_target_path=target_s0_path if moved_specials else "",
                                            unverified_items=rem_videos,
                                            next_step="recheck_remaining",
                                        )
                                    )
                                    continue

                                # 正片视频已全部移出源端：处理源季目录收尾 (A02, A12)
                                season_cleanup_ok = False
                                outcome_code = "archived"
                                retained_season_list = list(expected_retained_season)

                                if rem_in_src:
                                    rem_names = [f.get("name", "") for f in rem_in_src if f.get("name")]
                                    expected_names_lower = {x["name"].lower() for x in expected_retained_season}
                                    unexpected_rem = [n for n in rem_names if n.lower() not in expected_names_lower]
                                    if unexpected_rem:
                                        outcome_code = "archived_pending_cleanup"
                                        for un in unexpected_rem:
                                            retained_season_list.append({
                                                "name": un,
                                                "path": f"{source_season_path}/{un}",
                                                "scope": "season",
                                                "reason": "源季残留未知非视频附件，安全保留待查看",
                                            })
                                        detail_msg = (
                                            f"已增量合并 {len(merged_files)} 个文件至 {existing_season_path}；源端保留 {len(rem_names)} 个附件待查看 ({', '.join(rem_names[:3])})"
                                        )
                                    else:
                                        detail_msg = (
                                            f"已成功增量合并 {len(merged_files)} 个文件至 {existing_season_path}（同名伴生文件 {', '.join(rem_names[:2])} 双端保留）"
                                        )
                                else:
                                    try:
                                        await cd2.delete_file(source_season_path)
                                        season_cleanup_ok = True
                                        log.info("源端季目录已全部增量合并，已安全清理源季空壳: %s", source_season_path)
                                        detail_msg = f"已成功增量合并 {len(merged_files)} 个文件至 {existing_season_path}"
                                    except Exception as del_season_err:
                                        log.warning("清理已合并源季目录失败: %s", del_season_err)
                                        outcome_code = "archived_pending_cleanup"
                                        detail_msg = f"媒体已增量合并至 {existing_season_path}，但源季空壳目录回收待处理 ({del_season_err})"

                                success_count += 1
                                rec.status = "archived"
                                rec.status_detail = detail_msg
                                rec.archive_target_path = existing_season_path
                                affected_tasks.add(rec.task_id)
                                if not rem_in_src:
                                    has_successful_regular_archive = True

                                op_log.status = "completed"
                                op_log.detail_json = json.dumps({
                                    "batch_id": batch_id,
                                    "plan_id": plan_id,
                                    "show": rec.show_name,
                                    "season": rec.season_number,
                                    "mode": "incremental_merge",
                                    "source": source_season_path,
                                    "target": existing_season_path,
                                    "planned_files": planned_merge_names,
                                    "merged_files": merged_files,
                                    "moved_specials": moved_specials,
                                    "merged_files_count": len(merged_files),
                                    "retained_items": retained_season_list,
                                    "outcome": outcome_code,
                                }, ensure_ascii=False)

                                receipt = _make_receipt(
                                    rec,
                                    outcome_code,
                                    detail_msg,
                                    mode="incremental_merge",
                                    target_path=existing_season_path,
                                    moved_files=merged_files,
                                    moved_specials=moved_specials,
                                    specials_target_path=target_s0_path if moved_specials else "",
                                    retained_items=retained_season_list,
                                    cleanup_result={"season_dir_cleaned": season_cleanup_ok},
                                    next_step="check_retained" if outcome_code == "archived_pending_cleanup" else "done",
                                )
                                season_results.append(receipt)
                                show_archived_receipts.append(receipt)
                                if plan_id:
                                    _COMPLETED_PLAN_SEASONS[(plan_id, rec.id)] = receipt
                                continue

                            except Exception as merge_err:
                                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 增量合并失败 - {merge_err}"
                                log.error(err_msg)
                                errors.append(err_msg)
                                season_results.append(_make_receipt(rec, "blocked", err_msg, mode="incremental_merge", next_step="retry_preview"))
                                continue

                        # 8B. 分支二：普通整季移动 / 保留版本归档 (P0: 统一 skip_existing=True)
                        planned_regular_names = [
                            f.get("name", "") for f in contents if not f.get("isDirectory") and f.get("name")
                        ]
                        actual_target_season = f"{target_parent}/{season_dir_name}"
                        mode_str = "preserve_versions" if preserve_versions else "full_season_move"

                        op_log = OperationLog(
                            task_id=rec.task_id,
                            season_record_id=rec.id,
                            action="archive",
                            status="in_progress",
                            detail_json=json.dumps({
                                "batch_id": batch_id,
                                "plan_id": plan_id,
                                "show": rec.show_name,
                                "season": rec.season_number,
                                "mode": mode_str,
                                "source": source_season_path,
                                "target": actual_target_season,
                                "planned_files": planned_regular_names,
                                "moved_specials": moved_specials,
                            }, ensure_ascii=False),
                        )
                        db.add(op_log)

                        log.info("执行归档迁移: %s -> %s", source_season_path, target_parent)
                        move_result = await cd2.move_file(source_season_path, target_parent, skip_existing=True)

                        if not move_result.get("success", True):
                            err_msg = move_result.get("errorMessage", "未知错误")
                            full_err = f"{rec.show_name} S{rec.season_number:02d}: {err_msg}"
                            errors.append(full_err)
                            op_log.status = "failed"
                            season_results.append(_make_receipt(rec, "blocked", full_err, mode=mode_str, next_step="retry_preview"))
                            continue

                        # P0/P1: 实物核验源端移出与目标到位，显式区分读取失败与成功 (A08)
                        verify_src_failed = False
                        src_season_still_there = False
                        try:
                            src_parent_files = await cd2.list_files(norm_src_show, force_refresh=True)
                            src_season_still_there = any(
                                f.get("path") == source_season_path or f.get("name", "").lower() == season_dir_name.lower()
                                for f in src_parent_files
                            )
                        except Exception as verify_err:
                            log.warning("核验源季移出状态失败: %s", verify_err)
                            verify_src_failed = True

                        if verify_src_failed:
                            err_msg = f"{rec.show_name} S{rec.season_number:02d}: 整季移动回执已返回，但末次读取源剧目录失败，结果待核实"
                            errors.append(err_msg)
                            rec.status = "review_needed"
                            rec.status_detail = err_msg
                            op_log.status = "archive_unverified"
                            affected_tasks.add(rec.task_id)
                            season_results.append(
                                _make_receipt(
                                    rec,
                                    "archive_unverified",
                                    err_msg,
                                    mode=mode_str,
                                    target_path=actual_target_season,
                                    unverified_items=planned_regular_names,
                                    next_step="verify_both_sides",
                                )
                            )
                            continue

                        if src_season_still_there:
                            err_msg = f"{rec.show_name}：源季仍存在，网盘尚未确认归档完成"
                            errors.append(err_msg)
                            rec.status = "review_needed"
                            rec.status_detail = "归档移动未完全生效，源季仍存，待核查两端"
                            op_log.status = "archive_unverified"
                            affected_tasks.add(rec.task_id)
                            season_results.append(
                                _make_receipt(
                                    rec,
                                    "archive_unverified",
                                    err_msg,
                                    mode=mode_str,
                                    target_path=actual_target_season,
                                    unverified_items=planned_regular_names,
                                    next_step="verify_both_sides",
                                )
                            )
                            continue

                        # 当源季有计划文件时，核对目标季文件是否完整到位 (A08)
                        if planned_regular_names:
                            target_verify_failed = False
                            target_season_files = []
                            try:
                                target_season_files = await cd2.list_files(actual_target_season, force_refresh=True)
                            except Exception as tgt_err:
                                log.warning("核验目标季目录内容失败: %s", tgt_err)
                                target_verify_failed = True

                            if target_verify_failed:
                                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 归档后读取目标季目录失败，结果待核实"
                                errors.append(err_msg)
                                rec.status = "review_needed"
                                rec.status_detail = err_msg
                                op_log.status = "archive_unverified"
                                affected_tasks.add(rec.task_id)
                                season_results.append(
                                    _make_receipt(
                                        rec,
                                        "archive_unverified",
                                        err_msg,
                                        mode=mode_str,
                                        target_path=actual_target_season,
                                        unverified_items=planned_regular_names,
                                        next_step="verify_both_sides",
                                    )
                                )
                                continue

                            if target_season_files or hasattr(cd2, "entries"):
                                tgt_names_lower = {
                                    f.get("name", "").lower() for f in target_season_files if not f.get("isDirectory")
                                }
                                missing_in_tgt = [
                                    fn for fn in planned_regular_names if fn.lower() not in tgt_names_lower
                                ]
                                if missing_in_tgt:
                                    err_msg = (
                                        f"{rec.show_name} S{rec.season_number:02d}: 目标季缺少计划文件 ({', '.join(missing_in_tgt[:3])})，待核对两端"
                                    )
                                    errors.append(err_msg)
                                    rec.status = "review_needed"
                                    rec.status_detail = err_msg
                                    op_log.status = "partial_archived"
                                    affected_tasks.add(rec.task_id)
                                    season_results.append(
                                        _make_receipt(
                                            rec,
                                            "partial_archived",
                                            err_msg,
                                            mode=mode_str,
                                            target_path=actual_target_season,
                                            moved_files=[fn for fn in planned_regular_names if fn.lower() in tgt_names_lower],
                                            moved_specials=moved_specials,
                                            specials_target_path=target_s0_path if moved_specials else "",
                                            unverified_items=missing_in_tgt,
                                            next_step="recheck_remaining",
                                        )
                                    )
                                    continue

                        success_count += 1
                        rec.status = "archived"
                        rec.status_detail = f"已成功归档至 {actual_target_season}"
                        rec.archive_target_path = actual_target_season
                        affected_tasks.add(rec.task_id)
                        if not preserve_versions:
                            has_successful_regular_archive = True

                        op_log.status = "completed"
                        op_log.detail_json = json.dumps({
                            "batch_id": batch_id,
                            "plan_id": plan_id,
                            "show": rec.show_name,
                            "season": rec.season_number,
                            "mode": mode_str,
                            "source": source_season_path,
                            "target": rec.archive_target_path,
                            "planned_files": planned_regular_names,
                            "moved_files": planned_regular_names,
                            "moved_specials": moved_specials,
                            "outcome": "archived",
                        }, ensure_ascii=False)

                        # 更新目标父目录快照（支持同剧多源互补目录第二目录自动走增量合并！）
                        if target_parent_files_cache is not None:
                            target_parent_files_cache.append({
                                "name": season_dir_name,
                                "path": actual_target_season,
                                "isDirectory": True,
                            })

                        receipt = _make_receipt(
                            rec,
                            "archived",
                            rec.status_detail,
                            mode=mode_str,
                            target_path=actual_target_season,
                            moved_files=planned_regular_names,
                            moved_specials=moved_specials,
                            specials_target_path=target_s0_path if moved_specials else "",
                            next_step="done",
                        )
                        season_results.append(receipt)
                        show_archived_receipts.append(receipt)
                        if plan_id:
                            _COMPLETED_PLAN_SEASONS[(plan_id, rec.id)] = receipt

                    except Exception as e:
                        err_msg = f"{rec.show_name} S{rec.season_number:02d}: {str(e)[:200]}"
                        errors.append(err_msg)
                        log.error("归档 %s 失败: %s", source_season_path, e)
                        season_results.append(_make_receipt(rec, "blocked", err_msg, next_step="retry_preview"))

                # 同一部剧处理完毕后：合并执行一次源剧集根空壳清理并记录收尾结果 (A01, A12)
                if has_successful_regular_archive and not is_flat_show and norm_src_show:
                    cleanup_info = await _inspect_and_cleanup_source_show_dir(
                        cd2, norm_src_show, norm_target_parent
                    )
                    if not cleanup_info.get("cleaned"):
                        log.info("源剧集根目录安全保留: %s (%s)", norm_src_show, cleanup_info.get("reason"))
                    for r_item in show_archived_receipts:
                        r_item["cleanup_result"] = {
                            **r_item.get("cleanup_result", {}),
                            "show_root_cleaned": bool(cleanup_info.get("cleaned")),
                            "show_root_status": cleanup_info.get("status", ""),
                            "reason": cleanup_info.get("reason", ""),
                            "retained_root_items": cleanup_info.get("retained_items", []),
                            "migrated_assets": cleanup_info.get("migrated_assets", []),
                        }
                        for ret_name in cleanup_info.get("retained_items", []):
                            r_item["retained_items"].append({
                                "name": ret_name,
                                "path": f"{norm_src_show}/{ret_name}",
                                "scope": "show_root",
                                "reason": cleanup_info.get("reason", ""),
                            })
                        if cleanup_info.get("status") in ("cleanup_failed", "retained_unknown_attachments"):
                            r_item["outcome"] = "archived_pending_cleanup"
                            r_item["next_step"] = "check_retained"
                            r_item["message"] = f"{r_item['message']}（{cleanup_info.get('reason')}）"

            finally:
                _release_show_locks(locks)

        # 刷新关联任务的 summary
        for tid in affected_tasks:
            await _refresh_task_summary(tid, db)

        await db.commit()

    finally:
        await cd2.close()

    summary_counts = {
        "total": len(season_results),
        "archived": sum(1 for r in season_results if r["outcome"] == "archived"),
        "archived_pending_cleanup": sum(1 for r in season_results if r["outcome"] == "archived_pending_cleanup"),
        "partial_archived": sum(1 for r in season_results if r["outcome"] == "partial_archived"),
        "archive_unverified": sum(1 for r in season_results if r["outcome"] == "archive_unverified"),
        "blocked": sum(1 for r in season_results if r["outcome"] == "blocked"),
    }

    for r in season_results:
        tid = r.get("task_id")
        if tid:
            prev_batch = _LAST_ARCHIVE_BATCH_BY_TASK.get(tid)
            if prev_batch and prev_batch.get("batch_id") == batch_id:
                existing_map = {x["season_record_id"]: x for x in prev_batch.get("results", [])}
                existing_map[r["season_record_id"]] = r
                prev_batch["results"] = list(existing_map.values())
                prev_batch["updated_at"] = datetime.utcnow().isoformat()
            else:
                _LAST_ARCHIVE_BATCH_BY_TASK[tid] = {
                    "batch_id": batch_id,
                    "plan_id": plan_id,
                    "task_id": tid,
                    "updated_at": datetime.utcnow().isoformat(),
                    "results": [x for x in season_results if x.get("task_id") == tid],
                }

    msg = f"归档完成：{success_count}/{len(records)} 季成功移动"
    if errors:
        msg += f"，遇到 {len(errors)} 项提醒: {'; '.join(errors[:2])}"

    return ArchiveExecuteResponse(
        ok=success_count > 0,
        message=msg,
        batch_id=batch_id,
        plan_id=plan_id,
        results=season_results,
        missing_ids=missing_ids,
        summary=summary_counts,
    )


@router.get("/last-batch/{task_id}")
async def get_last_archive_batch(
    task_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """获取指定任务最近一次归档批次的结构化结果（支持刷新或断线后恢复查看 A09, A10）。"""
    cached = _LAST_ARCHIVE_BATCH_BY_TASK.get(task_id)
    if cached:
        return {"ok": True, "has_batch": True, **cached}

    stmt = (
        select(OperationLog)
        .where(
            OperationLog.task_id == task_id,
            OperationLog.action.in_(("archive", "archive_incremental_merge")),
        )
        .order_by(OperationLog.id.desc())
        .limit(50)
    )
    logs = list((await db.execute(stmt)).scalars().all())
    if not logs:
        return {"ok": True, "has_batch": False, "task_id": task_id, "results": []}

    latest_detail = {}
    try:
        latest_detail = json.loads(logs[0].detail_json or "{}")
    except Exception:
        latest_detail = {}
    target_batch_id = latest_detail.get("batch_id", "")

    results = []
    for lg in logs:
        try:
            d = json.loads(lg.detail_json or "{}")
        except Exception:
            continue
        if target_batch_id and d.get("batch_id") != target_batch_id:
            continue
        results.append({
            "season_record_id": lg.season_record_id,
            "task_id": lg.task_id,
            "show_name": d.get("show", ""),
            "season_number": d.get("season", 1),
            "outcome": d.get("outcome") or ("archived" if lg.status == "completed" else lg.status),
            "mode": d.get("mode", "full_season_move"),
            "source_path": d.get("source", ""),
            "target_path": d.get("target", ""),
            "moved_files": d.get("merged_files") or d.get("moved_files") or [],
            "planned_files": d.get("planned_files") or [],
            "retained_items": d.get("retained_items") or [],
        })

    return {
        "ok": True,
        "has_batch": bool(results),
        "batch_id": target_batch_id,
        "task_id": task_id,
        "results": results,
    }


@router.post("/reconcile")
async def reconcile_archive_season(
    body: ArchiveReconcileRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """核对两端 / 核对剩余项 — 对部分归档或结果待核实的剧季进行双端实物对账与断点续传 (A08, A09, A12)。"""
    rec = await db.get(SeasonRecord, body.season_record_id)
    if not rec:
        raise HTTPException(status_code=404, detail="季度记录不存在")

    stmt = (
        select(OperationLog)
        .where(
            OperationLog.season_record_id == rec.id,
            OperationLog.action.in_(("archive", "archive_incremental_merge")),
        )
        .order_by(OperationLog.id.desc())
        .limit(1)
    )
    last_log = (await db.execute(stmt)).scalars().first()
    log_detail = {}
    if last_log and last_log.detail_json:
        try:
            log_detail = json.loads(last_log.detail_json)
        except Exception:
            log_detail = {}

    settings = get_settings()
    norm_source_root = posixpath.normpath(settings.source_root.rstrip("/")) if settings.source_root else ""
    norm_archive_root = posixpath.normpath(settings.archive_root.rstrip("/")) if settings.archive_root else ""

    source_season_path = rec.season_path
    norm_src_season = posixpath.normpath(source_season_path)
    norm_src_show = posixpath.normpath(rec.show_path or posixpath.dirname(norm_src_season))
    target_season_path = log_detail.get("target") or rec.archive_target_path
    if not target_season_path and norm_source_root and norm_src_season.startswith(norm_source_root + "/"):
        target_season_path = norm_archive_root + norm_src_season[len(norm_source_root):]
    norm_target_season = posixpath.normpath(target_season_path) if target_season_path else ""
    norm_target_parent = posixpath.dirname(norm_target_season) if norm_target_season else ""

    planned_files: list[str] = list(log_detail.get("planned_files") or [])

    cd2 = CloudDrive2Client()
    locks = await _acquire_show_locks([norm_src_show, norm_target_parent])
    try:
        try:
            src_files = await cd2.list_files(source_season_path, force_refresh=True)
            src_exists = True
        except Exception:
            src_files = []
            src_exists = False

        try:
            tgt_files = await cd2.list_files(norm_target_season, force_refresh=True) if norm_target_season else []
        except Exception as tgt_err:
            return {
                "ok": False,
                "outcome": "archive_unverified",
                "message": f"读取目标季目录失败 ({tgt_err})，仍待核实",
            }

        src_names_map = {f.get("name", "").lower(): f for f in src_files if not f.get("isDirectory") and f.get("name")}
        tgt_names_set = {f.get("name", "").lower() for f in tgt_files if not f.get("isDirectory") and f.get("name")}

        if not planned_files:
            planned_files = [
                f.get("name", "")
                for f in (tgt_files + src_files)
                if not f.get("isDirectory") and f.get("name")
            ]
            planned_files = list(dict.fromkeys(planned_files))

        arrived_files = [fn for fn in planned_files if fn.lower() in tgt_names_set]
        remaining_in_source = [
            f.get("name", "")
            for f in src_files
            if not f.get("isDirectory")
            and f.get("name")
            and f.get("name", "").lower() not in tgt_names_set
        ]

        newly_moved: list[str] = []
        if body.resume_remaining and remaining_in_source and norm_target_season:
            for rem_name in list(remaining_in_source):
                sf = src_names_map.get(rem_name.lower())
                if not sf:
                    continue
                mv_res = await cd2.move_file(
                    sf.get("path") or f"{source_season_path}/{rem_name}",
                    norm_target_season,
                    skip_existing=True,
                )
                if mv_res.get("success", True):
                    newly_moved.append(rem_name)
                    arrived_files.append(rem_name)

            # 重新核实两端
            try:
                src_files = await cd2.list_files(source_season_path, force_refresh=True)
            except Exception:
                src_files = []
            try:
                tgt_files = await cd2.list_files(norm_target_season, force_refresh=True)
            except Exception:
                pass
            tgt_names_set = {f.get("name", "").lower() for f in tgt_files if not f.get("isDirectory") and f.get("name")}
            remaining_in_source = [
                f.get("name", "")
                for f in src_files
                if not f.get("isDirectory")
                and f.get("name")
                and f.get("name", "").lower() not in tgt_names_set
            ]

        rem_videos = [fn for fn in remaining_in_source if is_video_file(fn)]
        tgt_videos = [f.get("name", "") for f in tgt_files if not f.get("isDirectory") and is_video_file(f.get("name", ""))]

        if not rem_videos and tgt_videos:
            # 目标正片已齐，源端无剩余待移视频 -> 完成收尾并转为 archived
            outcome = "archived"
            if src_exists and not src_files:
                try:
                    await cd2.delete_file(source_season_path)
                except Exception:
                    outcome = "archived_pending_cleanup"
            if norm_src_show and norm_src_show != norm_src_season:
                cleanup_info = await _inspect_and_cleanup_source_show_dir(cd2, norm_src_show, norm_target_parent)
                if cleanup_info.get("status") in ("cleanup_failed", "retained_unknown_attachments"):
                    outcome = "archived_pending_cleanup"

            rec.status = "archived"
            rec.archive_target_path = norm_target_season
            rec.status_detail = f"经两端对账核实已完成归档至 {norm_target_season}" + (
                f"（本次续传 {len(newly_moved)} 个文件）" if newly_moved else ""
            )
            if last_log:
                last_log.status = "completed"
                log_detail["outcome"] = outcome
                log_detail["merged_files"] = arrived_files
                last_log.detail_json = json.dumps(log_detail, ensure_ascii=False)
            await _refresh_task_summary(rec.task_id, db)
            await db.commit()
            return {
                "ok": True,
                "season_record_id": rec.id,
                "outcome": outcome,
                "arrived_files": arrived_files,
                "newly_moved": newly_moved,
                "remaining_in_source": remaining_in_source,
                "message": rec.status_detail,
            }

        rec.status = "review_needed"
        rec.status_detail = f"两端对账：目标已到位 {len(arrived_files)} 个文件，源端仍余 {len(remaining_in_source)} 个待迁移文件"
        await _refresh_task_summary(rec.task_id, db)
        await db.commit()
        return {
            "ok": True,
            "season_record_id": rec.id,
            "outcome": "partial_archived",
            "arrived_files": arrived_files,
            "newly_moved": newly_moved,
            "remaining_in_source": remaining_in_source,
            "message": rec.status_detail,
        }
    finally:
        _release_show_locks(locks)
        await cd2.close()


@router.post("/clean", response_model=StatusResponse)
async def clean_duplicates(
    body: _ArchiveRequest,
    db: AsyncSession = Depends(get_db),
) -> StatusResponse:
    """清理重复文件 — 保留最高画质版本，连带清理孤儿伴生文件，自动重新诊断并流转状态。

    核心安全原则：
    1. 外挂字幕 (.srt, .ass 等) 绝对不删；
    2. 全局资料 (poster.jpg, season.nfo, tvshow.nfo) 绝对不删；
    3. 清理冗余视频主文件的同时，连带清理该版本的附属单集 .nfo 与截图 .jpg，不留垃圾；
    4. 清理完毕自动重新识别集号，若无重复且集全，自动流转至 archive_ready（齐全可归档）；
    5. 级联刷新任务统计 summary_json。
    """
    return await _clean_duplicates_impl(body, db)


def _duplicate_cleanup_block_reason(rec) -> str:
    # archive_check 仅表示归档端待核验，不妨碍本季已证实的等价副本清理。
    if rec.status in ("identity_review", "already_archived", "manually_handled", "verified_complete", "source_absent"):
        return "当前状态不适用重复版本清理"
    try:
        conflict = json.loads(rec.conflict_json or "{}")
    except (TypeError, ValueError):
        return "身份冲突资料无法读取"
    if isinstance(conflict, dict) and conflict.get("blocking"):
        return "存在未解除的身份冲突"
    return ""


async def _clean_duplicates_impl(
    body: _ArchiveRequest,
    db: AsyncSession,
    *,
    cd2_client: CloudDrive2Client | None = None,
    active_dl_paths_override: set[str] | None = None,
    skip_summary_refresh: bool = False,
) -> StatusResponse:
    if not body.season_record_ids:
        return StatusResponse(ok=False, message="未选择任何项目")

    stmt = select(SeasonRecord).where(SeasonRecord.id.in_(body.season_record_ids))
    result = await db.execute(stmt)
    records = list(result.scalars().all())

    if not records:
        return StatusResponse(ok=False, message="未找到匹配的季度记录")

    deleted_history = await _verified_deleted_names(db, [r.id for r in records])
    owns_cd2 = cd2_client is None
    cd2 = cd2_client or CloudDrive2Client()
    total_deleted_videos = 0
    total_deleted_orphans = 0
    errors: list[str] = []
    affected_tasks: set[str] = set()

    try:
        # 可选查询 MoviePilot 活跃下载路径以防止清理连载季正在写入/做种的文件 (G02)
        active_dl_paths: set[str] = set(active_dl_paths_override or ())
        if active_dl_paths_override is None:
            try:
                settings = get_settings()
                if settings.mp_url and settings.mp_token:
                    mp_probe = MoviePilotClient()
                    try:
                        dls = await mp_probe.get_active_downloads()
                        for dl in (dls if isinstance(dls, list) else []):
                            for k in ("path", "save_path", "content_path", "target_path", "title", "name"):
                                val = str(dl.get(k) or "").strip()
                                if val:
                                    active_dl_paths.add(val)
                    finally:
                        await mp_probe.close()
            except Exception:
                pass

        for rec in records:
            season_path = rec.season_path

            from app.core.season_classifier import get_record_work_main_category, is_airing_protected
            work_cat = await get_record_work_main_category(db, rec)
            was_airing = bool(
                work_cat == "airing"
                or rec.status == "airing"
                or is_airing_protected(rec)
                or (rec.season_finished is False and "连载中" in str(rec.status_detail or ""))
            )

            block_reason = _duplicate_cleanup_block_reason(rec)
            if block_reason:
                errors.append(f"{rec.show_name}：{block_reason}")
                continue
            blocked_group = await find_blocking_group_for_record(
                db, rec, cd2_client=cd2, operation="clean_duplicates"
            )
            if blocked_group is not None:
                errors.append(
                    f"{rec.show_name}: 存在未确认的{blocked_group.title or '同 ID 多目录组'}（TMDB {blocked_group.tmdb_id}），已暂停自动清理"
                )
                await db.commit()
                continue

            # 获取该季目录在 CD2 上的实时最新文件（强制刷新缓存）
            try:
                all_files = await cd2.list_files(season_path, force_refresh=True)
            except Exception as e:
                errors.append(f"{rec.show_name} S{rec.season_number:02d}: 列出网盘文件失败: {e}")
                continue

            # 使用统一内容等价组算法生成复核清理计划（G01-G05）
            total_eps = rec.total_episodes or len(all_files)
            plan = generate_review_clean_plan(
                files=all_files,
                season_number=rec.season_number,
                total_episodes=total_eps,
                force_clean=body.force_clean,
                active_download_paths=active_dl_paths if was_airing else None,
                verified_deleted_video_names=deleted_history.get(rec.id, set()),
            )

            # G24: 执行前局部复核文件变化，若预览后新增或变化了视频文件导致范围扩大则阻断并要求重新预览
            if body.expected_fingerprints:
                planned_del_set = {d.get("path", "") for d in (plan.get("to_delete") or []) if d.get("path")}
                fp_diff = compare_plan_fingerprints(body.expected_fingerprints, all_files, planned_del_set)
                if fp_diff.get("needs_repreview"):
                    errors.append(
                        f"{rec.show_name} S{rec.season_number:02d}: 预览后检测到目录文件发生变化（新增/变动视频 {len(fp_diff['added_videos']) + len(fp_diff['modified_videos'])} 个），已拦截执行，请先更新预览后再确认"
                    )
                    continue

            # G26: 若全部同集文件均属于不同内容/剪辑版本（无需删除），落库防循环指纹并直接重新分流
            if not plan.get("can_clean") and (plan.get("all_distinct_retained") or (plan.get("status") == "clean_done" and not plan.get("unparsed_videos") and not plan.get("preserved_subdirs") and not plan.get("deferred_files"))):
                affected_tasks.add(rec.task_id)
                try:
                    ev_cur = json.loads(rec.evidence_json or "{}") if getattr(rec, "evidence_json", None) else {}
                except Exception:
                    ev_cur = {}
                if not isinstance(ev_cur, dict):
                    ev_cur = {}
                ev_cur["review_resolution"] = {
                    "all_distinct_retained": bool(plan.get("all_distinct_retained")),
                    "fingerprint": ev_cur.get("asset_fingerprint") or "",
                    "note": plan.get("distinct_retention_note") or "实时核验已无可清理副本",
                }
                ev_cur["review_plan_preview"] = {
                    "plan_id": plan.get("plan_id") or "",
                    "rules_version": plan.get("rules_version") or "v2.5.54",
                    "can_clean": False,
                    "to_delete_count": 0,
                    "all_distinct_retained": bool(plan.get("all_distinct_retained")),
                    "distinct_retention_note": plan.get("distinct_retention_note") or "实时核验已无可清理副本",
                }
                rec.evidence_json = json.dumps(ev_cur, ensure_ascii=False)
                rec.duplicate_episodes_json = "[]"
                if was_airing:
                    rec.status = "airing"
                    rec.season_finished = False
                    rec.status_detail = f"多内容版本均已保留，返回连载留源（{plan.get('distinct_retention_note') or ''}）"
                elif rec.status == "review_needed":
                    cur_rec_eps = json.loads(rec.recognized_episodes_json or "[]")
                    re_st, re_det = diagnose_season(
                        recognized=cur_rec_eps,
                        total_episodes=rec.total_episodes,
                        season_finished=rec.season_finished,
                        has_duplicates=False,
                        duplicate_episodes=[],
                        allow_bonus_specials=True,
                    )
                    rec.status = re_st
                    rec.status_detail = f"{re_det}（{plan.get('distinct_retention_note') or '多内容版本均已保留'}）"
                continue

            if not plan.get("can_clean", False):
                reason_str = plan.get("reason", "未满足清理条件")
                log.info("剧季 %s S%02d 暂缓清理: %s", rec.show_name, rec.season_number, reason_str)
                errors.append(f"{rec.show_name} S{rec.season_number:02d}: 暂缓清理 ({reason_str})")
                continue

            to_delete = plan.get("to_delete", [])
            if not to_delete:
                v_note = plan.get("version_note", "无需清理")
                log.info("剧季 %s S%02d 无需清理文件 (%s)", rec.show_name, rec.season_number, v_note)
                continue

            # 依方案第一阶段：先处理待删视频，再处理仅属于已确认删除视频的伴生文件
            video_items = [it for it in to_delete if is_video_file(it.get("name", ""))]
            orphan_items = [it for it in to_delete if not is_video_file(it.get("name", ""))]

            deleted_video_paths: list[str] = []
            deleted_files_this_season: list[str] = []
            failed_items: list[str] = []

            original_files = {f.get("path"): f for f in all_files}
            mutated_name_groups: set[str] = set()

            async def delete_planned_file(planned_path: str) -> None:
                group_key = _strip_cd2_dup_suffix(planned_path)
                actual_path = planned_path
                if group_key in mutated_name_groups:
                    original = original_files.get(planned_path) or {}
                    file_id = str(original.get("id") or "")
                    if not file_id:
                        raise RuntimeError("同名序号可能收缩，缺少稳定文件 ID，需更新预览后处理剩余项")
                    current = await cd2.list_files(season_path, force_refresh=True)
                    matching = next((f for f in current if str(f.get("id") or "") == file_id), None)
                    if matching is None:
                        return  # 该稳定 ID 已不在目录中，不能按旧路径删除其他文件。
                    if int(matching.get("size") or 0) != int(original.get("size") or 0):
                        raise RuntimeError("同名文件内容已变化，需更新预览")
                    actual_path = matching["path"]
                await cd2.delete_file(actual_path)
                mutated_name_groups.add(group_key)

            # 1. 优先按顺序执行主视频删除
            for item in video_items:
                fpath = item["path"]
                fname = item["name"]
                try:
                    await delete_planned_file(fpath)
                    deleted_video_paths.append(fpath)
                    deleted_files_this_season.append(fpath)
                    total_deleted_videos += 1
                    log.info("已安全清理冗余视频: %s (%s)", fname, item.get("reason", ""))
                except Exception as de:
                    failed_items.append(fpath)
                    errors.append(f"删除视频 {fname} 失败: {str(de)[:100]}")

            # 提取成功删除视频的词干名 (stem)，伴生文件必须严格依附于已确认删除的主文件
            deleted_video_stems = {posixpath.splitext(posixpath.basename(p))[0] for p in deleted_video_paths}

            # 2. 安全删除仅属于已确认删除视频的伴生文件，主视频删除失败严格保留附件
            for item in orphan_items:
                fpath = item["path"]
                fname = item["name"]
                parent_path = item.get("parent_video_path")
                if not ((parent_path and parent_path in deleted_video_paths)
                        or (not parent_path and item.get("historical_parent"))):
                    failed_items.append(fpath)
                    errors.append(f"伴生文件 {fname} 的主视频尚未确认删除")
                    continue
                try:
                    await delete_planned_file(fpath)
                    deleted_files_this_season.append(fpath)
                    total_deleted_orphans += 1
                    log.info("已安全清理伴生文件: %s", fname)
                except Exception as de:
                    failed_items.append(fpath)
                    errors.append(f"删除伴生文件 {fname} 失败: {str(de)[:100]}")

            # 3. 若有实际删除发生，仅强读一次真实目录用于双向实物核验与状态诊断
            if deleted_files_this_season:
                affected_tasks.add(rec.task_id)
                try:
                    fresh_files = await cd2.list_files(season_path, force_refresh=True)
                    fresh_paths = {f.get("path") for f in fresh_files}
                    orig_id_by_path = {
                        str(f.get("path") or ""): str(f.get("id") or "")
                        for f in all_files
                        if not f.get("isDirectory") and f.get("id")
                    }
                    fresh_ids = {
                        str(f.get("id") or "")
                        for f in fresh_files
                        if not f.get("isDirectory") and f.get("id")
                    }
                    use_id_verify = bool(orig_id_by_path) and (bool(fresh_ids) or not fresh_files)

                    # 双向核验：待删文件确已不在网盘，计划保留文件依然完整（支持 CD2 删除同名文件后 (1) 后缀自动收缩场景）
                    phantom_files = [
                        p for p in deleted_files_this_season
                        if (
                            (orig_id_by_path[p] in fresh_ids)
                            if (use_id_verify and p in orig_id_by_path)
                            else (p in fresh_paths)
                        )
                    ]
                    missing_keep = [
                        k["path"] for k in plan.get("to_keep", [])
                        if (
                            (orig_id_by_path[k["path"]] not in fresh_ids)
                            if (use_id_verify and k.get("path") in orig_id_by_path)
                            else (k.get("path") not in fresh_paths)
                        )
                    ]

                    verification_passed = (len(phantom_files) == 0 and len(missing_keep) == 0 and len(failed_items) == 0)

                    # 复用初检正片过滤口径，基于实物重新诊断
                    fresh_parsed = [
                        parse_episode(f["name"], f["path"], f.get("size", 0))
                        for f in fresh_files
                        if not f.get("isDirectory") and is_video_file(f.get("name", ""))
                    ]
                    regular_fresh = [ep for ep in fresh_parsed if _verified_video(ep, rec.season_number)]

                    all_fresh_eps: list[int] = []
                    for fv in regular_fresh:
                        all_fresh_eps.extend(fv.episode_numbers)

                    from collections import Counter
                    fresh_recognized = sorted(set(all_fresh_eps))

                    # 清理后立即以统一内容等价引擎重算是否还存在真重复副本（防止加长版/普通版共存被判成重复循环 · G26）
                    historical_names = deleted_history.get(rec.id, set()) | {posixpath.basename(p) for p in deleted_video_paths}
                    post_plan = generate_review_clean_plan(
                        verified_deleted_video_names=historical_names,
                        files=fresh_files,
                        season_number=rec.season_number,
                        total_episodes=rec.total_episodes or len(fresh_files),
                        active_download_paths=active_dl_paths if was_airing else None,
                    )
                    if post_plan.get("can_clean"):
                        ep_cnt = Counter(all_fresh_eps)
                        fresh_duplicates = sorted([n for n, c in ep_cnt.items() if c > 1])
                    else:
                        fresh_duplicates = []

                    fresh_missing = []
                    if rec.total_episodes:
                        fresh_missing = sorted(set(range(1, rec.total_episodes + 1)) - set(fresh_recognized))

                    try:
                        ev_after = json.loads(rec.evidence_json or "{}") if getattr(rec, "evidence_json", None) else {}
                    except Exception:
                        ev_after = {}
                    if not isinstance(ev_after, dict):
                        ev_after = {}
                    ev_after["review_plan_preview"] = {
                        "plan_id": post_plan.get("plan_id") or "",
                        "rules_version": post_plan.get("rules_version") or "v2.5.54",
                        "can_clean": bool(post_plan.get("can_clean")),
                        "to_delete_count": len(post_plan.get("to_delete") or []),
                        "all_distinct_retained": bool(post_plan.get("all_distinct_retained")),
                        "distinct_retention_note": str(post_plan.get("distinct_retention_note") or ""),
                    }
                    if post_plan.get("all_distinct_retained") and not post_plan.get("can_clean"):
                        ev_after["review_resolution"] = {
                            "all_distinct_retained": True,
                            "fingerprint": ev_after.get("asset_fingerprint") or "",
                            "note": post_plan.get("distinct_retention_note") or "多内容版本均已保留",
                        }

                    if post_plan.get("can_clean"):
                        verification_passed = False
                        errors.append(f"{rec.show_name}：核验后仍有推荐清理项，请更新预览")
                    if not verification_passed and not errors:
                        errors.append(f"{rec.show_name}：清理后实物核验未通过")
                    if verification_passed:
                        if was_airing:
                            # G02: 连载季清理稳定副本后必须返回连载留源，绝不越级归档
                            new_status = "airing"
                            new_detail = f"已清理 {len(deleted_video_paths)} 个稳定重复副本，保全连载期其余内容，已返回「连载留源」"
                        else:
                            new_status, new_detail = diagnose_season(
                                recognized=fresh_recognized,
                                total_episodes=rec.total_episodes,
                                season_finished=rec.season_finished,
                                has_duplicates=bool(fresh_duplicates),
                                duplicate_episodes=fresh_duplicates,
                                allow_bonus_specials=True,
                            )
                        p_ev = ev_after.get("period_evidence") or {}
                        period_status = p_ev.get("source_status") or (rec.status if rec.status.startswith("period_") else "")
                        if not was_airing and period_status in ("period_ready", "period_missing", "period_review"):
                            from app.core.segments import diagnose_periods
                            period_result = diagnose_periods(
                                fresh_files, rec.season_number, p_ev.get("mapping") or {}, p_ev.get("ordinals") or {},
                                rec.total_episodes, rec.season_finished, False,
                                whole_period_verified=bool(p_ev.get("whole_period_verified")),
                            ) if p_ev.get("mapping") else None
                            if period_result:
                                new_status, new_detail, fresh_missing, fresh_recognized = period_result
                            else:
                                new_status = period_status
                                new_detail = "稳定副本已核验清理，沿用已确认的期段覆盖结论"
                                fresh_recognized = json.loads(rec.recognized_episodes_json or "[]")
                                fresh_missing = json.loads(rec.missing_episodes_json or "[]")
                        if rec.status == "archive_check" and not was_airing:
                            new_status = "archive_check"
                            new_detail = f"本季稳定副本已清理；{rec.status_detail or '归档端覆盖仍需核验'}"
                        op_status = "completed"
                    else:
                        new_status = "review_needed"
                        issue_desc = []
                        if phantom_files:
                            issue_desc.append(f"{len(phantom_files)}个已删文件仍残留")
                        if missing_keep:
                            issue_desc.append(f"{len(missing_keep)}个保留文件缺失")
                        if failed_items:
                            issue_desc.append(f"{len(failed_items)}个文件删除失败")
                        new_detail = f"清理待核实: {', '.join(issue_desc)}"
                        op_status = "partial_success"

                    rec.local_episode_count = len(regular_fresh)
                    rec.recognized_episodes_json = json.dumps(fresh_recognized)
                    rec.duplicate_episodes_json = json.dumps(fresh_duplicates)
                    rec.missing_episodes_json = json.dumps(fresh_missing)
                    rec.status = new_status
                    rec.status_detail = new_detail
                    rec.evidence_json = json.dumps(ev_after, ensure_ascii=False)
                    log.info("季度状态自动重新计算: %s S%02d -> %s (%s)", rec.show_name, rec.season_number, new_status, new_detail)

                    op_log = OperationLog(
                        task_id=rec.task_id,
                        season_record_id=rec.id,
                        action="clean_duplicate",
                        status=op_status,
                        detail_json=json.dumps({
                            "show": rec.show_name,
                            "season": rec.season_number,
                            "plan_id": plan.get("plan_id"),
                            "version_note": plan["version_note"],
                            "deleted_paths": deleted_files_this_season,
                            "failed_paths": failed_items,
                            "verification_passed": verification_passed,
                            "next_status": new_status,
                        }, ensure_ascii=False),
                    )
                    db.add(op_log)

                except Exception as fe:
                    log.warning("清理后实物核验与重新诊断失败: %s", fe)
                    errors.append(f"{rec.show_name} S{rec.season_number:02d}: 清理后实物核验失败: {fe}")

        # 5. 级联刷新受影响的任务统计
        if not skip_summary_refresh:
            for tid in affected_tasks:
                await _refresh_task_summary(tid, db)

        await db.commit()

    finally:
        if owns_cd2:
            await cd2.close()

    is_ok = not errors

    msg = f"清理完成：成功删除 {total_deleted_videos} 个冗余视频，清理 {total_deleted_orphans} 个伴生孤儿文件"
    if errors:
        msg += f"，遇到 {len(errors)} 项提醒: {'; '.join(errors[:2])}"

    return StatusResponse(ok=is_ok, message=msg)


# ---------- 试跑预览与源端冗余清理 ----------

class CleanPreviewItem(BaseModel):
    season_record_id: str
    show_name: str
    season_number: int
    can_clean: bool
    reason: str
    version_note: str
    to_keep: list[dict]
    to_delete: list[dict]
    plan_id: str = ""
    rules_version: str = "v2.5.54"
    reclaimable_bytes: int = 0
    file_fingerprints: dict[str, dict[str, Any]] = {}
    deferred_files: list[dict] = []
    all_distinct_retained: bool = False
    distinct_retention_note: str = ""
    is_airing: bool = False


class CleanPreviewResponse(BaseModel):
    ok: bool
    message: str
    previews: list[CleanPreviewItem]


@router.post("/clean-preview", response_model=CleanPreviewResponse)
async def clean_duplicates_preview(
    body: _ArchiveRequest,
    db: AsyncSession = Depends(get_db),
) -> CleanPreviewResponse:
    """试跑复核清理计划 — 预览将要保留与删除的文件列表、内容等价理由与文件指纹，不执行真实删除。"""
    if not body.season_record_ids:
        return CleanPreviewResponse(ok=False, message="未选择任何项目", previews=[])

    stmt = select(SeasonRecord).where(SeasonRecord.id.in_(body.season_record_ids))
    result = await db.execute(stmt)
    records = list(result.scalars().all())

    if not records:
        return CleanPreviewResponse(ok=False, message="未找到匹配的季度记录", previews=[])

    cd2 = CloudDrive2Client()
    previews: list[CleanPreviewItem] = []

    deleted_history = await _verified_deleted_names(db, [r.id for r in records])
    try:
        for rec in records:
            season_path = rec.season_path
            from app.core.season_classifier import get_record_work_main_category, is_airing_protected
            work_cat = await get_record_work_main_category(db, rec)
            was_airing = bool(
                work_cat == "airing"
                or rec.status == "airing"
                or is_airing_protected(rec)
                or (rec.season_finished is False and "连载中" in str(rec.status_detail or ""))
            )
            try:
                all_files = await cd2.list_files(season_path, force_refresh=True)
            except Exception as e:
                previews.append(CleanPreviewItem(
                    season_record_id=rec.id,
                    show_name=rec.show_name,
                    season_number=rec.season_number,
                    can_clean=False,
                    reason=f"列出网盘文件失败: {e}",
                    version_note="",
                    to_keep=[],
                    to_delete=[],
                    is_airing=was_airing,
                ))
                continue

            total_eps = rec.total_episodes or len(all_files)
            plan = generate_review_clean_plan(
                verified_deleted_video_names=deleted_history.get(rec.id, set()),
                files=all_files,
                season_number=rec.season_number,
                total_episodes=total_eps,
                force_clean=body.force_clean,
            )

            previews.append(CleanPreviewItem(
                season_record_id=rec.id,
                show_name=rec.show_name,
                season_number=rec.season_number,
                can_clean=bool(plan.get("can_clean", False)),
                reason=str(plan.get("reason", "")),
                version_note=str(plan.get("version_note", "")),
                to_keep=list(plan.get("to_keep", [])),
                to_delete=list(plan.get("to_delete", [])),
                plan_id=str(plan.get("plan_id", "")),
                rules_version=str(plan.get("rules_version", "v2.5.54")),
                reclaimable_bytes=int(plan.get("reclaimable_bytes", 0)),
                file_fingerprints=dict(plan.get("file_fingerprints") or {}),
                deferred_files=list(plan.get("deferred_files") or []),
                all_distinct_retained=bool(plan.get("all_distinct_retained", False)),
                distinct_retention_note=str(plan.get("distinct_retention_note", "")),
                is_airing=was_airing,
            ))
    finally:
        await cd2.close()

    return CleanPreviewResponse(
        ok=True,
        message=f"已生成 {len(previews)} 季的复核清理预览",
        previews=previews,
    )


@router.post("/clean-source-redundant", response_model=StatusResponse)
async def clean_source_redundant(
    body: _ArchiveRequest,
    db: AsyncSession = Depends(get_db),
) -> StatusResponse:
    """一键清理源端冗余季目录 — 用于归档库已完整存在、源端仅剩历史残留的情况。

    执行步骤：
    1. 校验源季目录真实性；
    2. 安全删除源端季目录 (cd2.delete_file)；
    3. 保留剧集父目录和其他季度；
    4. 更新数据库状态为 cleaned_redundant，写入操作日志；
    5. 级联刷新任务统计。
    """
    return await _clean_source_redundant_impl(body, db)


async def _clean_source_redundant_impl(
    body: _ArchiveRequest,
    db: AsyncSession,
    *,
    cd2_client: CloudDrive2Client | None = None,
    skip_summary_refresh: bool = False,
) -> StatusResponse:
    if not body.season_record_ids:
        return StatusResponse(ok=False, message="未选择任何项目")

    stmt = select(SeasonRecord).where(SeasonRecord.id.in_(body.season_record_ids))
    result = await db.execute(stmt)
    records = list(result.scalars().all())

    if not records:
        return StatusResponse(ok=False, message="未找到匹配的季度记录")

    owns_cd2 = cd2_client is None
    cd2 = cd2_client or CloudDrive2Client()
    success_count = 0
    errors: list[str] = []
    affected_tasks: set[str] = set()
    cleanup_notes: list[str] = []

    try:
        for rec in records:
            source_season_path = rec.season_path
            show_path = rec.show_path

            # 【P0-1 & P1-1: 物理安全门禁：状态、路径隔离、两端集号与独有附件覆盖核验】
            if rec.status not in {"already_archived", "cleaned_redundant"}:
                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 当前状态为 {rec.status}，非已归档状态，安全拦截源端删除！"
                log.warning(err_msg)
                errors.append(err_msg)
                continue

            import posixpath
            settings = get_settings()
            norm_source_root = posixpath.normpath(settings.source_root.rstrip("/")) if settings.source_root else ""
            norm_archive_root = posixpath.normpath(settings.archive_root.rstrip("/")) if settings.archive_root else ""
            norm_src = posixpath.normpath(source_season_path)

            # 门禁 1: 源季路径必须为受控源根目录的严格子目录
            if not norm_source_root or not (norm_src.startswith(norm_source_root + "/") and norm_src != norm_source_root):
                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 源季路径非受控源根严格子目录 ({source_season_path})，拦截删除！"
                log.error(err_msg)
                errors.append(err_msg)
                continue

            archive_target = rec.archive_target_path
            if not archive_target:
                if norm_src.startswith(norm_source_root + "/"):
                    relative = norm_src[len(norm_source_root):]
                    archive_target = norm_archive_root + relative
                else:
                    err_msg = f"{rec.show_name} S{rec.season_number:02d}: 无法安全推导归档目标路径，需要重新初检，拦截删除！"
                    log.warning(err_msg)
                    errors.append(err_msg)
                    continue

            norm_target = posixpath.normpath(archive_target)

            # 门禁 2: 目标路径必须属于归档库根目录，且源与目标路径绝对不能重叠
            if not norm_archive_root or not (norm_target.startswith(norm_archive_root + "/") and norm_target != norm_archive_root):
                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 归档目标非受控归档根严格子目录 ({archive_target})，拦截删除！"
                log.error(err_msg)
                errors.append(err_msg)
                continue

            if norm_src == norm_target or norm_src.startswith(norm_target + "/") or norm_target.startswith(norm_src + "/"):
                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 严重危险拦截：源路径与目标路径重叠 ({norm_src} vs {norm_target})，拒绝删除！"
                log.error(err_msg)
                errors.append(err_msg)
                continue

            try:
                source_present = await source_entry(cd2, source_season_path, norm_source_root) is not None
                show_present = await source_entry(cd2, show_path, norm_source_root) is not None if not source_present else True
                if not source_present and not show_present:
                    # 整个源剧根已在外部消失：只结束源端待办，不声称本次删除或已归档。
                    ev = json.loads(rec.evidence_json or "{}")
                    if ev.get("source_root_preservation") or ev.get("source_cleanup_auxiliary"):
                        # 有本应用未完成的保全事务时仍走原后验，不能以源消失掩盖资料丢失。
                        pass
                    else:
                        await record_source_absent(db, rec, show_absent=True)
                        affected_tasks.add(rec.task_id)
                        success_count += 1
                        cleanup_notes.append(f"{rec.show_name}: 源剧集外层已不存在，已对账移出待办；未执行删除，不代表归档完成")
                        continue
            except Exception as exc:
                errors.append(f"{rec.show_name}: 源目录状态未核实（{exc}），保留待办")
                continue

            blocked_group = await find_blocking_group_for_record(
                db, rec, cd2_client=cd2, operation="clean_source_redundant"
            )
            if blocked_group is not None:
                err_msg = (
                    f"{rec.show_name} S{rec.season_number:02d}: 存在未确认或已标记共存的{blocked_group.title or '多目录组'}"
                    f"（TMDB {blocked_group.tmdb_id}），已暂停删除源端冗余：{blocked_group.invalidated_reason or '请先确认归档目标'}"
                )
                log.warning(err_msg)
                errors.append(err_msg)
                await db.commit()
                continue

            # 门禁 3: 重新实时获取源端与目标端文件清单，执行集号与独有附件覆盖比对
            try:
                import re
                expected_target = norm_archive_root + norm_src[len(norm_source_root):]
                if norm_src != source_season_path or norm_target != archive_target:
                    raise ValueError("路径含未规范化的目录层级")
                if (posixpath.dirname(norm_src) != show_path or
                        _parse_season_number(posixpath.basename(norm_src)) != rec.season_number):
                    raise ValueError("源目录并非本剧的独立季目录")
                if norm_target != expected_target:
                    src_parent = posixpath.dirname(expected_target)
                    dst_parent = posixpath.dirname(norm_target)
                    tag = re.search(r"\[tmdbid[=:](\d+)\]", posixpath.basename(src_parent), re.I)
                    if (not tag or str(rec.tmdb_id) != tag[1]
                            or posixpath.dirname(src_parent) != posixpath.dirname(dst_parent)):
                        raise ValueError("归档别名目录的作品或分类不匹配")
                    category = await cd2.list_files(posixpath.dirname(dst_parent), force_refresh=True)
                    matches = [i["path"] for i in category if i["isDirectory"]
                               and re.search(rf"\[tmdbid[=:]{tag[1]}\]", i["name"], re.I)]
                    seasons = await cd2.list_files(dst_parent, force_refresh=True)
                    paths = [i["path"] for i in seasons if i["isDirectory"]
                             and _parse_season_number(i["name"]) == rec.season_number]
                    if (dst_parent not in matches or paths != [norm_target]) if len(matches) > 1 else (matches != [dst_parent] or paths != [norm_target]):
                        raise ValueError("同作品及季号未匹配到唯一归档目录")
                cleanup_evidence = json.loads(rec.evidence_json or "{}")
                deleted_marker = cleanup_evidence.get("source_season_cleanup") or {}
                season_already_deleted = rec.status == "cleaned_redundant" or (
                    deleted_marker.get("verified") is True
                    and deleted_marker.get("source_path") == source_season_path
                    and deleted_marker.get("archive_target") == archive_target)
                if season_already_deleted:
                    category = posixpath.dirname(show_path)
                    shows = checked_listing(await cd2.list_files(category, force_refresh=True), category)
                    show_exists = any(f["path"] == show_path for f in shows)
                    source_children = checked_listing(await cd2.list_files(show_path, force_refresh=True), show_path) if show_exists else []
                    if any(f["path"] == source_season_path for f in source_children):
                        raise ValueError("本季曾完成删除但源季重新出现，禁止按旧回执再次删除，请重新核对")
                    src_files = []
                elif not source_present:
                    # 外部已删本季但剧根仍在：跳过重复删除，仍核验归档与外层保全。
                    season_already_deleted = True
                    src_files = []
                else:
                    src_files = await cd2.list_files(source_season_path, force_refresh=True)
                target_files = await cd2.list_files(archive_target, force_refresh=True)
                checked_listing(src_files, source_season_path)
                checked_listing(target_files, archive_target)

                src_video_eps: set[int] = set()
                src_subs: list[dict] = []
                auxiliary_folders = []
                preserved_auxiliary = []
                previous_auxiliary = cleanup_evidence.get("source_cleanup_auxiliary")
                if previous_auxiliary and not any(f["path"] == previous_auxiliary.get("source_path") for f in src_files):
                    previous_target = previous_auxiliary.get("target_path", "")
                    if (previous_auxiliary.get("source_path") != norm_src + "/合并保留"
                            or not (previous_target == norm_target + "/合并保留"
                                    or previous_target.startswith(norm_target + "/合并保留/"))
                            or posixpath.normpath(previous_target) != previous_target):
                        raise ValueError("既有附件保全回执与本季路径不一致")
                    await verify_preserved_folder(cd2, previous_auxiliary)
                    preserved_auxiliary.append(previous_auxiliary)
                src_unique_content_videos: list[tuple[dict, tuple]] = []
                unrecognized_videos: list[dict] = []
                for sf in src_files:
                    if sf.get("isDirectory"):
                        if sf["name"] != "合并保留":
                            raise ValueError("源季内另有子目录：" + sf["name"])
                        auxiliary_folders.append((sf["path"], await read_auxiliary_tree(cd2, sf["path"])))
                        continue
                    sf_name = sf.get("name", "")
                    if is_video_file(sf_name):
                        p_sf = parse_episode(sf_name, sf.get("path", ""), sf.get("size", 0))
                        # G10: 识别源端独有的加更/加长版/Vlog/纯享/先导等明确内容版本（先保全至归档再清源）
                        is_distinct_known_content = (
                            (p_sf.content_kind in ("vlog", "extra_ep", "pure", "pilot", "interview", "easter_egg"))
                            or (p_sf.content_kind == "regular" and p_sf.edit_version != "standard" and bool(p_sf.episode_numbers))
                        )
                        if is_distinct_known_content and int(sf.get("size") or 0) > 0:
                            src_unique_content_videos.append((sf, p_sf.content_unit_key))
                        elif _verified_video(p_sf, rec.season_number) and not segment_number(sf_name):
                            src_video_eps.update(p_sf.episode_numbers)
                        else:
                            unrecognized_videos.append(sf)
                    elif is_subtitle_file(sf_name):
                        src_subs.append(sf)
                    elif posixpath.splitext(sf_name)[1].lower() not in {".nfo", ".jpg", ".jpeg", ".png", ".webp"}:
                        raise ValueError("源端另有待核对文件：" + sf_name)

                target_video_eps: set[int] = set()
                target_content_keys: set[tuple] = set()
                target_names_lower: set[str] = set()
                target_sub_sizes = {}
                for tf in target_files:
                    if tf.get("isDirectory"):
                        continue
                    tf_name = tf.get("name", "")
                    target_names_lower.add(tf_name.casefold())
                    if is_video_file(tf_name):
                        p_tf = parse_episode(tf_name, tf.get("path", ""), tf.get("size", 0))
                        target_content_keys.add(p_tf.content_unit_key)
                        if _verified_video(p_tf, rec.season_number) and not segment_number(tf_name):
                            target_video_eps.update(p_tf.episode_numbers)
                    elif is_subtitle_file(tf_name):
                        target_sub_sizes[tf_name.casefold()] = tf.get("size")

                # 核验 A: 源端若含有无法识别集号的正片视频，绝对拦截删除（源集数与记录不变）
                if unrecognized_videos:
                    unrec_names = [f.get("name", "") for f in unrecognized_videos[:2]]
                    err_msg = f"{rec.show_name} S{rec.season_number:02d}: 源端含有未识别集号的正片视频 ({unrec_names})，证据不足拦截删除！"
                    log.warning(err_msg)
                    errors.append(err_msg)
                    continue

                if not src_video_eps and not src_unique_content_videos:
                    # 源端仅剩附件且归档已独立完整覆盖全季（Section 6.1）
                    if rec.total_episodes and set(range(1, rec.total_episodes + 1)) <= target_video_eps:
                        pass
                    else:
                        has_video = any(not f.get("isDirectory") and is_video_file(f.get("name", "")) for f in src_files)
                        reason = "本层的视频均标记为花絮" if has_video else "源季目录本层没有支持的视频文件"
                        err_msg = f"{rec.show_name} S{rec.season_number:02d}: {reason}，已保留源目录。"
                        log.warning(err_msg)
                        errors.append(err_msg)
                        continue

                # 核验 B: 目标端必须完整包含源端所有正片集号 (source_eps ⊆ target_eps)，证据不足坚决拦截
                uncovered_eps = sorted(src_video_eps - target_video_eps)
                if rec.total_episodes and not set(range(1, rec.total_episodes + 1)) <= target_video_eps:
                    raise ValueError("归档目录已无法核实全季覆盖")
                if uncovered_eps:
                    err_msg = f"{rec.show_name} S{rec.season_number:02d}: 源端含有归档库未包含的集数 {uncovered_eps}，拦截删除！"
                    log.warning(err_msg)
                    errors.append(err_msg)
                    continue

                if any(posixpath.dirname(f["path"]) != norm_src for f in src_files):
                    raise ValueError("文件路径与源季目录不一致")

                # 核验 C (G10): 先保全源端独有加更/加长版/Vlog/纯享内容至归档目录，核验到达后再清源
                preserved_unique_names: set[str] = set()
                for uf, u_ckey in src_unique_content_videos:
                    u_name = uf.get("name", "")
                    if u_ckey in target_content_keys or u_name.casefold() in target_names_lower:
                        continue
                    try:
                        mv_u = await cd2.move_file(uf.get("path"), archive_target, skip_existing=True)
                        if not mv_u.get("success", False):
                            raise ValueError(f"网盘未确认独有内容 {u_name} 迁入归档成功")
                        preserved_unique_names.add(u_name)
                        log.info("源端清源前已安全保全独有内容至归档: %s -> %s", u_name, archive_target)
                    except Exception as u_mv_err:
                        raise ValueError(f"保全源端独有内容 {u_name} 失败") from u_mv_err

                # 核验 D: 源端独有外挂字幕自动迁移至目标库
                for sub in src_subs:
                    s_name = sub.get("name", "")
                    if s_name.casefold() in target_sub_sizes and sub.get("size") != target_sub_sizes[s_name.casefold()]:
                        raise ValueError("两端存在同名但大小不同的字幕：" + s_name)
                    if s_name.casefold() not in target_sub_sizes:
                        try:
                            moved = await cd2.move_file(sub.get("path"), archive_target, skip_existing=True)
                            if not moved.get("success", False):
                                raise ValueError("网盘未确认字幕移动成功")
                            log.info("源端残留清理前已安全迁移独有字幕: %s -> %s", s_name, archive_target)
                        except Exception as sub_mv_err:
                            raise ValueError(f"迁移字幕 {s_name} 失败") from sub_mv_err
                moved_names = {s["name"] for s in src_subs if s["name"].casefold() not in target_sub_sizes} | preserved_unique_names
                # 与识别流程同步：已核实纯附件的合并保留目录先保全，再继续同一次清源。
                # 整体迁入，遇到归档端同名目录使用独立保全位置，绝不覆盖既有附件。
                async def persist_auxiliary_intent(receipt):
                    cleanup_evidence["source_cleanup_auxiliary"] = dict(receipt, state="pending")
                    rec.evidence_json = json.dumps(cleanup_evidence, ensure_ascii=False)
                    await db.commit()
                for folder_path, inventory in auxiliary_folders:
                    preserved_auxiliary.append(await preserve_auxiliary_folder(
                        cd2, folder_path, archive_target, inventory, before_move=persist_auxiliary_intent))
                    moved_names.add(posixpath.basename(folder_path))
                def signature(files):
                    return sorted((f["path"], f.get("size"), f.get("modified"), f.get("isDirectory"), str(f.get("id", "")))
                                  for f in files)
                fresh_source = [] if season_already_deleted else await cd2.list_files(source_season_path, force_refresh=True)
                checked_listing(fresh_source, source_season_path)
                if signature(fresh_source) != signature([f for f in src_files if f["name"] not in moved_names]):
                    raise ValueError("源季内容已变化，请重新初检")

                # 若保全了独有内容，核验目标归档目录中确已存在
                if preserved_unique_names:
                    fresh_target_after = await cd2.list_files(archive_target, force_refresh=True)
                    fresh_target_names = {f.get("name", "").casefold() for f in fresh_target_after}
                    missing_preserved = [n for n in preserved_unique_names if n.casefold() not in fresh_target_names]
                    if missing_preserved:
                        raise ValueError(f"归档目标未核验到保全文件: {missing_preserved}")

                if preserved_auxiliary:
                    # 附件迁移可能耗时：删除前复查归档正片和附件，不能沿用移动前的覆盖快照。
                    fresh_target = checked_listing(await cd2.list_files(archive_target, force_refresh=True), archive_target)
                    original_media = [f for f in target_files if not f["isDirectory"] and is_video_file(f["name"])]
                    original_paths = {f["path"] for f in original_media}
                    if signature([f for f in fresh_target if f["path"] in original_paths]) != signature(original_media):
                        raise ValueError("附件保全期间归档正片清单已变化，暂不清源")
                    for receipt in preserved_auxiliary:
                        await verify_preserved_folder(cd2, receipt)
                    final_source = [] if season_already_deleted else checked_listing(await cd2.list_files(source_season_path, force_refresh=True), source_season_path)
                    if signature(final_source) != signature(fresh_source):
                        raise ValueError("附件后验期间源季清单已变化，暂不清源")

            except Exception as chk_err:
                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 校验两端文件清单失败 ({chk_err})，安全拦截源端删除！"
                log.warning(err_msg)
                errors.append(err_msg)
                continue

            try:
                # 1. 安全回收源端冗余季目录
                log.info("已通过全部覆盖门禁，正在安全清理源端冗余季目录: %s", source_season_path)
                if not season_already_deleted:
                    delete_receipt = await cd2.delete_file(source_season_path)
                    if not isinstance(delete_receipt, dict) or delete_receipt.get("success") is not True:
                        raise ValueError("源季删除回执未确认成功")
                    remaining_seasons = checked_listing(await cd2.list_files(show_path, force_refresh=True), show_path)
                    if any(f["path"] == source_season_path for f in remaining_seasons):
                        raise ValueError("源季删除后仍可见，尚未确认清理完成")
                cleanup_evidence["source_season_cleanup"] = dict(
                    verified=True, source_path=source_season_path, archive_target=archive_target)
                rec.evidence_json = json.dumps(cleanup_evidence, ensure_ascii=False)
                await db.commit()

                # 2. 空壳检查：仅在剧根仅剩空壳且无其他季/内容时清理剧根
                async def persist_root_intent(receipt):
                    cleanup_evidence["source_root_preservation"] = dict(receipt, state="pending")
                    rec.evidence_json = json.dumps(cleanup_evidence, ensure_ascii=False)
                    await db.commit()
                cleanup_info = await _inspect_and_cleanup_source_show_dir(
                    cd2, show_path, posixpath.dirname(archive_target), before_move=persist_root_intent,
                    previous_receipt=cleanup_evidence.get("source_root_preservation"))
                shell_cleaned = bool(cleanup_info.get("cleaned"))
                cleanup_evidence["source_root_cleanup"] = cleanup_info
                if shell_cleaned and cleanup_evidence.get("source_root_preservation"):
                    cleanup_evidence["source_root_preservation"]["state"] = "completed"
                rec.evidence_json = json.dumps(cleanup_evidence, ensure_ascii=False)
                if cleanup_info.get("status") in {"cleanup_failed", "retained_unknown_attachments"}:
                    rec.status = "already_archived"
                    rec.status_detail = "本季已清理，剧集外层待处理：" + str(cleanup_info.get("reason", ""))
                    affected_tasks.add(rec.task_id)
                    errors.append(f"{rec.show_name} S{rec.season_number:02d}: {rec.status_detail}")
                    db.add(OperationLog(task_id=rec.task_id, season_record_id=rec.id,
                        action="clean_source_redundant", status="partial",
                        detail_json=json.dumps(dict(season_deleted=True, shell_cleaned=False,
                            shell_cleanup=cleanup_info, receipt=rec.status_detail), ensure_ascii=False)))
                    continue

                # 3. 刷新剧集所在父目录及相关季目录
                refresh_errors: list[str] = []
                for ref_dir in {posixpath.dirname(show_path) if show_path else "", show_path or "", archive_target}:
                    if ref_dir and not (shell_cleaned and ref_dir == show_path):
                        try:
                            await cd2.list_files(ref_dir, force_refresh=True)
                        except Exception as re_err:
                            refresh_errors.append(f"{ref_dir}: {re_err}")

                success_count += 1
                rec.status = "cleaned_redundant"
                if preserved_auxiliary:
                    cleanup_evidence["source_cleanup_auxiliary"]["state"] = "completed"
                    rec.evidence_json = json.dumps(cleanup_evidence, ensure_ascii=False)
                preserved_note = f"（已先保全 {len(preserved_unique_names)} 个独有内容至归档）" if preserved_unique_names else ""
                if preserved_auxiliary:
                    preserved_note += f"（已先保全合并保留目录内 {sum(r['file_count'] for r in preserved_auxiliary)} 个附件）"
                root_note = "剧集外层已移除" if shell_cleaned else "剧集外层保留：" + str(cleanup_info.get("reason", ""))
                rec.status_detail = f"本季已清理，{root_note}；归档已保留{preserved_note}"
                cleanup_notes.append(f"{rec.show_name} S{rec.season_number:02d}: {root_note}")
                affected_tasks.add(rec.task_id)

                op_log = OperationLog(
                    task_id=rec.task_id,
                    season_record_id=rec.id,
                    action="clean_source_redundant",
                    status="completed",
                    detail_json=json.dumps({
                        "show": rec.show_name,
                        "season": rec.season_number,
                        "deleted_path": source_season_path,
                        "preserved_unique_files": sorted(preserved_unique_names),
                        "preserved_auxiliary_folders": [{k: v for k, v in r.items() if k != "inventory"} for r in preserved_auxiliary],
                        "shell_cleaned": shell_cleaned,
                        "shell_cleanup": cleanup_info,
                        "refresh_errors": refresh_errors,
                        "receipt": rec.status_detail,
                    }, ensure_ascii=False),
                )
                db.add(op_log)

            except Exception as e:
                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 清理失败 - {str(e)[:150]}"
                log.error(err_msg)
                errors.append(err_msg)

        if not skip_summary_refresh:
            for tid in affected_tasks:
                await _refresh_task_summary(tid, db)

        await db.commit()

    finally:
        if owns_cd2:
            await cd2.close()

    msg = f"清源结果：{success_count}/{len(records)} 季完成，其余未确认完成"
    if cleanup_notes:
        msg += "；" + "; ".join(cleanup_notes[:2])
    if errors:
        msg += f"，遇到 {len(errors)} 项提醒: {'; '.join(errors[:2])}"

    return StatusResponse(ok=success_count > 0, message=msg)


@router.post("/calibrate-episodes", response_model=StatusResponse)
async def calibrate_episodes(
    body: CalibrateEpisodesRequest,
    db: AsyncSession = Depends(get_db),
) -> StatusResponse:
    """人工校准剧季官方总集数（解决综艺拆分、网络短剧TMDB虚标等元数据失真问题）。"""
    rec = await db.get(SeasonRecord, body.season_record_id)
    if not rec:
        return StatusResponse(ok=False, message="剧季记录不存在")

    # SCAN-08 安全门禁：总集数必须大于 0，非法输入明确拒绝
    if body.total_episodes is None or body.total_episodes <= 0:
        return StatusResponse(ok=False, message=f"非法总集数 ({body.total_episodes})，总集数必须为大于 0 的正整数")

    if rec.status in ("archive_check", "period_review", "period_ready", "period_missing", "identity_review", "already_archived", "manually_handled", "verified_complete"):
        return StatusResponse(ok=False, message="归档核对记录需通过重新初检更新")
    c_info = json.loads(rec.conflict_json or "{}") if getattr(rec, "conflict_json", None) else {}
    if isinstance(c_info, dict) and c_info.get("blocking"):
        return StatusResponse(ok=False, message="存在作品身份冲突，请先在身份确认面板核对或撤销后再校准")

    old_total = rec.total_episodes
    rec.total_episodes = body.total_episodes

    recognized = json.loads(rec.recognized_episodes_json or "[]")
    duplicates = json.loads(rec.duplicate_episodes_json or "[]")

    expected = set(range(1, body.total_episodes + 1))
    missing = sorted(expected - set(recognized))
    rec.missing_episodes_json = json.dumps(missing)

    eff_finished = bool(rec.season_finished) if rec.season_finished is not None else False
    new_status, new_detail = diagnose_season(
        recognized=recognized,
        total_episodes=body.total_episodes,
        season_finished=eff_finished,
        has_duplicates=bool(duplicates),
        duplicate_episodes=duplicates,
    )
    rec.status = new_status
    rec.status_detail = f"人工已校准总集数为 {body.total_episodes} 集（原 {old_total} 集）：{new_detail}"

    await _refresh_task_summary(rec.task_id, db)
    await db.commit()

    return StatusResponse(
        ok=True,
        message=f"已成功将 {rec.show_name} S{rec.season_number:02d} 总集数校准为 {body.total_episodes} 集，最新状态：{new_status}"
    )


class DeleteSourceShowRequest(BaseModel):
    season_record_id: str


@router.post("/delete-source-show", response_model=StatusResponse)
async def delete_source_show(
    body: DeleteSourceShowRequest,
    db: AsyncSession = Depends(get_db),
) -> StatusResponse:
    """彻底删除源端整个剧集目录及其所有文件，并在删除后强制刷新父目录。"""
    rec = await db.get(SeasonRecord, body.season_record_id)
    if not rec:
        return StatusResponse(ok=False, message="剧季记录不存在")

    show_path = rec.show_path
    if not show_path or len(show_path.strip("/").split("/")) < 2:
        return StatusResponse(ok=False, message=f"非法或不安全的剧集源路径: {show_path}")

    import posixpath
    settings = get_settings()
    norm_show_path = posixpath.normpath(show_path)

    # 安全红线 1: 绝对禁止将根目录或顶层作为剧集删除
    if not norm_show_path or norm_show_path in ("/", ".", "..") or len(norm_show_path.strip("/").split("/")) < 2:
        return StatusResponse(ok=False, message=f"非法或危险的剧集源路径: {show_path}")

    # 安全红线 2: 绝对禁止删除配置的源根目录自身 (P0-3)
    source_root = settings.source_root.rstrip("/")
    norm_source_root = posixpath.normpath(source_root) if source_root else ""
    if norm_source_root:
        if norm_show_path == norm_source_root:
            return StatusResponse(ok=False, message=f"严重危险拦截：禁止删除受控源根目录本身 ({show_path})！")
        if not norm_show_path.startswith(norm_source_root + "/"):
            return StatusResponse(ok=False, message=f"目标路径不在受控源目录下: {show_path}")

    # 安全红线 3: 绝对禁止将归档根目录及其子项作为源端删除
    archive_root = settings.archive_root.rstrip("/")
    norm_archive_root = posixpath.normpath(archive_root) if archive_root else ""
    if norm_archive_root:
        if norm_show_path == norm_archive_root or norm_show_path.startswith(norm_archive_root + "/"):
            return StatusResponse(ok=False, message=f"严重危险拦截：目标路径属于归档库根目录或其子路径，严禁删除: {show_path}")

    if rec.status in ("download_needed", "period_missing"):
        return StatusResponse(ok=False, message="缺集待补剧集严禁删除源端整剧，仅支持推送补缺")
    return StatusResponse(
        ok=False,
        message="安全拦截：异常剧集模块已禁止整剧级删除父目录，仅支持单季级受控清理",
    )
    c_info = json.loads(rec.conflict_json or "{}") if getattr(rec, "conflict_json", None) else {}
    if isinstance(c_info, dict) and c_info.get("blocking"):
        return StatusResponse(ok=False, message="存在作品身份冲突，安全拦截删除源端目录")

    cd2 = CloudDrive2Client()
    try:
        blocked_group = await find_blocking_group_for_record(
            db, rec, cd2_client=cd2, operation="delete_source_show"
        )
        if blocked_group is not None:
            await db.commit()
            return StatusResponse(
                ok=False,
                message=(
                    f"存在未确认的{blocked_group.title or '同 ID 多目录组'}（TMDB {blocked_group.tmdb_id}），"
                    f"安全拦截删除源端目录：{blocked_group.invalidated_reason or '请先确认归档目标或标记有意共存'}"
                ),
            )
        # 1. 物理删除源端剧集完整目录（移至回收站）
        log.warning("用户主动请求删除源端剧集目录: %s (关联记录: %s)", show_path, rec.id)
        await cd2.delete_file(show_path)

        # 2. 刷新父级目录（强制同步网盘最新文件列表）
        parent_dir = show_path.rsplit("/", 1)[0]
        try:
            await cd2.list_files(parent_dir, force_refresh=True)
            log.info("已成功强制刷新源端父级目录: %s", parent_dir)
        except Exception as ref_err:
            log.warning("强制刷新源端父级目录失败: %s - %s", parent_dir, ref_err)

        # 3. 从数据库中级联清理同属该 task_id 和该 show_path 的所有季记录
        stmt = select(SeasonRecord).where(
            SeasonRecord.task_id == rec.task_id,
            SeasonRecord.show_path == show_path,
        )
        show_recs = (await db.scalars(stmt)).all()
        for sr in show_recs:
            await db.delete(sr)

        task_id = rec.task_id
        await _refresh_task_summary(task_id, db)
        await db.commit()

        # 4. 记录审计日志 (F04 修复字段模型定义)
        op_log = OperationLog(
            task_id=task_id,
            season_record_id=rec.id,
            action="delete_source_show",
            status="completed",
            detail_json=json.dumps({
                "show_name": rec.show_name,
                "deleted_show_path": show_path,
                "refreshed_parent": parent_dir,
            }, ensure_ascii=False),
        )
        db.add(op_log)
        await db.commit()

        return StatusResponse(
            ok=True,
            message=f"已成功删除源端剧集目录: {rec.show_name}，并完成源路径强制刷新！"
        )
    except Exception as e:
        log.error("删除源端剧集失败 %s: %s", show_path, e)
        return StatusResponse(ok=False, message=f"删除源端失败: {str(e)[:200]}")
    finally:
        await cd2.close()


def _fmt_bytes(size_bytes: int | None) -> str:
    if not size_bytes or size_bytes <= 0:
        return "0 B"
    gb = 1024 ** 3
    mb = 1024 ** 2
    if size_bytes >= gb:
        return f"{size_bytes / gb:.2f} GiB"
    return f"{size_bytes / mb:.1f} MiB"


class DeleteSourceSeasonPreviewRequest(BaseModel):
    season_record_id: str


class DeleteSourceSeasonRequest(BaseModel):
    season_record_id: str
    confirm_abandon: bool = True


@router.post("/delete-source-season/preview")
async def preview_delete_source_season(
    body: DeleteSourceSeasonPreviewRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """预览单季源端删除影响 — 用于放弃补缺时，展示将失去的正片、附件及活跃任务状态。"""
    rec = await db.get(SeasonRecord, body.season_record_id)
    if not rec:
        return {"ok": False, "message": "剧季记录不存在"}

    from app.core.season_classifier import get_record_work_main_category
    work_cat = await get_record_work_main_category(db, rec)
    if work_cat == "airing" or rec.status == "airing":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="当前作品季处于「连载留源」状态，连载期严禁执行单季删源"
        )

    if rec.status != "already_archived":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"「缺集待补」已取消删除源端入口，单季清源仅限「待复核清理」中经核验通过的归档已齐季（already_archived）执行，当前状态为: {rec.status}"
        )

    season_path = rec.season_path
    if not season_path or len(season_path.strip("/").split("/")) < 2:
        return {"ok": False, "message": f"非法或不安全的剧季源路径: {season_path}"}

    settings = get_settings()
    norm_season_path = posixpath.normpath(season_path)
    source_root = settings.source_root.rstrip("/")
    norm_source_root = posixpath.normpath(source_root) if source_root else ""
    archive_root = settings.archive_root.rstrip("/")
    norm_archive_root = posixpath.normpath(archive_root) if archive_root else ""

    if not norm_source_root or not (norm_season_path.startswith(norm_source_root + "/") and norm_season_path != norm_source_root):
        return {"ok": False, "message": f"目标路径不在受控源目录下或属于根目录: {season_path}"}
    if norm_archive_root and (norm_season_path == norm_archive_root or norm_season_path.startswith(norm_archive_root + "/")):
        return {"ok": False, "message": f"严重危险拦截：目标路径属于归档库根目录或其子路径: {season_path}"}

    stmt_shared = select(SeasonRecord).where(
        SeasonRecord.task_id == rec.task_id,
        SeasonRecord.season_path == rec.season_path,
        SeasonRecord.id != rec.id,
    )
    shared_recs = list((await db.scalars(stmt_shared)).all())
    if shared_recs:
        other_seasons = [f"S{s.season_number:02d}" for s in shared_recs]
        return {
            "ok": False,
            "message": f"该路径被多个季度共享 ({', '.join(other_seasons)})，为平铺整剧目录，禁止按季单独删除目录以防误伤其他季正片！",
        }

    # 检查当前任务或数据库中该剧目是否已无其他有效剧季记录
    stmt_other = (
        select(SeasonRecord).where(
            SeasonRecord.show_path == rec.show_path,
            SeasonRecord.id != rec.id,
        )
        if rec.show_path
        else select(SeasonRecord).where(
            SeasonRecord.task_id == rec.task_id,
            SeasonRecord.show_name == rec.show_name,
            SeasonRecord.id != rec.id,
        )
    )
    other_recs = list((await db.scalars(stmt_other)).all())
    active_other_seasons = [s for s in other_recs if s.status != "source_absent"]

    task = await db.get(Task, rec.task_id)
    task_source_path = posixpath.normpath(task.source_path) if (task and task.source_path) else ""

    will_delete_show_dir = False
    target_delete_path = norm_season_path
    if len(active_other_seasons) == 0 and rec.show_path:
        norm_show_path = posixpath.normpath(rec.show_path)
        if (
            norm_source_root
            and norm_show_path.startswith(norm_source_root + "/")
            and norm_show_path != norm_source_root
            and norm_show_path != task_source_path
            and not (norm_archive_root and (norm_show_path == norm_archive_root or norm_show_path.startswith(norm_archive_root + "/")))
            and len(norm_show_path.strip("/").split("/")) >= 2
        ):
            will_delete_show_dir = True
            target_delete_path = norm_show_path

    cd2 = CloudDrive2Client()
    files_list: list[dict] = []
    total_size = 0
    video_files: list[dict] = []
    sub_files: list[dict] = []
    extra_files: list[dict] = []
    present_eps: set[int] = set()

    try:
        raw_files = await cd2.list_files(season_path, force_refresh=True)
        for f in raw_files:
            if f.get("isDirectory"):
                continue
            fname = str(f.get("name") or "")
            fsize = int(f.get("size") or 0)
            total_size += fsize
            fitem = {"name": fname, "size": fsize, "size_display": _fmt_bytes(fsize)}
            files_list.append(fitem)
            if is_video_file(fname):
                video_files.append(fitem)
                ep_obj = parse_episode(fname, f.get("path", ""), fsize)
                if _verified_video(ep_obj, rec.season_number):
                    present_eps.update(ep_obj.episode_numbers)
            elif is_subtitle_file(fname):
                sub_files.append(fitem)
            else:
                extra_files.append(fitem)
    except Exception as list_err:
        log.warning("读取源端季文件清单异常: %s", list_err)
    finally:
        await cd2.close()

    active_downloads: list[dict] = []
    if settings.mp_url and settings.mp_token:
        try:
            from app.clients.moviepilot import MoviePilotClient
            mp = MoviePilotClient()
            try:
                dls = await mp.get_active_downloads()
                if isinstance(dls, list):
                    for dl in dls:
                        dl_title = str(dl.get("title") or dl.get("name") or "")
                        dl_tid = str(dl.get("tmdb_id") or dl.get("tmdbid") or "")
                        if rec.tmdb_id and dl_tid and str(rec.tmdb_id) == dl_tid:
                            active_downloads.append({"title": dl_title, "id": dl.get("id") or dl.get("download_id")})
                        elif rec.show_name and rec.show_name in dl_title:
                            active_downloads.append({"title": dl_title, "id": dl.get("id") or dl.get("download_id")})
            finally:
                await mp.close()
        except Exception:
            pass

    sorted_present = sorted(present_eps)
    loss_msg = (
        f"源端本季现有 {len(sorted_present)} 集正片（{', '.join(f'E{e:02d}' for e in sorted_present[:10])}{'...' if len(sorted_present) > 10 else ''}），"
        f"删除后将彻底失去这 {len(sorted_present)} 集文件！"
        if sorted_present else "源端本季未发现有效正片文件（仅有残余非视频或空目录）"
    )

    warnings_list = [loss_msg] if loss_msg else []
    if will_delete_show_dir:
        warnings_list.append(
            f"检测到该剧目在当前任务/数据库中已无其他有效剧季，本次将直接删除整剧目录【{target_delete_path}】"
            f"（彻底清除海报、nfo等刮削伴生文件），删除后将在初检目录【{task_source_path or posixpath.dirname(target_delete_path)}】中执行 CloudDrive2 强制刷新。"
        )

    payload = {
        "season_record_id": rec.id,
        "show_name": rec.show_name,
        "season_number": rec.season_number,
        "season_path": rec.season_path,
        "show_path": rec.show_path or "",
        "task_source_path": task_source_path,
        "will_delete_show_dir": will_delete_show_dir,
        "target_delete_path": target_delete_path,
        "active_other_seasons_count": len(active_other_seasons),
        "archive_target_path": rec.archive_target_path or "",
        "archive_impact": "none",
        "archive_impact_note": "仅删除源端本季范围，归档库与同剧其他季目录不受影响",
        "total_files": len(files_list),
        "total_size_bytes": total_size,
        "total_size_display": _fmt_bytes(total_size),
        "video_count": len(video_files),
        "sub_count": len(sub_files),
        "extra_count": len(extra_files),
        "file_stats": {
            "video_count": len(video_files),
            "sub_count": len(sub_files),
            "other_count": len(extra_files),
            "total_size_bytes": total_size,
            "total_size_display": _fmt_bytes(total_size),
        },
        "present_episodes": sorted_present,
        "episodes": [f"S{rec.season_number:02d}E{ep:02d}" for ep in sorted_present],
        "missing_episodes": json.loads(rec.missing_episodes_json or "[]"),
        "has_active_downloads": len(active_downloads) > 0,
        "has_active_mp_downloads": len(active_downloads) > 0,
        "active_downloads": active_downloads,
        "loss_warning": loss_msg,
        "warnings": warnings_list,
    }

    return {
        "ok": True,
        **payload,
        "data": payload,
    }


@router.post("/delete-source-season", response_model=StatusResponse)
async def delete_source_season(
    body: DeleteSourceSeasonRequest,
    db: AsyncSession = Depends(get_db),
) -> StatusResponse:
    """彻底删除源端本季目录及其所有文件，放弃补缺并结案。"""
    rec = await db.get(SeasonRecord, body.season_record_id)
    if not rec:
        return StatusResponse(ok=False, message="剧季记录不存在")

    from app.core.season_classifier import get_record_work_main_category
    work_cat = await get_record_work_main_category(db, rec)
    if work_cat == "airing" or rec.status == "airing":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="当前作品季处于「连载留源」状态，连载期严禁执行单季删源"
        )

    if rec.status != "already_archived":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"「缺集待补」已取消删除源端入口，单季清源仅限「待复核清理」中经核验通过的归档已齐季（already_archived）执行，当前状态为: {rec.status}"
        )

    if not body.confirm_abandon:
        return StatusResponse(ok=False, message="未确认执行源端清理")

    # 统一委派至归档已齐安全清源引擎（先保全独有加更/加长/Vlog/字幕 -> 核验归档 -> 回收源季 -> 空壳清理 -> 刷新 -> 结案）
    return await clean_source_redundant(_ArchiveRequest(season_record_ids=[rec.id]), db)

    season_path = rec.season_path
    if not season_path or len(season_path.strip("/").split("/")) < 2:
        return StatusResponse(ok=False, message=f"非法或不安全的剧季源路径: {season_path}")

    settings = get_settings()
    norm_season_path = posixpath.normpath(season_path)
    source_root = settings.source_root.rstrip("/")
    norm_source_root = posixpath.normpath(source_root) if source_root else ""
    archive_root = settings.archive_root.rstrip("/")
    norm_archive_root = posixpath.normpath(archive_root) if archive_root else ""

    if not norm_source_root or not (norm_season_path.startswith(norm_source_root + "/") and norm_season_path != norm_source_root):
        return StatusResponse(ok=False, message=f"目标路径不在受控源目录下或属于根目录: {season_path}")
    if norm_archive_root and (norm_season_path == norm_archive_root or norm_season_path.startswith(norm_archive_root + "/")):
        return StatusResponse(ok=False, message=f"严重危险拦截：目标路径属于归档库根目录或其子路径: {season_path}")

    stmt_shared = select(SeasonRecord).where(
        SeasonRecord.task_id == rec.task_id,
        SeasonRecord.season_path == rec.season_path,
        SeasonRecord.id != rec.id,
    )
    shared_recs = list((await db.scalars(stmt_shared)).all())
    if shared_recs:
        other_seasons = [f"S{s.season_number:02d}" for s in shared_recs]
        return StatusResponse(
            ok=False,
            message=f"该路径被多个季度共享 ({', '.join(other_seasons)})，为平铺整剧目录，严禁按季删除整剧目录！",
        )

    # 检查当前任务或数据库中该剧目是否已无其他有效剧季记录
    stmt_other = (
        select(SeasonRecord).where(
            SeasonRecord.show_path == rec.show_path,
            SeasonRecord.id != rec.id,
        )
        if rec.show_path
        else select(SeasonRecord).where(
            SeasonRecord.task_id == rec.task_id,
            SeasonRecord.show_name == rec.show_name,
            SeasonRecord.id != rec.id,
        )
    )
    other_recs = list((await db.scalars(stmt_other)).all())
    active_other_seasons = [s for s in other_recs if s.status != "source_absent"]

    task = await db.get(Task, rec.task_id)
    task_source_path = posixpath.normpath(task.source_path) if (task and task.source_path) else ""

    will_delete_show_dir = False
    target_delete_path = norm_season_path
    delete_type = "season_dir"

    if len(active_other_seasons) == 0 and rec.show_path:
        norm_show_path = posixpath.normpath(rec.show_path)
        if (
            norm_source_root
            and norm_show_path.startswith(norm_source_root + "/")
            and norm_show_path != norm_source_root
            and norm_show_path != task_source_path
            and not (norm_archive_root and (norm_show_path == norm_archive_root or norm_show_path.startswith(norm_archive_root + "/")))
            and len(norm_show_path.strip("/").split("/")) >= 2
        ):
            will_delete_show_dir = True
            target_delete_path = norm_show_path
            delete_type = "show_dir"

    cd2 = CloudDrive2Client()
    try:
        blocked_group = await find_blocking_group_for_record(
            db, rec, cd2_client=cd2, operation="delete_source_season"
        )
        if blocked_group is not None:
            await db.commit()
            return StatusResponse(
                ok=False,
                message=(
                    f"存在未确认的{blocked_group.title or '同 ID 多目录组'}（TMDB {blocked_group.tmdb_id}），"
                    f"安全拦截删除源端目录：{blocked_group.invalidated_reason or '请先确认归档目标或标记有意共存'}"
                ),
            )

        log.warning(
            "用户主动放弃补缺，删除%s: %s (关联记录: %s, 无其他有效季=%s)",
            "整剧目录" if delete_type == "show_dir" else "源端本季目录",
            target_delete_path,
            rec.id,
            will_delete_show_dir,
        )
        await cd2.delete_file(target_delete_path)

        # 删除后在该任务的初检目录中执行CloudDrive2的刷新操作
        refresh_dir = task_source_path or posixpath.dirname(target_delete_path)
        try:
            await cd2.list_files(refresh_dir, force_refresh=True)
            log.info("已成功在初检目录强制刷新 CloudDrive2 缓存: %s", refresh_dir)
        except Exception as ref_err:
            log.warning("强制刷新初检目录缓存失败: %s - %s", refresh_dir, ref_err)

        rec.status = "source_absent"
        if delete_type == "show_dir":
            rec.status_detail = f"用户主动放弃补缺，该剧目已无其他有效季，已直接删除整剧目录 ({target_delete_path})"
        else:
            rec.status_detail = f"用户主动放弃补缺，已删除源端本季目录 ({target_delete_path})"

        from app.models.gap_fill import GapFillEpisode
        ep_rows = list((await db.scalars(
            select(GapFillEpisode).where(
                GapFillEpisode.task_id == rec.task_id,
                GapFillEpisode.season_path == season_path.rstrip("/"),
            )
        )).all())
        for ep in ep_rows:
            ep.state = "cancelled_or_failed"
            ep.outcome_code = "user_abandoned_deleted"
            ep.reason = "用户主动放弃补缺并删除源端目录"

        task_id = rec.task_id
        await _refresh_task_summary(task_id, db)
        await db.commit()

        op_log = OperationLog(
            task_id=task_id,
            season_record_id=rec.id,
            action="delete_source_season",
            status="completed",
            detail_json=json.dumps({
                "show_name": rec.show_name,
                "season_number": rec.season_number,
                "season_path": season_path,
                "deleted_path": target_delete_path,
                "delete_type": delete_type,
                "will_delete_show_dir": will_delete_show_dir,
                "refreshed_dir": refresh_dir,
                "task_source_path": task_source_path,
                "reason": "user_abandoned_deleted",
            }, ensure_ascii=False),
        )
        db.add(op_log)
        await db.commit()

        success_msg = (
            f"已成功删除【{rec.show_name}】整剧目录并在初检目录刷新云盘缓存！"
            if delete_type == "show_dir"
            else f"已成功删除【{rec.show_name} S{rec.season_number:02d}】源端目录并在初检目录刷新云盘缓存！"
        )
        return StatusResponse(
            ok=True,
            message=success_msg,
        )
    except Exception as e:
        log.error("删除源端本季目录失败 %s: %s", season_path, e)
        return StatusResponse(ok=False, message=f"删除源端本季目录失败: {str(e)[:200]}")
    finally:
        await cd2.close()


async def _verified_deleted_names(db: AsyncSession, record_ids: list[str]) -> dict[str, set[str]]:
    """一次读取成功实物核验的清理回执，用于辨认历史残留附件。"""
    result: dict[str, set[str]] = {}
    if not record_ids:
        return result
    logs = (await db.scalars(select(OperationLog).where(
        OperationLog.season_record_id.in_(record_ids),
        OperationLog.action == "clean_duplicate",
        OperationLog.status == "completed",
    ))).all()
    for row in logs:
        try:
            detail = json.loads(row.detail_json or "{}")
        except (ValueError, TypeError):
            continue
        if detail.get("verification_passed") is not True:
            continue
        names = result.setdefault(row.season_record_id, set())
        names.update(posixpath.basename(p) for p in detail.get("deleted_paths", []) if is_video_file(p))
    return result


# ---------- 统一批量「执行推荐处理」预览与闭环执行 (v2.5.45 · Section 9 / G23 / G24 / G28) ----------

_RECOMMENDED_BATCH_PLANS: dict[str, dict[str, Any]] = {}


class RecommendedPlanRequest(BaseModel):
    task_id: str
    season_record_ids: list[str] | None = None
    group_ids: list[str] | None = None
    mode: str = "all"  # "all" | "merge_directory" | "redundant_clean"


class RecommendedExecuteRequest(BaseModel):
    task_id: str
    plan_id: str


@router.post("/recommended-plan")
async def build_recommended_batch_plan(
    body: RecommendedPlanRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """为当前任务（或选定项目）生成「执行推荐处理」统一批量预览计划（不执行写入）。

    汇总三类可执行动作：
    1. 同 ID 目录规范化归并 (merge_items)
    2. 同内容副本清理 (clean_items)
    3. 归档已齐源端清理 (redundant_source_items)
    分别统计作品季数、目录组数、文件数和预计可回收容量，避免重复计数。
    """
    from uuid import uuid4
    from app.core.directory_merge import build_directory_merge_plan
    from app.core.season_classifier import TERMINAL_STATUSES, consolidate_seasons_to_work_records
    from app.models.directory_group import DirectoryGroup

    task = await db.get(Task, body.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    mode = str(body.mode or "all").strip().lower()
    if mode not in ("all", "merge_directory", "redundant_clean"):
        raise HTTPException(status_code=400, detail=f"不支持的批量处理模式: {mode}")
    include_merge = mode in ("all", "merge_directory")
    include_clean = mode in ("all", "redundant_clean")

    rows = list((await db.scalars(
        select(SeasonRecord).where(
            SeasonRecord.task_id == body.task_id,
            SeasonRecord.status.not_in(tuple(TERMINAL_STATUSES)),
        )
    )).all())
    pending_groups = list((await db.scalars(
        select(DirectoryGroup).where(
            DirectoryGroup.task_id == body.task_id,
            DirectoryGroup.status == "pending_confirm",
        )
    )).all())
    pending_dg_ids = {g.id for g in pending_groups}

    work_records, _ = consolidate_seasons_to_work_records(rows, pending_dg_ids)
    review_works = [w for w in work_records if w.get("main_category") == "review"]
    if body.season_record_ids:
        sel_set = set(body.season_record_ids)
        review_works = [
            w for w in review_works
            if w.get("id") in sel_set or any(pid in sel_set for pid in (w.get("participating_record_ids") or []))
        ]

    merge_items: list[dict[str, Any]] = []
    clean_items: list[dict[str, Any]] = []
    redundant_source_items: list[dict[str, Any]] = []
    skipped_items: list[dict[str, Any]] = []

    cd2 = CloudDrive2Client()
    sem = asyncio.Semaphore(10)
    plans_to_persist: list[tuple[DirectoryGroup, dict[str, Any]]] = []
    try:
        # 1. 同 ID 多目录规范化归并组级计划 (G06-G08) — 并发只读盘点，避免持锁阻塞 SQLite
        target_groups = pending_groups if include_merge else []
        if include_merge and body.group_ids:
            g_sel = set(body.group_ids)
            target_groups = [g for g in pending_groups if g.id in g_sel]

        async def _plan_one_group(grp: DirectoryGroup) -> tuple[DirectoryGroup, dict[str, Any] | None, str | None]:
            async with sem:
                try:
                    m_plan = await asyncio.wait_for(
                        build_directory_merge_plan(
                            db,
                            body.task_id,
                            grp.id,
                            all_seasons=True,
                            auto_canonical_target=True,
                            smart_collision=True,
                            cd2_client=cd2,
                            persist_plan=False,
                            force_refresh=False,
                            task_obj=task,
                            group_obj=grp,
                            prefetched_season_records=rows,
                        ),
                        timeout=20.0,
                    )
                    return grp, m_plan, None
                except asyncio.TimeoutError:
                    return grp, None, "读取目录超时 (20s)，请稍后重试或单独处理"
                except Exception as mg_err:
                    return grp, None, str(mg_err)

        group_results = await asyncio.gather(*(_plan_one_group(g) for g in target_groups))
        for grp, m_plan, err_msg in group_results:
            entries = json.loads(grp.paths_json or "[]")
            work_title = next((r.show_name for r in rows if str(r.tmdb_id) == str(grp.tmdb_id)), "")
            group_title = work_title or next((e.get("raw_name") for e in entries if e.get("raw_name")), f"TMDB {grp.tmdb_id}")
            group_context = {"tmdb_id": grp.tmdb_id, "paths": [e.get("norm_path") for e in entries if e.get("norm_path")]}
            if err_msg or m_plan is None:
                skipped_items.append({
                    "kind": "merge_directory",
                    "group_id": grp.id,
                    "title": group_title,
                    **group_context,
                    "reason": err_msg or "未知错误",
                    "reason_type": "read_error",
                    "next_action": "恢复目录读取后重新预览",
                })
            elif m_plan.get("can_execute") and (m_plan.get("files_to_move") or m_plan.get("collision_dedup_items") or m_plan.get("assets_to_preserve") or m_plan.get("directories_to_clean") or m_plan.get("junk_files_to_clean") or m_plan.get("align_only")):
                plans_to_persist.append((grp, m_plan))
                merge_items.append({
                    "group_id": grp.id,
                    "title": group_title,
                    "tmdb_id": grp.tmdb_id,
                    "plan_id": m_plan["plan_id"],
                    "canonical_dir_name": m_plan.get("canonical_dir_name") or "",
                    "naming_basis": m_plan.get("naming_basis") or "",
                    "source_target_show_path": m_plan["source_target_show_path"],
                    "source_target_will_create": bool(m_plan.get("source_target_will_create")),
                    "merge_from_show_paths": m_plan.get("merge_from_show_paths") or [],
                    "affected_seasons": m_plan.get("affected_seasons") or [1],
                    "file_count": len(m_plan.get("files_to_move") or []),
                    "asset_count": len(m_plan.get("assets_to_preserve") or []),
                    "empty_dir_count": len(m_plan.get("directories_to_clean") or []),
                    "junk_file_count": len(m_plan.get("junk_files_to_clean") or []),
                    "is_empty_shell_cleanup": bool(m_plan.get("is_empty_shell_cleanup")),
                    "align_only": bool(m_plan.get("align_only")),
                    "changes_season_files": bool(m_plan.get("files_to_move") or m_plan.get("collision_dedup_items")),
                    "dedup_count": len(m_plan.get("collision_dedup_items") or []),
                    "isolated_conflicts": m_plan.get("isolated_conflicts") or [],
                    "prefix_selection_basis": m_plan.get("prefix_selection_basis") or "",
                })
            else:
                isolated = m_plan.get("isolated_conflicts") or []
                deferred = m_plan.get("deferred_active_downloads") or []
                reasons = m_plan.get("block_reasons") or []
                reason_type = "directory_blocked"
                if not reasons and isolated:
                    reasons = list(dict.fromkeys(i.get("reason") or "文件归属待核实" for i in isolated))
                    reason_type = "file_conflict"
                elif not reasons and deferred:
                    reasons = [f"{len(deferred)} 个文件仍有下载写入，稳定文件已独立处理"]
                    reason_type = "active_download"
                skipped_items.append({
                    "kind": "merge_directory",
                    "group_id": grp.id,
                    "title": group_title,
                    **group_context,
                    "reason": "; ".join(reasons[:3]) or "未生成有效目录动作，需重新读取本组文件清单",
                    "reason_type": reason_type,
                    "affected_files": [{"name":i.get("filename"), "path":i.get("source_path"), "reason":i.get("reason")} for i in (isolated+deferred)],
                    "next_action": "下载结束后更新预览" if deferred and not isolated else "查看本作品的具体文件与路径",
                })

        # 2. 同内容副本清理 & 归档已齐清源 — 并发盘点文件指纹
        clean_candidates: list[dict[str, Any]] = []
        row_by_id = {r.id: r for r in rows}
        executable_group_ids = {m["group_id"] for m in merge_items}

        def pending_group_for_work(work):
            row = row_by_id.get(work.get("id"))
            if row is None:
                return None
            for group in pending_groups:
                entries = json.loads(group.paths_json or "[]")
                if any(normalize_dir_path(e.get("norm_path")) == normalize_dir_path(row.show_path) for e in entries):
                    return group
            return None

        for w in (review_works if include_clean else []):
            rec_id = w.get("id")
            if not rec_id:
                continue
            r_kind = w.get("review_action_kind") or ""
            if r_kind == "clean_redundant_source" or w.get("status") == "already_archived":
                group = pending_group_for_work(w)
                if group is not None and group.id not in executable_group_ids:
                    skipped_items.append({"kind": "clean_redundant_source", "season_record_id": rec_id,
                        "title": f"{w.get('show_name')} S{int(w.get('season_number') or 1):02d}",
                        "reason": "等待同作品目录归并完成后再核验清源条件", "reason_type": "dependency",
                        "next_action": "请先点击「批量处理多目录问题」完成目录归并" if mode == "redundant_clean" else "先解决目录组阻塞",
                        "group_id": group.id})
                    continue
                redundant_source_items.append({
                    "season_record_id": rec_id,
                    "show_name": w.get("show_name") or "",
                    "season_number": int(w.get("season_number") or 1),
                    "season_path": w.get("season_path") or "",
                    "archive_target_path": w.get("archive_target_path") or "",
                    "predicted_next_label": "源端已清理，归档已保留",
                })
                continue

            if "clean_duplicates" in (w.get("review_actions") or []) or r_kind == "clean_duplicates":
                row = row_by_id.get(rec_id)
                reason = _duplicate_cleanup_block_reason(row) if row else "季记录已不存在"
                if reason:
                    skipped_items.append({"kind": "clean_duplicates", "season_record_id": rec_id,
                        "title": w.get("show_name"), "reason": reason, "reason_type": "fact_conflict",
                        "next_action": "核实具体冲突事实后自动重算"})
                else:
                    clean_candidates.append(w)

        deleted_history = await _verified_deleted_names(db, [w["id"] for w in clean_candidates])

        async def _plan_one_clean(w: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
            async with sem:
                try:
                    live_files = await asyncio.wait_for(
                        cd2.list_files(w.get("season_path") or "", force_refresh=True),
                        timeout=15.0,
                    )
                    c_plan = generate_review_clean_plan(
                        verified_deleted_video_names=deleted_history.get(w["id"], set()),
                        files=live_files,
                        season_number=int(w.get("season_number") or 1),
                        total_episodes=int(w.get("total_episodes") or len(live_files)),
                    )
                    return w, c_plan, None
                except asyncio.TimeoutError:
                    return w, None, "读取目录超时 (15s)"
                except Exception as cl_err:
                    return w, None, str(cl_err)

        clean_results = await asyncio.gather(*(_plan_one_clean(w) for w in clean_candidates))
        for w, c_plan, err_msg in clean_results:
            rec_id = w.get("id")
            if err_msg or c_plan is None:
                skipped_items.append({
                    "kind": "clean_duplicates",
                    "season_record_id": rec_id,
                    "title": f"{w.get('show_name')} S{int(w.get('season_number') or 1):02d}",
                    "reason": err_msg or "未知错误",
                })
                continue
            if c_plan.get("can_clean") or c_plan.get("all_distinct_retained") or (c_plan.get("status") == "clean_done" and not c_plan.get("unparsed_videos") and not c_plan.get("preserved_subdirs") and not c_plan.get("deferred_files")):
                to_del = c_plan.get("to_delete") or []
                to_keep = c_plan.get("to_keep") or []
                clean_items.append({
                    "season_record_id": rec_id,
                    "show_name": w.get("show_name") or "",
                    "season_number": int(w.get("season_number") or 1),
                    "season_path": w.get("season_path") or "",
                    "plan_id": c_plan.get("plan_id") or "",
                    "rules_version": c_plan.get("rules_version") or "v2.5.54",
                    "can_clean": bool(c_plan.get("can_clean")),
                    "all_distinct_retained": bool(c_plan.get("all_distinct_retained")),
                    "distinct_retention_note": str(c_plan.get("distinct_retention_note") or ""),
                    "version_note": str(c_plan.get("version_note") or ""),
                    "to_delete": to_del,
                    "to_keep": to_keep,
                    "delete_count": len(to_del),
                    "keep_count": len(to_keep),
                    "reclaimable_bytes": int(c_plan.get("reclaimable_bytes") or 0),
                    "file_fingerprints": c_plan.get("file_fingerprints") or {},
                    "predicted_next_category": w.get("predicted_next_category") or "",
                    "predicted_next_label": w.get("predicted_next_category_label") or "",
                })
            else:
                skipped_items.append({"kind": "clean_duplicates", "season_record_id": rec_id,
                    "title": f"{w.get('show_name')} S{int(w.get('season_number') or 1):02d}",
                    "reason": c_plan.get("reason") or "尚无可安全执行的清理动作",
                    "reason_type": "deferred" if c_plan.get("deferred_files") else "inventory_blocked"})
    finally:
        await cd2.close()

    # 3. 并发盘点全部完成后再一次性写入 group.evidence_json 并立刻提交（持锁 <50ms）
    for grp, m_plan in plans_to_persist:
        try:
            grp_ev = json.loads(grp.evidence_json or "{}")
        except Exception:
            grp_ev = {}
        grp_ev["latest_merge_plan"] = m_plan
        grp.evidence_json = json.dumps(grp_ev, ensure_ascii=False)

    work_season_ids = {c["season_record_id"] for c in clean_items} | {r["season_record_id"] for r in redundant_source_items}
    total_files_affected = (
        sum(m["file_count"] + m["dedup_count"] + m.get("asset_count", 0) for m in merge_items)
        + sum(c["delete_count"] for c in clean_items)
    )
    total_reclaimable_bytes = sum(c["reclaimable_bytes"] for c in clean_items)

    empty_shell_count = sum(1 for m in merge_items if m.get("is_empty_shell_cleanup"))

    # 按模式精确计算本批次范围记录，防止多目录模式与冗余清理模式结果互相串扰
    if mode == "merge_directory":
        merge_group_paths: set[str] = set()
        for grp in target_groups:
            for e in json.loads(grp.paths_json or "[]"):
                if e.get("norm_path"):
                    merge_group_paths.add(normalize_dir_path(e["norm_path"]))
        scoped_works = [
            w for w in work_records
            if normalize_dir_path(w.get("show_path") or "") in merge_group_paths
            or any(
                normalize_dir_path(getattr(row_by_id.get(pid), "show_path", "") or "") in merge_group_paths
                for pid in (w.get("participating_record_ids") or [])
            )
        ]
        scope_record_ids = sorted({
            rid
            for w in scoped_works
            for rid in [w["id"], *(w.get("participating_record_ids") or [])]
        })
        before_categories = {w["id"]: w.get("main_category") for w in scoped_works}
    elif mode == "redundant_clean":
        clean_scoped_ids = (
            {c["season_record_id"] for c in clean_items}
            | {r["season_record_id"] for r in redundant_source_items}
            | {s["season_record_id"] for s in skipped_items if s.get("season_record_id")}
        )
        scoped_works = [
            w for w in review_works
            if w.get("id") in clean_scoped_ids
            or bool(set(w.get("participating_record_ids") or []) & clean_scoped_ids)
        ]
        scope_record_ids = sorted(
            clean_scoped_ids
            | {rid for w in scoped_works for rid in [w["id"], *(w.get("participating_record_ids") or [])]}
        )
        before_categories = {w["id"]: w.get("main_category") for w in scoped_works}
    else:
        scope_record_ids = sorted({rid for w in review_works for rid in [w["id"], *(w.get("participating_record_ids") or [])]})
        before_categories = {w["id"]: w.get("main_category") for w in review_works}

    plan_id = f"plan_rec_{uuid4().hex[:12]}"
    plan_payload = {
        "ok": True,
        "plan_id": plan_id,
        "mode": mode,
        "rules_version": "v2.5.54",
        "task_id": body.task_id,
        "created_at": datetime.now().isoformat(),
        "counts": {
            "work_season_count": len(work_season_ids),
            "dir_group_count": len(merge_items),
            "empty_shell_count": empty_shell_count,
            "file_count": total_files_affected,
            "reclaimable_bytes": total_reclaimable_bytes,
            "reclaimable_display": _fmt_bytes(total_reclaimable_bytes),
            "merge_count": len(merge_items),
            "clean_count": len(clean_items),
            "redundant_source_count": len(redundant_source_items),
        },
        "merge_items": merge_items,
        "clean_items": clean_items,
        "redundant_source_items": redundant_source_items,
        "skipped_items": skipped_items,
        "step_receipts": {},
        "scope_record_ids": scope_record_ids,
        "before_categories": before_categories,
    }
    _RECOMMENDED_BATCH_PLANS[plan_id] = plan_payload
    batch_journal.save(plan_id, plan_payload)
    await db.commit()

    # 返回精简摘要给前端弹窗渲染，避免超大文件指纹载荷导致浏览器解析/渲染卡顿
    return {
        "ok": True,
        "plan_id": plan_id,
        "mode": mode,
        "rules_version": "v2.5.54",
        "task_id": body.task_id,
        "created_at": plan_payload["created_at"],
        "counts": plan_payload["counts"],
        "merge_items": [
            {
                "group_id": m["group_id"],
                "title": m["title"],
                "canonical_dir_name": m["canonical_dir_name"],
                "affected_seasons": m.get("affected_seasons") or [],
                "file_count": m["file_count"],
                "dedup_count": m["dedup_count"],
                "asset_count": m.get("asset_count", 0),
                "empty_dir_count": m.get("empty_dir_count", 0),
                "junk_file_count": m.get("junk_file_count", 0),
                "is_empty_shell_cleanup": bool(m.get("is_empty_shell_cleanup")),
                "align_only": m.get("align_only", False),
            }
            for m in merge_items
        ],
        "clean_items": [
            {
                "season_record_id": c["season_record_id"],
                "show_name": c["show_name"],
                "season_number": c["season_number"],
                "delete_count": c["delete_count"],
                "keep_count": c["keep_count"],
                "reclaimable_bytes": c["reclaimable_bytes"],
                "predicted_next_label": c["predicted_next_label"],
            }
            for c in clean_items
        ],
        "redundant_source_items": [
            {
                "season_record_id": r["season_record_id"],
                "show_name": r["show_name"],
                "season_number": r["season_number"],
                "predicted_next_label": r["predicted_next_label"],
            }
            for r in redundant_source_items
        ],
        "skipped_items": skipped_items,
    }


_RECOMMENDED_EXECUTE_JOBS: dict[str, dict[str, Any]] = {}


def _build_recommended_job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": job.get("status") != "failed",
        "job_id": job["job_id"],
        "plan_id": job["plan_id"],
        "task_id": job["task_id"],
        "status": job["status"],
        "phase": job.get("phase", ""),
        "message": job.get("message", ""),
        "done_steps": int(job.get("done_steps") or 0),
        "total_steps": int(job.get("total_steps") or 0),
        "current_item": job.get("current_item", ""),
        "result": job.get("result"),
        "error": job.get("error"),
    }


_RECOMMENDED_TASK_LOCKS: dict[str, asyncio.Lock] = {}


async def _run_recommended_batch_plan(body, db, *, progress_cb=None):
    lock = _RECOMMENDED_TASK_LOCKS.setdefault(body.task_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(status_code=409, detail="该任务已有推荐处理正在执行")
    async with lock:
        return await _run_recommended_batch_plan_locked(body, db, progress_cb=progress_cb)


async def _run_recommended_batch_plan_locked(
    body: RecommendedExecuteRequest,
    db: AsyncSession,
    *,
    progress_cb: Any | None = None,
) -> dict[str, Any]:
    """执行已确认预览的「执行推荐处理」批次计划，支持并发指纹复核 (G24)、合并与清理联动、幂等恢复 (G23) 与四类去向汇总 (G28)。"""
    from app.core.directory_merge import execute_directory_merge
    from app.core.season_classifier import TERMINAL_STATUSES, consolidate_seasons_to_work_records
    from app.models.directory_group import DirectoryGroup

    def _emit(phase: str, message: str, done_steps: int, total_steps: int, current_item: str = "") -> None:
        if progress_cb is not None:
            try:
                progress_cb({
                    "phase": phase,
                    "message": message,
                    "done_steps": done_steps,
                    "total_steps": total_steps,
                    "current_item": current_item,
                })
            except Exception:
                pass

    plan = _RECOMMENDED_BATCH_PLANS.get(body.plan_id) or batch_journal.load(body.plan_id)
    if not plan or plan.get("task_id") != body.task_id:
        raise HTTPException(status_code=404, detail="未找到匹配且有效的批量推荐处理计划，请重新生成预览")

    if plan.get("rules_version") and plan["rules_version"] != "v2.5.54":
        return {"ok": False, "needs_repreview": True, "message": "处理规则已更新，请重新生成本批次预览；旧版计划不会继续执行"}
    _RECOMMENDED_BATCH_PLANS[body.plan_id] = plan
    if plan.get("inflight_step") or plan.get("requires_repreview"):
        return {"ok": False, "needs_repreview": True, "message": "上次执行在文件操作中中断，请更新剩余项预览，先对账再继续"}
    plan["stop_requested"] = False
    step_receipts: dict[str, dict[str, Any]] = plan.setdefault("step_receipts", {})
    merge_items = plan.get("merge_items") or []
    clean_items = plan.get("clean_items") or []
    redundant_items = plan.get("redundant_source_items") or []
    total_steps = len(merge_items) + len(clean_items) + len(redundant_items)

    # G24: 并发对所有待删清理项做局部文件指纹复核（Semaphore=10），发现真正新增或变化视频则阻断并提示重新预览
    pending_clean_checks = [
        c_item for c_item in clean_items
        if step_receipts.get(f"clean:{c_item['season_record_id']}", {}).get("status") != "completed"
        and (c_item.get("file_fingerprints") or {})
    ]
    _emit("fingerprint_check", f"正在并发校验 {len(pending_clean_checks)} 个待清理目录文件指纹...", 0, total_steps)

    cd2_check = CloudDrive2Client()
    changed_seasons: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(10)

    async def _check_one_fp(c_item: dict[str, Any]) -> dict[str, Any] | None:
        saved_fps = c_item.get("file_fingerprints") or {}
        async with sem:
            try:
                curr_files = await asyncio.wait_for(
                    cd2_check.list_files(c_item["season_path"], force_refresh=True),
                    timeout=15.0,
                )
                del_paths = {d.get("path", "") for d in (c_item.get("to_delete") or []) if d.get("path")}
                diff = compare_plan_fingerprints(saved_fps, curr_files, del_paths)
                if diff.get("needs_repreview"):
                    return {
                        "season_record_id": c_item["season_record_id"],
                        "show_name": c_item["show_name"],
                        "season_number": c_item["season_number"],
                        "added_videos": diff["added_videos"],
                        "modified_videos": diff["modified_videos"],
                        "modified_details": diff.get("modified_details", {}),
                    }
            except Exception as exc:
                return {"season_record_id": c_item["season_record_id"], "show_name": c_item["show_name"], "season_number": c_item["season_number"], "error": f"文件指纹读取失败: {type(exc).__name__}", "added_videos": [], "modified_videos": []}
        return None

    try:
        if pending_clean_checks:
            fp_results = await asyncio.gather(*(_check_one_fp(c) for c in pending_clean_checks))
            changed_seasons = [r for r in fp_results if r is not None]
    finally:
        await cd2_check.close()

    if changed_seasons:
        return {
            "ok": False,
            "needs_repreview": True,
            "changed_seasons": changed_seasons,
            "message": f"检测到 {len(changed_seasons)} 个季的文件信息与预览不一致或目录读取未完成，已暂停执行，请更新预览后再确认。",
        }

    executed_merges: list[dict[str, Any]] = []
    executed_cleans: list[dict[str, Any]] = []
    executed_redundant: list[dict[str, Any]] = []
    failed_steps: list[dict[str, Any]] = []
    touched_record_ids: set[str] = set()
    merged_show_paths_norm: set[str] = set()
    merged_target_by_from_norm: dict[str, str] = {}
    done_steps = 0

    cd2_exec = CloudDrive2Client()
    # 批量开始前统一读取一次 MoviePilot 活跃下载清单，避免循环内重复请求 90+ 次
    active_dl_paths: set[str] = set()
    try:
        settings = get_settings()
        if settings.mp_url and settings.mp_token:
            mp_probe = MoviePilotClient()
            try:
                dls = await mp_probe.get_active_downloads()
                for dl in (dls if isinstance(dls, list) else []):
                    for k in ("path", "save_path", "content_path", "target_path", "title", "name"):
                        val = str(dl.get(k) or "").strip()
                        if val:
                            active_dl_paths.add(val)
            finally:
                await mp_probe.close()
    except Exception:
        pass

    try:
        # 1. 执行同 ID 目录规范化归并（幂等跳过已完成步骤）
        for idx, m_item in enumerate(merge_items, start=1):
            label = m_item.get("canonical_dir_name") or m_item.get("title") or m_item["group_id"]
            _emit("merge", f"正在归并同作品目录 ({idx}/{len(merge_items)})：{label}", done_steps, total_steps, label)
            step_key = f"merge:{m_item['group_id']}"
            src_target_norm = normalize_dir_path(m_item.get("source_target_show_path") or "")
            from_norms = [normalize_dir_path(p) for p in (m_item.get("merge_from_show_paths") or []) if p]
            if step_receipts.get(step_key, {}).get("status") == "completed":
                executed_merges.append(step_receipts[step_key])
                if src_target_norm and m_item.get("changes_season_files", True):
                    merged_show_paths_norm.add(src_target_norm)
                for fn in from_norms:
                    if m_item.get("changes_season_files", True):
                        merged_show_paths_norm.add(fn)
                    if m_item.get("source_target_show_path"):
                        merged_target_by_from_norm[fn] = m_item["source_target_show_path"]
                done_steps += 1
                continue
            if plan.get("stop_requested"):
                break
            plan["inflight_step"] = step_key
            batch_journal.save(body.plan_id, plan)
            try:
                res = await execute_directory_merge(
                    db, body.task_id, m_item["group_id"], plan_id=m_item["plan_id"], cd2_client=cd2_exec, skip_summary_refresh=True
                )
                rec_obj = {"status": "completed", "group_id": m_item["group_id"], "title": m_item["title"], "result": res}
                step_receipts[step_key] = rec_obj
                executed_merges.append(rec_obj)
                if src_target_norm and m_item.get("changes_season_files", True):
                    merged_show_paths_norm.add(src_target_norm)
                for fn in from_norms:
                    if m_item.get("changes_season_files", True):
                        merged_show_paths_norm.add(fn)
                    if m_item.get("source_target_show_path"):
                        merged_target_by_from_norm[fn] = m_item["source_target_show_path"]
            except Exception as e:
                failed_steps.append({"step": step_key, "kind": "merge_directory", "title": label, "error": str(e)})
            plan.pop("inflight_step", None)
            batch_journal.save(body.plan_id, plan)
            done_steps += 1

        # 2. 执行同内容副本清理
        for idx, c_item in enumerate(clean_items, start=1):
            rec_id = c_item["season_record_id"]
            label = f"{c_item['show_name']} S{int(c_item.get('season_number') or 1):02d}"
            _emit("clean", f"正在清理重复版本 ({idx}/{len(clean_items)})：{label}", done_steps, total_steps, label)
            touched_record_ids.add(rec_id)
            step_key = f"clean:{rec_id}"
            if step_receipts.get(step_key, {}).get("status") == "completed":
                executed_cleans.append(step_receipts[step_key])
                done_steps += 1
                continue
            if plan.get("stop_requested"):
                break
            plan["inflight_step"] = step_key
            batch_journal.save(body.plan_id, plan)
            try:
                target_rec_id = rec_id
                exp_fps = c_item.get("file_fingerprints")
                rec_row = await db.get(SeasonRecord, rec_id)
                c_show_norm = normalize_dir_path(posixpath.dirname(c_item.get("season_path") or ""))
                c_rec_show_norm = normalize_dir_path(rec_row.show_path) if rec_row and rec_row.show_path else c_show_norm

                # 若前序目录归并已将本季并入主目录并退场，则转而核验归并后的主目录同季记录
                if rec_row is not None and rec_row.status in TERMINAL_STATUSES:
                    merged_dst_show = merged_target_by_from_norm.get(c_show_norm) or merged_target_by_from_norm.get(c_rec_show_norm)
                    if merged_dst_show:
                        dst_norm = normalize_dir_path(merged_dst_show)
                        cand_rows = list((await db.scalars(
                            select(SeasonRecord).where(
                                SeasonRecord.task_id == body.task_id,
                                SeasonRecord.season_number == int(c_item.get("season_number") or 1),
                                SeasonRecord.status.not_in(tuple(TERMINAL_STATUSES)),
                            )
                        )).all())
                        surviving = next(
                            (r for r in cand_rows if normalize_dir_path(r.show_path) == dst_norm),
                            None,
                        )
                        if surviving is None:
                            rec_obj = {
                                "status": "completed",
                                "season_record_id": rec_id,
                                "show_name": c_item["show_name"],
                                "season_number": c_item["season_number"],
                                "message": "已随目录规范化归并完成",
                            }
                            step_receipts[step_key] = rec_obj
                            executed_cleans.append(rec_obj)
                            done_steps += 1
                            plan.pop("inflight_step", None)
                            batch_journal.save(body.plan_id, plan)
                            continue
                        target_rec_id = surviving.id
                        touched_record_ids.add(target_rec_id)
                        raise ValueError("目录归并已完成；归并后清理范围需更新预览，未执行额外删除")
                    else:
                        rec_obj = {
                            "status": "completed",
                            "season_record_id": rec_id,
                            "show_name": c_item["show_name"],
                            "season_number": c_item["season_number"],
                            "message": f"状态已更新为 {rec_row.status}",
                        }
                        step_receipts[step_key] = rec_obj
                        executed_cleans.append(rec_obj)
                        done_steps += 1
                        plan.pop("inflight_step", None)
                        batch_journal.save(body.plan_id, plan)
                        continue
                elif c_show_norm in merged_show_paths_norm or c_rec_show_norm in merged_show_paths_norm:
                    # 目录刚在步骤 1 接收合并文件，清空合并前旧指纹并按合并后实目录重算计划
                    raise ValueError("目录归并已完成；归并后清理范围需更新预览，未执行额外删除")

                res = await _clean_duplicates_impl(
                    _ArchiveRequest(
                        season_record_ids=[target_rec_id],
                        plan_id=c_item.get("plan_id") if exp_fps else None,
                        expected_fingerprints=exp_fps,
                    ),
                    db,
                    cd2_client=cd2_exec,
                    active_dl_paths_override=active_dl_paths,
                    skip_summary_refresh=True,
                )
                if res.ok:
                    rec_obj = {
                        "status": "completed",
                        "season_record_id": target_rec_id,
                        "show_name": c_item["show_name"],
                        "season_number": c_item["season_number"],
                        "message": res.message,
                    }
                    step_receipts[step_key] = rec_obj
                    executed_cleans.append(rec_obj)
                else:
                    failed_steps.append({"step": step_key, "kind": "clean_duplicates", "title": label, "error": res.message})
            except Exception as e:
                failed_steps.append({"step": step_key, "kind": "clean_duplicates", "title": label, "error": str(e)})
            plan.pop("inflight_step", None)
            batch_journal.save(body.plan_id, plan)
            done_steps += 1

        # 3. 执行归档已齐源端清理
        for idx, r_item in enumerate(redundant_items, start=1):
            rec_id = r_item["season_record_id"]
            label = f"{r_item['show_name']} S{int(r_item.get('season_number') or 1):02d}"
            _emit("redundant", f"正在清理已归档源端 ({idx}/{len(redundant_items)})：{label}", done_steps, total_steps, label)
            touched_record_ids.add(rec_id)
            step_key = f"redundant:{rec_id}"
            if step_receipts.get(step_key, {}).get("status") == "completed":
                executed_redundant.append(step_receipts[step_key])
                done_steps += 1
                continue
            if plan.get("stop_requested"):
                break
            plan["inflight_step"] = step_key
            batch_journal.save(body.plan_id, plan)
            try:
                res = await _clean_source_redundant_impl(
                    _ArchiveRequest(season_record_ids=[rec_id]),
                    db,
                    cd2_client=cd2_exec,
                    skip_summary_refresh=True,
                )
                if res.ok:
                    rec_obj = {"status": "completed", "season_record_id": rec_id, "show_name": r_item["show_name"], "season_number": r_item["season_number"], "message": "源端已清理，归档已保留"}
                    step_receipts[step_key] = rec_obj
                    executed_redundant.append(rec_obj)
                else:
                    failed_steps.append({"step": step_key, "kind": "clean_redundant_source", "title": label, "error": res.message})
            except Exception as e:
                failed_steps.append({"step": step_key, "kind": "clean_redundant_source", "title": label, "error": str(e)})
            plan.pop("inflight_step", None)
            batch_journal.save(body.plan_id, plan)
            done_steps += 1
    finally:
        await cd2_exec.close()

    # 4. 刷新任务汇总并按四类去向分类汇报 (G28)
    _emit("recalc", "正在全局重算任务汇总与五模块分类状态...", done_steps, total_steps)
    summary = await _refresh_task_summary(body.task_id, db)
    await db.commit()

    all_rows_after = list((await db.scalars(
        select(SeasonRecord).where(SeasonRecord.task_id == body.task_id)
    )).all())
    pending_dg_after = {
        g.id for g in (await db.scalars(
            select(DirectoryGroup).where(DirectoryGroup.task_id == body.task_id, DirectoryGroup.status == "pending_confirm")
        )).all()
    }
    active_after = [r for r in all_rows_after if r.status not in TERMINAL_STATUSES]
    work_after, _ = consolidate_seasons_to_work_records(active_after, pending_dg_after)

    outcomes: dict[str, list[dict[str, Any]]] = {
        "to_download": [],
        "to_airing": [],
        "to_archive": [],
        "source_cleaned": [
            {"season_record_id": x["season_record_id"], "show_name": x["show_name"], "season_number": x["season_number"], "receipt": "源端已清理，归档已保留"}
            for x in executed_redundant
        ],
        "remaining_anomalies": [],
        "remaining_review": [],
        "skipped_items": list(plan.get("skipped_items") or []),
    }
    scope_ids = set(plan.get("scope_record_ids") or touched_record_ids)
    for w in work_after:
        wid = w.get("id")
        part_ids = set(w.get("participating_record_ids") or [])
        if wid not in scope_ids and not (part_ids & scope_ids):
            continue
        cat = w.get("main_category")
        brief = {
            "season_record_id": wid,
            "show_name": w.get("show_name") or "",
            "season_number": int(w.get("season_number") or 1),
            "status_detail": w.get("status_detail") or "",
            "anomaly_problem_label": w.get("anomaly_problem_label") or "",
        }
        brief["previous_category"] = (plan.get("before_categories") or {}).get(wid, "")
        if cat == "review":
            brief["reason"] = w.get("status_detail") or {"clean_duplicates": "仍有副本或关联附件待处理", "merge_directory": "目录归并尚未完成", "clean_redundant_source": "源端清理尚未完成"}.get(w.get("review_action_kind"), "仍有待处理项")
            outcomes["remaining_review"].append(brief)
        elif cat == "anomaly":
            outcomes["remaining_anomalies"].append(brief)
        elif cat == "held":
            outcomes["skipped_items"].append(brief)
        else:
            if cat == "download":
                outcomes["to_download"].append(brief)
            elif cat == "airing":
                outcomes["to_airing"].append(brief)
            elif cat == "archive":
                outcomes["to_archive"].append(brief)

    unresolved_count = len(outcomes["remaining_review"]) + len(outcomes["remaining_anomalies"]) + len(outcomes["skipped_items"])
    fully_resolved = not failed_steps and unresolved_count == 0 and not plan.get("stop_requested")
    progress_made = bool(executed_merges or executed_cleans or executed_redundant)
    no_progress = not fully_resolved and not progress_made and not plan.get("stop_requested")
    if failed_steps:
        plan["requires_repreview"] = True
    batch_journal.save(body.plan_id, plan)
    plan_mode = str(plan.get("mode") or "all")
    if plan_mode == "merge_directory":
        final_msg = (
            f"批量处理多目录完成：目录归并 {len(executed_merges)} 组"
            f"；待复核剩余 {len(outcomes['remaining_review'])} 季、待核实异常 {len(outcomes['remaining_anomalies'])} 季、失败 {len(failed_steps)} 项"
        )
    elif plan_mode == "redundant_clean":
        final_msg = (
            f"批量冗余清理完成：副本清理 {len(executed_cleans)} 季、源端清源 {len(executed_redundant)} 季"
            f"；待复核剩余 {len(outcomes['remaining_review'])} 季、待核实异常 {len(outcomes['remaining_anomalies'])} 季、失败 {len(failed_steps)} 项"
        )
    else:
        final_msg = (
            f"本轮执行结束：目录归并 {len(executed_merges)} 组、副本清理 {len(executed_cleans)} 季、源端清源 {len(executed_redundant)} 季"
            + f"；待复核清理剩余 {len(outcomes['remaining_review'])} 季、待核实异常 {len(outcomes['remaining_anomalies'])} 季、失败 {len(failed_steps)} 项"
        )
    return {
        "ok": len(failed_steps) == 0,
        "mode": plan_mode,
        "fully_resolved": fully_resolved,
        "no_progress": no_progress,
        "can_refresh_remaining": bool(not fully_resolved and (progress_made or plan.get("stop_requested"))),
        "stopped": bool(plan.get("stop_requested")),
        "unresolved_count": unresolved_count,
        "partial_success": bool(failed_steps and (executed_merges or executed_cleans or executed_redundant)),
        "plan_id": body.plan_id,
        "executed_merges": executed_merges,
        "executed_cleans": executed_cleans,
        "executed_redundant": executed_redundant,
        "failed_steps": failed_steps,
        "outcomes": outcomes,
        "summary": summary,
        "message": final_msg,
    }


@router.post("/recommended-execute")
async def execute_recommended_batch_plan(
    body: RecommendedExecuteRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """同步执行已确认预览的「执行推荐处理」批次计划。"""
    return await _run_recommended_batch_plan(body, db)


@router.post("/recommended-execute-start")
async def start_recommended_batch_plan_job(
    body: RecommendedExecuteRequest,
) -> dict[str, Any]:
    """异步启动「执行推荐处理」后台作业并立即返回 job_id，供前端轮询实时进度与防超时断连。"""
    from uuid import uuid4
    from app.database import async_session

    plan = _RECOMMENDED_BATCH_PLANS.get(body.plan_id) or batch_journal.load(body.plan_id)
    if not plan or plan.get("task_id") != body.task_id:
        raise HTTPException(status_code=404, detail="未找到匹配且有效的批量推荐处理计划，请重新生成预览")

    # 幂等防重：同 plan_id 若已有进行中作业直接复用
    for existing in _RECOMMENDED_EXECUTE_JOBS.values():
        if existing.get("task_id") == body.task_id and existing.get("status") == "running":
            if existing.get("plan_id") != body.plan_id:
                raise HTTPException(status_code=409, detail="该任务已有推荐处理正在执行")
            return _build_recommended_job_snapshot(existing)

    total_steps = (
        len(plan.get("merge_items") or [])
        + len(plan.get("clean_items") or [])
        + len(plan.get("redundant_source_items") or [])
    )
    job_id = f"job_rec_{uuid4().hex[:12]}"
    job: dict[str, Any] = {
        "job_id": job_id,
        "plan_id": body.plan_id,
        "task_id": body.task_id,
        "status": "running",
        "phase": "fingerprint_check",
        "message": "正在启动推荐处理后台执行...",
        "done_steps": 0,
        "total_steps": total_steps,
        "current_item": "",
        "result": None,
        "error": None,
    }
    _RECOMMENDED_EXECUTE_JOBS[job_id] = job
    batch_journal.save(job_id, job)

    def _on_progress(ev: dict[str, Any]) -> None:
        job.update({
            "phase": ev.get("phase", job["phase"]),
            "message": ev.get("message", job["message"]),
            "done_steps": ev.get("done_steps", job["done_steps"]),
            "total_steps": ev.get("total_steps", job["total_steps"]),
            "current_item": ev.get("current_item", ""),
        })
        batch_journal.save(job_id, job)

    async def _worker() -> None:
        try:
            async with async_session() as bg_db:
                res = await _run_recommended_batch_plan(body, bg_db, progress_cb=_on_progress)
            job["result"] = res
            if res.get("needs_repreview"):
                job["status"] = "needs_repreview"
                job["phase"] = "needs_repreview"
                job["message"] = res.get("message") or "检测到目录文件变化，需重新预览"
            else:
                job["status"] = "completed" if res.get("fully_resolved") else ("blocked" if res.get("no_progress") else "partial_success")
                job["phase"] = "done"
                if not res.get("stopped"):
                    job["done_steps"] = job["total_steps"]
                job["message"] = res.get("message") or "推荐处理执行完成"
        except Exception as exc:
            log.exception("后台执行推荐处理失败: %s", exc)
            job["status"] = "failed"
            job["phase"] = "failed"
            job["error"] = str(exc)
            job["message"] = f"执行失败: {exc}"
        finally:
            batch_journal.save(job_id, job)

    asyncio.create_task(_worker())
    return _build_recommended_job_snapshot(job)


@router.get("/recommended-execute-status/{job_id}")
async def get_recommended_batch_plan_status(job_id: str) -> dict[str, Any]:
    """查询「执行推荐处理」后台作业实时进度与结果。"""
    job = _RECOMMENDED_EXECUTE_JOBS.get(job_id)
    if not job:
        job = batch_journal.load(job_id)
        if job and job.get("status") == "running":
            job.update(status="needs_repreview", message="服务曾中断，请重新盘点剩余步骤", result={"needs_repreview": True})
    if not job:
        raise HTTPException(status_code=404, detail="未找到对应的推荐处理执行任务")
    return _build_recommended_job_snapshot(job)


@router.post("/recommended-execute-stop/{job_id}")
async def stop_recommended_batch_plan(job_id: str) -> dict[str, Any]:
    job = _RECOMMENDED_EXECUTE_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="未找到运行中的批次")
    plan = _RECOMMENDED_BATCH_PLANS.get(job["plan_id"])
    if plan and job.get("status") == "running":
        plan["stop_requested"] = True
        batch_journal.save(job["plan_id"], plan)
        job["message"] = "已请求停止；当前步骤核验结束后停止，已完成结果保留"
    return _build_recommended_job_snapshot(job)


# 用户主动放弃缺集季与归档已齐清源分离，旧接口门禁不放宽。
from app.api.source_discard import router as source_discard_router
router.include_router(source_discard_router)
