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

import hashlib
import json
import os
import posixpath
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

from app.core.episode_parser import (
    companion_matches_video,
    classify_content_unit,
    detect_segment_part,
    parse_episode,
)

REVIEW_RULES_VERSION = "v2.5.53"

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
    r"(?:^|[\s._\-\(\[])(?:日记|Diary|花活儿|Extra|花絮|彩蛋|纯享|采访|访谈|先行片|先导(?:片)?|超前(?:企划)?|预告|Preview|Behind[._\s-]the[._\s-]Scenes|Making[._\s-]of|SP\d*)(?:$|[\s._\-\)\]])",
    re.IGNORECASE,
)
SPECIAL_E00_REGEX = re.compile(
    r"(?:^|[^a-z0-9])[Ss]0*\d+[Ee]00(?!\d)|(?:^|[^a-z0-9])[Ee]00(?!\d)|(?:^|[^a-z0-9])SP0*1(?!\d)",
    re.IGNORECASE,
)
TMP_DOWNLOAD_REGEX = re.compile(r"\.(?:part|tmp|temp|download|downloading|crdownload|!qb|aria2|xltd)$", re.IGNORECASE)


def rank_copy(f: Dict[str, Any]) -> Tuple[int, int, int, int, str]:
    """同内容等价组重复副本梯级打分（分辨率规格 > 标准命名规整度 > 文件体积 > 编码偏好 > 修改时间戳）。"""
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

    # 编码偏好（同体积/同分辨率平局决胜：HEVC/H.265 > H.264/AVC）
    codec_score = 0
    if re.search(r"HEVC|H\.?265|x265", f_name, re.I):
        codec_score = 2
    elif re.search(r"H\.?264|x264|AVC", f_name, re.I):
        codec_score = 1

    mod_str = str(f.get("modified") or "")
    return (res_score, clean_score, size, codec_score, mod_str)


def _strip_cd2_dup_suffix(path: str) -> str:
    """剥离 CloudDrive2/网盘同名冲突自动附加的 (N) 后缀，用于识别纯展示序号交换。"""
    parent, name = posixpath.split(str(path or ""))
    clean_name = re.sub(r"\(\d+\)(?=\.[^./\\]+$|$)", "", name)
    return posixpath.join(parent, clean_name) if parent else clean_name


def build_file_fingerprints(files: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """构建文件稳定指纹映射（路径 -> 大小/修改时间/文件ID），用于执行前局部复核防扩大删除范围 (G24)。"""
    fps: Dict[str, Dict[str, Any]] = {}
    for f in files or []:
        if f.get("isDirectory"):
            continue
        p = str(f.get("path") or f.get("name") or "")
        if not p:
            continue
        fps[p] = {
            "id": str(f.get("id") or ""),
            "name": str(f.get("name") or ""),
            "size": int(f.get("size") or 0),
            "modified": str(f.get("modified") or ""),
        }
    return fps


def compare_plan_fingerprints(
    plan_fingerprints: Dict[str, Dict[str, Any]],
    current_files: List[Dict[str, Any]],
    planned_delete_paths: Set[str],
) -> Dict[str, Any]:
    """执行前局部复核文件变化（G24）：检测新增/变化文件是否会导致删除范围扩大或预览失效。

    兼容 CloudDrive2 对 115 同名文件动态分配 `(1)` 后缀时发生的顺序交换：
    若同一基础文件名簇内文件数量不变且 `(id, size, modified)`（或无 id 时的 `(size, modified)` 多重集）完全一致，
    说明物理文件未发生任何增删改，不触发误报重预览。
    """
    from collections import Counter

    curr_fps = build_file_fingerprints(current_files)

    plan_groups: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    for p, meta in (plan_fingerprints or {}).items():
        plan_groups.setdefault(_strip_cd2_dup_suffix(p), []).append((p, meta))

    curr_groups: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    for p, meta in curr_fps.items():
        curr_groups.setdefault(_strip_cd2_dup_suffix(p), []).append((p, meta))

    stable_swapped_paths: Set[str] = set()
    for base_key in set(plan_groups) & set(curr_groups):
        pg = plan_groups[base_key]
        cg = curr_groups[base_key]
        if len(pg) <= 1 or len(pg) != len(cg):
            continue
        p_ids = [str(m.get("id") or "") for _, m in pg]
        c_ids = [str(m.get("id") or "") for _, m in cg]
        matched = False
        if all(p_ids) and all(c_ids):
            p_by_id = {str(m.get("id")): (int(m.get("size") or 0), str(m.get("modified") or "")) for _, m in pg}
            c_by_id = {str(m.get("id")): (int(m.get("size") or 0), str(m.get("modified") or "")) for _, m in cg}
            matched = p_by_id == c_by_id
        else:
            p_multi = Counter((int(m.get("size") or 0), str(m.get("modified") or "")) for _, m in pg)
            c_multi = Counter((int(m.get("size") or 0), str(m.get("modified") or "")) for _, m in cg)
            matched = p_multi == c_multi
        if matched:
            stable_swapped_paths.update(p for p, _ in pg)
            stable_swapped_paths.update(p for p, _ in cg)

    added = sorted(p for p in (set(curr_fps) - set(plan_fingerprints)) if p not in stable_swapped_paths)
    removed = sorted(p for p in (set(plan_fingerprints) - set(curr_fps)) if p not in stable_swapped_paths)
    modified = sorted(
        p for p in (set(curr_fps) & set(plan_fingerprints))
        if p not in stable_swapped_paths
        and (
            curr_fps[p]["size"] != plan_fingerprints[p]["size"]
            or (curr_fps[p]["id"] and plan_fingerprints[p].get("id") and curr_fps[p]["id"] != plan_fingerprints[p]["id"])
            or (curr_fps[p]["modified"] and plan_fingerprints[p]["modified"] and curr_fps[p]["modified"] != plan_fingerprints[p]["modified"])
        )
    )
    # 仅新增视频或修改了关键视频/待删文件才触发重新预览
    added_videos = [p for p in added if MEDIA_REGEX.search(curr_fps[p]["name"])]
    modified_videos = [p for p in modified if MEDIA_REGEX.search(curr_fps[p]["name"])]
    missing_delete_targets = [p for p in removed if p in planned_delete_paths]
    changed_delete_targets = [p for p in added + modified if p in planned_delete_paths]
    needs_repreview = bool(added_videos or modified_videos or changed_delete_targets)
    return {
        "changed": bool(added or removed or modified),
        "needs_repreview": needs_repreview,
        "added_files": added,
        "added_videos": added_videos,
        "removed_files": removed,
        "modified_files": modified,
        "modified_videos": modified_videos,
        "missing_delete_targets": missing_delete_targets,
        "changed_delete_targets": changed_delete_targets,
    }


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
    """从文件名中提取所有集号（支持范围集如 S01E01-E02、EP01-03、第01-02集 等多集格式）。"""
    if not filename or not MEDIA_REGEX.search(filename):
        return []

    name = re.sub(r"\.[^.]+$", "", filename)

    # 1. 优先提取明确的季集号与分集编号
    extracted: List[int] = []
    if season is not None:
        se_pattern = re.compile(rf"(?:^|[^a-z0-9])S0*{season}\s*E(\d{{1,4}})(?:-[Ee]?(\d{{1,4}}))?(?!\d)", re.IGNORECASE)
        m = se_pattern.search(name)
        if m:
            s_ep = int(m.group(1))
            e_ep = int(m.group(2)) if m.group(2) else s_ep
            extracted = list(range(s_ep, e_ep + 1))

    if not extracted:
        m = re.search(r"(?:^|[^a-z0-9])S\d{1,4}\s*E(\d{1,4})(?:-[Ee]?(\d{1,4}))?(?!\d)", name, re.IGNORECASE)
        if m:
            s_ep = int(m.group(1))
            e_ep = int(m.group(2)) if m.group(2) else s_ep
            extracted = list(range(s_ep, e_ep + 1))

    if not extracted:
        m = re.search(r"第\s*(\d{1,4})(?:\s*[-~至到]\s*(\d{1,4}))?\s*[集话話回章]", name)
        if m:
            s_ep = int(m.group(1))
            e_ep = int(m.group(2)) if m.group(2) else s_ep
            extracted = list(range(s_ep, e_ep + 1))

    if not extracted:
        m = re.search(r"(?:^|[\s._\-[\]])(?:EP|E)\s*(\d{1,4})(?:-[Ee]?[Pp]?(\d{1,4}))?(?!\d)", name, re.IGNORECASE)
        if m:
            s_ep = int(m.group(1))
            e_ep = int(m.group(2)) if m.group(2) else s_ep
            extracted = list(range(s_ep, e_ep + 1))

    if not extracted:
        m = re.search(r"[\s._-](\d{2,4})(?:v\d+)?[\s._\-\]]", name)
        if m:
            ep_val = int(m.group(1))
            if 0 < ep_val < 1000:
                extracted = [ep_val]

    # 2. 特典文件安全识别：严格词边界，严防误伤 CMCTV、CMCT、PreviewX 等发布组标签
    # 且若文件名已携带标准 SxxExx 明确正片集号（>=1），具有绝对正片优先级，禁止视作纯特典抛弃
    has_explicit_episode = bool(extracted and any(e > 0 for e in extracted) and re.search(r"[Ss]\d{1,2}[Ee]\d+", name))
    if not has_explicit_episode:
        is_pure_special = bool(re.search(
            r"(?:^|[\s._\-[\]])(?:NCOP|NCED|PV|CM|Menu|Creditless|Textless|CleanOP|CleanED)\d*(?:$|[\s._\-[\]])",
            name,
            re.IGNORECASE,
        ))
        if is_pure_special:
            return []

    return extracted


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
    active_download_paths: Optional[Set[str]] = None,
    verified_deleted_video_names: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    v2.5.45 统一内容等价组复核清理计划生成器（预览与执行共用同一计划与证据版本）。

    核心约束：
    1. 统一内容单元：(集号元组, 分段, 内容类别, 剪辑版本)。
       - 普通版与加长版（edit_version="extended"）绝不互相淘汰 (G04)；
       - 正片与 Vlog/加更/纯享/先导/彩蛋等衍生内容（content_kind != "regular"）绝不互相淘汰 (G03)；
       - 上/中/下段与整期版（segment_part）在无完整包含证据前绝不互相淘汰，且单个分段/子目录不阻塞其他已证实重复副本的清理 (G05)；
    2. 质量优选（rank_copy / score_group）仅在同内容等价组内进行；
    3. 临时下载中或活动下载路径依赖的组仅局部延后，不阻塞其他已稳定副本 (G02)；
    4. 最终单调性校验按内容等价键校验，且产出 plan_id、rules_version、file_fingerprints 及 all_distinct_retained (G24, G26)。
    """
    from collections import Counter
    import hashlib

    file_fingerprints = build_file_fingerprints(files)
    fp_sig = "|".join(
        f"{k}:{v.get('size', 0)}:{v.get('modified', '')}"
        for k, v in sorted(file_fingerprints.items())
    )
    plan_hash = hashlib.sha1(f"{REVIEW_RULES_VERSION}:{season_number}:{total_episodes}:{fp_sig}".encode("utf-8")).hexdigest()[:12]
    plan_id = f"plan_clean_{plan_hash}"
    active_paths_norm = {
        str(p).strip().rstrip("/").lower()
        for p in (active_download_paths or set())
        if p
    }

    preserved_subdirs = [f for f in files if f.get("isDirectory")]
    tmp_files = [f for f in files if not f.get("isDirectory") and TMP_DOWNLOAD_REGEX.search(f.get("name", ""))]
    videos = [f for f in files if not f.get("isDirectory") and MEDIA_REGEX.search(f.get("name", ""))]

    # 1. 安全门禁：0 字节视频安全熔断
    zero_byte = [f for f in videos if int(f.get("size") or 0) <= 0]
    if zero_byte:
        names = "、".join(f.get("name", "") for f in zero_byte[:2])
        return {
            "plan_id": plan_id,
            "rules_version": REVIEW_RULES_VERSION,
            "file_fingerprints": file_fingerprints,
            "can_clean": False,
            "reason": f"发现 {len(zero_byte)} 个 0 字节损坏视频 ({names})，熔断保护",
            "version_note": "发现损坏视频，熔断保护",
            "status": "skip",
            "to_keep": [],
            "to_delete": [],
            "deferred_files": [],
            "preserved_subdirs": [d.get("name", "") for d in preserved_subdirs],
            "unparsed_videos": [],
            "all_distinct_retained": False,
            "distinct_retention_note": "",
        }

    # 识别活动下载/临时文件影响的集号，仅延后相关集，不盲目整季熔断（若无其他可清副本则提示等待完成）
    tmp_affected_eps: Set[int] = set()
    deferred_files: List[Dict[str, Any]] = []
    for tf in tmp_files:
        t_ep = episode_from_any_name(tf.get("name", ""), season_number)
        if t_ep is not None:
            tmp_affected_eps.add(t_ep)
        deferred_files.append({
            "name": tf.get("name", ""),
            "path": tf.get("path", ""),
            "reason": "临时下载写入中，已局部延后",
        })

    # 2. 统一内容单元分类解析
    regular_standard_parsed: List[Dict[str, Any]] = []
    other_content_groups: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
    unparsed_videos: List[Dict[str, Any]] = []
    raw_ep_counter: Counter[int] = Counter()

    for f in videos:
        f_name = f.get("name", "")
        f_path = f.get("path") or ""
        f_path_norm = str(f_path).strip().rstrip("/").lower()

        unit = classify_content_unit(f_name, season_number=season_number, filepath=f_path, file_size=int(f.get("size") or 0))
        eps = unit["episode_numbers"] or episodes_from_name(f_name, season_number)
        seg_part = unit["segment_part"]
        c_kind = unit["content_kind"]
        e_ver = unit["edit_version"]
        c_label = unit["content_label"]

        # 兼容 EXTRA_REGEX（仅当未识别为正片加长版等明确剪辑版本时）
        if c_kind == "regular" and e_ver == "standard" and EXTRA_REGEX.search(f_name):
            c_kind = "special"
            c_label = "衍生花絮/特别篇"

        # E00 先导特辑保护
        if eps and ((eps == [0] or (len(eps) == 1 and eps[0] == 0)) or (bool(SPECIAL_E00_REGEX.search(f_name)) and not any(e > 0 for e in eps))):
            c_kind = "pilot"
            c_label = "先导/特辑(E00)"

        # 检查是否受活动下载路径依赖或临时下载文件影响（仅局部延后）
        is_active_dep = bool(
            (f_path_norm and any(f_path_norm == ap or ap in f_path_norm or f_path_norm in ap for ap in active_paths_norm))
            or (eps and any(ep in tmp_affected_eps for ep in eps))
        )
        if is_active_dep:
            deferred_files.append({
                "name": f_name,
                "path": f_path,
                "episodes": eps,
                "content_label": c_label,
                "reason": "关联活动下载/写入路径，已局部延后不清理",
            })
            # 延后文件记入保留，防止被当成孤儿误删伴生
            unparsed_videos.append({
                "episode": eps[0] if eps else None,
                "episodes": eps,
                "key": "deferred_active",
                "name": f_name,
                "path": f_path,
                "modified": f.get("modified"),
                "size": f.get("size", 0),
                "content_kind": c_kind,
                "edit_version": e_ver,
                "segment_part": seg_part,
                "content_label": c_label,
                "content_unit_key": (tuple(eps), seg_part, c_kind, e_ver),
                "reason": "活动下载写入中，保留暂缓清理",
            })
            continue

        if not eps and c_kind == "regular":
            # 无法解析集号的视频：安全保留，绝不删其伴生 NFO/图片
            unparsed_videos.append({
                "episode": None,
                "episodes": [],
                "key": "unparsed",
                "name": f_name,
                "path": f_path,
                "modified": f.get("modified"),
                "size": f.get("size", 0),
                "content_kind": "unknown",
                "edit_version": e_ver,
                "segment_part": seg_part,
                "content_label": "未识别视频(安全保留)",
                "content_unit_key": ("unparsed", f_path or f_name),
                "reason": "未识别内容单元，安全保留",
            })
            continue

        for e_num in eps:
            if isinstance(e_num, int) and e_num > 0:
                raw_ep_counter[e_num] += 1

        stem = re.sub(r"\.[^.]+$", "", f_name)
        se_re = re.compile(rf"S0*{season_number}\s*E\d+(?:-[Ee]?\d+)?", re.IGNORECASE)
        template = se_re.sub(f"S{season_number}E{{集}}", stem)
        if template == stem:
            template = re.sub(r"第\s*\d+(?:\s*[-~至到]\s*\d+)?\s*集", "第{集}集", template)
        m_ext = re.search(r"\.[^.]+$", f_name)
        ext = m_ext.group(0) if m_ext else ""
        key = re.sub(r"\s+", " ", template).strip().lower() + ext

        content_unit_key = (tuple(eps), seg_part, c_kind, e_ver)
        item_dict = {
            "episode": eps[0] if eps else None,
            "episodes": eps,
            "key": key,
            "name": f_name,
            "path": f_path,
            "modified": f.get("modified"),
            "size": int(f.get("size") or 0),
            "content_kind": c_kind,
            "edit_version": e_ver,
            "segment_part": seg_part,
            "content_label": c_label,
            "content_unit_key": content_unit_key,
        }

        if c_kind == "regular" and e_ver == "standard" and seg_part is None and eps and all(e > 0 for e in eps):
            regular_standard_parsed.append(item_dict)
        else:
            # 非普通正片整期流（加长版、Vlog、加更、纯享、先导、上下段等）：严格按独立内容等价键分组
            group_key = (tuple(eps), seg_part, c_kind, e_ver) if eps else ("non_ep", c_kind, e_ver, seg_part, re.sub(r"[\s._-]+", " ", stem).strip().lower())
            other_content_groups.setdefault(group_key, []).append(item_dict)

    all_found_eps: Set[int] = set()
    for p in regular_standard_parsed:
        all_found_eps.update(p.get("episodes", []))
    for grp_items in other_content_groups.values():
        for p in grp_items:
            if p.get("content_kind") == "regular":
                all_found_eps.update(e for e in (p.get("episodes") or []) if e > 0)
    found_episodes = sorted(all_found_eps)
    expected_range = list(range(1, max(int(total_episodes or 0), 0) + 1))

    to_keep: List[Dict[str, Any]] = []
    to_delete: List[Dict[str, Any]] = []
    version_notes: List[str] = []

    # 3. 普通正片整期流 (regular + standard + segment_part=None) 完整版/残卷组内仲裁
    groups: Dict[str, Dict[str, Any]] = {}
    for p in regular_standard_parsed:
        k = p["key"]
        if k not in groups:
            groups[k] = {"key": k, "files": [], "episodes": []}
        groups[k]["files"].append(p)
        groups[k]["episodes"].extend(p.get("episodes", []))

    for g in groups.values():
        g_eps = sorted(set(g["episodes"]))
        g["episodes"] = g_eps
        g["missing"] = [n for n in expected_range if n not in g_eps]
        g["complete"] = (len(expected_range) > 0 and len(g["missing"]) == 0)

    complete_groups = [g for g in groups.values() if g["complete"]]

    def _pick_best_for_ep(copies: List[Dict[str, Any]], dup_reason: str) -> None:
        copies_sorted = sorted(copies, key=rank_copy, reverse=True)
        chosen = dict(copies_sorted[0])
        chosen.setdefault("keep_reason", f"{chosen.get('content_label', '正片·普通版')}优选保留")
        chosen_key = chosen.get("path") or chosen.get("name") or ""
        if not any((k.get("path") or k.get("name") or "") == chosen_key for k in to_keep):
            to_keep.append(chosen)
        for dup in copies_sorted[1:]:
            d_item = dict(dup)
            d_item["reason"] = dup_reason
            d_item["replaced_by"] = chosen["name"]
            d_item["replaced_by_path"] = chosen["path"]
            d_item["equivalence_basis"] = f"同内容等价组（{chosen.get('content_label', '正片·普通版')}），由更高画质/规范副本 [{chosen['name']}] 替代"
            d_key = d_item.get("path") or d_item.get("name") or ""
            if not any((d.get("path") or d.get("name") or "") == d_key for d in to_delete) and not any((k.get("path") or k.get("name") or "") == d_key for k in to_keep):
                to_delete.append(d_item)

    if len(complete_groups) > 1:
        complete_groups.sort(key=score_group, reverse=True)
        best = complete_groups[0]
        version_notes.append(f"正片普通版存在 {len(complete_groups)} 个完整版本，优选保留 [{best['key']}]，淘汰低质版本")
        best_by_ep: Dict[int, Dict[str, Any]] = {}
        for ep in expected_range:
            copies = [f for f in best["files"] if ep in f.get("episodes", [])]
            if not copies:
                continue
            _pick_best_for_ep(copies, "同版本较旧或冗余副本")
            best_by_ep[ep] = sorted(copies, key=rank_copy, reverse=True)[0]
        for inferior_g in complete_groups[1:]:
            for f in inferior_g["files"]:
                d_item = dict(f)
                rep = best_by_ep.get(f["episode"])
                d_item["reason"] = f"淘汰低画质完整版本 ({inferior_g['key']})"
                d_item["replaced_by"] = rep["name"] if rep else best["key"]
                d_item["equivalence_basis"] = f"同正片普通版等价组，由更优完整版 [{d_item['replaced_by']}] 覆盖"
                to_delete.append(d_item)
        for k, g in groups.items():
            if g in complete_groups:
                continue
            for f in g["files"]:
                d_item = dict(f)
                rep = best_by_ep.get(f["episode"])
                d_item["reason"] = "其他不完整残卷版本"
                d_item["replaced_by"] = rep["name"] if rep else best["key"]
                d_item["equivalence_basis"] = f"同正片普通版等价组，由完整版 [{d_item['replaced_by']}] 覆盖"
                to_delete.append(d_item)
    elif len(complete_groups) == 1:
        best = complete_groups[0]
        version_notes.append(f"保留正片普通版完整版本: {best['key']}")
        best_by_ep = {}
        for ep in expected_range:
            copies = [f for f in best["files"] if ep in f.get("episodes", [])]
            if not copies:
                continue
            _pick_best_for_ep(copies, "同版本较旧或冗余副本")
            best_by_ep[ep] = sorted(copies, key=rank_copy, reverse=True)[0]
        for k, g in groups.items():
            if k == best["key"]:
                continue
            for f in g["files"]:
                d_item = dict(f)
                rep = best_by_ep.get(f["episode"])
                d_item["reason"] = "零散不完整残卷版本"
                d_item["replaced_by"] = rep["name"] if rep else best["key"]
                d_item["equivalence_basis"] = f"同正片普通版等价组，由完整版 [{d_item['replaced_by']}] 覆盖"
                to_delete.append(d_item)
    elif regular_standard_parsed:
        std_eps = sorted({e for p in regular_standard_parsed for e in p.get("episodes", [])})
        all_missing = [n for n in expected_range if n not in found_episodes]
        missing_desc = format_missing_episodes_text(all_missing) if all_missing else ""
        if all_missing:
            version_notes.append(f"{missing_desc}，按已有同内容组优选最佳画质并清理冗余副本")
            target_eps = std_eps
        else:
            version_notes.append(f"各集版本不同但已拼齐 (共 {total_episodes} 集)，按同内容组优选各集最佳画质")
            target_eps = std_eps
        for ep in target_eps:
            copies = [p for p in regular_standard_parsed if ep in p.get("episodes", [])]
            if not copies:
                continue
            _pick_best_for_ep(copies, "同集较低画质或冗余副本")

    # 4. 其他内容等价组（加长版、Vlog、加更、纯享、先导、上下段等）：各自独立成组，绝不跨组淘汰！
    retained_distinct_labels: List[str] = []
    for grp_key, grp_items in other_content_groups.items():
        if not grp_items:
            continue
        label = grp_items[0].get("content_label") or "独立内容版本"
        if label not in retained_distinct_labels:
            retained_distinct_labels.append(label)
        eps_tuple, seg_p, c_kind, e_ver = grp_key
        # E00 / 特辑 / 先导 / 非正片衍生内容（Vlog/加更/纯享/彩蛋/访谈）不同条目绝不互相淘汰！
        is_protected_special_or_derivative = (
            (not eps_tuple)
            or (0 in eps_tuple)
            or (c_kind != "regular")
        )
        if len(grp_items) == 1 or is_protected_special_or_derivative:
            for item_keep in grp_items:
                kept_one = dict(item_keep)
                kept_one["keep_reason"] = f"{label}独立保留（不与普通正片或其他特辑互删）"
                to_keep.append(kept_one)
        else:
            # 同一正片剪辑版/分段等价组（如加长版 1080p vs 2160p）内存在多个技术副本时，仅在组内优选清理真副本 (G04)
            grp_sorted = sorted(grp_items, key=rank_copy, reverse=True)
            chosen = dict(grp_sorted[0])
            chosen["keep_reason"] = f"{label}组内优选保留"
            to_keep.append(chosen)
            for dup in grp_sorted[1:]:
                d_item = dict(dup)
                d_item["reason"] = f"同【{label}】内容组内的低画质或冗余技术副本"
                d_item["replaced_by"] = chosen["name"]
                d_item["replaced_by_path"] = chosen["path"]
                d_item["equivalence_basis"] = f"同内容等价组（{label}），由更高画质/规范副本 [{chosen['name']}] 替代"
                to_delete.append(d_item)

    if retained_distinct_labels:
        version_notes.append(f"已独立保留内容版本：{'、'.join(retained_distinct_labels[:6])}")

    # 未识别视频 / 活动延后视频保留，防止其伴生文件被误删
    for uv in unparsed_videos:
        to_keep.append(uv)

    # ── 终道收敛与绝对安全不变量校验 (P0-2 按内容等价键单调不减) ──
    kept_keys = {(f.get("path") or f.get("name") or "") for f in to_keep if (f.get("path") or f.get("name"))}
    dedup_to_delete: List[Dict[str, Any]] = []
    seen_del_keys: Set[str] = set()

    for d in to_delete:
        d_key = d.get("path") or d.get("name") or ""
        if not d_key or d_key in kept_keys or d_key in seen_del_keys:
            continue
        seen_del_keys.add(d_key)
        dedup_to_delete.append(d)

    # 单调不减性：删除任何视频文件绝对不得导致任何内容等价键 (episodes, segment_part, content_kind, edit_version) 覆盖减少！
    final_to_delete: List[Dict[str, Any]] = []
    current_kept_content_keys: Set[Any] = {
        k.get("content_unit_key") for k in to_keep if k.get("content_unit_key") is not None
    }
    current_kept_eps: Set[int] = set()
    for k in to_keep:
        if k.get("content_kind", "regular") == "regular":
            for e in (k.get("episodes") or ([k.get("episode")] if k.get("episode") else [])):
                if isinstance(e, int):
                    current_kept_eps.add(e)

    for d in dedup_to_delete:
        is_vid_del = bool(MEDIA_REGEX.search(d.get("name", "")))
        if is_vid_del:
            d_ckey = d.get("content_unit_key")
            d_eps = {e for e in (d.get("episodes") or []) if isinstance(e, int)}
            must_protect = False
            if d_ckey is not None and d_ckey not in current_kept_content_keys:
                must_protect = True
            elif d.get("content_kind", "regular") == "regular" and d_eps and not d_eps.issubset(current_kept_eps):
                must_protect = True
            if must_protect:
                d["keep_reason"] = "内容等价覆盖保护，禁止删除独有内容"
                to_keep.append(d)
                kept_keys.add(d.get("path") or d.get("name") or "")
                if d_ckey is not None:
                    current_kept_content_keys.add(d_ckey)
                if d.get("content_kind", "regular") == "regular":
                    current_kept_eps.update(d_eps)
                continue
        final_to_delete.append(d)

    to_delete = final_to_delete

    # 5. 最终视频保留/删除集合确定后，才计算附件依赖；被保护视频的附件同步保留。
    # 外挂字幕绝对保护 + 伴生刮削级联清理（无关联依据的 NFO/图片保留）
    kept_video_eps: Set[int] = set()
    for kv in to_keep:
        for e in (kv.get("episodes") or []):
            if isinstance(e, int) and e > 0:
                kept_video_eps.add(e)

    for f in files:
        if f.get("isDirectory"):
            continue
        f_name = f.get("name", "")
        is_sub = bool(SUBTITLE_REGEX.search(f_name))
        is_scrape = bool(SCRAPE_REGEX.search(f_name))
        if not is_sub and not is_scrape:
            continue

        stem = re.sub(r"\.[^.]+$", "", f_name)
        if is_scrape and SHARED_SCRAPE.search(stem):
            continue

        clean_stem = re.sub(r"\.(?:zh|chs|cht|chi|eng|sc|tc|jpn|default|forced)$", "", stem, flags=re.IGNORECASE)

        matched_deleted = [v for v in to_delete if MEDIA_REGEX.search(v.get("name", "")) and companion_matches_video(f_name, v.get("name", ""))]
        matched_kept = [v for v in to_keep if companion_matches_video(f_name, v.get("name", ""))]
        historical_parent = next((v for v in sorted(verified_deleted_video_names or ()) if companion_matches_video(f_name, v)), "")

        if is_scrape:
            if matched_kept:
                continue
            ep_in_scrape = episode_from_any_name(f_name, season_number)
            # 仅清理属于已淘汰视频的伴生刮削，或明确带集号且该集已有保留视频覆盖的孤儿单集刮削；无关联依据的一律保留
            if matched_deleted or historical_parent:
                reason = "对应已淘汰视频的单集刮削文件" if matched_deleted else "无对应视频文件的孤儿残留刮削文件"
                to_delete.append({
                    "episode": ep_in_scrape,
                    "name": f_name,
                    "path": f.get("path"),
                    "modified": f.get("modified"),
                    "size": f.get("size", 0),
                    "reason": reason,
                    "replaced_by": matched_deleted[0].get("replaced_by", "") if matched_deleted else "",
                    "equivalence_basis": "关联本次待删视频或已核验删除的视频回执",
                    "parent_video_path": matched_deleted[0].get("path", "") if matched_deleted else "",
                    "historical_parent": historical_parent,
                })
        elif is_sub:
            # 外挂字幕绝对保护不删
            continue

    can_clean = len(to_delete) > 0
    all_missing = [n for n in expected_range if n not in found_episodes]
    missing_desc = format_missing_episodes_text(all_missing) if all_missing else ""

    raw_dup_eps = sorted(ep for ep, cnt in raw_ep_counter.items() if cnt > 1)
    all_distinct_retained = bool(raw_dup_eps and not can_clean and not deferred_files)
    distinct_retention_note = (
        f"多内容版本均已保留（第 {','.join(map(str, raw_dup_eps[:8]))} 集共存不同版本/衍生/分段，无可淘汰重复副本）"
        if all_distinct_retained
        else ""
    )

    # 兼容无其它可删文件但存在临时下载文件/子目录时的明确提示
    if can_clean:
        reason = "OK"
    elif tmp_files:
        reason = "发现临时下载文件，等待完成"
    elif preserved_subdirs and not regular_standard_parsed and not other_content_groups:
        reason = "季内存在子目录，需人工确认"
    elif all_distinct_retained:
        reason = distinct_retention_note
    elif all_missing:
        reason = f"现有各集无多余冗余副本 ({missing_desc})"
    else:
        reason = "全剧已就绪，无需清理任何文件"

    # 序列化时将 tuple 类型的 content_unit_key 转为 list 以便 JSON 持久化
    for coll in (to_keep, to_delete):
        for item in coll:
            if isinstance(item.get("content_unit_key"), tuple):
                item["content_unit_key"] = list(item["content_unit_key"])

    reclaimable_bytes = sum(int(d.get("size") or 0) for d in to_delete)
    version_note = "；".join(version_notes) if version_notes else (distinct_retention_note or reason)

    return {
        "plan_id": plan_id,
        "rules_version": REVIEW_RULES_VERSION,
        "file_fingerprints": file_fingerprints,
        "can_clean": can_clean,
        "reason": reason,
        "version_note": version_note,
        "to_keep": to_keep,
        "to_delete": to_delete,
        "reclaimable_bytes": reclaimable_bytes,
        "deferred_files": deferred_files,
        "preserved_subdirs": [d.get("name", "") for d in preserved_subdirs],
        "unparsed_videos": [u.get("name", "") for u in unparsed_videos if u.get("key") == "unparsed"],
        "all_distinct_retained": all_distinct_retained,
        "distinct_retention_note": distinct_retention_note,
        "status": "ready" if can_clean else ("skip" if tmp_files else "clean_done"),
    }
