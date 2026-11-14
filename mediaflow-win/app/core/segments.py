"""按期号和上下段保留文件覆盖证据。"""
import re
from collections import defaultdict
from app.core.episode_parser import parse_episode, is_video_file

DERIVATIVE_PATTERN = re.compile(
    r"(?:"
    r"加码|Plus|投递|Deliver|日记|Diary|纯享|Pure|陪看|Watch|"
    r"加更|出发前|Before|EX\d+|AI档案|超前|先导|前瞻|预热|"
    r"会员|VIP|专享|饭局|请吃饭|衍生|彩蛋|幕后|Behind"
    r")",
    re.I
)


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


def diagnose_periods(files, season, mapping, ordinals, total, finished, archive_present, archive_files=None, douban_total=None):
    """分别按期号和 TMDB 原集号核对，两个解释都覆盖不到的期才可报缺。"""
    numbered = set()
    explicit = set()
    versions = defaultdict(set)
    part_files = set()
    samples = []
    derivative_files = []
    derivative_periods = set()
    regular_files = []
    regular_episode_files = defaultdict(list)
    explicit_parts_by_period = defaultdict(set)

    norm_ordinals = {int(k): int(v) for k, v in ordinals.items() if str(k).isdigit() and str(v).isdigit()}
    norm_mapping = {int(k): [int(p) for p in v] for k, v in mapping.items() if str(k).isdigit()}

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

        # 隔离加码/Plus/投递/先导等衍生版本，不污染正片
        if DERIVATIVE_PATTERN.search(item["name"]) or ep.episode_numbers == [0]:
            derivative_files.append(item)
            for num in ep.episode_numbers:
                if num > 0:
                    derivative_periods.add(num)
                    if num in norm_ordinals:
                        derivative_periods.add(norm_ordinals[num])
            continue

        regular_files.append(item)
        for number in ep.episode_numbers:
            if len(samples) < 2 and item["name"] not in samples:
                samples.append(item["name"])
            part = segment_number(item["name"])
            if part is None:
                numbered.add(number)
                # 模板归一化：剥离分辨率、编码、发布组标签，支持跨 1080p/2160p 正片序列
                norm_name = re.sub(
                    r"(?i)[ ._]?(2160p|1080p|720p|4k|h\.?26[45]|hevc|x26[45]|aac[\d.]*|ddp[\d.]*|web-dl|web-rip|mkv|mp4)[ ._]?",
                    " ",
                    item["name"],
                )
                norm_name = re.sub(r"(S\d+E|第\s*)\d+", r"\1#", norm_name, flags=re.I)
                norm_name = re.sub(r"\s+", " ", norm_name).strip()
                versions[norm_name.casefold()].add(number)
                part_files.add((number, 0))
                regular_episode_files[number].append(item["name"])
            else:
                explicit.add(number)
                if 3 in norm_mapping.get(number, []) and re.search(r"第\s*\d+\s*[期集]下", item["name"]):
                    part = 3
                part_files.add((number, part))
                explicit_parts_by_period[number].add(part)
                regular_episode_files[number].append(item["name"])

    if not (numbered or explicit) and not derivative_files:
        return None

    expected = set(range(1, total + 1))
    detail = f"本季共 {total} 期。"
    if not finished:
        return "airing", detail + f"当前分段资料列出 {len(mapping)} 期，尚未确认全季播毕，后续期数留源跟踪。", [], sorted((numbered | explicit) & expected)

    # 1. 整期编号解释覆盖
    direct_covered = {num for num in numbered if num in expected}
    direct_missing = expected - direct_covered

    # 2. 分段流水号解释覆盖
    linear_covered = set()
    for p in expected:
        req_parts = norm_mapping.get(p, [])
        if req_parts and set(req_parts) <= explicit_parts_by_period[p]:
            linear_covered.add(p)
        else:
            req_linear_eps = [k for k, v in norm_ordinals.items() if v == p]
            if req_linear_eps and all(k in numbered for k in req_linear_eps):
                linear_covered.add(p)
    linear_missing = expected - linear_covered

    # 3. 双重解释均确认缺失 -> period_missing
    # 只有所有有效解释与候选衍生文件均无法覆盖的期数，才能判定为确认缺失
    both_missing = sorted((direct_missing & linear_missing) - derivative_periods)
    if both_missing and norm_ordinals:
        missing_str = ", ".join(map(str, both_missing))
        return (
            "period_missing",
            detail + f"按期号和分段集号分别核对，两种编号下都缺第 {missing_str} 期。补缺时请按期号选择资源。",
            both_missing,
            sorted((numbered | explicit) & expected)
        )

    all_parts = {(n, p) for n, parts in norm_mapping.items() for p in parts}
    explicit_complete = set(norm_mapping) == expected and all_parts <= part_files
    linear_complete = (
        set(norm_ordinals.values()) == expected
        and len(norm_ordinals) > total
        and (
            any(numbers == set(norm_ordinals) for numbers in versions.values())
            or set(norm_ordinals) <= numbered
        )
    )

    direct_confirmed = direct_covered == expected and (
        douban_total == total or all(v in ([0], [1]) for v in norm_mapping.values())
    )

    if not archive_present and (explicit_complete or linear_complete or direct_confirmed) and all(v != [1] for v in norm_mapping.values()):
        evidence = "逐期核实了全部上下段" if explicit_complete else ("整期编号齐全" if direct_confirmed else "同一版本的分段编号齐全")
        return "period_ready", detail + evidence + "，可将整季连同其他版本一起归档。", [], sorted(expected)

    if explicit_complete or linear_complete:
        detail += "两库合计已覆盖全季。请比较源端与归档中同号文件的版本。"
    else:
        direct_gaps = sorted(expected - numbered - explicit)
        flat_gaps = sorted(set(norm_ordinals) - numbered)
        if direct_covered == expected:
            detail += f"已识别第 1–{total} 期编号，暂缺能确认整期版的发行资料。文件已保留，系统跟踪中无需操作。"
        elif direct_covered | derivative_periods >= expected:
            detail += f"已识别 {len(direct_covered)} 期正片，另有期号仅见衍生版本文件已隔离留存。正片证据暂缺，系统跟踪中无需操作。"
        else:
            detail += (
                f"按整期编号，尚未看到的期号为 {direct_gaps or '无'}；"
                f"按分段编号，未标段文件中尚缺编号 {flat_gaps[:10] or '无'}。"
                "文件已保留，系统跟踪中无需操作。"
            )
        if derivative_files:
            detail += f" 识别到 {len(derivative_files)} 个衍生版本文件已隔离留存。"

    if samples:
        detail += " 可查看：" + "、".join(samples) + "。"
    return "period_review", detail, [], sorted((numbered | explicit) & expected)

