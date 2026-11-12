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
import posixpath
import urllib.parse
import httpx
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.clients.clouddrive2 import GrpcError
from app.core.directory_governance import (
    build_physical_dir_entry,
    classify_tmdb_directory_group,
    extract_dir_tmdb_id,
    normalize_dir_path,
    resolve_category_key,
    serialize_directory_group,
    sync_directory_groups_for_task,
)
from app.core.segments import episode_segments, diagnose_periods, segment_number
from app.core.episode_parser import ParsedEpisode, is_video_file, parse_episode
from app.core.identity_resolver import (
    analyze_season_release_signals,
    compute_episode_title_overlap,
    get_verified_metadata_snapshot,
    is_short_or_shared_title,
    normalize_title_token,
    resolve_active_season_mapping,
    save_verified_metadata_snapshot,
    translate_search_to_local_episodes,
)

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

    # 证据、冲突与跨条目映射详情
    evidence: dict[str, Any] = field(default_factory=dict)
    conflict: dict[str, Any] = field(default_factory=dict)
    mapping: dict[str, Any] = field(default_factory=dict)


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
    directory_groups: list[dict[str, Any]] = field(default_factory=list)

    def compute_summary(self) -> None:
        """根据 seasons 计算汇总统计。"""
        self.archive_ready_count = sum(1 for s in self.seasons if s.status in ("archive_ready", "period_ready"))
        self.review_needed_count = sum(1 for s in self.seasons if s.status in ("review_needed", "period_review", "identity_review"))
        self.download_needed_count = sum(1 for s in self.seasons if s.status in ("download_needed", "period_missing"))
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


async def _find_archive_season(
    cd2,
    cache: dict[str, list[dict]],
    expected_path: str,
    season_number: int,
    tagged_show: re.Match | None,
    mapped_targets: list[dict[str, Any]] | None = None,
    extra_targets_out: list[dict[str, Any]] | None = None,
    confirmed_target_dir: str = "",
    multi_archive_dirs_out: list[str] | None = None,
) -> tuple[str | None, bool]:
    """在归档分类内按显式作品 ID 和季号匹配；兼容跨条目映射目标目录双向取证。"""
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
    category = parent.rsplit("/", 1)[0] if "/" in parent else ""

    # 若存在跨条目映射，在同一归档分类下兼容独立条目归档目录（例如 银色恶魔 [tmdbid=323903]/S01）
    if mapped_targets and category:
        cat_dirs = await directories(category or "/")
        seen_mapped: set[str] = set()
        for mt in mapped_targets:
            target_tid = mt.get("tmdb_id")
            target_sn = int(mt.get("season") or 1)
            aliases = [normalize_title_token(a) for a in (mt.get("aliases") or []) if a]
            matched_shows: list[str] = []
            for d in cat_dirs:
                dname = d["name"]
                if target_tid and re.search(rf"\[tmdbid[=:]{int(target_tid)}\]", dname, re.I):
                    matched_shows.append(dname)
                    continue
                if aliases:
                    d_norm = normalize_title_token(dname)
                    if any(a and (a == d_norm or (len(a) >= 3 and a in d_norm)) for a in aliases):
                        matched_shows.append(dname)
            for sdir_name in matched_shows:
                show_dir = f"{category}/{sdir_name}"
                for sub in await directories(show_dir):
                    if _parse_season_number(sub["name"]) == target_sn:
                        mpath = f"{show_dir}/{sub['name']}"
                        if mpath not in seen_mapped and extra_targets_out is not None:
                            seen_mapped.add(mpath)
                            extra_targets_out.append({
                                "path": mpath,
                                "season": target_sn,
                                "tmdb_id": target_tid,
                                "title": sdir_name,
                            })

    alias = False
    if tagged_show and is_season_folder:
        cat_prefix, expected_name = parent.rsplit("/", 1)
        tag = tagged_show.group(1)
        same_work = [e["name"] for e in await directories(cat_prefix or "/")
                     if re.search(rf"\[tmdbid[=:]{tag}\]", e["name"], re.I)]
        if len(same_work) > 1:
            chosen_name = ""
            if confirmed_target_dir:
                cand_base = posixpath.basename(confirmed_target_dir.rstrip("/"))
                for sw in same_work:
                    if sw == cand_base or f"{cat_prefix}/{sw}" == confirmed_target_dir.rstrip("/"):
                        chosen_name = sw
                        break
            if not chosen_name:
                if multi_archive_dirs_out is not None:
                    multi_archive_dirs_out.extend(f"{cat_prefix}/{sw}" for sw in same_work)
                    return None, True
                raise ValueError("同分类有多个相同 TMDB ID 的归档目录：" + "、".join(same_work))
            same_work = [chosen_name]
        if not same_work:
            return None, False
        alias = same_work[0] != expected_name
        parent = f"{cat_prefix}/{same_work[0]}"

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
        return "airing", f"尚未确认本季播毕，当前已识别 {len(recognized_set)}/{total_episodes} 集，留源跟踪"

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


async def _fetch_candidate_season_episodes(
    cid: int,
    season_num: int,
    mp_client,
    tmdb_client,
) -> list[dict[str, Any]]:
    """拉取候选 TMDB 条目指定季分集清单（优先英文以核对原始发行文件名集名）。"""
    if tmdb_client is not None:
        if callable(getattr(type(tmdb_client), "fetch_season_detail", None)):
            sd_en, status_en, _ = await tmdb_client.fetch_season_detail(cid, season_num, language="en-US")
            if status_en == "ok" and isinstance(sd_en, dict) and sd_en.get("episodes"):
                return list(sd_en["episodes"])
            sd_zh, status_zh, _ = await tmdb_client.fetch_season_detail(cid, season_num)
            if status_zh == "ok" and isinstance(sd_zh, dict) and sd_zh.get("episodes"):
                return list(sd_zh["episodes"])
        elif callable(getattr(type(tmdb_client), "get_season_detail", None)):
            try:
                sd = await tmdb_client.get_season_detail(cid, season_num)
                if isinstance(sd, dict) and sd.get("episodes"):
                    return list(sd["episodes"])
            except Exception:
                pass
    if mp_client is not None:
        try:
            eps = await mp_client.get_tmdb_episodes(cid, season_num)
            if isinstance(eps, list):
                return eps
        except Exception:
            pass
    return []


async def _search_conflict_candidates(
    query: str,
    season_num: int,
    exclude_tmdb_id: int | None,
    tmdb_client,
    mp_client,
) -> list[dict[str, Any]]:
    """根据发行标题线索检索候选 TMDB 条目（仅检查确实包含目标季号的候选）。"""
    if not query:
        return []
    raw_pool: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    if tmdb_client is not None:
        try:
            if callable(getattr(type(tmdb_client), "fetch_search_tv", None)):
                res, _, _ = await tmdb_client.fetch_search_tv(query)
            else:
                res = await tmdb_client.search_tv(query)
            for item in (res or [])[:8]:
                cid = item.get("id")
                if cid and int(cid) not in seen_ids:
                    seen_ids.add(int(cid))
                    raw_pool.append(dict(item))
        except Exception:
            pass

    if not raw_pool and mp_client is not None:
        try:
            for item in (await mp_client.search_media(query, source="tmdb") or [])[:8]:
                cid = item.get("tmdb_id") or item.get("tmdbid") or item.get("id")
                if str(cid or "").isdigit() and int(cid) not in seen_ids:
                    seen_ids.add(int(cid))
                    raw_pool.append({
                        "id": int(cid),
                        "name": item.get("title") or item.get("name") or "",
                        "original_name": item.get("original_title") or "",
                        "first_air_date": str(item.get("year") or ""),
                    })
        except Exception:
            pass

    enriched: list[dict[str, Any]] = []
    for cand in raw_pool[:6]:
        cid = int(cand["id"])
        if exclude_tmdb_id and cid == int(exclude_tmdb_id):
            continue
        summary, _, _ = await _fetch_tv_summary(cid, mp_client, tmdb_client)
        season_match = next(
            (s for s in (summary.get("seasons_raw") or []) if s.get("season_number") == season_num),
            None,
        )
        if not season_match and mp_client is not None and not summary.get("seasons_raw"):
            try:
                mp_seasons = await mp_client.get_tmdb_seasons(cid)
                season_match = next(
                    (s for s in (mp_seasons or []) if isinstance(s, dict) and s.get("season_number") == season_num),
                    None,
                )
            except Exception:
                pass
        if not season_match:
            continue
        cnt = int(season_match.get("episode_count") or 0)
        enriched.append({
            "tmdb_id": cid,
            "name": summary.get("name") or cand.get("name") or "",
            "original_name": summary.get("original_name") or cand.get("original_name") or "",
            "status": summary.get("status") or "",
            "first_air_date": summary.get("first_air_date") or cand.get("first_air_date") or "",
            "season_number": season_num,
            "episode_count": cnt,
            "season_year": str(season_match.get("air_date") or "")[:4],
        })
    return enriched


async def _investigate_season_identity(
    show_name: str,
    tmdb_id_num: int | None,
    season_num: int,
    season_files: list[dict[str, Any]],
    parsed_episodes: list[ParsedEpisode],
    primary_work_info: dict[str, Any],
    primary_seasons_data: dict[int, dict],
    meta_evidence: dict[str, Any],
    active_tmdb,
    mp_client,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """分析文件发行证据并裁决是 作品归属冲突(强证据) 还是 同名作品待确认(弱证据/短词)."""
    release_signals = analyze_season_release_signals(
        season_files=season_files,
        parsed_episodes=parsed_episodes,
        show_name=show_name,
        primary_work=primary_work_info,
    )
    if not parsed_episodes and not release_signals.get("video_count"):
        return {}, release_signals

    primary_name = str(primary_work_info.get("name") or show_name)
    present_seasons = primary_work_info.get("seasons_present") or sorted(sn for sn in primary_seasons_data if sn > 0)
    present_label = "、".join(f"S{s:02d}" for s in present_seasons) if present_seasons else "未知"

    # 0. 文件名显式携带与目录主条目冲突的 [tmdbid=...] 或多套显式 TMDB ID 混装：始终高优拦截
    explicit_file_conflicts = release_signals.get("explicit_file_tmdb_conflicts") or []
    distinct_file_ids = release_signals.get("distinct_file_tmdb_ids") or []
    if len(distinct_file_ids) >= 2:
        return {
            "kind": "mixed_works",
            "blocking": True,
            "ambiguous": False,
            "message": f"同季目录内文件显式携带多个不同 TMDB ID（{', '.join(distinct_file_ids)}），请拆清来源或人工确认后处理。",
            "detected_work": {},
            "clues": distinct_file_ids,
            "sample_files": release_signals.get("sample_files") or [],
        }, release_signals
    if explicit_file_conflicts:
        bad = explicit_file_conflicts[0]
        return {
            "kind": "identity_conflict",
            "blocking": True,
            "ambiguous": False,
            "message": (
                f"作品归属冲突：目录主条目 {primary_name} (TMDB {tmdb_id_num or '?'})，"
                f"但文件 {bad['filename']} 显式标注了外部 TMDB {bad['file_tmdb_id']}。"
            ),
            "detected_work": {"tmdb_id": int(bad["file_tmdb_id"])} if str(bad["file_tmdb_id"]).isdigit() else {},
            "clues": [str(bad["file_tmdb_id"])],
            "sample_files": [c["filename"] for c in explicit_file_conflicts],
        }, release_signals

    # 1. 同季目录混放多个发行作品文件（已语义合并同作品多前缀）
    if release_signals.get("mixed_works"):
        titles = " / ".join(release_signals.get("distinct_release_titles") or [])
        return {
            "kind": "mixed_works",
            "blocking": True,
            "ambiguous": False,
            "message": f"同季目录混放多个发行作品文件（{titles}），请拆清来源或人工确认后处理。",
            "detected_work": {},
            "clues": release_signals.get("distinct_release_titles") or [],
            "sample_files": release_signals.get("sample_files") or [],
        }, release_signals

    # 核心判决顺序：当目录带有明确且有效的 TMDB ID、目录内仅为单一作品发行文件且当前季已收录在该作品官方季表中时，
    # 不得仅因外文发行名、双语/字幕组前缀差异或缺少英文译名而误报 identity_conflict / mixed_works。
    has_explicit_dir_tmdb = bool(tmdb_id_num and re.search(r"\[tmdbid[=:]\d+\]", show_name, re.I))
    season_listed_in_primary = bool(tmdb_id_num and present_seasons and season_num in present_seasons)
    if has_explicit_dir_tmdb and season_listed_in_primary:
        return {}, release_signals

    # 2. 文件实际发行片名与目录主作品明确不一致（例如 Gravesend 目录内实为 The.Neighborhood）
    dominant = str(release_signals.get("dominant_release_title") or "")
    if release_signals.get("incompatible_with_primary") and dominant:
        cands = await _search_conflict_candidates(
            query=dominant,
            season_num=season_num,
            exclude_tmdb_id=tmdb_id_num,
            tmdb_client=active_tmdb,
            mp_client=mp_client,
        )
        best_cand = cands[0] if cands else {}
        ep_matches: list[dict[str, Any]] = []
        if best_cand.get("tmdb_id") and release_signals.get("ep_title_hints"):
            ep_list = await _fetch_candidate_season_episodes(
                best_cand["tmdb_id"], season_num, mp_client, active_tmdb
            )
            ep_matches = compute_episode_title_overlap(release_signals["ep_title_hints"], ep_list)

        cand_desc = (
            f"《{best_cand.get('name') or dominant}》(TMDB {best_cand['tmdb_id']}, S{season_num:02d} 共 {best_cand.get('episode_count') or '?'} 集)"
            if best_cand.get("tmdb_id")
            else f"《{dominant}》"
        )
        ep_desc = f"，{len(ep_matches)} 集分集英文名吻合" if ep_matches else ""
        msg = (
            f"作品归属冲突：目录主条目 {primary_name} (TMDB {tmdb_id_num or '?'}) 仅收录 {present_label}，"
            f"本地文件实为发行片名「{dominant}」，高度对应 {cand_desc}{ep_desc}。请确认是否拆回/改绑本季。"
        )
        return {
            "kind": "identity_conflict",
            "blocking": True,
            "ambiguous": False,
            "message": msg,
            "detected_work": best_cand,
            "episode_title_matches": ep_matches[:6],
            "clues": [dominant, *release_signals.get("platform_tags", []), *(str(y) for y in release_signals.get("release_years", []))],
            "sample_files": release_signals.get("incompatible_files") or release_signals.get("sample_files") or [],
        }, release_signals

    # 3. 主条目有效但未收录当前季号，且存在年份/平台/同名短词特征（如 Kingdom 2025 Baha vs 搏击王国 61427）
    season_unlisted = bool(tmdb_id_num and present_seasons and season_num not in present_seasons)
    conflict_cands = (meta_evidence.get("candidate_conflicts") or {}).get(season_num) or []
    release_years = release_signals.get("release_years") or []
    platform_tags = release_signals.get("platform_tags") or []
    primary_status = str(primary_work_info.get("status") or "")

    if season_unlisted and (release_years or platform_tags or conflict_cands):
        if not conflict_cands and dominant:
            conflict_cands = await _search_conflict_candidates(
                query=dominant,
                season_num=season_num,
                exclude_tmdb_id=tmdb_id_num,
                tmdb_client=active_tmdb,
                mp_client=mp_client,
            )
        if not conflict_cands and primary_work_info.get("original_name"):
            conflict_cands = await _search_conflict_candidates(
                query=str(primary_work_info["original_name"]),
                season_num=season_num,
                exclude_tmdb_id=tmdb_id_num,
                tmdb_client=active_tmdb,
                mp_client=mp_client,
            )

        matched_homonym = None
        for cand in conflict_cands:
            c_year = cand.get("season_year") or str(cand.get("first_air_date") or "")[:4]
            if release_years and c_year and c_year.isdigit() and int(c_year) not in release_years:
                continue
            matched_homonym = cand
            break
        if not matched_homonym and conflict_cands:
            matched_homonym = conflict_cands[0]

        if matched_homonym:
            shared = is_short_or_shared_title(
                dominant or matched_homonym.get("original_name") or matched_homonym.get("name") or "",
                primary_work_info,
                show_name,
            )
            clue_items = [
                *(str(y) for y in release_years),
                *platform_tags,
            ]
            if dominant and dominant not in clue_items:
                clue_items.insert(0, dominant)
            clue_str = "、".join(clue_items) if clue_items else f"S{season_num:02d}"
            cand_label = f"《{matched_homonym.get('name') or '?'}》(TMDB {matched_homonym['tmdb_id']}, S{season_num:02d})"
            if shared:
                msg = (
                    f"同名作品待确认：主条目 {primary_name} (TMDB {tmdb_id_num}, 状态:{primary_status or '已完结'}) 仅收录 {present_label}，"
                    f"但 S{season_num:02d} 文件含线索（{clue_str}），疑似另一同名作品 {cand_label}；同名短词严禁自动改绑，请人工确认是否仅改绑本季。"
                )
                return {
                    "kind": "ambiguous_title",
                    "blocking": True,
                    "ambiguous": True,
                    "message": msg,
                    "detected_work": matched_homonym,
                    "clues": clue_items,
                    "sample_files": release_signals.get("sample_files") or [],
                }, release_signals
            else:
                msg = (
                    f"作品归属疑点：主条目 {primary_name} (TMDB {tmdb_id_num}) 仅收录 {present_label}，"
                    f"未收录 S{season_num:02d}，候选 {cand_label}（线索：{clue_str}），请确认后再行补缺或归档。"
                )
                return {
                    "kind": "identity_conflict",
                    "blocking": True,
                    "ambiguous": False,
                    "message": msg,
                    "detected_work": matched_homonym,
                    "clues": clue_items,
                    "sample_files": release_signals.get("sample_files") or [],
                }, release_signals

    return {}, release_signals


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
    only_season_path: str | None = None,
    db=None,
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
    entry = PurePosixPath(source_path)
    entry_label = re.sub(r"\s*[（(]\d{4}[)）]$", "", entry.name)
    single_season = any(pattern.fullmatch(entry_label) for pattern in _SEASON_PATTERNS)
    if single_season and entry.parent.name:
        show_list.append({"name": entry.parent.name, "path": str(entry.parent)})
    else:
        await _discover_shows(cd2_client, source_path, show_list, max_depth=4, log=log, progress_callback=progress_callback)

    total_shows = len(show_list)
    log.info("源目录 %s 下递归发现 %d 个剧集目录", source_path, total_shows)
    if progress_callback:
        await progress_callback(0, total_shows, "", f"发现 {total_shows} 个剧集目录，开始逐一分析...")

    # ── 2. 多子任务并发加速分析 ─────────────────────────
    completed_shows = 0
    lock = asyncio.Lock()
    db_lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, concurrency))
    physical_dir_entries_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    tmdb_meta_by_id: dict[str, dict[str, Any]] = {}
    confirmed_targets_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    if db is not None:
        try:
            from sqlalchemy import select as _sa_select
            from app.models.directory_group import DirectoryGroup as _DirGroup

            async with db_lock:
                dg_rows = list(
                    (
                        await db.scalars(
                            _sa_select(_DirGroup).where(_DirGroup.task_id == task_id)
                        )
                    ).all()
                )
            for dg in dg_rows:
                confirmed_targets_by_key[(str(dg.category_key), str(dg.tmdb_id))] = {
                    "id": dg.id,
                    "status": dg.status,
                    "confirmation_mode": dg.confirmation_mode,
                    "confirmed_target_path": dg.confirmed_target_path or "",
                    "paths_signature": dg.paths_signature or "",
                }
        except Exception:
            confirmed_targets_by_key = {}

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
                if sn is not None and (not single_season or item["path"] == str(entry)):
                    season_dirs.append((sn, item))
            else:
                if is_video_file(name):
                    loose_files.append(item)

        if not single_season and not season_dirs and loose_files:
            season_dirs = [(1, {"name": show_name, "path": show_path, "isDirectory": True})]

        if only_season_path is not None:
            season_dirs = [(sn, item) for sn, item in season_dirs if item["path"] == only_season_path]
        if not season_dirs:
            return []

        tmdb_id_num: int | None = None
        tmdb_seasons_data: dict[int, dict] = {}
        meta_evidence: dict[str, Any] = {}

        tmdb_match = re.search(r"\[tmdbid[=:](\d+)\]", show_name, re.IGNORECASE)
        if tmdb_match:
            tmdb_id_num = int(tmdb_match.group(1))

        # 预加载已生效的单季映射，确保只作用于目标单季而不污染全剧
        season_mappings: dict[int, dict[str, Any]] = {}
        for sn, sitem in season_dirs:
            spath = sitem["path"]
            async with db_lock:
                mp_map = await resolve_active_season_mapping(db, tmdb_id_num, sn, show_path, spath)
            if mp_map:
                season_mappings[sn] = mp_map

        needed_seasons = [sn for sn, _ in season_dirs]
        try:
            resolved_tmdb_id, tmdb_seasons_data = await _resolve_show_metadata(
                show_name=show_name,
                needed_seasons=needed_seasons,
                tmdb_id_num=tmdb_id_num,
                mp_client=mp_client,
                tmdb_client=active_tmdb,
                log=log,
                evidence_out=meta_evidence,
            )
        except TypeError:
            # 兼容测试环境中对 _resolve_show_metadata 的 5/6 参 Patch
            resolved_tmdb_id, tmdb_seasons_data = await _resolve_show_metadata(
                show_name=show_name,
                needed_seasons=needed_seasons,
                tmdb_id_num=tmdb_id_num,
                mp_client=mp_client,
                tmdb_client=active_tmdb,
                log=log,
            )

        primary_work_info: dict[str, Any] = dict(meta_evidence.get("work_info") or {})
        primary_work_valid = bool(primary_work_info.get("seasons_present") or tmdb_seasons_data)

        if db is not None and primary_work_info.get("tmdb_id") and primary_work_info.get("seasons_present"):
            async with db_lock:
                await save_verified_metadata_snapshot(
                    db,
                    f"tmdb_tv:{primary_work_info['tmdb_id']}",
                    "tmdb_tv",
                    str(primary_work_info["tmdb_id"]),
                    None,
                    {"seasons": primary_work_info.get("seasons_present"), **primary_work_info},
                )
                if primary_work_info.get("aliases"):
                    await save_verified_metadata_snapshot(
                        db,
                        f"tmdb_aliases:{primary_work_info['tmdb_id']}",
                        "tmdb_aliases",
                        str(primary_work_info["tmdb_id"]),
                        None,
                        {
                            "aliases": primary_work_info.get("aliases") or [],
                            "en_name": primary_work_info.get("en_name") or "",
                        },
                    )
        if db is not None and (tmdb_id_num or primary_work_info.get("tmdb_id")) and not primary_work_info.get("aliases"):
            alias_tid = primary_work_info.get("tmdb_id") or tmdb_id_num
            async with db_lock:
                cached_aliases = await get_verified_metadata_snapshot(db, f"tmdb_aliases:{alias_tid}")
            if isinstance(cached_aliases, dict):
                if cached_aliases.get("aliases"):
                    primary_work_info["aliases"] = list(cached_aliases["aliases"])
                if cached_aliases.get("en_name") and not primary_work_info.get("en_name"):
                    primary_work_info["en_name"] = str(cached_aliases["en_name"])

        if resolved_tmdb_id:
            if tmdb_match and str(resolved_tmdb_id) != tmdb_match.group(1):
                for info in tmdb_seasons_data.values():
                    info.setdefault("candidate_id", resolved_tmdb_id)
            else:
                tmdb_id_num = resolved_tmdb_id

        show_cat_key = resolve_category_key(show_path, global_source_root, archive_root)
        show_tid_str = str(tmdb_id_num or (tmdb_match.group(1) if tmdb_match else "") or "").strip()
        if show_tid_str.isdigit():
            confirmed_group_info = confirmed_targets_by_key.get((show_cat_key, show_tid_str), {})
            confirmed_target_dir = (
                confirmed_group_info.get("confirmed_target_path") or ""
                if confirmed_group_info.get("status") == "confirmed"
                and confirmed_group_info.get("confirmation_mode") == "use_archive_target"
                else ""
            )
            # 单季局部重检时补充扫描源端同分类父目录，避免漏检同侧重复目录
            if only_season_path is not None:
                src_parent_dir = posixpath.dirname(normalize_dir_path(show_path))
                if src_parent_dir and src_parent_dir != "/":
                    try:
                        sibs = await cd2_client.list_files(src_parent_dir)
                        sib_entries = []
                        for sib in sibs:
                            if sib.get("isDirectory") and extract_dir_tmdb_id(sib.get("name")) == show_tid_str:
                                sp = normalize_dir_path(sib.get("path") or f"{src_parent_dir}/{sib.get('name')}")
                                sib_entries.append(
                                    build_physical_dir_entry(
                                        side="source",
                                        category=show_cat_key,
                                        show_path=sp,
                                        tmdb_id=show_tid_str,
                                        stable_id=str(sib.get("id") or sib.get("fileId") or ""),
                                    )
                                )
                        if sib_entries:
                            async with lock:
                                physical_dir_entries_by_key.setdefault((show_cat_key, show_tid_str), []).extend(sib_entries)
                    except Exception:
                        pass
        else:
            confirmed_target_dir = ""

        show_src_ep_map: dict[str, list[int]] = {}
        show_src_seasons: list[int] = []
        show_ms_cache: dict[int, dict[str, Any]] = {}

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
            active_mapping = season_mappings.get(season_num)
            mapped_work_info: dict[str, Any] = {}
            mapped_fetch_failed = False

            if active_mapping:
                target_tid = active_mapping.get("target_tmdb_id")
                target_sn = int(active_mapping.get("target_season") or 1)
                target_did = active_mapping.get("target_douban_id") or ""
                mapped_probe: dict[str, Any] = {}
                mapped_seasons: dict[int, dict] = {}
                if target_tid and (mp_client or active_tmdb):
                    try:
                        mapped_seasons = await _fetch_seasons_by_tmdb_id(
                            mp_client, int(target_tid), log, tmdb_client=active_tmdb, meta_out=mapped_probe
                        )
                    except TypeError:
                        mapped_seasons = await _fetch_seasons_by_tmdb_id(mp_client, int(target_tid), log)
                    mapped_work_info = dict(mapped_probe.get("work_info") or {})
                    if mapped_probe.get("fetch_failed") and not mapped_seasons:
                        mapped_fetch_failed = True
                info = dict(mapped_seasons.get(target_sn, {}))
                if not info.get("total") and db is not None and target_tid:
                    async with db_lock:
                        cached_mapped = await get_verified_metadata_snapshot(
                            db, f"tmdb:{int(target_tid)}:S{target_sn}"
                        )
                    if cached_mapped:
                        info = dict(cached_mapped)
                        info["stale_cache_reused"] = True
                if not info.get("total") and target_did and mp_client:
                    try:
                        db_detail = await mp_client.get_douban_media_info(target_did, type_name="电视剧")
                        db_parsed = _douban_evidence(
                            db_detail, target_did, "", active_mapping.get("target_title") or show_name,
                            {season_num, target_sn}, target_tid, season_num, None,
                        )
                        db_hit = db_parsed.get(season_num) or db_parsed.get(target_sn)
                        if db_hit:
                            info = {**info, **db_hit}
                    except Exception:
                        pass
                info["mapped"] = True
                info["mapping_id"] = active_mapping.get("mapping_id")
                if info.get("total") and db is not None and target_tid and not info.get("stale_cache_reused"):
                    async with db_lock:
                        await save_verified_metadata_snapshot(
                            db, f"tmdb:{int(target_tid)}:S{target_sn}",
                            "tmdb_season", str(target_tid), target_sn, info,
                        )
            else:
                info = dict(tmdb_seasons_data.get(season_num, {}))
                if not info.get("total") and db is not None and tmdb_id_num and meta_evidence.get("fetch_failed"):
                    async with db_lock:
                        cached_primary = await get_verified_metadata_snapshot(
                            db, f"tmdb:{int(tmdb_id_num)}:S{season_num}"
                        )
                    if cached_primary:
                        info = dict(cached_primary)
                        info["stale_cache_reused"] = True
                elif info.get("total") and db is not None and tmdb_id_num:
                    async with db_lock:
                        await save_verified_metadata_snapshot(
                            db, f"tmdb:{int(tmdb_id_num)}:S{season_num}",
                            "tmdb_season", str(tmdb_id_num), season_num, info,
                        )

            if (not active_mapping and mp_client and info.get("total") and max(all_ep_nums, default=0) > info["total"]
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
            ref_max = max(db_tot or 0, total_eps or 0) or None
            regular_nums, outlier_nums = separate_outliers(all_ep_nums, expected_max=ref_max)
            recognized = sorted(set(regular_nums)) if regular_nums else sorted(set(all_ep_nums))
            if not regular_nums:
                outlier_nums = []

            if db_tot and tmdb_tot and db_tot != tmdb_tot:
                smaller, larger = sorted((db_tot, tmdb_tot))
                if is_split:
                    if (info.get("bound") or info.get("partial_listing")) and db_tot == larger:
                        total_eps = db_tot
                    elif max(recognized, default=0) > smaller:
                        total_eps = larger
                    elif recognized == list(range(1, smaller + 1)):
                        total_eps = smaller
                elif db_tot > tmdb_tot:
                    total_eps = db_tot
                if info.get("partial_listing") and not info.get("douban_finished"):
                    season_finished = False
                edition_note = (f"（TMDB 收录 {tmdb_tot} 集，豆瓣条目 {info['douban_id']} "
                                f"收录 {db_tot} 集，本次按 {total_eps} 集核对）")

            set_recognized = set(recognized)
            ep_counter = Counter(n for n in all_ep_nums if n in set_recognized)
            duplicates = sorted([n for n, cnt in ep_counter.items() if cnt > 1])

            # 身份冲突与同名歧义核查（先于自动校准，避免同名/错位污染主条目）
            identity_conflict: dict[str, Any] = {}
            release_signals: dict[str, Any] = {}
            if not active_mapping:
                identity_conflict, release_signals = await _investigate_season_identity(
                    show_name=show_name,
                    tmdb_id_num=tmdb_id_num,
                    season_num=season_num,
                    season_files=season_files,
                    parsed_episodes=parsed_episodes,
                    primary_work_info=primary_work_info,
                    primary_seasons_data=tmdb_seasons_data,
                    meta_evidence=meta_evidence,
                    active_tmdb=active_tmdb,
                    mp_client=mp_client,
                )
            else:
                release_signals = analyze_season_release_signals(
                    season_files, parsed_episodes, show_name, mapped_work_info or primary_work_info
                )
                if release_signals.get("mixed_works"):
                    identity_conflict = {
                        "kind": "mixed_works",
                        "blocking": True,
                        "ambiguous": False,
                        "message": f"同季目录混放多个发行作品文件（{' / '.join(release_signals.get('distinct_release_titles') or [])}），请拆分后处理。",
                        "sample_files": release_signals.get("sample_files") or [],
                    }

            # 超范围集数/缺失官方集数智能自愈校准保护 (仅当目录未绑定有效季且无作品冲突/接口失败时触发)
            bound_id = _DOUBAN_SEASON_IDS.get(str(tmdb_id_num), {}).get(str(season_num))
            season_unlisted_on_valid_primary = bool(
                tmdb_match and primary_work_valid and season_num not in tmdb_seasons_data
            )
            if (
                recognized
                and not total_eps
                and not bound_id
                and not active_mapping
                and not identity_conflict
                and not season_unlisted_on_valid_primary
                and not meta_evidence.get("fetch_failed")
                and not info.get("episodes_tba")
            ):
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
                    if tmdb_match and calib_tmdb_id and str(calib_tmdb_id) != tmdb_match.group(1):
                        info["candidate_id"] = calib_tmdb_id
                    elif calib_tmdb_id:
                        if not tmdb_match and calib_tmdb_id != tmdb_id_num:
                            info["candidate_id"] = calib_tmdb_id
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
            archive_verified = False
            archive_items = []
            actual = None
            alias = False
            extra_archive_targets: list[dict[str, Any]] = []
            multi_archive_dirs: list[str] = []
            archived_eps_this_season: list[int] = []
            if archive_root:
                if confirmed_target_dir:
                    if normalize_dir_path(season_path) == normalize_dir_path(show_path):
                        archive_target = confirmed_target_dir
                    else:
                        archive_target = f"{confirmed_target_dir}/{posixpath.basename(normalize_dir_path(season_path))}"
                else:
                    archive_target = compute_archive_target(show_path, season_path, archive_root, global_source_root)

            missing: list[int] = []
            if total_eps and total_eps > 0:
                missing = [ep for ep in range(1, total_eps + 1) if ep not in recognized]

            dual_archive_info: dict[str, Any] = {}
            mediaserver_check: dict[str, Any] = {}
            if archive_target and not identity_conflict:
                mapped_targets_arg = None
                if active_mapping:
                    mapped_targets_arg = [{
                        "tmdb_id": active_mapping.get("target_tmdb_id"),
                        "season": int(active_mapping.get("target_season") or 1),
                        "aliases": [
                            active_mapping.get("target_title") or "",
                            active_mapping.get("target_original_title") or "",
                            *(active_mapping.get("archive_aliases") or []),
                        ],
                    }]
                try:
                    actual, alias = await _find_archive_season(
                        cd2_client, archive_show_cache, archive_target, season_num,
                        tmdb_match if season_path != show_path else None,
                        mapped_targets=mapped_targets_arg,
                        extra_targets_out=extra_archive_targets,
                        confirmed_target_dir=confirmed_target_dir,
                        multi_archive_dirs_out=multi_archive_dirs,
                    )
                    if multi_archive_dirs:
                        tid_label = show_tid_str or (tmdb_match.group(1) if tmdb_match else "?")
                        status = "archive_check"
                        detail = (
                            f"归档库同一 TMDB ID ({tid_label}) 存在多个（{len(multi_archive_dirs)} 个）目录，"
                            "已归并到组级复核卡，确认目标目录前暂停写入与删除。"
                        )
                        if show_tid_str.isdigit():
                            arc_entries = [
                                build_physical_dir_entry(
                                    side="archive",
                                    category=show_cat_key,
                                    show_path=mp_dir,
                                    tmdb_id=show_tid_str,
                                )
                                for mp_dir in multi_archive_dirs
                            ]
                            async with lock:
                                physical_dir_entries_by_key.setdefault((show_cat_key, show_tid_str), []).extend(arc_entries)
                    archived_eps: set[int] = set()
                    if actual:
                        archive_target = actual
                        archive_items = await cd2_client.list_files(actual)
                        for item in archive_items:
                            if item["isDirectory"] or not is_video_file(item["name"]):
                                continue
                            ep = parse_episode(item["name"], item["path"], item.get("size", 0))
                            if _verified_video(ep, season_num):
                                archived_eps.update(ep.episode_numbers)
                        archived_eps_this_season = sorted(archived_eps)
                        if show_tid_str.isdigit():
                            actual_show_dir = (
                                normalize_dir_path(actual)
                                if normalize_dir_path(season_path) == normalize_dir_path(show_path)
                                else posixpath.dirname(normalize_dir_path(actual))
                            )
                            async with lock:
                                physical_dir_entries_by_key.setdefault((show_cat_key, show_tid_str), []).append(
                                    build_physical_dir_entry(
                                        side="archive",
                                        category=show_cat_key,
                                        show_path=actual_show_dir,
                                        tmdb_id=show_tid_str,
                                        seasons=[season_num],
                                        episodes_by_season={str(season_num): archived_eps_this_season},
                                    )
                                )

                    mapped_archive_eps: set[int] = set()
                    mapped_archive_items: list[dict] = []
                    for mt in extra_archive_targets:
                        mpath = mt["path"]
                        if actual and mpath == actual:
                            continue
                        mitems = await cd2_client.list_files(mpath)
                        mapped_archive_items.extend(mitems)
                        for item in mitems:
                            if item["isDirectory"] or not is_video_file(item["name"]):
                                continue
                            ep = parse_episode(item["name"], item["path"], item.get("size", 0))
                            if _verified_video(ep, int(mt["season"])):
                                local_eps = translate_search_to_local_episodes(ep.episode_numbers, active_mapping)
                                mapped_archive_eps.update(local_eps)

                    if mapped_archive_eps:
                        archived_eps.update(mapped_archive_eps)

                    if actual or extra_archive_targets:
                        source_eps = set(recognized)
                        expected = set(range(1, (total_eps or 0) + 1))
                        combined = source_eps | archived_eps
                        archive_verified = bool(expected) and expected <= archived_eps
                        missing = sorted(expected - combined)
                        uncertain_source = (len(regular_episodes) != len(parsed_episodes)
                                            or any(i["isDirectory"] for i in season_files))
                        dual_archive_info = {
                            "primary_path": actual or "",
                            "mapped_paths": [m["path"] for m in extra_archive_targets],
                            "archived_episodes": sorted(archived_eps),
                            "combined_episodes": sorted(combined),
                        }
                        if actual:
                            alias_covered = (alias and archive_verified and source_eps and source_eps <= archived_eps
                                             and not uncertain_source and actual != season_path
                                             and not any(segment_number(i["name"]) for i in season_files + archive_items))
                            if alias_covered:
                                status = "already_archived"
                                detail = (f"按同一 TMDB ID 和季号核实别名归档目录，已齐 {total_eps}/{total_eps} 集。"
                                          f"源端 {len(source_eps)} 集在归档中均有对应正片，可删除本季源端。")
                            elif not source_eps or alias or uncertain_source or actual == season_path:
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
                                elif source_eps <= archived_eps and archive_verified:
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
                        else:
                            # 仅命中跨条目独立归档目录（例如 银色恶魔 [tmdbid=323903]/S01）
                            if not archive_target or archive_target == compute_archive_target(show_path, season_path, archive_root, global_source_root):
                                archive_target = extra_archive_targets[0]["path"]
                            if not uncertain_source and status != "airing":
                                if source_eps and source_eps <= archived_eps and archive_verified:
                                    status = "already_archived"
                                    detail = f"跨条目归档目录已齐 {total_eps}/{total_eps} 集，已覆盖源端正片 {sorted(source_eps)}"
                                elif expected and not missing and not duplicates:
                                    status = "archive_ready"
                                    detail = f"与跨条目归档目录合并已齐 {total_eps}/{total_eps} 集（跨条目已有 {sorted(archived_eps)}，源端含 {sorted(source_eps - archived_eps)}）"
                                elif duplicates and not missing:
                                    status = "review_needed"
                                    detail = f"跨条目归档已齐，但源端存在重复集 {duplicates}"
                                elif expected:
                                    status = "download_needed"
                                    detail = f"结合跨条目归档目录（已有 {sorted(archived_eps)}），两库合计仍缺 {missing}"
                                recognized = sorted(combined)
                            elif expected:
                                status = "download_needed" if (missing and season_finished) else "archive_check"
                                detail = f"跨条目归档已识别 {sorted(archived_eps)}；结合源端仍缺 {missing}" if missing else detail
                    elif not recognized and not multi_archive_dirs:
                        inv_desc = _source_inventory(season_files, parsed_episodes, season_num)
                        has_subdirs = any(i["isDirectory"] for i in season_files)
                        can_check_emby = (
                            bool(tmdb_match)
                            and bool(tmdb_id_num)
                            and not parsed_episodes
                            and not has_subdirs
                            and bool(archive_root)
                            and not extra_archive_targets
                            and not identity_conflict
                            and not season_unlisted_on_valid_primary
                            and bool(total_eps and total_eps > 0)
                            and mp_client is not None
                            and callable(getattr(mp_client, "check_mediaserver_exists_remote", None))
                        )
                        if can_check_emby:
                            tid_key = int(tmdb_id_num)
                            if tid_key not in show_ms_cache:
                                show_ms_cache[tid_key] = await mp_client.check_mediaserver_exists_remote(
                                    tid_key,
                                    title=str(primary_work_info.get("name") or ""),
                                    year=str(primary_work_info.get("first_air_date") or "")[:4],
                                    media_type="电视剧",
                                )
                            ms_res = show_ms_cache[tid_key]
                            emby_eps = list((ms_res.get("season_episodes") or {}).get(season_num, []))
                            mediaserver_check = {
                                "checked": bool(ms_res.get("checked")),
                                "server_type": "emby",
                                "season_episodes": emby_eps,
                                "error": str(ms_res.get("error") or ""),
                            }
                            if ms_res.get("checked"):
                                if emby_eps:
                                    status = "archive_check"
                                    sample = ", ".join(f"E{e:02d}" for e in emby_eps[:6])
                                    suffix = "..." if len(emby_eps) > 6 else ""
                                    detail = (
                                        f"资料总集数 {total_eps}；未找到对应归档季。{inv_desc}。"
                                        f"媒体服务器 (Emby) 已有第 S{season_num:02d} 季共 {len(emby_eps)} 集"
                                        f"（{sample}{suffix}），疑似正片存放在 Emby 关联的其他归档目录，暂不转入补缺。"
                                    )
                                elif season_finished:
                                    status = "download_needed"
                                    missing = list(range(1, total_eps + 1))
                                    detail = (
                                        f"归档库与媒体服务器 (Emby) 均确认第 S{season_num:02d} 季 0 集入库"
                                        f"（{inv_desc}），缺失全部 {total_eps} 集：{missing}，已直接转入「真缺集待补」。"
                                    )
                                else:
                                    status = "archive_check"
                                    detail = (
                                        f"资料总集数 {total_eps}；未找到对应归档季。{inv_desc}。"
                                        f"媒体服务器 (Emby) 当前同样 0 集，且官方尚未确认本季完结，暂不触发整季待补。"
                                    )
                            else:
                                status = "archive_check"
                                err_hint = mediaserver_check["error"] or "未连接"
                                detail = (
                                    f"资料总集数 {total_eps or '未知'}；未找到对应归档季。{inv_desc}。"
                                    f"（媒体服务器 Emby 核查未完成: {err_hint}）"
                                )
                        else:
                            status = "archive_check"
                            detail = f"资料总集数 {total_eps or '未知'}；未找到对应归档季。{inv_desc}。"
                    elif alias and status == "archive_ready" and not multi_archive_dirs:
                        status = "archive_check"
                        detail = "归档分类中存在同 TMDB ID 的别名目录，但未找到对应季，需核对归档位置。"
                except Exception as arc_err:
                    status = "archive_check"
                    detail = f"归档季核对未完成：{arc_err}。资料总集数 {total_eps or '未知'}，源端已识别 {len(recognized)} 集。"
                    log.warning("归档季核对未完成 %s: %s", archive_target, arc_err)

            parts = info.get("parts") or {}
            segmented = any(any(v) for v in parts.values())
            if segmented and status != "archive_check" and not identity_conflict:
                period_total = len(parts)
                if db_tot and db_tot > period_total and (info.get("bound") or db_tot > (tmdb_tot or 0)):
                    period_total = db_tot
                    season_finished = info.get("douban_finished", False)
                period = diagnose_periods(season_files + archive_items, season_num, parts,
                                          info.get("ordinals", {}), period_total, season_finished, bool(actual))
                if period:
                    status, detail, missing, recognized = period
                    if status == "period_ready" and alias and not actual:
                        status, detail = "archive_check", "同作品的别名归档目录缺少本季。请核对归档位置后重新初检。"
                    total_eps = period_total
                    if info.get("partial_listing") and not db_tot and status == "airing":
                        total_eps = None
                        detail = f"目前仅取得 {len(parts)} 期的分段资料，全季期数待更新。"
                    duplicates = []
                    edition_note = f"（豆瓣条目 {info['douban_id']} 收录 {db_tot} 集）" if db_tot else ""
                else:
                    status = "archive_check"
                    detail = "本季含分段，目录中还有无法核实的内容。请检查子目录及未编号视频。"
            if status == "review_needed" and total_eps:
                overflow = [e.filename for e in regular_episodes if any(n > total_eps for n in e.episode_numbers)]
                if overflow:
                    detail += " 请核对越界文件：" + "、".join(overflow[:3]) + "。"
            attachments = {".nfo", ".jpg", ".jpeg", ".png", ".webp"}
            if (status == "archive_check" and archive_verified and not segmented and not parsed_episodes
                    and all(not f["isDirectory"] and PurePosixPath(f["name"]).suffix.lower() in attachments for f in season_files)):
                status = "verified_complete"
                detail = "归档正片已齐，源端只留元数据附件，已完成"
            if status == "archive_check":
                if not parsed_episodes and not any(i["isDirectory"] for i in season_files):
                    detail += " 如打算收藏本季，请查找正片所在目录；仅保留附件时可标记已手动处理。"
                elif parsed_episodes and all(ep.is_special or ep.is_extra for ep in parsed_episodes):
                    detail += " 源端只找到特别篇或花絮。想保留这些内容可直接标记已手动处理。"
                elif archive_verified:
                    detail += " 归档已有对应集号，源端仍有未核实内容。请先检查分段、子目录或异常大小的文件。"

            # 明确区分“查询失败”“季未收录”“季存在集数未公布”的元数据状态
            meta_status = "ok"
            if active_mapping:
                meta_status = "mapped"
            present_list_check = (
                primary_work_info.get("seasons_present")
                or sorted(sn for sn in tmdb_seasons_data if sn > 0)
            )
            if total_eps is None and not identity_conflict:
                if (meta_evidence.get("fetch_failed") or mapped_fetch_failed) and not info.get("stale_cache_reused"):
                    meta_status = "fetch_failed"
                    if status != "archive_check":
                        status = "unknown"
                        detail = f"元数据查询失败（{meta_evidence.get('meta_error') or '请求超时或接口异常'}），请稍后重新核算/重试（未覆盖历史核实记录）。"
                    else:
                        detail += f"（元数据查询失败：{meta_evidence.get('meta_error') or '请求超时'}）"
                elif info.get("episodes_tba"):
                    meta_status = "episodes_tba"
                    if status != "archive_check":
                        status = "unknown"
                        detail = f"已找到第 S{season_num:02d} 季条目，但官方总集数尚未公布（待定/TBA），暂不借用其他季集数或盲推缺集。"
                elif primary_work_valid and not active_mapping and present_list_check and season_num not in present_list_check:
                    meta_status = "not_listed"
                    present_text = "、".join(f"S{s:02d}" for s in present_list_check)
                    unlisted_note = (
                        f"主条目 TMDB {tmdb_id_num or '?'} 仅收录 {present_text}，"
                        f"当前季表未收录 S{season_num:02d}（存在跨作品或季号错配疑点），请人工核对片源来源。"
                    )
                    if status != "archive_check":
                        status = "unknown"
                        detail = unlisted_note
                    else:
                        detail += f" {unlisted_note}"
                else:
                    meta_status = "no_candidate"

            if info.get("candidate_id") and status != "archive_check" and not identity_conflict:
                status = "identity_review"
                identity = f"目录标注 TMDB {tmdb_match.group(1)}" if tmdb_match else "目录缺少作品 ID，名称和年份未能唯一匹配"
                detail = (f"{identity}，"
                          f"第 {season_num} 季的 {total_eps} 集来自另一候选作品 {info['candidate_id']}。"
                          f"请核对片头或地区并修正作品 ID，再初检。{detail if '越界文件' in detail else ''}")
                missing = []

            if identity_conflict:
                status = "identity_review"
                detail = identity_conflict["message"]
                missing = []
                meta_status = identity_conflict.get("kind") or "identity_conflict"

            if edition_note:
                detail += edition_note

            evidence_payload = {
                "meta_status": meta_status,
                "meta_error": meta_evidence.get("meta_error") or "",
                "stale_cache_reused": bool(info.get("stale_cache_reused")),
                "matched_work": (
                    {
                        "tmdb_id": active_mapping.get("target_tmdb_id"),
                        "name": mapped_work_info.get("name") or active_mapping.get("target_title") or "",
                        "original_name": mapped_work_info.get("original_name") or active_mapping.get("target_original_title") or "",
                        "status": mapped_work_info.get("status") or active_mapping.get("expected_status") or "",
                        "seasons_present": mapped_work_info.get("seasons_present") or [],
                        "douban_id": active_mapping.get("target_douban_id") or "",
                    }
                    if active_mapping
                    else {
                        "tmdb_id": tmdb_id_num,
                        "name": primary_work_info.get("name") or "",
                        "original_name": primary_work_info.get("original_name") or "",
                        "en_name": primary_work_info.get("en_name") or "",
                        "aliases": list(primary_work_info.get("aliases") or []),
                        "status": primary_work_info.get("status") or "",
                        "seasons_present": primary_work_info.get("seasons_present") or sorted(sn for sn in tmdb_seasons_data if sn > 0),
                    }
                ),
                "candidates": (meta_evidence.get("candidates") or [])[:8],
                "release_evidence": {
                    "dominant_release_title": release_signals.get("dominant_release_title") or "",
                    "distinct_release_titles": release_signals.get("distinct_release_titles") or [],
                    "release_years": release_signals.get("release_years") or [],
                    "platform_tags": release_signals.get("platform_tags") or [],
                    "sample_files": release_signals.get("sample_files") or [],
                },
                "dual_archive_info": dual_archive_info,
                "mediaserver_check": mediaserver_check,
            }

            show_src_seasons.append(season_num)
            show_src_ep_map[str(season_num)] = list(recognized)

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
                evidence=evidence_payload,
                conflict=identity_conflict,
                mapping=active_mapping or {},
            )
            diagnosed_seasons.append(diag)

        # 登记本剧集源端物理目录条目及归档分类下同 TMDB ID 存在的物理别名目录（不含尚未建立的预期目录）
        if show_tid_str.isdigit():
            entries_to_add = [
                build_physical_dir_entry(
                    side="source",
                    category=show_cat_key,
                    show_path=show_path,
                    tmdb_id=show_tid_str,
                    stable_id=str(show_item.get("stable_id") or ""),
                    seasons=show_src_seasons,
                    episodes_by_season=show_src_ep_map,
                )
            ]
            if archive_root:
                exp_arch_show = (
                    compute_archive_target(show_path, f"{show_path}/Season 1", archive_root, global_source_root).rsplit("/", 1)[0]
                    if show_path
                    else ""
                )
                arc_cat_dir = posixpath.dirname(normalize_dir_path(exp_arch_show)) if exp_arch_show else ""
                for arc_d in archive_show_cache.get(arc_cat_dir, []) or []:
                    if arc_d.get("isDirectory") and extract_dir_tmdb_id(arc_d.get("name")) == show_tid_str:
                        arc_show_path = normalize_dir_path(arc_d.get("path") or f"{arc_cat_dir}/{arc_d.get('name')}")
                        entries_to_add.append(
                            build_physical_dir_entry(
                                side="archive",
                                category=show_cat_key,
                                show_path=arc_show_path,
                                tmdb_id=show_tid_str,
                                stable_id=str(arc_d.get("id") or arc_d.get("fileId") or ""),
                            )
                        )
            async with lock:
                physical_dir_entries_by_key.setdefault((show_cat_key, show_tid_str), []).extend(entries_to_add)
                if isinstance(primary_work_info, dict) and primary_work_info.get("name"):
                    tmdb_meta_by_id.setdefault(show_tid_str, dict(primary_work_info))

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

        # 汇总同一分类、同一 TMDB ID 下的多目录 / 异名目录组级状态
        raw_groups: list[dict[str, Any]] = []
        for (_, tid_k), entries in sorted(physical_dir_entries_by_key.items()):
            grp = classify_tmdb_directory_group(
                entries,
                tmdb_meta=tmdb_meta_by_id.get(str(tid_k)),
                source_root=global_source_root,
                archive_root=archive_root,
            )
            if grp:
                raw_groups.append(grp)

        if db is not None:
            partial_keys_arg = (
                {
                    (
                        resolve_category_key(s.show_path, global_source_root, archive_root),
                        str(s.tmdb_id or extract_dir_tmdb_id(s.show_name) or ""),
                    )
                    for s in result.seasons
                    if str(s.tmdb_id or extract_dir_tmdb_id(s.show_name) or "").isdigit()
                }
                if only_season_path is not None
                else None
            )
            synced = await sync_directory_groups_for_task(
                db, task_id, raw_groups, partial_keys=partial_keys_arg
            )
            result.directory_groups = [serialize_directory_group(g) for g in synced]
        else:
            result.directory_groups = [
                {
                    **g,
                    "status": "pending_confirm",
                    "requires_confirm": True,
                    "confirmed_target_path": "",
                    "confirmation_mode": "",
                    "invalidated_reason": "",
                }
                for g in raw_groups
            ]

        group_by_key: dict[tuple[str, str], dict[str, Any]] = {
            (str(g.get("category_key") or ""), str(g.get("tmdb_id") or "")): g
            for g in result.directory_groups
        }
        primary_card_marked: set[tuple[str, str]] = set()
        for diag in result.seasons:
            tid_k = str(diag.tmdb_id or extract_dir_tmdb_id(diag.show_name) or "").strip()
            cat_k = resolve_category_key(diag.show_path, global_source_root, archive_root)
            ginfo = group_by_key.get((cat_k, tid_k))
            if not ginfo:
                continue
            is_primary = (cat_k, tid_k) not in primary_card_marked
            if is_primary:
                primary_card_marked.add((cat_k, tid_k))
            diag.evidence["directory_group"] = {
                "id": ginfo.get("id") or f"{cat_k}:{tid_k}",
                "tmdb_id": tid_k,
                "group_kind": ginfo.get("group_kind") or "",
                "title": ginfo.get("title") or "",
                "status": ginfo.get("status") or "pending_confirm",
                "requires_confirm": bool(ginfo.get("requires_confirm", True)),
                "confirmation_mode": ginfo.get("confirmation_mode") or "",
                "confirmed_target_path": ginfo.get("confirmed_target_path") or "",
                "suggested_target_path": ginfo.get("suggested_target_path") or "",
                "canonical_tmdb_title": ginfo.get("canonical_tmdb_title") or "",
                "canonical_tmdb_original_name": ginfo.get("canonical_tmdb_original_name") or "",
                "canonical_tmdb_year": ginfo.get("canonical_tmdb_year") or "",
                "candidate_targets": ginfo.get("candidate_targets") or [],
                "invalidated_reason": ginfo.get("invalidated_reason") or "",
                "entries": ginfo.get("entries") or [],
                "is_primary_group_card": is_primary,
            }

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
    re.compile(r"^Specials?$", re.IGNORECASE),  # Season 0
]

# 不应被视为剧集目录的目录名
_SKIP_DIRS = {"mount-check", ".recycle", "@eaDir", "#recycle", ".trash"}


async def _discover_shows(
    cd2_client, dir_path: str, results: list[dict], max_depth: int = 4, log=None, progress_callback=None, stable_id: str = ""
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

    labelled_children = any(re.search(r"\[tmdbid[=:]\d+\]|[（(]\d{4}[)）]", sub["name"], re.I)
                            for sub in subdirs)
    if has_season_dir or (has_video and not labelled_children):
        # 这是一个剧集目录
        dir_name = dir_path.rstrip("/").rsplit("/", 1)[-1] if "/" in dir_path else dir_path
        item_rec: dict[str, Any] = {"name": dir_name, "path": dir_path}
        if stable_id:
            item_rec["stable_id"] = stable_id
        results.append(item_rec)
    else:
        # 这是分类目录，继续递归
        for sub in subdirs:
            sub_sid = str(sub.get("id") or sub.get("fileId") or "")
            await _discover_shows(
                cd2_client,
                sub["path"],
                results,
                max_depth - 1,
                log,
                progress_callback,
                stable_id=sub_sid,
            )



def _parse_season_number(dirname: str) -> int | None:
    """从目录名提取季号。返回 None 表示不是 Season 目录。"""
    # 分类目录中常有片名包含 Season、Special 的剧。作品标签和上映年份标记的是剧目录。
    if re.search(r"\[tmdbid[=:]\d+\]", dirname, re.I):
        return None
    label = re.sub(r"\s*[（(]\d{4}[)）]$", "", dirname)
    if label != dirname:
        if not any(pattern.fullmatch(label) for pattern in _SEASON_PATTERNS):
            return None
        dirname = label
    for i, pat in enumerate(_SEASON_PATTERNS):
        m = pat.search(dirname)
        if m:
            if i == 3:  # Specials
                return 0
            return int(m.group(1))
    return None


async def _fetch_tv_summary(
    tmdb_id: int,
    mp_client=None,
    tmdb_client=None,
) -> tuple[dict[str, Any], str, str | None]:
    """读取 TMDB 条目的标题、原名、连载状态与季列表概况，明确区分未收录与网络失败。"""
    detail: dict[str, Any] = {}
    status_kind = "ok"
    err_msg: str | None = None

    if tmdb_client is not None:
        if callable(getattr(type(tmdb_client), "fetch_tv_detail", None)):
            detail, status_kind, err_msg = await tmdb_client.fetch_tv_detail(tmdb_id)
        elif callable(getattr(type(tmdb_client), "get_tv_detail", None)):
            try:
                raw = await tmdb_client.get_tv_detail(tmdb_id)
                if isinstance(raw, dict):
                    detail = raw
                    if not detail:
                        status_kind = "not_found"
            except Exception as exc:
                status_kind = "error"
                err_msg = str(exc)

    if not detail and mp_client is not None and callable(getattr(type(mp_client), "get_media_info", None)):
        try:
            mp_info = await mp_client.get_media_info(tmdb_id)
            if isinstance(mp_info, dict) and mp_info:
                detail = {
                    "id": tmdb_id,
                    "name": mp_info.get("title") or mp_info.get("name") or "",
                    "original_name": mp_info.get("original_title") or mp_info.get("original_name") or "",
                    "status": mp_info.get("status") or "",
                    "first_air_date": mp_info.get("release_date") or mp_info.get("first_air_date") or "",
                    "alternative_titles": mp_info.get("names") or [],
                }
                status_kind = "ok"
        except Exception:
            pass

    if not isinstance(detail, dict):
        detail = {}
    seasons_present = sorted(
        int(s["season_number"])
        for s in (detail.get("seasons") or [])
        if isinstance(s, dict) and s.get("season_number") is not None and int(s["season_number"]) > 0
    )
    aliases: list[str] = []
    for t in (detail.get("alternative_titles") or detail.get("names") or []):
        sval = t if isinstance(t, str) else (str(t.get("title") or "") if isinstance(t, dict) else "")
        if sval.strip() and sval.strip() not in aliases:
            aliases.append(sval.strip())
    en_name = ""
    translations_ok = False
    if tmdb_client is not None and callable(getattr(type(tmdb_client), "fetch_tv_aliases", None)):
        try:
            al_info = await tmdb_client.fetch_tv_aliases(tmdb_id)
            if isinstance(al_info, dict):
                translations_ok = bool(al_info.get("translations_ok"))
                en_name = str(al_info.get("en_name") or "").strip()
                for a in (al_info.get("aliases") or []):
                    sa = str(a or "").strip()
                    if sa and sa not in aliases:
                        aliases.append(sa)
        except Exception:
            translations_ok = False
    summary = {
        "tmdb_id": int(tmdb_id),
        "name": str(detail.get("name") or detail.get("title") or ""),
        "original_name": str(detail.get("original_name") or detail.get("original_title") or ""),
        "en_name": en_name,
        "status": str(detail.get("status") or ""),
        "first_air_date": str(detail.get("first_air_date") or ""),
        "alternative_titles": aliases,
        "aliases": aliases,
        "translations_ok": translations_ok,
        "seasons_present": seasons_present,
        "seasons_raw": [s for s in (detail.get("seasons") or []) if isinstance(s, dict)],
    }
    return summary, status_kind, err_msg


async def _fetch_seasons_by_tmdb_id(
    mp_client,
    tmdb_id: int,
    log,
    tmdb_client=None,
    meta_out: dict[str, Any] | None = None,
) -> dict[int, dict]:
    """已知 tmdb_id，获取季度信息并进行非正片预告剔除与科学完结判定。

    Returns:
        {season_num: {"total": N, "finished": bool, ...}}
    """
    from datetime import datetime as _dt

    seasons_data: dict[int, dict] = {}
    mp_error: str | None = None
    seasons_raw: Any = None
    tv_summary: dict[str, Any] = {}
    tv_status = "ok"
    tv_err: str | None = None

    if mp_client is not None:
        try:
            seasons_raw = await mp_client.get_tmdb_seasons(tmdb_id)
        except Exception as e:
            mp_error = str(e)
            if log:
                log.warning("获取 tmdb_id=%d 的季度信息失败: %s", tmdb_id, e)

    need_tv_detail = (
        tmdb_client is not None
        and (
            callable(getattr(type(tmdb_client), "fetch_tv_detail", None))
            or mp_error is not None
            or not isinstance(seasons_raw, list)
            or not seasons_raw
        )
    )
    if need_tv_detail:
        tv_summary, tv_status, tv_err = await _fetch_tv_summary(tmdb_id, mp_client, tmdb_client)
        if not isinstance(seasons_raw, list) or not seasons_raw:
            if tv_summary.get("seasons_raw"):
                seasons_raw = tv_summary["seasons_raw"]

    if not tv_summary and mp_client is not None and callable(getattr(type(mp_client), "get_media_info", None)):
        tv_summary, _, _ = await _fetch_tv_summary(tmdb_id, mp_client, None)

    if isinstance(seasons_raw, list) and seasons_raw:
        today = _dt.now()
        work_status = str((tv_summary or {}).get("status") or "")
        for s in seasons_raw:
            sn = s.get("season_number")
            if sn is None:
                continue
            sn = int(sn)
            raw_ep_count = int(s.get("episode_count") or 0)
            partial_listing = False
            finished = False
            real_total = raw_ep_count
            parts = {}
            ordinals = {}
            episode_titles: dict[int, str] = {}

            if raw_ep_count > 0:
                try:
                    episodes_info = await mp_client.get_tmdb_episodes(tmdb_id, sn) if mp_client else None
                    if not episodes_info and tmdb_client is not None and callable(getattr(type(tmdb_client), "fetch_season_detail", None)):
                        sd, _, _ = await tmdb_client.fetch_season_detail(tmdb_id, sn)
                        episodes_info = sd.get("episodes") if isinstance(sd, dict) else None
                    if isinstance(episodes_info, list) and episodes_info:
                        regular_eps = []
                        for ep in episodes_info:
                            ep_name = str(ep.get("name") or "")
                            if NON_REGULAR_EP_PATTERN.search(ep_name):
                                if log:
                                    log.info("TMDB tmdb_id=%d S%02d: 自动剔除非正片条目 [%s]", tmdb_id, sn, ep_name)
                                continue
                            regular_eps.append(ep)
                            if isinstance(ep.get("episode_number"), int) and ep_name:
                                episode_titles[int(ep["episode_number"])] = ep_name

                        parts = episode_segments(regular_eps)
                        if parts and all(isinstance(e.get("episode_number"), int) for e in regular_eps):
                            ordinals = {e["episode_number"]: int(re.search(r"第\s*(\d+)\s*期", e["name"])[1])
                                        for e in regular_eps}
                            if len(ordinals) != len(regular_eps):
                                ordinals = {}
                        if regular_eps:
                            partial_listing = len(episodes_info) < raw_ep_count
                            real_total = len(regular_eps) + max(0, raw_ep_count - len(episodes_info))

                        valid_air_dates = []
                        for ep in (regular_eps or episodes_info):
                            ad = ep.get("air_date")
                            if ad:
                                try:
                                    valid_air_dates.append(_dt.strptime(ad, "%Y-%m-%d"))
                                except Exception:
                                    pass

                        finished = (not partial_listing and bool(valid_air_dates)
                                    and len(valid_air_dates) == len(regular_eps)
                                    and max(valid_air_dates) <= today)
                    else:
                        season_air = s.get("air_date") or ""
                        if season_air:
                            try:
                                s_date = _dt.strptime(season_air, "%Y-%m-%d")
                                if (today - s_date).days > 180 or work_status == "Ended":
                                    finished = True
                            except Exception:
                                pass
                        elif work_status == "Ended":
                            finished = True
                except Exception:
                    pass

            seasons_data[sn] = {
                "total": real_total if real_total > 0 else None,
                "season_listed": True,
                "episodes_tba": raw_ep_count <= 0,
                "raw_episode_count": raw_ep_count,
                "partial_listing": partial_listing,
                "parts": parts,
                "ordinals": ordinals,
                "episode_titles": episode_titles,
                "season_year": str(s.get("air_date") or "")[:4],
                "finished": finished,
            }
        if log:
            log.info("TMDB tmdb_id=%d: 获取到 %d 季信息 (已清洗非正片)", tmdb_id, len(seasons_data))
    elif mp_error or tv_status == "error":
        if meta_out is not None:
            meta_out["fetch_failed"] = True
            meta_out["error"] = mp_error or tv_err or "TMDB/MP 接口请求失败"

    if meta_out is not None:
        if tv_summary:
            present = sorted(sn for sn in seasons_data if sn > 0) or tv_summary.get("seasons_present") or []
            meta_out["work_info"] = {**tv_summary, "seasons_present": present}
        elif seasons_data:
            meta_out["work_info"] = {
                "tmdb_id": int(tmdb_id),
                "name": "",
                "original_name": "",
                "status": "",
                "seasons_present": sorted(sn for sn in seasons_data if sn > 0),
            }

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
        year = re.search(r"[（(](\d{4})[)）]", show_name)
        for source in ("douban", "tmdb"):
            candidates = set()
            for item in await mp_client.search_media(clean_name, source=source) or []:
                if not isinstance(item, dict):
                    continue
                titles = [item.get("title"), item.get("original_title"), item.get("name")]
                if _series_title(clean_name) not in {_series_title(t) for t in titles if t}:
                    continue
                if year and str(item.get("year") or str(item.get("first_air_date") or "")[:4]) != year[1]:
                    continue
                # 豆瓣搜索结果的通用 id 属于豆瓣，只有显式映射才能用来查询 TMDB。
                cid = item.get("tmdb_id") or item.get("tmdbid") or (item.get("id") if source == "tmdb" else None)
                if str(cid or "").isdigit():
                    candidates.add(int(cid))
            if len(candidates) == 1:
                tmdb_id = candidates.pop()
                break

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
    # 数字可能是片名的一部分。仅在季号与独立身份或首播年份相符时认可紧连数字的续季标题。
    series = _series_title(clean)
    same_id = bool(tmdb_id and str(detail.get("tmdb_id")) == str(tmdb_id))
    numbered_season = next((sn for sn in wanted if sn > 1 and
                            _series_title(titles[0]) == series + str(sn) and
                            (same_id or (season_years or {}).get(sn) == str(detail.get("year") or "-"))), None)
    if bound_season is None and not numbered_season and series not in {_series_title(t) for t in titles}:
        if not tmdb_id or str(detail.get("tmdb_id")) != str(tmdb_id):
            return {}

    season = _parse_season_from_text(titles[0]) or numbered_season or _parse_season_from_text(search_title)
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
        seasonal = []
        chinese_title = bool(re.search(r"[\u4e00-\u9fff]", base_queries[0]))
        for sn in sorted(unbound):
            if sn > 0:
                seasonal.append(f"{base_queries[0]} 第{season_labels.get(sn, sn)}季" if chinese_title
                                else f"{base_queries[0]} Season {sn}")
            if sn > 1 and chinese_title:
                seasonal.append(f"{base_queries[0]}{sn}")
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
        # 优先读取标题中命中季号的候选，详情预算随本次涉及的季数增长。
        ranked = sorted(candidates.items(), key=lambda item: _parse_season_from_text(item[1]) not in unbound)
        for did, title in ranked[:max(24, 4 * len(unbound))]:
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
            "douban_finished": secondary.get("finished", False),
            "is_split_edition": max(tmdb_total, db_total) >= 1.5 * min(tmdb_total, db_total)}


async def _resolve_show_metadata(
    show_name: str,
    needed_seasons: list[int],
    tmdb_id_num: int | None,
    mp_client,
    tmdb_client=None,
    log=None,
    evidence_out: dict[str, Any] | None = None,
) -> tuple[int | None, dict[int, dict]]:
    """多层闭环媒体元数据解析引擎：
    Pass 1: 本地指定的 TMDB ID 查询 (若有效则保留；若主条目有效但仅缺部分季，绝不把外剧强行当成同剧)
    Pass 2: TMDB 标题级多维智能打分检索（主条目失效/未绑定时补齐，或仅采集冲突候选供核对）
    Pass 3: 豆瓣双轨深度检索（兼容拆分版口径与季号拒绝理由记录）
    Pass 4: MoviePilot TMDB 兜底检索

    Returns:
        (resolved_tmdb_id, seasons_data)
    """
    seasons_data: dict[int, dict] = {}
    resolved_id: int | None = tmdb_id_num
    meta_probe: dict[str, Any] = {}
    primary_valid = False
    ev = evidence_out if evidence_out is not None else {}
    ev.setdefault("candidates", [])
    ev.setdefault("candidate_conflicts", {})
    ev.setdefault("rejected_douban", [])

    def is_complete() -> bool:
        if not needed_seasons:
            return bool(seasons_data)
        for sn in needed_seasons:
            if sn not in seasons_data or not (seasons_data[sn].get("total") or 0) > 0:
                return False
        return True

    # 1. 优先尝试本地指定的 TMDB ID
    if tmdb_id_num and (mp_client or tmdb_client):
        try:
            seasons_data = await _fetch_seasons_by_tmdb_id(
                mp_client, tmdb_id_num, log, tmdb_client=tmdb_client, meta_out=meta_probe
            )
        except TypeError:
            # 兼容仅接受 3 参的老式单测 Mock
            seasons_data = await _fetch_seasons_by_tmdb_id(mp_client, tmdb_id_num, log)

        if seasons_data:
            primary_valid = True
        else:
            if meta_probe.get("fetch_failed"):
                ev["fetch_failed"] = True
                ev["meta_error"] = meta_probe.get("error") or "元数据请求超时/失败"
                if log:
                    log.warning("TMDB %d 元数据查询失败: %s（保留待重查状态，不清除绑定）", tmdb_id_num, ev["meta_error"])
            else:
                if log:
                    log.warning("本地目录指定的 tmdb_id=%d 未获取到任何季信息，重置为待纠偏状态", tmdb_id_num)
                resolved_id = None

    if meta_probe.get("work_info"):
        ev["work_info"] = meta_probe["work_info"]

    # 2. TMDB 标题与母系列检索
    queries = _generate_search_queries(show_name)
    discovered_cn: list[str] = []

    clean_name = re.sub(r"\s*\[.*?\]\s*", " ", show_name)
    m_year = re.search(r"\((\d{4})\)", clean_name)
    show_year = int(m_year.group(1)) if m_year else None
    clean_name = re.sub(r"\s*\(\d{4}\)\s*", " ", clean_name).strip()
    clean_name = re.sub(r"\s+\d{4}$", "", clean_name).strip()

    if tmdb_client and not is_complete() and not ev.get("fetch_failed"):
        missing_seasons = [sn for sn in needed_seasons if sn not in seasons_data or not (seasons_data[sn].get("total") or 0) > 0]

        candidate_pool: list[dict] = []
        seen_cids = set()
        search_errors: list[str] = []

        for q in queries:
            try:
                if callable(getattr(type(tmdb_client), "fetch_search_tv", None)):
                    cands, s_status, s_err = await tmdb_client.fetch_search_tv(q)
                    if s_status == "error":
                        search_errors.append(s_err or "TMDB 搜索失败")
                        continue
                else:
                    cands = await tmdb_client.search_tv(q)
                for cand in (cands or [])[:8]:
                    cid = cand.get("id")
                    if not cid or cid in seen_cids:
                        continue
                    seen_cids.add(cid)
                    cname = cand.get("name") or ""
                    air_date = cand.get("first_air_date") or ""
                    c_year = int(air_date[:4]) if len(air_date) >= 4 and air_date[:4].isdigit() else None

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
                        "original_name": cand.get("original_name") or "",
                        "score": cand_score,
                        "exact": (_series_title(clean_name) in {_series_title(cname), _series_title(cand.get("original_name") or "")}
                                  and (not show_year or show_year == c_year)),
                        "year": c_year,
                    })
            except Exception as e:
                search_errors.append(str(e) or type(e).__name__)
                if log:
                    log.warning("TMDB 搜索 '%s' 异常: %s", q, e)
            if len(candidate_pool) >= 12:
                break

        if not candidate_pool and search_errors:
            ev["fetch_failed"] = True
            ev["meta_error"] = search_errors[0]

        candidate_pool.sort(key=lambda x: (str(x["id"]) == str(tmdb_id_num), x["exact"], x["score"]), reverse=True)
        exact_ids = {c["id"] for c in candidate_pool if c["exact"]}

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
                                if (_dt.now() - s_d).days > 180 or detail.get("status") == "Ended":
                                    s_finished = True
                            except Exception:
                                pass
                        # 关键防护：若目录已有有效 TMDB 主条目但仅缺当前季号，绝不把另一作品季数直接覆盖主条目的 total
                        if primary_valid and tmdb_id_num and str(cid) != str(tmdb_id_num):
                            ev["candidate_conflicts"].setdefault(sn, []).append({
                                "tmdb_id": cid,
                                "name": cname,
                                "original_name": detail.get("original_name") or cand.get("original_name") or "",
                                "season_number": sn,
                                "episode_count": cnt,
                                "season_year": str(s_air)[:4],
                                "first_air_date": str(detail.get("first_air_date") or ""),
                                "status": detail.get("status") or "",
                                "score": cand["score"],
                            })
                            ev["candidates"].append({
                                "tmdb_id": cid,
                                "name": cname,
                                "season_number": sn,
                                "episode_count": cnt,
                                "result": "rejected_as_primary",
                                "reason": f"主条目 TMDB {tmdb_id_num} 已有效但未收录 S{sn:02d}，候选 {cid} 仅留作冲突对照",
                            })
                            continue

                        seasons_data[sn] = {
                            "total": cnt,
                            "finished": s_finished,
                            "season_listed": True,
                            "episodes_tba": False,
                            "season_year": str(s_air)[:4],
                        }
                        if ((tmdb_id_num and str(cid) != str(tmdb_id_num)) or
                                (not tmdb_id_num and exact_ids != {cid})):
                            seasons_data[sn]["candidate_id"] = cid
                        ev["candidates"].append({
                            "tmdb_id": cid,
                            "name": cname,
                            "season_number": sn,
                            "episode_count": cnt,
                            "result": "accepted",
                            "reason": "按剧名/年份候选匹配",
                        })
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

    # 3. 豆瓣双轨深度检索与交叉核对
    if mp_client and not ev.get("fetch_failed"):
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
            if sn in db_seasons or sn in seasons_data:
                seasons_data[sn] = _merge_season_sources(seasons_data.get(sn, {}), db_seasons.get(sn))
        if log and db_seasons:
            log.info("豆瓣季度核对 %s: %s", show_name, {sn: (v["douban_id"], v["total"]) for sn, v in db_seasons.items()})

    # 4. 若依然无信息且未指定有效 tmdb_id，通过 MoviePilot 兜底检索
    if not is_complete() and not resolved_id and not ev.get("fetch_failed") and mp_client:
        mp_tid, mp_tmdb_data = await _fetch_tmdb_via_mp(mp_client, show_name, log)
        if mp_tmdb_data:
            for sn, info in mp_tmdb_data.items():
                if sn not in seasons_data or not (seasons_data[sn].get("total") or 0) > 0:
                    seasons_data[sn] = info
            if mp_tid and not resolved_id:
                resolved_id = mp_tid

    return resolved_id, seasons_data
