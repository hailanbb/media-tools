"""初检引擎 — 扫描剧集目录并生成诊断结果。

工作流：
1. 通过 CD2 列出目标目录下的所有子目录（每个子目录视为一部剧）
2. 进入每部剧的子目录，识别 Season 文件夹
3. 对每个 Season 文件夹中的视频文件进行集号解析
4. 通过 TMDB/MoviePilot 获取该季官方总集数
5. 对比本地文件与官方数据，生成诊断状态

诊断状态：
- archive_ready:   齐全可归档（本地集数 ≥ 官方集数 且该季已完结 且无重复）
- review_needed:   需要人工复核（存在重复集、可疑文件等）
- download_needed: 缺集待补（该季已完结但本地缺少已播出集）
- airing:          连载中（该季尚未完结）
- unknown:         无法判断（无法匹配 TMDB 信息等）
"""

from __future__ import annotations

import asyncio

import json
import re
from dataclasses import dataclass, field

from app.core.episode_parser import ParsedEpisode, is_video_file, parse_episode

# 非正片判定正则（预告、片花、特辑、宣传片、花絮、Trailer、Teaser 等，杜绝污染正片总集数）
NON_REGULAR_EP_PATTERN = re.compile(
    r"(?:预告|片花|特辑|花絮|宣传|特番|番外|制作特辑|花絮篇|Trailer|Teaser|Preview|Making\s*of|Extra|Special)",
    re.IGNORECASE,
)


@dataclass
class SeasonDiagnosis:
    """单季诊断结果。"""

    show_name: str
    show_path: str
    season_number: int
    season_path: str

    # 本地数据
    local_files: list[ParsedEpisode] = field(default_factory=list)
    local_episode_count: int = 0
    recognized_episodes: list[int] = field(default_factory=list)
    duplicate_episodes: list[int] = field(default_factory=list)

    # 远程数据
    tmdb_id: str | None = None
    total_episodes: int | None = None
    season_finished: bool = False

    # 缺失集
    missing_episodes: list[int] = field(default_factory=list)

    # 诊断结论
    status: str = "unknown"  # archive_ready / review_needed / download_needed / airing / unknown
    status_detail: str = ""
    archive_target_path: str | None = None


@dataclass
class InspectionResult:
    """一次完整初检的结果。"""

    task_id: str
    source_path: str
    seasons: list[SeasonDiagnosis] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # 汇总
    archive_ready_count: int = 0
    review_needed_count: int = 0
    download_needed_count: int = 0
    airing_count: int = 0
    already_archived_count: int = 0

    def compute_summary(self) -> None:
        """根据 seasons 计算汇总统计。"""
        self.archive_ready_count = sum(1 for s in self.seasons if s.status == "archive_ready")
        self.review_needed_count = sum(1 for s in self.seasons if s.status == "review_needed")
        self.download_needed_count = sum(1 for s in self.seasons if s.status == "download_needed")
        self.airing_count = sum(1 for s in self.seasons if s.status == "airing")
        self.already_archived_count = sum(1 for s in self.seasons if s.status == "already_archived")


def compute_archive_target(show_path: str, season_path: str, archive_root: str, global_source_root: str = "") -> str:
    """计算归档目标路径。"""
    if not archive_root:
        return ""
    arch_norm = archive_root.rstrip("/")
    if season_path.startswith(arch_norm + "/") or season_path == arch_norm:
        return season_path
    if global_source_root:
        src_norm = global_source_root.rstrip("/")
        if season_path.startswith(src_norm + "/") or season_path == src_norm:
            rel = season_path[len(src_norm):].lstrip("/")
            return f"{arch_norm}/{rel}"
    parts = season_path.rstrip("/").split("/")
    if len(parts) >= 2:
        return f"{arch_norm}/{parts[-2]}/{parts[-1]}"
    return f"{arch_norm}/{parts[-1]}"


def diagnose_season(
    recognized: list[int],
    total_episodes: int | None,
    season_finished: bool,
    has_duplicates: bool,
    duplicate_episodes: list[int] | None = None,
) -> tuple[str, str]:
    """根据本地集号与官方数据做诊断。

    Args:
        recognized: 已识别的集号列表（去重后）
        total_episodes: 官方总集数（None 表示无法获取）
        season_finished: 该季是否已完结
        has_duplicates: 是否有重复集
        duplicate_episodes: 可选，具体重复的集数列表

    Returns:
        (status, detail) 元组
    """
    if total_episodes is None:
        return "unknown", "无法获取该季的官方集数信息"

    recognized_set = set(recognized)
    expected_set = set(range(1, total_episodes + 1))
    missing = sorted(expected_set - recognized_set)

    # 1. 连载中剧集（官方尚未完结）：暂留源或复核
    if not season_finished:
        if has_duplicates:
            dup_text = f"第 {','.join(str(d) for d in sorted(duplicate_episodes))} 集" if duplicate_episodes else "部分集数"
            return "review_needed", f"连载中存在重复版本 ({dup_text})，需清理低质冗余"
        return "airing", f"该季尚在连载中，当前 {len(recognized_set)}/{total_episodes} 集"

    # 2. 已完结但有真实缺集：【集齐大于一切】缺集属实时优先归入“真缺集待补”
    if missing:
        missing_text = f"第 {','.join(str(m) for m in missing[:5])} 集" if len(missing) <= 5 else f"{len(missing)} 集"
        if has_duplicates:
            dup_text = f"第 {','.join(str(d) for d in sorted(duplicate_episodes))} 集" if duplicate_episodes else "部分集数"
            return "download_needed", f"已完结但缺失 {missing_text}（同时存在重复集: {dup_text}，优先补缺，亦可顺带清理冗余）"
        else:
            return "download_needed", f"已完结但缺失 {missing_text}，缺少已播出集：{missing}"

    # 3. 已完结且全部集数已就绪，但存在同集重复版本：进入复核清理
    if has_duplicates:
        dup_text = f"第 {','.join(str(d) for d in sorted(duplicate_episodes))} 集" if duplicate_episodes else "部分集数"
        return "review_needed", f"全 {total_episodes} 集已齐，但存在重复版本 ({dup_text})，需清理低质冗余后归档"

    # 4. 已完结、无缺集、无重复：完美就绪，可归档
    return "archive_ready", f"全 {total_episodes} 集齐全，可归档"


async def run_inspection(
    task_id: str,
    source_path: str,
    cd2_client=None,
    mp_client=None,
    tmdb_client=None,
    archive_root: str = "",
    global_source_root: str = "",
    progress_callback=None,
    control_event=None,
    cancelled_tasks=None,
    concurrency: int = 3,
) -> InspectionResult:
    """执行初检流程 — 扫描 CD2 目录、解析集号、查询 TMDB、生成诊断。

    流程：
    1. CD2 列出 source_path 下的子目录（每个 = 一部剧）
    2. 对每部剧，扫描 Season 子目录
    3. 解析每个视频文件的集号
    4. 通过 MoviePilot/TMDB 获取官方集数与完结状态
    5. 调用 diagnose_season() 生成诊断

    Args:
        task_id: 任务 ID
        source_path: 待检目录路径（CD2 路径）
        cd2_client: CloudDrive2 客户端实例
        mp_client: MoviePilot 客户端实例
        tmdb_client: TMDB 客户端实例
        archive_root: 归档根路径，用于计算归档目标

    Returns:
        InspectionResult 初检结果
    """
    import logging
    from collections import Counter

    log = logging.getLogger("mediaflow.inspector")
    result = InspectionResult(task_id=task_id, source_path=source_path)

    if not cd2_client:
        log.warning("未提供 CD2 客户端，返回空结果")
        return result

    # ── 1. 递归发现剧集目录 ──────────────────────────────
    # 目录结构可能是多级分类：源根/电视剧/国产剧/XXX (2024)/Season 1/
    # 需要递归直到找到含 Season 子目录或视频文件的目录
    show_list: list[dict] = []  # [{"name": ..., "path": ...}]
    archive_show_cache: dict[str, list[dict]] = {}  # 跨库感知：归档目录内容缓存
    if progress_callback:
        await progress_callback(0, 0, "", "正在扫描目录结构...")
    await _discover_shows(cd2_client, source_path, show_list, max_depth=4, log=log, progress_callback=progress_callback)

    total_shows = len(show_list)
    log.info("源目录 %s 下递归发现 %d 个剧集目录", source_path, total_shows)
    if progress_callback:
        await progress_callback(0, total_shows, "", f"发现 {total_shows} 个剧集目录，开始逐一分析...")

    # ── 2. 多子任务并发加速分析 ─────────────────────────
    completed_shows = 0
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _inspect_single_show(show_item: dict) -> list[SeasonDiagnosis]:
        show_name = show_item["name"]
        show_path = show_item["path"]
        diagnosed_seasons: list[SeasonDiagnosis] = []

        try:
            all_items = await cd2_client.list_files(show_path)
        except Exception as e:
            result.errors.append(f"扫描 {show_name} 失败: {e}")
            log.error("扫描 %s 失败: %s", show_path, e)
            return []

        season_dirs: list[tuple[int, dict]] = []
        loose_files: list[dict] = []

        for item in all_items:
            name = item["name"]
            if item["isDirectory"]:
                sn = _parse_season_number(name)
                if sn is not None:
                    season_dirs.append((sn, item))
            else:
                if is_video_file(name):
                    loose_files.append(item)

        if not season_dirs and loose_files:
            season_dirs = [(1, {"name": show_name, "path": show_path, "isDirectory": True})]

        if not season_dirs:
            return []

        tmdb_id_num: int | None = None
        tmdb_seasons_data: dict[int, dict] = {}

        tmdb_match = re.search(r"\[tmdbid[=:](\d+)\]", show_name, re.IGNORECASE)
        if tmdb_match:
            tmdb_id_num = int(tmdb_match.group(1))

        if mp_client:
            if tmdb_id_num:
                tmdb_seasons_data = await _fetch_seasons_by_tmdb_id(mp_client, tmdb_id_num, log)
            else:
                tmdb_id_num, tmdb_seasons_data = await _fetch_tmdb_via_mp(mp_client, show_name, log)

        for season_num, season_item in sorted(season_dirs, key=lambda x: x[0]):
            season_path = season_item["path"]
            try:
                if season_path == show_path:
                    video_items = loose_files
                else:
                    season_files = await cd2_client.list_files(season_path)
                    video_items = [f for f in season_files if not f["isDirectory"] and is_video_file(f["name"])]
            except Exception as e:
                result.errors.append(f"扫描 {show_name}/S{season_num:02d} 失败: {e}")
                continue

            parsed_episodes: list[ParsedEpisode] = []
            for vf in video_items:
                parsed_episodes.append(parse_episode(vf["name"], vf["path"], vf.get("size", 0)))

            regular_episodes = [ep for ep in parsed_episodes if not ep.is_extra]
            target_eps_list = regular_episodes if regular_episodes else parsed_episodes

            all_ep_nums: list[int] = []
            for ep in target_eps_list:
                all_ep_nums.extend(ep.episode_numbers)

            recognized = sorted(set(all_ep_nums))
            ep_counter = Counter(all_ep_nums)
            duplicates = sorted([n for n, cnt in ep_counter.items() if cnt > 1])

            total_eps: int | None = None
            season_finished = False
            if season_num in tmdb_seasons_data:
                info = tmdb_seasons_data[season_num]
                total_eps = info.get("total")
                season_finished = info.get("finished", False)

            status, detail = diagnose_season(
                recognized=recognized,
                total_episodes=total_eps,
                season_finished=season_finished,
                has_duplicates=bool(duplicates),
                duplicate_episodes=duplicates,
            )

            archive_target = ""
            if archive_root:
                archive_target = compute_archive_target(show_path, season_path, archive_root, global_source_root)

            missing: list[int] = []
            if total_eps and total_eps > 0:
                missing = [ep for ep in range(1, total_eps + 1) if ep not in recognized]

            # 跨库查重校验
            if archive_target and status != "airing":
                try:
                    target_parent = archive_target.rsplit("/", 1)[0]
                    if target_parent not in archive_show_cache:
                        try:
                            archive_show_cache[target_parent] = await cd2_client.list_files(target_parent)
                        except Exception:
                            archive_show_cache[target_parent] = []

                    target_dir_name = archive_target.rsplit("/", 1)[-1]
                    exists_in_archive = any(
                        item["name"] == target_dir_name and item["isDirectory"]
                        for item in archive_show_cache[target_parent]
                    )

                    if exists_in_archive:
                        status = "already_archived"
                        detail = f"归档库对应目录已存在 ({archive_target})，归类为异常剧集（归档库已有）"
                except Exception as arc_err:
                    log.warning("跨库感知查重探测失败 %s: %s", archive_target, arc_err)

            diag = SeasonDiagnosis(
                show_name=show_name,
                show_path=show_path,
                tmdb_id=tmdb_id_num,
                season_number=season_num,
                season_path=season_path,
                total_episodes=total_eps,
                local_episode_count=len(parsed_episodes),
                recognized_episodes=recognized,
                missing_episodes=missing,
                duplicate_episodes=duplicates,
                status=status,
                status_detail=detail,
                archive_target_path=archive_target,
                season_finished=season_finished,
            )
            diagnosed_seasons.append(diag)

        return diagnosed_seasons

    async def _worker(show_item: dict):
        nonlocal completed_shows
        show_name = show_item["name"]

        # 检查停止信号
        if cancelled_tasks and task_id in cancelled_tasks:
            return

        # 检查暂停挂起信号
        if control_event and not control_event.is_set():
            if progress_callback:
                try:
                    await progress_callback(completed_shows, total_shows, show_name, "初检任务已暂停，等待恢复...")
                except Exception:
                    pass
            await control_event.wait()

        if cancelled_tasks and task_id in cancelled_tasks:
            return

        async with sem:
            if cancelled_tasks and task_id in cancelled_tasks:
                return

            seasons = await _inspect_single_show(show_item)

            async with lock:
                result.seasons.extend(seasons)
                completed_shows += 1
                if progress_callback:
                    try:
                        pct = int((completed_shows / total_shows) * 100) if total_shows > 0 else 100
                        await progress_callback(
                            completed_shows,
                            total_shows,
                            show_name,
                            f"正在并行加速分析 ({completed_shows}/{total_shows}): {show_name}",
                        )
                    except Exception:
                        pass

    if show_list:
        tasks = [asyncio.create_task(_worker(item)) for item in show_list]
        await asyncio.gather(*tasks)

        result.compute_summary()
    log.info(
        "初检完成: %d季 (归档=%d, 复核=%d, 补集=%d, 连载=%d, 已归档=%d)",
        len(result.seasons),
        result.archive_ready_count,
        result.review_needed_count,
        result.download_needed_count,
        result.airing_count,
        result.already_archived_count,
    )
    return result


# ──────────────────────────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────────────────────────

_SEASON_PATTERNS = [
    re.compile(r"[Ss]eason\s*(\d+)", re.IGNORECASE),
    re.compile(r"[Ss](\d{1,2})(?:\s|$|[-_])"),
    re.compile(r"第\s*(\d+)\s*季"),
    re.compile(r"Specials?", re.IGNORECASE),  # Season 0
]

# 不应被视为剧集目录的目录名
_SKIP_DIRS = {"mount-check", ".recycle", "@eaDir", "#recycle", ".trash"}


async def _discover_shows(
    cd2_client, dir_path: str, results: list[dict], max_depth: int = 4, log=None, progress_callback=None
) -> None:
    """递归发现剧集目录。

    判断标准：如果目录下包含 Season 子目录或视频文件，则视为剧集目录。
    否则继续递归其子目录（如分类目录 电视剧/国产剧 等）。

    Args:
        cd2_client: CD2 客户端
        dir_path: 当前扫描路径
        results: 累积的剧集目录列表
        max_depth: 最大递归深度
        log: 日志记录器
        progress_callback: 进度回调
    """
    if max_depth <= 0:
        return

    dir_basename = dir_path.rstrip("/").rsplit("/", 1)[-1] if "/" in dir_path else dir_path
    if progress_callback:
        try:
            await progress_callback(0, 0, dir_basename, f"正在递归探测目录: {dir_basename}")
        except Exception:
            pass

    try:
        items = await cd2_client.list_files(dir_path)
    except Exception as e:
        if log:
            log.warning("递归扫描 %s 失败: %s", dir_path, e)
        return

    has_season_dir = False
    has_video = False
    subdirs: list[dict] = []

    for item in items:
        name = item["name"]
        if item["isDirectory"]:
            if name in _SKIP_DIRS:
                continue
            sn = _parse_season_number(name)
            if sn is not None:
                has_season_dir = True
            subdirs.append(item)
        else:
            if is_video_file(name):
                has_video = True

    if has_season_dir or has_video:
        # 这是一个剧集目录
        dir_name = dir_path.rstrip("/").rsplit("/", 1)[-1] if "/" in dir_path else dir_path
        results.append({"name": dir_name, "path": dir_path})
    else:
        # 这是分类目录，继续递归
        for sub in subdirs:
            await _discover_shows(cd2_client, sub["path"], results, max_depth - 1, log, progress_callback)



def _parse_season_number(dirname: str) -> int | None:
    """从目录名提取季号。返回 None 表示不是 Season 目录。"""
    for i, pat in enumerate(_SEASON_PATTERNS):
        m = pat.search(dirname)
        if m:
            if i == 3:  # Specials
                return 0
            return int(m.group(1))
    return None


async def _fetch_seasons_by_tmdb_id(
    mp_client, tmdb_id: int, log
) -> dict[int, dict]:
    """已知 tmdb_id，通过 MoviePilot 获取季度信息并进行非正片预告剔除与科学完结判定。

    Returns:
        {season_num: {"total": N, "finished": bool}}
    """
    from datetime import datetime as _dt

    seasons_data: dict[int, dict] = {}
    try:
        seasons_raw = await mp_client.get_tmdb_seasons(tmdb_id)
        if isinstance(seasons_raw, list):
            today = _dt.now()
            for s in seasons_raw:
                sn = s.get("season_number")
                if sn is None:
                    continue
                sn = int(sn)
                raw_ep_count = s.get("episode_count") or 0
                finished = False
                real_total = raw_ep_count

                if raw_ep_count > 0:
                    try:
                        episodes_info = await mp_client.get_tmdb_episodes(tmdb_id, sn)
                        if isinstance(episodes_info, list) and episodes_info:
                            # 1. 过滤 TMDB 中混入的预告片、花絮、特辑等非正片伪条目
                            regular_eps = []
                            for ep in episodes_info:
                                ep_name = str(ep.get("name") or "")
                                if NON_REGULAR_EP_PATTERN.search(ep_name):
                                    if log:
                                        log.info("TMDB tmdb_id=%d S%02d: 自动剔除非正片条目 [%s]", tmdb_id, sn, ep_name)
                                    continue
                                regular_eps.append(ep)

                            if regular_eps:
                                real_total = len(regular_eps)

                            # 2. 提取正片中有效播出日期
                            valid_air_dates = []
                            for ep in (regular_eps or episodes_info):
                                ad = ep.get("air_date")
                                if ad:
                                    try:
                                        valid_air_dates.append(_dt.strptime(ad, "%Y-%m-%d"))
                                    except Exception:
                                        pass

                            if valid_air_dates and max(valid_air_dates) < today:
                                finished = True

                            # 3. 首播年份保底规则（历史老剧绝不能因缺少某集播出时间误判为连载追更）
                            season_air = s.get("air_date") or ""
                            if season_air:
                                try:
                                    s_date = _dt.strptime(season_air, "%Y-%m-%d")
                                    # 若首播距今已超 180 天且已有正片播出，判定该季已播出完结
                                    if (today - s_date).days > 180 and valid_air_dates:
                                        finished = True
                                except Exception:
                                    pass
                        else:
                            finished = True
                    except Exception:
                        finished = True

                seasons_data[sn] = {
                    "total": real_total if real_total > 0 else None,
                    "finished": finished,
                }
            if log:
                log.info("TMDB tmdb_id=%d: 获取到 %d 季信息 (已清洗非正片)", tmdb_id, len(seasons_data))
    except Exception as e:
        if log:
            log.warning("获取 tmdb_id=%d 的季度信息失败: %s", tmdb_id, e)

    return seasons_data


async def _fetch_tmdb_via_mp(
    mp_client, show_name: str, log
) -> tuple[int | None, dict[int, dict]]:
    """通过 MoviePilot 搜索剧集并获取 TMDB 季度信息。

    Returns:
        (tmdb_id, {season_num: {"total": N, "finished": bool}})
    """
    tmdb_id: int | None = None
    seasons_data: dict[int, dict] = {}

    # 清洗剧名：去掉年份、括号/方括号内容等
    clean_name = re.sub(r"\s*[\(\[（].*?[\)\]）]\s*", " ", show_name).strip()
    clean_name = re.sub(r"\s*\(\d{4}\)\s*", " ", clean_name).strip()
    clean_name = re.sub(r"\s+\d{4}$", "", clean_name).strip()

    if not clean_name:
        clean_name = show_name

    try:
        # 先用豆瓣源搜索（国产剧命中率更高）
        results = await mp_client.search_media(clean_name, source="douban")

        if not results:
            results = await mp_client.search_media(clean_name, source="tmdb")

        if results and isinstance(results, list):
            first = results[0] if results else None
            if first and isinstance(first, dict):
                tmdb_id = (
                    first.get("tmdb_id")
                    or first.get("tmdbid")
                    or first.get("id")
                )
                if tmdb_id is not None:
                    tmdb_id = int(tmdb_id) if tmdb_id else None

        if tmdb_id:
            seasons_data = await _fetch_seasons_by_tmdb_id(mp_client, tmdb_id, log)
            log.info("TMDB 搜索匹配: %s → tmdb_id=%s", show_name, tmdb_id)

    except Exception as e:
        log.warning("搜索 %s 的 TMDB 信息失败: %s", show_name, e)

    return tmdb_id, seasons_data
