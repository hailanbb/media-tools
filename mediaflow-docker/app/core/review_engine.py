"""
MediaFlow 复核清理核心引擎
移植参考于 tv-workflow-win (tv-workflow-Python/lib/stages/review.py) 的工业级成熟算法：

1. 【版本模板与分组】提取 S01E{集} 占位符模板，归类统计每个版本的全集完整性；
2. 【完整版本优先】
   - 若存在多个完整版本：按 score_group（4K 3000分 > 1080p 2000分 > 720p 500分 + 平均体积）优选保留最高画质版本，清理淘汰其余劣质完整版和残缺散版；
   - 若存在单一完整版本：坚决保留该完整版本，严禁散集残版鸠占鹊巢，将其他零散不完整版本（如只有1~2集的残版）全部淘汰清理；
   - 若无单一完整版本：当且仅当所有集号齐全时才执行“混版拼齐”，按单集 rank_copy 优选各集最佳画质；若有缺集坚决不删；
3. 【梯级副本仲裁 rank_copy】分辨率规格 > 标准命名规整度（排除(1)、copy等副本标记） > 文件体积 > 修改时间；
4. 【安全门禁】0 字节损坏/传输中文件安全熔断；临时下载文件（.tmp/.download/!qb）暂缓清理；
5. 【伴生级联清理】淘汰某版本视频时，专属同名外挂字幕与单集刮削（.nfo/.jpg/-thumb.jpg）级联清理；全局元数据（poster/season/tvshow/fanart/banner）绝对保护；
6. 【删除后强制刷新】删除后调用 cd2.list_files(path, force_refresh=True) 刷新网盘底层。
"""

from __future__ import annotations

import os
import posixpath
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

# 视频与伴生正则
MEDIA_REGEX = re.compile(
    r"\.(?:mp4|mkv|avi|mov|m4v|ts|m2ts|wmv|flv|webm|mpg|mpeg|strm|rmvb|vob|iso)$",
    re.IGNORECASE,
)
SUBTITLE_REGEX = re.compile(
    r"\.(?:srt|ass|ssa|vtt|sub|idx|sup)$",
    re.IGNORECASE,
)
SCRAPE_REGEX = re.compile(
    r"\.(?:nfo|jpg|jpeg|png|webp|avif|bmp|gif|tbn)$",
    re.IGNORECASE,
)
SHARED_SCRAPE = re.compile(
    r"^(?:tvshow|season(?:\d+)?|movie|poster|fanart|folder|banner|logo|clearlogo|clearart|landscape|disc|season(?:\d+)?[-_.](?:poster|fanart|banner|landscape))$",
    re.IGNORECASE,
)
EXTRA_REGEX = re.compile(
    r"(?:^|[\s._\-\(\[])(?:日记|Diary|花活儿|Extra|花絮|彩蛋|纯享|采访|访谈|先行片|预告|Preview|Behind[._\s-]the[._\s-]Scenes|Making[._\s-]of)(?:$|[\s._\-\)\]])",
    re.IGNORECASE,
)
TMP_DOWNLOAD_REGEX = re.compile(r"\.(?:part|tmp|temp|download|downloading|crdownload|!qb|aria2|xltd)$", re.IGNORECASE)


def rank_copy(f: Dict[str, Any]) -> Tuple[int, int, int, str]:
    """同集重复副本梯级打分（分辨率规格 > 标准命名规整度 > 文件体积 > 修改时间戳）"""
    f_name = f.get("name", "")
    size = int(f.get("size") or 0)
    # 规整命名优先（避免带 (1)、_1、副本 等拷贝后缀）
    has_dup_marker = 1 if re.search(r"[\s._\-(](?:1|2|copy|副本)[\s._\-)]", f_name, re.I) else 0
    clean_score = 0 if has_dup_marker else 1

    # 画质规格偏好
    res_score = 0
    if re.search(r"2160p|4k|uhd", f_name, re.I):
        res_score = 3
    elif re.search(r"1080p|1080i|fhd", f_name, re.I):
        res_score = 2
    elif re.search(r"720p|hd(?![a-z0-9])", f_name, re.I):
        res_score = 1

    mod_str = str(f.get("modified") or "")
    return (res_score, clean_score, size, mod_str)


def score_group(g: Dict[str, Any]) -> float:
    """计算完整版本的综合画质得分，用于多完整版本优选"""
    files = g.get("files") or []
    if not files:
        return 0.0
    total_size = sum(int(f.get("size") or 0) for f in files)
    avg_size_mb = (total_size / len(files)) / (1024 * 1024)
    res_score = 0.0
    k = str(g.get("key") or "").lower()
    if re.search(r"2160p|4k|uhd", k):
        res_score = 3000.0
    elif re.search(r"1080p|1080i|fhd", k):
        res_score = 2000.0
    elif re.search(r"720p|hd(?![a-z0-9])", k):
        res_score = 500.0
    return res_score + avg_size_mb


def episode_from_any_name(filename: str, season: Optional[int] = None) -> Optional[int]:
    """从任意文件名（包括视频、图片、nfo等）提取集号。"""
    if not filename:
        return None
    name = re.sub(r"\.[^.]+$", "", filename)
    if season is not None:
        se_pattern = re.compile(rf"(?:^|[^a-z0-9])S0*{season}\s*E(\d{{1,4}})(?!\d)", re.IGNORECASE)
        m = se_pattern.search(name)
        if m:
            return int(m.group(1))
    m = re.search(r"(?:^|[^a-z0-9])S\d{1,4}\s*E(\d{1,4})(?!\d)", name, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"第\s*(\d{1,4})\s*[集话話期]", name)
    if m:
        return int(m.group(1))
    return None


def episodes_from_name(filename: str, season: Optional[int] = None) -> List[int]:
    """从文件名中提取所有集号（F07: 支持范围集如 S01E01-E02、EP01-03、第01-02集 等多集格式）。"""
    if not filename or not MEDIA_REGEX.search(filename):
        return []

    name = re.sub(r"\.[^.]+$", "", filename)
    # 忽略特典
    if re.search(r"(?:^|[\s._\-[\]])(?:NCOP|NCED|PV|CM|Preview|Menu|Creditless|Textless|CleanOP|CleanED)\d*", name, re.IGNORECASE):
        return []

    if season is not None:
        se_pattern = re.compile(rf"(?:^|[^a-z0-9])S0*{season}\s*E(\d{{1,4}})(?:-[Ee]?(\d{{1,4}}))?(?!\d)", re.IGNORECASE)
        m = se_pattern.search(name)
        if m:
            s_ep = int(m.group(1))
            e_ep = int(m.group(2)) if m.group(2) else s_ep
            return list(range(s_ep, e_ep + 1))

    m = re.search(r"(?:^|[^a-z0-9])S\d{1,4}\s*E(\d{1,4})(?:-[Ee]?(\d{1,4}))?(?!\d)", name, re.IGNORECASE)
    if m:
        s_ep = int(m.group(1))
        e_ep = int(m.group(2)) if m.group(2) else s_ep
        return list(range(s_ep, e_ep + 1))

    m = re.search(r"第\s*(\d{1,4})(?:\s*[-~至到]\s*(\d{1,4}))?\s*[集话話回章]", name)
    if m:
        s_ep = int(m.group(1))
        e_ep = int(m.group(2)) if m.group(2) else s_ep
        return list(range(s_ep, e_ep + 1))

    m = re.search(r"(?:^|[\s._\-[\]])(?:EP|E)\s*(\d{1,4})(?:-[Ee]?[Pp]?(\d{1,4}))?(?!\d)", name, re.IGNORECASE)
    if m:
        s_ep = int(m.group(1))
        e_ep = int(m.group(2)) if m.group(2) else s_ep
        return list(range(s_ep, e_ep + 1))

    m = re.search(r"[\s._-](\d{2,4})(?:v\d+)?[\s._\-\]]", name)
    if m:
        ep_val = int(m.group(1))
        if 0 < ep_val < 1000:
            return [ep_val]

    return []


def episode_from_name(filename: str, season: Optional[int] = None) -> Optional[int]:
    """单集兼容接口。"""
    eps = episodes_from_name(filename, season)
    return eps[0] if eps else None


def format_missing_episodes_text(missing_eps: List[int]) -> str:
    """人性化格式化缺失集数，避免 '缺 14 集' 被误读为缺失 14 集数量。"""
    if not missing_eps:
        return ""
    if len(missing_eps) == 1:
        return f"缺失第 {missing_eps[0]} 集"
    elif len(missing_eps) <= 5:
        eps_str = "、".join(str(e) for e in missing_eps)
        return f"缺失第 {eps_str} 集 (共 {len(missing_eps)} 集)"
    else:
        sample_str = "、".join(str(e) for e in missing_eps[:3])
        return f"缺失第 {sample_str} 等共 {len(missing_eps)} 集"


def generate_review_clean_plan(
    files: List[Dict[str, Any]],
    season_number: int,
    total_episodes: int,
    force_clean: bool = False,
) -> Dict[str, Any]:
    """
    根据 tv-workflow-win 成熟逻辑生成清理计划。
    
    Returns:
        {
            "can_clean": bool,
            "reason": str,
            "version_note": str,
            "to_keep": List[Dict],
            "to_delete": List[Dict],
            "status": str  # 'skip', 'ready', 'cleaned'
        }
    """
    now_iso = datetime.now().isoformat()

    # 1. 安全门禁：存在子目录
    if any(f.get("isDirectory") for f in files):
        return {
            "can_clean": False,
            "reason": "季内存在子目录，需人工确认",
            "version_note": "季内存在子目录，需人工确认",
            "status": "skip",
            "to_keep": [],
            "to_delete": [],
        }

    # 2. 安全门禁：下载中临时文件
    if any(TMP_DOWNLOAD_REGEX.search(f.get("name", "")) for f in files):
        return {
            "can_clean": False,
            "reason": "发现临时下载文件，等待完成",
            "version_note": "发现临时下载文件，等待完成",
            "status": "skip",
            "to_keep": [],
            "to_delete": [],
        }

    videos = [f for f in files if MEDIA_REGEX.search(f.get("name", ""))]

    # 3. 安全门禁：0 字节视频安全熔断
    zero_byte = [f for f in videos if int(f.get("size") or 0) <= 0]
    if zero_byte:
        names = "、".join(f.get("name", "") for f in zero_byte[:2])
        return {
            "can_clean": False,
            "reason": f"发现 {len(zero_byte)} 个 0 字节损坏视频 ({names})，熔断保护",
            "version_note": f"发现损坏视频，熔断保护",
            "status": "skip",
            "to_keep": [],
            "to_delete": [],
        }

    # 4. 解析各视频集号与版本模板（花絮单独分离保护）
    valid_parsed = []
    extra_files = []
    for f in videos:
        f_name = f.get("name", "")
        # 检测是否为衍生花絮/日记等
        if EXTRA_REGEX.search(f_name):
            extra_files.append({
                "episode": None,
                "key": "extras",
                "name": f_name,
                "path": f.get("path"),
                "modified": f.get("modified"),
                "size": f.get("size", 0),
                "reason": "衍生花絮/日记/彩蛋文件，保护保留",
            })
            continue

        eps = episodes_from_name(f_name, season_number)
        if not eps:
            continue

        stem = re.sub(r"\.[^.]+$", "", f_name)
        se_re = re.compile(rf"S0*{season_number}\s*E\d+(?:-[Ee]?\d+)?", re.IGNORECASE)
        template = se_re.sub(f"S{season_number}E{{集}}", stem)
        if template == stem:
            template = re.sub(r"第\s*\d+(?:\s*[-~至到]\s*\d+)?\s*集", "第{集}集", template)

        m_ext = re.search(r"\.[^.]+$", f_name)
        ext = m_ext.group(0) if m_ext else ""
        key = re.sub(r"\s+", " ", template).strip().lower() + ext

        valid_parsed.append({
            "episode": eps[0],
            "episodes": eps,
            "key": key,
            "name": f_name,
            "path": f.get("path"),
            "modified": f.get("modified"),
            "size": f.get("size", 0),
        })

    all_found_eps = set()
    for p in valid_parsed:
        all_found_eps.update(p.get("episodes", [p["episode"]]))
    found_episodes = sorted(all_found_eps)
    expected_range = list(range(1, total_episodes + 1))

    # 按原生版本模板分组
    groups: Dict[str, Dict[str, Any]] = {}
    for p in valid_parsed:
        k = p["key"]
        if k not in groups:
            groups[k] = {"key": k, "files": [], "episodes": []}
        groups[k]["files"].append(p)
        groups[k]["episodes"].extend(p.get("episodes", [p["episode"]]))

    for g in groups.values():
        g_eps = sorted(set(g["episodes"]))
        g["episodes"] = g_eps
        g["missing"] = [n for n in expected_range if n not in g_eps]
        g["complete"] = (len(g["missing"]) == 0)

    complete_groups = [g for g in groups.values() if g["complete"]]

    to_keep: List[Dict[str, Any]] = []
    to_delete: List[Dict[str, Any]] = []
    version_note = ""

    # 5. 核心仲裁决策
    if len(complete_groups) > 1:
        # 多完整版本：按画质综合评分优选最佳画质完整版，淘汰劣质完整版和所有残版
        complete_groups.sort(key=score_group, reverse=True)
        best = complete_groups[0]
        version_note = f"存在 {len(complete_groups)} 个完整版本，优选保留最佳画质 [{best['key']}]，淘汰劣质版本"

        for ep in expected_range:
            copies = [f for f in best["files"] if ep in f.get("episodes", [f.get("episode")])]
            if not copies:
                continue
            copies.sort(key=rank_copy, reverse=True)
            chosen = copies[0]
            if not any(k["path"] == chosen["path"] for k in to_keep):
                to_keep.append(chosen)
            for dup in copies[1:]:
                d_item = dict(dup)
                d_item["reason"] = "同版本较旧或冗余副本"
                if not any(d["path"] == d_item["path"] for d in to_delete) and not any(k["path"] == d_item["path"] for k in to_keep):
                    to_delete.append(d_item)

        for inferior_g in complete_groups[1:]:
            for f in inferior_g["files"]:
                d_item = dict(f)
                d_item["reason"] = f"淘汰低画质完整版本 ({inferior_g['key']})"
                to_delete.append(d_item)

        for k, g in groups.items():
            if g in complete_groups:
                continue
            for f in g["files"]:
                d_item = dict(f)
                d_item["reason"] = "其他不完整残卷版本"
                to_delete.append(d_item)

    elif len(complete_groups) == 1:
        # 单一完整版本：坚决保留该完整版！淘汰所有其他零散残版
        best = complete_groups[0]
        version_note = f"保留完整版本: {best['key']}"

        for ep in expected_range:
            copies = [f for f in best["files"] if ep in f.get("episodes", [f.get("episode")])]
            if not copies:
                continue
            copies.sort(key=rank_copy, reverse=True)
            chosen = copies[0]
            if not any(k["path"] == chosen["path"] for k in to_keep):
                to_keep.append(chosen)
            for dup in copies[1:]:
                d_item = dict(dup)
                d_item["reason"] = "同版本较旧或冗余副本"
                if not any(d["path"] == d_item["path"] for d in to_delete) and not any(k["path"] == d_item["path"] for k in to_keep):
                    to_delete.append(d_item)

        for k, g in groups.items():
            if k == best["key"]:
                continue
            for f in g["files"]:
                d_item = dict(f)
                d_item["reason"] = "零散不完整残卷版本"
                to_delete.append(d_item)

    else:
        # 无单一完整版本：检查混版能否拼齐全集，按已有集数优选最佳画质并清理低质冗余
        all_missing = [n for n in expected_range if n not in found_episodes]
        missing_desc = format_missing_episodes_text(all_missing) if all_missing else ""

        if all_missing:
            version_note = f"{missing_desc}，按已有集数优选最佳画质并清理冗余副本"
            target_eps = found_episodes
        else:
            version_note = f"各集版本不同但已拼齐 (共 {total_episodes} 集)，优选各集最佳画质"
            target_eps = expected_range

        for ep in target_eps:
            copies = [p for p in valid_parsed if ep in p.get("episodes", [p.get("episode")])]
            if not copies:
                continue
            copies.sort(key=rank_copy, reverse=True)
            chosen = copies[0]
            if not any(k["path"] == chosen["path"] for k in to_keep):
                to_keep.append(chosen)
            for dup in copies[1:]:
                d_item = dict(dup)
                d_item["reason"] = "同集较低画质或冗余副本"
                if not any(d["path"] == d_item["path"] for d in to_delete) and not any(k["path"] == d_item["path"] for k in to_keep):
                    to_delete.append(d_item)

    # 衍生花絮文件绝对保护保留
    for ef in extra_files:
        to_keep.append(ef)

    # 6. 外挂字幕与单集刮削级联清理
    kept_paths = {f["path"] for f in to_keep}
    deleted_paths = {f["path"] for f in to_delete}

    for f in files:
        f_name = f.get("name", "")
        is_sub = bool(SUBTITLE_REGEX.search(f_name))
        is_scrape = bool(SCRAPE_REGEX.search(f_name))
        if not is_sub and not is_scrape:
            continue

        stem = re.sub(r"\.[^.]+$", "", f_name)
        # 全局公共元数据坚决不删
        if is_scrape and SHARED_SCRAPE.search(stem):
            continue

        clean_stem = re.sub(r"\.(?:zh|chs|cht|chi|eng|sc|tc|jpn|default|forced)$", "", stem, flags=re.IGNORECASE)

        matched_deleted = [
            v for v in to_delete
            if stem.lower() == re.sub(r"\.[^.]+$", "", v.get("name", "")).lower()
            or stem.lower() == re.sub(r"\.[^.]+$", "", v.get("name", "")).lower() + "-thumb"
            or clean_stem.lower() == re.sub(r"\.[^.]+$", "", v.get("name", "")).lower()
        ]
        matched_kept = [
            v for v in to_keep
            if stem.lower() == re.sub(r"\.[^.]+$", "", v.get("name", "")).lower()
            or stem.lower() == re.sub(r"\.[^.]+$", "", v.get("name", "")).lower() + "-thumb"
            or clean_stem.lower() == re.sub(r"\.[^.]+$", "", v.get("name", "")).lower()
        ]

        if is_scrape:
            # 刮削文件（.nfo, .jpg, .png 等）：若不属于任何当前保留的有效视频，属于冗余伴生或孤儿残留
            if not matched_kept:
                reason = "对应已淘汰视频的单集刮削文件" if matched_deleted else "无对应视频文件的孤儿残留刮削文件"
                to_delete.append({
                    "episode": episode_from_any_name(f_name, season_number),
                    "name": f_name,
                    "path": f.get("path"),
                    "modified": f.get("modified"),
                    "size": f.get("size", 0),
                    "reason": reason,
                })
        elif is_sub:
            # 外挂字幕文件：遵循核心安全原则，默认绝对保护不删
            # 即使对应视频被淘汰，外挂字幕也安全保留防误删，坚决不加入待删除清单
            continue

    # ── 终道收敛与绝对安全不变量校验 (P0-2) ───────────────────
    # 1. 绝对互斥性：任何进入 to_keep 的文件绝对禁止出现在 to_delete 中！
    kept_paths = {f["path"] for f in to_keep if f.get("path")}
    dedup_to_delete: List[Dict[str, Any]] = []
    seen_del_paths: Set[str] = set()

    for d in to_delete:
        d_path = d.get("path")
        if not d_path or d_path in kept_paths or d_path in seen_del_paths:
            continue
        seen_del_paths.add(d_path)
        dedup_to_delete.append(d)

    # 2. 单调不减性：删除任何视频文件绝对不得导致正片集号覆盖减少！
    final_to_delete: List[Dict[str, Any]] = []
    current_kept_eps = set()
    for k in to_keep:
        current_kept_eps.update(k.get("episodes", [k.get("episode")]))

    for d in dedup_to_delete:
        # 如果是伴生刮削或字幕文件，直接保留在删除列表
        d_eps = set(d.get("episodes", [d.get("episode")])) if d.get("episodes") or d.get("episode") else set()
        if d_eps and not d_eps.issubset(current_kept_eps):
            # 存在未被覆盖的唯一集号！具有绝对免死保护，坚决不能删，转入 to_keep！
            to_keep.append(d)
            kept_paths.add(d["path"])
            current_kept_eps.update(d_eps)
        else:
            final_to_delete.append(d)

    to_delete = final_to_delete

    can_clean = len(to_delete) > 0
    all_missing = [n for n in expected_range if n not in found_episodes]
    missing_desc = format_missing_episodes_text(all_missing) if all_missing else ""

    if can_clean:
        reason = "OK"
    else:
        if all_missing:
            reason = f"现有各集无多余冗余副本 ({missing_desc})"
        else:
            reason = "全剧已就绪，无需清理任何文件"

    return {
        "can_clean": can_clean,
        "reason": reason,
        "version_note": version_note,
        "to_keep": to_keep,
        "to_delete": to_delete,
        "status": "ready" if can_clean else "clean_done",
    }
