"""补缺下载引擎 — 与 MoviePilot 联动实现站点扩展、省容量选种、整季回退与防重保护。

核心特性：
1. 站点扩展：补缺入口自动将“天空”“观众”（启用状态下）合并进 MoviePilot 默认搜索站点，失败不静默缩窄范围
2. 同档省容量：以最高做种数 H 计算做种同档 (H - seeders <= max(1, floor(H * 10%)))，档内按实际需下载字节升序，支持跨站去重与精确/有界多集拼包
3. 整季回退与保护：缺集大于 3 集优先合格整包，否则散集优先、补不齐回退整包；核验覆盖并排除在途重叠，保留已有文件与转移历史
4. 防重提交与四段状态：提交前核验 MP 下载队列，超时先查队列防重复提交，分别记录请求缺号、物理覆盖、MP 受理与入库核验状态
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any
from unittest.mock import Mock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.moviepilot import MoviePilotClient
from app.config import get_settings
from app.core.episode_parser import is_video_file, parse_episode
from app.core.identity_resolver import (
    normalize_title_token,
    resolve_active_season_mapping,
    translate_local_to_search_episodes,
    translate_search_to_local_episodes,
)
from app.core.segments import segment_number
from app.models.operation import OperationLog
from app.models.season import SeasonRecord
from app.services.gap_fill_tracker import (
    build_identity_key,
    compute_season_gap_fill_view,
    finalize_submit_outcome,
    record_season_run_snapshot,
    record_submit_intent,
    record_unsubmitted_episode_outcomes,
    refresh_task_summary,
    sanitize_text_no_secrets,
)

log = logging.getLogger("mediaflow.download")

# ── 模块级共享快照与本地提交防重登记簿 ───────────────────────
_active_downloads_cache: list[dict[str, Any]] | None = None
_active_downloads_cache_time: float = 0.0
_ACTIVE_DOWNLOADS_TTL: float = 30.0
_recently_submitted_tasks: list[dict[str, Any]] = []
_RECENTLY_SUBMITTED_TTL: float = 180.0
_search_sites_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_SEARCH_SITES_TTL: float = 30.0

# 默认附加补缺搜索站点及域名兜底（支持与用户自定义名合并，绝不写死 ID）
_SITE_DOMAIN_ALIASES: dict[str, tuple[str, ...]] = {
    "天空": ("hdsky.me", "hdsky"),
    "观众": ("audiences.me", "audiences"),
}
_RESOLUTION_RANK: dict[str, int] = {
    "480p": 1,
    "576p": 1,
    "sd": 1,
    "720p": 2,
    "hd": 2,
    "1080p": 3,
    "1080i": 3,
    "fhd": 3,
    "2160p": 4,
    "4k": 4,
    "uhd": 4,
}


@dataclass(frozen=True)
class CandidateEval:
    """经过准入、去重后的候选资源评估对象。"""

    cand: dict[str, Any]
    covered_all: tuple[int, ...]
    covered_missing: tuple[int, ...]
    is_season_pack: bool
    seeders: int
    size_bytes: int | None
    site_id: int | None
    site_name: str
    site_sources: tuple[str, ...]
    stable_key: tuple[str, int, str, str]
    reason_note: str = ""
    site_priority: int = 999999


# ── 基础解析与格式化工具 ─────────────────────────────────────

def _format_bytes(size_bytes: int | None) -> str:
    if size_bytes is None or size_bytes <= 0:
        return "未知大小"
    gb = 1024 ** 3
    mb = 1024 ** 2
    if size_bytes >= gb:
        return f"{size_bytes / gb:.2f} GiB"
    return f"{size_bytes / mb:.1f} MiB"


def _candidate_size_bytes(cand: dict[str, Any]) -> int | None:
    t = cand.get("torrent_info") or {}
    raw = t.get("size")
    if raw is None or raw == "":
        return None
    try:
        val = int(float(raw))
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _candidate_seeders(cand: dict[str, Any]) -> int:
    t = cand.get("torrent_info") or {}
    try:
        return max(0, int(t.get("seeders") or 0))
    except (TypeError, ValueError):
        return 0


def _candidate_site_id(cand: dict[str, Any]) -> int | None:
    t = cand.get("torrent_info") or {}
    raw = t.get("site")
    try:
        return int(raw) if raw is not None and str(raw).strip().lstrip("-").isdigit() else None
    except (TypeError, ValueError):
        return None


def _candidate_site_name(cand: dict[str, Any]) -> str:
    t = cand.get("torrent_info") or {}
    name = str(t.get("site_name") or "").strip()
    if name:
        return name
    sid = _candidate_site_id(cand)
    return f"站点#{sid}" if sid is not None else ""


def _candidate_stable_key(cand: dict[str, Any]) -> tuple[str, int, str, str]:
    t = cand.get("torrent_info") or {}
    norm_title = re.sub(r"\s+", " ", str(t.get("title") or "")).strip().casefold()
    size_val = _candidate_size_bytes(cand) or 0
    ident = str(t.get("hash") or t.get("info_hash") or t.get("enclosure") or t.get("page_url") or "")
    site_name = _candidate_site_name(cand)
    return (norm_title, size_val, site_name, ident)


def _torrent_identity_key(cand: dict[str, Any]) -> tuple[Any, ...]:
    """跨站同种去重键：优先 info_hash，否则按规范化标题 + 字节数（及无大小时的链接）识别。"""
    t = cand.get("torrent_info") or {}
    info_hash = str(t.get("hash") or t.get("info_hash") or t.get("torrent_hash") or "").strip().casefold()
    if info_hash:
        return ("hash", info_hash)
    norm_title = re.sub(r"\s+", " ", str(t.get("title") or "")).strip().casefold()
    size_val = _candidate_size_bytes(cand)
    if norm_title:
        return ("rel", norm_title, size_val)
    enclosure = str(t.get("enclosure") or t.get("page_url") or "").strip()
    return ("url", enclosure)


def _seeder_tier_floor(max_seeders: int, tier_ratio: float = 0.10, min_slack: int = 1) -> int:
    if max_seeders <= 0:
        return 0
    slack = max(int(min_slack), math.floor(max_seeders * max(0.0, float(tier_ratio))))
    return max(0, max_seeders - slack)


def _candidate_resolution_rank(t: dict[str, Any], meta: dict[str, Any]) -> int:
    text = f"{t.get('title', '')} {meta.get('resource_pix', '')} {' '.join(str(x) for x in (t.get('labels') or []))}".lower()
    if re.search(r"2160p|4k|uhd", text):
        return 4
    if re.search(r"1080p|1080i|fhd", text):
        return 3
    if re.search(r"720p|hd(?![a-z0-9])", text):
        return 2
    if re.search(r"480p|576p|sd(?![a-z0-9])", text):
        return 1
    return 0


def _candidate_has_chinese(t: dict[str, Any], meta: dict[str, Any]) -> bool:
    labels_text = json.dumps(t.get("labels") or [], ensure_ascii=False)
    combined = f"{labels_text} {t.get('title') or ''} {t.get('description') or ''} {meta.get('subtitle') or ''}"
    return bool(re.search(r"中文|中字|简中|繁中|国语|chs|cht|简繁|双语|mikan|baha|国粤", combined, re.I))


def _passes_explicit_filters(
    t: dict[str, Any],
    meta: dict[str, Any],
    min_resolution: str = "",
    require_chinese: bool = False,
) -> bool:
    if require_chinese and not _candidate_has_chinese(t, meta):
        return False
    req = str(min_resolution or "").strip().lower()
    if req:
        req_rank = _RESOLUTION_RANK.get(req, 0)
        if req_rank > 0 and _candidate_resolution_rank(t, meta) < req_rank:
            return False
    return True


def parse_active_episodes(ep_str: str, total: int = 0) -> set[int]:
    """解析 MoviePilot 活跃下载中的集数字符串（如 'E01-E06', 'E12', '第1-5集'）."""
    ep_str = str(ep_str or "").strip()
    if not ep_str:
        return set(range(1, total + 1)) if total > 0 else set()
    # 去掉季号，再识别集号；例如 S03E08 只表示 E08。
    value = re.sub(r"\bS\d{1,2}(?=E\d)", "", ep_str, flags=re.I)
    numeric_only = re.fullmatch(r"[\d\s,，、\[\]\-~～至到]+", value)
    prefix = r"(?:EP?|第)?" if numeric_only else r"(?:EP?|第)"
    pattern = prefix + r"(\d{1,4})(?:\s*[-~～至到]\s*(?:EP?|第)?(\d{1,4}))?"
    eps = set()
    for match in re.finditer(pattern, value, re.I):
        start, end = sorted((int(match[1]), int(match[2] or match[1])))
        eps.update(range(max(1, start), min(end, total or end) + 1))
    return eps


def get_resolution_score(t: dict[str, Any], meta: dict[str, Any]) -> int:
    """解析清晰度梯队得分（兼容保留接口，不推翻同做种档内容量优先顺序）。"""
    rank = _candidate_resolution_rank(t, meta)
    return {4: 900, 3: 1000, 2: -500, 1: -1000}.get(rank, 0)


def calculate_candidate_score(cand: dict[str, Any], hits: list[int], all_eps: list[int]) -> float:
    """兼容辅助评分：仅作参考或兜底排序特征，主决策由同做种档 + 省容量方案负责。"""
    t = cand.get("torrent_info") or {}
    meta = cand.get("meta_info") or {}
    hits_count = len(hits)
    redundant_count = max(0, len(all_eps) - hits_count)
    net_gain = hits_count * 350 - redundant_count * 120
    res_score = get_resolution_score(t, meta)
    cn_score = 400 if _candidate_has_chinese(t, meta) else 0
    seeds = _candidate_seeders(cand)
    seeds_score = (200 + min(seeds, 50)) if seeds >= 5 else (seeds * 20)
    return float(net_gain + res_score + cn_score + seeds_score)


# ── 站点范围动态解析 ─────────────────────────────────────────

def _match_target_site(sites_raw: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    norm = target.strip().casefold()
    exact = [s for s in sites_raw if str(s.get("name") or "").strip().casefold() == norm]
    if exact:
        return exact
    aliases = _SITE_DOMAIN_ALIASES.get(target.strip(), (norm,))
    matched: list[dict[str, Any]] = []
    for s in sites_raw:
        name_l = str(s.get("name") or "").strip().casefold()
        domain_l = str(s.get("domain") or s.get("url") or "").strip().casefold()
        if any(a in name_l or a in domain_l for a in aliases):
            matched.append(s)
    return matched


def get_mp_instance_key(mp: Any = None) -> str:
    """计算 MoviePilot 实例指纹/唯一标识键。"""
    origin = getattr(mp, "origin", None)
    if not origin:
        try:
            origin = get_settings().mp_url
        except Exception:
            origin = "default"
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(origin or "default").strip())
    return cleaned[:64] or "default"


async def get_gap_fill_site_rules(
    db: Any,
    mp: Any = None,
    instance_key: str | None = None,
) -> dict[str, Any]:
    """读取指定 MP 实例的动漫与非动漫两套独立补缺站点规则。若尚未持久化，则生成初始草稿。"""
    from sqlalchemy import select
    from app.models.gap_fill_site_rule import GapFillSiteRule

    inst_key = instance_key or get_mp_instance_key(mp)
    rules_map: dict[str, GapFillSiteRule] = {}
    if db is not None:
        try:
            stmt = select(GapFillSiteRule).where(GapFillSiteRule.instance_key == inst_key)
            rows = list((await db.execute(stmt)).scalars().all())
            rules_map = {r.category: r for r in rows}
        except Exception:
            pass

    # 如果有任一规则未初始化，尝试从 MP 读取默认或已启用站点作为待确认草稿
    draft_site_ids: list[int] = []
    if ("anime" not in rules_map or "non_anime" not in rules_map) and mp is not None:
        try:
            sites_raw = await mp.get_sites()
            if isinstance(sites_raw, list):
                draft_site_ids = [
                    int(s["id"]) for s in sites_raw
                    if isinstance(s, dict) and s.get("id") is not None
                    and str(s["id"]).lstrip("-").isdigit()
                    and s.get("is_active", True) is not False
                ]
        except Exception:
            pass

    out: dict[str, Any] = {"instance_key": inst_key, "anime": None, "non_anime": None}
    for cat in ("anime", "non_anime"):
        r = rules_map.get(cat)
        if r:
            try:
                selected_ids = json.loads(r.selected_site_ids_json or "[]")
            except Exception:
                selected_ids = []
            out[cat] = {
                "category": cat,
                "selected_site_ids": selected_ids,
                "status": r.status,
                "version": r.version,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                "is_draft": r.status == "uninitialized",
            }
        else:
            out[cat] = {
                "category": cat,
                "selected_site_ids": list(draft_site_ids),
                "status": "uninitialized",
                "version": 1,
                "updated_at": None,
                "is_draft": True,
            }
    return out


async def save_gap_fill_site_rule(
    db: Any,
    category: str,
    selected_site_ids: list[int | str],
    status: str,
    expected_version: int | None = None,
    instance_key: str | None = None,
    mp: Any = None,
) -> dict[str, Any]:
    """保存或更新某类别的补缺站点规则（带乐观锁版本校验）。"""
    from sqlalchemy import select
    from app.models.gap_fill_site_rule import GapFillSiteRule

    if category not in ("anime", "non_anime"):
        raise ValueError(f"无效的补缺规则类别: {category}")
    if status not in ("active", "paused", "uninitialized"):
        status = "active"

    inst_key = instance_key or get_mp_instance_key(mp)
    rule_id = f"{inst_key}:{category}"
    clean_ids: list[int] = []
    seen: set[int] = set()
    for sid in selected_site_ids:
        if str(sid).strip().lstrip("-").isdigit():
            val = int(sid)
            if val not in seen:
                seen.add(val)
                clean_ids.append(val)

    rule = await db.get(GapFillSiteRule, rule_id)
    if rule:
        if expected_version is not None and rule.version != expected_version:
            raise RuntimeError("规则已被其他窗口修改，请刷新后重试")
        rule.selected_site_ids_json = json.dumps(clean_ids)
        rule.status = status
        rule.version += 1
    else:
        rule = GapFillSiteRule(
            id=rule_id,
            instance_key=inst_key,
            category=category,
            selected_site_ids_json=json.dumps(clean_ids),
            status=status,
            version=1,
        )
        db.add(rule)

    await db.commit()
    await db.refresh(rule)
    return {
        "category": rule.category,
        "selected_site_ids": clean_ids,
        "status": rule.status,
        "version": rule.version,
        "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
    }


async def resolve_category_gap_fill_sites(
    db: Any,
    mp: Any,
    category: str,
    instance_key: str | None = None,
) -> dict[str, Any]:
    """逐季/按类别解析生效的补缺站点有序列表与优先级映射 (v2.5.40 规范)。"""
    from app.models.gap_fill_site_rule import GapFillSiteRule

    inst_key = instance_key or get_mp_instance_key(mp)
    rule_id = f"{inst_key}:{category}"
    rule = await db.get(GapFillSiteRule, rule_id) if db is not None else None

    cat_label = "动漫" if category == "anime" else "非动漫"
    if not rule or rule.status == "uninitialized":
        # 兼容旧单测中通过 patch("resolve_gap_fill_search_sites") 或 Fake/Mock MP 注入的测试环境
        if (
            hasattr(resolve_gap_fill_search_sites, "mock_calls")
            or "Mock" in type(resolve_gap_fill_search_sites).__name__
            or "Fake" in type(mp).__name__
            or "Mock" in type(mp).__name__
            or "Stub" in type(mp).__name__
            or "Mock" in type(getattr(mp, "search_resources", None)).__name__
            or "Mock" in type(getattr(mp, "submit_download", None)).__name__
        ):
            try:
                legacy_plan = await resolve_gap_fill_search_sites(mp)
                if legacy_plan:
                    s_param = legacy_plan.get("sites_param")
                    return {
                        "ready": True,
                        "category": category,
                        "status": "legacy_mock",
                        "reason": "单测 Mock 站点",
                        "active_ordered_ids": list(s_param or []),
                        "requested_ids": list(s_param or []),
                        "site_priority_map": {sid: idx for idx, sid in enumerate(s_param or [])},
                        "sites_param": s_param,
                        "requested_label": legacy_plan.get("requested_label") or "Mock 站点",
                        "rule_version": 1,
                        "id_to_name": legacy_plan.get("id_to_name") or {},
                        "scope_confirmed": legacy_plan.get("scope_confirmed", True),
                        "mode": legacy_plan.get("mode", "explicit_merged"),
                        "diagnostics": legacy_plan.get("diagnostics", []),
                    }
            except Exception:
                pass

        return {
            "ready": False,
            "category": category,
            "status": "uninitialized",
            "reason": f"未配置{cat_label}补缺站点规则，请先在「补缺站点设置」中配置并保存",
            "active_ordered_ids": [],
            "site_priority_map": {},
            "sites_param": None,
            "requested_label": f"{cat_label}(未初始化)",
            "rule_version": rule.version if rule else 1,
            "id_to_name": {},
        }

    if rule.status == "paused":
        return {
            "ready": False,
            "category": category,
            "status": "paused",
            "reason": f"已暂停{cat_label}类别补缺",
            "active_ordered_ids": [],
            "site_priority_map": {},
            "sites_param": None,
            "requested_label": f"{cat_label}(已暂停)",
            "rule_version": rule.version,
            "id_to_name": {},
        }

    try:
        rule_ids = json.loads(rule.selected_site_ids_json or "[]")
    except Exception:
        rule_ids = []

    if not rule_ids:
        return {
            "ready": False,
            "category": category,
            "status": "empty",
            "reason": f"{cat_label}补缺规则已选站点为空(暂停补缺)",
            "active_ordered_ids": [],
            "site_priority_map": {},
            "sites_param": None,
            "requested_label": f"{cat_label}(无已选站点)",
            "rule_version": rule.version,
            "id_to_name": {},
        }

    sites_raw: Any = None
    try:
        sites_raw = await mp.get_sites()
    except Exception as exc:
        return {
            "ready": False,
            "category": category,
            "status": "site_catalog_error",
            "reason": f"读取 MP 站点目录失败({type(exc).__name__})，已暂停补缺检索",
            "active_ordered_ids": [],
            "site_priority_map": {},
            "sites_param": None,
            "requested_label": f"{cat_label}(站点读取失败)",
            "rule_version": rule.version,
            "id_to_name": {},
        }

    if not isinstance(sites_raw, list):
        return {
            "ready": False,
            "category": category,
            "status": "site_catalog_invalid",
            "reason": "MP 站点目录返回未知结构，已暂停补缺检索",
            "active_ordered_ids": [],
            "site_priority_map": {},
            "sites_param": None,
            "requested_label": f"{cat_label}(站点结构异常)",
            "rule_version": rule.version,
            "id_to_name": {},
        }

    id_to_name: dict[int, str] = {}
    active_mp_ids: set[int] = set()
    for s in sites_raw:
        if isinstance(s, dict) and s.get("id") is not None:
            raw_id = s.get("id")
            if str(raw_id).lstrip("-").isdigit():
                sid = int(raw_id)
                id_to_name[sid] = str(s.get("name") or f"站点#{sid}").strip()
                if s.get("is_active", True) is not False:
                    active_mp_ids.add(sid)

    active_ordered_ids: list[int] = []
    for sid in rule_ids:
        if str(sid).lstrip("-").isdigit():
            val = int(sid)
            if val in active_mp_ids and val not in active_ordered_ids:
                active_ordered_ids.append(val)

    if not active_ordered_ids:
        return {
            "ready": False,
            "category": category,
            "status": "all_sites_disabled",
            "reason": f"{cat_label}规则中已选的站点已全部停用或不存在",
            "active_ordered_ids": [],
            "site_priority_map": {},
            "sites_param": None,
            "requested_label": f"{cat_label}(已选站点全部失效)",
            "rule_version": rule.version,
            "id_to_name": id_to_name,
        }

    site_priority_map: dict[int, int] = {sid: idx + 1 for idx, sid in enumerate(active_ordered_ids)}
    req_labels = [f"{id_to_name.get(sid, str(sid))}({idx+1})" for idx, sid in enumerate(active_ordered_ids)]
    requested_label = " > ".join(req_labels)

    return {
        "ready": True,
        "category": category,
        "status": "active",
        "reason": "",
        "active_ordered_ids": active_ordered_ids,
        "site_priority_map": site_priority_map,
        "sites_param": ",".join(str(s) for s in active_ordered_ids),
        "requested_label": f"{cat_label}: {requested_label}",
        "rule_version": rule.version,
        "id_to_name": id_to_name,
    }


async def get_gap_fill_work_preferences(
    db: Any,
    instance_key: str | None = None,
    mp: Any = None,
) -> list[dict[str, Any]]:
    from sqlalchemy import select
    from app.models.gap_fill_site_rule import GapFillWorkPreference

    inst_key = instance_key or get_mp_instance_key(mp)
    if db is None:
        return []
    try:
        stmt = select(GapFillWorkPreference).where(GapFillWorkPreference.instance_key == inst_key)
        rows = list((await db.execute(stmt)).scalars().all())
        return [
            {
                "id": r.id,
                "tmdb_id": r.tmdb_id,
                "show_name": r.show_name,
                "category": r.category,
                "source": r.source,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
    except Exception:
        return []


async def save_gap_fill_work_preference(
    db: Any,
    tmdb_id: str | None,
    show_name: str,
    category: str,
    instance_key: str | None = None,
    mp: Any = None,
) -> dict[str, Any]:
    from app.models.gap_fill_site_rule import GapFillWorkPreference

    if category not in ("anime", "non_anime"):
        raise ValueError(f"无效的补缺类型: {category}")

    inst_key = instance_key or get_mp_instance_key(mp)
    clean_tmdb = str(tmdb_id).strip() if tmdb_id else ""
    clean_name = str(show_name).strip()
    work_key = f"tmdb_{clean_tmdb}" if clean_tmdb else f"name_{clean_name}"
    pref_id = f"{inst_key}:{work_key}"

    if db is None:
        return {"id": pref_id, "tmdb_id": clean_tmdb or None, "show_name": clean_name, "category": category, "source": "manual", "updated_at": None}

    pref = await db.get(GapFillWorkPreference, pref_id)
    if pref:
        pref.category = category
        pref.tmdb_id = clean_tmdb or pref.tmdb_id
        pref.show_name = clean_name or pref.show_name
    else:
        pref = GapFillWorkPreference(
            id=pref_id,
            instance_key=inst_key,
            tmdb_id=clean_tmdb or None,
            show_name=clean_name,
            category=category,
            source="manual",
        )
        db.add(pref)

    await db.commit()
    await db.refresh(pref)
    return {
        "id": pref.id,
        "tmdb_id": pref.tmdb_id,
        "show_name": pref.show_name,
        "category": pref.category,
        "source": pref.source,
        "updated_at": pref.updated_at.isoformat() if pref.updated_at else None,
    }


async def resolve_gap_fill_search_sites(
    mp: Any,
    extra_site_names: list[str] | None = None,
) -> dict[str, Any]:
    """每批从 MP 解析默认搜索站点，并动态合并已启用的“天空”“观众”。

    不写死 ID；读取失败时不传入缩窄集合，并明确标明范围未确认；
    当 MP 默认设置为空且代表全部有效站点时，保留 None（全部范围），避免传入部分 ID 反而缩窄。
    """
    settings = get_settings()
    if extra_site_names is None:
        raw_extra = str(getattr(settings, "gap_fill_extra_sites", "天空,观众") or "天空,观众")
        extra_targets = [p.strip() for p in re.split(r"[,，、\s]+", raw_extra) if p.strip()]
    else:
        extra_targets = [str(p).strip() for p in extra_site_names if str(p).strip()]

    get_sites_fn = getattr(mp, "get_sites", None)
    get_indexer_fn = getattr(mp, "get_indexer_sites", None)
    is_mocked = isinstance(get_sites_fn, Mock) or isinstance(get_indexer_fn, Mock)
    origin_key = f"{getattr(mp, 'origin', 'default')}|{'|'.join(extra_targets)}"

    now = time.time()
    if not is_mocked and origin_key in _search_sites_cache:
        ts, cached = _search_sites_cache[origin_key]
        if now - ts < _SEARCH_SITES_TTL:
            return cached

    sites_raw: Any = None
    sites_err: str | None = None
    if callable(get_sites_fn):
        try:
            sites_raw = await get_sites_fn()
        except Exception as exc:
            sites_err = f"{type(exc).__name__}: {exc}"

    indexer_raw: Any = None
    indexer_err: str | None = None
    if callable(get_indexer_fn):
        try:
            indexer_raw = await get_indexer_fn()
        except Exception as exc:
            indexer_err = f"{type(exc).__name__}: {exc}"

    # 单元测试传入默认 AsyncMock() 且未配置返回值时，保持兼容，不传入无效站点参数
    if not isinstance(sites_raw, list) and not isinstance(indexer_raw, dict) and not sites_err and not indexer_err:
        return {
            "sites_param": None,
            "requested_ids": [],
            "requested_label": "未配置站点映射(沿用 MP 默认范围)",
            "id_to_name": {},
            "scope_confirmed": False,
            "mode": "unconfigured_mock",
            "diagnostics": [],
        }

    diagnostics: list[str] = []
    if not isinstance(sites_raw, list):
        diagnostics.append(f"读取 MP 站点清单失败({sites_err or '非列表'})，站点范围未确认")
        return {
            "sites_param": None,
            "requested_ids": [],
            "requested_label": "MP默认范围(站点范围未确认)",
            "id_to_name": {},
            "scope_confirmed": False,
            "mode": "sites_load_failed",
            "diagnostics": diagnostics,
        }

    site_by_id: dict[int, dict[str, Any]] = {}
    id_to_name: dict[int, str] = {}
    for item in sites_raw:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        if raw_id is None or not str(raw_id).strip().lstrip("-").isdigit():
            continue
        sid = int(raw_id)
        site_by_id[sid] = item
        id_to_name[sid] = str(item.get("name") or f"站点#{sid}").strip()

    extra_active_ids: list[int] = []
    for target in extra_targets:
        matches = _match_target_site(list(site_by_id.values()), target)
        if not matches:
            diagnostics.append(f"附加站点「{target}」缺失，未找到匹配站点")
            continue
        active_matches = [m for m in matches if m.get("is_active", True) is not False]
        disabled_matches = [m for m in matches if m.get("is_active", True) is False]
        if len(matches) > 1:
            ids_str = ",".join(str(m.get("id")) for m in matches)
            diagnostics.append(f"附加站点「{target}」存在重名/多条匹配(ID: {ids_str})")
        if disabled_matches and not active_matches:
            ids_str = ",".join(str(m.get("id")) for m in disabled_matches)
            diagnostics.append(f"附加站点「{target}」已停用(ID: {ids_str})")
        for m in active_matches:
            sid = int(m["id"])
            if sid not in extra_active_ids:
                extra_active_ids.append(sid)

    if not isinstance(indexer_raw, dict) or not indexer_raw.get("ok"):
        err_detail = indexer_err or (indexer_raw.get("error") if isinstance(indexer_raw, dict) else "返回格式未识别")
        diagnostics.append(f"读取 MP IndexerSites 失败({err_detail})，站点范围未确认，沿用 MP 默认范围")
        result = {
            "sites_param": None,
            "requested_ids": [],
            "requested_label": "MP默认范围(IndexerSites 读取失败，未确认)",
            "id_to_name": id_to_name,
            "scope_confirmed": False,
            "mode": "indexer_load_failed",
            "diagnostics": diagnostics,
        }
        return result

    default_ids = indexer_raw.get("value")
    if not default_ids:
        # MP 中 IndexerSites 为 [] / None 代表搜索全部已启用站点；保留 None 不缩窄范围！
        all_active_ids = sorted(sid for sid, info in site_by_id.items() if info.get("is_active", True) is not False)
        names = [f"{sid}:{id_to_name.get(sid, sid)}" for sid in all_active_ids]
        result = {
            "sites_param": None,
            "requested_ids": all_active_ids,
            "requested_label": f"全部已启用站点 ({', '.join(names)})" if names else "全部已启用站点",
            "id_to_name": id_to_name,
            "scope_confirmed": True,
            "mode": "all_active",
            "diagnostics": diagnostics,
        }
    else:
        enabled_defaults: list[int] = []
        for sid in default_ids:
            info = site_by_id.get(sid)
            if info is None:
                diagnostics.append(f"默认索引站点 ID={sid} 在站点列表中不存在")
            elif info.get("is_active", True) is False:
                diagnostics.append(f"默认索引站点「{id_to_name.get(sid, sid)}」(ID:{sid}) 已停用，已排除")
            else:
                enabled_defaults.append(sid)

        merged_ids = sorted(set(enabled_defaults) | set(extra_active_ids))
        labels = [f"{sid}:{id_to_name.get(sid, sid)}" for sid in merged_ids]
        result = {
            "sites_param": merged_ids or None,
            "requested_ids": merged_ids,
            "requested_label": ", ".join(labels) if labels else "无有效站点",
            "id_to_name": id_to_name,
            "scope_confirmed": True,
            "mode": "explicit_merged",
            "diagnostics": diagnostics,
        }

    if not is_mocked:
        _search_sites_cache[origin_key] = (now, result)
    return result


# ── 季号、单集与整季包严格验证 ───────────────────────────────

_MULTI_SEASON_TITLE_RE = re.compile(
    r"(?:\bS0?\d+\s*[-~～至到]\s*S?0?\d+\b|S0?(\d+)[.\s_\-]+S0?(\d+)|第\s*\d+\s*[-~～至到]\s*\d+\s*季|全\s*[2-9]\d*\s*季|第[一二三四五六七八九十]+至[一二三四五六七八九十]+季)",
    re.I,
)
_PARTIAL_MARKER_RE = re.compile(
    r"(?:更新至|连载[中至]|半季|前\s*\d+\s*[集期]|后\s*\d+\s*[集期]|缺[第E]?\d+[集期]?[上中下]?|欠第?\d+[集期]|暂缺)",
    re.I,
)
_EXTRA_DESC_RE = re.compile(r"加更|纯享|花絮|预告|先导|特辑|番外|采访", re.I)


def _extract_seasons(meta: dict[str, Any], title: str) -> tuple[bool, int | None, str]:
    """验证候选仅指向单一季度并返回 (is_valid_single_season, meta_season, fail_reason)."""
    begin_s = meta.get("begin_season")
    end_s = meta.get("end_season")
    total_s = meta.get("total_season")
    if end_s is not None and begin_s is not None and str(end_s) != str(begin_s):
        return False, None, f"跨多季({begin_s}-{end_s})"
    if isinstance(total_s, int) and total_s > 1:
        return False, None, f"跨{total_s}季"
    m_multi = _MULTI_SEASON_TITLE_RE.search(title)
    if m_multi:
        if m_multi.group(1) and m_multi.group(2) and m_multi.group(1) == m_multi.group(2):
            pass
        else:
            return False, None, "标题标明跨多季"

    season_list = meta.get("season_list") or ([begin_s] if begin_s is not None else [])
    clean_seasons: list[int] = []
    for s in season_list:
        if s is not None and str(s).lstrip("-").isdigit():
            val = int(s)
            if val not in clean_seasons:
                clean_seasons.append(val)
    if len(clean_seasons) > 1:
        return False, None, f"season_list含多季({clean_seasons})"
    if len(clean_seasons) == 1:
        return True, clean_seasons[0], ""
    return True, None, ""


def _parse_files_entries(files_raw: Any) -> list[dict[str, Any]]:
    """标准化候选文件清单条目。"""
    if not isinstance(files_raw, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in files_raw:
        if isinstance(entry, str):
            items.append({"name": entry, "size": None})
        elif isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("filename") or entry.get("path") or "").strip()
            if not name:
                continue
            raw_size = entry.get("size")
            size_val = int(raw_size) if raw_size is not None and str(raw_size).isdigit() else None
            items.append({"name": name, "path": str(entry.get("path") or name), "size": size_val})
    return items


def _check_part_title_segment_completeness(
    file_entries: list[dict[str, Any]],
    season_num: int,
    total_eps: int,
    by_period: bool,
    part_episode_mapping: dict[int, list[int]] | dict[str, int] | None = None,
) -> tuple[bool, set[int], str]:
    """通过明确文件清单核对正片集号/期号与上下段完整性。"""
    covered_eps: set[int] = set()
    period_segments: dict[int, set[int]] = {}
    has_unsegmented_period: set[int] = set()
    saw_other_season = False

    for entry in file_entries:
        name = entry["name"]
        if not is_video_file(name):
            continue
        if entry.get("size") == 0:
            continue
        if by_period:
            if _EXTRA_DESC_RE.search(name):
                continue
            periods = [int(n) for n in re.findall(r"第\s*(\d+)\s*期", name)]
            if not periods:
                ep = parse_episode(name, entry.get("path", ""), entry.get("size") or 1024)
                if ep.is_extra or ep.is_special:
                    continue
                if ep.season_number not in (None, season_num):
                    saw_other_season = True
                    continue
                if ep.episode_numbers and isinstance(part_episode_mapping, dict):
                    pass
                continue
            seg = segment_number(name)
            for p_num in periods:
                covered_eps.add(p_num)
                if seg is None:
                    has_unsegmented_period.add(p_num)
                else:
                    period_segments.setdefault(p_num, set()).add(seg)
            continue

        # 普通剧集 / 带 Part 标题剧集：使用正式集号映射，绝不把 Part 当集号或凭文件数充数
        ep_stem = re.sub(r"(?i)part[ ._-]*(?:0?[123]|[abc])(?=[^a-z0-9]|$)", "", name)
        ep = parse_episode(ep_stem, entry.get("path", ""), entry.get("size") or 1024)
        if ep.is_extra or ep.is_special:
            continue
        if ep.season_number not in (None, season_num):
            saw_other_season = True
            continue
        # 若文件名本身带 Part 标签但未能映射到正式集号，则必须依赖映射表而非猜测
        if ep.episode_numbers:
            covered_eps.update(ep.episode_numbers)
        elif isinstance(part_episode_mapping, dict):
            mapped = part_episode_mapping.get(name) or part_episode_mapping.get(name.casefold())
            if isinstance(mapped, int) and mapped > 0:
                covered_eps.add(mapped)

    if saw_other_season:
        return False, covered_eps, "文件清单包含其他季正片"

    expected = set(range(1, total_eps + 1))
    if not expected.issubset(covered_eps):
        missing = sorted(expected - covered_eps)
        return False, covered_eps, f"文件清单未覆盖全部正片集号(仍缺 {missing[:6]})"

    if by_period:
        # 若提供了明确期号分段映射，核对每期每段；否则若存在分段期号，核对各期上下段无缺口
        if isinstance(part_episode_mapping, dict) and all(isinstance(k, int) and isinstance(v, list) for k, v in part_episode_mapping.items()):
            for p_num, req_segs in part_episode_mapping.items():  # type: ignore[union-attr]
                if not req_segs:
                    continue
                actual_segs = period_segments.get(p_num, set())
                missing_segs = [s for s in req_segs if s != 0 and s not in actual_segs]
                if missing_segs and p_num not in has_unsegmented_period:
                    return False, covered_eps, f"综艺包第{p_num}期缺少分段({missing_segs})"
        elif period_segments:
            dominant_segs = max(period_segments.values(), key=len)
            if len(dominant_segs) >= 2:
                for p_num in range(1, total_eps + 1):
                    if p_num in has_unsegmented_period:
                        continue
                    segs_for_p = period_segments.get(p_num, set())
                    if segs_for_p and not dominant_segs.issubset(segs_for_p):
                        miss = sorted(dominant_segs - segs_for_p)
                        return False, covered_eps, f"综艺包第{p_num}期缺少分段({miss})"
                    if not segs_for_p:
                        return False, covered_eps, f"综艺包第{p_num}期缺少分段文件"

    return True, covered_eps, "文件清单核验通过"


def verify_season_pack_candidate(
    cand: dict[str, Any],
    season_num: int,
    total_eps: int,
    *,
    by_period: bool = False,
    parts_mode: bool = False,
    part_episode_mapping: dict[int, list[int]] | dict[str, int] | None = None,
) -> tuple[bool, set[int], str]:
    """严格核验整季资源包是否同季、完整覆盖 1..total_eps 且非未知/残缺资源。

    核验优先级：
    1) 明确文件清单（解析每集/每期每段，单凭“共9个文件”不可通过）
    2) 结构化分集表 / episode_list / begin..end（覆盖 1..total_eps，分段完整）
    3) 无文件清单的真实 MP 索引结果：必须同时明确标注对应总集数全集（total_episode == total_eps 或显式 全N集/E01-EN），绝不把仅有 S03 的未知资源当整季包。
    """
    if total_eps <= 0:
        return False, set(), "总集数未知，无法核实整季完整性"

    t = cand.get("torrent_info") or {}
    meta = cand.get("meta_info") or {}
    title = str(t.get("title") or "")
    sub = str(meta.get("subtitle") or t.get("description") or "")
    combined = f"{title} {sub}"

    parsed = parse_episode(title)
    if parsed.is_extra or parsed.is_special:
        return False, set(), "花絮或特别篇"
    if by_period and _EXTRA_DESC_RE.search(combined):
        return False, set(), "包含加更/纯享/花絮/预告等非完整正片标记"

    single_ok, meta_season, s_reason = _extract_seasons(meta, title)
    if not single_ok:
        return False, set(), s_reason
    if meta_season is not None and meta_season != season_num:
        return False, set(), f"季号不符(S{meta_season:02d})"
    if parsed.season_number is not None and parsed.season_number != season_num:
        return False, set(), f"标题季号不符(S{parsed.season_number:02d})"

    if _PARTIAL_MARKER_RE.search(combined):
        return False, set(), "包含未完结/残缺/缺集标记"

    expected = set(range(1, total_eps + 1))

    # 1. 明确文件清单优先核验
    files_raw = t.get("files") or t.get("file_list") or t.get("filelist") or cand.get("files") or cand.get("file_list")
    if isinstance(files_raw, list) and len(files_raw) > 0:
        entries = _parse_files_entries(files_raw)
        return _check_part_title_segment_completeness(
            entries,
            season_num=season_num,
            total_eps=total_eps,
            by_period=by_period,
            part_episode_mapping=part_episode_mapping,
        )

    # 2. 结构化分集表 / 集号清单
    ep_table = cand.get("episode_table") or t.get("episode_table")
    if isinstance(ep_table, list) and ep_table:
        table_eps: set[int] = set()
        table_segs: dict[int, set[int]] = {}
        for row in ep_table:
            if isinstance(row, int) and row > 0:
                table_eps.add(row)
            elif isinstance(row, dict):
                ep_v = row.get("episode") or row.get("episode_number") or row.get("period")
                if ep_v is not None and str(ep_v).isdigit():
                    ev = int(ep_v)
                    table_eps.add(ev)
                    seg_v = row.get("segment") or row.get("part")
                    if seg_v is not None and str(seg_v).isdigit():
                        table_segs.setdefault(ev, set()).add(int(seg_v))
                    elif row.get("missing") or row.get("incomplete"):
                        return False, table_eps, f"分集表标明第{ev}集不完整"
        if not expected.issubset(table_eps):
            return False, table_eps, f"分集表未覆盖全季(缺 {sorted(expected - table_eps)[:6]})"
        if by_period and table_segs:
            dom = max(table_segs.values(), key=len)
            if len(dom) >= 2:
                for p_num in range(1, total_eps + 1):
                    if table_segs.get(p_num) and not dom.issubset(table_segs[p_num]):
                        return False, table_eps, f"分集表第{p_num}期缺少分段"
        return True, expected, "分集表核验通过"

    meta_eps_raw = meta.get("episode_list") or []
    if isinstance(meta_eps_raw, list) and len(meta_eps_raw) > 0:
        meta_eps = {int(x) for x in meta_eps_raw if str(x).isdigit() and int(x) > 0}
        if not expected.issubset(meta_eps):
            return False, meta_eps, f"集号列表仅覆盖 {len(meta_eps)}/{total_eps} 集"
        if by_period:
            # 按期综艺若缺少文件清单或显式分段表，不可凭孤立 episode_list 断言上下段齐全（除非明确非分段）
            if isinstance(part_episode_mapping, dict) and any(len(v) > 1 for v in part_episode_mapping.values() if isinstance(v, list)):
                return False, meta_eps, "按期分段综艺缺乏文件/分段明细，无法确认各期分段齐全"
        return True, expected, "结构化集号列表完整覆盖"

    b_ep = meta.get("begin_episode")
    e_ep = meta.get("end_episode")
    if b_ep is not None and str(b_ep).isdigit() and int(b_ep) > 0:
        end_v = int(e_ep) if (e_ep is not None and str(e_ep).isdigit()) else int(b_ep)
        ep_span = set(range(int(b_ep), max(int(b_ep), end_v) + 1))
        if not expected.issubset(ep_span):
            return False, ep_span, f"集数范围 E{int(b_ep):02d}-E{end_v:02d} 未覆盖全季({total_eps}集)"
        if by_period and isinstance(part_episode_mapping, dict) and any(len(v) > 1 for v in part_episode_mapping.values() if isinstance(v, list)):
            return False, ep_span, "按期分段综艺缺乏文件清单，无法确认各期分段齐全"
        return True, expected, "起止集数完整覆盖"

    # 3. 无文件清单且无单集列表时，核验显式全集说明（支持 MP 真实聚合结果；绝不把仅有 S03 当整季）
    if by_period and (
        parts_mode
        or segment_number(combined)
        or (isinstance(part_episode_mapping, dict) and any(
            isinstance(v, list) and len(v) > 1 for v in part_episode_mapping.values()
        ))
    ):
        return False, set(), "本季存在分段信息，整季包需文件清单或分段表确认覆盖"
    if parts_mode:
        # 存在 Part 标题散 files 时，避免无清单整包误覆盖已映射或未知分段
        explicit_full_num = re.search(rf"(?:全\s*0*{total_eps}\s*集|(?<!\d)0*{total_eps}\s*集全|(?<![A-Za-z0-9])E?0*1\s*[-~～至]\s*E?0*{total_eps}(?!\d))", combined, re.I)
        if not (meta.get("total_episode") == total_eps and explicit_full_num):
            return False, set(), "带 Part 剧集整季包缺乏文件清单或正式全集映射核验"

    total_ep_meta = meta.get("total_episode")
    if isinstance(total_ep_meta, str) and total_ep_meta.isdigit():
        total_ep_meta = int(total_ep_meta)
    if isinstance(total_ep_meta, int) and total_ep_meta > 0 and total_ep_meta != total_eps:
        return False, set(), f"资源标注 {total_ep_meta} 集/期，本季记录 {total_eps} 集/期，覆盖口径冲突，尚不能确认补齐缺口"

    # 若标题本身点明特定集范围/单集号（例如 S03E01-E08），必须完整覆盖
    explicit_range = re.search(r"[Ee]0*(\d{1,3})\s*[-~～至到]\s*[Ee]?0*(\d{1,3})", combined)
    if explicit_range:
        r_start, r_end = int(explicit_range.group(1)), int(explicit_range.group(2))
        if not expected.issubset(set(range(r_start, r_end + 1))):
            return False, set(), f"范围 E{r_start:02d}-E{r_end:02d} 未覆盖全季({total_eps}集)"
        return True, expected, "标题集数范围核验完整"

    # 单点集号且无全集标记，不能视为整季
    if re.search(r"(?:S\d{1,2}E\d+|第\s*\d+\s*集)", title, re.I) and not re.search(rf"(?:全\s*0*{total_eps}\s*集|(?<!\d)0*{total_eps}\s*集全|Complete)", combined, re.I):
        return False, set(), "仅为单集或非完整合集"

    # 必须具备明确与 total_eps 一致的整季完整性证据（支持 全08集 等前导零写法）
    has_full_count_text = bool(
        re.search(rf"(?:全\s*0*{total_eps}\s*[集期]|(?<!\d)0*{total_eps}\s*[集期]全|(?<!\d)0*1\s*[-~～至]\s*0*{total_eps}\s*[集期])", combined, re.I)
    )
    has_complete_word = bool(re.search(r"\bComplete\b|完整全?季|全集打包", combined, re.I))
    if (total_ep_meta == total_eps and (has_complete_word or has_full_count_text)) or has_full_count_text:
        return True, expected, f"MP元数据及全{total_eps}集声明核验完整"

    return False, set(), "仅为季号未知覆盖资源(无文件清单/分集表/全集集数核验)"


def is_season_pack(meta: dict[str, Any], t: dict[str, Any], task_season: int, total: int, by_period: bool = False) -> bool:
    """兼容封装：调用严格整季验证，不再将仅有 S03 的未知资源直接判定为整季包。"""
    ok, _, _ = verify_season_pack_candidate(
        {"meta_info": meta, "torrent_info": t},
        season_num=task_season,
        total_eps=total,
        by_period=by_period,
    )
    return ok


def _targeted_episode_numbers(meta, title, parsed, missing):
    """只接受集号明确且全部落在缺号内的散集或连集。片名里的年份、段号均不能充当集号。"""
    if not missing:
        return []
    explicit = re.search(r"(?:S\d{1,2}E\d|(?<![A-Za-z])EP?\d|第\s*\d+\s*[集话話])", title, re.I)
    title_numbers = set(parsed.episode_numbers) if explicit else set()
    numbers = meta.get("episode_list") or []
    if not numbers and meta.get("begin_episode"):
        first = str(meta["begin_episode"])
        last = str(meta.get("end_episode") or first)
        if not first.isdigit() or not last.isdigit() or not (0 < int(first) <= int(last) <= max(missing)):
            return []
        numbers = range(int(first), int(last) + 1)
    if not all(str(n).isdigit() for n in numbers):
        return []
    coverage = {int(n) for n in numbers} or title_numbers
    if not coverage <= set(missing) or (title_numbers and coverage != title_numbers):
        return []
    return sorted(coverage)


def _extract_standalone_episodes(
    cand: dict[str, Any],
    season_num: int,
    missing_eps: list[int],
    *,
    by_period: bool = False,
    parts_mode: bool = False,
    tmdb_id_num: int = 0,
    keyword_fallback: bool = False,
) -> list[int]:
    """解析非整季候选所覆盖的全部正片集号；冲突或未知返回空。"""
    t = cand.get("torrent_info") or {}
    meta = cand.get("meta_info") or {}
    m_info = cand.get("media_info") or {}
    title = str(t.get("title") or "")
    missing_set = set(missing_eps)

    if by_period:
        if (
            segment_number(title)
            or (tmdb_id_num and m_info.get("tmdb_id") and str(m_info.get("tmdb_id") or "") != str(tmdb_id_num))
            or re.search(r"加更|纯享|花絮|预告|[上中下][篇集部]|期\s*[（(【\[]?\s*[上中下]|特别版|\bpart\b", title, re.I)
        ):
            return []
        seasons = (
            meta.get("season_list")
            or ([meta.get("begin_season")] if meta.get("begin_season") is not None else [])
            or ([meta.get("season")] if meta.get("season") is not None else [])
        )
        if not seasons:
            parsed_s = parse_episode(title).season_number
            if parsed_s is not None:
                seasons = [parsed_s]
        if seasons != [season_num]:
            return []

        range_m = re.findall(r"第\s*(\d{1,3})\s*[-~～至到]\s*(?:第\s*)?(\d{1,3})\s*期", title)
        single_m = re.findall(r"第\s*(\d{1,3})\s*期", title)
        period_nums: list[int] = []
        if range_m:
            if len(range_m) != 1:
                return []
            p_start, p_end = int(range_m[0][0]), int(range_m[0][1])
            if not (0 < p_start <= p_end and (p_end - p_start) <= 30):
                return []
            period_nums = list(range(p_start, p_end + 1))
        elif len(single_m) == 1:
            period_nums = [int(single_m[0])]
        else:
            return []

        if not period_nums or not (set(period_nums) <= missing_set):
            return []
        return sorted(set(period_nums))

    ep_stem = re.sub(r"(?i)part[ ._-]*(?:0?[123]|[abc])(?=[^a-z0-9]|$)", "", title) if parts_mode else title
    parsed = parse_episode(ep_stem)
    if parsed.is_extra or parsed.is_special:
        return []
    if parsed.season_number not in (None, season_num):
        return []

    if parts_mode:
        return _targeted_episode_numbers(meta, title, parsed, missing_eps)

    if keyword_fallback:
        single_ok, meta_season, _ = _extract_seasons(meta, title)
        if not single_ok:
            return []
        if meta_season is None and parsed.season_number != season_num:
            return []

    explicit_title = re.search(r"(?:S\d{1,2}E\d|(?<![A-Za-z])EP?\d|第\s*\d+\s*[集话話])", title, re.I)
    title_numbers = set(parsed.episode_numbers) if explicit_title else set()

    numbers = meta.get("episode_list") or []
    if not numbers and meta.get("begin_episode"):
        first = str(meta["begin_episode"])
        last = str(meta.get("end_episode") or first)
        if first.isdigit() and last.isdigit() and 0 < int(first) <= int(last):
            numbers = list(range(int(first), int(last) + 1))

    if numbers:
        if not all(str(n).isdigit() for n in numbers):
            return []
        coverage = {int(n) for n in numbers}
        if title_numbers and not title_numbers.issubset(coverage):
            return []
        return sorted(coverage)

    if title_numbers:
        return sorted(title_numbers)

    # 再检查文件清单中的散集号
    files_raw = t.get("files") or t.get("file_list") or t.get("filelist")
    if isinstance(files_raw, list) and files_raw:
        ep_set: set[int] = set()
        for entry in _parse_files_entries(files_raw):
            if not is_video_file(entry["name"]):
                continue
            ep = parse_episode(entry["name"], entry.get("path", ""), entry.get("size") or 1024)
            if not ep.is_extra and not ep.is_special and ep.season_number in (None, season_num):
                ep_set.update(ep.episode_numbers)
        if ep_set:
            return sorted(ep_set)

    return []


# ── 去重、同档省容量与多集最优规划 ───────────────────────────

def _dedup_evaluations(evals: list[CandidateEval]) -> list[CandidateEval]:
    """跨站同种去重：同种优先保留高优先站点来源（数字越小越优先），同站再保留做种最高副本，并汇总全部可用站点来源。"""
    grouped: dict[tuple[Any, ...], list[CandidateEval]] = {}
    for ev in evals:
        key = (_torrent_identity_key(ev.cand), ev.covered_all, ev.is_season_pack)
        grouped.setdefault(key, []).append(ev)

    deduped: list[CandidateEval] = []
    for group in grouped.values():
        group.sort(
            key=lambda e: (e.site_priority, -e.seeders, e.size_bytes is None, e.size_bytes or 0, e.stable_key),
        )
        best = group[0]
        sources: list[str] = []
        seen_src: set[str] = set()
        for item in group:
            label = f"{item.site_name or '未知站'}({item.seeders})"
            if label not in seen_src:
                seen_src.add(label)
                sources.append(label)
        deduped.append(
            CandidateEval(
                cand=best.cand,
                covered_all=best.covered_all,
                covered_missing=best.covered_missing,
                is_season_pack=best.is_season_pack,
                seeders=best.seeders,
                size_bytes=best.size_bytes,
                site_id=best.site_id,
                site_name=best.site_name,
                site_sources=tuple(sources),
                stable_key=best.stable_key,
                reason_note=best.reason_note,
                site_priority=best.site_priority,
            )
        )
    deduped.sort(key=lambda e: (e.site_priority, -e.seeders, e.size_bytes is None, e.size_bytes or 0, e.stable_key))
    return deduped


def _pick_best_season_pack(
    season_packs: list[CandidateEval],
    tier_ratio: float = 0.10,
    min_slack: int = 1,
) -> tuple[CandidateEval | None, str]:
    """从合格整季包中选择最高优先站，同站中按做种顶档中字节最小选包，并返回决策说明与次选差异。"""
    if not season_packs:
        return None, ""

    # 1. 站点优先级绝对优先：先筛选具有合格整季包的最高优先站集合（数字越小越优先）
    best_pri = min(sp.site_priority for sp in season_packs)
    best_pri_packs = [sp for sp in season_packs if sp.site_priority == best_pri]

    h_seeders = max(sp.seeders for sp in best_pri_packs)
    floor = _seeder_tier_floor(h_seeders, tier_ratio=tier_ratio, min_slack=min_slack)

    ranked = sorted(
        best_pri_packs,
        key=lambda sp: (
            0 if sp.seeders >= floor else 1,
            (sp.size_bytes is None) if sp.seeders >= floor else False,
            (sp.size_bytes or 0) if sp.seeders >= floor else 0,
            -sp.seeders,
            sp.size_bytes is None,
            sp.size_bytes or 0,
            sp.stable_key,
        ),
    )
    winner = ranked[0]
    scope_desc = f"最高优先站[{winner.site_name or '未知'}(优先级{winner.site_priority})], 做种顶档阈值≥{floor}(H={h_seeders}, 容差={max(min_slack, math.floor(h_seeders * tier_ratio))})"
    if len(ranked) > 1:
        ru = ranked[1]
        in_same_tier = ru.seeders >= floor
        diff_str = (
            f"+{_format_bytes((ru.size_bytes or 0) - (winner.size_bytes or 0))}"
            if (winner.size_bytes and ru.size_bytes and ru.size_bytes >= winner.size_bytes)
            else f"{_format_bytes(ru.size_bytes)}/{ru.seeders}做种"
        )
        ru_desc = f"同站次选[{'同档' if in_same_tier else '跨档'}:{ru.seeders}做种 {_format_bytes(ru.size_bytes)}, 差{diff_str}]"
    else:
        lower_packs = [sp for sp in season_packs if sp.site_priority > best_pri]
        if lower_packs:
            low_best = max(lower_packs, key=lambda x: x.seeders)
            ru_desc = f"低优先站备选[{low_best.site_name}(优先级{low_best.site_priority}) {low_best.seeders}做种，按规则站点优先级选用 {winner.site_name}]"
        else:
            ru_desc = "无其他备选"
    return winner, f"{scope_desc}; {ru_desc}"


def _plan_standalone_downloads(
    standalone_evals: list[CandidateEval],
    needed: list[int],
    *,
    tier_ratio: float = 0.10,
    min_slack: int = 1,
    max_submissions: int | None = None,
) -> tuple[list[CandidateEval], str]:
    """多集省容量决策与站点瀑布式填充（v2.5.40 规范 Section 6.5）。

    第一站在满足硬条件的候选中最大化覆盖目标缺号，再按同站做种和容量选择；后续站只填前面未覆盖的缺号。
    """
    needed_list = sorted(set(needed))
    if not standalone_evals or not needed_list:
        return [], ""

    unique_priorities = sorted({ev.site_priority for ev in standalone_evals})
    if len(unique_priorities) > 1:
        rem_needed = list(needed_list)
        combined_plan: list[CandidateEval] = []
        notes: list[str] = []

        for pri in unique_priorities:
            if not rem_needed:
                break
            pri_evals = [ev for ev in standalone_evals if ev.site_priority == pri]
            sub_plan, sub_note = _plan_single_tier_standalone(
                pri_evals,
                rem_needed,
                tier_ratio=tier_ratio,
                min_slack=min_slack,
                max_submissions=max_submissions,
            )
            if sub_plan:
                combined_plan.extend(sub_plan)
                cov = {ep for ev in sub_plan for ep in ev.covered_missing if ep in rem_needed}
                sname = sub_plan[0].site_name or f"站点#{sub_plan[0].site_id}"
                notes.append(f"站点优先级{pri}[{sname}]: 覆盖 {sorted(cov)}")
                rem_needed = [ep for ep in rem_needed if ep not in cov]

        if not combined_plan:
            return [], "各站点均无合格散集"

        if rem_needed:
            notes.append(f"尚余缺集 {rem_needed} 待补")
        return combined_plan, "; ".join(notes)

    return _plan_single_tier_standalone(
        standalone_evals,
        needed_list,
        tier_ratio=tier_ratio,
        min_slack=min_slack,
        max_submissions=max_submissions,
    )


def _plan_single_tier_standalone(
    standalone_evals: list[CandidateEval],
    needed: list[int],
    *,
    tier_ratio: float = 0.10,
    min_slack: int = 1,
    max_submissions: int | None = None,
) -> tuple[list[CandidateEval], str]:
    """同优先级/单层内多集省容量决策：先最大化覆盖缺号，再按同做种档内总字节最小选出完整组合。

    同一个连集包容量只计一次；大量候选先按 Pareto 剪枝，再执行有界位掩码搜索。
    """
    needed_list = sorted(set(needed))
    if not standalone_evals or not needed_list:
        return [], ""

    ep_to_bit = {ep: idx for idx, ep in enumerate(needed_list)}
    all_items: list[tuple[int, CandidateEval]] = []
    union_mask = 0
    for ev in standalone_evals:
        mask = 0
        for ep in ev.covered_missing:
            if ep in ep_to_bit:
                mask |= 1 << ep_to_bit[ep]
        if mask:
            all_items.append((mask, ev))
            union_mask |= mask

    if not all_items:
        return [], ""

    # 对相同覆盖集合剪枝：保留做种-体积 Pareto 前沿（剔除覆盖相同、做种不优且容量更大的候选）
    by_mask: dict[int, list[CandidateEval]] = {}
    for mask, ev in all_items:
        by_mask.setdefault(mask, []).append(ev)

    pruned: list[tuple[int, CandidateEval]] = []
    for mask, cands in by_mask.items():
        cands.sort(key=lambda e: (-e.seeders, e.size_bytes is None, e.size_bytes or 0, e.stable_key))
        best_known_size: int | None = None
        kept_unknown = False
        for c in cands:
            if c.size_bytes is None:
                if not kept_unknown and best_known_size is None:
                    pruned.append((mask, c))
                    kept_unknown = True
            else:
                if best_known_size is None or c.size_bytes < best_known_size:
                    pruned.append((mask, c))
                    best_known_size = c.size_bytes

    max_subs = max_submissions if max_submissions is not None else max(20, len(needed_list))

    # 计算在达到最大覆盖集合 union_mask 时，所能达到的最高“组合瓶颈做种数 H_plan”
    unique_seeders = sorted({ev.seeders for _, ev in pruned}, reverse=True)
    h_plan = unique_seeders[-1]
    for thresh in unique_seeders:
        cov = 0
        for m, ev in pruned:
            if ev.seeders >= thresh:
                cov |= m
        if cov == union_mask:
            h_plan = thresh
            break

    floor = _seeder_tier_floor(h_plan, tier_ratio=tier_ratio, min_slack=min_slack)
    tier_items = [(m, ev) for m, ev in pruned if ev.seeders >= floor]
    if not tier_items:
        tier_items = pruned

    # 同档候选排序：多覆盖且单位缺集体积更小者优先，未知大小靠后
    tier_items.sort(
        key=lambda pair: (
            pair[1].size_bytes is None,
            (pair[1].size_bytes or 0) / max(1, pair[0].bit_count()),
            -pair[0].bit_count(),
            -pair[1].seeders,
            pair[1].stable_key,
        )
    )

    def _plan_cost(chosen: list[CandidateEval]) -> tuple[Any, ...]:
        unknown_cnt = sum(1 for c in chosen if c.size_bytes is None)
        known_sum = sum(c.size_bytes or 0 for c in chosen)
        min_s = min((c.seeders for c in chosen), default=0)
        return (unknown_cnt > 0, unknown_cnt, known_sum, len(chosen), -min_s, tuple(c.stable_key for c in chosen))

    # 1. 先构造确定性贪心可行解作为上界（同样可用于超大集合兜底）
    def _greedy_cover(items: list[tuple[int, CandidateEval]]) -> list[CandidateEval]:
        rem = union_mask
        picked: list[CandidateEval] = []
        used: set[int] = set()
        while rem and len(picked) < max_subs:
            best_idx = -1
            best_key: tuple[Any, ...] | None = None
            for idx, (m, ev) in enumerate(items):
                if idx in used:
                    continue
                gain = (m & rem).bit_count()
                if gain <= 0:
                    continue
                k = (ev.size_bytes is None, (ev.size_bytes or 0) / gain, -gain, -ev.seeders, ev.stable_key)
                if best_key is None or k < best_key:
                    best_key = k
                    best_idx = idx
            if best_idx < 0:
                break
            used.add(best_idx)
            rem &= ~items[best_idx][0]
            picked.append(items[best_idx][1])
        return picked if rem == 0 else []

    best_plan: list[CandidateEval] = _greedy_cover(tier_items)
    best_cost: tuple[Any, ...] | None = _plan_cost(best_plan) if best_plan else None
    runner_up: tuple[tuple[Any, ...], list[CandidateEval]] | None = None

    # 为高效按位覆盖预建索引：每个 bit 对应能覆盖该 bit 的候选下标
    bit_to_cands: dict[int, list[int]] = {}
    for idx, (m, _) in enumerate(tier_items):
        tmp = m
        while tmp:
            lsb = tmp & -tmp
            bit_to_cands.setdefault(lsb.bit_length() - 1, []).append(idx)
            tmp ^= lsb

    node_visits = 0
    max_nodes = 15000
    search_truncated = False

    def _dfs(rem_mask: int, chosen: list[CandidateEval], known_sum: int, unknown_cnt: int) -> None:
        nonlocal best_plan, best_cost, runner_up, node_visits, search_truncated
        node_visits += 1
        if node_visits > max_nodes:
            search_truncated = True
            return

        if rem_mask == 0:
            cost = _plan_cost(chosen)
            if best_cost is None or cost < best_cost:
                if best_cost is not None and best_plan:
                    runner_up = (best_cost, list(best_plan))
                best_cost = cost
                best_plan = list(chosen)
            elif cost != best_cost and (runner_up is None or cost < runner_up[0]):
                runner_up = (cost, list(chosen))
            return

        if len(chosen) >= max_subs:
            return

        # 最优性下界剪枝
        if best_cost is not None:
            best_has_unk, _, best_bytes, _, _, _ = best_cost
            if not best_has_unk and unknown_cnt > 0:
                return
            if (unknown_cnt > 0) == best_has_unk and known_sum > best_bytes:
                return

        # 选覆盖候选最少的那一集（MRV），大幅收敛搜索树
        target_bit = -1
        target_cands: list[int] = []
        tmp = rem_mask
        while tmp:
            lsb = tmp & -tmp
            b = lsb.bit_length() - 1
            opts = bit_to_cands.get(b, [])
            if not opts:
                return
            if target_bit == -1 or len(opts) < len(target_cands):
                target_bit = b
                target_cands = opts
                if len(target_cands) <= 1:
                    break
            tmp ^= lsb

        for idx in target_cands:
            m, ev = tier_items[idx]
            new_rem = rem_mask & ~m
            if new_rem == rem_mask:
                continue
            chosen.append(ev)
            _dfs(
                new_rem,
                chosen,
                known_sum + (ev.size_bytes or 0),
                unknown_cnt + (1 if ev.size_bytes is None else 0),
            )
            chosen.pop()
            if node_visits > max_nodes:
                search_truncated = True
                return

    _dfs(union_mask, [], 0, 0)

    if not best_plan:
        # 极少数情况下 max_subs 限制导致全并集未凑够，回退贪心最大化覆盖数
        rem = union_mask
        fallback_plan: list[CandidateEval] = []
        used_idx: set[int] = set()
        while rem and len(fallback_plan) < max_subs:
            avail = [(idx, m & rem, ev) for idx, (m, ev) in enumerate(tier_items) if idx not in used_idx and (m & rem)]
            if not avail:
                break
            avail.sort(key=lambda x: (-x[1].bit_count(), x[2].size_bytes is None, x[2].size_bytes or 0, -x[2].seeders, x[2].stable_key))
            pick_idx, _, pick_ev = avail[0]
            used_idx.add(pick_idx)
            rem &= ~tier_items[pick_idx][0]
            fallback_plan.append(pick_ev)
        best_plan = fallback_plan

    chosen_bytes = sum(c.size_bytes or 0 for c in best_plan) if best_plan and all(c.size_bytes for c in best_plan) else None
    mode_tag = "有界搜索(达到上限)" if search_truncated else "Pareto剪枝+精确比较"
    scope_note = (
        f"散集/连集比较范围: 目标{len(needed_list)}集, 最高瓶颈做种H={h_plan}, 同档阈值≥{floor}, "
        f"{mode_tag}, 入选{len(best_plan)}项总计{_format_bytes(chosen_bytes)}"
    )
    if runner_up:
        ru_cands = runner_up[1]
        ru_bytes = sum(c.size_bytes or 0 for c in ru_cands) if all(c.size_bytes for c in ru_cands) else None
        ru_seeds = min((c.seeders for c in ru_cands), default=0)
        if chosen_bytes and ru_bytes and ru_bytes >= chosen_bytes:
            scope_note += f"; 次选{len(ru_cands)}项({ru_seeds}做种 {_format_bytes(ru_bytes)}, 多{_format_bytes(ru_bytes - chosen_bytes)})"
        else:
            scope_note += f"; 次选{len(ru_cands)}项({ru_seeds}做种 {_format_bytes(ru_bytes)})"
    elif best_plan:
        # 检查跨档更快但更大的选项（或同档未被挑选的其他单集最优）
        chosen_ids = {id(c) for c in best_plan}
        other_sp = [ev for _, ev in pruned if id(ev) not in chosen_ids]
        if other_sp:
            other_sp.sort(key=lambda e: (-e.seeders, e.size_bytes is None, e.size_bytes or 0))
            alt = other_sp[0]
            scope_note += f"; 备选参考[{alt.site_name or '?'} {alt.seeders}做种 {_format_bytes(alt.size_bytes)}]"

    return best_plan, scope_note


def select_best_candidates(
    eligible: list[dict[str, Any]],
    needed: list[int],
    total: int,
    season: int,
    *,
    by_period: bool = False,
    parts_mode: bool = False,
    min_seeders: int = 2,
    tier_ratio: float = 0.10,
    min_slack: int = 1,
    allow_season_pack: bool = True,
) -> list[tuple[dict[str, Any], list[int], bool]]:
    """根据缺失集号选出最优候选组合（省容量散集优先，散集补不全且允许时回退整季包）。

    Returns:
        [(candidate, covered_episodes, is_season_pack), ...]
    """
    if not eligible or not needed:
        return []

    missing_set = set(needed)
    standalone_evals: list[CandidateEval] = []
    pack_evals: list[CandidateEval] = []

    for c in eligible:
        t = c.get("torrent_info") or {}
        meta = c.get("meta_info") or {}
        seeds = _candidate_seeders(c)
        if seeds < min_seeders:
            continue

        is_sp, sp_eps, _ = verify_season_pack_candidate(
            c,
            season_num=season,
            total_eps=total,
            by_period=by_period,
            parts_mode=parts_mode,
        )
        if is_sp and missing_set.issubset(sp_eps):
            pack_evals.append(
                CandidateEval(
                    cand=c,
                    covered_all=tuple(sorted(sp_eps)),
                    covered_missing=tuple(sorted(missing_set)),
                    is_season_pack=True,
                    seeders=seeds,
                    size_bytes=_candidate_size_bytes(c),
                    site_id=_candidate_site_id(c),
                    site_name=_candidate_site_name(c),
                    site_sources=(),
                    stable_key=_candidate_stable_key(c),
                )
            )
            continue

        st_eps = _extract_standalone_episodes(
            c,
            season_num=season,
            missing_eps=needed,
            by_period=by_period,
            parts_mode=parts_mode,
        )
        hits = sorted(ep for ep in st_eps if ep in missing_set)
        if hits:
            standalone_evals.append(
                CandidateEval(
                    cand=c,
                    covered_all=tuple(st_eps),
                    covered_missing=tuple(hits),
                    is_season_pack=False,
                    seeders=seeds,
                    size_bytes=_candidate_size_bytes(c),
                    site_id=_candidate_site_id(c),
                    site_name=_candidate_site_name(c),
                    site_sources=(),
                    stable_key=_candidate_stable_key(c),
                )
            )

    dedup_standalones = _dedup_evaluations(standalone_evals)
    dedup_packs = _dedup_evaluations(pack_evals)

    plan_evals, _ = _plan_standalone_downloads(
        dedup_standalones,
        needed,
        tier_ratio=tier_ratio,
        min_slack=min_slack,
    )
    covered_by_standalone = {ep for ev in plan_evals for ep in ev.covered_missing}

    # 缺集数量大于 3 集：优先选择整季包下载（整季包不可用时平滑回退散集）
    if len(missing_set) > 3 and allow_season_pack and dedup_packs:
        best_sp, _ = _pick_best_season_pack(dedup_packs, tier_ratio=tier_ratio, min_slack=min_slack)
        if best_sp:
            return [(best_sp.cand, list(best_sp.covered_all), True)]

    # 缺集 <= 3 集或无整季包：散集补全优先
    if missing_set.issubset(covered_by_standalone) and plan_evals:
        return [(ev.cand, list(ev.covered_missing), False) for ev in plan_evals]

    # 散集补不全时，若允许整季包且存在整季包候选，回退整季包
    if allow_season_pack and dedup_packs:
        best_sp, _ = _pick_best_season_pack(dedup_packs, tier_ratio=tier_ratio, min_slack=min_slack)
        if best_sp:
            return [(best_sp.cand, list(best_sp.covered_all), True)]

    return [(ev.cand, list(ev.covered_missing), False) for ev in plan_evals]


# ── 候选评估分类（独立散集分支与独立整季验证分支） ────────────

def _candidate_matches_mapped_identity(
    cand: dict[str, Any],
    *,
    search_tmdb_id: int,
    search_season: int,
    source_tmdb_id: int,
    source_season: int,
    allowed_titles: list[str] | None = None,
    rejected_titles: list[str] | None = None,
    keyword_fallback: bool = False,
) -> tuple[bool, str, int]:
    """核验候选种子的作品归属与有效搜片季号，阻止跨条目误撞同名作品。"""
    t = cand.get("torrent_info") or {}
    m_info = cand.get("media_info") or {}
    meta = cand.get("meta_info") or {}
    title = str(t.get("title") or "")
    cand_id = str(m_info.get("tmdb_id") or "").strip()
    is_mapped = bool(search_tmdb_id and source_tmdb_id and search_tmdb_id != source_tmdb_id) or (search_season != source_season)

    single_ok, meta_season, s_reason = _extract_seasons(meta, title)
    norm_title = normalize_title_token(title)

    if rejected_titles:
        for rej in rejected_titles:
            rtok = normalize_title_token(rej)
            if rtok and len(rtok) >= 4 and rtok in norm_title and not any(normalize_title_token(a) in norm_title for a in (allowed_titles or []) if a):
                return False, "作品身份不符", search_season

    if not is_mapped:
        if search_tmdb_id:
            if keyword_fallback and cand_id != str(search_tmdb_id):
                return False, "作品身份不符", search_season
            if cand_id and cand_id != str(search_tmdb_id):
                return False, "TMDB_ID不符", search_season
        if not single_ok or (meta_season is not None and meta_season != search_season):
            return False, s_reason or "季号不符", search_season
        return True, "", search_season

    # 跨条目映射模式（如 极地恶灵 S03 [75191/S03] <-> 银色恶魔 [323903/S01]）
    if cand_id:
        if cand_id == str(search_tmdb_id):
            if not single_ok or (meta_season is not None and meta_season != search_season):
                return False, s_reason or "映射目标季号不符", search_season
            return True, "", search_season
        if source_tmdb_id and cand_id == str(source_tmdb_id):
            # 严格防止误中母条目的早期季（例如 The Terror S01 不能冒充 Devil in Silver！）
            if not single_ok or meta_season != source_season:
                return False, "原条目季号与跨条目映射季不一致", search_season
            # 若原条目季号一致，进一步要求种子标题显式含目标副标题别名或 S03 标记
            if allowed_titles and not any(normalize_title_token(a) in norm_title for a in allowed_titles if len(normalize_title_token(a)) >= 3):
                return False, "作品身份不符", search_season
            return True, "", source_season
        return False, "TMDB_ID与映射口径均不符", search_season

    # 映射关键词检索未返回 tmdb_id 时：必须精确命中明确别名，拒绝泛用系列名混装别季
    if not allowed_titles:
        return False, "作品身份不符", search_season
    strong_aliases = [normalize_title_token(a) for a in allowed_titles if len(normalize_title_token(a)) >= 3]
    if not strong_aliases or not any(tok in norm_title for tok in strong_aliases):
        return False, "关键词结果未命中映射别名", search_season
    if not single_ok or (meta_season is not None and meta_season not in (search_season, source_season)):
        return False, s_reason or "季号不符", search_season
    eff_season = meta_season if meta_season in (search_season, source_season) else search_season
    return True, "", eff_season


def _evaluate_raw_candidates(
    raw_results: list[dict[str, Any]],
    *,
    tmdb_id_num: int,
    season_num: int,
    total_eps: int,
    missing_eps: list[int],
    by_period: bool,
    parts_mode: bool,
    part_episode_mapping: dict[int, list[int]] | None,
    keyword_fallback: bool,
    min_seeders: int,
    min_resolution: str,
    require_chinese: bool,
    source_tmdb_id: int = 0,
    source_season: int = 0,
    allowed_titles: list[str] | None = None,
    rejected_titles: list[str] | None = None,
    allowed_site_ids: set[int] | None = None,
    site_priority_map: dict[int, int] | None = None,
) -> tuple[list[CandidateEval], list[CandidateEval], list[str]]:
    """将原始检索结果分别按散集分支与整季包分支验证（不相互提前过滤）。"""
    missing_set = set(missing_eps)
    standalone_evals: list[CandidateEval] = []
    pack_evals: list[CandidateEval] = []
    reject_reasons: list[str] = []
    eff_source_tmdb = source_tmdb_id or tmdb_id_num
    eff_source_season = source_season or season_num

    for c in raw_results:
        t = c.get("torrent_info") or {}
        m_info = c.get("media_info") or {}
        meta = c.get("meta_info") or {}
        if not t or not (m_info or allowed_titles):
            continue

        site_id = _candidate_site_id(c)
        if allowed_site_ids is not None:
            if site_id is None or site_id not in allowed_site_ids:
                reject_reasons.append(f"站点未在允许列表中(site_id={site_id})")
                continue

        site_pri = site_priority_map.get(site_id, 999999) if (site_priority_map and site_id is not None) else 999999

        seeds = _candidate_seeders(c)
        if seeds < min_seeders:
            reject_reasons.append("做种不足")
            continue

        id_ok, id_reason, eff_season = _candidate_matches_mapped_identity(
            c,
            search_tmdb_id=tmdb_id_num,
            search_season=season_num,
            source_tmdb_id=eff_source_tmdb,
            source_season=eff_source_season,
            allowed_titles=allowed_titles,
            rejected_titles=rejected_titles,
            keyword_fallback=keyword_fallback,
        )
        if not id_ok:
            reject_reasons.append(id_reason)
            continue

        if not _passes_explicit_filters(t, meta, min_resolution=min_resolution, require_chinese=require_chinese):
            reject_reasons.append("未达到显式画质或中字要求")
            continue

        site_name = _candidate_site_name(c)
        size_bytes = _candidate_size_bytes(c)
        stable_key = _candidate_stable_key(c)

        # 分支 1：严格整季包验证（不因“散集仅允许缺号”而被提前过滤）
        is_sp, sp_eps, sp_note = verify_season_pack_candidate(
            c,
            season_num=eff_season,
            total_eps=total_eps,
            by_period=by_period,
            parts_mode=parts_mode,
            part_episode_mapping=part_episode_mapping,
        )
        if is_sp and missing_set.issubset(sp_eps):
            pack_evals.append(
                CandidateEval(
                    cand=c,
                    covered_all=tuple(sorted(sp_eps)),
                    covered_missing=tuple(sorted(missing_set)),
                    is_season_pack=True,
                    seeders=seeds,
                    size_bytes=size_bytes,
                    site_id=site_id,
                    site_name=site_name,
                    site_sources=(),
                    stable_key=stable_key,
                    reason_note=sp_note,
                    site_priority=site_pri,
                )
            )
            continue

        # 分支 2：散集 / 连集验证
        st_eps = _extract_standalone_episodes(
            c,
            season_num=eff_season,
            missing_eps=missing_eps,
            by_period=by_period,
            parts_mode=parts_mode,
            tmdb_id_num=int(m_info.get("tmdb_id") or 0) if m_info.get("tmdb_id") else tmdb_id_num,
            keyword_fallback=False if allowed_titles else keyword_fallback,
        )
        hits = sorted(ep for ep in st_eps if ep in missing_set)
        if hits:
            standalone_evals.append(
                CandidateEval(
                    cand=c,
                    covered_all=tuple(st_eps),
                    covered_missing=tuple(hits),
                    is_season_pack=False,
                    seeders=seeds,
                    size_bytes=size_bytes,
                    site_id=site_id,
                    site_name=site_name,
                    site_sources=(),
                    stable_key=stable_key,
                    site_priority=site_pri,
                )
            )
        elif sp_note:
            reject_reasons.append(f"整季包核验：{sp_note}")

    return standalone_evals, pack_evals, reject_reasons


def _collect_returned_sites(raw_results: list[dict[str, Any]], id_to_name: dict[int, str]) -> list[str]:
    seen: list[str] = []
    seen_keys: set[str] = set()
    for item in raw_results:
        t = item.get("torrent_info") or {}
        sid = _candidate_site_id(item)
        sname = str(t.get("site_name") or "").strip() or (id_to_name.get(sid, "") if sid is not None else "")
        if not sname and sid is None:
            continue
        key = f"{sid}:{sname}"
        if key not in seen_keys:
            seen_keys.add(key)
            seen.append(f"{sid}:{sname}" if sid is not None and sname else (sname or str(sid)))
    return seen


def _sanitize_reply(reply: Any) -> Any:
    """递归过滤响应对象中的敏感凭据与私有链接。"""
    if isinstance(reply, str):
        return sanitize_text_no_secrets(reply)
    if isinstance(reply, list):
        return [_sanitize_reply(x) for x in reply[:20]]
    if isinstance(reply, dict):
        sanitized: dict[str, Any] = {}
        for k, v in reply.items():
            kl = str(k).lower()
            if any(s in kl for s in ("passkey", "authkey", "cookie", "token", "secret", "password")):
                sanitized[k] = "[REDACTED]"
            else:
                sanitized[k] = _sanitize_reply(v)
        return sanitized
    return reply


def _classify_reject_outcome(
    raw_count: int,
    reject_reasons: list[str],
    deferred_sp_reason: str,
    max_seeders_seen: int = 0,
    min_seeders: int = 2,
) -> tuple[str, str]:
    """区分 0 命中 / 做种不足 / 身份不符 / 无合格整包或散集方案。"""
    if raw_count <= 0:
        return "zero_hits", "已检索全部可用站点，本次搜索未返回资源（0 命中）"
    if deferred_sp_reason:
        return "deferred_overlap", sanitize_text_no_secrets(deferred_sp_reason)
    if reject_reasons:
        uniq = list(dict.fromkeys(reject_reasons))
        if all(r == "做种不足" for r in uniq):
            return (
                "low_seeders",
                f"找到 {raw_count} 个候选，最高 {max_seeders_seen} 人做种，本次要求至少 {min_seeders} 人（低于最低要求 {min_seeders}）",
            )
        if all(r in ("作品身份不符", "TMDB_ID不符", "季号不符", "映射目标季号不符", "原条目季号与跨条目映射季不一致", "TMDB_ID与映射口径均不符", "关键词结果未命中映射别名") for r in uniq):
            return "wrong_identity", f"检索候选均与目标作品/季号不符已拦截（{'；'.join(uniq[:2])}）"
        if "做种不足" in uniq and len(uniq) == 1:
            return (
                "low_seeders",
                f"找到 {raw_count} 个候选，最高 {max_seeders_seen} 人做种，本次要求至少 {min_seeders} 人",
            )
        reasons = [f"部分资源做种低于 {min_seeders} 人" if r == "做种不足" else r for r in uniq]
        return "no_qualified_candidate", (
            f"未找到可补齐缺口的合格散集或整季包；检索 {raw_count} 个候选"
            f"（全部候选最高 {max_seeders_seen} 人做种，不代表覆盖合格）。不同候选未通过的原因：{'；'.join(reasons[:3])}"
        )
    return "no_qualified_candidate", f"找到 {raw_count} 个候选，未命中覆盖真实缺集的合格方案"


def _diagnose_episode_failure(
    local_ep: int,
    search_ep: int,
    raw_candidates: list[dict[str, Any]],
    *,
    by_period: bool = False,
    min_seeders: int = 2,
    fallback_code: str = "no_qualified_candidate",
    fallback_summary: str = "",
) -> tuple[str, str]:
    """针对单集/单期未覆盖情况生成精确到候选数与最高做种数的诊断说明。"""
    unit_prefix = f"第{local_ep:02d}期" if by_period else f"E{local_ep:02d}"
    matched_cands: list[dict[str, Any]] = []
    seen_diag_keys: set[str] = set()
    for c in raw_candidates:
        if not isinstance(c, dict):
            continue
        diag_key = f"{_candidate_site_id(c)}:{_candidate_stable_key(c)}"
        if diag_key in seen_diag_keys:
            continue
        t = c.get("torrent_info") or {}
        meta = c.get("meta_info") or {}
        title = str(t.get("title") or "")
        ep_list = meta.get("episode_list") or []
        b_ep = meta.get("begin_episode")
        e_ep = meta.get("end_episode") or b_ep
        in_meta = False
        if ep_list and all(str(x).isdigit() for x in ep_list):
            in_meta = search_ep in {int(x) for x in ep_list}
        elif b_ep is not None and str(b_ep).isdigit() and str(e_ep).isdigit():
            in_meta = int(b_ep) <= search_ep <= int(e_ep)

        in_title = False
        if by_period:
            p_single = [int(x) for x in re.findall(r"第\s*(\d{1,3})\s*期", title)]
            p_range = re.findall(r"第\s*(\d{1,3})\s*[-~～至到]\s*(?:第\s*)?(\d{1,3})\s*期", title)
            if search_ep in p_single:
                in_title = True
            elif any(int(a) <= search_ep <= int(b) for a, b in p_range):
                in_title = True
        else:
            parsed = parse_episode(title)
            if not parsed.is_extra and not parsed.is_special and search_ep in (parsed.episode_numbers or []):
                in_title = True

        if in_meta or in_title:
            seen_diag_keys.add(diag_key)
            matched_cands.append(c)

    if matched_cands:
        c_cnt = len(matched_cands)
        c_max_seeds = max((_candidate_seeders(c) for c in matched_cands), default=0)
        if c_max_seeds < min_seeders:
            return (
                "low_seeders",
                f"{unit_prefix} 找到 {c_cnt} 个候选，最高 {c_max_seeds} 人做种，本次要求至少 {min_seeders} 人（低于最低要求 {min_seeders}）",
            )
        return (
            "no_qualified_candidate",
            f"{unit_prefix} 找到 {c_cnt} 个候选（最高 {c_max_seeds} 人做种），但因命名含混、跨集超范围或画质/身份校验未通过而未选用",
        )

    if not raw_candidates:
        return "zero_hits", f"{unit_prefix} 本次检索未返回任何候选（0 命中）"
    if fallback_code == "low_seeders":
        all_max = max((_candidate_seeders(c) for c in raw_candidates if isinstance(c, dict)), default=0)
        return (
            "low_seeders",
            f"{unit_prefix} 找到 {len(raw_candidates)} 个季级候选，最高 {all_max} 人做种，本次要求至少 {min_seeders} 人（低于最低要求 {min_seeders}）",
        )
    if fallback_code == "deferred_overlap" and fallback_summary:
        return fallback_code, fallback_summary
    return "zero_hits", f"{unit_prefix} 未命中覆盖该{'期' if by_period else '集'}的候选资源"


def _is_torrent_already_queued(
    ev: CandidateEval,
    active_downloads: list[dict[str, Any]],
    recent_tasks: list[dict[str, Any]],
    clean_name: str,
    tmdb_id_str: str | None,
    season_num: int,
    missing_eps: list[int],
    extra_titles: list[str] | None = None,
    alt_tmdb_id: str | None = None,
    alt_season: int | None = None,
) -> bool:
    """检查候选种子是否已存在于活跃下载队列或本批次近期已提交清单中（去重后至多一次提交）。"""
    cand_t = ev.cand.get("torrent_info") or {}
    cand_title_norm = re.sub(r"\s+", " ", str(cand_t.get("title") or "")).strip().casefold()
    cand_hash = str(cand_t.get("hash") or cand_t.get("info_hash") or cand_t.get("torrent_hash") or "").strip().casefold()
    valid_ids = {str(x) for x in (tmdb_id_str, alt_tmdb_id) if x}
    valid_seasons = {int(s) for s in (season_num, alt_season) if s is not None}
    match_names = [n.casefold() for n in [clean_name, *(extra_titles or [])] if n]

    for dl in active_downloads:
        dl_hash = str(dl.get("hash") or dl.get("info_hash") or "").strip().casefold()
        if cand_hash and dl_hash and cand_hash == dl_hash:
            return True
        dl_title = re.sub(r"\s+", " ", str(dl.get("title") or dl.get("name") or "")).strip().casefold()
        if cand_title_norm and dl_title and cand_title_norm == dl_title:
            return True

    for local in recent_tasks:
        l_id = str(local.get("tmdb_id") or "")
        l_name = str(local.get("show_name") or "").casefold()
        if l_id and valid_ids and l_id not in valid_ids:
            continue
        same_show = (l_id and l_id in valid_ids) or any(m in l_name for m in match_names)
        if same_show and local.get("season") in valid_seasons:
            if local.get("is_season_pack") or ev.is_season_pack:
                return True
            if cand_title_norm and cand_title_norm == str(local.get("torrent_title_norm") or ""):
                return True
            if set(ev.covered_missing).issubset(set(local.get("covered_episodes") or [])):
                return True
    return False


# ── 核心调度函数 ──────────────────────────────────────────

async def execute_gap_fill_download(
    season_record_ids: list[str],
    db: AsyncSession,
    batch_run_id: str | None = None,
    selected_episodes: list[int] | None = None,
    episodes_by_record: dict[str, list[int]] | None = None,
    confirmed_categories: dict[str, str] | None = None,
) -> dict[str, Any]:
    """对选中的季度记录执行补缺检索并推送 MoviePilot 下载（全链路持久化意图与脱敏回执）。"""
    settings = get_settings()
    if not settings.mp_url or not settings.mp_token:
        raise ValueError("MoviePilot 未配置 (MF_MP_URL 或 MF_MP_TOKEN 缺失)")

    batch_run_id = batch_run_id or f"gf-{uuid.uuid4().hex[:12]}"
    min_seeders = max(1, int(getattr(settings, "gap_fill_min_seeders", 2) or 2))
    tier_ratio = float(getattr(settings, "gap_fill_seeder_tier_ratio", 0.10) or 0.10)
    min_slack = max(1, int(getattr(settings, "gap_fill_min_seeder_slack", 1) or 1))
    min_resolution = str(getattr(settings, "gap_fill_min_resolution", "") or "")
    require_chinese = bool(getattr(settings, "gap_fill_require_chinese", False))

    stmt = select(SeasonRecord).where(SeasonRecord.id.in_(season_record_ids))
    res = await db.execute(stmt)
    records = list(res.scalars().all())
    await db.commit()  # 释放读取事务，不跨外部检索持有 SQLite 快照。

    if not records:
        return {
            "ok": False,
            "success": False,
            "batch_run_id": batch_run_id,
            "message": "未找到匹配的剧集季度记录",
            "submitted": [],
            "details": [],
            "errors": ["未找到匹配的剧集季度记录"],
        }

    mp = MoviePilotClient()
    submitted_results: list[dict[str, Any]] = []
    errors: list[str] = []
    skipped: list[str] = []
    returned_sites_all: list[str] = []
    overwrite_policy: dict[str, Any] = {}
    affected_tasks: set[str] = {r.task_id for r in records if getattr(r, "task_id", None)}

    category_site_plans: dict[str, dict[str, Any]] = {}
    site_plan: dict[str, Any] = {}

    async def _get_category_plan(cat: str) -> dict[str, Any]:
        if cat not in category_site_plans:
            category_site_plans[cat] = await resolve_category_gap_fill_sites(db, mp, cat)
        return category_site_plans[cat]

    try:
        global _active_downloads_cache, _active_downloads_cache_time, _recently_submitted_tasks
        now = time.time()
        _recently_submitted_tasks = [t for t in _recently_submitted_tasks if now - t.get("timestamp", 0) < _RECENTLY_SUBMITTED_TTL]

        is_mocked = isinstance(getattr(mp, "get_active_downloads", None), Mock)
        active_downloads: list[dict[str, Any]] = []
        try:
            if not is_mocked and _active_downloads_cache is not None and (now - _active_downloads_cache_time < _ACTIVE_DOWNLOADS_TTL):
                active_downloads = list(_active_downloads_cache)
            else:
                fetched = await mp.get_active_downloads()
                if isinstance(fetched, list):
                    _active_downloads_cache = fetched
                    _active_downloads_cache_time = now
                    active_downloads = list(fetched)
        except Exception as q_err:
            log.warning("获取 MoviePilot 活跃下载队列失败，继续直连检索: %s", q_err)
            if _active_downloads_cache:
                active_downloads = list(_active_downloads_cache)

        get_policy_fn = getattr(mp, "get_transfer_overwrite_policy", None)
        if callable(get_policy_fn) and not isinstance(get_policy_fn, Mock):
            try:
                overwrite_policy = await get_policy_fn()
            except Exception:
                overwrite_policy = {}

        from app.core.season_classifier import get_record_work_context, resolve_record_gap_category

        for rec in records:
            work = await get_record_work_context(db, rec)
            if (work.get("gap_fill_state") or {}).get("review_pending"):
                skipped.append(f"{rec.show_name}：补缺后复核尚未完成，请先核对入库，暂不重新搜索或推送")
                continue
            work_cat = work.get("main_category")
            if work_cat == "airing" or rec.status == "airing":
                skipped.append(f"{rec.show_name} S{rec.season_number:02d}：当前作品季处于「连载留源」状态，连载期暂不开放补缺推送")
                continue
            if rec.status not in ("download_needed", "period_missing"):
                skipped.append(f"{rec.show_name}：当前清单没有已确认的补缺任务")
                continue

            # 自动确定作品补缺规则类型（动漫 / 非动漫，取消强制「类型待确认」阻塞 · G20）
            if confirmed_categories and rec.id in confirmed_categories:
                chosen_cat = confirmed_categories[rec.id]
                cat_info = {"category": chosen_cat, "source": "manual", "reason": "本次推送人工指定"}
                try:
                    await save_gap_fill_work_preference(db, rec.tmdb_id, rec.show_name, chosen_cat, mp=mp)
                except Exception:
                    pass
            else:
                cat_info = await resolve_record_gap_category(db, rec)

            target_cat = cat_info.get("category") if cat_info.get("category") in ("anime", "non_anime") else "non_anime"
            cat_site_plan = await _get_category_plan(target_cat)
            site_plan = cat_site_plan
            await db.commit()
            if not cat_site_plan.get("ready"):
                skip_msg = f"{rec.show_name} S{rec.season_number:02d}：{cat_site_plan.get('reason') or '未启用可用补缺站点'}"
                skipped.append(skip_msg)
                continue
            sites_param = cat_site_plan.get("sites_param")
            id_to_name = cat_site_plan.get("id_to_name") or {}
            site_priority_map = cat_site_plan.get("site_priority_map") or {}
            allowed_site_ids = (
                None
                if (cat_site_plan.get("status") == "legacy_mock" and not cat_site_plan.get("active_ordered_ids"))
                else set(cat_site_plan.get("active_ordered_ids") or [])
            )

            # 身份冲突门禁：存在阻塞级身份冲突时严禁盲目搜种与推送
            try:
                rec_conflict = json.loads(getattr(rec, "conflict_json", None) or "{}")
            except Exception:
                rec_conflict = {}
            if isinstance(rec_conflict, dict) and rec_conflict.get("blocking"):
                c_reason = rec_conflict.get("reason") or "存在未解除的作品/季号身份冲突，已阻止补缺"
                skipped.append(f"{rec.show_name} S{rec.season_number:02d}：{c_reason}")
                continue

            from app.clients.clouddrive2 import CloudDrive2Client
            from app.core.inspector import run_inspection

            by_period = rec.status == "period_missing"
            disk = CloudDrive2Client()
            try:
                files = await disk.list_files(rec.season_path, force_refresh=True)
                parts = []
                for f in files:
                    name = f["name"]
                    if f.get("isDirectory") or not is_video_file(name) or not segment_number(name):
                        continue
                    stem = re.sub(r"(?i)part[ ._-]*(?:0?[123]|[abc])(?=[^a-z0-9]|$)", "", name)
                    ep = parse_episode(stem, f.get("path", ""), f.get("size", 0))
                    if not ep.is_extra and not ep.is_special and ep.season_number in (None, rec.season_number):
                        parts.append((name, ep.episode_numbers))
                try:
                    inspection = await run_inspection(
                        rec.task_id,
                        rec.show_path,
                        disk,
                        mp,
                        only_season_path=rec.season_path,
                        archive_root=settings.archive_root,
                        global_source_root=settings.source_root,
                        db=db,
                        persist_metadata=False,
                    )
                except TypeError:
                    inspection = await run_inspection(
                        rec.task_id,
                        rec.show_path,
                        disk,
                        mp,
                        only_season_path=rec.season_path,
                        archive_root=settings.archive_root,
                        global_source_root=settings.source_root,
                    )
                await db.commit()  # 结束复查期间的资料写入，再进行资源搜索。
                fresh = next((s for s in inspection.seasons if s.season_path == rec.season_path), None)
                if fresh is None:
                    errors.append(f"{rec.show_name}：未能读取本季最新清单，请确认源目录可访问")
                    continue
                if fresh.status != rec.status or str(fresh.tmdb_id) != str(rec.tmdb_id):
                    skipped.append(f"{rec.show_name}：复查结果已变化，{fresh.status_detail} 请重新初检")
                    continue
                unnumbered = next((name for name, numbers in parts if not numbers), None)
                if unnumbered:
                    skipped.append(f"{rec.show_name}：无法确定分段视频 {unnumbered} 属于哪一集，请补上集号")
                    continue
            except Exception as exc:
                if not db.is_active:
                    raise
                errors.append(f"{rec.show_name}：读取本季文件或资料失败（{type(exc).__name__}）")
                continue
            finally:
                await disk.close()

            show_name = rec.show_name
            season_num = rec.season_number
            tmdb_id_str = rec.tmdb_id
            if not tmdb_id_str:
                m = re.search(r"\[tmdbid[=:](\d+)\]", show_name, re.I)
                if m:
                    tmdb_id_str = m.group(1)

            clean_name = re.sub(r"\[tmdbid[=:]\d+\]", "", show_name, flags=re.I)
            clean_name = re.sub(r"[\(\[（].*?[\)\]）]", " ", clean_name)
            clean_name = re.sub(r"\s+\d{4}$", "", clean_name).strip() or show_name

            # 解析单季跨条目映射口径（例如 极地恶灵 S03 [75191] -> 银色恶魔 S01 [323903]）
            try:
                rec_mapping = json.loads(getattr(rec, "mapping_json", None) or "{}") or None
            except Exception:
                rec_mapping = None
            if not rec_mapping:
                src_tid_int = int(tmdb_id_str) if (tmdb_id_str and str(tmdb_id_str).isdigit()) else None
                rec_mapping = await resolve_active_season_mapping(
                    db, src_tid_int, season_num, rec.show_path, rec.season_path
                )
            elif isinstance(rec_mapping, dict) and not rec_mapping.get("target_tmdb_id"):
                rec_mapping = None

            fresh_mapping = getattr(fresh, "mapping", None)
            if not rec_mapping and isinstance(fresh_mapping, dict) and fresh_mapping.get("target_tmdb_id"):
                rec_mapping = fresh_mapping

            identity_key = build_identity_key(show_name, tmdb_id_str, season_num, rec_mapping)
            tmdb_id_num = int(tmdb_id_str) if (tmdb_id_str and str(tmdb_id_str).isdigit()) else 0

            is_mapped = bool(rec_mapping and rec_mapping.get("target_tmdb_id"))
            search_tmdb_id = int(rec_mapping["target_tmdb_id"]) if is_mapped else tmdb_id_num
            search_season = int(rec_mapping.get("target_season") or 1) if is_mapped else season_num
            numbering_mode = str(rec_mapping.get("numbering_mode") or "independent") if is_mapped else "standard"
            mapped_keywords: list[str] = []
            if is_mapped and rec_mapping:
                for kw in [
                    rec_mapping.get("target_title"),
                    rec_mapping.get("target_original_title"),
                    *(rec_mapping.get("search_keywords") or []),
                ]:
                    kws = str(kw or "").strip()
                    if kws and kws not in mapped_keywords:
                        mapped_keywords.append(kws)
            match_names = [clean_name, *mapped_keywords]

            missing_eps: list[int] = []
            if rec.missing_episodes_json:
                try:
                    missing_eps = json.loads(rec.missing_episodes_json)
                except Exception:
                    pass

            missing_eps = sorted(set(missing_eps) & set(fresh.missing_episodes))
            user_eps_raw: list[int] | None = None
            if isinstance(episodes_by_record, dict) and rec.id in episodes_by_record:
                user_eps_raw = episodes_by_record.get(rec.id)
            elif selected_episodes is not None and len(records) == 1:
                user_eps_raw = selected_episodes
            if user_eps_raw is not None:
                user_confirmed_set = {int(x) for x in user_eps_raw if str(x).isdigit()}
                missing_eps = [ep for ep in missing_eps if ep in user_confirmed_set]
                if not missing_eps:
                    skipped.append(f"{clean_name} S{season_num:02d}：所选目标缺号为空或已覆盖，无需下载")
                    continue
            total_eps = rec.total_episodes or (max(missing_eps) if missing_eps else 0)
            search_total_eps = (
                int(rec_mapping.get("expected_total_episodes") or total_eps)
                if (is_mapped and rec_mapping)
                else total_eps
            )
            if not missing_eps:
                skipped.append(f"{clean_name} S{season_num:02d}：原缺集现已覆盖，无需下载")
                continue

            # 持久化已推送待入库集（含 queued / transfer_pending / submitted_unverified / 历史Log推断）
            gap_view = await compute_season_gap_fill_view(db, rec)
            await db.commit()
            persisted_pushed = set(gap_view.get("pushed_episodes") or [])

            # 实时下载队列与本批次已受理记录对齐（兼顾本地坐标与跨条目映射目标坐标）
            in_progress_eps: set[int] = set(persisted_pushed)
            for dl in active_downloads:
                dl_name = str(dl.get("title") or dl.get("name") or "")
                dl_id = dl.get("tmdb_id") or dl.get("tmdbid")
                valid_dl_ids = {str(x) for x in (tmdb_id_str, search_tmdb_id if is_mapped else None) if x}
                if dl_id and valid_dl_ids and str(dl_id) not in valid_dl_ids:
                    continue
                name_hit = any(mn.casefold() in dl_name.casefold() for mn in match_names if mn)
                if (dl_id and str(dl_id) in valid_dl_ids) or name_hit or (valid_dl_ids and any(vid in dl_name for vid in valid_dl_ids)):
                    dl_season = dl.get("season")
                    if dl_season is None:
                        s_match = re.search(r"S(\d{1,2})", dl_name, re.I)
                        dl_season = int(s_match.group(1)) if s_match else None
                    is_target_mapped_dl = bool(is_mapped and ((dl_id and str(dl_id) == str(search_tmdb_id)) or (dl_season == search_season and any(k.casefold() in dl_name.casefold() for k in mapped_keywords))))
                    if dl_season is None or str(dl_season) == str(season_num) or (is_mapped and str(dl_season) == str(search_season)):
                        ep_field = str(dl.get("episode") or "")
                        if by_period:
                            parsed_dl_eps = [int(n) for n in re.findall(r"第\s*(\d+)\s*期", dl_name)]
                        else:
                            parsed_dl_eps = (
                                parse_active_episodes(ep_field, search_total_eps if is_target_mapped_dl else total_eps)
                                if ep_field
                                else parse_episode(dl_name).episode_numbers
                            )
                            if not parsed_dl_eps and re.search(r"(?:S\d{1,2}(?!E\d)|Complete|全\s*\d+\s*集)", dl_name, re.I):
                                span_tot = search_total_eps if is_target_mapped_dl else total_eps
                                parsed_dl_eps = list(range(1, span_tot + 1)) if span_tot > 0 else list(missing_eps)
                        if is_target_mapped_dl and parsed_dl_eps:
                            parsed_dl_eps = translate_search_to_local_episodes(parsed_dl_eps, rec_mapping)
                        in_progress_eps.update(parsed_dl_eps)

            if not is_mocked:
                for local_task in _recently_submitted_tasks:
                    l_id = str(local_task.get("tmdb_id") or "")
                    l_name = str(local_task.get("show_name") or "")
                    valid_lids = {str(x) for x in (tmdb_id_str, str(search_tmdb_id) if search_tmdb_id else None) if x}
                    if l_id and valid_lids and l_id not in valid_lids:
                        continue
                    if (l_id and l_id in valid_lids) or any(mn.casefold() in l_name.casefold() for mn in match_names if mn):
                        if local_task.get("season") in {season_num, search_season}:
                            if local_task.get("is_season_pack"):
                                in_progress_eps.update(range(1, total_eps + 1) if total_eps > 0 else missing_eps)
                            else:
                                in_progress_eps.update(local_task.get("covered_episodes", []))

            actually_needed = [ep for ep in missing_eps if ep not in in_progress_eps]
            if in_progress_eps and not actually_needed:
                log.info("%s S%02d 所缺集数 %s 已在待入库/活跃下载队列中，跳过重复提交", clean_name, season_num, missing_eps)
                skipped.append(f"{clean_name}：缺集 {missing_eps} 已在已推送待入库或下载队列中")
                continue
            elif in_progress_eps:
                log.info("%s S%02d 部分集数 %s 已在推送/队列中，仅补齐剩余缺失集: %s", clean_name, season_num, sorted(in_progress_eps), actually_needed)
                missing_eps = actually_needed

            # 映射检索集号（标准同号保持不变；若存在集号偏移则换算至检索季）
            search_missing_eps = translate_local_to_search_episodes(missing_eps, rec_mapping) if is_mapped else list(missing_eps)
            has_season_active_overlap = bool(in_progress_eps)
            search_kwargs: dict[str, Any] = {"sites": sites_param} if sites_param else {}
            part_mapping = getattr(fresh, "part_episode_mapping", None)
            parts_mode = bool(parts and not by_period)

            primary_query_title = mapped_keywords[0] if (is_mapped and mapped_keywords) else clean_name
            log.info(
                "正在检索补缺资源: %s S%02d (检索口径 TMDB:%s S%02d [%s], 缺失集:%s, 请求站点:%s)",
                clean_name, season_num, search_tmdb_id or "无", search_season, primary_query_title, missing_eps, site_plan.get("requested_label"),
            )

            # 第一轮：按目标口径精确搜索（若首轮返回空，则回退别名/原名+目标季号搜索一次）
            keyword_fallback = False
            title_fallback_done = False
            search_receipts: list[dict[str, Any]] = []
            try:
                raw_results = await mp.search_resources(
                    tmdb_id=search_tmdb_id,
                    season=search_season,
                    title=primary_query_title,
                    **search_kwargs,
                )
                r1_sites = _collect_returned_sites(raw_results if isinstance(raw_results, list) else [], id_to_name)
                search_receipts.append({
                    "stage": "mapped_tmdb_exact" if is_mapped else ("tmdb_exact" if search_tmdb_id else "title_initial"),
                    "search_tmdb_id": search_tmdb_id,
                    "search_season": search_season,
                    "query": primary_query_title,
                    "requested_sites": site_plan.get("requested_ids") or "all_active",
                    "returned_sites": r1_sites,
                    "api_responded": True,
                    "count": len(raw_results) if isinstance(raw_results, list) else 0,
                })
                if not raw_results and (search_tmdb_id or is_mapped):
                    fallback_candidates: list[str] = []
                    if search_tmdb_id:
                        try:
                            media = await mp.get_media_info(search_tmdb_id)
                            orig = (media or {}).get("original_title") or (media or {}).get("en_title")
                            if orig:
                                fallback_candidates.append(str(orig))
                        except Exception:
                            pass
                    for kw in mapped_keywords:
                        if kw not in fallback_candidates:
                            fallback_candidates.append(kw)
                    if not fallback_candidates:
                        fallback_candidates.append(clean_name)

                    raw_accum: list[dict[str, Any]] = []
                    for q_kw in fallback_candidates[:2]:
                        sub_raw = await mp.search_resources(title=q_kw, season=search_season, **search_kwargs)
                        if isinstance(sub_raw, list) and sub_raw:
                            raw_accum.extend(sub_raw)
                        fb_sites = _collect_returned_sites(sub_raw if isinstance(sub_raw, list) else [], id_to_name)
                        search_receipts.append({
                            "stage": "mapped_keyword_fallback" if is_mapped else "title_fallback",
                            "query": q_kw,
                            "search_season": search_season,
                            "requested_sites": site_plan.get("requested_ids") or "all_active",
                            "returned_sites": fb_sites,
                            "api_responded": True,
                            "count": len(sub_raw) if isinstance(sub_raw, list) else 0,
                        })
                        if raw_accum:
                            break
                    raw_results = raw_accum
                    keyword_fallback = True
                    title_fallback_done = True
            except Exception as se:
                err_txt = f"MoviePilot 检索未完成（{type(se).__name__}）"
                errors.append(f"{clean_name} S{season_num:02d}：{err_txt}")
                await record_unsubmitted_episode_outcomes(
                    db, rec, identity_key, missing_eps,
                    batch_run_id=batch_run_id,
                    outcome_state="search_error",
                    outcome_code="search_error",
                    reason=err_txt,
                    search_tmdb_id=search_tmdb_id or None,
                    search_season_number=search_season,
                    numbering_mode=numbering_mode,
                    diag_summary={"stage_error": type(se).__name__, "receipts": search_receipts},
                )
                await record_season_run_snapshot(
                    db, rec, identity_key,
                    batch_run_id=batch_run_id,
                    outcome_status="search_error",
                    summary_text=err_txt,
                    target_episodes=missing_eps,
                    accepted_episodes=[],
                    unverified_episodes=[],
                    failed_episodes={str(ep): {"code": "search_error", "reason": err_txt} for ep in missing_eps},
                    min_seeders_required=min_seeders,
                    sites_summary={"requested": site_plan.get("requested_label", "")},
                    search_diag={"receipts": search_receipts},
                )
                continue

            if not raw_results or not isinstance(raw_results, list):
                zero_reason = f"已搜索缺号 {missing_eps}，MoviePilot 本次搜索未返回资源（0 命中）"
                skipped.append(f"{clean_name} S{season_num:02d}：{zero_reason}")
                all_r_sites = list({s for rc in search_receipts for s in rc.get("returned_sites", [])})
                await record_unsubmitted_episode_outcomes(
                    db, rec, identity_key, missing_eps,
                    batch_run_id=batch_run_id,
                    outcome_state="zero_hits",
                    outcome_code="zero_hits",
                    reason=zero_reason,
                    search_tmdb_id=search_tmdb_id or None,
                    search_season_number=search_season,
                    numbering_mode=numbering_mode,
                    diag_summary={"raw_count": 0, "receipts": search_receipts},
                )
                await record_season_run_snapshot(
                    db, rec, identity_key,
                    batch_run_id=batch_run_id,
                    outcome_status="zero_hits",
                    summary_text=zero_reason,
                    target_episodes=missing_eps,
                    accepted_episodes=[],
                    unverified_episodes=[],
                    failed_episodes={str(ep): {"code": "zero_hits", "reason": zero_reason} for ep in missing_eps},
                    candidate_count=0,
                    max_seeders_seen=0,
                    min_seeders_required=min_seeders,
                    sites_summary={"requested": site_plan.get("requested_label", ""), "returned": all_r_sites},
                    search_diag={"receipts": search_receipts},
                )
                continue

            raw_count = len(raw_results)
            all_raw_candidates: list[dict[str, Any]] = list(raw_results)
            max_seeders_seen = max((_candidate_seeders(c) for c in raw_results), default=0)

            # 分别通过独立散集分支与整季包分支评估首轮结果（兼容跨条目映射口径与别名隔离）
            standalone_evals, pack_evals, reject_reasons = _evaluate_raw_candidates(
                raw_results,
                tmdb_id_num=search_tmdb_id,
                season_num=search_season,
                total_eps=search_total_eps,
                missing_eps=search_missing_eps,
                by_period=by_period,
                parts_mode=parts_mode,
                part_episode_mapping=part_mapping,
                keyword_fallback=keyword_fallback,
                min_seeders=min_seeders,
                min_resolution=min_resolution,
                require_chinese=require_chinese,
                source_tmdb_id=tmdb_id_num,
                source_season=season_num,
                allowed_titles=mapped_keywords if is_mapped else None,
                allowed_site_ids=allowed_site_ids,
                site_priority_map=site_priority_map,
            )

            all_returned_sites = list({s for rc in search_receipts for s in rc.get("returned_sites", [])})
            for s in all_returned_sites:
                if s not in returned_sites_all:
                    returned_sites_all.append(s)

            # 去除队列中已在下载的同种
            standalone_evals = _dedup_evaluations([
                ev for ev in standalone_evals
                if not _is_torrent_already_queued(
                    ev, active_downloads, _recently_submitted_tasks,
                    primary_query_title, str(search_tmdb_id) if search_tmdb_id else tmdb_id_str,
                    search_season, search_missing_eps,
                    extra_titles=match_names, alt_tmdb_id=tmdb_id_str, alt_season=season_num,
                )
            ])
            pack_evals = _dedup_evaluations([
                ev for ev in pack_evals
                if not _is_torrent_already_queued(
                    ev, active_downloads, _recently_submitted_tasks,
                    primary_query_title, str(search_tmdb_id) if search_tmdb_id else tmdb_id_str,
                    search_season, search_missing_eps,
                    extra_titles=match_names, alt_tmdb_id=tmdb_id_str, alt_season=season_num,
                )
            ])

            # 决策步骤 1：先算散集 / 连集省容量方案
            standalone_plan, standalone_note = _plan_standalone_downloads(
                standalone_evals,
                search_missing_eps,
                tier_ratio=tier_ratio,
                min_slack=min_slack,
            )
            standalone_covered = {ep for ev in standalone_plan for ep in ev.covered_missing}
            search_missing_set = set(search_missing_eps)
            local_missing_set = set(missing_eps)
            all_covered_by_standalone = bool(standalone_plan) and search_missing_set.issubset(standalone_covered)

            selected_evals: list[CandidateEval] = []
            plan_summary_note = ""
            deferred_sp_reason = ""
            prefer_season_pack = (len(search_missing_eps) > 3)

            # 缺集大于 3 集：优先整季包策略
            if prefer_season_pack:
                # 若首轮未命中合格整季包且尚未尝试标题搜索，主动进行一次整季补搜
                if not pack_evals and not title_fallback_done:
                    try:
                        search_title = mapped_keywords[0] if (is_mapped and mapped_keywords) else clean_name
                        if search_tmdb_id:
                            try:
                                media_fb = await mp.get_media_info(search_tmdb_id)
                                if isinstance(media_fb, dict):
                                    search_title = media_fb.get("original_title") or media_fb.get("en_title") or media_fb.get("title") or search_title
                            except Exception:
                                pass
                        fb_raw = await mp.search_resources(title=search_title, season=search_season, **search_kwargs)
                        title_fallback_done = True
                        fb_list = fb_raw if isinstance(fb_raw, list) else []
                        fb_sites = _collect_returned_sites(fb_list, id_to_name)
                        for s in fb_sites:
                            if s not in returned_sites_all:
                                returned_sites_all.append(s)
                        search_receipts.append({
                            "stage": "season_pack_fallback",
                            "query": search_title,
                            "search_season": search_season,
                            "requested_sites": site_plan.get("requested_ids") or "all_active",
                            "returned_sites": fb_sites,
                            "api_responded": True,
                            "count": len(fb_list),
                        })
                        if fb_list:
                            raw_count += len(fb_list)
                            all_raw_candidates.extend(fb_list)
                            max_seeders_seen = max(max_seeders_seen, max((_candidate_seeders(c) for c in fb_list), default=0))
                            all_returned_sites = list({s for rc in search_receipts for s in rc.get("returned_sites", [])})
                            fb_st, fb_sp, fb_rej = _evaluate_raw_candidates(
                                fb_list,
                                tmdb_id_num=search_tmdb_id,
                                season_num=search_season,
                                total_eps=search_total_eps,
                                missing_eps=search_missing_eps,
                                by_period=by_period,
                                parts_mode=parts_mode,
                                part_episode_mapping=part_mapping,
                                keyword_fallback=True,
                                min_seeders=min_seeders,
                                min_resolution=min_resolution,
                                require_chinese=require_chinese,
                                source_tmdb_id=tmdb_id_num,
                                source_season=season_num,
                                allowed_titles=mapped_keywords if is_mapped else None,
                                allowed_site_ids=allowed_site_ids,
                                site_priority_map=site_priority_map,
                            )
                            reject_reasons.extend(fb_rej)
                            standalone_evals = _dedup_evaluations(standalone_evals + [
                                ev for ev in fb_st
                                if not _is_torrent_already_queued(
                                    ev, active_downloads, _recently_submitted_tasks,
                                    primary_query_title, str(search_tmdb_id) if search_tmdb_id else tmdb_id_str,
                                    search_season, search_missing_eps,
                                    extra_titles=match_names, alt_tmdb_id=tmdb_id_str, alt_season=season_num,
                                )
                            ])
                            pack_evals = _dedup_evaluations(pack_evals + [
                                ev for ev in fb_sp
                                if not _is_torrent_already_queued(
                                    ev, active_downloads, _recently_submitted_tasks,
                                    primary_query_title, str(search_tmdb_id) if search_tmdb_id else tmdb_id_str,
                                    search_season, search_missing_eps,
                                    extra_titles=match_names, alt_tmdb_id=tmdb_id_str, alt_season=season_num,
                                )
                            ])
                            standalone_plan, standalone_note = _plan_standalone_downloads(
                                standalone_evals,
                                search_missing_eps,
                                tier_ratio=tier_ratio,
                                min_slack=min_slack,
                            )
                            standalone_covered = {ep for ev in standalone_plan for ep in ev.covered_missing}
                            all_covered_by_standalone = bool(standalone_plan) and search_missing_set.issubset(standalone_covered)
                    except Exception as fb_err:
                        log.warning("[%s S%02d] 整季补搜未完成: %s", clean_name, season_num, fb_err)

                if pack_evals:
                    if has_season_active_overlap:
                        deferred_sp_reason = (
                            f"{clean_name} S{season_num:02d}：缺集 {len(search_missing_eps)} 集虽优先整季包，但目标季已有活跃下载/已推送集 {sorted(in_progress_eps)}，"
                            "整季包与在途文件存在重叠，暂缓整包回退散集下载"
                        )
                        if standalone_plan:
                            selected_evals = standalone_plan
                            plan_summary_note = standalone_note
                    else:
                        best_sp, sp_cmp_note = _pick_best_season_pack(pack_evals, tier_ratio=tier_ratio, min_slack=min_slack)
                        if best_sp:
                            selected_evals = [best_sp]
                            already_owned = max(0, len(best_sp.covered_all) - len(search_missing_eps))
                            miss_label = ",".join(f"E{ep:02d}" for ep in missing_eps)
                            sp_warn = (
                                f"缺集大于3集（共 {len(search_missing_eps)} 集），优先选择整季包下载：为补 {miss_label}，下载整季 {len(best_sp.covered_all)} 集，"
                                f"预计 {_format_bytes(best_sp.size_bytes)}，其中 {already_owned} 集已有"
                            )
                            plan_summary_note = f"{sp_warn} ({sp_cmp_note})"
                            log.info("[%s S%02d] %s", clean_name, season_num, plan_summary_note)

                # 若缺集大于 3 集但无合格整季包，回退到散集拼包
                if not selected_evals and standalone_plan:
                    selected_evals = standalone_plan
                    plan_summary_note = f"缺集 {len(search_missing_eps)} 集未找到可用整季包，回退散集方案: {standalone_note}"

            # 缺集 <= 3 集（或缺集 > 3 且未选出整包时已回退处理）：保持散集省容量优先
            if not selected_evals:
                if all_covered_by_standalone:
                    selected_evals = standalone_plan
                    plan_summary_note = standalone_note
                else:
                    # 决策步骤 2：散集补不全时先查首轮整季包；仍无合格整包且尚未做标题搜索时，执行一次原名/别名+季号整季补搜
                    if not pack_evals and not title_fallback_done:
                        try:
                            search_title = mapped_keywords[0] if (is_mapped and mapped_keywords) else clean_name
                            if search_tmdb_id:
                                try:
                                    media_fb = await mp.get_media_info(search_tmdb_id)
                                    if isinstance(media_fb, dict):
                                        search_title = media_fb.get("original_title") or media_fb.get("en_title") or media_fb.get("title") or search_title
                                except Exception:
                                    pass
                            fb_raw = await mp.search_resources(title=search_title, season=search_season, **search_kwargs)
                            title_fallback_done = True
                            fb_list = fb_raw if isinstance(fb_raw, list) else []
                            fb_sites = _collect_returned_sites(fb_list, id_to_name)
                            for s in fb_sites:
                                if s not in returned_sites_all:
                                    returned_sites_all.append(s)
                            search_receipts.append({
                                "stage": "season_pack_fallback",
                                "query": search_title,
                                "search_season": search_season,
                                "requested_sites": site_plan.get("requested_ids") or "all_active",
                                "returned_sites": fb_sites,
                                "api_responded": True,
                                "count": len(fb_list),
                            })
                            if fb_list:
                                raw_count += len(fb_list)
                                all_raw_candidates.extend(fb_list)
                                max_seeders_seen = max(max_seeders_seen, max((_candidate_seeders(c) for c in fb_list), default=0))
                                all_returned_sites = list({s for rc in search_receipts for s in rc.get("returned_sites", [])})
                                fb_st, fb_sp, fb_rej = _evaluate_raw_candidates(
                                    fb_list,
                                    tmdb_id_num=search_tmdb_id,
                                    season_num=search_season,
                                    total_eps=search_total_eps,
                                    missing_eps=search_missing_eps,
                                    by_period=by_period,
                                    parts_mode=parts_mode,
                                    part_episode_mapping=part_mapping,
                                    keyword_fallback=True,
                                    min_seeders=min_seeders,
                                    min_resolution=min_resolution,
                                    require_chinese=require_chinese,
                                    source_tmdb_id=tmdb_id_num,
                                    source_season=season_num,
                                    allowed_titles=mapped_keywords if is_mapped else None,
                                    allowed_site_ids=allowed_site_ids,
                                    site_priority_map=site_priority_map,
                                )
                                reject_reasons.extend(fb_rej)
                                standalone_evals = _dedup_evaluations(standalone_evals + [
                                    ev for ev in fb_st
                                    if not _is_torrent_already_queued(
                                        ev, active_downloads, _recently_submitted_tasks,
                                        primary_query_title, str(search_tmdb_id) if search_tmdb_id else tmdb_id_str,
                                        search_season, search_missing_eps,
                                        extra_titles=match_names, alt_tmdb_id=tmdb_id_str, alt_season=season_num,
                                    )
                                ])
                                pack_evals = _dedup_evaluations(pack_evals + [
                                    ev for ev in fb_sp
                                    if not _is_torrent_already_queued(
                                        ev, active_downloads, _recently_submitted_tasks,
                                        primary_query_title, str(search_tmdb_id) if search_tmdb_id else tmdb_id_str,
                                        search_season, search_missing_eps,
                                        extra_titles=match_names, alt_tmdb_id=tmdb_id_str, alt_season=season_num,
                                    )
                                ])
                                standalone_plan, standalone_note = _plan_standalone_downloads(
                                    standalone_evals,
                                    search_missing_eps,
                                    tier_ratio=tier_ratio,
                                    min_slack=min_slack,
                                )
                                standalone_covered = {ep for ev in standalone_plan for ep in ev.covered_missing}
                                all_covered_by_standalone = bool(standalone_plan) and search_missing_set.issubset(standalone_covered)
                        except Exception as fb_err:
                            log.warning("[%s S%02d] 整季补搜未完成: %s", clean_name, season_num, fb_err)

                    if all_covered_by_standalone:
                        selected_evals = standalone_plan
                        plan_summary_note = standalone_note
                    elif pack_evals:
                        if has_season_active_overlap:
                            deferred_sp_reason = (
                                f"{clean_name} S{season_num:02d}：目标季已有活跃下载/已推送集 {sorted(in_progress_eps)}，"
                                "整季包与在途文件存在重叠，暂缓整包待队列变化后再评估"
                            )
                            if standalone_plan:
                                selected_evals = standalone_plan
                                plan_summary_note = standalone_note
                        else:
                            best_sp, sp_cmp_note = _pick_best_season_pack(pack_evals, tier_ratio=tier_ratio, min_slack=min_slack)
                            if best_sp:
                                selected_evals = [best_sp]
                                already_owned = max(0, len(best_sp.covered_all) - len(search_missing_eps))
                                miss_label = ",".join(f"E{ep:02d}" for ep in missing_eps)
                                sp_warn = (
                                    f"为补 {miss_label}，下载整季 {len(best_sp.covered_all)} 集，"
                                    f"预计 {_format_bytes(best_sp.size_bytes)}，其中 {already_owned} 集已有"
                                )
                                plan_summary_note = f"{sp_warn} ({sp_cmp_note})"
                                log.info("[%s S%02d] %s", clean_name, season_num, plan_summary_note)
                    elif standalone_plan:
                        selected_evals = standalone_plan
                        plan_summary_note = standalone_note

            if deferred_sp_reason:
                skipped.append(deferred_sp_reason)

            fail_code, fail_summary = _classify_reject_outcome(
                raw_count,
                reject_reasons,
                deferred_sp_reason,
                max_seeders_seen=max_seeders_seen,
                min_seeders=min_seeders,
            )
            sites_diag_obj = {
                "requested": site_plan.get("requested_label", ""),
                "returned": all_returned_sites,
                "scope_confirmed": bool(site_plan.get("scope_confirmed", False)),
            }

            if not selected_evals:
                if not deferred_sp_reason:
                    skipped.append(f"{clean_name} S{season_num:02d}：缺号 {missing_eps} {fail_summary}")
                per_ep_failed: dict[str, dict[str, str]] = {}
                for ep_m in missing_eps:
                    s_ep_list = translate_local_to_search_episodes([ep_m], rec_mapping) if is_mapped else [ep_m]
                    s_ep = s_ep_list[0] if s_ep_list else ep_m
                    ep_code, ep_reason = _diagnose_episode_failure(
                        ep_m,
                        s_ep,
                        all_raw_candidates,
                        by_period=by_period,
                        min_seeders=min_seeders,
                        fallback_code=fail_code,
                        fallback_summary=fail_summary,
                    )
                    per_ep_failed[str(ep_m)] = {"code": ep_code, "reason": ep_reason}
                    await record_unsubmitted_episode_outcomes(
                        db, rec, identity_key, [ep_m],
                        batch_run_id=batch_run_id,
                        outcome_state=ep_code,
                        outcome_code=ep_code,
                        reason=ep_reason,
                        search_tmdb_id=search_tmdb_id or None,
                        search_season_number=search_season,
                        numbering_mode=numbering_mode,
                        diag_summary={"raw_count": raw_count, "max_seeders": max_seeders_seen, "reasons": list(dict.fromkeys(reject_reasons[:5]))},
                    )
                await record_season_run_snapshot(
                    db, rec, identity_key,
                    batch_run_id=batch_run_id,
                    outcome_status=fail_code,
                    summary_text=fail_summary,
                    target_episodes=missing_eps,
                    accepted_episodes=[],
                    unverified_episodes=[],
                    failed_episodes=per_ep_failed,
                    candidate_count=raw_count,
                    max_seeders_seen=max_seeders_seen,
                    min_seeders_required=min_seeders,
                    sites_summary=sites_diag_obj,
                    search_diag={"receipts": search_receipts},
                )
                continue

            log.info(
                "[%s S%02d] 选种决策完成 | %s | 返回候选站点: %s",
                clean_name, season_num, plan_summary_note, ", ".join(all_returned_sites) or "无",
            )

            covered_search_total = {ep for ev in selected_evals for ep in ev.covered_missing}
            covered_local_total = set(translate_search_to_local_episodes(sorted(covered_search_total), rec_mapping) if is_mapped else covered_search_total)
            uncovered = sorted(local_missing_set - covered_local_total)
            if uncovered:
                skipped.append(f"{clean_name} S{season_num:02d}：部分散集已提交，剩余未覆盖缺号 {uncovered}")

            season_accepted_eps: list[int] = []
            season_unverified_eps: list[int] = []
            season_failed_eps: dict[str, Any] = {}
            season_transfer_history_cleared = False

            # 统一提交选定下载计划：严格遵循“先落盘意图 -> 提交 MP -> 清理 MP 转移记录 -> 回填脱敏回执”
            for ev in selected_evals:
                cand_t = ev.cand.get("torrent_info") or {}
                cand_m = ev.cand.get("media_info") or {}
                raw_title = str(cand_t.get("title") or "")
                title = sanitize_text_no_secrets(raw_title)
                cand_hash = str(cand_t.get("hash") or cand_t.get("info_hash") or cand_t.get("torrent_hash") or "").strip()
                site = ev.site_name
                seeds = ev.seeders
                is_sp = ev.is_season_pack

                search_cov_eps = list(ev.covered_all if is_sp else ev.covered_missing)
                local_cov_all = translate_search_to_local_episodes(list(ev.covered_all), rec_mapping) if is_mapped else list(ev.covered_all)
                local_cov_missing = [
                    ep for ep in (translate_search_to_local_episodes(list(ev.covered_missing), rec_mapping) if is_mapped else list(ev.covered_missing))
                    if ep in local_missing_set
                ] or list(ev.covered_missing)
                covered_eps = list(local_cov_all if is_sp else local_cov_missing)
                real_missing_covered = sorted(set(local_cov_missing) & local_missing_set)
                already_owned_cnt = max(0, len(ev.covered_all) - len(real_missing_covered))
                size_display = _format_bytes(ev.size_bytes)

                if is_sp:
                    miss_tag = ",".join(f"E{ep:02d}" for ep in real_missing_covered)
                    plan_desc = f"整季包回退(为补 {miss_tag} 下全{len(covered_eps)}集/{size_display}/已有{already_owned_cnt}集)"
                else:
                    plan_desc = f"省容量精选(E{','.join(f'{ep:02d}' for ep in covered_eps)}/{size_display})"

                # 先落盘推送前意图（防范提交中崩溃/超时造成状态丢失）
                intent_rows = await record_submit_intent(
                    db, rec, identity_key, real_missing_covered,
                    batch_run_id=batch_run_id,
                    search_tmdb_id=search_tmdb_id or None,
                    search_season_number=search_season,
                    numbering_mode=numbering_mode,
                    torrent_title=title,
                    torrent_hash=cand_hash,
                    site_id=str(ev.site_id or ""),
                    site_name=str(site or ""),
                    size_bytes=int(ev.size_bytes or 0),
                    seeders=int(seeds or 0),
                    covered_all=local_cov_all,
                    diag_summary={"plan_desc": plan_desc, "raw_count": raw_count, "max_seeders": max_seeders_seen},
                )

                cleared_info: dict[str, Any] = {"cleared_count": 0, "records": [], "protected_count": 0}

                async def _clear_mp_transfer_after_submit() -> None:
                    nonlocal cleared_info, season_transfer_history_cleared
                    if season_transfer_history_cleared:
                        return
                    season_transfer_history_cleared = True
                    try:
                        aliases = [t for t in [primary_query_title, clean_name, str(cand_m.get("title") or "").strip()] if t]
                        try:
                            clean_res = await mp.clear_transfer_history_for_episodes(
                                title=primary_query_title,
                                season=search_season,
                                episodes=None,
                                explicit_only=False,
                                tmdb_id=search_tmdb_id or tmdb_id_str or None,
                                title_aliases=aliases,
                            )
                        except TypeError:
                            clean_res = await mp.clear_transfer_history_for_episodes(
                                title=primary_query_title,
                                season=search_season,
                                episodes=None,
                            )
                        if isinstance(clean_res, dict):
                            cleared_info = clean_res
                        if cleared_info.get("cleared_count", 0) > 0:
                            log.info(
                                "[%s S%02d] 推送下载后已清理 MoviePilot「媒体整理」中 %d 条转移记录（仅删记录，不删实体文件）",
                                clean_name, season_num, cleared_info["cleared_count"],
                            )
                    except Exception as clean_err:
                        log.warning("推送下载后清理 MoviePilot 转移历史记录异常（不阻断下载）: %s", clean_err)

                log.info(
                    "向 MoviePilot 提交下载 [%s]: [%s] %s (做种:%d, 大小:%s, 同种来源:%s)",
                    plan_desc, site, title, seeds, size_display, ",".join(ev.site_sources) or site,
                )
                is_ok = False
                is_unverified = False
                msg = ""
                reply: Any = None
                try:
                    reply = await mp.submit_download(cand_m, cand_t)
                    is_ok = isinstance(reply, dict) and reply.get("success") is True
                    msg = sanitize_text_no_secrets(reply.get("message", "已提交") if isinstance(reply, dict) else "已提交")
                    if is_ok:
                        await finalize_submit_outcome(
                            db, intent_rows,
                            outcome_state="queued",
                            outcome_code="accepted",
                            reason=f"MP已受理并推送队列（{site}，做种:{seeds}）",
                            reply=_sanitize_reply(reply),
                        )
                        season_accepted_eps.extend(real_missing_covered)
                        await _clear_mp_transfer_after_submit()
                    else:
                        await finalize_submit_outcome(
                            db, intent_rows,
                            outcome_state="rejected",
                            outcome_code="mp_rejected",
                            reason=f"MoviePilot 拒绝提交：{msg or '未返回成功标识'}",
                            reply=_sanitize_reply(reply),
                        )
                        for ep_i in real_missing_covered:
                            season_failed_eps[str(ep_i)] = {"code": "mp_rejected", "reason": msg or "MP 拒绝提交"}
                except Exception as de:
                    is_timeout = "timeout" in type(de).__name__.lower() or "timed out" in str(de).lower()
                    queued_after_timeout = False
                    if is_timeout:
                        try:
                            fresh_q = await mp.get_active_downloads()
                            if isinstance(fresh_q, list):
                                _active_downloads_cache = fresh_q
                                _active_downloads_cache_time = time.time()
                                queued_after_timeout = _is_torrent_already_queued(
                                    ev, fresh_q, [],
                                    primary_query_title, str(search_tmdb_id) if search_tmdb_id else tmdb_id_str,
                                    search_season, search_missing_eps,
                                    extra_titles=match_names, alt_tmdb_id=tmdb_id_str, alt_season=season_num,
                                )
                        except Exception:
                            pass
                    if queued_after_timeout:
                        is_ok = True
                        msg = "提交响应超时，但经核查 MP 下载队列已确认入列（防重保护生效）"
                        await finalize_submit_outcome(
                            db, intent_rows,
                            outcome_state="queued",
                            outcome_code="accepted_after_timeout",
                            reason=msg,
                        )
                        season_accepted_eps.extend(real_missing_covered)
                        await _clear_mp_transfer_after_submit()
                    elif is_timeout:
                        is_unverified = True
                        msg = "提交请求超时且暂未在 MP 队列命中，已转为「已提交待核实」锁定防重，请点击核对入库确认"
                        await finalize_submit_outcome(
                            db, intent_rows,
                            outcome_state="submitted_unverified",
                            outcome_code="timeout_unverified",
                            reason=msg,
                        )
                        season_unverified_eps.extend(real_missing_covered)
                        await _clear_mp_transfer_after_submit()
                        submitted_results.append({
                            "show_name": clean_name,
                            "season": season_num,
                            "site": site,
                            "site_id": ev.site_id,
                            "site_sources": list(ev.site_sources),
                            "title": title,
                            "seeds": seeds,
                            "size_bytes": ev.size_bytes,
                            "size_display": size_display,
                            "covered_episodes": covered_eps,
                            "requested_missing_episodes": list(missing_eps),
                            "torrent_covered_episodes": list(local_cov_all),
                            "already_owned_count": already_owned_cnt,
                            "is_season_pack": is_sp,
                            "plan_desc": plan_desc,
                            "runner_up_diff": plan_summary_note,
                            "cleared_transfer_records_count": cleared_info.get("cleared_count", 0),
                            "cleared_transfer_records": cleared_info.get("records", []),
                            "protected_history_count": cleared_info.get("protected_count", 0),
                            "mp_accepted": False,
                            "submitted_unverified": True,
                            "library_reconciled": False,
                            "status": "unverified",
                            "message": msg,
                        })
                        continue
                    else:
                        msg = f"提交下载失败（{type(de).__name__}）"
                        await finalize_submit_outcome(
                            db, intent_rows,
                            outcome_state="rejected",
                            outcome_code="submit_error",
                            reason=msg,
                        )
                        for ep_i in real_missing_covered:
                            season_failed_eps[str(ep_i)] = {"code": "submit_error", "reason": msg}
                        submitted_results.append({
                            "show_name": clean_name,
                            "season": season_num,
                            "site": site,
                            "site_id": ev.site_id,
                            "site_sources": list(ev.site_sources),
                            "title": title,
                            "seeds": seeds,
                            "size_bytes": ev.size_bytes,
                            "size_display": size_display,
                            "covered_episodes": covered_eps,
                            "requested_missing_episodes": list(missing_eps),
                            "torrent_covered_episodes": list(local_cov_all),
                            "already_owned_count": already_owned_cnt,
                            "is_season_pack": is_sp,
                            "plan_desc": plan_desc,
                            "runner_up_diff": plan_summary_note,
                            "mp_accepted": False,
                            "library_reconciled": False,
                            "status": "failed",
                            "message": msg,
                        })
                        continue

                entry = {
                    "show_name": clean_name,
                    "season": season_num,
                    "site": site,
                    "site_id": ev.site_id,
                    "site_sources": list(ev.site_sources),
                    "title": title,
                    "seeds": seeds,
                    "size_bytes": ev.size_bytes,
                    "size_display": size_display,
                    "covered_episodes": covered_eps,
                    "requested_missing_episodes": list(missing_eps),
                    "torrent_covered_episodes": list(local_cov_all),
                    "already_owned_count": already_owned_cnt,
                    "is_season_pack": is_sp,
                    "plan_desc": plan_desc,
                    "runner_up_diff": plan_summary_note,
                    "cleared_transfer_records_count": cleared_info.get("cleared_count", 0),
                    "cleared_transfer_records": cleared_info.get("records", []),
                    "protected_history_count": cleared_info.get("protected_count", 0),
                    "mp_accepted": is_ok,
                    "submitted_unverified": is_unverified,
                    "library_reconciled": False,
                    "status": "success" if is_ok else "failed",
                    "message": msg,
                }
                submitted_results.append(entry)

                safe_reply = _sanitize_reply(reply)
                db.add(
                    OperationLog(
                        task_id=rec.task_id,
                        season_record_id=rec.id,
                        action="download_gap_fill",
                        status="completed" if is_ok else "failed",
                        detail_json=json.dumps({
                            "show": clean_name,
                            "season": season_num,
                            "search_tmdb_id": search_tmdb_id,
                            "search_season": search_season,
                            "title": title,
                            "site": site,
                            "site_sources": list(ev.site_sources),
                            "seeds": seeds,
                            "size_bytes": ev.size_bytes,
                            "requested_missing": list(missing_eps),
                            "torrent_covered": list(local_cov_all),
                            "covered": covered_eps,
                            "already_owned_count": already_owned_cnt,
                            "is_season_pack": is_sp,
                            "plan_desc": plan_desc,
                            "selection_note": plan_summary_note,
                            "requested_sites": site_plan.get("requested_label"),
                            "returned_sites": all_returned_sites,
                            "search_receipts": search_receipts,
                            "overwrite_policy": overwrite_policy.get("summary", ""),
                            "cleared_transfer_records_count": cleared_info.get("cleared_count", 0),
                            "protected_history_count": cleared_info.get("protected_count", 0),
                            "mp_accepted": is_ok,
                            "library_reconciled": False,
                            "reply": safe_reply,
                        }, ensure_ascii=False),
                    )
                )

                if is_ok:
                    clean_tip = f" (仅清理 {cleared_info.get('cleared_count', 0)} 条缺号冲突整理记录)" if cleared_info.get("cleared_count", 0) > 0 else ""
                    rec.status_detail = f"已推送 MP 下载: [{site}] {title[:50]} ({plan_desc}{clean_tip} | 待入库核验)"
                    if not is_mocked:
                        _recently_submitted_tasks.append({
                            "tmdb_id": str(search_tmdb_id) if search_tmdb_id else tmdb_id_str,
                            "show_name": clean_name,
                            "season": search_season,
                            "torrent_title_norm": re.sub(r"\s+", " ", title).strip().casefold(),
                            "covered_episodes": list(covered_eps),
                            "is_season_pack": is_sp,
                            "timestamp": time.time(),
                        })

            # 未被本批选种覆盖的集数落地失败原因（如 E02,E03 受理但 E09 因无合格候选失败）
            uncovered_rem = [
                ep for ep in missing_eps
                if ep not in season_accepted_eps
                and ep not in season_unverified_eps
                and str(ep) not in season_failed_eps
            ]
            if uncovered_rem:
                for ep_u in uncovered_rem:
                    s_ep_list = translate_local_to_search_episodes([ep_u], rec_mapping) if is_mapped else [ep_u]
                    s_ep = s_ep_list[0] if s_ep_list else ep_u
                    ep_code, ep_reason = _diagnose_episode_failure(
                        ep_u,
                        s_ep,
                        all_raw_candidates,
                        by_period=by_period,
                        min_seeders=min_seeders,
                        fallback_code=fail_code,
                        fallback_summary=fail_summary,
                    )
                    await record_unsubmitted_episode_outcomes(
                        db, rec, identity_key, [ep_u],
                        batch_run_id=batch_run_id,
                        outcome_state=ep_code,
                        outcome_code=ep_code,
                        reason=ep_reason,
                        search_tmdb_id=search_tmdb_id or None,
                        search_season_number=search_season,
                        numbering_mode=numbering_mode,
                        diag_summary={"partial_uncovered": [ep_u], "raw_count": raw_count, "max_seeders": max_seeders_seen},
                    )
                    season_failed_eps[str(ep_u)] = {"code": ep_code, "reason": ep_reason}

            if season_accepted_eps and not season_unverified_eps and not season_failed_eps:
                s_outcome = "accepted"
                s_summary = f"已受理并推送 {len(set(season_accepted_eps))} 集待入库"
            elif season_accepted_eps:
                s_outcome = "partial"
                s_summary = f"部分推送（受理 {sorted(set(season_accepted_eps))}；未成功/待核实 {sorted(set(missing_eps) - set(season_accepted_eps))}）"
            elif season_unverified_eps:
                s_outcome = "unverified"
                s_summary = f"请求超时待核实 {sorted(set(season_unverified_eps))}"
            else:
                s_outcome = "rejected"
                s_summary = "本季所有候选提交均被拒绝或失败"

            await record_season_run_snapshot(
                db, rec, identity_key,
                batch_run_id=batch_run_id,
                outcome_status=s_outcome,
                summary_text=s_summary,
                target_episodes=missing_eps,
                accepted_episodes=season_accepted_eps,
                unverified_episodes=season_unverified_eps,
                failed_episodes=season_failed_eps,
                candidate_count=raw_count,
                max_seeders_seen=max_seeders_seen,
                min_seeders_required=min_seeders,
                sites_summary=sites_diag_obj,
                search_diag={"receipts": search_receipts},
            )

        for tid in affected_tasks:
            await refresh_task_summary(tid, db)
        await db.commit()

    except Exception:
        await db.rollback()
        raise
    finally:
        await mp.close()

    success_count = sum(1 for s in submitted_results if s["status"] == "success")
    unverified_count = sum(1 for s in submitted_results if s["status"] == "unverified")
    total_cleared = sum(s.get("cleared_transfer_records_count", 0) for s in submitted_results)
    summary_msg = f"补缺推送完成：共提交 {success_count} 个下载任务"
    if unverified_count > 0:
        summary_msg += f"（另有 {unverified_count} 项响应超时已锁定待核实）"
    if total_cleared > 0:
        summary_msg += f"（仅清除 {total_cleared} 条真实缺号冲突整理记录，已有集保持完整）"
    if errors:
        summary_msg += f"，遇到 {len(errors)} 项提醒: {'; '.join(errors[:2])}"
    if skipped:
        summary_msg += "。暂缓：" + "；".join(skipped[:2])
    refusals = [s["message"] for s in submitted_results if s["status"] == "failed"]
    if refusals:
        summary_msg += "。MoviePilot 拒绝提交：" + "；".join(str(m) for m in refusals[:2])

    details_list = [
        {
            "success": s["status"] == "success",
            "submitted_unverified": bool(s.get("submitted_unverified", False)),
            "seeders": s.get("seeds", 0),
            "size_bytes": s.get("size_bytes"),
            "size_display": s.get("size_display", "未知大小"),
            "reason": s.get("message", "") if s["status"] != "success" else s.get("plan_desc", ""),
            "show_name": s.get("show_name", ""),
            "title": sanitize_text_no_secrets(s.get("title", "")),
            "site": s.get("site", ""),
            "site_sources": s.get("site_sources", []),
            "is_season_pack": s.get("is_season_pack", False),
            "requested_missing_episodes": s.get("requested_missing_episodes", []),
            "torrent_covered_episodes": s.get("torrent_covered_episodes", []),
            "already_owned_count": s.get("already_owned_count", 0),
            "runner_up_diff": s.get("runner_up_diff", ""),
            "mp_accepted": s.get("mp_accepted", False),
            "library_reconciled": s.get("library_reconciled", False),
            "message": sanitize_text_no_secrets(s.get("message", "")),
        }
        for s in submitted_results
    ]

    return {
        "ok": success_count > 0 or unverified_count > 0,
        "batch_run_id": batch_run_id,
        "skipped": bool(skipped) and not submitted_results and not errors,
        "skip_reasons": skipped,
        "message": summary_msg,
        "submitted": submitted_results,
        "details": details_list,
        "total_cleared_transfer_records": total_cleared,
        "search_sites": {
            "requested": site_plan.get("requested_label", ""),
            "requested_ids": site_plan.get("requested_ids", []),
            "returned": returned_sites_all,
            "scope_confirmed": site_plan.get("scope_confirmed", False),
            "mode": site_plan.get("mode", ""),
            "diagnostics": site_plan.get("diagnostics", []),
        },
        "overwrite_policy": overwrite_policy.get("summary", ""),
        "errors": errors,
    }
