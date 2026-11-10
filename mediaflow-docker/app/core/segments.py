"""按期号和上下段保留文件覆盖证据。"""
import re
from collections import defaultdict
from app.core.episode_parser import parse_episode, is_video_file


def segment_number(name: str) -> int | None:
    m = re.search(r"(?:Part[ ._-]*(01|02|03|[ABC])\b|第\s*\d+\s*[期集][ ._-]*([上中下]))", name, re.I)
    if not m:
        return None
    return {"A": 1, "B": 2, "C": 3, "01": 1, "02": 2, "03": 3,
            "上": 1, "中": 2, "下": 2}[next(v for v in m.groups() if v).upper()]


def episode_segments(episodes: list[dict]) -> dict[int, list[int]]:
    groups = defaultdict(list)
    for e in episodes:
        m = re.search(r"第\s*(\d+)\s*期", e.get("name", ""))
        if not m:
            return {}
        groups[int(m[1])].append(e)
    if not groups or set(groups) != set(range(1, max(groups) + 1)):
        return {}
    result = {}
    for number, items in groups.items():
        parts = [3 if len(items) == 3 and re.search(r"第\s*\d+\s*期下", e.get("name", "")) else segment_number(e.get("name", "")) for e in items]
        if len(items) > 1 and (None in parts or len(set(parts)) != len(parts)):
            return {}
        result[number] = parts if len(items) > 1 else [0]
    return result


def segment_report(files: list[dict], season: int, mapping: dict[int, list[int]]) -> str:
    found = defaultdict(set)
    unsegmented = set()
    for f in files:
        if f.get("isDirectory") or not is_video_file(f["name"]):
            continue
        p = parse_episode(f["name"], f.get("path", ""), f.get("size", 0))
        if p.is_extra or p.is_special or p.season_number not in (None, season) or p.file_size_bytes <= 0:
            continue
        for n in p.episode_numbers:
            part = segment_number(f["name"])
            if 3 in mapping.get(n, []) and re.search(r"第\s*\d+\s*[期集]下", f["name"]):
                part = 3
            if part is None:
                unsegmented.add(n)
            found[n].add(part or 0)
    missing = [(n, part) for n, parts in mapping.items() for part in parts if part not in found[n]]
    extras = sorted((set(found) | unsegmented) - set(mapping))
    text = f"本季资料按 {len(mapping)} 期、{sum(map(len, mapping.values()))} 段收录。"
    if missing:
        text += "待核对分段 " + "、".join(f"第{n}期第{p}段" if p else f"第{n}期" for n, p in missing) + "。未标分段的文件需核对是否为整期版。"
    else:
        text += "按文件名已覆盖全部正片分段，无需按条目数补集。"
    if unsegmented:
        text += f"未标分段文件涉及期号 {sorted(unsegmented)}，可核对是否为整期版或其他版本。"
    if extras:
        text += f"另有资料未列的期号 {extras}，需核对内容。"
    return text
