"""Validate legacy auxiliary folders by contents, never by directory name alone."""

from __future__ import annotations

import posixpath
import re

_AUX_EXTENSIONS = {".nfo", ".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".gif", ".tbn", ".txt"}

_IGNORABLE_JUNK_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini"})

# 匹配 Emby/Jellyfin/MP/115 刮削原子写中断产生的临时文件（如 .tvshow.nfo.6TlXQz、poster.cpt54r）及 AppleDouble (._*)
_SCRAPE_STEM_PATTERN = (
    r"(?:tvshow|season\d*|movie|poster|fanart|backdrop|banner|logo|clearlogo|clearart|"
    r"landscape|thumb|folder|disc|season(?:\d+|-specials)-(?:poster|fanart|backdrop|banner|landscape|thumb))"
)
_PROTECTED_MEDIA_OR_SUB_EXTS = frozenset({
    ".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv", ".webm", ".iso", ".strm",
    ".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup",
    ".zip", ".rar", ".7z", ".tar", ".gz",
})
_SCRAPE_TEMP_FILE_RE = re.compile(
    rf"(?i)^(?:"
    rf"\._.+|"
    rf"\.{_SCRAPE_STEM_PATTERN}(?:\.[a-z0-9]{{2,6}})?\.[a-z0-9_-]{{4,24}}|"
    rf"{_SCRAPE_STEM_PATTERN}(?:\.(?:nfo|jpg|jpeg|png|webp|tbn))?\.(?:tmp|temp|bak|(?=[a-z0-9_-]*\d)[a-z0-9_-]{{5,24}})"
    rf")$"
)

# 标准剧根及季级公共刮削元数据文件（可由媒体服务器随时重新生成，同名无需重复堆叠至子目录）
_STANDARD_SCRAPE_ASSET_RE = re.compile(
    r"(?i)^(?:"
    r"tvshow\.nfo|season(?:\d+)?\.nfo|movie\.nfo|"
    r"(?:poster|backdrop|fanart|banner|clearlogo|logo|clearart|landscape|thumb|folder|disc)\.(?:jpg|jpeg|png|webp|avif|bmp|gif|tbn)|"
    r"season(?:\d+|-specials)-(?:poster|backdrop|fanart|banner|landscape|thumb)\.(?:jpg|jpeg|png|webp|avif|bmp|gif|tbn)"
    r")$"
)


def is_ignorable_junk_or_scrape_temp(name: str | None) -> bool:
    """判断是否为操作系统隐藏垃圾或刮削器原子写临时残留文件（如 .DS_Store / Thumbs.db / .tvshow.nfo.6TlXQz / poster.cpt54r）。"""
    s = str(name or "").strip()
    if not s:
        return False
    if s.casefold() in _IGNORABLE_JUNK_NAMES:
        return True
    ext = posixpath.splitext(s)[1].lower()
    if ext in _PROTECTED_MEDIA_OR_SUB_EXTS:
        return False
    return bool(_SCRAPE_TEMP_FILE_RE.match(s))


def is_standard_scrape_asset(name: str | None) -> bool:
    """判断是否为标准剧根或季级公共刮削元数据文件（tvshow.nfo / season.nfo / poster / backdrop / fanart 等）。"""
    s = str(name or "").strip()
    if not s:
        return False
    return bool(_STANDARD_SCRAPE_ASSET_RE.match(s))


def is_disposable_legacy_aux_file(name: str | None) -> bool:
    """判断历史遗留「合并保留」目录内的文件是否属于可安全销毁的纯刮削/临时垃圾文件（严禁包含视频、字幕、STRM 或未知二进制）。"""
    s = str(name or "").strip()
    if not s:
        return False
    if is_ignorable_junk_or_scrape_temp(s):
        return True
    ext = posixpath.splitext(s)[1].lower()
    return ext in _AUX_EXTENSIONS


async def mark_verified_auxiliary_folders(files, client):
    result = [dict(f) for f in files]

    async def verify(path, depth, budget):
        if depth > 3 or budget[0] <= 0:
            return False
        children = await client.list_files(path, force_refresh=True)
        if not isinstance(children, list):
            return False
        for child in children:
            budget[0] -= 1
            child_path = str(child.get("path") or "")
            if budget[0] < 0 or not child_path.startswith(path.rstrip("/") + "/"):
                return False
            if child.get("isDirectory"):
                if not await verify(child_path, depth + 1, budget):
                    return False
            elif not is_disposable_legacy_aux_file(child.get("name", "")):
                return False
        return True

    for item in result:
        item.pop("verified_auxiliary_only", None)
        if item.get("isDirectory") and item.get("name") == "合并保留" and item.get("path"):
            try:
                item["verified_auxiliary_only"] = await verify(item["path"], 0, [500])
            except Exception:
                item["verified_auxiliary_only"] = False
    return result
