"""媒体整理智能修复核心引擎 — 诊断分类、参数推导、冲突拦截与计划生成。

遵循《MediaFlow媒体整理智能修复需求与交互方案-v2.5.40.md》：
- R01/R29/R30/R32: 接管基线隔离，仅接管启用后新失败，旧历史不导入、不自动重试；
- R03: 标准 S02E03 且身份明确时自动预览并准备整理；
- R04: 仅含 E03 且季号无法确定时，绝不盲目套 S01，提示补充一次季选择；
- R05: 同名电影与电视剧以类型隔离，电影不带季集参数；
- R06: 动漫 Ver1.1a - 11 排除版本号干扰，精准提取集号 11；
- R07: 绝对集号跨季复用已确认映射，offset 严格仅计算一次；
- R08/R09/R10: 多季展开、连集 E01-E02 不复制成两份、加更 part1/part2 保持分段语义不误转正片；
- R11/R12/R13: 目标已存在且合格则结案；同名不符或含 dest_fileitem 时拦截防止删除旧目标；
- R14/R15/R31: 区分存储离线与源文件不存在；源已不存在且无后续成功时标记 skipped/source_missing；
- R17/R18/R24: 逐文件预览核验、状态失效重算与规则作用域复用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.episode_parser import SUBTITLE_EXTENSIONS, VIDEO_EXTENSIONS

log = logging.getLogger("mediaflow.transfer_repair")

# 排除为纯集号的数字与关键词黑名单（年份、分辨率、编码、常见版本标记）
_NOISE_NUMBERS = {
    1970, 1980, 1990, 2000, 2010, 2015, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026, 2027, 2028,
    480, 576, 720, 1080, 2160, 4320, 264, 265, 8, 10,  # 8bit, 10bit
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


def normalize_file_path(path: str | None) -> str:
    """标准化文件路径，统一为正斜杠并去除末尾斜杠。"""
    if not path:
        return ""
    p = str(path).replace("\\", "/").strip()
    return posixpath.normpath(p)


def calculate_plan_hash(plan_dict: dict[str, Any]) -> str:
    """计算计划摘要哈希（用于幂等防重与失效比对）。"""
    s = json.dumps(plan_dict, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def parse_clean_title(filename_or_title: str) -> str:
    """从文件名或原始标题中提取清晰剧名/作品名。"""
    raw = str(filename_or_title or "").strip()
    if not raw:
        return ""
    # 去除常见的发布组前缀 [XXX] 或 (XXX)
    s = re.sub(r"^\[[^\]]+\]\s*", "", raw)
    s = re.sub(r"^\([^\)]+\)\s*", "", s)

    # 遇到标准季集标记 S01 / E01 / EP01 / 第X季 时截断
    s = re.split(r"(?i)(?:[-._\s]|\b)(?:S\d+|EP?\d+|第\d+[季集话話])", s)[0]
    # 去除年份 (2024)
    s = re.sub(r"\(\d{4}\)", "", s)
    # 去除版本/压制组尾巴
    s = re.sub(r"\[[^\]]+\]", "", s)
    s = re.sub(r"(?i)\b(4k|2160p|1080p|720p|hevc|x265|x264|web-dl|hdtv)\b.*", "", s)
    # 标点符号清理
    s = s.replace(".", " ").replace("_", " ").strip()
    return s


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

    # 先从文件名中剔除版本干扰，如 "Ver1.1a" -> ""
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
            r"(?i)(?:Season\s*(\d{1,2})|S(\d{1,2})|第(\d{1,2})季)",
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
    # 3.1 连集 E01-E03 或 EP01-EP03
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
        # 3.2 单集 E03 / EP03 / 第03集
        ep_single = re.search(
            r"(?i)(?<![a-zA-Z0-9])(?:EP|E|第)(\d{1,4})(?:集|话話)?(?![a-zA-Z0-9])",
            scrubbed,
        )
        if ep_single:
            ep_val = int(ep_single.group(1))
            if ep_val not in _NOISE_NUMBERS:
                episodes = [ep_val]
        else:
            # 3.3 尝试匹配被空格、中括号或横杠包围的集号 (如 " - 11 " 或 " [11] ")
            bracket_match = re.search(
                r"(?:\[|\s-\s|[-._\s])(\d{1,3})(?:\]|\s|\.|$)",
                scrubbed,
            )
            if bracket_match:
                cand = int(bracket_match.group(1))
                if cand not in _NOISE_NUMBERS and cand > 0:
                    episodes = [cand]

    # 4. 若具备 confirmed_offset 且解析出了集号，应用一次偏移
    if episodes and confirmed_offset != 0:
        episodes = [max(1, e - confirmed_offset) for e in episodes]

    return season_number, episodes, part_str, is_special, special_type


def diagnose_failure_record(
    history_record: dict[str, Any],
    source_exists: bool,
    target_exists: bool = False,
    target_file_size: int = 0,
    storage_healthy: bool = True,
    active_rules: list[dict[str, Any]] | None = None,
    dest_dir_sample: str | None = None,
) -> dict[str, Any]:
    """对单条失败历史记录执行全流程智能诊断，生成文件修复项初态与计划。

    返回 dict，包含:
    - status
    - error_category
    - main_block_reason
    - title
    - media_type
    - tmdb_id
    - season_number
    - episode_numbers
    - part
    - is_special
    - resolution_code
    - skip_reason
    - plan_json (dict)
    - plan_hash
    """
    raw_error = str(history_record.get("error") or history_record.get("reason") or history_record.get("message") or "")
    src_path = normalize_file_path(
        history_record.get("src")
        or history_record.get("source_path")
        or history_record.get("file_path")
        or history_record.get("path")
        or ""
    )
    file_basename = posixpath.basename(src_path) if src_path else str(history_record.get("title") or "")
    parent_dir = posixpath.dirname(src_path)

    # 媒体类型初判（根据目录关键词或历史字段）
    media_type = "movie" if ("/电影/" in src_path or history_record.get("type") in ("电影", "movie")) else "tv"

    # 提取标题
    candidate_title = parse_clean_title(file_basename or history_record.get("title") or "")
    if not candidate_title and parent_dir:
        candidate_title = parse_clean_title(posixpath.basename(parent_dir))

    # 匹配规则（作用域复用 R24）
    matched_rule: dict[str, Any] | None = None
    offset = 0
    rule_season: int | None = None
    if active_rules:
        for r in active_rules:
            scope_key = r.get("scope_key") or ""
            scope_kind = r.get("scope_kind") or ""
            if (scope_kind == "this_package" and scope_key and scope_key in src_path) or (
                scope_kind == "this_dir_template" and scope_key and parent_dir.startswith(scope_key)
            ):
                matched_rule = r
                offset = int(r.get("episode_offset") or 0)
                rule_season = int(r.get("season_number") or 1)
                break

    # 解析季与集
    s_num, eps, part_str, is_sp, sp_type = extract_season_and_episodes(
        file_basename,
        parent_path=parent_dir,
        confirmed_offset=offset,
    )
    if s_num is None and rule_season is not None:
        s_num = rule_season

    # 归纳错误大类
    error_cat = "unknown"
    if "存在同名文件" in raw_error or "不覆盖" in raw_error:
        error_cat = "target_exists"
    elif "未识别到文件集数" in raw_error or "未识别集数" in raw_error:
        error_cat = "no_episodes"
    elif "未识别到媒体信息" in raw_error or "未识别媒体" in raw_error:
        error_cat = "no_media_info"
    elif "源文件不存在" in raw_error:
        error_cat = "source_missing"
    elif "copy" in raw_error.lower() or "拷贝失败" in raw_error:
        error_cat = "copy_failed"

    # 上游破坏性风险标识：如果历史记录含 dest_fileitem，直接重整会触发 MP 隐式删除旧目标 (R13)
    has_dest_risk = bool(history_record.get("dest_fileitem") or history_record.get("dest"))

    # 诊断分支判定
    status = "diagnosing"
    res_code: str | None = None
    skip_reason: str | None = None
    main_reason = ""

    # 1. 目标核验：如果目标文件已在目标目录存在且为有效大视频 (>10MB) -> R11 核实结案
    if target_exists and target_file_size > 10 * 1024 * 1024:
        status = "resolved"
        res_code = "already_present_verified"
        main_reason = "目标媒体已有效存在于目标库，无需重复整理；核实结案并保留历史审计"

    # 2. 检查存储健康度 (R31)
    elif not storage_healthy:
        status = "waiting_environment"
        main_reason = "源或目标存储处于离线或无权限状态，等待环境恢复探测"

    # 3. 检查源文件存在性 (R15)
    elif not source_exists:
        # 源不存在且无有效目标 -> 标记跳过，不自动重试
        status = "skipped"
        skip_reason = "source_missing"
        main_reason = "源文件已确认不存在且无合格目标证据，跳过活跃修复"

    # 4. 检查同名冲突与 dest_fileitem 风险 (R12, R13)
    elif error_cat == "target_exists" or (target_exists and target_file_size <= 10 * 1024 * 1024) or has_dest_risk:
        if has_dest_risk:
            status = "conflict"
            main_reason = "历史包含旧目标引用 (dest_fileitem)，重整存在隐式删除目标风险；前置拦截转人工复核"
        else:
            status = "conflict"
            main_reason = "目标位置已存在不同版本或冲突文件，禁止静默覆盖；生成对比等待人工决策"

    # 5. 电影类型诊断 (R05)
    elif media_type == "movie":
        if candidate_title:
            status = "ready"
            main_reason = f"已识别电影《{candidate_title}》，无季集参数，等待自动整理"
        else:
            status = "needs_confirmation"
            main_reason = "电影作品名称无法唯一定位，需补充作品确认"

    # 6. 电视剧季集推导检查 (R03, R04, R06, R08, R10)
    else:
        # 电视剧必须同时具备季号与集号
        if not candidate_title:
            status = "needs_confirmation"
            main_reason = "未识别到作品标题，需补充作品确认"
        elif s_num is None:
            # R04: 绝不盲目默认 S01！
            status = "needs_confirmation"
            main_reason = f"已提取集号 {eps or '未知'}，但季号无法明确，绝不擅自套用 S01；需补充一次季选择"
        elif not eps and not is_sp:
            status = "needs_confirmation"
            main_reason = "未能从文件名识别出有效正片集号；需补充集号选择"
        elif is_sp and not eps:
            status = "needs_confirmation"
            main_reason = f"检测到特辑/附加内容 [{special_type or '特辑'}]，需确认特别篇归宿"
        else:
            # 参数齐全，符合自动执行门槛！(R03)
            status = "ready"
            main_reason = f"已核实《{candidate_title}》S{s_num:02d}{'E' + '-E'.join(f'{e:02d}' for e in eps)}，参数完备，可自动整理"

    # 生成计划对象
    plan = {
        "source_path": src_path,
        "file_basename": file_basename,
        "title": candidate_title,
        "media_type": media_type,
        "season_number": s_num,
        "episode_numbers": eps,
        "part": part_str,
        "is_special": is_sp,
        "special_type": sp_type,
        "target_dir": dest_dir_sample or "",
        "rule_id": matched_rule.get("id") if matched_rule else None,
        "safe_to_execute": (status == "ready"),
        "from_history": False,
        "reorganize": False,
        "diagnosed_at": datetime.now(timezone.utc).isoformat(),
    }
    p_hash = calculate_plan_hash(plan)

    return {
        "status": status,
        "error_category": error_cat,
        "main_block_reason": main_reason,
        "title": candidate_title,
        "media_type": media_type,
        "tmdb_id": int(history_record.get("tmdbid") or 0) or None,
        "season_number": s_num,
        "episode_numbers": eps,
        "part": part_str,
        "is_special": is_sp,
        "special_type": sp_type,
        "resolution_code": res_code,
        "skip_reason": skip_reason,
        "plan_json": plan,
        "plan_hash": p_hash,
    }
