"""作品身份冲突识别、发行文件名取证与单季可逆跨条目映射引擎。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.episode_parser import ParsedEpisode
from app.models.metadata_cache import MetadataCache
from app.models.season_mapping import SeasonMapping

_SEED_MAPPING_PATH = Path(__file__).with_name("season_mappings.json")

_RELEASE_STOP_TOKENS = {
    "2160p", "1080p", "720p", "480p", "4k", "8k", "uhd", "hd",
    "web", "web-dl", "webdl", "webrip", "hdtv", "bluray", "bdrip", "remux", "dvdrip",
    "h264", "h265", "x264", "x265", "hevc", "avc", "vp9", "av1", "10bit", "8bit",
    "aac", "ddp", "ddp5", "dd5", "ac3", "eac3", "dts", "dtshd", "flac", "truehd", "atmos",
    "hdr", "hdr10", "dv", "dovi", "sdr", "amzn", "nf", "netflix", "dsnp", "hmax", "atvp",
    "baha", "bahamut", "cr", "crunchyroll", "bilibili", "b-global", "iq", "wetv", "myvideo",
    "friday", "hulu", "max", "peacock", "paramount", "showtime", "amc", "bbc", "itv",
    "multi", "dual", "chs", "cht", "zh", "cn", "eng", "sub", "subs", "hardsub", "softsub",
    "proper", "repack", "internal", "extended", "uncut",
}

_PLATFORM_TAG_MAP = {
    "baha": "Baha",
    "bahamut": "Baha",
    "nf": "Netflix",
    "netflix": "Netflix",
    "amzn": "AMZN",
    "dsnp": "DSNP",
    "hmax": "HMAX",
    "atvp": "ATVP",
    "cr": "CR",
    "crunchyroll": "CR",
    "bilibili": "Bilibili",
    "amc": "AMC",
}


def load_seed_season_mappings() -> list[dict[str, Any]]:
    """读取预置的已核实跨条目季映射注册表。"""
    try:
        data = json.loads(_SEED_MAPPING_PATH.read_text(encoding="utf-8"))
        items = data.get("mappings") or []
        return [dict(m) for m in items if isinstance(m, dict)]
    except Exception:
        return []


def normalize_title_token(text: str | None) -> str:
    """将剧名或集名归一化为纯小写字母数字/中文串，便于跨符号对比。"""
    if not text:
        return ""
    cleaned = re.sub(r"\[.*?\]|\((?:19|20)\d{2}\)", " ", str(text))
    cleaned = re.sub(
        r"第\s*[零一二两三四五六七八九十0-9]+\s*[季部辑]|\bseason\s*\d+\b|\bS\d+\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[\W_]+", "", cleaned).casefold()


def strip_leading_article(norm_text: str) -> str:
    """去除英文常见冠词前缀以支持模糊等价比较。"""
    for art in ("the", "an", "a"):
        if norm_text.startswith(art) and len(norm_text) > len(art) + 2:
            return norm_text[len(art):]
    return norm_text


def _clean_tokens_text(tokens: list[str]) -> str:
    joined = " ".join(t for t in tokens if t)
    return re.sub(r"\s+", " ", joined).strip(" ._-")


def extract_explicit_tmdb_ids(filename: str) -> list[str]:
    """从文件名中提取显式标注的 TMDB ID（例如 [tmdbid=12345] 或 {tmdb-12345}）。"""
    if not filename:
        return []
    ids: list[str] = []
    for pat in (r"\[tmdbid[=:](\d+)\]", r"\{tmdb[-=:](\d+)\}"):
        for m in re.finditer(pat, str(filename), re.IGNORECASE):
            tid = m.group(1)
            if tid and tid not in ids:
                ids.append(tid)
    return ids


def _extract_latin_core(text: str) -> str:
    """提取混排片名（如 '国际名厨争霸赛 The Final Table' 或 '[组] Final Table'）中的英文/拉丁主体。"""
    if not text:
        return ""
    cleaned = re.sub(r"\[.*?\]|\{(?:tmdb[-=:]\d+)\}|\((?:19|20)\d{2}\)", " ", str(text), flags=re.I)
    latin_runs = re.findall(r"[A-Za-z][A-Za-z0-9'’:&!?\-.\s]*[A-Za-z0-9]|[A-Za-z]{2,}", cleaned)
    if not latin_runs:
        return ""
    # 选择包含最多字母词的连续拉丁片段
    best = max(latin_runs, key=lambda s: len(re.sub(r"[^A-Za-z]", "", s)))
    return re.sub(r"\s+", " ", best).strip()


def _is_predominantly_latin(text: str) -> bool:
    cleaned = re.sub(r"\[.*?\]|\((?:19|20)\d{2}\)|[\s\W_0-9]+", "", str(text or ""))
    if not cleaned:
        return False
    latin_cnt = len(re.findall(r"[A-Za-z]", cleaned))
    non_latin_cnt = len(cleaned) - latin_cnt
    return latin_cnt >= 3 and latin_cnt > non_latin_cnt


def extract_release_clues(filename: str) -> dict[str, Any]:
    """从文件名提取原发行片名、分集标题线索、发行年份与平台标签。

    兼容两类常见命名：
    1. 重命名+保留原名：`<中文名> - S08E01 - The.Neighborhood.S08E01.Welcome.to.the.Neighborhood.1080p.mkv`
    2. 原始种子命名：`Kingdom.S06E01.2025.1080p.Baha.WEB-DL.mkv` 或 `The.Neighborhood.S08E01.mkv`
    精简无原名如 `Gravesend - S08E01.mkv` 返回 `has_release_name=False`，绝不把目录名误当成外部冲突线索。
    """
    stem = re.sub(r"\.[^.]{1,5}$", "", filename.strip())
    parts = [p.strip() for p in stem.split(" - ")]

    has_release_segment = False
    release_part = ""
    catalog_prefix = ""
    if len(parts) >= 3 and re.search(r"S\d{1,2}E\d{1,4}", parts[1], re.IGNORECASE):
        has_release_segment = True
        catalog_prefix = parts[0]
        release_part = " - ".join(parts[2:])
    elif len(parts) >= 2 and re.search(r"S\d{1,2}E\d{1,4}", parts[0], re.IGNORECASE):
        has_release_segment = True
        release_part = stem
    elif len(parts) == 1:
        # 仅当保留了原始点分/发行后缀信息时才认定具备发行线索
        if re.search(r"S\d{1,2}E\d{1,4}.*(?:1080p|2160p|720p|4K|WEB|HDTV|BluRay|H264|H265|x264|x265|HEVC|Baha|NF|AMZN|AAC|DDP)", stem, re.IGNORECASE):
            has_release_segment = True
            release_part = stem
        elif re.search(r"[.\s_]S\d{1,2}E\d{1,4}[.\s_]+", stem, re.IGNORECASE):
            # 如 Show.Name.S08E01.Episode.Title.mkv
            has_release_segment = True
            release_part = stem

    target = release_part or stem
    m_se = re.search(
        r"^(?P<head>.*?)\bS(?P<season>\d{1,2})[\s._-]*E(?P<ep>\d{1,4})(?:[-~]?E\d{1,4})?(?P<tail>.*)$",
        target,
        re.IGNORECASE,
    )

    series_title = ""
    ep_title = ""
    years: list[int] = []
    platform_tags: list[str] = []

    if m_se and has_release_segment:
        head = m_se.group("head") or ""
        tail = m_se.group("tail") or ""
        head_tokens = [t for t in re.split(r"[.\s_]+", head) if t]
        # 剥离末尾年份如 "Kingdom 2025" -> title="Kingdom", year=2025
        title_tokens: list[str] = []
        for tok in head_tokens:
            if re.fullmatch(r"(19\d{2}|20[0-3]\d)", tok):
                years.append(int(tok))
                continue
            title_tokens.append(tok)
        series_title = _clean_tokens_text(title_tokens)

        tail_tokens = [t for t in re.split(r"[.\s_-]+", tail) if t]
        ep_tokens: list[str] = []
        hit_tech = False
        for tok in tail_tokens:
            low = tok.casefold()
            if low in _PLATFORM_TAG_MAP and _PLATFORM_TAG_MAP[low] not in platform_tags:
                platform_tags.append(_PLATFORM_TAG_MAP[low])
            if re.fullmatch(r"(19\d{2}|20[0-3]\d)", tok):
                y = int(tok)
                if y not in years:
                    years.append(y)
                hit_tech = True
                continue
            if low in _RELEASE_STOP_TOKENS or re.match(r"^(?:\d{3,4}[pi]|x?26[45]|h\.?26[45]|ddp?\d|aac\d?)", low):
                hit_tech = True
                continue
            if not hit_tech and len(ep_tokens) < 10:
                ep_tokens.append(tok)
        ep_title = _clean_tokens_text(ep_tokens)
    else:
        # 即使没有标准发行段，仍提取明确平台标签/年份线索但不伪造 release_series_title
        for tok in re.split(r"[.\s_-]+", stem):
            low = tok.casefold()
            if low in _PLATFORM_TAG_MAP and _PLATFORM_TAG_MAP[low] not in platform_tags:
                platform_tags.append(_PLATFORM_TAG_MAP[low])
            if re.fullmatch(r"(19\d{2}|20[0-3]\d)", tok):
                y = int(tok)
                if y not in years:
                    years.append(y)

    file_tmdb_ids = extract_explicit_tmdb_ids(filename)
    latin_series = _extract_latin_core(series_title) if series_title else ""

    return {
        "filename": filename,
        "has_release_name": has_release_segment and bool(series_title),
        "catalog_prefix": catalog_prefix,
        "release_series_title": series_title,
        "latin_series_title": latin_series,
        "release_episode_title": ep_title,
        "release_years": years,
        "platform_tags": platform_tags,
        "file_tmdb_ids": file_tmdb_ids,
    }


def _collect_known_work_titles_with_meta(
    primary_work: dict[str, Any], show_name: str
) -> tuple[set[str], set[str], bool]:
    """汇总主条目与目录名已知的合法剧名归一化集合，并区分是否存在有效拉丁语片名/译名。"""
    candidates: list[str] = []
    clean_dir = re.sub(r"\s*\[.*?\]\s*", " ", show_name)
    clean_dir = re.sub(r"\s*\((?:19|20)\d{2}\)\s*", " ", clean_dir).strip()
    if clean_dir:
        candidates.append(clean_dir)
    for k in ("name", "title", "original_name", "original_title", "en_name"):
        val = primary_work.get(k)
        if isinstance(val, str) and val.strip():
            candidates.append(val.strip())
    for alias in (
        list(primary_work.get("aliases") or [])
        + list(primary_work.get("alternative_titles") or [])
        + list(primary_work.get("names") or [])
    ):
        if isinstance(alias, str) and alias.strip():
            candidates.append(alias.strip())
        elif isinstance(alias, dict) and alias.get("title"):
            candidates.append(str(alias["title"]).strip())

    norms: set[str] = set()
    latin_norms: set[str] = set()
    has_latin_identity = False

    for c in candidates:
        n = normalize_title_token(c)
        if n:
            norms.add(n)
            norms.add(strip_leading_article(n))
        lcore = _extract_latin_core(c)
        ln = normalize_title_token(lcore) if lcore else ""
        if ln and len(ln) >= 3:
            latin_norms.add(ln)
            latin_norms.add(strip_leading_article(ln))

    # 仅当 original_name/en_name/aliases 含拉丁片名，或主标题/目录名本身以拉丁文为主时，才判定拥有可比的拉丁发行名集合；
    # 避免 "Boss的甜蜜诱惑" + 韩文原名 被误认为已包含英文发行名从而把 "What a Boss Wants" 判作异作品。
    for k in ("original_name", "original_title", "en_name"):
        if _has_latin(str(primary_work.get(k) or "")):
            has_latin_identity = True
            break
    if not has_latin_identity:
        for alias in (
            list(primary_work.get("aliases") or [])
            + list(primary_work.get("alternative_titles") or [])
            + list(primary_work.get("names") or [])
        ):
            at = alias if isinstance(alias, str) else str((alias or {}).get("title") or "")
            if _has_latin(at) and _is_predominantly_latin(at):
                has_latin_identity = True
                break
    if not has_latin_identity:
        if _is_predominantly_latin(str(primary_work.get("name") or "")) or _is_predominantly_latin(clean_dir):
            has_latin_identity = True

    return norms | latin_norms, latin_norms, has_latin_identity


def _collect_known_work_titles(primary_work: dict[str, Any], show_name: str) -> set[str]:
    """汇总主条目与目录名已知的合法剧名归一化集合。"""
    norms, _, _ = _collect_known_work_titles_with_meta(primary_work, show_name)
    return norms


def _titles_pair_compatible(title_a: str, title_b: str) -> bool:
    """判断两个发行片名是否只是中英混排前缀/冠词/标点差异，实际指代同一作品。"""
    na = strip_leading_article(normalize_title_token(title_a))
    nb = strip_leading_article(normalize_title_token(title_b))
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 4 and len(nb) >= 4 and (na in nb or nb in na):
        return True
    la = strip_leading_article(normalize_title_token(_extract_latin_core(title_a)))
    lb = strip_leading_article(normalize_title_token(_extract_latin_core(title_b)))
    if len(la) >= 4 and len(lb) >= 4 and (la == lb or la in lb or lb in la):
        return True
    return False


def _cluster_release_titles(raw_titles: list[str], known_norms: set[str]) -> list[str]:
    """按语义等价合并多个片名写法（字幕组前缀、双语标题、已知别名），仅在真正存在不同作品时保留多簇。"""
    clusters: list[str] = []
    for t in raw_titles:
        if not t:
            continue
        merged = False
        for rep in clusters:
            if _titles_pair_compatible(t, rep):
                merged = True
                break
            if known_norms and _titles_compatible(t, known_norms) and _titles_compatible(rep, known_norms):
                merged = True
                break
        if not merged:
            clusters.append(t)
    return clusters


def _titles_compatible(title_a: str, known_norms: set[str]) -> bool:
    if not title_a or not known_norms:
        return True
    na = normalize_title_token(title_a)
    if not na:
        return True
    na_stripped = strip_leading_article(na)
    candidates_a = {na, na_stripped}
    latin_a = normalize_title_token(_extract_latin_core(title_a))
    if latin_a and len(latin_a) >= 3:
        candidates_a.add(latin_a)
        candidates_a.add(strip_leading_article(latin_a))
    if candidates_a & known_norms:
        return True
    for ca in candidates_a:
        if len(ca) < 4:
            continue
        for kn in known_norms:
            if len(kn) >= 4 and (ca in kn or kn in ca):
                return True
    return False


def _has_latin(text: str) -> bool:
    return bool(re.search(r"[a-zA-Z]{2,}", text or ""))


def analyze_season_release_signals(
    season_files: list[dict[str, Any]],
    parsed_episodes: list[ParsedEpisode],
    show_name: str,
    primary_work: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """聚合并分析整季视频文件的发行片名、跨作品混装与分集标题特征。"""
    work_info = primary_work or {}
    known_norms, latin_norms, has_latin_identity = _collect_known_work_titles_with_meta(work_info, show_name)
    primary_tid = str(work_info.get("tmdb_id") or "").strip()

    ep_by_path: dict[str, ParsedEpisode] = {ep.filepath: ep for ep in parsed_episodes}
    video_names: list[str] = []
    release_titles: dict[str, str] = {}
    ep_title_hints: dict[int, str] = {}
    years_seen: list[int] = []
    tags_seen: list[str] = []
    incompatible_files: list[str] = []
    sample_files: list[str] = []
    distinct_file_tmdb_ids: list[str] = []
    explicit_file_tmdb_conflicts: list[dict[str, str]] = []

    for item in season_files:
        if item.get("isDirectory"):
            continue
        name = str(item.get("name") or "")
        path = str(item.get("path") or name)
        if path not in ep_by_path and not re.search(r"\.(mkv|mp4|avi|ts|m2ts|mov|wmv|flv|rmvb|webm)$", name, re.I):
            continue
        video_names.append(name)
        if len(sample_files) < 5:
            sample_files.append(name)
        clue = extract_release_clues(name)
        for fid in clue.get("file_tmdb_ids") or []:
            if fid not in distinct_file_tmdb_ids:
                distinct_file_tmdb_ids.append(fid)
            if primary_tid and primary_tid.isdigit() and fid != primary_tid:
                explicit_file_tmdb_conflicts.append({"filename": name, "file_tmdb_id": fid})
        for y in clue["release_years"]:
            if y not in years_seen:
                years_seen.append(y)
        for tag in clue["platform_tags"]:
            if tag not in tags_seen:
                tags_seen.append(tag)
        rtitle = clue["release_series_title"]
        if clue["has_release_name"] and rtitle:
            norm_rt = strip_leading_article(normalize_title_token(rtitle))
            if norm_rt:
                release_titles.setdefault(norm_rt, rtitle)
                if _has_latin(rtitle):
                    comparable = has_latin_identity and bool(latin_norms or known_norms)
                else:
                    comparable = bool(known_norms)
                if comparable and not _titles_compatible(rtitle, known_norms):
                    if name not in incompatible_files:
                        incompatible_files.append(name)
        parsed = ep_by_path.get(path)
        if parsed and parsed.episode_numbers and clue["release_episode_title"]:
            for enum in parsed.episode_numbers:
                ep_title_hints.setdefault(enum, clue["release_episode_title"])

    raw_distinct = list(release_titles.values())
    clustered_titles = _cluster_release_titles(raw_distinct, known_norms)
    dominant_title = clustered_titles[0] if clustered_titles else (raw_distinct[0] if raw_distinct else "")
    mixed_works = (len(distinct_file_tmdb_ids) >= 2) or (len(clustered_titles) >= 2)

    return {
        "video_count": len(video_names),
        "dominant_release_title": dominant_title,
        "distinct_release_titles": clustered_titles,
        "raw_release_titles": raw_distinct,
        "mixed_works": mixed_works,
        "has_latin_known_identity": has_latin_identity,
        "incompatible_with_primary": bool(incompatible_files),
        "incompatible_files": incompatible_files[:5],
        "distinct_file_tmdb_ids": distinct_file_tmdb_ids,
        "explicit_file_tmdb_conflicts": explicit_file_tmdb_conflicts[:5],
        "release_years": years_seen,
        "platform_tags": tags_seen,
        "ep_title_hints": ep_title_hints,
        "sample_files": sample_files,
        "known_primary_norms": sorted(known_norms),
    }


def compute_episode_title_overlap(
    ep_title_hints: dict[int, str],
    tmdb_episodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """对比本地文件分集英文/译名线索与 TMDB 季内集名，返回命中的核对证据。"""
    if not ep_title_hints or not tmdb_episodes:
        return []
    tmdb_map = {
        int(ep["episode_number"]): str(ep.get("name") or "").strip()
        for ep in tmdb_episodes
        if isinstance(ep, dict) and ep.get("episode_number") is not None and ep.get("name")
    }
    matches: list[dict[str, Any]] = []
    for enum, hint in sorted(ep_title_hints.items()):
        official = tmdb_map.get(enum, "")
        if not official:
            continue
        nh = normalize_title_token(hint)
        no = normalize_title_token(official)
        if nh and no and (nh == no or (len(nh) >= 6 and (nh in no or no in nh))):
            matches.append({"episode": enum, "local_hint": hint, "official_title": official})
    return matches


def is_short_or_shared_title(title: str, primary_work: dict[str, Any] | None, show_name: str) -> bool:
    """判断标题是否属于主作品共用词/常见短词（如 Kingdom），严禁自动改绑。"""
    norm = strip_leading_article(normalize_title_token(title))
    if not norm:
        return True
    known = _collect_known_work_titles(primary_work or {}, show_name)
    if norm in known:
        return True
    if len(norm) <= 8 and re.fullmatch(r"[a-z0-9]+", norm):
        return True
    return False


def build_season_mapping_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """统一格式化跨条目/单季改绑映射对象（确保来源清晰、可追溯、可撤销）。"""
    return {
        "mapping_id": str(raw.get("mapping_id") or raw.get("id") or ""),
        "mapping_kind": str(raw.get("mapping_kind") or "cross_entry_alias"),
        "source_tmdb_id": int(raw["source_tmdb_id"]) if raw.get("source_tmdb_id") else None,
        "source_season": int(raw.get("source_season") or 0),
        "target_tmdb_id": int(raw["target_tmdb_id"]) if raw.get("target_tmdb_id") else None,
        "target_douban_id": str(raw.get("target_douban_id") or ""),
        "target_season": int(raw.get("target_season") or 1),
        "target_title": str(raw.get("target_title") or ""),
        "target_original_title": str(raw.get("target_original_title") or ""),
        "search_keywords": list(raw.get("search_keywords") or []),
        "archive_aliases": list(raw.get("archive_aliases") or []),
        "numbering_mode": str(raw.get("numbering_mode") or "independent"),
        "episode_offset": int(raw.get("episode_offset") or 0),
        "expected_total_episodes": int(raw["expected_total_episodes"]) if raw.get("expected_total_episodes") else None,
        "expected_status": str(raw.get("expected_status") or ""),
        "evidence_note": str(raw.get("evidence_note") or ""),
        "scope": "season",
        "created_by": str(raw.get("created_by") or "seed"),
        "reversible": True,
    }


def _orm_mapping_to_dict(row: SeasonMapping) -> dict[str, Any]:
    return build_season_mapping_payload({
        "mapping_id": f"db:{row.id}",
        "mapping_kind": row.mapping_kind,
        "source_tmdb_id": row.source_tmdb_id,
        "source_season": row.source_season,
        "target_tmdb_id": row.target_tmdb_id,
        "target_douban_id": row.target_douban_id,
        "target_season": row.target_season,
        "target_title": row.target_title,
        "target_original_title": row.target_original_title,
        "search_keywords": json.loads(row.search_keywords_json or "[]"),
        "archive_aliases": json.loads(row.archive_aliases_json or "[]"),
        "numbering_mode": row.numbering_mode,
        "episode_offset": row.episode_offset,
        "expected_total_episodes": row.expected_total_episodes,
        "evidence_note": row.evidence_note,
        "created_by": row.created_by,
    })


def find_seed_season_mapping(
    source_tmdb_id: int | None,
    source_season: int,
) -> dict[str, Any] | None:
    """从预置注册表中查找匹配当前单季的跨条目映射。"""
    if not source_tmdb_id or source_season <= 0:
        return None
    for item in load_seed_season_mappings():
        if int(item.get("source_tmdb_id") or 0) == int(source_tmdb_id) and int(item.get("source_season") or 0) == int(source_season):
            return build_season_mapping_payload(item)
    return None


async def resolve_active_season_mapping(
    db: AsyncSession | None,
    source_tmdb_id: int | None,
    source_season: int,
    show_path: str = "",
    season_path: str = "",
) -> dict[str, Any] | None:
    """按 单季路径 -> DB(tmdb_id+season) -> 预置注册表 顺序解析生效中的单季映射。"""
    norm_season_path = season_path.rstrip("/") if season_path else ""
    norm_show_path = show_path.rstrip("/") if show_path else ""
    revoked_seeds: set[str] = set()

    if db is not None:
        rows = (
            await db.execute(
                select(SeasonMapping)
                .where(SeasonMapping.source_season == source_season)
                .order_by(SeasonMapping.updated_at.desc(), SeasonMapping.id.desc())
            )
        ).scalars().all()

        for row in rows:
            if row.mapping_kind == "revoked" and row.is_active:
                if row.scope_key.startswith("seed:"):
                    revoked_seeds.add(row.scope_key.split("seed:", 1)[1])
                continue

        # 1. 最精确：精确匹配当前单季目录路径
        if norm_season_path:
            for row in rows:
                if row.is_active and row.mapping_kind != "revoked" and row.season_path.rstrip("/") == norm_season_path:
                    return _orm_mapping_to_dict(row)

        # 2. 次精确：匹配同剧目录 + 单季
        if norm_show_path:
            for row in rows:
                if row.is_active and row.mapping_kind != "revoked" and not row.season_path and row.show_path.rstrip("/") == norm_show_path:
                    return _orm_mapping_to_dict(row)

        # 3. 绑定同一 (source_tmdb_id, source_season)
        if source_tmdb_id:
            for row in rows:
                if row.is_active and row.mapping_kind != "revoked" and row.source_tmdb_id == int(source_tmdb_id):
                    if not row.season_path or row.season_path.rstrip("/") == norm_season_path:
                        return _orm_mapping_to_dict(row)

    seed = find_seed_season_mapping(source_tmdb_id, source_season)
    if seed and seed["mapping_id"] not in revoked_seeds:
        # 检查是否针对具体 path 撤销过该预置映射
        scope_revoked_key = f"seed:{seed['mapping_id']}|{norm_season_path}" if norm_season_path else ""
        if not (scope_revoked_key and scope_revoked_key in revoked_seeds):
            return seed
    return None


def translate_local_to_search_episodes(episodes: list[int], mapping: dict[str, Any] | None) -> list[int]:
    """将本地季集号转换为搜索条目的目标集号。"""
    if not episodes:
        return []
    if not mapping:
        return sorted(set(episodes))
    offset = int(mapping.get("episode_offset") or 0)
    return sorted({max(1, int(ep) - offset) for ep in episodes})


def translate_search_to_local_episodes(episodes: list[int], mapping: dict[str, Any] | None) -> list[int]:
    """将搜索/下载命中的目标集号还原为本地季集号。"""
    if not episodes:
        return []
    if not mapping:
        return sorted(set(episodes))
    offset = int(mapping.get("episode_offset") or 0)
    return sorted({max(1, int(ep) + offset) for ep in episodes})


async def get_verified_metadata_snapshot(
    db: AsyncSession | None,
    cache_key: str,
    ttl_seconds: int | None = None,
) -> dict[str, Any] | None:
    """读取已核实的持久化元数据快照（支持按完结/连载分级 TTL 有效期管理）。"""
    if db is None:
        return None
    row = (
        await db.execute(
            select(MetadataCache).where(
                MetadataCache.cache_key == cache_key,
                MetadataCache.verified.is_(True),
                MetadataCache.status == "ok",
            )
        )
    ).scalar_one_or_none()
    if not row:
        return None
    try:
        data = json.loads(row.payload_json or "{}")
        if isinstance(data, dict) and data:
            # 跨任务元数据复用有效期：已完结季 7 天，连载季 1 小时
            ref_dt = row.updated_at or row.fetched_at
            if ref_dt:
                from datetime import datetime as _dt, timezone as _tz
                now_utc = _dt.now(_tz.utc)
                if ref_dt.tzinfo is None:
                    ref_dt = ref_dt.replace(tzinfo=_tz.utc)
                age_sec = (now_utc - ref_dt).total_seconds()
                max_age = ttl_seconds
                if max_age is None:
                    is_fin = bool(data.get("finished") or (data.get("work_info") or {}).get("status") == "Ended")
                    max_age = 7 * 86400 if is_fin else 3600
                if age_sec > max_age:
                    return None
            return data
    except Exception:
        pass
    return None


async def save_verified_metadata_snapshot(
    db: AsyncSession | None,
    cache_key: str,
    source: str,
    media_id: str,
    season_number: int | None,
    payload: dict[str, Any],
) -> None:
    """仅保存真实非空且已核实的元数据快照，绝不用空结果或超时失败覆盖旧快照。"""
    if db is None or not isinstance(payload, dict) or not payload:
        return
    # 确保至少有基础有效载荷
    if source == "tmdb_season" and not payload.get("total"):
        return
    if source == "tmdb_tv" and not payload.get("seasons"):
        return
    if source == "tmdb_aliases" and not isinstance(payload.get("aliases"), list):
        return

    row = (
        await db.execute(
            select(MetadataCache).where(MetadataCache.cache_key == cache_key)
        )
    ).scalar_one_or_none()
    encoded = json.dumps(payload, ensure_ascii=False)
    if row:
        row.payload_json = encoded
        row.verified = True
        row.status = "ok"
    else:
        db.add(
            MetadataCache(
                cache_key=cache_key,
                source=source,
                media_id=str(media_id),
                season_number=season_number,
                payload_json=encoded,
                verified=True,
                status="ok",
            )
        )


def reevaluate_offline_conflict(
    show_name: str,
    tmdb_id: str | int | None,
    season_number: int,
    conflict: dict[str, Any] | None,
    evidence: dict[str, Any] | None = None,
    primary_work_snapshot: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    """按最新外文发行名/别名规则只读重估既有冲突，自动消除旧误报并保留真实错配拦截。"""
    conf = dict(conflict or {})
    ev = dict(evidence or {})
    matched_work = dict(ev.get("matched_work") or {})
    snap = dict(primary_work_snapshot or {})

    merged_work: dict[str, Any] = {
        "tmdb_id": str(tmdb_id or matched_work.get("tmdb_id") or snap.get("tmdb_id") or "").strip(),
        "name": matched_work.get("name") or snap.get("name") or "",
        "original_name": matched_work.get("original_name") or snap.get("original_name") or "",
        "en_name": matched_work.get("en_name") or snap.get("en_name") or "",
        "aliases": list(
            dict.fromkeys(
                [*(matched_work.get("aliases") or []), *(snap.get("aliases") or []), *(snap.get("alternative_titles") or [])]
            )
        ),
        "seasons_present": snap.get("seasons") or snap.get("seasons_present") or matched_work.get("seasons_present") or [],
    }

    rel_ev = ev.get("release_evidence") or {}
    sample_files = list(
        dict.fromkeys(
            [*(conf.get("sample_files") or []), *(rel_ev.get("sample_files") or [])]
        )
    )
    fake_items = [{"name": fn, "path": fn, "isDirectory": False} for fn in sample_files if fn]
    signals = analyze_season_release_signals(fake_items, [], show_name, merged_work)

    # 若样本中无文件名只存了 distinct_release_titles（历史缓存），补充前缀合并检查
    rel_distinct = [str(x) for x in (rel_ev.get("distinct_release_titles") or conf.get("clues") or []) if x]
    if not fake_items and rel_distinct:
        known_norms, _, _ = _collect_known_work_titles_with_meta(merged_work, show_name)
        clustered = _cluster_release_titles(rel_distinct, known_norms)
        signals["distinct_release_titles"] = clustered
        signals["mixed_works"] = len(clustered) >= 2

    if not conf.get("blocking"):
        return False, {}, signals

    # 1. 显式文件内 [tmdbid=...] 跨作品冲突或混装始终保留拦截
    if signals.get("explicit_file_tmdb_conflicts") or len(signals.get("distinct_file_tmdb_ids") or []) >= 2:
        return True, conf, signals

    tid_str = str(tmdb_id or "").strip()
    has_explicit_dir_tmdb = bool(
        (tid_str.isdigit() and re.search(r"\[tmdbid[=:]\d+\]", show_name or "", re.I))
        or (tid_str.isdigit() and merged_work.get("seasons_present"))
    )
    present_seasons = [int(s) for s in (merged_work.get("seasons_present") or []) if str(s).isdigit()]
    season_listed = bool(tid_str.isdigit() and present_seasons and int(season_number) in present_seasons)

    # 2. 目录明确有有效 TMDB ID 且当前季已收录：不因外文/双语译名/前缀差异保留旧误报
    if has_explicit_dir_tmdb and season_listed:
        return False, {}, signals

    kind = str(conf.get("kind") or "")
    if kind == "mixed_works":
        if signals.get("mixed_works"):
            return True, conf, signals
        return False, {}, signals

    if kind == "identity_conflict":
        dominant = str((rel_ev.get("dominant_release_title") or signals.get("dominant_release_title") or "")).strip()
        if signals.get("incompatible_with_primary"):
            return True, conf, signals
        # 若历史记录未保留文件名，仅保留 distinct 标题：仅当明确具备可比拉丁身份或有真实候选集名重叠时保留
        if not fake_items and dominant:
            known_norms, _, has_latin_id = _collect_known_work_titles_with_meta(merged_work, show_name)
            comparable = (has_latin_id if _has_latin(dominant) else bool(known_norms))
            if (comparable and not _titles_compatible(dominant, known_norms)) or conf.get("episode_title_matches"):
                return True, conf, signals
        return False, {}, signals

    if kind == "homonym_review":
        if present_seasons and int(season_number) not in present_seasons:
            return True, conf, signals
        return False, {}, signals

    return True, conf, signals
