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
    content_kind: str = "regular"  # regular / vlog / extra_ep / pure / pilot / easter_egg / interview / special
    edit_version: str = "standard"  # standard / extended / vip / cut
    segment_part: int | None = None  # None(整期/未标段) / 1(上/Part1) / 2(中/Part2/下) / 3(下/Part3)
    segment_label: str = ""
    resolution: str | None = None  # 4K/2160p/1080p/720p
    codec: str | None = None  # HEVC/x265/x264/AVC
    is_hdr: bool = False
    subtitle_languages: list[str] = field(default_factory=list)
    file_size_bytes: int = 0
    file_ext: str = ""

    @property
    def content_unit_key(self) -> tuple:
        """统一内容等价键：(集号元组, 分段, 内容类别, 剪辑版本)。"""
        return (
            tuple(self.episode_numbers),
            self.segment_part,
            self.content_kind,
            self.edit_version,
        )

    @property
    def content_label(self) -> str:
        """可读内容版本标签。"""
        kind_labels = {
            "regular": "正片",
            "vlog": "Vlog/日记",
            "extra_ep": "加更/加码",
            "pure": "纯享版",
            "pilot": "先导/超前",
            "easter_egg": "彩蛋/花絮",
            "interview": "采访/陪看",
            "special": "特别篇/衍生",
        }
        edit_labels = {
            "standard": "普通版",
            "extended": "加长版",
            "vip": "会员版",
            "cut": "精简/删减版",
        }
        parts = [kind_labels.get(self.content_kind, self.content_kind)]
        if self.edit_version != "standard":
            parts.append(edit_labels.get(self.edit_version, self.edit_version))
        elif self.content_kind == "regular":
            parts.append("普通版")
        if self.segment_label:
            parts.append(self.segment_label)
        elif self.segment_part is not None:
            parts.append(f"第{self.segment_part}段")
        return "·".join(parts)


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

# ── 统一内容类别、剪辑版本与分段识别正则（全模块共用） ─────────
_SEGMENT_PART_PATTERN = re.compile(
    r"(?:Part[ ._-]*(0?[123]|[ABC])(?=[^a-z0-9]|$)|第\s*\d+\s*[期集][ ._-]*([上中下])|[\s._\-\(\[（]([上中下])(?:集|期|半|段)?[\s._\-\)\]）])",
    re.IGNORECASE,
)
_CONTENT_KIND_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("vlog", re.compile(r"(?:^|[\s._\-\(\[（])(?:Vlog|Diary)(?:$|[\s._\-\)\]）\d])|(?:日记|VLOG)", re.IGNORECASE)),
    ("extra_ep", re.compile(r"(?:加更(?:版|期)?|加码(?:版)?|(?<![a-zA-Z])Plus(?![a-zA-Z])|EX\d+|投递|Deliver|饭局|请吃饭)", re.IGNORECASE)),
    ("pure", re.compile(r"(?:纯享(?:版)?|(?<![a-zA-Z])Pure(?![a-zA-Z]))", re.IGNORECASE)),
    ("pilot", re.compile(r"(?:先导(?:片|期)?|先行片|超前(?:企划|点映|预热)?|前瞻|预热|出发前|(?<![a-zA-Z])Before(?![a-zA-Z]))", re.IGNORECASE)),
    ("interview", re.compile(r"(?:采访|访谈|陪看|(?<![a-zA-Z])Watch(?![a-zA-Z]))", re.IGNORECASE)),
    ("easter_egg", re.compile(r"(?:彩蛋|花絮|花活儿|幕后|母带(?:版)?|切片|看点(?:版)?|片段|预告(?:片)?|(?:^|[\s._\-\(\[])(?:Extra|Preview|Behind[._\s-]the[._\s-]Scenes|Making[._\s-]of|Cut|Clip|Tape)(?:$|[\s._\-\)\]\d]))", re.IGNORECASE)),
    ("special", re.compile(r"(?:特辑|收官(?:特辑)?|特别篇|特别企划|番外|特典|衍生|AI档案|\b(?:SP\d*|OVA\d*|OAD\d*)\b)", re.IGNORECASE)),
]
_EDIT_VERSION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("extended", re.compile(r"(?:加长(?:版)?|完整(?:版)?|未删减(?:版)?|导演剪辑(?:版)?|(?<![a-zA-Z])(?:Extended|Uncut|Director'?s?[._\s-]*Cut)(?![a-zA-Z]))", re.IGNORECASE)),
    ("vip", re.compile(r"(?:会员(?:版|专享|加长)?|专享(?:版)?|(?<![a-zA-Z])(?:SVIP|VIP)(?![a-zA-Z]))", re.IGNORECASE)),
    ("cut", re.compile(r"(?:删减(?:版)?|精简(?:版)?|精华(?:版)?|速看(?:版)?)", re.IGNORECASE)),
]


def detect_segment_part(filename: str) -> tuple[int | None, str]:
    """统一提取上下段 / Part 编号与可读标签。"""
    m = re.search(
        r"(?:Part[ ._-]*(0?[123]|[ABC])(?=[^a-z0-9]|$)|第\s*\d+\s*[期集][ ._-]*([上中下]))",
        filename or "",
        re.IGNORECASE,
    )
    if not m:
        # 兼容括号/分隔符包围的独立 上/中/下 标记（如 S01E01.上.mkv / (上)）
        m2 = re.search(r"[\s._\-\(\[（]([上中下])(?:段|半)?(?:[\s._\-\)\]）]|\.[^.]+$)", filename or "")
        if not m2:
            return None, ""
        tok = m2.group(1)
        part_map = {"上": (1, "上段"), "中": (2, "中段"), "下": (2, "下段")}
        return part_map.get(tok, (None, ""))
    raw = next(v for v in m.groups() if v).upper()
    mapping = {
        "A": (1, "PartA"), "B": (2, "PartB"), "C": (3, "PartC"),
        "01": (1, "Part1"), "02": (2, "Part2"), "03": (3, "Part3"),
        "1": (1, "Part1"), "2": (2, "Part2"), "3": (3, "Part3"),
        "上": (1, "上段"), "中": (2, "中段"), "下": (2, "下段"),
    }
    return mapping.get(raw, (None, ""))


def extract_douban_subject_id(text: str | None) -> str | None:
    """从豆瓣 URL 或纯数字字符串中提取 subject ID。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    m = re.search(r"douban\.com/subject/(\d+)", raw, re.IGNORECASE)
    if m:
        return m.group(1)
    if raw.isdigit() and 5 <= len(raw) <= 12:
        return raw
    return None


def parse_episode(filename: str, filepath: str = "", file_size: int = 0) -> ParsedEpisode:
    """从文件名解析集号和统一内容单元元数据。

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

    classification_name = filename
    episode_tag = re.search(r"[Ss]\d{1,2}[Ee]\d+", filename)
    if episode_tag and " - " in filename[:episode_tag.start()]:
        classification_name = filename[episode_tag.start():]
        title_tokens = re.findall(r"\w+", filename[:episode_tag.start()].rstrip(" -._"))
        if title_tokens:
            title_pattern = r"[\W_]*".join(re.escape(t) for t in title_tokens)
            classification_name = re.sub(title_pattern, "", classification_name, flags=re.I)

    # 剥离扩展名避免误匹配
    cls_stem = re.sub(r"\.[^.]+$", "", classification_name)

    # 统一识别内容类别 (content_kind)
    for kind, pat in _CONTENT_KIND_PATTERNS:
        if pat.search(cls_stem):
            result.content_kind = kind
            break

    # 统一识别剪辑版本 (edit_version)
    for ev_name, pat in _EDIT_VERSION_PATTERNS:
        if pat.search(cls_stem):
            result.edit_version = ev_name
            break

    # 兼容原有 is_extra 语义（衍生类内容不计入普通正片，但普通正片的“加长版”保留为正片剪辑版本）
    if result.content_kind in ("vlog", "extra_ep", "pure", "pilot", "interview", "easter_egg"):
        result.is_extra = True
    elif re.search(
        r"(?:^|[\s._\-\(\[])(?:Diary|Extra|Preview|Behind[._\s-]the[._\s-]Scenes|Making[._\s-]of|Cut|Clip|Tape|Vlog)(?:$|[\s._\-\)\]\d])|"
        r"(?:日记|花活儿|花絮|彩蛋|纯享(?:版)?|会员版|采访|访谈|先行片|先导(?:片)?|预告(?:片)?|母带(?:版)?|切片|看点(?:版)?|片段|加更(?:版|期)?|特辑|收官(?:特辑)?)",
        classification_name,
        re.IGNORECASE
    ):
        result.is_extra = True
        if result.content_kind == "regular":
            result.content_kind = "special"

    # 分段识别
    seg_part, seg_label = detect_segment_part(filename)
    result.segment_part = seg_part
    result.segment_label = seg_label

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

    # E00 先导片归类为 pilot
    if result.episode_numbers == [0] and result.content_kind == "regular":
        result.content_kind = "pilot"

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


def classify_content_unit(
    filename: str,
    season_number: int | None = None,
    filepath: str = "",
    file_size: int = 0,
) -> dict:
    """统一内容单元结构（供 review_engine / segments / inspector / directory_merge 共用）。"""
    pe = parse_episode(filename, filepath, file_size)
    eff_season = pe.season_number if pe.season_number is not None else season_number
    return {
        "filename": filename,
        "filepath": filepath,
        "season_number": eff_season,
        "episode_numbers": list(pe.episode_numbers),
        "segment_part": pe.segment_part,
        "segment_label": pe.segment_label,
        "content_kind": pe.content_kind,
        "edit_version": pe.edit_version,
        "content_unit_key": pe.content_unit_key,
        "content_label": pe.content_label,
        "is_regular": pe.content_kind == "regular" and not pe.is_special and not pe.is_extra and bool(pe.episode_numbers) and all(e > 0 for e in pe.episode_numbers),
        "is_special": pe.is_special,
        "is_extra": pe.is_extra,
        "resolution": pe.resolution,
        "codec": pe.codec,
        "is_hdr": pe.is_hdr,
        "file_size_bytes": file_size,
    }


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
