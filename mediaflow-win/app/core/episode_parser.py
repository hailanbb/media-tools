"""集号解析引擎 — 从文件名中提取集号、季号、版本信息。

解析规则覆盖中日韩剧集的常见命名模式：
- 标准格式：S01E01, s01e01
- 简写格式：EP01, E01, 第01集, 第1话
- 范围格式：E01-E03, EP01-03
- 特殊标记：SP, OVA, OAD, 番外
- 版本标记：[4K], [2160p], [1080p], [720p], [HEVC], [x265], [HDR]
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ParsedEpisode:
    """单个视频文件的解析结果。"""

    filename: str
    filepath: str
    season_number: int | None = None
    episode_numbers: list[int] = field(default_factory=list)
    is_special: bool = False  # SP/OVA/番外
    is_extra: bool = False    # 衍生花絮/日记/花活儿/彩蛋等
    resolution: str | None = None  # 4K/2160p/1080p/720p
    codec: str | None = None  # HEVC/x265/x264/AVC
    is_hdr: bool = False
    subtitle_languages: list[str] = field(default_factory=list)
    file_size_bytes: int = 0
    file_ext: str = ""


# ── 视频文件扩展名白名单 ────────────────────────────────
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".ts", ".wmv", ".rmvb", ".flv", ".mov", ".m2ts",
}

# ── 字幕文件扩展名 ──────────────────────────────────────
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup"}

# ── 集号提取正则（按优先级排列）──────────────────────────
_EPISODE_PATTERNS: list[tuple[str, str]] = [
    # S01E01-E03  / S01E01
    (r"[Ss](\d{1,2})[Ee](\d{1,4})(?:-[Ee]?(\d{1,4}))?", "se"),
    # EP01-03 / EP01
    (r"[Ee][Pp](\d{1,4})(?:-[Ee]?[Pp]?(\d{1,4}))?", "ep"),
    # 第01集 / 第1话 / 第1話
    (r"第(\d{1,4})[集话話]", "cn"),
    # E01 (单独)
    (r"[^a-zA-Z][Ee](\d{1,4})(?:[^a-zA-Z\d]|$)", "e"),
    # - 01 / .01. (空格或点分隔的纯数字，需要上下文判断)
    (r"[\s._-](\d{2,4})[\s._\-\[\]]", "bare"),
]

_RESOLUTION_PATTERN = re.compile(
    r"(4[Kk]|2160[pP]|1080[pPiI]|720[pP]|576[pP]|480[pP])", re.IGNORECASE
)
_CODEC_PATTERN = re.compile(
    r"(HEVC|H\.?265|x265|H\.?264|x264|AVC|AV1|VP9)", re.IGNORECASE
)
_HDR_PATTERN = re.compile(r"(HDR10\+?|Dolby\s*Vision|DV|HLG)", re.IGNORECASE)


def parse_episode(filename: str, filepath: str = "", file_size: int = 0) -> ParsedEpisode:
    """从文件名解析集号和元数据。

    Args:
        filename: 文件名（不含路径）
        filepath: 完整路径
        file_size: 文件大小（字节）

    Returns:
        ParsedEpisode 实例
    """
    result = ParsedEpisode(
        filename=filename,
        filepath=filepath,
        file_size_bytes=file_size,
    )

    # 提取扩展名
    dot_idx = filename.rfind(".")
    if dot_idx > 0:
        result.file_ext = filename[dot_idx:].lower()

    # 特殊集与衍生花絮检测
    if re.search(r"\b(SP\d*|OVA\d*|OAD\d*|番外|特别篇|特典)\b", filename, re.IGNORECASE):
        result.is_special = True

    if re.search(
        r"(?:^|[\s._\-\(\[])(?:Diary|Extra|Preview|Behind[._\s-]the[._\s-]Scenes|Making[._\s-]of|Cut|Clip|Tape)(?:$|[\s._\-\)\]\d])|"
        r"(?:日记|花活儿|花絮|彩蛋|纯享(?:版)?|会员版|采访|访谈|先行片|先导(?:片)?|预告(?:片)?|母带(?:版)?|切片|看点(?:版)?|片段|加更(?:版|期)?|特辑|收官(?:特辑)?)",
        filename,
        re.IGNORECASE
    ):
        result.is_extra = True

    # 集号提取
    for pattern, ptype in _EPISODE_PATTERNS:
        m = re.search(pattern, filename)
        if m:
            if ptype == "se":
                result.season_number = int(m.group(1))
                ep_start = int(m.group(2))
                ep_end = int(m.group(3)) if m.group(3) else ep_start
                result.episode_numbers = list(range(ep_start, ep_end + 1))
            elif ptype == "ep":
                ep_start = int(m.group(1))
                ep_end = int(m.group(2)) if m.group(2) else ep_start
                result.episode_numbers = list(range(ep_start, ep_end + 1))
            elif ptype in ("cn", "e"):
                result.episode_numbers = [int(m.group(1))]
            elif ptype == "bare":
                ep_num = int(m.group(1))
                # 避免把年份/分辨率误判为集号
                if ep_num < 1000 and ep_num > 0:
                    result.episode_numbers = [ep_num]
            break  # 使用第一个匹配的模式

    # 分辨率
    res_m = _RESOLUTION_PATTERN.search(filename)
    if res_m:
        result.resolution = res_m.group(1).upper().replace("P", "p").replace("K", "K")

    # 编码格式
    codec_m = _CODEC_PATTERN.search(filename)
    if codec_m:
        result.codec = codec_m.group(1).upper()

    # HDR
    if _HDR_PATTERN.search(filename):
        result.is_hdr = True

    return result


def is_video_file(filename: str) -> bool:
    """判断是否为视频文件。"""
    dot_idx = filename.rfind(".")
    if dot_idx <= 0:
        return False
    return filename[dot_idx:].lower() in VIDEO_EXTENSIONS


def is_subtitle_file(filename: str) -> bool:
    """判断是否为字幕文件。"""
    dot_idx = filename.rfind(".")
    if dot_idx <= 0:
        return False
    return filename[dot_idx:].lower() in SUBTITLE_EXTENSIONS
