"""媒体整理智能修复核心引擎 — 真实证据诊断、参数推导、破坏性副作用拦截、契约构造与预览核验。

严格遵循《MediaFlow媒体整理智能修复需求与交互方案-v2.5.40.md》：
- R01/R29/R30/R32: 接管基线隔离，仅接管启用后新失败，旧历史不导入、不自动重试；
- R03: 标准 S02E03 且身份明确时自动预览并准备整理；
- R04: 仅含 E03 且季号无法确定时，绝不盲目套 S01，提示补充一次季选择；
- R05: 同名电影与电视剧以类型隔离，电影不带季集参数；
- R06: 动漫 Ver1.1a - 11 排除版本号干扰，精准提取集号 11；
- R07: 绝对集号跨季复用已确认映射，offset 严格仅计算一次；
- R08/R09/R10: 多季展开、连集 E01-E02 不复制成两份、加更 part1/part2 保持分段语义不误转正片；
- R11/R12/R13: 目标已存在且核验合格则结案；同名不符或含 dest_fileitem 隐式删目标风险时前置拦截；
- R14/R15/R31: 区分存储离线与源文件不存在；源已不存在且无后续成功时标记 skipped/source_missing；
- R16/R17/R18/R20/R24: MP v2.15.6 ManualTransferItem 契约、逐项预览核验、状态漂移失效、伴随字幕清点与规则复用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import re
from datetime import datetime, timezone
from typing import Any

from app.core.episode_parser import SUBTITLE_EXTENSIONS

log = logging.getLogger("mediaflow.transfer_repair")

# 排除为纯集号的数字与关键词黑名单（年份、分辨率、编码、常见版本标记）
_NOISE_NUMBERS = {
    1970, 1980, 1990, 2000, 2010, 2015, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026, 2027, 2028,
    480, 576, 720, 1080, 2160, 4320, 264, 265, 8, 10,
}

# 常见版本标记过滤正则 (如 Ver1.1a, v2, v1.0, Build01)
_VERSION_TAG_REGEX = re.compile(
    r"(?i)\b(?:ver\.?|v)\d+(?:\.\d+)?[a-z]?\b|\bbuild\d+\b", re.IGNORECASE
)

# 常见分段标记 (part1, pt1, 上, 中, 下)
_PART_REGEX = re.compile(
    r"(?i)(?:[-._\s]|\b)(part\d+|pt\d+|上(?:集|期|段)?|中(?:集|期|段)?|下(?:集|期|段)?)(?:[-._\s]|\b|$)"
)

# 特辑与附加内容标识 (加更, 衍生, 纯享, 特别篇, 跨年, 花絮, SP, OVA, OAD)
_SPECIAL_REGEX = re.compile(
    r"(?i)(加更|纯享|衍生|番外|特辑|特别篇|花絮|彩蛋|跨年|预告|先行|片花|SP|OVA|OAD|NCOP|NCED)"
)

# FileItem 敏感字段脱敏列表（第 8.3 节：不得在日志或持久快照中留存临时链接/密钥）
_SENSITIVE_FILEITEM_KEYS = {"url", "download_url", "pickcode", "token", "cookie", "auth"}

# 合格视频最小有效大小阈值（10MB）
MIN_QUALIFIED_VIDEO_BYTES = 10 * 1024 * 1024


def normalize_file_path(path: str | None) -> str:
    """标准化文件路径，统一为正斜杠并去除冗余斜杠。"""
    if not path:
        return ""
    p = str(path).replace("\\", "/").strip()
    return posixpath.normpath(p)


def sanitize_fileitem(fi: Any) -> dict[str, Any]:
    """脱敏清洗 MP FileItem 对象，剔除临时下载链接、pickcode 等敏感字段。"""
    if not isinstance(fi, dict):
        return {}
    return {
        k: v
        for k, v in fi.items()
        if str(k).lower() not in _SENSITIVE_FILEITEM_KEYS and k != "children"
    }


def extract_raw_error(history_record: dict[str, Any]) -> str:
    """提取 MoviePilot 转移历史中的真实报错文本（MP v2.15.6 核心字段为 errmsg）。"""
    for key in ("errmsg", "error", "reason", "message"):
        val = history_record.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def classify_error_category(raw_error: str) -> str:
    """按真实报错文本归纳原始错误大类。"""
    err = str(raw_error or "").strip()
    low = err.lower()
    if not err:
        return "unknown"
    if "存在同名文件" in err or "不覆盖" in err or "已存在" in err:
        return "target_exists"
    if "未识别到文件集数" in err or "未识别集数" in err or "未识别到季" in err:
        return "no_episodes"
    if "未识别到媒体信息" in err or "未识别媒体" in err or "未找到媒体" in err:
        return "no_media_info"
    if "源文件不存在" in err or "文件不存在" in err or "no such file" in low:
        return "source_missing"
    if "copy" in low or "拷贝失败" in err or "移动失败" in err or "硬链接" in err or "permission" in low or "space" in low:
        return "copy_failed"
    if "timeout" in low or "超时" in err or "connection" in low or "502" in low or "503" in low:
        return "network_error"
    return "unknown"


def calculate_plan_hash(plan_dict: dict[str, Any]) -> str:
    """计算计划摘要哈希（剔除易变时间戳，确保相同业务计划哈希稳定，用于幂等与漂移比对）。"""
    stable = {
        k: v
        for k, v in plan_dict.items()
        if k not in ("diagnosed_at", "updated_at", "checked_at")
    }
    s = json.dumps(stable, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def build_idempotency_key(
    source_path: str,
    file_size: int,
    plan_hash: str,
    target_path: str = "",
) -> str:
    """构造持久化幂等键（包含源文件版本、目标路径与计划哈希，第 10.2 节）。"""
    raw = f"{normalize_file_path(source_path)}|{int(file_size or 0)}|{normalize_file_path(target_path)}|{plan_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def parse_clean_title(filename_or_title: str) -> str:
    """从文件名或原始标题中提取清晰剧名/作品名。"""
    raw = str(filename_or_title or "").strip()
    if not raw:
        return ""
    s = re.sub(r"^\[[^\]]+\]\s*", "", raw)
    s = re.sub(r"^\([^\)]+\)\s*", "", s)
    s = re.split(r"(?i)(?:[-._\s]|\b)(?:S\d+|EP?\d+|第\d+[季集话話])", s)[0]
    s = re.sub(r"\(\d{4}\)", "", s)
    s = re.sub(r"\[[^\]]+\]", "", s)
    s = re.sub(r"(?i)\b(4k|2160p|1080p|720p|hevc|x265|x264|web-dl|hdtv)\b.*", "", s)
    s = s.replace(".", " ").replace("_", " ").strip(" -")
    return s


def parse_mp_season_number(seasons_raw: Any) -> int | None:
    """解析 MP 历史记录中的 seasons 字段（如 'S01', '1', 'S02'）。"""
    if seasons_raw is None:
        return None
    s = str(seasons_raw).strip()
    if not s:
        return None
    m = re.search(r"(?i)S?(\d{1,2})", s)
    return int(m.group(1)) if m else None


def parse_mp_episode_numbers(episodes_raw: Any) -> list[int]:
    """解析 MP 历史记录中的 episodes 字段（如 'E06', 'E01-E02', '11,12'）。"""
    if episodes_raw is None:
        return []
    s = str(episodes_raw).strip()
    if not s:
        return []
    eps: set[int] = set()
    for m in re.finditer(r"(?i)E?(\d{1,4})(?:\s*[-~～]\s*E?(\d{1,4}))?", s):
        a = int(m.group(1))
        if m.group(2):
            b = int(m.group(2))
            eps.update(range(min(a, b), max(a, b) + 1))
        else:
            eps.add(a)
    return sorted(eps)


def extract_season_and_episodes(
    filename: str,
    parent_path: str = "",
    confirmed_offset: int = 0,
) -> tuple[int | None, list[int], str | None, bool, str | None]:
    """精准推导 (season_number, episode_numbers, part, is_special, special_type).

    保证：
    - 排除 Ver1.1a、1080p、2026 等噪音；
    - 识别 E01-E02 合集（不拆分）；
    - 识别 part1/part2 分段；
    - 仅有 E03 且无任何季依据时不擅自套用 S01；
    - 结合 parent_path 补充季依据。
    """
    clean_name = str(filename or "")
    part_match = _PART_REGEX.search(clean_name)
    part_str = part_match.group(1) if part_match else None

    is_special = False
    special_type = None
    sp_match = _SPECIAL_REGEX.search(clean_name)
    if sp_match:
        is_special = True
        special_type = sp_match.group(1)

    scrubbed = _VERSION_TAG_REGEX.sub(" ", clean_name)

    season_number: int | None = None
    episodes: list[int] = []

    # 1. 尝试匹配显式标准 S01E02 / S1E02 / S01E01-E02 / S01E01-03
    se_match = re.search(
        r"(?i)(?<![a-zA-Z0-9])S(\d{1,2})[Ee](\d{1,4})(?:-[Ee]?(\d{1,4}))?",
        scrubbed,
    )
    if se_match:
        season_number = int(se_match.group(1))
        ep1 = int(se_match.group(2))
        if se_match.group(3):
            ep2 = int(se_match.group(3))
            low, high = min(ep1, ep2), max(ep1, ep2)
            episodes = list(range(low, high + 1))
        else:
            episodes = [ep1]
        return season_number, episodes, part_str, is_special, special_type

    # 2. 检查父目录中是否存在明确的季目录（如 Season 1, S02, 第1季）
    parent_clean = normalize_file_path(parent_path)
    if parent_clean:
        s_dir_match = re.search(
            r"(?i)(?:Season\s*(\d{1,2})|(?<![a-zA-Z0-9])S(\d{1,2})(?![a-zA-Z0-9])|第(\d{1,2})季)",
            parent_clean,
        )
        if s_dir_match:
            g = s_dir_match.group(1) or s_dir_match.group(2) or s_dir_match.group(3)
            season_number = int(g)

    # 若仍未从父目录获取到季号，检查文件名本身的独立季标记 (如 S09, 第9季)
    if season_number is None:
        file_s_match = re.search(
            r"(?i)(?<![a-zA-Z0-9])(?:S(\d{1,2})|第(\d{1,2})季)(?:[-._\s]|$)",
            scrubbed,
        )
        if file_s_match:
            g = file_s_match.group(1) or file_s_match.group(2)
            season_number = int(g)

    # 3. 匹配集号：EP01-03 / E01-E02 / 第01集 / E03 / [11] / - 11
    range_match = re.search(
        r"(?i)(?<![a-zA-Z0-9])(?:EP?|第)(\d{1,4})(?:集|话話)?\s*[-~～至到]\s*(?:EP?|第)?(\d{1,4})(?:集|话話)?",
        scrubbed,
    )
    if range_match:
        ep1 = int(range_match.group(1))
        ep2 = int(range_match.group(2))
        low, high = min(ep1, ep2), max(ep1, ep2)
        episodes = list(range(low, high + 1))
    else:
        ep_single = re.search(
            r"(?i)(?<![a-zA-Z0-9])(?:EP|E|第)(\d{1,4})(?:集|话話)?(?![a-zA-Z0-9])",
            scrubbed,
        )
        if ep_single:
            ep_val = int(ep_single.group(1))
            if ep_val not in _NOISE_NUMBERS:
                episodes = [ep_val]
        else:
            bracket_match = re.search(
                r"(?:\[|\s-\s|[-._\s])(\d{1,3})(?:\]|\s|\.|$)",
                scrubbed,
            )
            if bracket_match:
                cand = int(bracket_match.group(1))
                if cand not in _NOISE_NUMBERS and cand > 0:
                    episodes = [cand]

    # 4. 若具备 confirmed_offset 且解析出了集号，严格应用一次偏移
    if episodes and confirmed_offset != 0:
        episodes = [max(1, e - confirmed_offset) for e in episodes]

    return season_number, episodes, part_str, is_special, special_type


def find_companion_subtitles(
    video_basename: str,
    sibling_entries: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """从同目录清单中清点与主视频同基名的伴随外挂字幕文件（第 7.2 节）。"""
    if not video_basename or not sibling_entries:
        return []
    stem = posixpath.splitext(video_basename)[0].lower()
    if not stem:
        return []
    subs: list[dict[str, Any]] = []
    for entry in sibling_entries:
        if entry.get("type") == "dir":
            continue
        name = str(entry.get("name") or "")
        low = name.lower()
        _, ext = posixpath.splitext(low)
        if ext not in SUBTITLE_EXTENSIONS:
            continue
        if low.startswith(stem + ".") or low == stem + ext:
            subs.append({
                "name": name,
                "path": normalize_file_path(entry.get("path") or ""),
                "size": int(entry.get("size") or 0),
                "storage": entry.get("storage") or "local",
            })
    return subs


def is_qualified_target_video(
    target_exists: bool,
    target_file_size: int,
    source_file_size: int = 0,
    has_identity_conflict: bool = False,
) -> tuple[bool, str]:
    """核验目标位置视频是否为可信合格文件（第 5/11 节 R11 vs R12）。

    - 目标不存在或 <= 10MB 残缺文件 -> 不合格；
    - 存在身份冲突 -> 不合格；
    - 若源与目标均有真实大小且目标不足源的 50%（疑似残缺或低清残卷）-> 不合格，转冲突对比；
    - 其余情况（如大小一致或均为完整大视频且身份季集一致）-> 合格。
    """
    if not target_exists:
        return False, "目标文件不存在"
    if target_file_size <= MIN_QUALIFIED_VIDEO_BYTES:
        return False, f"目标文件过小 ({target_file_size} B)，疑似残缺或空文件"
    if has_identity_conflict:
        return False, "目标作品身份或版本与源存在冲突"
    if source_file_size > MIN_QUALIFIED_VIDEO_BYTES:
        ratio = target_file_size / float(source_file_size)
        if ratio < 0.5:
            return False, f"目标体积 ({target_file_size} B) 显著小于源文件 ({source_file_size} B)，存在残缺或低版本冲突"
    return True, f"目标视频已核实存在且大小有效 ({target_file_size} B)"


def format_episode_detail(episodes: list[int]) -> str | None:
    """将集号列表转换为 MoviePilot v2.15.6 ManualTransferItem 期望的字符串格式。"""
    clean = sorted({int(e) for e in episodes if isinstance(e, int) and e >= 0})
    if not clean:
        return None
    if len(clean) == 1:
        return str(clean[0])
    # 连续区间用 start-end（如 "1-2"），离散多集用逗号分隔（如 "1,3"）
    if clean == list(range(clean[0], clean[-1] + 1)):
        return f"{clean[0]}-{clean[-1]}"
    return ",".join(str(e) for e in clean)


def build_mp_manual_transfer_payload(
    *,
    logid: int | None,
    fileitem: dict[str, Any] | None,
    source_path: str,
    src_storage: str,
    media_type: str,
    tmdb_id: int | None,
    season_number: int | None,
    episode_numbers: list[int],
    part: str | None = None,
    episode_offset: int = 0,
    target_storage: str | None = None,
    target_path: str | None = None,
    transfer_type: str | None = None,
    preview: bool = False,
    allow_logid: bool = True,
) -> dict[str, Any]:
    """严格构造 MoviePilot v2.15.6 ManualTransferItem 载荷（第 8.1/8.2 节）。

    - 显式关闭 from_history=False、reorganize=False，防止旧错字段回填或隐式删除目标；
    - 电影绝不携带 season / episode_detail / episode_part；
    - 携带真实 fileitem 对象（含 storage/type/path/name），仅在无 dest_fileitem 清理风险时附加 logid。
    """
    norm_src = normalize_file_path(source_path)
    name = posixpath.basename(norm_src)
    stem, ext = posixpath.splitext(name)

    fi = dict(fileitem) if isinstance(fileitem, dict) and fileitem.get("path") else {
        "storage": src_storage or "local",
        "type": "file",
        "path": norm_src,
        "name": name,
        "basename": stem,
        "extension": ext.lstrip("."),
    }

    is_movie = (media_type == "movie")
    payload: dict[str, Any] = {
        "fileitem": fi,
        "type_name": "电影" if is_movie else "电视剧",
        "from_history": False,
        "reorganize": False,
        "preview": bool(preview),
        "background": False,
    }
    if allow_logid and logid and int(logid) > 0:
        payload["logid"] = int(logid)
    if tmdb_id and int(tmdb_id) > 0:
        payload["tmdbid"] = int(tmdb_id)
    if target_storage:
        payload["target_storage"] = target_storage
    if target_path:
        payload["target_path"] = target_path
    if transfer_type:
        payload["transfer_type"] = transfer_type

    if not is_movie:
        if season_number is not None:
            payload["season"] = int(season_number)
        ep_detail = format_episode_detail(episode_numbers)
        if ep_detail:
            payload["episode_detail"] = ep_detail
        if part:
            payload["episode_part"] = str(part)
        if episode_offset:
            payload["episode_offset"] = str(episode_offset)

    return payload


def parse_mp_preview_response(
    preview_res: Any,
    expected_source_path: str = "",
) -> dict[str, Any]:
    """严格解析 MoviePilot preview=true 响应（R17：绝不因顶层 success=true 忽略单项失败）。"""
    if not isinstance(preview_res, dict):
        return {
            "ok": False,
            "reason": f"预览返回未知结构 ({type(preview_res).__name__})",
            "items": [],
            "target_path": "",
        }
    if preview_res.get("success") is False:
        return {
            "ok": False,
            "reason": str(preview_res.get("message") or "MoviePilot 预览失败"),
            "items": [],
            "target_path": "",
        }

    data = preview_res.get("data")
    items: list[dict[str, Any]] = []
    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            items = [x for x in data["items"] if isinstance(x, dict)]
        elif isinstance(data.get("list"), list):
            items = [x for x in data["list"] if isinstance(x, dict)]
        elif data.get("dest") or data.get("target_path"):
            items = [data]
    elif isinstance(data, list):
        items = [x for x in data if isinstance(x, dict)]

    # R17: 检查每个 preview item 是否存在失败标记
    failed_items = []
    target_paths: list[str] = []
    for it in items:
        if it.get("success") is False or it.get("status") is False or it.get("errmsg") or it.get("error"):
            failed_items.append(
                str(it.get("errmsg") or it.get("error") or it.get("message") or "单项预览失败")
            )
        dest_p = it.get("dest") or it.get("target_path") or (it.get("dest_fileitem") or {}).get("path") or ""
        if dest_p:
            target_paths.append(normalize_file_path(dest_p))

    if failed_items:
        return {
            "ok": False,
            "reason": f"预览包含失败项: {'; '.join(failed_items[:3])}",
            "items": items,
            "target_path": target_paths[0] if target_paths else "",
        }

    # 如果顶层既没有 explicit success=True 也没有任何预览项，视为未知/未证实
    if preview_res.get("success") is not True and not items:
        return {
            "ok": False,
            "reason": str(preview_res.get("message") or "预览未返回明确成功状态或目标路径"),
            "items": [],
            "target_path": "",
        }

    return {
        "ok": True,
        "reason": str(preview_res.get("message") or "预览通过"),
        "items": items,
        "target_path": target_paths[0] if target_paths else "",
        "expected_source_path": normalize_file_path(expected_source_path),
    }


def diagnose_failure_record(
    history_record: dict[str, Any],
    source_exists: bool,
    target_exists: bool = False,
    target_file_size: int = 0,
    source_file_size: int = 0,
    storage_healthy: bool = True,
    storage_error: str = "",
    active_rules: list[dict[str, Any]] | None = None,
    dest_dir_sample: str | None = None,
    subsequent_success: dict[str, Any] | None = None,
    companion_subtitles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """对单条失败历史记录执行全流程智能诊断，生成真实证据快照与文件修复计划。"""
    raw_error = extract_raw_error(history_record)
    error_cat = classify_error_category(raw_error)

    src_fi = sanitize_fileitem(history_record.get("src_fileitem"))
    dest_fi = sanitize_fileitem(history_record.get("dest_fileitem"))

    src_path = normalize_file_path(
        history_record.get("src")
        or src_fi.get("path")
        or history_record.get("source_path")
        or history_record.get("file_path")
        or history_record.get("path")
        or ""
    )
    dest_path = normalize_file_path(
        history_record.get("dest")
        or dest_fi.get("path")
        or dest_dir_sample
        or ""
    )
    src_storage = str(history_record.get("src_storage") or src_fi.get("storage") or "local")
    dest_storage = str(history_record.get("dest_storage") or dest_fi.get("storage") or "")

    file_basename = posixpath.basename(src_path) if src_path else str(history_record.get("title") or "")
    parent_dir = posixpath.dirname(src_path)
    package_name = posixpath.basename(parent_dir) if parent_dir else ""

    # MP 原始识别信息（保留原识别，不盲目覆盖）
    mp_title = str(history_record.get("title") or "").strip()
    mp_type = str(history_record.get("type") or "").strip()
    mp_seasons_raw = str(history_record.get("seasons") or "").strip()
    mp_episodes_raw = str(history_record.get("episodes") or "").strip()
    mp_mode = str(history_record.get("mode") or "").strip()
    mp_date = str(history_record.get("date") or "").strip()
    mp_tmdbid = int(history_record.get("tmdbid") or 0) or None

    # 媒体类型判定（R05：电影与电视剧严格隔离）
    if mp_type in ("电影", "movie", "Movie") or "/电影/" in src_path or "/movies/" in src_path.lower():
        media_type = "movie"
    else:
        media_type = "tv"

    # 标题推导：优先保留 MP 已识别的有效剧名（除非原始错误本身是未识别媒体信息）
    parsed_title = parse_clean_title(file_basename) or (parse_clean_title(package_name) if package_name else "")
    if mp_title and error_cat != "no_media_info":
        candidate_title = mp_title
    else:
        candidate_title = parsed_title or mp_title

    # 匹配经验复用规则（R24）
    matched_rule: dict[str, Any] | None = None
    offset = 0
    rule_season: int | None = None
    if active_rules:
        for r in active_rules:
            scope_key = str(r.get("scope_key") or "")
            scope_kind = str(r.get("scope_kind") or "")
            if (scope_kind == "this_package" and scope_key and scope_key in src_path) or (
                scope_kind == "this_dir_template" and scope_key and parent_dir.startswith(scope_key)
            ):
                # R24: 若规则剧名与 MP 明确识别剧名（有 tmdbid）冲突，停止套用
                rule_tmdbid = r.get("tmdb_id")
                if rule_tmdbid and mp_tmdbid and int(rule_tmdbid) != int(mp_tmdbid):
                    continue
                matched_rule = r
                offset = int(r.get("episode_offset") or 0)
                if r.get("season_number") is not None:
                    rule_season = int(r["season_number"])
                if r.get("show_title"):
                    candidate_title = str(r["show_title"])
                break

    # 解析季与集（电影绝不携带季集，R05）
    if media_type == "movie":
        s_num, eps, part_str, is_sp, sp_type = None, [], None, False, None
    else:
        s_num, eps, part_str, is_sp, sp_type = extract_season_and_episodes(
            file_basename,
            parent_path=parent_dir,
            confirmed_offset=offset,
        )
        # 仅当文件名未给出季号时，借鉴规则或 MP 已识别的明确季号
        if s_num is None and rule_season is not None:
            s_num = rule_season
        if s_num is None and mp_seasons_raw and error_cat != "no_media_info":
            s_num = parse_mp_season_number(mp_seasons_raw)
        if not eps and mp_episodes_raw and not is_sp and error_cat not in ("no_episodes", "no_media_info"):
            eps = parse_mp_episode_numbers(mp_episodes_raw)

    # 上游破坏性副作用标识：失败历史带 dest_fileitem 时，直接以 logid 重试可能触发 MP 清理旧目标 (R13)
    has_dest_fileitem = bool(dest_fi or history_record.get("dest"))

    # 源与目标有效大小（若未实测到大小，回退历史快照仅作参考标注）
    effective_src_size = int(source_file_size or src_fi.get("size") or 0)
    effective_dest_size = int(target_file_size or 0)

    # 合格目标判定 (R11 vs R12)
    target_ok, target_ok_reason = is_qualified_target_video(
        target_exists=target_exists,
        target_file_size=effective_dest_size,
        source_file_size=effective_src_size,
        has_identity_conflict=False,
    )

    # 状态决策主链（严格短路优先级）
    status = "diagnosing"
    res_code: str | None = None
    skip_reason: str | None = None
    main_reason = ""
    failure_group_key = ""

    # 1. 存储/环境不可达（R23, R31：绝不误判为源不存在跳过）
    if not storage_healthy:
        status = "waiting_environment"
        err_sig = (storage_error or error_cat or "storage_unavailable")[:48]
        failure_group_key = f"env:{src_storage}:{err_sig}"
        main_reason = f"存储 [{src_storage}] 暂时不可达或查询失败 ({storage_error or '离线/超时'})，等待环境恢复后自动重查"

    # 2. 后续成功记录已核实（R14）
    elif subsequent_success and subsequent_success.get("verified"):
        status = "resolved"
        res_code = "superseded_by_success"
        main_reason = (
            f"已核实后续成功记录 #{subsequent_success.get('history_id')} 目标文件已有效落地，"
            "MediaFlow 已结案（保留 MP 原历史）"
        )

    # 3. 目标文件经实查核验合格（R11）
    elif target_ok:
        status = "resolved"
        res_code = "already_present_verified"
        main_reason = f"{target_ok_reason}，无需重复整理；MediaFlow 已核实结案（保留 MP 原历史与源文件）"

    # 4. 存储健康但源文件确认不存在且无合格目标/后续成功（R15）
    elif not source_exists:
        status = "skipped"
        skip_reason = "source_missing"
        main_reason = "存储健康，但源文件已确认不存在且无合格目标或后续成功证据；已跳过活跃修复（不自动重试、不触发重下）"

    # 5. 同名冲突或携带 dest_fileitem 风险拦截（R12, R13）
    elif error_cat == "target_exists" or (target_exists and not target_ok) or has_dest_fileitem:
        status = "conflict"
        if target_exists and not target_ok:
            main_reason = f"目标同名文件核验未通过（{target_ok_reason}），禁止自动覆盖；请人工对比处理"
        elif has_dest_fileitem:
            main_reason = (
                "历史包含旧目标引用 (dest_fileitem)，直接重整存在上游隐式清理旧目标风险；"
                "前置拦截转人工复核"
            )
        else:
            main_reason = "目标位置存在同名冲突，禁止静默覆盖；生成对比等待人工决策"

    # 6. 电影类型诊断（R05）
    elif media_type == "movie":
        if candidate_title:
            status = "ready"
            main_reason = f"已核实电影《{candidate_title}》（无季集参数），源稳定可读，待预览并整理"
        else:
            status = "needs_confirmation"
            main_reason = "电影作品名称无法唯一定位，需补充一次作品确认"

    # 7. 电视剧季集推导检查（R03, R04, R06, R08, R10）
    else:
        if not candidate_title:
            status = "needs_confirmation"
            main_reason = "未识别到作品标题，需补充一次作品确认"
        elif s_num is None:
            # R04: 绝不盲目默认 S01！
            status = "needs_confirmation"
            main_reason = f"已提取集号 {eps or '未知'}，但缺少季号依据，绝不擅自套用 S01；请补充一次季选择"
        elif is_sp and not eps:
            status = "needs_confirmation"
            main_reason = f"检测到特辑/附加内容 [{sp_type or '特辑'}]{' (' + part_str + ')' if part_str else ''}，请确认特别篇归宿"
        elif not eps:
            status = "needs_confirmation"
            main_reason = "未能从文件名识别出明确正片集号；请补充集号选择"
        else:
            ep_label = "E" + "-E".join(f"{e:02d}" for e in eps)
            part_label = f" [{part_str}]" if part_str else ""
            status = "ready"
            main_reason = f"已核实《{candidate_title}》S{s_num:02d}{ep_label}{part_label}，源稳定可读且无目标冲突，可预览并整理"

    if not failure_group_key:
        season_tag = f"S{s_num:02d}" if s_num is not None else "SXX"
        failure_group_key = f"work:{media_type}:{mp_tmdbid or candidate_title or 'unknown'}:{season_tag}:{package_name}"

    subs = companion_subtitles or []
    evidence = {
        "mp_original": {
            "history_id": int(history_record.get("id") or 0),
            "title": mp_title,
            "type": mp_type,
            "tmdbid": mp_tmdbid,
            "seasons": mp_seasons_raw,
            "episodes": mp_episodes_raw,
            "mode": mp_mode,
            "date": mp_date,
            "errmsg": raw_error,
            "src_storage": src_storage,
            "dest_storage": dest_storage,
            "has_dest_fileitem": has_dest_fileitem,
        },
        "filename_parsed": {
            "parsed_title": parsed_title,
            "season_number": s_num,
            "episode_numbers": eps,
            "part": part_str,
            "is_special": is_sp,
            "special_type": sp_type,
        },
        "storage_probe": {
            "storage_healthy": storage_healthy,
            "storage_error": storage_error,
            "source_exists": source_exists,
            "source_file_size": effective_src_size,
            "target_exists": target_exists,
            "target_file_size": effective_dest_size,
            "target_path": dest_path,
            "target_qualified": target_ok,
            "target_check_reason": target_ok_reason,
        },
        "subsequent_success": subsequent_success or {},
        "companion_subtitles": subs,
        "matched_rule_id": matched_rule.get("id") if matched_rule else None,
        "dest_cleanup_risk": has_dest_fileitem,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    target_verify = {
        "dest_path": dest_path,
        "dest_storage": dest_storage or src_storage,
        "target_exists": target_exists,
        "target_file_size": effective_dest_size,
        "source_file_size": effective_src_size,
        "qualified": target_ok,
        "reason": target_ok_reason,
    }

    plan = {
        "source_path": src_path,
        "src_storage": src_storage,
        "file_basename": file_basename,
        "title": candidate_title,
        "media_type": media_type,
        "tmdb_id": mp_tmdbid,
        "season_number": s_num,
        "episode_numbers": eps,
        "episode_detail": format_episode_detail(eps) if media_type != "movie" else None,
        "part": part_str,
        "episode_offset": offset,
        "is_special": is_sp,
        "special_type": sp_type,
        "target_dir": dest_dir_sample or posixpath.dirname(dest_path) if dest_path else "",
        "target_path": dest_path,
        "target_storage": dest_storage or src_storage,
        "companion_subtitles": [s.get("name") for s in subs],
        "rule_id": matched_rule.get("id") if matched_rule else None,
        "dest_cleanup_risk": has_dest_fileitem,
        "safe_to_execute": (status == "ready" and not has_dest_fileitem),
        "from_history": False,
        "reorganize": False,
        "diagnosed_at": datetime.now(timezone.utc).isoformat(),
    }
    p_hash = calculate_plan_hash(plan)

    return {
        "status": status,
        "error_category": error_cat,
        "raw_error": raw_error,
        "main_block_reason": main_reason,
        "failure_group_key": failure_group_key,
        "mp_title": mp_title,
        "mp_seasons": mp_seasons_raw,
        "mp_episodes": mp_episodes_raw,
        "mp_mode": mp_mode,
        "mp_date": mp_date,
        "src_storage": src_storage,
        "dest_storage": dest_storage,
        "src_fileitem": src_fi,
        "dest_fileitem": dest_fi,
        "title": candidate_title,
        "media_type": media_type,
        "tmdb_id": mp_tmdbid,
        "season_number": s_num,
        "episode_numbers": eps,
        "part": part_str,
        "is_special": is_sp,
        "special_type": sp_type,
        "target_path": dest_path,
        "resolution_code": res_code,
        "skip_reason": skip_reason,
        "evidence_json": evidence,
        "target_verify_json": target_verify,
        "plan_json": plan,
        "plan_hash": p_hash,
    }
