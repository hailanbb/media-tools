"""同一 TMDB ID 多目录索引、异名目录判定、组级确认与安全门禁模块。"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.directory_group import DirectoryGroup
from app.models.metadata_cache import MetadataCache
from app.models.season import SeasonRecord

_TMDB_TAG_RE = re.compile(r"\[tmdbid[=:](\d+)\]", re.IGNORECASE)


def extract_dir_tmdb_id(name: str | None) -> str:
    """从剧集目录名提取显式标注的 TMDB ID。"""
    if not name:
        return ""
    m = _TMDB_TAG_RE.search(str(name))
    return m.group(1) if m else ""


def normalize_dir_path(path: str | None) -> str:
    """统一目录路径格式，消除尾斜杠、多余分隔符与 Unicode 组合字符差异。"""
    if not path:
        return ""
    raw = unicodedata.normalize("NFC", str(path).strip()).rstrip("/")
    if not raw:
        return "/"
    return posixpath.normpath(raw)


def normalize_dir_basename(name: str | None) -> str:
    """归一化目录 basename，忽略 [tmdbid=...]、年份、大小写、全半角、空格与标点。"""
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name).strip())
    s = re.sub(r"\[tmdbid[=:]\d+\]", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\{tmdb[-=:]\d+\}", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"[（(]\s*(?:19|20)\d{2}\s*[)）]", " ", s)
    s = re.sub(r"[\W_]+", "", s)
    return s.casefold()


def is_substantive_alias_dir(name_a: str | None, name_b: str | None) -> bool:
    """判断两个目录名是否存在实质性别名差异（忽略大小写/全半角/空格/标点/年份/TMDB标签）。"""
    na = normalize_dir_basename(name_a)
    nb = normalize_dir_basename(name_b)
    if not na or not nb:
        return False
    return na != nb


def resolve_category_key(show_path: str, source_root: str = "", archive_root: str = "") -> str:
    """计算剧集目录所属的相对分类键（用于隔离不同分类，不跨分类误合并）。"""
    norm_show = normalize_dir_path(show_path)
    parent = posixpath.dirname(norm_show)
    for root in (source_root, archive_root):
        norm_root = normalize_dir_path(root)
        if norm_root and norm_root != "/" and (parent == norm_root or parent.startswith(norm_root + "/")):
            rel = parent[len(norm_root):].lstrip("/")
            return f"cat:{rel or '_root_'}"
    return f"parent:{posixpath.basename(parent) or parent}"


def build_physical_dir_entry(
    *,
    side: str,
    category: str,
    show_path: str,
    tmdb_id: str | int,
    stable_id: str = "",
    seasons: list[int] | None = None,
    episodes_by_season: dict[str, list[int]] | None = None,
) -> dict[str, Any]:
    """构建单条物理剧集目录索引记录。"""
    norm_path = normalize_dir_path(show_path)
    raw_name = posixpath.basename(norm_path)
    return {
        "side": side,
        "category": category,
        "norm_path": norm_path,
        "raw_name": raw_name,
        "stable_id": str(stable_id or "").strip(),
        "tmdb_id": str(tmdb_id),
        "seasons": sorted({int(s) for s in (seasons or [])}),
        "episodes_by_season": {
            str(k): sorted({int(e) for e in v})
            for k, v in (episodes_by_season or {}).items()
        },
    }


def merge_physical_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (side, norm_path) 与相同非空 stable_id 去重合并目录索引条目，合并季集覆盖。"""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    stable_seen: dict[tuple[str, str], tuple[str, str]] = {}

    for ent in entries:
        side = str(ent.get("side") or "source")
        norm_path = normalize_dir_path(ent.get("norm_path") or "")
        if not norm_path:
            continue
        sid = str(ent.get("stable_id") or "").strip()
        if sid and (side, sid) in stable_seen:
            key = stable_seen[(side, sid)]
        else:
            key = (side, norm_path)
            if sid:
                stable_seen[(side, sid)] = key

        existing = by_key.get(key)
        if existing is None:
            by_key[key] = {
                "side": side,
                "category": str(ent.get("category") or ""),
                "norm_path": norm_path,
                "raw_name": str(ent.get("raw_name") or posixpath.basename(norm_path)),
                "stable_id": sid,
                "tmdb_id": str(ent.get("tmdb_id") or ""),
                "seasons": sorted({int(s) for s in (ent.get("seasons") or [])}),
                "episodes_by_season": {
                    str(k): sorted({int(e) for e in v})
                    for k, v in (ent.get("episodes_by_season") or {}).items()
                },
            }
        else:
            existing["seasons"] = sorted(
                set(existing["seasons"]) | {int(s) for s in (ent.get("seasons") or [])}
            )
            ep_map = existing["episodes_by_season"]
            for k, vals in (ent.get("episodes_by_season") or {}).items():
                ep_map[str(k)] = sorted(set(ep_map.get(str(k), [])) | {int(e) for e in vals})
            # 若同一 stable_id 发生路径改名，保留最新路径
            if sid and norm_path != existing["norm_path"]:
                existing["norm_path"] = norm_path
                existing["raw_name"] = posixpath.basename(norm_path)
    return list(by_key.values())


def compute_group_paths_signature(entries: list[dict[str, Any]]) -> str:
    """生成目录集合签名；若新增第 3 个目录、路径缺失或 stable_id 变化则签名立即失效。"""
    canonical = sorted(
        (
            str(e.get("side") or ""),
            normalize_dir_path(e.get("norm_path") or ""),
            str(e.get("stable_id") or ""),
        )
        for e in entries
    )
    payload = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _derive_archive_show_path(
    show_path: str,
    category: str = "",
    source_root: str = "",
    archive_root: str = "",
) -> str:
    """将源端剧集目录路径映射为归档库下的对应剧集目标目录（仅推导字符串，不触盘）。"""
    norm_show = normalize_dir_path(show_path)
    norm_src = normalize_dir_path(source_root) if source_root else ""
    norm_arc = normalize_dir_path(archive_root) if archive_root else ""
    if not norm_arc or norm_arc == "/":
        return norm_show
    if norm_src and norm_src != "/" and (norm_show == norm_src or norm_show.startswith(norm_src + "/")):
        rel = norm_show[len(norm_src):].lstrip("/")
        return normalize_dir_path(f"{norm_arc}/{rel}")
    base_name = posixpath.basename(norm_show)
    if category.startswith("cat:"):
        rel_cat = "" if category == "cat:_root_" else category[4:].strip("/")
        return normalize_dir_path(f"{norm_arc}/{rel_cat}/{base_name}" if rel_cat else f"{norm_arc}/{base_name}")
    return normalize_dir_path(f"{norm_arc}/{base_name}")


def build_directory_group_target_options(
    entries: list[dict[str, Any]],
    *,
    category: str = "",
    tmdb_id: str = "",
    tmdb_meta: dict[str, Any] | None = None,
    source_root: str = "",
    archive_root: str = "",
) -> dict[str, Any]:
    """构建可供前端单选勾选的归档目标候选列表，并自动识别与 TMDB 官网页面名称一致的默认推荐项。"""
    meta = tmdb_meta if isinstance(tmdb_meta, dict) else {}
    canonical_title = str(meta.get("name") or "").strip()
    canonical_original = str(meta.get("original_name") or "").strip()
    canonical_year = str(meta.get("first_air_date") or "")[:4].strip()
    canonical_norm = normalize_dir_basename(canonical_title)

    by_target: dict[str, dict[str, Any]] = {}

    def _matches_tmdb(raw_name: str) -> bool:
        if not canonical_title:
            return False
        base = str(raw_name or "").strip()
        if not base:
            return False
        clean_title = normalize_dir_basename(base)
        if canonical_norm and clean_title and clean_title == canonical_norm:
            return True
        return base.casefold() == canonical_title.casefold()

    for ent in entries:
        if ent.get("side") != "archive":
            continue
        tpath = normalize_dir_path(ent.get("norm_path") or "")
        if not tpath:
            continue
        rname = str(ent.get("raw_name") or posixpath.basename(tpath))
        by_target[tpath] = {
            "target_path": tpath,
            "raw_name": rname,
            "option_kind": "existing_archive",
            "option_label": "归档库现有目录",
            "exists_in_archive": True,
            "source_show_paths": [],
            "seasons": sorted({int(s) for s in (ent.get("seasons") or [])}),
            "matches_tmdb_page": _matches_tmdb(rname),
            "is_recommended": False,
        }

    for ent in entries:
        if ent.get("side") != "source":
            continue
        spath = normalize_dir_path(ent.get("norm_path") or "")
        if not spath:
            continue
        rname = str(ent.get("raw_name") or posixpath.basename(spath))
        derived = _derive_archive_show_path(
            spath,
            category=category or str(ent.get("category") or ""),
            source_root=source_root,
            archive_root=archive_root,
        )
        if not derived:
            continue
        existing = by_target.get(derived)
        if existing is not None:
            if spath not in existing["source_show_paths"]:
                existing["source_show_paths"].append(spath)
            existing["seasons"] = sorted(
                set(existing["seasons"]) | {int(s) for s in (ent.get("seasons") or [])}
            )
            if _matches_tmdb(rname):
                existing["matches_tmdb_page"] = True
        else:
            by_target[derived] = {
                "target_path": derived,
                "raw_name": rname,
                "option_kind": "from_source_name",
                "option_label": "按源端目录名归档",
                "exists_in_archive": False,
                "source_show_paths": [spath],
                "seasons": sorted({int(s) for s in (ent.get("seasons") or [])}),
                "matches_tmdb_page": _matches_tmdb(rname),
                "is_recommended": False,
            }

    candidates = list(by_target.values())
    if canonical_title and not any(c["matches_tmdb_page"] for c in candidates):
        parent_dir = posixpath.dirname(candidates[0]["target_path"]) if candidates else ""
        if parent_dir:
            yr_part = f" ({canonical_year})" if canonical_year.isdigit() else ""
            tid_part = f" [tmdbid={tmdb_id}]" if str(tmdb_id).isdigit() else ""
            synth_name = f"{canonical_title}{yr_part}{tid_part}"
            synth_path = normalize_dir_path(f"{parent_dir}/{synth_name}")
            if synth_path not in by_target:
                candidates.append(
                    {
                        "target_path": synth_path,
                        "raw_name": synth_name,
                        "option_kind": "tmdb_canonical_new",
                        "option_label": "TMDB 官网正名标准目录",
                        "exists_in_archive": False,
                        "source_show_paths": [],
                        "seasons": [],
                        "matches_tmdb_page": True,
                        "is_recommended": False,
                    }
                )

    def _pick_priority(c: dict[str, Any]) -> tuple[int, int, int, str]:
        # 优先：与 TMDB 页面一致 + 归档库现有；其次：与 TMDB 页面一致；再次：归档库现有；最后按路径排序
        return (
            0 if (c["matches_tmdb_page"] and c["exists_in_archive"]) else 1,
            0 if c["matches_tmdb_page"] else 1,
            0 if c["exists_in_archive"] else 1,
            c["target_path"],
        )

    candidates.sort(key=_pick_priority)
    suggested_target = candidates[0]["target_path"] if candidates else ""
    for c in candidates:
        c["is_recommended"] = bool(suggested_target and c["target_path"] == suggested_target)

    return {
        "canonical_tmdb_title": canonical_title,
        "canonical_tmdb_original_name": canonical_original,
        "canonical_tmdb_year": canonical_year,
        "candidate_targets": candidates,
        "suggested_target_path": suggested_target,
    }


def classify_tmdb_directory_group(
    entries: list[dict[str, Any]],
    *,
    tmdb_meta: dict[str, Any] | None = None,
    source_root: str = "",
    archive_root: str = "",
) -> dict[str, Any] | None:
    """根据同分类同 TMDB ID 下的物理目录集合判定是否需要生成组级复核/确认。"""
    merged = merge_physical_entries(entries)
    if not merged:
        return None
    tmdb_id = str(merged[0].get("tmdb_id") or "")
    category = str(merged[0].get("category") or "")
    if not tmdb_id:
        return None

    source_entries = sorted(
        [e for e in merged if e["side"] == "source"],
        key=lambda x: x["norm_path"],
    )
    archive_entries = sorted(
        [e for e in merged if e["side"] == "archive"],
        key=lambda x: x["norm_path"],
    )

    group_kind = ""
    title = ""

    if len(source_entries) >= 2:
        group_kind = "multi_source_dirs"
        title = "同一作品有多个目录"
    elif len(archive_entries) >= 2:
        group_kind = "multi_archive_dirs"
        title = "同一作品在归档库有多个目录"
    elif len(source_entries) == 1 and len(archive_entries) == 1:
        if is_substantive_alias_dir(source_entries[0]["raw_name"], archive_entries[0]["raw_name"]):
            group_kind = "alias_dir_confirm"
            title = "同 ID 异名目录"
        else:
            return None
    else:
        # 仅 1 个源端目录且无物理归档目录（只有计算出的预期目标路径）时，不报警
        return None

    all_entries = source_entries + archive_entries
    season_nums: set[int] = set()
    for e in all_entries:
        season_nums.update(int(s) for s in (e.get("seasons") or []))

    settings = get_settings()
    opts = build_directory_group_target_options(
        all_entries,
        category=category,
        tmdb_id=tmdb_id,
        tmdb_meta=tmdb_meta,
        source_root=source_root or settings.source_root or "",
        archive_root=archive_root or settings.archive_root or "",
    )

    return {
        "tmdb_id": tmdb_id,
        "category_key": category,
        "group_kind": group_kind,
        "title": title,
        "entries": all_entries,
        "source_count": len(source_entries),
        "archive_count": len(archive_entries),
        "paths_signature": compute_group_paths_signature(all_entries),
        "suggested_target_path": opts["suggested_target_path"],
        "canonical_tmdb_title": opts["canonical_tmdb_title"],
        "canonical_tmdb_original_name": opts["canonical_tmdb_original_name"],
        "canonical_tmdb_year": opts["canonical_tmdb_year"],
        "candidate_targets": opts["candidate_targets"],
        "season_count": len(season_nums),
    }


def _group_kind_label(kind: str) -> str:
    return {
        "multi_source_dirs": "同一作品多源端目录",
        "multi_archive_dirs": "同一作品多归档目录",
        "alias_dir_confirm": "同 ID 异名目录",
    }.get(kind, "多目录组")


def serialize_directory_group(group: DirectoryGroup) -> dict[str, Any]:
    """序列化 DirectoryGroup ORM 对象供 API 与前端渲染使用。"""
    try:
        entries = json.loads(group.paths_json or "[]")
    except Exception:
        entries = []
    try:
        ev = json.loads(group.evidence_json or "{}")
    except Exception:
        ev = {}
    if not isinstance(ev, dict):
        ev = {}
    candidates = ev.get("candidate_targets") if isinstance(ev.get("candidate_targets"), list) else []
    return {
        "id": group.id,
        "task_id": group.task_id,
        "tmdb_id": group.tmdb_id,
        "category_key": group.category_key,
        "group_kind": group.group_kind,
        "group_label": _group_kind_label(group.group_kind),
        "title": group.title or _group_kind_label(group.group_kind),
        "paths_signature": group.paths_signature,
        "entries": entries if isinstance(entries, list) else [],
        "suggested_target_path": group.suggested_target_path or "",
        "confirmed_target_path": group.confirmed_target_path or "",
        "confirmation_mode": group.confirmation_mode or "",
        "status": group.status,
        "requires_confirm": group.status == "pending_confirm",
        "invalidated_reason": group.invalidated_reason or "",
        "evidence": ev,
        "canonical_tmdb_title": str(ev.get("canonical_tmdb_title") or ""),
        "canonical_tmdb_original_name": str(ev.get("canonical_tmdb_original_name") or ""),
        "canonical_tmdb_year": str(ev.get("canonical_tmdb_year") or ""),
        "candidate_targets": candidates,
        "season_count": group.season_count or 0,
        "confirmed_at": group.confirmed_at.isoformat() if group.confirmed_at else None,
        "updated_at": group.updated_at.isoformat() if group.updated_at else None,
    }


def enrich_directory_group_dict(
    group_dict: dict[str, Any],
    *,
    tmdb_meta: dict[str, Any] | None = None,
    source_root: str = "",
    archive_root: str = "",
) -> dict[str, Any]:
    """用最新 TMDB 元数据与源/归档根路径实时补全多目录组的候选项列表与默认推荐项。"""
    entries = group_dict.get("entries") if isinstance(group_dict.get("entries"), list) else []
    tmdb_id = str(group_dict.get("tmdb_id") or "")
    category = str(group_dict.get("category_key") or "")
    meta = dict(tmdb_meta) if isinstance(tmdb_meta, dict) else {}
    if not meta.get("name") and group_dict.get("canonical_tmdb_title"):
        meta["name"] = group_dict["canonical_tmdb_title"]
    if not meta.get("original_name") and group_dict.get("canonical_tmdb_original_name"):
        meta["original_name"] = group_dict["canonical_tmdb_original_name"]
    if not meta.get("first_air_date") and group_dict.get("canonical_tmdb_year"):
        meta["first_air_date"] = f"{group_dict['canonical_tmdb_year']}-01-01"

    opts = build_directory_group_target_options(
        entries,
        category=category,
        tmdb_id=tmdb_id,
        tmdb_meta=meta if meta else None,
        source_root=source_root,
        archive_root=archive_root,
    )
    ev = dict(group_dict.get("evidence") or {})
    if opts["canonical_tmdb_title"]:
        ev["canonical_tmdb_title"] = opts["canonical_tmdb_title"]
    if opts["canonical_tmdb_original_name"]:
        ev["canonical_tmdb_original_name"] = opts["canonical_tmdb_original_name"]
    if opts["canonical_tmdb_year"]:
        ev["canonical_tmdb_year"] = opts["canonical_tmdb_year"]
    if opts["candidate_targets"]:
        ev["candidate_targets"] = opts["candidate_targets"]

    suggested = str(group_dict.get("suggested_target_path") or "")
    if opts["suggested_target_path"] and (
        not suggested
        or not archive_root
        or suggested != opts["suggested_target_path"]
    ):
        suggested = opts["suggested_target_path"]

    return {
        **group_dict,
        "suggested_target_path": suggested,
        "canonical_tmdb_title": str(ev.get("canonical_tmdb_title") or ""),
        "canonical_tmdb_original_name": str(ev.get("canonical_tmdb_original_name") or ""),
        "canonical_tmdb_year": str(ev.get("canonical_tmdb_year") or ""),
        "candidate_targets": opts["candidate_targets"] or group_dict.get("candidate_targets") or [],
        "evidence": ev,
    }


async def enrich_directory_groups_from_db(
    db: AsyncSession,
    task_id: str,
    group_dicts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """结合 DB 中的 MetadataCache（tmdb_tv:<id>）与 SeasonRecord，补全历史任务多目录组的候选列表。"""
    if not group_dicts:
        return group_dicts

    import inspect as _inspect

    settings = get_settings()
    tids = sorted(
        {
            str(g.get("tmdb_id") or "").strip()
            for g in group_dicts
            if str(g.get("tmdb_id") or "").strip().isdigit()
        }
    )
    meta_by_tid: dict[str, dict[str, Any]] = {}
    scalars_fn = getattr(db, "scalars", None)

    if tids and callable(scalars_fn):
        keys = [f"tmdb_tv:{tid}" for tid in tids]
        q = scalars_fn(select(MetadataCache).where(MetadataCache.cache_key.in_(keys)))
        if _inspect.isawaitable(q):
            for mc in (await q).all():
                try:
                    payload = json.loads(mc.payload_json or "{}")
                except Exception:
                    payload = {}
                if isinstance(payload, dict) and payload.get("name"):
                    tid_str = str(mc.media_id or mc.cache_key.split(":", 1)[-1]).strip()
                    meta_by_tid[tid_str] = payload

    missing_tids = {t for t in tids if not meta_by_tid.get(t, {}).get("name")}
    arc_parent_by_show: dict[str, str] = {}
    if callable(scalars_fn) and (missing_tids or not settings.archive_root):
        q_recs = scalars_fn(select(SeasonRecord).where(SeasonRecord.task_id == task_id))
        if _inspect.isawaitable(q_recs):
            for rec in (await q_recs).all():
                rtid = str(rec.tmdb_id or "").strip()
                if rtid in missing_tids and rec.evidence_json:
                    try:
                        ev = json.loads(rec.evidence_json or "{}")
                    except Exception:
                        ev = {}
                    mw = (ev or {}).get("matched_work") or {}
                    if isinstance(mw, dict) and mw.get("name"):
                        meta_by_tid[rtid] = {
                            "name": mw.get("name") or "",
                            "original_name": mw.get("original_name") or "",
                            "first_air_date": mw.get("first_air_date") or "",
                        }
                        missing_tids.discard(rtid)
                if not settings.archive_root and rec.show_path and rec.archive_target_path:
                    n_show = normalize_dir_path(rec.show_path)
                    n_arc_t = normalize_dir_path(rec.archive_target_path)
                    if normalize_dir_path(rec.season_path) != n_show:
                        n_arc_show = posixpath.dirname(n_arc_t)
                    else:
                        n_arc_show = n_arc_t
                    if n_arc_show and "/" in n_arc_show:
                        arc_parent_by_show[n_show] = posixpath.dirname(n_arc_show)

    enriched: list[dict[str, Any]] = []
    for g in group_dicts:
        tid = str(g.get("tmdb_id") or "").strip()
        src_root = settings.source_root or ""
        arc_root = settings.archive_root or ""
        if not arc_root:
            for ent in g.get("entries") or []:
                if isinstance(ent, dict) and ent.get("side") == "source":
                    n_show = normalize_dir_path(ent.get("norm_path") or "")
                    if n_show in arc_parent_by_show:
                        arc_root = arc_parent_by_show[n_show]
                        src_root = posixpath.dirname(n_show)
                        break
        enriched.append(
            enrich_directory_group_dict(
                g,
                tmdb_meta=meta_by_tid.get(tid),
                source_root=src_root,
                archive_root=arc_root,
            )
        )
    return enriched


async def sync_directory_groups_for_task(
    db: AsyncSession,
    task_id: str,
    detected_groups: list[dict[str, Any]],
    *,
    partial_keys: set[tuple[str, str]] | None = None,
) -> list[DirectoryGroup]:
    """同步任务下检测到的同 TMDB 多目录/异名目录组，自动保留有效确认并在路径变化时自动失效。"""
    settings = get_settings()
    existing_rows = list(
        (
            await db.scalars(
                select(DirectoryGroup).where(DirectoryGroup.task_id == task_id)
            )
        ).all()
    )
    if not settings.enable_multi_dir_governance:
        for old in existing_rows:
            await db.delete(old)
        await db.flush()
        return []

    existing_by_key: dict[tuple[str, str], DirectoryGroup] = {
        (row.category_key, row.tmdb_id): row for row in existing_rows
    }
    kept_keys: set[tuple[str, str]] = set()
    active_groups: list[DirectoryGroup] = []

    for g in detected_groups:
        key = (str(g["category_key"]), str(g["tmdb_id"]))
        kept_keys.add(key)
        entries = g.get("entries") or []
        new_sig = str(g.get("paths_signature") or compute_group_paths_signature(entries))
        encoded_entries = json.dumps(entries, ensure_ascii=False)
        archive_paths = {
            normalize_dir_path(e.get("norm_path") or "")
            for e in entries
            if e.get("side") == "archive"
        }

        ev_payload = {
            "source_count": g.get("source_count", 0),
            "archive_count": g.get("archive_count", 0),
            "canonical_tmdb_title": str(g.get("canonical_tmdb_title") or ""),
            "canonical_tmdb_original_name": str(g.get("canonical_tmdb_original_name") or ""),
            "canonical_tmdb_year": str(g.get("canonical_tmdb_year") or ""),
            "candidate_targets": g.get("candidate_targets") if isinstance(g.get("candidate_targets"), list) else [],
        }
        encoded_ev = json.dumps(ev_payload, ensure_ascii=False)

        row = existing_by_key.get(key)
        if row is None:
            row = DirectoryGroup(
                task_id=task_id,
                tmdb_id=str(g["tmdb_id"]),
                category_key=str(g["category_key"]),
                group_kind=str(g["group_kind"]),
                title=str(g.get("title") or _group_kind_label(str(g["group_kind"]))),
                paths_signature=new_sig,
                paths_json=encoded_entries,
                suggested_target_path=str(g.get("suggested_target_path") or ""),
                confirmed_target_path="",
                confirmation_mode="",
                status="pending_confirm",
                invalidated_reason="",
                evidence_json=encoded_ev,
                season_count=int(g.get("season_count") or 0),
            )
            db.add(row)
        else:
            was_confirmed = row.status in ("confirmed", "coexist")
            invalidated_reason = ""
            if was_confirmed:
                if row.paths_signature != new_sig:
                    invalidated_reason = "同 TMDB ID 下的物理目录集合发生变化（新增、移除或改名），原确认已自动失效，请重新确认"
                elif (
                    row.confirmation_mode == "use_archive_target"
                    and row.confirmed_target_path
                    and archive_paths
                    and normalize_dir_path(row.confirmed_target_path) not in archive_paths
                ):
                    invalidated_reason = "已确认的归档目标目录在归档库中已变更或消失，原确认已自动失效，请重新确认"

            row.group_kind = str(g["group_kind"])
            row.title = str(g.get("title") or _group_kind_label(str(g["group_kind"])))
            row.paths_signature = new_sig
            row.paths_json = encoded_entries
            row.suggested_target_path = str(g.get("suggested_target_path") or "")
            row.season_count = int(g.get("season_count") or 0)
            row.evidence_json = encoded_ev
            if invalidated_reason:
                row.status = "pending_confirm"
                row.confirmation_mode = ""
                row.confirmed_target_path = ""
                row.confirmed_at = None
                row.invalidated_reason = invalidated_reason

        active_groups.append(row)

    for key, old in existing_by_key.items():
        if key not in kept_keys:
            if partial_keys is not None and key not in partial_keys:
                active_groups.append(old)
                continue
            await db.delete(old)

    await db.flush()
    return active_groups


async def find_blocking_group_for_record(
    db: AsyncSession,
    rec: SeasonRecord,
    *,
    cd2_client=None,
    operation: str = "",
) -> DirectoryGroup | None:
    """检查单季记录是否受未确认或已失效的同 TMDB 多目录/异名目录组阻塞。"""
    settings = get_settings()
    if not settings.enable_multi_dir_governance:
        return None

    import inspect as _inspect

    scalars_fn = getattr(db, "scalars", None)
    if not callable(scalars_fn):
        return None
    scalars_call = scalars_fn(
        select(DirectoryGroup).where(DirectoryGroup.task_id == rec.task_id)
    )
    if not _inspect.isawaitable(scalars_call):
        return None
    groups = list((await scalars_call).all())
    if not groups:
        return None

    norm_show = normalize_dir_path(rec.show_path)
    rec_tid = str(rec.tmdb_id or "").strip()

    matched_group: DirectoryGroup | None = None
    for g in groups:
        try:
            entries = json.loads(g.paths_json or "[]")
        except Exception:
            entries = []
        paths_in_group = {normalize_dir_path(e.get("norm_path") or "") for e in entries if isinstance(e, dict)}
        if norm_show in paths_in_group or (rec_tid and str(g.tmdb_id) == rec_tid and any(
            resolve_category_key(norm_show, settings.source_root, settings.archive_root) == g.category_key
            for _ in (1,)
        )):
            matched_group = g
            break

    if matched_group is None:
        return None

    # 若提供 cd2_client 且此前已确认，执行实时网盘快速复核：一旦目标消失或新目录出现，立即自动失效
    if cd2_client is not None and matched_group.status in ("confirmed", "coexist"):
        drifted = await _check_live_group_drift(cd2_client, matched_group, settings)
        if drifted:
            matched_group.status = "pending_confirm"
            matched_group.confirmation_mode = ""
            matched_group.confirmed_target_path = ""
            matched_group.confirmed_at = None
            matched_group.invalidated_reason = drifted
            await db.flush()
            return matched_group

    if matched_group.status == "pending_confirm":
        return matched_group

    # 对同 ID 异名目录标记"有意共存"时，允许常规查看但禁止把源端并入或当作相同目录误删冗余
    if (
        matched_group.status == "coexist"
        and matched_group.group_kind == "alias_dir_confirm"
        and operation == "clean_source_redundant"
    ):
        return matched_group

    return None


async def _check_live_group_drift(cd2_client, group: DirectoryGroup, settings) -> str:
    """在执行写入/删除前复核已确认目录组在网盘上是否仍与确认快照一致。"""
    try:
        entries = json.loads(group.paths_json or "[]")
    except Exception:
        return ""
    if not isinstance(entries, list) or not entries:
        return ""

    target_tid = str(group.tmdb_id or "").strip()
    source_parents = {
        posixpath.dirname(normalize_dir_path(e.get("norm_path") or ""))
        for e in entries
        if isinstance(e, dict) and e.get("side") == "source" and e.get("norm_path")
    }
    archive_parents = {
        posixpath.dirname(normalize_dir_path(e.get("norm_path") or ""))
        for e in entries
        if isinstance(e, dict) and e.get("side") == "archive" and e.get("norm_path")
    }

    live_source_paths: set[str] = set()
    for pdir in source_parents:
        try:
            items = await cd2_client.list_files(pdir, force_refresh=True)
        except Exception:
            return ""
        for it in items:
            if it.get("isDirectory") and target_tid and extract_dir_tmdb_id(it.get("name")) == target_tid:
                live_source_paths.add(normalize_dir_path(it.get("path") or f"{pdir}/{it.get('name')}"))

    live_archive_paths: set[str] = set()
    for pdir in archive_parents:
        try:
            items = await cd2_client.list_files(pdir, force_refresh=True)
        except Exception:
            return ""
        for it in items:
            if it.get("isDirectory") and target_tid and extract_dir_tmdb_id(it.get("name")) == target_tid:
                live_archive_paths.add(normalize_dir_path(it.get("path") or f"{pdir}/{it.get('name')}"))

    expected_src = {
        normalize_dir_path(e.get("norm_path") or "")
        for e in entries
        if isinstance(e, dict) and e.get("side") == "source"
    }
    expected_arc = {
        normalize_dir_path(e.get("norm_path") or "")
        for e in entries
        if isinstance(e, dict) and e.get("side") == "archive"
    }

    if (source_parents and live_source_paths != expected_src) or (
        archive_parents and live_archive_paths != expected_arc
    ):
        return "网盘中的同 TMDB 目录数量或路径已变化（新增第 3 个目录或目录被移动），原确认已自动失效，请重新确认"

    if (
        group.confirmation_mode == "use_archive_target"
        and group.confirmed_target_path
        and archive_parents
        and normalize_dir_path(group.confirmed_target_path) not in live_archive_paths
    ):
        return "已确认的归档目标目录已不存在，原确认已自动失效，请重新确认"

    return ""


async def confirm_directory_group(
    db: AsyncSession,
    group: DirectoryGroup,
    *,
    mode: str,
    target_path: str = "",
) -> DirectoryGroup:
    """持久化用户对同 TMDB 多目录/异名目录组的确认选择（绝不触发自动删移）。"""
    clean_mode = str(mode or "").strip()
    if clean_mode not in ("use_archive_target", "intentional_coexist"):
        raise ValueError(f"不支持的确认模式: {clean_mode}")

    norm_target = normalize_dir_path(target_path) if target_path else ""
    if clean_mode == "use_archive_target":
        if not norm_target:
            norm_target = normalize_dir_path(group.suggested_target_path or "")
        if not norm_target:
            raise ValueError("选择确认归档目标时必须指定有效的归档目标目录路径")
        group.status = "confirmed"
        group.confirmation_mode = "use_archive_target"
        group.confirmed_target_path = norm_target
    else:
        group.status = "coexist"
        group.confirmation_mode = "intentional_coexist"
        group.confirmed_target_path = ""

    group.invalidated_reason = ""
    group.confirmed_at = datetime.now(timezone.utc)
    await db.flush()

    # 若确认了目标归档剧集目录，同步更新关联源季记录的 archive_target_path 与 evidence 标记（仅改计划指向，不移动文件）
    try:
        entries = json.loads(group.paths_json or "[]")
    except Exception:
        entries = []
    source_paths = {
        normalize_dir_path(e.get("norm_path") or "")
        for e in entries
        if isinstance(e, dict) and e.get("side") == "source"
    }
    recs = list(
        (
            await db.scalars(
                select(SeasonRecord).where(SeasonRecord.task_id == group.task_id)
            )
        ).all()
    )
    for rec in recs:
        if normalize_dir_path(rec.show_path) not in source_paths:
            continue
        if clean_mode == "use_archive_target" and norm_target:
            if normalize_dir_path(rec.season_path) == normalize_dir_path(rec.show_path):
                rec.archive_target_path = norm_target
            else:
                season_dir = posixpath.basename(normalize_dir_path(rec.season_path))
                rec.archive_target_path = f"{norm_target}/{season_dir}"
        try:
            ev = json.loads(rec.evidence_json or "{}")
        except Exception:
            ev = {}
        if isinstance(ev, dict) and isinstance(ev.get("directory_group"), dict):
            ev["directory_group"] = {
                **ev["directory_group"],
                "status": group.status,
                "requires_confirm": False,
                "confirmation_mode": group.confirmation_mode,
                "confirmed_target_path": group.confirmed_target_path,
                "invalidated_reason": "",
            }
            rec.evidence_json = json.dumps(ev, ensure_ascii=False)

    await db.flush()
    return group
