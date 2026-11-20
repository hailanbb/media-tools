"""按期号和上下段保留文件覆盖证据（v2.5.45 统一内容单元与无视频/附件分类诊断）。"""
import re
from collections import defaultdict
from app.core.episode_parser import (
    detect_segment_part,
    is_video_file,
    parse_episode,
)

DERIVATIVE_PATTERN = re.compile(
    r"(?:"
    r"加码|Plus|投递|Deliver|日记|Diary|Vlog|纯享|Pure|陪看|Watch|"
    r"加更|出发前|Before|EX\d+|AI档案|超前|先导|前瞻|预热|"
    r"会员|VIP|专享|饭局|请吃饭|衍生|彩蛋|幕后|Behind"
    r")",
    re.I
)

ATTACHMENT_EXTENSIONS = {".nfo", ".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".gif", ".tbn", ".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}


def segment_number(name: str) -> int | None:
    part, _ = detect_segment_part(name)
    return part


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


def _is_attachment_file(name: str) -> bool:
    dot_idx = (name or "").rfind(".")
    if dot_idx <= 0:
        return False
    return name[dot_idx:].lower() in ATTACHMENT_EXTENSIONS


def diagnose_periods_structured(
    files,
    season,
    mapping,
    ordinals,
    total,
    finished,
    archive_present,
    archive_files=None,
    douban_total=None,
    whole_period_verified: bool = False,
) -> dict:
    """结构化分段/期号诊断：明确区分读取结果、无视频/仅附件、子目录、未识别视频与覆盖成功。"""
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
    subdirs = []
    unrecognized_videos = []
    strm_files = []
    attachment_files = []

    norm_ordinals = {int(k): int(v) for k, v in (ordinals or {}).items() if str(k).isdigit() and str(v).isdigit()}
    norm_mapping = {int(k): [int(p) for p in v] for k, v in (mapping or {}).items() if str(k).isdigit()}

    for item in (files or []):
        fname = str(item.get("name") or "")
        if item.get("isDirectory"):
            if item.get("verified_auxiliary_only"):
                continue
            subdirs.append(fname)
            continue
        if fname.lower().endswith(".strm"):
            strm_files.append(fname)
            continue
        if not is_video_file(fname):
            if _is_attachment_file(fname):
                attachment_files.append(fname)
            continue
        ep = parse_episode(fname, item.get("path", ""), item.get("size", 0))
        if ep.is_extra or ep.is_special or ep.content_kind != "regular":
            derivative_files.append(item)
            for num in ep.episode_numbers:
                if num > 0:
                    derivative_periods.add(num)
                    if num in norm_ordinals:
                        derivative_periods.add(norm_ordinals[num])
            continue
        if not ep.episode_numbers or ep.season_number not in (None, season) or ep.file_size_bytes <= 0:
            unrecognized_videos.append(fname)
            continue

        # 隔离加码/Plus/投递/先导等衍生版本，不污染正片
        if DERIVATIVE_PATTERN.search(fname) or ep.episode_numbers == [0]:
            derivative_files.append(item)
            for num in ep.episode_numbers:
                if num > 0:
                    derivative_periods.add(num)
                    if num in norm_ordinals:
                        derivative_periods.add(norm_ordinals[num])
            continue

        regular_files.append(item)
        for number in ep.episode_numbers:
            if len(samples) < 2 and fname not in samples:
                samples.append(fname)
            part = segment_number(fname)
            if part is None:
                numbered.add(number)
                # 模板归一化：剥离分辨率、编码、发布组标签，支持跨 1080p/2160p 正片序列
                norm_name = re.sub(
                    r"(?i)[ ._]?(2160p|1080p|720p|4k|h\.?26[45]|hevc|x26[45]|aac[\d.]*|ddp[\d.]*|web-dl|web-rip|mkv|mp4)[ ._]?",
                    " ",
                    fname,
                )
                norm_name = re.sub(r"(S\d+E|第\s*)\d+", r"\1#", norm_name, flags=re.I)
                norm_name = re.sub(r"\s+", " ", norm_name).strip()
                versions[norm_name.casefold()].add(number)
                part_files.add((number, 0))
                regular_episode_files[number].append(fname)
            else:
                m_per = re.search(r"第\s*(\d+)\s*期", fname)
                p_num = int(m_per[1]) if m_per else norm_ordinals.get(number, number)
                explicit.add(p_num)
                if 3 in norm_mapping.get(p_num, []) and re.search(r"第\s*\d+\s*[期集]下", fname):
                    part = 3
                part_files.add((p_num, part))
                explicit_parts_by_period[p_num].add(part)
                regular_episode_files[p_num].append(fname)

    # 明确区分：存在子目录 / 未识别视频 / STRM / 仅附件 / 空目录（G15, G16）
    if subdirs or unrecognized_videos or strm_files:
        reasons = []
        if subdirs:
            reasons.append(f"含未核实子目录 ({'、'.join(subdirs[:3])})")
        if unrecognized_videos:
            reasons.append(f"含未识别正片视频 ({'、'.join(unrecognized_videos[:3])})")
        if strm_files:
            reasons.append(f"含可用性待核实的 STRM 文件 ({'、'.join(strm_files[:3])})")
        return {
            "outcome_kind": "unrecognized_content",
            "result": None,
            "subdirs": subdirs,
            "unrecognized_videos": unrecognized_videos,
            "strm_files": strm_files,
            "attachment_files": attachment_files,
            "derivative_files": derivative_files,
            "detail": "文件内容待识别：" + "；".join(reasons) + "。请定位具体文件核实后再处理。",
        }

    expected = set(range(1, total + 1)) if total and total > 0 else set()
    detail = f"本季共 {total} 期。"

    # 仅见附件或完全无正片视频（非读取失败，G15）
    if not (numbered or explicit) and not derivative_files:
        outcome_kind = "attachments_only" if attachment_files else "empty_dir"
        if not finished:
            msg = (
                f"{detail}源端目前仅见附件文件（共 {len(attachment_files)} 个），本季尚在连载或待播，保留附件在源目录。"
                if attachment_files
                else f"{detail}源端目前暂无正片视频，本季尚在连载或待播。"
            )
            return {
                "outcome_kind": outcome_kind,
                "result": ("airing", msg, [], []),
                "subdirs": [],
                "unrecognized_videos": [],
                "strm_files": [],
                "attachment_files": attachment_files,
                "derivative_files": [],
                "detail": msg,
            }
        if not archive_present and expected:
            all_missing = sorted(expected)
            msg = (
                f"{detail}本季已播毕，源端仅见附件（{len(attachment_files)} 个）且归档/媒体库均无正片，"
                f"按确认的 {total} 期生成全季补缺目标（附件随季保留）。"
                if attachment_files
                else f"{detail}本季已播毕，源端与归档均无正片，按确认的 {total} 期生成全季补缺目标。"
            )
            return {
                "outcome_kind": outcome_kind,
                "result": ("period_missing", msg, all_missing, []),
                "subdirs": [],
                "unrecognized_videos": [],
                "strm_files": [],
                "attachment_files": attachment_files,
                "derivative_files": [],
                "detail": msg,
            }
        return {
            "outcome_kind": outcome_kind,
            "result": None,
            "subdirs": [],
            "unrecognized_videos": [],
            "strm_files": [],
            "attachment_files": attachment_files,
            "derivative_files": [],
            "detail": f"{detail}源端仅见附件（{len(attachment_files)} 个），待核实归档正片覆盖。",
        }

    if not finished:
        partial_period_nums: set[int] = set()
        partial_notes: list[str] = []
        for p_no in sorted(explicit_parts_by_period):
            got_p = explicit_parts_by_period[p_no]
            if 1 in got_p and 2 not in got_p and p_no not in numbered:
                partial_period_nums.add(p_no)
                partial_notes.append(f"第 {p_no} 期上段已收，下段播期待确认")
        complete_recognized = sorted(((numbered | explicit) & expected) - partial_period_nums)
        airing_detail = detail + f"当前分段资料列出 {len(norm_mapping)} 期，本季仍在连载中，源端文件继续保留。"
        if partial_notes:
            airing_detail += " " + "；".join(partial_notes) + "。"
        if derivative_files:
            airing_detail += f" 纯享/先导等 {len(derivative_files)} 个非正片文件已单列，不补足正片下段。"
        return {
            "outcome_kind": "ok",
            "result": ("airing", airing_detail, [], complete_recognized),
            "subdirs": [],
            "unrecognized_videos": [],
            "strm_files": [],
            "attachment_files": attachment_files,
            "derivative_files": derivative_files,
            "detail": airing_detail,
        }

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
    both_missing = sorted((direct_missing & linear_missing) - derivative_periods)
    if both_missing and norm_ordinals:
        missing_str = ", ".join(map(str, both_missing))
        ambig_deriv = sorted((direct_missing & linear_missing) & derivative_periods)
        extra_note = ""
        if ambig_deriv:
            extra_note = f"（另有第 {', '.join(map(str, ambig_deriv))} 期仅见纯享/加更等衍生文件或段数待核对，已暂缓自动补缺）"
        res_tuple = (
            "period_missing",
            detail + f"按期号和分段集号分别核对，两种编号下都缺第 {missing_str} 期。补缺时请按期号选择资源。{extra_note}",
            both_missing,
            sorted((numbered | explicit) & expected)
        )
        return {
            "outcome_kind": "ok",
            "result": res_tuple,
            "subdirs": [],
            "unrecognized_videos": [],
            "strm_files": [],
            "attachment_files": attachment_files,
            "derivative_files": derivative_files,
            "detail": res_tuple[1],
        }

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
        douban_total == total
        or whole_period_verified
        or all(v in ([0], [1]) for v in norm_mapping.values())
    )

    if not archive_present and (explicit_complete or linear_complete or direct_confirmed) and all(v != [1] for v in norm_mapping.values()):
        evidence = "逐期核实了全部上下段" if explicit_complete else ("整期发行版编号齐全" if direct_confirmed else "同一版本的分段编号齐全")
        res_tuple = ("period_ready", detail + evidence + "，可将整季连同其他版本一起归档。", [], sorted(expected))
        return {
            "outcome_kind": "ok",
            "result": res_tuple,
            "subdirs": [],
            "unrecognized_videos": [],
            "strm_files": [],
            "attachment_files": attachment_files,
            "derivative_files": derivative_files,
            "detail": res_tuple[1],
        }

    if explicit_complete or linear_complete:
        detail += "两库合计已覆盖全季。请比较源端与归档中同号文件的版本。"
    else:
        direct_gaps = sorted(expected - numbered - explicit)
        flat_gaps = sorted(set(norm_ordinals) - numbered)
        if direct_covered == expected:
            detail += f"集数口径待核对：已识别第 1–{total} 期编号，暂缺能确认整期版的发行资料（文件已保留，可补充整期/豆瓣条目核对）。"
        elif direct_covered | derivative_periods >= expected:
            detail += f"集数口径待核对：已识别 {len(direct_covered)} 期正片，另有期号仅见衍生版本文件已隔离留存。"
        else:
            detail += (
                f"集数口径待核对：按整期编号尚未看到第 {direct_gaps or '无'} 期；"
                f"按分段编号未标段文件中尚缺编号 {flat_gaps[:10] or '无'}。"
            )
        if derivative_files:
            detail += f" 识别到 {len(derivative_files)} 个衍生版本文件已隔离留存。"

    if samples:
        detail += " 可查看：" + "、".join(samples) + "。"
    res_tuple = ("period_review", detail, [], sorted((numbered | explicit) & expected))
    return {
        "outcome_kind": "ok",
        "result": res_tuple,
        "subdirs": [],
        "unrecognized_videos": [],
        "strm_files": [],
        "attachment_files": attachment_files,
        "derivative_files": derivative_files,
        "detail": res_tuple[1],
    }


def diagnose_periods(
    files,
    season,
    mapping,
    ordinals,
    total,
    finished,
    archive_present,
    archive_files=None,
    douban_total=None,
    whole_period_verified: bool = False,
):
    """分别按期号和 TMDB 原集号核对，两个解释都覆盖不到的期才可报缺。"""
    structured = diagnose_periods_structured(
        files=files,
        season=season,
        mapping=mapping,
        ordinals=ordinals,
        total=total,
        finished=finished,
        archive_present=archive_present,
        archive_files=archive_files,
        douban_total=douban_total,
        whole_period_verified=whole_period_verified,
    )
    return structured.get("result")

