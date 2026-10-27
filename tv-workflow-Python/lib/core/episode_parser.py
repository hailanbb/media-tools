"""
集号解析模块 — 从文件名和报告字段中提取集号

移植自原成熟逻辑，具备中文数字转换、正则容错匹配与字段范围解析能力。
"""

import re
from typing import Any, List, Optional, Set

# 视频文件扩展名正则
MEDIA_REGEX = re.compile(
    r"\.(?:mp4|mkv|avi|mov|m4v|ts|m2ts|wmv|flv|webm|mpg|mpeg|strm|rmvb|vob|iso)$",
    re.IGNORECASE
)

# 刮削文件扩展名正则
SCRAPE_REGEX = re.compile(
    r"\.(?:nfo|jpg|jpeg|png|webp|avif|bmp|gif|tbn)$",
    re.IGNORECASE
)

# 共用刮削文件名前缀
SHARED_SCRAPE = re.compile(
    r"^(?:tvshow|season(?:\d+)?|movie|poster|fanart|folder|banner|logo|clearlogo|clearart|landscape|disc|season(?:\d+)?[-_.](?:poster|fanart|banner|landscape))$",
    re.IGNORECASE
)

# 中文数字映射
CN_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def cn_to_int(cn: str) -> Optional[int]:
    """将简单中文数字字符串转为整数（支持 0 到 99）"""
    if not cn:
        return None
    chars = list(cn)
    if len(chars) == 1:
        return CN_DIGIT.get(chars[0])
    if chars[0] == "十":
        return 10 + CN_DIGIT.get(chars[1], 0)
    if "十" in chars:
        ti = chars.index("十")
        tens = CN_DIGIT.get(chars[ti - 1], 0)
        ones = CN_DIGIT.get(chars[ti + 1], 0) if ti + 1 < len(chars) else 0
        return tens * 10 + ones
    return None


def episode_from_name(filename: str, season: Optional[int] = None) -> Optional[int]:
    """
    从文件名中提取集号。

    按优先级依次匹配：
    1. 动漫特典过滤（NCOP/NCED/PV/CM/Preview/Menu 等直接忽略）
    2. S01E05 格式（季号可选校验）
    3. 第N集/话/話/回/章（阿拉伯数字 + 中文数字，兼容日文話）
    4. EP05 / E05 格式
    5. 中间或尾部 " - 05" 格式（常见于动漫压制组）
    6. 独立方括号集号格式（如 [01] 或 [01v2]）
    7. 裸数字文件名
    """
    if not filename or not MEDIA_REGEX.search(filename):
        return None

    # 去掉扩展名
    name = re.sub(r"\.[^.]+$", "", filename)

    # 动漫专属过滤：忽略 NCOP/NCED/PV/CM/Preview/Menu 等特典与非正片视频
    if re.search(r"(?:^|[\s._\-[\]])(?:NCOP|NCED|PV|CM|Preview|Menu|Creditless|Textless|CleanOP|CleanED)\d*", name, re.IGNORECASE):
        return None

    # ① S01E05 格式
    if season is not None:
        se_pattern = re.compile(rf"(?:^|[^a-z0-9])S0*{season}\s*E(\d{{1,4}})(?!\d)", re.IGNORECASE)
        m = se_pattern.search(name)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    else:
        m = re.search(r"(?:^|[^a-z0-9])S\d{1,4}\s*E(\d{1,4})(?!\d)", name, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass

    # ② 第N集/话/話/回/章（阿拉伯数字，兼容动漫日文“話”）
    m = re.search(r"第\s*(\d{1,4})\s*[集话話回章]", name)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    # ② 第N集/话/話/回/章（中文数字）
    m = re.search(r"第\s*([零〇一二两三四五六七八九十]+)\s*[集话話回章]", name)
    if m:
        n = cn_to_int(m.group(1))
        if n is not None and n > 0:
            return n

    # ③ EP05 / E05 格式
    m = re.search(r"(?:^|[\s._-])(?:EP?)(\d{1,4})(?!\d)", name, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    # ④ 文件名中或尾部 " - 05" 格式（常见于动漫压制组 [Group] Title - 05 [1080p]）
    m = re.search(r"(?:^|[\s._])-\s*(\d{1,4})(?!\d)", name)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    # ⑤ 独立方括号集号格式（常见于动漫，如 [01] 或 [01v2]）
    m = re.search(r"\[(\d{1,4})(?:v\d+)?\]", name)
    if m:
        try:
            val = int(m.group(1))
            if 0 < val < 1900 and val not in (720, 1080, 2160):
                return val
        except ValueError:
            pass

    # ⑥ 纯数字文件名
    stripped = name.strip()
    if re.match(r"^\d{1,4}$", stripped):
        try:
            return int(stripped)
        except ValueError:
            pass

    return None


def parse_episode_field(value: Any, blank_unknown: bool = False) -> Optional[List[int]]:
    """
    解析报告中的集号字段。

    支持格式：
    - 逗号/顿号/分号/空格: "2、4" "1,3,5" "2；4"
    - 连续范围: "2-5" "3～8" "2至5"
    - 带 E 前缀: "E03-E06"
    - 特殊文本: "齐全" "无缺集" -> []，"未确认" "待核实" -> None
    """
    text = str(value or "").strip()
    if not text:
        return None if blank_unknown else []
    if re.match(r"^(无|无缺集|齐全)$", text):
        return []
    if re.search(r"未确认|未知|待核实|无法", text):
        return None

    values: List[int] = []
    parts = [p for p in re.split(r"[、,，;；\s]+", text) if p]
    for part in parts:
        m = re.match(r"^(?:E)?(\d{1,4})(?:[-~～至](?:E)?(\d{1,4}))?$", part, re.IGNORECASE)
        if not m:
            raise ValueError(f"集号格式无法确认：「{part[:25]}」（原文: \"{text}\"）")
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a < 1 or b < a or b > 10000:
            raise ValueError(f"集号范围无效：{a}-{b}（原文: \"{text}\"）")
        values.extend(range(a, b + 1))

    return sorted(set(values))


def range_episodes(n: int) -> List[int]:
    """生成 1..n 的连续整数列表"""
    return list(range(1, n + 1))


def uniq(arr: List[Any]) -> List[Any]:
    """去重并排序"""
    try:
        return sorted(set(arr))
    except Exception:
        return list(dict.fromkeys(arr))


def is_positive(n: Any) -> bool:
    """判断是否为有效集数正整数 (1 ~ 10000)"""
    return isinstance(n, int) and 0 < n <= 10000


# 别名兼容
positive = is_positive
range_eps = range_episodes
