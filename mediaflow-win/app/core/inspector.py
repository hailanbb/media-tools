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
import urllib.parse
import httpx
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.clients.clouddrive2 import GrpcError
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
    archive_check_count: int = 0
    already_archived_count: int = 0

    def compute_summary(self) -> None:
        """根据 seasons 计算汇总统计。"""
        self.archive_ready_count = sum(1 for s in self.seasons if s.status == "archive_ready")
        self.review_needed_count = sum(1 for s in self.seasons if s.status == "review_needed")
        self.download_needed_count = sum(1 for s in self.seasons if s.status == "download_needed")
        self.airing_count = sum(1 for s in self.seasons if s.status == "airing")
        self.archive_check_count = sum(s.status == "archive_check" for s in self.seasons)
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


async def _find_archive_season(cd2, cache: dict[str, list[dict]], expected_path: str,
                               season_number: int, tagged_show: re.Match | None) -> tuple[str | None, bool]:
    """在归档分类内按显式作品 ID 和季号匹配；返回实际路径及是否跨别名。"""
    async def directories(path):
        if path not in cache:
            try:
                entries = await cd2.list_files(path)
            except FileNotFoundError:
                entries = []
            except GrpcError as exc:
                if exc.status != 5:
                    raise
                entries = []
            cache[path] = [e for e in entries if e["isDirectory"]]
        return cache[path]

    parent, season_name = expected_path.rsplit("/", 1)
    is_season_folder = _parse_season_number(season_name) == season_number
    alias = False
    if tagged_show and is_season_folder:
        category, expected_name = parent.rsplit("/", 1)
        tag = tagged_show.group(1)
        same_work = [e["name"] for e in await directories(category or "/")
                     if re.search(rf"\[tmdbid[=:]{tag}\]", e["name"], re.I)]
        if len(same_work) > 1:
            raise ValueError("同分类有多个相同 TMDB ID 的归档目录：" + "、".join(same_work))
        if not same_work:
            return None, False
        alias = same_work[0] != expected_name
        parent = f"{category}/{same_work[0]}"

    names = [e["name"] for e in await directories(parent)
             if e["name"] == season_name or
             (is_season_folder and _parse_season_number(e["name"]) == season_number)]
    if len(names) > 1:
        raise ValueError("多个归档目录对应同一季：" + "、".join(names))
    if not names:
        return None, alias
    return f"{parent}/{names[0]}", alias


def _verified_video(ep: ParsedEpisode, season_number: int) -> bool:
    return (ep.file_size_bytes > 0 and not ep.is_extra and not ep.is_special
            and ep.season_number in (None, season_number) and bool(ep.episode_numbers))


def _source_inventory(items: list[dict], parsed: list[ParsedEpisode], season_number: int) -> str:
    if not items:
        return "源季目录为空"
    folders = sum(item["isDirectory"] for item in items)
    types = Counter(PurePosixPath(item["name"]).suffix.lower() for item in items
                    if not item["isDirectory"] and not is_video_file(item["name"]))
    images = {".jpg", ".jpeg", ".png", ".webp"}
    labels = []
    for ext, count in types.most_common():
        kind = ext.lstrip(".").upper() or "无扩展名"
        labels.append(f"{count} 张 {kind} 图片" if ext in images else f"{count} 个 {kind} 文件")
    types_text = "、".join(labels)
    sidecars = {".nfo", ".jpg", ".jpeg", ".png", ".webp", ".srt", ".ass", ".ssa", ".sub", ".idx", ".sup"}
    if not parsed:
        if not folders and types.keys() <= sidecars:
            return f"源季目录只有 {types_text}，没有视频文件"
        detail = "源季目录本层没有支持的视频文件"
        if types:
            detail += f"，有 {types_text}"
    else:
        detail = f"源季目录有 {len(parsed)} 个视频文件"
        reasons: dict[str, list[str]] = {}
        for ep in parsed:
            if ep.is_extra or ep.is_special:
                reason = "标记为花絮或特别篇"
            elif ep.file_size_bytes <= 0:
                reason = "文件大小为零或未知"
            elif ep.season_number not in (None, season_number):
                reason = "文件名标注了其他季号"
            elif not ep.episode_numbers:
                reason = "未能从文件名解析出集号"
            else:
                continue
            reasons.setdefault(reason, []).append(ep.filename)
        for reason, names in reasons.items():
            detail += f"；{len(names)} 个{reason}（如 {names[0]}）"
        if not reasons:
            detail += "，均已识别出本季集号"
    if folders:
        detail += f"；另有 {folders} 个子目录，尚未核对里面的文件"
    return detail


def separate_outliers(ep_nums: list[int], expected_max: int | None = None) -> tuple[list[int], list[int]]:
    """将断崖式孤立异常集号（如正片为 1..12，跳空至 56）从正片集号序列中剥离。

    规则与底线保护：
    1. 只要集号在官方总集数内（ep <= expected_max），绝对属于正片范围，严禁作为 outlier 剥离；
    2. 只有当集号超出官方总集数（ep > expected_max），且与前一段连续集号出现严重断崖（gap >= 8 或跳空到 50+）时，
       或者在无官方集数时出现严重突兀跳空（gap >= 15 且集号 >= 50 且仅 1~2 个孤立集），才判定为孤立断崖花絮予以剥离。

    Returns:
        (regular_ep_nums, outlier_ep_nums)
    """
    if not ep_nums:
        return [], []
    sorted_eps = sorted(set(ep_nums))
    if len(sorted_eps) <= 1:
        return sorted_eps, []

    regular: list[int] = []
    outliers: list[int] = []

    for i, ep in enumerate(sorted_eps):
        if i == 0:
            regular.append(ep)
            continue
        prev = regular[-1]
        gap = ep - prev
        remaining = len(sorted_eps) - i

        is_break = False
        if expected_max and expected_max > 0:
            if ep > expected_max:
                if gap >= 8 or (ep >= 50 and gap >= 5) or (remaining <= 2 and gap >= 10):
                    is_break = True
        else:
            if gap >= 15 and ep >= 50 and remaining <= 2:
                is_break = True

        if is_break:
            outliers.extend(sorted_eps[i:])
            break
        else:
            regular.append(ep)

    return regular, outliers


def diagnose_season(
    recognized: list[int],
    total_episodes: int | None,
    season_finished: bool,
    has_duplicates: bool,
    duplicate_episodes: list[int] | None = None,
    allow_bonus_specials: bool = False,
) -> tuple[str, str]:
    """根据本地集号与官方数据做诊断。

    Args:
        recognized: 已识别的集号列表（去重后）
        total_episodes: 官方总集数（None 表示无法获取）
        season_finished: 该季是否已完结
        has_duplicates: 是否有重复集
        duplicate_episodes: 可选，具体重复的集数列表
        allow_bonus_specials: 是否允许在正片全齐时容纳紧邻的 1~3 集加更特辑直接归档

    Returns:
        (status, detail) 元组
    """
    if total_episodes is None or total_episodes <= 0:
        return "unknown", "无法获取该季的官方集数信息"

    recognized_set = set(recognized)
    expected_set = set(range(1, total_episodes + 1))
    missing = sorted(expected_set - recognized_set)
    out_of_range = sorted(recognized_set - expected_set)

    # 1. 连载中剧集（官方尚未完结）：暂留源或复核
    if not season_finished:
        if has_duplicates:
            dup_text = f"第 {','.join(str(d) for d in sorted(duplicate_episodes))} 集" if duplicate_episodes else "部分集数"
            return "review_needed", f"连载中存在重复版本 ({dup_text})，需清理低质冗余"
        return "airing", f"该季尚在连载中，当前 {len(recognized_set)}/{total_episodes} 集"

    # 2. 已完结但有真实缺集：【集齐大于一切】缺集属实时优先归入“真缺集待补”
    if missing:
        missing_text = f"第 {','.join(str(m) for m in missing[:5])} 集" if len(missing) <= 5 else f"{len(missing)} 集"
        out_desc = f"（同时包含超范围集: {out_of_range}）" if out_of_range else ""
        if has_duplicates:
            dup_text = f"第 {','.join(str(d) for d in sorted(duplicate_episodes))} 集" if duplicate_episodes else "部分集数"
            return "download_needed", f"已完结但缺失 {missing_text}{out_desc}（同时存在重复集: {dup_text}，优先补缺，亦可顺带清理冗余）"
        else:
            return "download_needed", f"已完结但缺失 {missing_text}，缺少已播出集：{missing}{out_desc}"

    # 3. 超范围集号容错与异常保护 (P1-4):
    if out_of_range:
        # 若允许加更特辑容错且官方总集数正常 (>=6)，超出部分紧邻总集数（<= total + 4 且数量 <= 3），无重复集，视为附随特辑直接就绪可归档
        if allow_bonus_specials and total_episodes >= 6 and max(out_of_range) <= total_episodes + 4 and len(out_of_range) <= 3 and not has_duplicates:
            extra_desc = f"（附带 {len(out_of_range)} 集加更/特辑: {out_of_range}）"
            return "archive_ready", f"全 {total_episodes} 集齐全{extra_desc}，可归档"
        dup_note = f"；第 {','.join(map(str, duplicate_episodes))} 集另有重复版本" if duplicate_episodes else ""
        return "review_needed", f"当前资料收录 {total_episodes} 集，本地另有集号 {out_of_range} 待核实{dup_note}。"

    # 4. 已完结且全部集数已就绪，但存在同集重复版本：进入复核清理
    if has_duplicates:
        dup_text = f"第 {','.join(str(d) for d in sorted(duplicate_episodes))} 集" if duplicate_episodes else "部分集数"
        return "review_needed", f"全 {total_episodes} 集已齐，但存在重复版本 ({dup_text})，请复核同集文件后再归档"

    # 5. 已完结、无缺集、无重复、无超范围：完美就绪，可归档
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

    log = logging.getLogger("mediaflow.inspector")
    result = InspectionResult(task_id=task_id, source_path=source_path)

    if not cd2_client:
        log.warning("未提供 CD2 客户端，返回空结果")
        return result

    local_tmdb_client = None
    if tmdb_client is None:
        try:
            from app.clients.tmdb import TMDBClient
            local_tmdb_client = TMDBClient()
        except Exception:
            local_tmdb_client = None
    active_tmdb = tmdb_client or local_tmdb_client

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

        needed_seasons = [sn for sn, _ in season_dirs]
        resolved_tmdb_id, tmdb_seasons_data = await _resolve_show_metadata(
            show_name=show_name,
            needed_seasons=needed_seasons,
            tmdb_id_num=tmdb_id_num,
            mp_client=mp_client,
            tmdb_client=active_tmdb,
            log=log,
        )
        if resolved_tmdb_id:
            tmdb_id_num = resolved_tmdb_id

        for season_num, season_item in sorted(season_dirs, key=lambda x: x[0]):
            season_path = season_item["path"]
            try:
                season_files = all_items if season_path == show_path else await cd2_client.list_files(season_path)
                video_items = [f for f in season_files if not f["isDirectory"] and is_video_file(f["name"])]
            except Exception as e:
                result.errors.append(f"扫描 {show_name}/S{season_num:02d} 失败: {e}")
                continue

            parsed_episodes: list[ParsedEpisode] = []
            for vf in video_items:
                parsed_episodes.append(parse_episode(vf["name"], vf["path"], vf.get("size", 0)))

            regular_episodes = [ep for ep in parsed_episodes if _verified_video(ep, season_num)]

            all_ep_nums: list[int] = []
            for ep in regular_episodes:
                all_ep_nums.extend(ep.episode_numbers)

            total_eps: int | None = None
            season_finished = False
            edition_note = ""
            db_tot: int | None = None
            tmdb_tot: int | None = None
            is_split = False
            info = tmdb_seasons_data.get(season_num, {})
            if (mp_client and info.get("total") and max(all_ep_nums, default=0) > info["total"]
                    and not info.get("douban_checked")):
                _, secondary = await _fetch_seasons_via_douban(
                    mp_client, show_name, log, [season_num], tmdb_id=tmdb_id_num,
                    season_years={season_num: info.get("season_year")})
                info = _merge_season_sources(info, secondary.get(season_num))
            if info:
                total_eps = info.get("total")
                season_finished = info.get("finished", False)
                db_tot = info.get("douban_total")
                tmdb_tot = info.get("tmdb_total") or total_eps
                is_split = info.get("is_split_edition", False)

            # 智能剥离断崖孤立集号（如正片 1..12，断崖孤立出现 56）
            # 优先以参考集数作为 expected_max 阈值
            ref_max = max(db_tot or 0, total_eps or 0) or None
            regular_nums, outlier_nums = separate_outliers(all_ep_nums, expected_max=ref_max)
            recognized = sorted(set(regular_nums)) if regular_nums else sorted(set(all_ep_nums))
            if not regular_nums:
                outlier_nums = []

            # 文件只能佐证某个来源的集号范围，不能单独证明版型或总集数。
            if db_tot and tmdb_tot and db_tot != tmdb_tot:
                smaller, larger = sorted((db_tot, tmdb_tot))
                if is_split:
                    if info.get("bound") and db_tot == larger:
                        total_eps = db_tot
                    elif max(recognized, default=0) > smaller:
                        total_eps = larger
                    elif recognized == list(range(1, smaller + 1)):
                        total_eps = smaller
                elif db_tot > tmdb_tot:
                    total_eps = db_tot
                edition_note = (f"（TMDB 收录 {tmdb_tot} 集，豆瓣条目 {info['douban_id']} "
                                f"收录 {db_tot} 集，本次按 {total_eps} 集核对）")

            set_recognized = set(recognized)
            ep_counter = Counter(n for n in all_ep_nums if n in set_recognized)
            duplicates = sorted([n for n, cnt in ep_counter.items() if cnt > 1])

            # 超范围集数/缺失官方集数智能自愈校准保护 (Auto-Recalibrate on Overflow / Missing Total)
            # 已知作品/季仍有无法解释的越界时保留差异，不能按文件数换一部作品。
            bound_id = _DOUBAN_SEASON_IDS.get(str(tmdb_id_num), {}).get(str(season_num))
            if recognized and not total_eps and not bound_id:
                calib_total, calib_finished, calib_tmdb_id, calib_src = await auto_recalibrate_season(
                    show_name=show_name,
                    season_num=season_num,
                    recognized=recognized,
                    current_total=total_eps,
                    current_tmdb_id=tmdb_id_num,
                    tmdb_client=active_tmdb,
                    mp_client=mp_client,
                    log=log,
                )
                if calib_total and calib_total >= max(recognized):
                    if log:
                        log.info(
                            "剧集 %s S%02d 触发超范围自愈校准：原集数 %s -> 校准为 %d 集 (来源: %s)",
                            show_name, season_num, total_eps, calib_total, calib_src
                        )
                    total_eps = calib_total
                    edition_note = f"（集数来源：{calib_src}）"
                    season_finished = calib_finished
                    if calib_tmdb_id:
                        tmdb_id_num = calib_tmdb_id
                    tmdb_seasons_data[season_num] = {"total": total_eps, "finished": season_finished}

            status, detail = diagnose_season(
                recognized=recognized,
                total_episodes=total_eps,
                season_finished=season_finished,
                has_duplicates=bool(duplicates),
                duplicate_episodes=duplicates,
            )
            if outlier_nums:
                detail = f"{detail}（已自动隔离断崖集: {outlier_nums}）"

            archive_target = ""
            if archive_root:
                archive_target = compute_archive_target(show_path, season_path, archive_root, global_source_root)

            missing: list[int] = []
            if total_eps and total_eps > 0:
                missing = [ep for ep in range(1, total_eps + 1) if ep not in recognized]

            # 官方总数、源端实物与归档实物分别取证。只读核对不会产生源端删除资格。
            if archive_target:
                try:
                    actual, alias = await _find_archive_season(
                        cd2_client, archive_show_cache, archive_target, season_num,
                        tmdb_match if season_path != show_path else None,
                    )
                    if actual:
                        archive_target = actual
                        archive_items = await cd2_client.list_files(actual)
                        archived_eps = set()
                        for item in archive_items:
                            if item["isDirectory"] or not is_video_file(item["name"]):
                                continue
                            ep = parse_episode(item["name"], item["path"], item.get("size", 0))
                            if _verified_video(ep, season_num):
                                archived_eps.update(ep.episode_numbers)

                        source_eps = set(recognized)
                        expected = set(range(1, (total_eps or 0) + 1))
                        combined = source_eps | archived_eps
                        missing = sorted(expected - combined)
                        uncertain_source = (len(regular_episodes) != len(parsed_episodes)
                                            or any(i["isDirectory"] for i in season_files))
                        if not source_eps or alias or uncertain_source or actual == season_path:
                            status = "archive_check"
                            official = f"本季资料共 {total_eps} 集" if expected else "资料总集数未知"
                            coverage = (f"归档库已核实 {len(expected & archived_eps)}/{total_eps} 集"
                                        if expected else f"归档库已识别 {len(archived_eps)} 集")
                            if missing:
                                coverage += f"，两库尚未核实的集号 {missing}"
                            if actual == season_path:
                                coverage += "（源端和归档指向同一目录）"
                            if alias:
                                coverage += "（按相同 TMDB ID 匹配到别名目录）"
                            detail = f"{_source_inventory(season_files, parsed_episodes, season_num)}。{official}，{coverage}。"
                        elif status != "airing":
                            if total_eps and any(n > total_eps for n in source_eps):
                                status = "review_needed"
                                detail += "。归档前请核对超出资料范围的视频"
                            elif source_eps <= archived_eps:
                                status = "already_archived"
                                detail = f"归档库已包含源端正片集号 {sorted(source_eps)}，可复核源端重复版本"
                            elif expected and not missing and not duplicates:
                                status = "archive_ready"
                                detail = f"两库合计已齐，源端含增量集 {sorted(source_eps - archived_eps)}"
                            elif duplicates and not missing:
                                status = "review_needed"
                                detail = f"源端存在重复集 {duplicates}，需复核"
                            elif expected:
                                status = "download_needed"
                                detail = f"两库合计仍缺 {missing}"
                            recognized = sorted(combined)
                    elif not recognized:
                        status = "archive_check"
                        detail = (f"资料总集数 {total_eps or '未知'}；未找到对应归档季。"
                                  + _source_inventory(season_files, parsed_episodes, season_num) + "。")
                    elif alias and status == "archive_ready":
                        status = "archive_check"
                        detail = "归档分类中存在同 TMDB ID 的别名目录，但未找到对应季，需核对归档位置。"
                except Exception as arc_err:
                    status = "archive_check"
                    detail = f"归档季核对未完成：{arc_err}。资料总集数 {total_eps or '未知'}，源端已识别 {len(recognized)} 集。"
                    log.warning("归档季核对未完成 %s: %s", archive_target, arc_err)

            if edition_note:
                detail += edition_note
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
    if local_tmdb_client:
        try:
            await local_tmdb_client.close()
        except Exception:
            pass
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
                            season_air = s.get("air_date") or ""
                            if season_air:
                                try:
                                    s_date = _dt.strptime(season_air, "%Y-%m-%d")
                                    if (today - s_date).days > 180:
                                        finished = True
                                except Exception:
                                    pass
                    except Exception:
                        pass

                seasons_data[sn] = {
                    "total": real_total if real_total > 0 else None,
                    "season_year": str(s.get("air_date") or "")[:4],
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

_DOUBAN_SUFFIX_VARIANTS = {
    "殿": ["传", "篇", "录", "志"],
    "传": ["殿", "篇", "记", "录"],
    "篇": ["季", "传", "章"],
    "记": ["传", "录", "志"],
    "录": ["志", "传", "记"],
}

_CN_SEASON_NUMS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10
}


def _parse_season_from_text(text: str) -> int | None:
    """从剧集标题、别名（又名）或描述文本中推断季号（支持中文数字与阿拉伯数字）。"""
    if not text:
        return None

    # 1. 阿拉伯数字匹配: 第3季, 第03季, Season 3, S3
    m1 = re.search(r"第\s*(\d{1,2})\s*[季部辑]", text)
    if m1:
        return int(m1.group(1))

    m2 = re.search(r"[Ss]eason\s*(\d{1,2})", text, re.IGNORECASE)
    if m2:
        return int(m2.group(1))

    m3 = re.search(r"[Ss](\d{1,2})(?:\s|$|[-_])", text)
    if m3:
        return int(m3.group(1))

    # 2. 中文数字匹配: 第一季, 第二季, 第三季, 第十季, 第十二季, 第二十一季
    m_cn = re.search(r"第\s*([一二两三四五六七八九十]+)\s*[季部辑]", text)
    if m_cn:
        cn_str = m_cn.group(1)
        if len(cn_str) == 1 and cn_str in _CN_SEASON_NUMS:
            return _CN_SEASON_NUMS[cn_str]
        elif cn_str.startswith("十"):
            if len(cn_str) == 1:
                return 10
            elif len(cn_str) == 2 and cn_str[1] in _CN_SEASON_NUMS:
                return 10 + _CN_SEASON_NUMS[cn_str[1]]
        elif len(cn_str) == 2 and cn_str[1] == "十" and cn_str[0] in _CN_SEASON_NUMS:
            return _CN_SEASON_NUMS[cn_str[0]] * 10
        elif len(cn_str) == 3 and cn_str[1] == "十" and cn_str[0] in _CN_SEASON_NUMS and cn_str[2] in _CN_SEASON_NUMS:
            return _CN_SEASON_NUMS[cn_str[0]] * 10 + _CN_SEASON_NUMS[cn_str[2]]

    return None


def _generate_search_queries(show_name: str, file_hints: list[str] | None = None) -> list[str]:
    """生成 TMDB 与豆瓣多维智能搜索的候选关键词。"""
    clean = re.sub(r"\s*\[.*?\]\s*", " ", show_name)
    clean = re.sub(r"\s*[\(\[（].*?[\)\]）]\s*", " ", clean).strip()
    clean = re.sub(r"\s*\(\d{4}\)\s*", " ", clean).strip()
    clean = re.sub(r"\s+\d{4}$", "", clean).strip()
    if not clean:
        clean = show_name

    # 中英文粘连拆分: Inside百万奖金真人秀 -> Inside 百万奖金真人秀
    spaced = re.sub(r"([a-zA-Z]+)([\u4e00-\u9fa5])", r"\1 \2", clean)
    spaced = re.sub(r"([\u4e00-\u9fa5])([a-zA-Z]+)", r"\1 \2", spaced)

    # 标点符号统一与归一化
    norm = spaced.replace("：", ": ").replace("，", ", ")
    no_punct = re.sub(r"[:：\-_·,，!！?？]+", " ", spaced).strip()
    no_punct = re.sub(r"\s+", " ", no_punct)

    queries: list[str] = []
    for cand in [clean, spaced, norm, no_punct]:
        c_str = cand.strip()
        if c_str and c_str not in queries:
            queries.append(c_str)

    # 变体字替换 (殿 <-> 传, 篇 <-> 季)
    for q_base in list(queries):
        for orig_char, replacements in _DOUBAN_SUFFIX_VARIANTS.items():
            if orig_char in q_base:
                for rep in replacements:
                    var = q_base.replace(orig_char, rep)
                    if var not in queries:
                        queries.append(var)

    # 主次成分拆分
    parts = [p.strip() for p in re.split(r"[:：\-_·\s]+", spaced) if p.strip()]
    if len(parts) >= 2:
        main_part = parts[0]
        sub_part = parts[1]
        combo = f"{main_part} {sub_part}"
        if combo not in queries:
            queries.append(combo)
        for orig_char, replacements in _DOUBAN_SUFFIX_VARIANTS.items():
            if orig_char in sub_part:
                for rep in replacements:
                    var_sub = sub_part.replace(orig_char, rep)
                    if var_sub not in queries:
                        queries.append(var_sub)
                    var_combo = f"{main_part} {var_sub}"
                    if var_combo not in queries:
                        queries.append(var_combo)
        if sub_part not in queries and len(sub_part) >= 2:
            queries.append(sub_part)
        if len(sub_part) > 2 and sub_part[:-1] not in queries:
            queries.append(sub_part[:-1])
        if main_part not in queries and len(main_part) >= 2:
            queries.append(main_part)

    # 提取纯中文片段
    cn_parts = re.findall(r"[\u4e00-\u9fa5]{2,}", spaced)
    for cp in cn_parts:
        if cp not in queries:
            queries.append(cp)

    # 提取纯英文片段
    en_parts = re.findall(r"[a-zA-Z]{3,}(?:\s+[a-zA-Z]{3,})*", spaced)
    for ep in en_parts:
        if ep not in queries:
            queries.append(ep)

    if file_hints:
        for fh in file_hints:
            if fh and fh not in queries:
                queries.append(fh)

    return queries

_generate_douban_queries = _generate_search_queries

# 绑定保存作品与季度的外部标识；集数始终读取详情。
_DOUBAN_SEASON_IDS = json.loads(Path(__file__).with_name("douban_season_ids.json").read_text(encoding="utf-8"))


def _series_title(text: str) -> str:
    text = re.sub(r"第\s*[零一二两三四五六七八九十0-9]+\s*[季部辑]|\bseason\s*\d+|\bS\d+\b", "", text, flags=re.I)
    return re.sub(r"[\W_]+", "", text).casefold()


def _douban_evidence(detail, subject_id, search_title, show_name, wanted, tmdb_id, bound_season=None, season_years=None):
    """仅返回身份、季号都可核实的季表；单季条目有时使用键 1。"""
    if not isinstance(detail, dict) or not detail:
        return {}
    if str(detail.get("type") or "").casefold() in ("电影", "movie"):
        return {}
    if detail.get("douban_id") and str(detail["douban_id"]) != subject_id:
        return {}
    if tmdb_id and detail.get("tmdb_id") and str(detail["tmdb_id"]) != str(tmdb_id):
        return {}
    aliases = [a for a in detail.get("names") or [] if isinstance(a, str)]
    titles = [detail.get("title") or search_title, detail.get("original_title") or "", *aliases]
    clean = _generate_search_queries(show_name)[0]
    if bound_season is None and _series_title(clean) not in {_series_title(t) for t in titles}:
        if not tmdb_id or str(detail.get("tmdb_id")) != str(tmdb_id):
            return {}

    season = _parse_season_from_text(titles[0]) or _parse_season_from_text(search_title)
    field = str(detail.get("season") or "")
    if field.isdigit():
        if season and int(field) not in (1, season):
            return {}
        season = season or int(field)
    if season in (None, 1) and not _parse_season_from_text(titles[0]):
        season = next((sn for a in aliases if (sn := _parse_season_from_text(a))), season)

    entries = detail.get("seasons") or {}
    counts = {}
    for key, episodes in entries.items() if isinstance(entries, dict) else ():
        if not str(key).isdigit() or not isinstance(episodes, list) or not episodes:
            continue
        sn = int(key)
        if len(entries) == 1:
            if sn == 1 and season:
                sn = season
            elif season and sn != season:
                continue
        numbers = [e.get("episode_number") if isinstance(e, dict) else e for e in episodes]
        if all(str(n).isdigit() for n in numbers):
            nums = {int(n) for n in numbers}
            if nums == set(range(1, len(nums) + 1)):
                counts[sn] = len(nums)
    if not entries and str(detail.get("number_of_episodes") or "").isdigit():
        counts[season or 1] = int(detail["number_of_episodes"])
    year = re.search(r"[（(](\d{4})[)）]", show_name)
    if bound_season is None and year and detail.get("year") and str(detail["year"]) != year[1]:
        counts.pop(1, None)
    if bound_season is None and detail.get("year"):
        counts = {sn: n for sn, n in counts.items() if not (season_years or {}).get(sn)
                  or str(detail["year"]) == season_years[sn]}
    return {sn: {"total": n, "douban_id": subject_id, "bound": bound_season is not None,
                 "finished": (detail.get("status") or "") in ("Ended", "已完结", "已播完")}
            for sn, n in counts.items()
            if n > 0 and (not wanted or sn in wanted) and bound_season in (None, sn)}


async def _fetch_seasons_via_douban(mp_client, show_name, log=None, needed_seasons=None,
                                  extra_queries=None, tmdb_id=None, season_years=None):
    wanted = set(needed_seasons or [])
    bindings = _DOUBAN_SEASON_IDS.get(str(tmdb_id), {})
    result = {}
    async def read(subject_id, title, season=None):
        try:
            detail = await mp_client.get_douban_media_info(subject_id, type_name="电视剧")
            return _douban_evidence(detail, subject_id, title, show_name, wanted, tmdb_id, season, season_years)
        except Exception as exc:
            if log:
                log.warning("豆瓣条目 %s 读取失败 (%s)", subject_id, type(exc).__name__)
            return {}

    for sn in wanted:
        did = bindings.get(str(sn))
        if did:
            result.update(await read(did, "", sn))
    unbound = wanted - {int(sn) for sn in bindings}
    if not wanted or unbound:
        base_queries = _generate_search_queries(show_name)
        season_labels = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十", 11: "十一", 12: "十二"}
        seasonal = [f"{base_queries[0]} 第{season_labels.get(sn, sn)}季" for sn in sorted(unbound) if sn > 0]
        queries = list(dict.fromkeys(seasonal + base_queries + list(extra_queries or [])))
        candidates = {}
        async with httpx.AsyncClient(timeout=8.0) as http:
            for q in queries:
                try:
                    resp = await http.get("https://movie.douban.com/j/subject_suggest", params={"q": q},
                                          headers={"User-Agent": "Mozilla/5.0"})
                    if resp.status_code == 200:
                        for it in resp.json():
                            if str(it.get("id") or "").isdigit():
                                candidates[str(it["id"])] = it.get("title") or ""
                except (ValueError, httpx.HTTPError, AttributeError):
                    pass
                try:
                    for it in await mp_client.search_media(q, source="douban"):
                        did = str(it.get("douban_id") or it.get("media_id") or it.get("id") or "")
                        if did.isdigit():
                            candidates[did] = it.get("title") or ""
                except Exception as exc:
                    if log:
                        log.debug("豆瓣搜索失败 (%s)", type(exc).__name__)
        matches = {}
        for did, title in list(candidates.items())[:24]:
            for sn, info in (await read(did, title)).items():
                if str(sn) not in bindings:
                    matches.setdefault(sn, []).append(info)
        for sn, hits in matches.items():
            if len(hits) == 1:
                result[sn] = hits[0]
    return next((i["douban_id"] for i in result.values()), None), result


async def auto_recalibrate_season(
    show_name: str,
    season_num: int,
    recognized: list[int],
    current_total: int | None,
    current_tmdb_id: int | None,
    tmdb_client=None,
    mp_client=None,
    log=None,
) -> tuple[int | None, bool, int | None, str | None]:
    """对存在超范围集数 (Overflow) 或缺失官方集数的季度进行自动自愈校准。

    核心逻辑：
    1. 当本地已识别集数超过官方总集数（如本地 18 集，元数据仅 10 集）时，不武断报错；
    2. 优先通过 TMDB 检索多候选条目，按照“集数拟合度（精确吻合 +80 分）+ 年份契合度 + 标题纯度”进行多维打分竞选；
    3. 若 TMDB 无法满足，自动升轨至豆瓣双轨检索（Suggest API + MoviePilot 媒体详情代理），
       定向检索对应季度条目，精准提取真实总集数；
    4. 成功校准后，自动返回更新后的总集数、完结状态与真实 TMDB ID。

    Returns:
        (calibrated_total, is_finished, calibrated_tmdb_id, source_desc)
    """
    if not recognized:
        return current_total, False, current_tmdb_id, None

    target_count = max(len(recognized), max(recognized))
    if target_count <= 0:
        return current_total, False, current_tmdb_id, None

    clean_title = re.sub(r"\s*\[.*?\]\s*", " ", show_name)
    m_year = re.search(r"\((\d{4})\)", clean_title)
    show_year = int(m_year.group(1)) if m_year else None
    clean_title = re.sub(r"\s*\(\d{4}\)\s*", " ", clean_title).strip()
    clean_title = re.sub(r"\s+\d{4}$", "", clean_title).strip()
    if not clean_title:
        clean_title = show_name

    # ── 1. TMDB 多候选深度集数拟合竞选 ─────────────────────
    if tmdb_client:
        try:
            queries = _generate_search_queries(show_name)
            candidate_pool: list[dict] = []
            seen_cids = set()

            for q in queries:
                try:
                    cands = await tmdb_client.search_tv(q)
                    for cand in cands[:8]:
                        cid = cand.get("id")
                        if not cid or cid in seen_cids:
                            continue
                        seen_cids.add(cid)
                        candidate_pool.append(cand)
                except Exception:
                    pass
                if len(candidate_pool) >= 12:
                    break

            best_cand = None
            best_score = -9999

            for cand in candidate_pool:
                cid = cand.get("id")
                cname = cand.get("name") or ""
                air_date = cand.get("first_air_date") or ""
                c_year = int(air_date[:4]) if len(air_date) >= 4 and air_date[:4].isdigit() else None

                try:
                    detail = await tmdb_client.get_tv_detail(cid)
                    for s in detail.get("seasons", []):
                        if s.get("season_number") == season_num:
                            cnt = s.get("episode_count") or 0
                            if cnt <= 0:
                                continue

                            score = 0
                            # A: 集数拟合度（核心权重）
                            if cnt == target_count:
                                score += 80  # 完美全覆盖且不产生任何超范围
                            elif cnt > target_count:
                                score += 25  # 官方更多（可能连载中或含花絮加更）
                            else:
                                score -= (target_count - cnt) * 12  # 官方仍少于本地，仍会造成溢出，重罚

                            # B: 年份契合度
                            if show_year and c_year:
                                if show_year == c_year:
                                    score += 35
                                elif abs(show_year - c_year) <= 1:
                                    score += 15
                                else:
                                    score -= 20

                            # C: 标题语义纯净度
                            if clean_title == cname.strip():
                                score += 25
                            elif clean_title in cname:
                                score += 15

                            s_finished = False
                            s_air = s.get("air_date") or ""
                            if s_air:
                                try:
                                    from datetime import datetime as _dt
                                    s_d = _dt.strptime(s_air, "%Y-%m-%d")
                                    if (_dt.now() - s_d).days > 180:
                                        s_finished = True
                                except Exception:
                                    pass

                            if score > best_score:
                                best_score = score
                                best_cand = {
                                    "tmdb_id": cid,
                                    "name": cname,
                                    "total": cnt,
                                    "finished": s_finished,
                                    "source": f"TMDB系列 [{cname}] (ID={cid})",
                                }
                except Exception:
                    pass

            if best_cand and best_score >= 50 and best_cand["total"] >= target_count:
                if log:
                    log.info(
                        "剧集 %s S%02d TMDB 自愈校准优胜选出: %s, 官方集数 %d 集 (拟合得分=%d)",
                        show_name, season_num, best_cand["source"], best_cand["total"], best_score
                    )
                return best_cand["total"], best_cand["finished"], best_cand["tmdb_id"], best_cand["source"]
        except Exception as e:
            if log:
                log.warning("TMDB 自愈校准执行异常: %s", e)

    if mp_client:
        _, seasons = await _fetch_seasons_via_douban(
            mp_client, show_name, log, [season_num], tmdb_id=current_tmdb_id)
        info = seasons.get(season_num)
        if info and info["total"] >= target_count:
            return info["total"], info["finished"], current_tmdb_id, f"豆瓣条目 {info['douban_id']}"

    return current_total, False, current_tmdb_id, None


def _merge_season_sources(primary: dict, secondary: dict | None) -> dict:
    info = {**primary, "douban_checked": True}
    if not secondary:
        return info
    tmdb_total = primary.get("total") or 0
    db_total = secondary["total"]
    if not tmdb_total:
        return {**info, **secondary}
    return {**info, "tmdb_total": tmdb_total, "douban_total": db_total,
            "douban_id": secondary["douban_id"], "bound": secondary["bound"],
            "is_split_edition": max(tmdb_total, db_total) >= 1.5 * min(tmdb_total, db_total)}


async def _resolve_show_metadata(
    show_name: str,
    needed_seasons: list[int],
    tmdb_id_num: int | None,
    mp_client,
    tmdb_client=None,
    log=None,
) -> tuple[int | None, dict[int, dict]]:
    """多层闭环媒体元数据解析引擎：
    Pass 1: 本地指定的 TMDB ID 查询 (若有效则保留，若返回 404/空则重置待纠偏)
    Pass 2: TMDB 标题级多维智能打分检索与母系列/跨语种识别（综合年份、标题纯度、季度覆盖优胜劣汰）
    Pass 3: 豆瓣双轨深度检索（公开 Suggest API + MoviePilot 代理，支持别名/变体/衍生剧映射）
    Pass 4: MoviePilot TMDB 兜底检索

    Returns:
        (resolved_tmdb_id, seasons_data)
    """
    seasons_data: dict[int, dict] = {}
    resolved_id: int | None = tmdb_id_num

    def is_complete() -> bool:
        if not needed_seasons:
            return bool(seasons_data)
        for sn in needed_seasons:
            if sn not in seasons_data or not (seasons_data[sn].get("total") or 0) > 0:
                return False
        return True

    # 1. 优先尝试本地指定的 TMDB ID
    if mp_client and tmdb_id_num:
        seasons_data = await _fetch_seasons_by_tmdb_id(mp_client, tmdb_id_num, log)
        if not seasons_data:
            # tmdb_id_num 完全失效 (例如 404 历史死链或已被删除合并)
            if log:
                log.warning("本地目录指定的 tmdb_id=%d 未获取到任何季信息，重置为待纠偏状态", tmdb_id_num)
            resolved_id = None
        elif is_complete():
            has_cn = bool(re.search(r"[\u4e00-\u9fa5]", show_name))
            need_douban_check = has_cn or any((seasons_data.get(sn, {}).get("total") or 0) >= 12 for sn in needed_seasons)
            if not need_douban_check and str(tmdb_id_num) not in _DOUBAN_SEASON_IDS:
                return resolved_id, seasons_data

    # 2. TMDB 标题与母系列检索（补齐缺失季度）
    queries = _generate_search_queries(show_name)
    discovered_cn: list[str] = []

    clean_name = re.sub(r"\s*\[.*?\]\s*", " ", show_name)
    m_year = re.search(r"\((\d{4})\)", clean_name)
    show_year = int(m_year.group(1)) if m_year else None
    clean_name = re.sub(r"\s*\(\d{4}\)\s*", " ", clean_name).strip()
    clean_name = re.sub(r"\s+\d{4}$", "", clean_name).strip()

    if tmdb_client and not is_complete():
        missing_seasons = [sn for sn in needed_seasons if sn not in seasons_data or not (seasons_data[sn].get("total") or 0) > 0]

        candidate_pool: list[dict] = []
        seen_cids = set()

        for q in queries:
            try:
                cands = await tmdb_client.search_tv(q)
                for cand in cands[:8]:
                    cid = cand.get("id")
                    if not cid or cid in seen_cids:
                        continue
                    seen_cids.add(cid)
                    cname = cand.get("name") or ""
                    air_date = cand.get("first_air_date") or ""
                    c_year = int(air_date[:4]) if len(air_date) >= 4 and air_date[:4].isdigit() else None

                    # 候选条目初评打分
                    cand_score = 0
                    if show_year and c_year:
                        if show_year == c_year:
                            cand_score += 40
                        elif abs(show_year - c_year) <= 1:
                            cand_score += 20
                        else:
                            cand_score -= 20

                    if clean_name and cname.strip() == clean_name:
                        cand_score += 30
                    elif clean_name and clean_name in cname:
                        cand_score += 15

                    candidate_pool.append({
                        "id": cid,
                        "name": cname,
                        "score": cand_score,
                        "year": c_year,
                    })
            except Exception as e:
                if log:
                    log.warning("TMDB 搜索 '%s' 异常: %s", q, e)
            if len(candidate_pool) >= 12:
                break

        # 按打分优胜劣汰，优先评估高分候选
        candidate_pool.sort(key=lambda x: x["score"], reverse=True)

        for cand in candidate_pool:
            cid = cand["id"]
            cname = cand["name"]
            if cname and cname not in discovered_cn:
                discovered_cn.append(cname)
            try:
                detail = await tmdb_client.get_tv_detail(cid)
                matched_any = False
                for s in detail.get("seasons", []):
                    sn = s.get("season_number")
                    cnt = s.get("episode_count") or 0
                    if sn in missing_seasons and cnt > 0 and sn not in seasons_data:
                        s_finished = False
                        s_air = s.get("air_date") or ""
                        if s_air:
                            try:
                                from datetime import datetime as _dt
                                s_d = _dt.strptime(s_air, "%Y-%m-%d")
                                if (_dt.now() - s_d).days > 180:
                                    s_finished = True
                            except Exception:
                                pass
                        seasons_data[sn] = {"total": cnt, "finished": s_finished}
                        matched_any = True
                        if log:
                            log.info("剧集 %s 缺失第 S%02d 季，通过 TMDB 检索系列 [%s] (ID=%d, 得分=%d) 成功补齐: %d 集",
                                     show_name, sn, cname, cid, cand["score"], cnt)
                if matched_any and not resolved_id:
                    resolved_id = cid
            except Exception:
                pass
            if is_complete():
                break

    # 3. 豆瓣双轨深度检索与交叉核对 (主动双轨驱动，解决拆分版与整期版口径冲突)
    has_cn = bool(re.search(r"[\u4e00-\u9fa5]", show_name))
    need_douban_check = (mp_client is not None) and (
        str(tmdb_id_num) in _DOUBAN_SEASON_IDS or not is_complete() or has_cn or any((seasons_data.get(sn, {}).get("total") or 0) >= 12 for sn in needed_seasons)
    )

    if need_douban_check and mp_client:
        db_id, db_seasons = await _fetch_seasons_via_douban(
            mp_client=mp_client,
            show_name=show_name,
            log=log,
            needed_seasons=needed_seasons,
            extra_queries=discovered_cn,
            tmdb_id=tmdb_id_num,
            season_years={sn: s.get("season_year") for sn, s in seasons_data.items()},
        )
        for sn in needed_seasons:
            seasons_data[sn] = _merge_season_sources(seasons_data.get(sn, {}), db_seasons.get(sn))
        if log and db_seasons:
            log.info("豆瓣季度核对 %s: %s", show_name, {sn: (v["douban_id"], v["total"]) for sn, v in db_seasons.items()})

    # 4. 若依然无信息且未指定 tmdb_id，通过 MoviePilot 兜底检索
    if not is_complete() and not resolved_id and mp_client:
        mp_tid, mp_tmdb_data = await _fetch_tmdb_via_mp(mp_client, show_name, log)
        if mp_tmdb_data:
            for sn, info in mp_tmdb_data.items():
                if sn not in seasons_data or not (seasons_data[sn].get("total") or 0) > 0:
                    seasons_data[sn] = info
            if mp_tid and not resolved_id:
                resolved_id = mp_tid

    return resolved_id, seasons_data
