"""归档与清理端点 — 生产级 CD2 文件操作与状态级联流转。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import posixpath
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.clouddrive2 import CloudDrive2Client
from app.clients.moviepilot import MoviePilotClient
from app.config import get_settings
from app.core.directory_governance import find_blocking_group_for_record, normalize_dir_path
from app.core.episode_parser import is_subtitle_file, is_video_file, parse_episode
from app.core.inspector import _parse_season_number, _verified_video, diagnose_season
from app.core.segments import segment_number
from app.core.review_engine import generate_review_clean_plan
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
    if norm_source_root and (norm_show == norm_source_root or not norm_show.startswith(norm_source_root + "/")):
        log.warning("剧集目录超出或等于源库根目录，严禁删除: %s", show_path)
        return {
            "cleaned": False,
            "status": "retained_expected",
            "reason": "剧集目录超出或等于源库根目录，安全保留",
            "retained_items": [],
            "migrated_assets": [],
        }

    try:
        source_items = await cd2.list_files(show_path, force_refresh=True)
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
    if subdirs:
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
    if known_assets and target_parent and not unknown_attachments:
        migration_failed = False
        for asset in known_assets:
            asset_name = asset.get("name")
            if not asset_name:
                continue
            asset_path = asset.get("path") or f"{show_path}/{asset_name}"
            try:
                mv_res = await cd2.move_file(asset_path, target_parent, skip_existing=True)
                if not mv_res.get("success", True):
                    log.warning("迁移公共资产 %s 失败，中止源剧集删除", asset_name)
                    migration_failed = True
                    break
                migrated_assets.append(asset_name)
            except Exception as mv_err:
                log.warning("迁移公共资产 %s 异常: %s，中止源剧集删除", asset_name, mv_err)
                migration_failed = True
                break

        if migration_failed:
            log.warning("公共资产迁移未完全成功，安全保留源剧集目录: %s", show_path)
            return {
                "cleaned": False,
                "status": "cleanup_failed",
                "reason": "公共资产迁移未完全成功，安全保留源剧集目录",
                "retained_items": [f.get("name", "") for f in non_dir_files if f.get("name")],
                "migrated_assets": migrated_assets,
            }

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
        final_check = await cd2.list_files(show_path, force_refresh=True)
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

        await cd2.delete_file(show_path)
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


async def _refresh_task_summary(task_id: str, db: AsyncSession) -> None:
    """级联刷新关联 Task 的汇总统计 summary_json（含真缺集待补与已推送待入库）。"""
    from app.services.gap_fill_tracker import refresh_task_summary

    await refresh_task_summary(task_id, db)


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
    if not body.season_record_ids:
        return StatusResponse(ok=False, message="未选择任何项目")

    stmt = select(SeasonRecord).where(SeasonRecord.id.in_(body.season_record_ids))
    result = await db.execute(stmt)
    records = list(result.scalars().all())

    if not records:
        return StatusResponse(ok=False, message="未找到匹配的季度记录")

    cd2 = CloudDrive2Client()
    total_deleted_videos = 0
    total_deleted_orphans = 0
    errors: list[str] = []
    affected_tasks: set[str] = set()

    try:
        for rec in records:
            season_path = rec.season_path

            from app.core.season_classifier import get_record_work_main_category
            work_cat = await get_record_work_main_category(db, rec)
            if work_cat == "airing" or rec.status == "airing" or (rec.season_finished is False and "连载中" in str(rec.status_detail or "")):
                errors.append(f"{rec.show_name}：当前作品季处于「连载留源」状态，连载期暂不开放重复版本清理")
                continue

            if rec.status in ("archive_check", "period_review", "period_ready", "period_missing", "identity_review", "already_archived", "manually_handled", "verified_complete"):
                errors.append(f"{rec.show_name}：当前处理方式不适用重复版本清理")
                continue
            c_info = json.loads(rec.conflict_json or "{}") if getattr(rec, "conflict_json", None) else {}
            if isinstance(c_info, dict) and c_info.get("blocking"):
                errors.append(f"{rec.show_name}：存在未解除的身份冲突，安全拦截清理")
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

            # 使用 tv-workflow-win 工业级算法生成复核清理计划（版本模板分组 + 完整版本优先淘汰残卷）
            total_eps = rec.total_episodes or len(all_files)
            plan = generate_review_clean_plan(
                files=all_files,
                season_number=rec.season_number,
                total_episodes=total_eps,
                force_clean=body.force_clean,
            )

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

            # 1. 优先按顺序执行主视频删除
            for item in video_items:
                fpath = item["path"]
                fname = item["name"]
                try:
                    await cd2.delete_file(fpath)
                    deleted_video_paths.append(fpath)
                    deleted_files_this_season.append(fpath)
                    total_deleted_videos += 1
                    log.info("已安全清理冗余视频: %s (%s)", fname, item.get("reason", ""))
                except Exception as de:
                    failed_items.append(fpath)
                    errors.append(f"删除视频 {fname} 失败: {str(de)[:100]}")

            # 提取成功删除视频的词干名 (stem)，伴生文件必须严格依附于已确认删除的主文件
            import posixpath
            deleted_video_stems = {posixpath.splitext(posixpath.basename(p))[0] for p in deleted_video_paths}

            # 2. 安全删除仅属于已确认删除视频的伴生文件，主视频删除失败严格保留附件
            for item in orphan_items:
                fpath = item["path"]
                fname = item["name"]
                base_stem = posixpath.splitext(fname)[0]
                if deleted_video_stems and not any(base_stem == s or base_stem.startswith(s) for s in deleted_video_stems):
                    log.info("伴生文件 %s 对应的主视频未确认删除或不匹配，严格保留", fname)
                    continue
                try:
                    await cd2.delete_file(fpath)
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

                    # 双向核验：待删文件确已不在网盘，计划保留文件依然完整
                    phantom_files = [p for p in deleted_files_this_season if p in fresh_paths]
                    missing_keep = [k["path"] for k in plan.get("to_keep", []) if k.get("path") not in fresh_paths]

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
                    ep_cnt = Counter(all_fresh_eps)
                    fresh_duplicates = sorted([n for n, c in ep_cnt.items() if c > 1])

                    fresh_missing = []
                    if rec.total_episodes:
                        fresh_missing = sorted(set(range(1, rec.total_episodes + 1)) - set(fresh_recognized))

                    if verification_passed:
                        new_status, new_detail = diagnose_season(
                            recognized=fresh_recognized,
                            total_episodes=rec.total_episodes,
                            season_finished=rec.season_finished,
                            has_duplicates=bool(fresh_duplicates),
                            duplicate_episodes=fresh_duplicates,
                            allow_bonus_specials=True,
                        )
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
                    log.info("季度状态自动重新计算: %s S%02d -> %s (%s)", rec.show_name, rec.season_number, new_status, new_detail)

                    op_log = OperationLog(
                        task_id=rec.task_id,
                        season_record_id=rec.id,
                        action="clean_duplicate",
                        status=op_status,
                        detail_json=json.dumps({
                            "show": rec.show_name,
                            "season": rec.season_number,
                            "version_note": plan["version_note"],
                            "deleted_paths": deleted_files_this_season,
                            "failed_paths": failed_items,
                            "verification_passed": verification_passed,
                        }, ensure_ascii=False),
                    )
                    db.add(op_log)

                except Exception as fe:
                    log.warning("清理后实物核验与重新诊断失败: %s", fe)
                    errors.append(f"{rec.show_name} S{rec.season_number:02d}: 清理后实物核验失败: {fe}")

        # 5. 级联刷新受影响的任务统计
        for tid in affected_tasks:
            await _refresh_task_summary(tid, db)

        await db.commit()

    finally:
        await cd2.close()

    has_deleted = (total_deleted_videos > 0 or total_deleted_orphans > 0)
    # P1-9 修复：若没有成功删除任何文件且存在错误/异常，绝对不能报告成功
    if not has_deleted and errors:
        is_ok = False
    elif is_all_failed := (len(errors) == len(records) and not has_deleted):
        is_ok = False
    else:
        is_ok = True

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


class CleanPreviewResponse(BaseModel):
    ok: bool
    message: str
    previews: list[CleanPreviewItem]


@router.post("/clean-preview", response_model=CleanPreviewResponse)
async def clean_duplicates_preview(
    body: _ArchiveRequest,
    db: AsyncSession = Depends(get_db),
) -> CleanPreviewResponse:
    """试跑复核清理计划 — 预览将要保留与删除的文件列表及判定理由，不执行真实删除。"""
    if not body.season_record_ids:
        return CleanPreviewResponse(ok=False, message="未选择任何项目", previews=[])

    stmt = select(SeasonRecord).where(SeasonRecord.id.in_(body.season_record_ids))
    result = await db.execute(stmt)
    records = list(result.scalars().all())

    if not records:
        return CleanPreviewResponse(ok=False, message="未找到匹配的季度记录", previews=[])

    cd2 = CloudDrive2Client()
    previews: list[CleanPreviewItem] = []

    try:
        for rec in records:
            season_path = rec.season_path
            from app.core.season_classifier import get_record_work_main_category
            work_cat = await get_record_work_main_category(db, rec)
            if work_cat == "airing" or rec.status == "airing" or (rec.season_finished is False and "连载中" in str(rec.status_detail or "")):
                previews.append(CleanPreviewItem(
                    season_record_id=rec.id,
                    show_name=rec.show_name,
                    season_number=rec.season_number,
                    can_clean=False,
                    reason="当前作品季处于「连载留源」状态，连载期暂不开放重复版本清理",
                    version_note="",
                    to_keep=[],
                    to_delete=[],
                ))
                continue
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
                ))
                continue

            total_eps = rec.total_episodes or len(all_files)
            plan = generate_review_clean_plan(
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
    if not body.season_record_ids:
        return StatusResponse(ok=False, message="未选择任何项目")

    stmt = select(SeasonRecord).where(SeasonRecord.id.in_(body.season_record_ids))
    result = await db.execute(stmt)
    records = list(result.scalars().all())

    if not records:
        return StatusResponse(ok=False, message="未找到匹配的季度记录")

    cd2 = CloudDrive2Client()
    success_count = 0
    errors: list[str] = []
    affected_tasks: set[str] = set()

    try:
        for rec in records:
            source_season_path = rec.season_path
            show_path = rec.show_path

            # 【P0-1 & P1-1: 物理安全门禁：状态、路径隔离、两端集号与独有附件覆盖核验】
            if rec.status != "already_archived":
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
                src_files = await cd2.list_files(source_season_path, force_refresh=True)
                target_files = await cd2.list_files(archive_target, force_refresh=True)

                src_video_eps: set[int] = set()
                src_subs: list[dict] = []
                unrecognized_videos: list[dict] = []
                for sf in src_files:
                    if sf.get("isDirectory"):
                        raise ValueError("源季内另有子目录：" + sf["name"])
                    sf_name = sf.get("name", "")
                    if is_video_file(sf_name):
                        p_sf = parse_episode(sf_name, sf.get("path", ""), sf.get("size", 0))
                        if _verified_video(p_sf, rec.season_number) and not segment_number(sf_name):
                            src_video_eps.update(p_sf.episode_numbers)
                        else:
                            unrecognized_videos.append(sf)
                    elif is_subtitle_file(sf_name):
                        src_subs.append(sf)
                    elif posixpath.splitext(sf_name)[1].lower() not in {".nfo", ".jpg", ".jpeg", ".png", ".webp"}:
                        raise ValueError("源端另有待核对文件：" + sf_name)

                target_video_eps: set[int] = set()
                target_sub_sizes = {}
                for tf in target_files:
                    if tf.get("isDirectory"):
                        continue
                    tf_name = tf.get("name", "")
                    if is_video_file(tf_name):
                        p_tf = parse_episode(tf_name, tf.get("path", ""), tf.get("size", 0))
                        if _verified_video(p_tf, rec.season_number) and not segment_number(tf_name):
                            target_video_eps.update(p_tf.episode_numbers)
                    elif is_subtitle_file(tf_name):
                        target_sub_sizes[tf_name.casefold()] = tf.get("size")

                # 核验 A: 源端若未识别到任何有效正片集号，或含有无法识别集号的正片视频，绝对拦截删除（源集数与记录不变）
                if unrecognized_videos:
                    unrec_names = [f.get("name", "") for f in unrecognized_videos[:2]]
                    err_msg = f"{rec.show_name} S{rec.season_number:02d}: 源端含有未识别集号的正片视频 ({unrec_names})，证据不足拦截删除！"
                    log.warning(err_msg)
                    errors.append(err_msg)
                    continue

                if not src_video_eps:
                    has_video = any(not f.get("isDirectory") and is_video_file(f.get("name", "")) for f in src_files)
                    reason = "本层的视频均标记为花絮" if has_video else "源季目录本层没有支持的视频文件"
                    err_msg = f"{rec.show_name} S{rec.season_number:02d}: {reason}，已保留源目录。"
                    log.warning(err_msg)
                    errors.append(err_msg)
                    continue

                # 核验 B: 目标端必须完整包含源端所有正片集号 (source_eps ⊆ target_eps)，证据不足坚决拦截（源集数与记录不变）
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
                # 核验 C: 源端独有外挂字幕自动迁移至目标库
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
                moved_names = {s["name"] for s in src_subs if s["name"].casefold() not in target_sub_sizes}
                def signature(files):
                    return sorted((f["path"], f.get("size"), f.get("modified"), f.get("isDirectory"))
                                  for f in files)
                fresh_source = await cd2.list_files(source_season_path, force_refresh=True)
                if signature(fresh_source) != signature([f for f in src_files if f["name"] not in moved_names]):
                    raise ValueError("源季内容已变化，请重新初检")

            except Exception as chk_err:
                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 校验两端文件清单失败 ({chk_err})，安全拦截源端删除！"
                log.warning(err_msg)
                errors.append(err_msg)
                continue

            try:
                # 1. 安全删除源端冗余季目录
                log.info("已通过全部覆盖门禁，正在安全清理源端冗余季目录: %s", source_season_path)
                await cd2.delete_file(source_season_path)
                success_count += 1
                rec.status = "cleaned_redundant"
                rec.status_detail = f"已安全清理源端冗余残留目录 ({source_season_path})"
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
                    }, ensure_ascii=False),
                )
                db.add(op_log)

            except Exception as e:
                err_msg = f"{rec.show_name} S{rec.season_number:02d}: 清理失败 - {str(e)[:150]}"
                log.error(err_msg)
                errors.append(err_msg)

        for tid in affected_tasks:
            await _refresh_task_summary(tid, db)

        await db.commit()

    finally:
        await cd2.close()

    msg = f"冗余清理完成：{success_count}/{len(records)} 季源端残留已删除"
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

    new_status, new_detail = diagnose_season(
        recognized=recognized,
        total_episodes=body.total_episodes,
        season_finished=True,  # 人工校准即视为该指定集数已完结
        has_duplicates=bool(duplicates),
        duplicate_episodes=duplicates,
    )
    rec.status = new_status
    rec.status_detail = f"人工已校准总集数为 {body.total_episodes} 集（原 {old_total} 集）：{new_detail}"
    rec.season_finished = True

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
        return StatusResponse(ok=False, message="真缺集待补剧集严禁删除源端整剧，仅支持推送补缺")
    if rec.status != "unknown":
        return StatusResponse(ok=False, message="仅未收录/元数据未知的异常剧集允许整剧删除源端")
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

    if rec.status not in ("download_needed", "period_missing"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"仅允许对「真缺集待补」状态的季度执行放弃补缺预检，当前状态为: {rec.status}"
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

    if rec.status not in ("download_needed", "period_missing"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"仅允许对「真缺集待补」状态的季度执行放弃补缺并删源，当前状态为: {rec.status}"
        )

    if not body.confirm_abandon:
        return StatusResponse(ok=False, message="未确认放弃补缺")

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
