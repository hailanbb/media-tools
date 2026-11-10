"""按期号和上下段保留文件覆盖证据。"""
import re
from collections import defaultdict
from app.core.episode_parser import parse_episode, is_video_file


def segment_number(name: str) -> int | None:
    m = re.search(r"(?:Part[ ._-]*(0?[123]|[ABC])(?=[^a-z0-9]|$)|第\s*\d+\s*[期集][ ._-]*([上中下]))", name, re.I)
    if not m:
        return None
    return {"A": 1, "B": 2, "C": 3, "01": 1, "02": 2, "03": 3, "1": 1, "2": 2, "3": 3,
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
        result[number] = [p or 0 for p in parts]
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
        if unsegmented:
            text += "本地还有未标上下段的文件，可能采用整期编号或资料单集编号，暂无法从文件名确认分段覆盖。"
        else:
            text += "待核对分段 " + "、".join(f"第{n}期第{p}段" if p else f"第{n}期" for n, p in missing[:6]) + (f"等 {len(missing)} 段" if len(missing) > 6 else "") + "。"
    else:
        text += "按文件名已覆盖全部正片分段，无需按条目数补集。"
    if unsegmented:
        text += f"未标分段文件涉及 {len(unsegmented)} 个期号，可查看是否为整期版或按资料单集编号。"
    if extras:
        text += f"另有资料未列的期号 {extras}，需核对内容。"
    return text


def diagnose_periods(files, season, mapping, ordinals, total, finished, archive_present):
    """分别按期号和 TMDB 原集号核对，两个解释都覆盖不到的期才可报缺。"""
    numbered = set()
    explicit = set()
    versions = defaultdict(set)
    part_files = set()
    samples = []
    for item in files:
        if item.get("isDirectory"):
            return None
        if not is_video_file(item["name"]):
            continue
        ep = parse_episode(item["name"], item.get("path", ""), item.get("size", 0))
        if ep.is_extra or ep.is_special:
            continue
        if not ep.episode_numbers or ep.season_number not in (None, season) or ep.file_size_bytes <= 0:
            return None
        for number in ep.episode_numbers:
            if len(samples) < 2 and item["name"] not in samples:
                samples.append(item["name"])
            if segment_number(item["name"]) is None:
                numbered.add(number)
                template = re.sub(r"(S\d+E|第\s*)\d+", r"\1#", item["name"], flags=re.I)
                versions[template.casefold()].add(number)
                part_files.add((number, 0))
            else:
                explicit.add(number)
                part = segment_number(item["name"])
                if 3 in mapping.get(number, []) and re.search(r"第\s*\d+\s*[期集]下", item["name"]):
                    part = 3
                part_files.add((number, part))
    if not (numbered or explicit):
        return None
    expected = set(range(1, total + 1))
    possible = (numbered | explicit | {ordinals[n] for n in numbered if n in ordinals}) & expected
    gaps = sorted(expected - possible)
    detail = f"本季共 {total} 期。"
    if not finished:
        return "airing", detail + f"当前分段资料列出 {len(mapping)} 期，尚未确认全季播毕，后续期数留源跟踪。", [], sorted((numbered | explicit) & expected)
    if gaps and ordinals:
        return "period_missing", detail + f"按期号和分段集号分别核对，两种编号下都缺第 {', '.join(map(str, gaps))} 期。补缺时请按期号选择资源。", gaps, sorted((numbered | explicit) & expected)
    all_parts = {(n, p) for n, parts in mapping.items() for p in parts}
    explicit_complete = set(mapping) == expected and all_parts <= part_files
    linear_complete = (set(ordinals.values()) == expected and len(ordinals) > total
                       and any(numbers == set(ordinals) for numbers in versions.values()))
    if not archive_present and (explicit_complete or linear_complete) and all(v != [1] for v in mapping.values()):
        evidence = "逐期核实了全部上下段" if explicit_complete else "同一版本的分段编号齐全"
        return "period_ready", detail + evidence + "，可将整季连同其他版本一起归档。", [], sorted(expected)
    if explicit_complete or linear_complete:
        detail += "两库合计已覆盖全季。请比较源端与归档中同号文件的版本。"
    else:
        direct_gaps = sorted(expected - numbered - explicit)
        flat_gaps = sorted(set(ordinals) - numbered)
        detail += (f"按整期编号，尚未看到的期号为 {direct_gaps or '无'}；"
                   f"按分段编号，未标段文件中尚缺编号 {flat_gaps[:10] or '无'}。"
                   "请打开同一版本的样片，确认一集包含整期还是其中一段。")
    if samples:
        detail += " 可查看：" + "、".join(samples) + "。"
    return "period_review", detail, [], sorted((numbered | explicit) & expected)
