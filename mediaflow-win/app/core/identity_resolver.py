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

    return {
        "filename": filename,
        "has_release_name": has_release_segment and bool(series_title),
        "catalog_prefix": catalog_prefix,
        "release_series_title": series_title,
        "release_episode_title": ep_title,
        "release_years": years,
        "platform_tags": platform_tags,
    }


def _collect_known_work_titles(primary_work: dict[str, Any], show_name: str) -> set[str]:
    """汇总主条目与目录名已知的合法剧名归一化集合。"""
    candidates: list[str] = []
    clean_dir = re.sub(r"\s*\[.*?\]\s*", " ", show_name)
    clean_dir = re.sub(r"\s*\((?:19|20)\d{2}\)\s*", " ", clean_dir).strip()
    if clean_dir:
        candidates.append(clean_dir)
    for k in ("name", "title", "original_name", "original_title"):
        val = primary_work.get(k)
        if isinstance(val, str) and val.strip():
            candidates.append(val.strip())
    for alias in primary_work.get("alternative_titles") or primary_work.get("names") or []:
        if isinstance(alias, str) and alias.strip():
            candidates.append(alias.strip())
        elif isinstance(alias, dict) and alias.get("title"):
            candidates.append(str(alias["title"]).strip())

    norms: set[str] = set()
    for c in candidates:
        n = normalize_title_token(c)
        if n:
            norms.add(n)
            norms.add(strip_leading_article(n))
    return norms


def _titles_compatible(title_a: str, known_norms: set[str]) -> bool:
    if not title_a or not known_norms:
        return True
    na = normalize_title_token(title_a)
    if not na:
        return True
    na_stripped = strip_leading_article(na)
    if na in known_norms or na_stripped in known_norms:
        return True
    for kn in known_norms:
        if len(na_stripped) >= 4 and len(kn) >= 4 and (na_stripped in kn or kn in na_stripped):
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
    known_norms = _collect_known_work_titles(work_info, show_name)
    has_work_identity = bool(work_info.get("original_name") or (work_info.get("name") and _has_latin(str(work_info.get("name")))))
    clean_dir_title = re.sub(r"\s*\[.*?\]\s*|\s*\((?:19|20)\d{2}\)\s*", " ", show_name).strip()
    dir_has_latin = _has_latin(clean_dir_title)

    ep_by_path: dict[str, ParsedEpisode] = {ep.filepath: ep for ep in parsed_episodes}
    video_names: list[str] = []
    release_titles: dict[str, str] = {}
    ep_title_hints: dict[int, str] = {}
    years_seen: list[int] = []
    tags_seen: list[str] = []
    incompatible_files: list[str] = []
    sample_files: list[str] = []

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
                comparable = has_work_identity or (dir_has_latin and _has_latin(rtitle))
                if comparable and not _titles_compatible(rtitle, known_norms):
                    if name not in incompatible_files:
                        incompatible_files.append(name)
        parsed = ep_by_path.get(path)
        if parsed and parsed.episode_numbers and clue["release_episode_title"]:
            for enum in parsed.episode_numbers:
                ep_title_hints.setdefault(enum, clue["release_episode_title"])

    dominant_title = ""
    if release_titles:
        dominant_title = next(iter(release_titles.values()))

    distinct_titles = list(release_titles.values())
    mixed_works = len(release_titles) >= 2

    return {
        "video_count": len(video_names),
        "dominant_release_title": dominant_title,
        "distinct_release_titles": distinct_titles,
        "mixed_works": mixed_works,
        "incompatible_with_primary": bool(incompatible_files),
        "incompatible_files": incompatible_files[:5],
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
) -> dict[str, Any] | None:
    """读取已核实的持久化元数据快照（永不返回未核实空快照）。"""
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
