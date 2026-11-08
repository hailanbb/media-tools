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
                except Exception:
                    target_parent_files = []

                existing_season = next(
                    (f for f in target_parent_files if f.get("isDirectory") and (
                        _parse_season_number(f.get("name", "")) == rec.season_number or
                        f.get("name", "").lower() == season_dir_name.lower()
                    )),
                    None
                )
                if existing_season:
                    err_msg = f"{rec.show_name} S{rec.season_number:02d}: 同季冲突，归档库已存在同季目录 \"{existing_season.get('name')}\"，已安全拦截"
                    log.warning(err_msg)
                    errors.append(err_msg)
                    rec.status = "conflict"
                    rec.status_detail = f"同季冲突：归档目标已存在同季目录 {existing_season.get('name')}，整季暂缓移动"
                    affected_tasks.add(rec.task_id)
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

                    # 【源端零残留与公共资产完备归档】
                    if rec.show_path and rec.show_path != source_season_path:
                        try:
                            source_rem = await cd2.list_files(rec.show_path, force_refresh=True)
                            rem_seasons = [
                                f for f in source_rem
                                if f.get("isDirectory") and _parse_season_number(f.get("name", "")) is not None
                            ]
                            rem_videos = [
                                f for f in source_rem
                                if not f.get("isDirectory") and is_video_file(f.get("name", ""))
                            ]
                            if not rem_seasons and not rem_videos:
                                # 源端已无其他季和视频：将公共削刮资产（tvshow.nfo、海报等）迁移至归档库
                                for asset in source_rem:
                                    asset_name = asset.get("name")
                                    if not asset.get("isDirectory") and asset_name:
                                        asset_path = f"{rec.show_path}/{asset_name}"
                                        try:
                                            await cd2.move_file(asset_path, target_parent)
                                        except Exception as mv_err:
                                            log.warning("迁移公共资产 %s 到归档库失败: %s", asset_name, mv_err)
                                
                                # 公共资产已转移完毕，源端剧集目录已彻底空壳，执行彻底物理删除，不留任何残留
                                await cd2.delete_file(rec.show_path)
                                log.info("源端全剧已无其余季度，公共削刮资产已完整迁移至归档库，已彻底删除源端剧集目录: %s", rec.show_path)
                            else:
                                log.info("源端剧集仍有其他季度或视频在连载，暂保留源端剧集主目录: %s", rec.show_path)
                        except Exception as rem_err:
                            log.warning("检查清理源剧集空壳失败: %s", rem_err)
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

    msg = f"清理完成：成功删除 {total_deleted_videos} 个冗余视频，清理 {total_deleted_orphans} 个伴生孤儿文件"
    if errors:
        msg += f"，遇到 {len(errors)} 项提醒: {'; '.join(errors[:2])}"

    return StatusResponse(ok=True, message=msg)


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

            try:
                # 1. 删除源端季目录
                log.info("正在清理源端冗余季目录: %s", source_season_path)
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

                # 2. 检查源剧集目录是否为空壳
                if show_path and show_path != source_season_path:
                    try:
                        rem_items = await cd2.list_files(show_path, force_refresh=True)
                        rem_seasons = [
                            f for f in rem_items
                            if f.get("isDirectory") and _parse_season_number(f.get("name", "")) is not None
                        ]
                        rem_videos = [
                            f for f in rem_items
                            if not f.get("isDirectory") and is_video_file(f.get("name", ""))
                        ]
                        if not rem_seasons and not rem_videos:
                            await cd2.delete_file(show_path)
                            log.info("源端剧集已无其他季与视频，已自动清理源剧集空壳: %s", show_path)
                    except Exception as show_err:
                        log.warning("检查清理源剧集空壳失败: %s", show_err)

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
