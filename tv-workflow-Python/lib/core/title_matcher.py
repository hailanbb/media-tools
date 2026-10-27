"""
剧集标题匹配与规范化模块

提供繁简转换、NFKC 正规化、标点与空白清除等文本规范化能力，
以及从目录名中提取剧名、年份、TMDB ID、季号等结构化信息。
"""

import re
from typing import Any, Callable, Dict, Optional, Tuple
import unicodedata

# 特殊后缀（匹配后在比较前移除）
SPECIAL_SUFFIXES = re.compile(r"(?:特别篇|总集篇|SP|OVA|OAD|番外篇|剧场版)$", re.IGNORECASE)

# 地区标签正则，例如 "(台)" "(港)" "(日)"
REGION_TAG_REGEX = re.compile(
    r"\s*[（(]\s*(?:台|港|日|韩|美|英|泰|法|德|意|西|俄|印|马|新|越|菲|印尼|巴西)\s*[）)]"
)

# 不可见字符清除正则
INVISIBLE_REGEX = re.compile(
    r"[\u200B-\u200F\u2028-\u202F\uFEFF\u00AD\u034F\u061C\u180E\u2060-\u2069\uFFF0-\uFFF8]"
)

# 标点符号与空白字符清除正则（保留字母、数字与所有语言文字字符）
STRIP_REGEX = re.compile(r"[^\w]", re.UNICODE)


def _init_opencc() -> Optional[Callable[[str], str]]:
    """尝试加载 OpenCC 转换器，如果未安装则返回 None"""
    try:
        import opencc
        converter = opencc.OpenCC("t2s.json")
        return converter.convert
    except Exception:
        pass

    try:
        from opencc import OpenCC
        converter = OpenCC("tw2sp")
        return converter.convert
    except Exception:
        pass

    return None


_opencc_converter = _init_opencc()


def create_normalizer() -> Callable[[str], str]:
    """
    创建文本规范化函数。

    规范化流程：
    1. 繁体中文 → 简体中文（若可用）
    2. Unicode NFKC 正规化
    3. 清除不可见字符
    4. 清除下划线、标点和空白
    5. 统一小写
    """
    def norm(text: str) -> str:
        if not text or not isinstance(text, str):
            return ""

        s = text
        if _opencc_converter is not None:
            try:
                s = _opencc_converter(s)
            except Exception:
                pass

        # NFKC 正规化
        s = unicodedata.normalize("NFKC", s)
        # 清除不可见字符
        s = INVISIBLE_REGEX.sub("", s)
        # 清除下划线与所有非文字数字符号
        s = s.replace("_", "")
        s = STRIP_REGEX.sub("", s)
        # 小写
        return s.lower()

    return norm


def clean_title_text(value: str) -> str:
    """清除标题中的地区标签（如 (台)、(港) 等）"""
    if not value or not isinstance(value, str):
        return ""
    return REGION_TAG_REGEX.sub("", value).strip()


def show_info(directory_name: str) -> Dict[str, Any]:
    """
    从剧集目录名中提取结构化信息。

    支持格式：
    - "龙珠 (2024) [tmdbid=12345]"
    - "进击的巨人 (2013)"
    - "海贼王"
    """
    if not directory_name or not isinstance(directory_name, str):
        return {"title": "", "year": None, "tmdb": None}

    rest = directory_name.strip()

    # 提取 TMDB ID
    tmdb = None
    m_tmdb = re.search(r"\[tmdbid[=:]?\s*(\d+)\]", rest, re.IGNORECASE)
    if m_tmdb:
        try:
            tmdb = int(m_tmdb.group(1))
        except ValueError:
            pass
        rest = rest[:m_tmdb.start()] + rest[m_tmdb.end():]

    # 清除 tvdbid / imdbid
    rest = re.sub(r"\[tvdbid[=:]?\s*\d+\]", "", rest, flags=re.IGNORECASE)
    rest = re.sub(r"\[imdbid[=:]?\s*tt\d+\]", "", rest, flags=re.IGNORECASE)

    # 提取年份 (19xx 或 20xx)
    year = None
    m_year = re.search(r"[（(]\s*((?:19|20)\d{2})\s*[）)]", rest)
    if m_year:
        try:
            year = int(m_year.group(1))
        except ValueError:
            pass
        rest = rest[:m_year.start()] + rest[m_year.end():]

    # 清除残余的方括号/圆括号标签
    rest = re.sub(r"\[[^\]]*\]", "", rest)
    rest = re.sub(r"[（(][^)）]*[)）]", "", rest)

    # 清除尾部分隔符
    rest = re.sub(r"[-–—_.\s]+$", "", rest).strip()

    return {"title": rest, "year": year, "tmdb": tmdb}


def season_number(dir_name: str) -> Optional[int]:
    """
    从季目录名中提取季号。

    支持格式：
    - "Season 3" / "season3" / "Season 03"
    - "S03" / "s3"
    - "第3季" / "第三季"
    - "Specials" / "SP" / "特别篇" -> 0
    """
    if not dir_name or not isinstance(dir_name, str):
        return None

    name = dir_name.strip()

    # 特别篇
    if re.match(r"^(?:Specials?|SP|特别篇)$", name, re.IGNORECASE):
        return 0

    # Season XX
    m = re.match(r"^Season\s*(\d{1,3})$", name, re.IGNORECASE)
    if m:
        return int(m.group(1))

    # SXX
    m = re.match(r"^S(\d{1,3})$", name, re.IGNORECASE)
    if m:
        return int(m.group(1))

    # 第X季（阿拉伯数字）
    m = re.match(r"^第\s*(\d{1,3})\s*季$", name)
    if m:
        return int(m.group(1))

    # 第X季（中文数字）
    m = re.match(r"^第\s*([零〇一二两三四五六七八九十]+)\s*季$", name)
    if m:
        from .episode_parser import cn_to_int
        return cn_to_int(m.group(1))

    return None


def is_duplicate_title(longer: str, shorter: str) -> bool:
    """检测 longer 是否是 shorter 的双重重复，例如 '龙珠龙珠' 与 '龙珠'"""
    if len(longer) != len(shorter) * 2:
        return False
    return longer == shorter + shorter


def same_title(a: str, b: str, norm: Callable[[str], str]) -> bool:
    """
    比较两个标题是否指向同一部剧。
    """
    if not a or not b or not norm:
        return False

    na = norm(clean_title_text(a))
    nb = norm(clean_title_text(b))

    if not na or not nb:
        return False

    if na == nb:
        return True

    # 移除特殊后缀后再比较
    na_clean = SPECIAL_SUFFIXES.sub("", na)
    nb_clean = SPECIAL_SUFFIXES.sub("", nb)
    if na_clean and nb_clean and na_clean == nb_clean:
        return True

    # 双标题检测
    if is_duplicate_title(na, nb) or is_duplicate_title(nb, na):
        return True

    # 子串包含检测
    if len(na) >= 2 and len(nb) >= 2:
        if len(na) > len(nb) and nb in na and len(nb) >= len(na) * 0.5:
            return True
        if len(nb) > len(na) and na in nb and len(na) >= len(nb) * 0.5:
            return True

    return False
