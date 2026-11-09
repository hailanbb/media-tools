"""归档与清理端点 — 生产级 CD2 文件操作与状态级联流转。"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.clouddrive2 import CloudDrive2Client
from app.config import get_settings
from app.core.episode_parser import is_subtitle_file, is_video_file, parse_episode
from app.core.inspector import _parse_season_number, diagnose_season
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
    "fanart", "clearart", "logo", "disc", "landscape"
}


class _ArchiveRequest(BaseModel):
    season_record_ids: list[str]
    force_clean: bool = False


class CalibrateEpisodesRequest(BaseModel):
    season_record_id: str
    total_episodes: int



async def _safe_cleanup_source_show_dir(
    cd2: CloudDrive2Client,
    show_path: str,
    target_parent: str | None = None,
) -> bool:
    """源端剧集目录空壳安全清理门禁（F01 防空壳误删连带花絮/自定义目录）。

    安全准则：
    1. 强制实时刷新列出 show_path 下全部子项；
    2. 如果存在任何子目录（无论是普通目录还是 Extras、Specials 等非正片季目录），绝对禁止删除剧集目录！
    3. 如果存在任何正片或非正片视频文件，绝对禁止删除剧集目录！
    4. 若有公共刮削资产（tvshow.nfo、poster.jpg 等）且提供了 target_parent，先安全迁移至归档目标。如果迁移过程中有任何一个文件失败，立即中止删除并返回 False！
    5. 迁移后再次强制刷新，确认该目录下已完全为空（0 个子项），方可调用 delete_file(show_path)。
    """
    if not show_path or len(show_path.strip("/").split("/")) < 2:
        return False

    try:
        source_items = await cd2.list_files(show_path, force_refresh=True)
    except Exception as e:
        log.warning("检查源剧集目录失败: %s - %s", show_path, e)
        return False

    subdirs = [f for f in source_items if f.get("isDirectory")]
    if subdirs:
        subdir_names = ", ".join(f.get("name", "") for f in subdirs[:3])
        log.info("源剧集目录仍有子目录 (%s 等)，安全保留源剧集主目录: %s", subdir_names, show_path)
        return False

    videos = [f for f in source_items if not f.get("isDirectory") and is_video_file(f.get("name", ""))]
    if videos:
        log.info("源剧集目录仍有视频文件，安全保留源剧集主目录: %s", show_path)
        return False

    assets = [f for f in source_items if not f.get("isDirectory")]
    if assets and target_parent:
        migration_failed = False
        for asset in assets:
            asset_name = asset.get("name")
            if not asset_name:
                continue
            asset_path = f"{show_path}/{asset_name}"
            try:
                mv_res = await cd2.move_file(asset_path, target_parent)
                if not mv_res.get("success", True):
                    log.warning("迁移公共资产 %s 失败，中止源剧集删除", asset_name)
                    migration_failed = True
                    break
            except Exception as mv_err:
                log.warning("迁移公共资产 %s 异常: %s，中止源剧集删除", asset_name, mv_err)
                migration_failed = True
                break

        if migration_failed:
            log.warning("公共资产迁移未完全成功，安全保留源剧集目录: %s", show_path)
            return False

    try:
        final_check = await cd2.list_files(show_path, force_refresh=True)
        if final_check:
            log.info("源剧集目录尚有残留项目 (%d 个)，保留主目录: %s", len(final_check), show_path)
            return False

        await cd2.delete_file(show_path)
        log.info("源端全剧已无其余季度与任何子目录/文件，已彻底安全清理源端空壳目录: %s", show_path)
        return True
    except Exception as del_err:
        log.warning("清理源剧集空壳目录失败: %s - %s", show_path, del_err)
        return False


async def _refresh_task_summary(task_id: str, db: AsyncSession) -> None:
    """级联刷新关联 Task 的汇总统计 summary_json。"""
    task = await db.get(Task, task_id)
    if not task:
        return

    stmt = select(SeasonRecord).where(SeasonRecord.task_id == task_id)
    res = await db.execute(stmt)
    all_seasons = list(res.scalars().all())

    archive_ready_count = sum(1 for s in all_seasons if s.status == "archive_ready")
    review_needed_count = sum(1 for s in all_seasons if s.status == "review_needed")
    download_needed_count = sum(1 for s in all_seasons if s.status == "download_needed")
    airing_count = sum(1 for s in all_seasons if s.status == "airing")
    already_archived_count = sum(1 for s in all_seasons if s.status == "already_archived")

    task.summary_json = json.dumps({
        "archive_ready": archive_ready_count,
        "review_needed": review_needed_count,
        "download_needed": download_needed_count,
        "airing": airing_count,
        "already_archived": already_archived_count,
        "total_seasons": len(all_seasons),
    })


@router.post("/execute", response_model=StatusResponse)
async def execute_archive(
    body: _ArchiveRequest,
    db: AsyncSession = Depends(get_db),
) -> StatusResponse:
    """执行归档 — 将选中的剧季按季移动到归档目录，具备同季冲突保护与源端空壳自动清理。"""
    if not body.season_record_ids:
        return StatusResponse(ok=False, message="未选择任何项目")

    settings = get_settings()
    if not settings.archive_root:
        return StatusResponse(ok=False, message="未配置归档目录 (MF_ARCHIVE_ROOT)")

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
            if rec.status != "archive_ready":
                errors.append(f"{rec.show_name} S{rec.season_number:02d}: 当前状态为 {rec.status}，跳过")
                continue

            source_season_path = rec.season_path
            archive_target = rec.archive_target_path

            if not archive_target:
                source_root = settings.source_root.rstrip("/")
                if source_season_path.startswith(source_root):
                    relative = source_season_path[len(source_root):]
                    archive_target = settings.archive_root.rstrip("/") + relative
                else:
                    errors.append(f"{rec.show_name} S{rec.season_number:02d}: 无法自动计算归档路径")
                    continue

            # 目标父目录 = 归档库中的剧集主目录
            target_parent = archive_target.rsplit("/", 1)[0] if "/" in archive_target else archive_target
            season_dir_name = source_season_path.rstrip("/").rsplit("/", 1)[-1]

            try:
                # 确保目标父目录存在
                await cd2.mkdir(target_parent)

                # 【tv-workflow-win 工业级保护：同季冲突检测】
                try:
                    target_parent_files = await cd2.list_files(target_parent, force_refresh=True)
                except Exception as list_err:
                    err_msg = f"{rec.show_name} S{rec.season_number:02d}: 读取目标目录失败 ({list_err})，安全拦截归档"
                    log.error(err_msg)
                    errors.append(err_msg)
                    continue

                existing_season = next(
                    (f for f in target_parent_files if f.get("isDirectory") and (
                        _parse_season_number(f.get("name", "")) == rec.season_number or
                        f.get("name", "").lower() == season_dir_name.lower()
                    )),
                    None
                )
                if existing_season:
                    existing_season_path = existing_season.get("path") or f"{target_parent}/{existing_season.get('name')}"
                    log.info("发现目标归档库已存在同季目录: %s，启动季内文件级增量合并通道 (F08)", existing_season_path)

                    try:
                        src_season_files = await cd2.list_files(source_season_path, force_refresh=True)
                        dest_season_files = await cd2.list_files(existing_season_path, force_refresh=True)
                        dest_file_names = {f.get("name", "").lower() for f in dest_season_files if not f.get("isDirectory")}

                        # 检查源端是否有与目标端同名的视频文件（发生真实文件名冲突）
                        duplicate_videos = [
                            f.get("name") for f in src_season_files
                            if not f.get("isDirectory") and is_video_file(f.get("name", "")) and f.get("name", "").lower() in dest_file_names
                        ]

                        if duplicate_videos:
                            err_msg = f"{rec.show_name} S{rec.season_number:02d}: 同季冲突，归档库已存在同名视频文件 ({duplicate_videos[0]})，已安全拦截"
                            log.warning(err_msg)
                            errors.append(err_msg)
                            rec.status = "conflict"
                            rec.status_detail = f"同季文件冲突：目标目录已存在同名视频 {duplicate_videos[0]}，整季暂缓合并"
                            affected_tasks.add(rec.task_id)
                            continue

                        # 无同名视频冲突：逐一将源端增量文件移动到目标同季目录
                        # 保护性策略 (P1-3): 目标端已存在同名的非视频资产（字幕/NFO/图片）严禁覆盖，安全保留目标端已有健康资产
                        merged_files = []
                        for sf in src_season_files:
                            if sf.get("isDirectory"):
                                continue
                            sf_name = sf.get("name")
                            if not sf_name:
                                continue
                            if sf_name.lower() in dest_file_names:
                                # 目标端已存在同名非视频文件，跳过移动以保护目标库健康资产
                                log.info("目标库已存在同名非视频资产 %s，安全保留目标端，跳过覆盖", sf_name)
                                continue

                            mv_res = await cd2.move_file(sf.get("path"), existing_season_path)
                            if mv_res.get("success", True):
                                merged_files.append(sf_name)
                            else:
                                log.warning("增量迁移文件 %s 失败: %s", sf_name, mv_res.get("errorMessage"))

                        # 检查源端剩余文件
                        rem_in_src = []
                        try:
                            rem_in_src = await cd2.list_files(source_season_path, force_refresh=True)
                            # 如果剩余的仅为刚才跳过的同名冗余伴生，安全清理它们
                            if rem_in_src and all(rf.get("name", "").lower() in dest_file_names for rf in rem_in_src if not rf.get("isDirectory")):
                                for rf in rem_in_src:
                                    if not rf.get("isDirectory"):
                                        try:
                                            await cd2.delete_file(rf.get("path"))
                                        except Exception:
                                            pass
                                rem_in_src = await cd2.list_files(source_season_path, force_refresh=True)

                            if not rem_in_src:
                                await cd2.delete_file(source_season_path)
                                log.info("源端季目录已全部增量合并，已安全清理源季空壳: %s", source_season_path)
                        except Exception as del_season_err:
                            log.warning("清理已合并源季目录失败: %s", del_season_err)

                        # P1-2 修复：严格核对移动结果
                        src_valid_files = [f for f in src_season_files if not f.get("isDirectory")]
                        if not merged_files and src_valid_files:
                            err_msg = f"{rec.show_name} S{rec.season_number:02d}: 增量合并失败，未能成功移动任何文件"
                            log.error(err_msg)
                            errors.append(err_msg)
                            rec.status = "failed"
                            rec.status_detail = err_msg
                            affected_tasks.add(rec.task_id)
                            continue

                        if rem_in_src:
                            # 仍有未移动成功的文件残留，标记为部分归档，不增加全局成功计数
                            err_msg = f"{rec.show_name} S{rec.season_number:02d}: 增量合并部分成功，已移动 {len(merged_files)} 个文件，源端仍残留 {len(rem_in_src)} 项，支持重试"
                            log.warning(err_msg)
                            errors.append(err_msg)
                            rec.status = "review_needed"
                            rec.status_detail = err_msg
                            affected_tasks.add(rec.task_id)
                            continue

                        success_count += 1
                        rec.status = "archived"
                        rec.status_detail = f"已成功增量合并 {len(merged_files)} 个文件至 {existing_season_path}"
                        rec.archive_target_path = existing_season_path
                        affected_tasks.add(rec.task_id)

                        op_log = OperationLog(
                            task_id=rec.task_id,
                            season_record_id=rec.id,
                            action="archive_incremental_merge",
                            status="completed",
                            detail_json=json.dumps({
                                "show": rec.show_name,
                                "season": rec.season_number,
                                "source": source_season_path,
                                "target": existing_season_path,
                                "merged_files_count": len(merged_files),
                            }, ensure_ascii=False),
                        )
                        db.add(op_log)

                        # 源剧集根目录清理门禁
                        if rec.show_path and rec.show_path != source_season_path:
                            await _safe_cleanup_source_show_dir(cd2, rec.show_path, target_parent)

                        continue

                    except Exception as merge_err:
                        err_msg = f"{rec.show_name} S{rec.season_number:02d}: 增量合并失败 - {merge_err}"
                        log.error(err_msg)
                        errors.append(err_msg)
                        continue

                log.info("执行归档迁移: %s -> %s", source_season_path, target_parent)

                move_result = await cd2.move_file(source_season_path, target_parent)
                if move_result.get("success", True):
                    success_count += 1
                    rec.status = "archived"
                    rec.status_detail = f"已成功归档至 {target_parent}/{season_dir_name}"
                    rec.archive_target_path = f"{target_parent}/{season_dir_name}"
                    affected_tasks.add(rec.task_id)

                    op_log = OperationLog(
                        task_id=rec.task_id,
                        season_record_id=rec.id,
                        action="archive",
                        status="completed",
                        detail_json=json.dumps({
                            "show": rec.show_name,
                            "season": rec.season_number,
                            "source": source_season_path,
                            "target": rec.archive_target_path,
                        }, ensure_ascii=False),
                    )
                    db.add(op_log)

                    # 【源端零残留与公共资产完备归档 (F01 强化安全门禁)】
                    if rec.show_path and rec.show_path != source_season_path:
                        await _safe_cleanup_source_show_dir(cd2, rec.show_path, target_parent)
                else:
                    err_msg = move_result.get("errorMessage", "未知错误")
                    errors.append(f"{rec.show_name} S{rec.season_number:02d}: {err_msg}")

            except Exception as e:
                errors.append(f"{rec.show_name} S{rec.season_number:02d}: {str(e)[:200]}")
                log.error("归档 %s 失败: %s", source_season_path, e)

        # 刷新关联任务的 summary
        for tid in affected_tasks:
            await _refresh_task_summary(tid, db)

        await db.commit()

    finally:
        await cd2.close()

    msg = f"归档完成：{success_count}/{len(records)} 季成功移动"
    if errors:
        msg += f"，遇到 {len(errors)} 项提醒: {'; '.join(errors[:2])}"

    return StatusResponse(ok=success_count > 0, message=msg)


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

            # 执行 CD2 删除
            deleted_files_this_season: list[str] = []
            for item in to_delete:
                fpath = item["path"]
                fname = item["name"]
                try:
                    await cd2.delete_file(fpath)
                    deleted_files_this_season.append(fpath)
                    if is_video_file(fname):
                        total_deleted_videos += 1
                    else:
                        total_deleted_orphans += 1
                    log.info("已安全清理冗余/残卷/伴生文件: %s (%s)", fname, item.get("reason", ""))
                except Exception as de:
                    errors.append(f"删除 {fname} 失败: {str(de)[:100]}")

            if deleted_files_this_season:
                affected_tasks.add(rec.task_id)

                op_log = OperationLog(
                    task_id=rec.task_id,
                    season_record_id=rec.id,
                    action="clean_duplicate",
                    status="completed",
                    detail_json=json.dumps({
                        "show": rec.show_name,
                        "season": rec.season_number,
                        "version_note": plan["version_note"],
                        "deleted_paths": deleted_files_this_season,
                    }, ensure_ascii=False),
                )
                db.add(op_log)

                # 删除后立即强制穿透刷新网盘季目录缓存（规约：确保网盘底层与前台缓存同步）
                try:
                    await cd2.list_files(season_path, force_refresh=True)
                except Exception as ref_err:
                    log.warning("删除后刷新季目录失败: %s", ref_err)

            # 重新诊断该季度的最新状态（自动流转状态）
            try:
                fresh_files = await cd2.list_files(season_path, force_refresh=True)
                fresh_videos = [
                    parse_episode(f["name"], f["path"], f.get("size", 0))
                    for f in fresh_files
                    if not f["isDirectory"] and is_video_file(f["name"])
                ]

                all_fresh_eps: list[int] = []
                for fv in fresh_videos:
                    all_fresh_eps.extend(fv.episode_numbers)

                from collections import Counter
                fresh_recognized = sorted(set(all_fresh_eps))
                ep_cnt = Counter(all_fresh_eps)
                fresh_duplicates = sorted([n for n, c in ep_cnt.items() if c > 1])

                new_status, new_detail = diagnose_season(
                    recognized=fresh_recognized,
                    total_episodes=rec.total_episodes,
                    season_finished=rec.season_finished,
                    has_duplicates=bool(fresh_duplicates),
                    duplicate_episodes=fresh_duplicates,
                )

                rec.local_episode_count = len(fresh_videos)
                rec.recognized_episodes_json = json.dumps(fresh_recognized)
                rec.duplicate_episodes_json = json.dumps(fresh_duplicates)
                rec.status = new_status
                rec.status_detail = new_detail
                log.info("季度状态自动重新计算: %s S%02d -> %s (%s)", rec.show_name, rec.season_number, new_status, new_detail)

            except Exception as fe:
                log.warning("重新计算状态失败: %s", fe)

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
    3. 检查源端剧集目录，若已无其他季且无视频文件，自动级联清理源端剧集目录空壳；
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

            # 门禁 3: 重新实时获取源端与目标端文件清单，执行集号与独有附件覆盖比对
            try:
                src_files = await cd2.list_files(source_season_path, force_refresh=True)
                target_files = await cd2.list_files(archive_target, force_refresh=True)

                src_video_eps: set[int] = set()
                src_subs: list[dict] = []
                unrecognized_videos: list[dict] = []
                for sf in src_files:
                    if sf.get("isDirectory"):
                        continue
                    sf_name = sf.get("name", "")
                    if is_video_file(sf_name):
                        p_sf = parse_episode(sf_name, sf.get("path", ""), sf.get("size", 0))
                        if not p_sf.is_extra:
                            if p_sf.episode_numbers:
                                src_video_eps.update(p_sf.episode_numbers)
                            else:
                                unrecognized_videos.append(sf)
                    elif is_subtitle_file(sf_name):
                        src_subs.append(sf)

                target_video_eps: set[int] = set()
                target_sub_names: set[str] = set()
                for tf in target_files:
                    if tf.get("isDirectory"):
                        continue
                    tf_name = tf.get("name", "")
                    if is_video_file(tf_name):
                        p_tf = parse_episode(tf_name, tf.get("path", ""), tf.get("size", 0))
                        if not p_tf.is_extra:
                            target_video_eps.update(p_tf.episode_numbers)
                    elif is_subtitle_file(tf_name):
                        target_sub_names.add(tf_name.lower())

                # 核验 A: 源端若未识别到任何有效正片集号，或含有无法识别集号的正片视频，绝对拦截删除（源集数与记录不变）
                if unrecognized_videos:
                    unrec_names = [f.get("name", "") for f in unrecognized_videos[:2]]
                    err_msg = f"{rec.show_name} S{rec.season_number:02d}: 源端含有未识别集号的正片视频 ({unrec_names})，证据不足拦截删除！"
                    log.warning(err_msg)
                    errors.append(err_msg)
                    continue

                if not src_video_eps:
                    err_msg = f"{rec.show_name} S{rec.season_number:02d}: 源端未识别出任何正片集号，证据不足拦截删除！"
                    log.warning(err_msg)
                    errors.append(err_msg)
                    continue

                # 核验 B: 目标端必须完整包含源端所有正片集号 (source_eps ⊆ target_eps)，证据不足坚决拦截（源集数与记录不变）
                uncovered_eps = sorted(src_video_eps - target_video_eps)
                if uncovered_eps:
                    err_msg = f"{rec.show_name} S{rec.season_number:02d}: 源端含有归档库未包含的集数 {uncovered_eps}，拦截删除！"
                    log.warning(err_msg)
                    errors.append(err_msg)
                    continue

                # 核验 C: 源端独有外挂字幕自动迁移至目标库
                for sub in src_subs:
                    s_name = sub.get("name", "")
                    if s_name.lower() not in target_sub_names:
                        try:
                            await cd2.move_file(sub.get("path"), archive_target)
                            log.info("源端残留清理前已安全迁移独有字幕: %s -> %s", s_name, archive_target)
                        except Exception as sub_mv_err:
                            log.warning("迁移独有字幕失败 (%s): %s", s_name, sub_mv_err)

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

                # 2. 检查源剧集目录是否为空壳 (F01 强化门禁)
                if show_path and show_path != source_season_path:
                    target_parent = archive_target.rsplit("/", 1)[0] if "/" in archive_target else None
                    await _safe_cleanup_source_show_dir(cd2, show_path, target_parent)

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

    cd2 = CloudDrive2Client()
    try:
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
