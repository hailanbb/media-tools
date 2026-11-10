"""补缺下载引擎 — 与 MoviePilot 联动实现智能搜种、多维选种与防重下载。

核心特性：
1. 缺集对齐：仅对初检确认的真实缺失集号进行定向补全
2. 选种算法：做种数≥2校验，清晰度梯队（2160p/1080p优选），中文字幕加分，健康码率保护
3. 能效决策：缺集多且有优质整季包时优选整季；缺集少时散集覆盖，避免下载冗余大包
4. 防重保护：自动排重，提交后写入 OperationLog 操作记录
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.moviepilot import MoviePilotClient
from app.config import get_settings
from app.models.operation import OperationLog
from app.models.season import SeasonRecord

log = logging.getLogger("mediaflow.download")


# ── 选种辅助算法 ──────────────────────────────────────────

def parse_active_episodes(ep_str: str, total: int = 0) -> set[int]:
    """解析 MoviePilot 活跃下载中的集数字符串（如 'E01-E06', 'E12', '第1-5集'）."""
    ep_str = str(ep_str or "").strip()
    if not ep_str:
        return set(range(1, total + 1)) if total > 0 else set()
    eps: set[int] = set()
    for m in re.finditer(r"(?:E|EP|第)?(\d+)(?:\s*[-~～至到_\ufffd]\s*(?:E|EP|第)?(\d+))?", ep_str, re.IGNORECASE):
        start = int(m.group(1))
        if m.group(2):
            end = int(m.group(2))
            if start <= end:
                eps.update(range(start, end + 1))
            else:
                eps.update(range(end, start + 1))
        else:
            eps.add(start)
    return eps


def is_season_pack(meta: dict[str, Any], t: dict[str, Any], task_season: int, total: int) -> bool:
    """精准判断是否为整季资源包。"""
    eps = meta.get("episode_list") or []
    if total > 0 and len(eps) == total:
        return True
    total_ep = meta.get("total_episode")
    if total > 0 and total_ep == total and len(eps) == 0:
        return True

    begin_season = meta.get("begin_season")
    season_list = meta.get("season_list") or ([begin_season] if begin_season else [])
    if len(season_list) == 1 and season_list[0] == task_season:
        begin_ep = meta.get("begin_episode")
        end_ep = meta.get("end_episode")
        season_ep = str(meta.get("season_episode") or "").upper().strip()
        is_season_only = bool(re.match(r"^S\d{1,2}$", season_ep))
        if (begin_ep is None or begin_ep == 0) and (end_ep is None or end_ep == 0) and len(eps) == 0:
            title = str(t.get("title") or "")
            sub = str(meta.get("subtitle") or t.get("description") or "")
            combined = f"{title} {sub}"
            if not re.search(r"(?:[Ee][Pp]?\s*\d+|第\s*\d+\s*集|\b\d+\s*集\b)", combined):
                return True
            if is_season_only and not re.search(r"[Ee][Pp]?\s*\d+", title):
                return True
    return False


def get_resolution_score(t: dict[str, Any], meta: dict[str, Any]) -> int:
    """解析清晰度梯队得分。"""
    text = f"{t.get('title', '')} {meta.get('resource_pix', '')} {' '.join(t.get('labels') or [])}".lower()
    if re.search(r"2160p|4k|uhd", text):
        return 900
    if re.search(r"1080p|1080i|fhd", text):
        return 1000
    if re.search(r"720p|hd(?![a-z0-9])", text):
        return -500
    if re.search(r"480p|576p|sd(?![a-z0-9])", text):
        return -1000
    return 0


def calculate_candidate_score(cand: dict[str, Any], hits: list[int], all_eps: list[int]) -> float:
    """计算单个候选资源的综合评分。"""
    t = cand.get("torrent_info") or {}
    meta = cand.get("meta_info") or {}
    title = str(t.get("title") or "")
    hits_count = len(hits)
    redundant_count = max(0, len(all_eps) - hits_count)

    # 1. 命中净增益 (命中+350, 冗余-120)
    net_gain = hits_count * 350 - redundant_count * 120

    # 2. 清晰度梯队
    res_score = get_resolution_score(t, meta)

    # 3. 码率区间合理性
    size_bytes = float(t.get("size") or 0)
    ep_count = len(all_eps) if all_eps else 1
    size_mb = (size_bytes / ep_count) / (1024 * 1024)
    bitrate_score = 200 if 200 <= size_mb <= 3500 else (-600 if size_mb < 80 else 0)

    # 4. 中文标识偏好
    labels_text = json.dumps(t.get("labels") or [], ensure_ascii=False) + title
    has_cn = 1 if re.search(r"中文|中字|简中|繁中|国语|chs|cht|简繁|双语|mikan|baha", labels_text, re.I) else 0
    cn_score = 400 if has_cn else 0

    # 5. 做种健康度
    seeds = int(t.get("seeders") or 0)
    seeds_score = (200 + min(seeds, 50)) if seeds >= 5 else (seeds * 20)

    return net_gain + res_score + bitrate_score + cn_score + seeds_score


def select_best_candidates(
    eligible: list[dict[str, Any]],
    needed: list[int],
    total: int,
    season: int,
) -> list[tuple[dict[str, Any], list[int], bool]]:
    """根据缺失集号选出最优候选组合（可能为整季包，也可能为多候选散集组合）。

    Returns:
        [(candidate, covered_episodes, is_season_pack), ...]
    """
    if not eligible or not needed:
        return []

    season_packs = [c for c in eligible if is_season_pack(c.get("meta_info") or {}, c.get("torrent_info") or {}, season, total)]
    standalone = [c for c in eligible if not is_season_pack(c.get("meta_info") or {}, c.get("torrent_info") or {}, season, total)]

    prefer_standalone = False
    if season_packs and needed and total > 0:
        missing_count = len(needed)
        missing_ratio = missing_count / total
        if missing_count <= 3 and missing_ratio < 0.35:
            # 缺失集极少，检查散集能否完全覆盖
            standalone_covered = set()
            for sc in standalone:
                sc_eps = (sc.get("meta_info") or {}).get("episode_list") or []
                standalone_covered.update(sc_eps)
            if set(needed).issubset(standalone_covered):
                prefer_standalone = True

    if season_packs and not prefer_standalone:
        # 选得分最高的整季包
        best_sp = max(
            season_packs,
            key=lambda c: calculate_candidate_score(c, list(range(1, total + 1)), list(range(1, total + 1)))
        )
        return [(best_sp, list(range(1, total + 1)), True)]

    # 散集贪心覆盖算法
    remaining_needed = set(needed)
    selected: list[tuple[dict[str, Any], list[int], bool]] = []
    max_submissions = 10

    while remaining_needed and len(selected) < max_submissions:
        scored: list[tuple[float, dict[str, Any], list[int]]] = []
        for c in eligible:
            meta = c.get("meta_info") or {}
            eps = meta.get("episode_list") or []
            hits = [ep for ep in eps if ep in remaining_needed]
            if not hits:
                continue
            score = calculate_candidate_score(c, hits, eps)
            scored.append((score, c, hits))

        if not scored:
            break

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_cand, best_hits = scored[0]
        selected.append((best_cand, best_hits, False))
        remaining_needed -= set(best_hits)

    return selected


# ── 核心调度函数 ──────────────────────────────────────────

async def execute_gap_fill_download(
    season_record_ids: list[str],
    db: AsyncSession,
) -> dict[str, Any]:
    """对选中的季度记录执行补缺检索并推送 MoviePilot 下载。"""
    settings = get_settings()
    if not settings.mp_url or not settings.mp_token:
        raise ValueError("MoviePilot 未配置 (MF_MP_URL 或 MF_MP_TOKEN 缺失)")

    stmt = select(SeasonRecord).where(SeasonRecord.id.in_(season_record_ids))
    res = await db.execute(stmt)
    records = list(res.scalars().all())

    if not records:
        return {
            "ok": False,
            "success": False,
            "message": "未找到匹配的剧集季度记录",
            "submitted": [],
            "details": [],
            "errors": ["未找到匹配的剧集季度记录"],
        }

    mp = MoviePilotClient()
    submitted_results: list[dict[str, Any]] = []
    errors: list[str] = []

    try:
        # 【tv-workflow-win 工业级机制：预先拉取 MoviePilot 活跃下载队列】
        active_downloads: list[dict[str, Any]] = []
        try:
            active_downloads = await mp.get_active_downloads()
            if not isinstance(active_downloads, list):
                active_downloads = []
        except Exception as q_err:
            log.warning("获取 MoviePilot 活跃下载队列失败，继续直连检索: %s", q_err)

        for rec in records:
            if rec.status in ("archive_check", "manually_handled"):
                errors.append(f"{rec.show_name}：归档核对记录仅供查看")
                continue
            show_name = rec.show_name
            season_num = rec.season_number
            tmdb_id_str = rec.tmdb_id

            # 如果 tmdb_id 为空，从 show_name 中提取（如 [tmdbid=298770]）
            if not tmdb_id_str:
                m = re.search(r"\[tmdbid[=:](\d+)\]", show_name, re.I)
                if m:
                    tmdb_id_str = m.group(1)

            # 清洗剧集标题用于检索
            clean_name = re.sub(r"\[tmdbid[=:]\d+\]", "", show_name, flags=re.I)
            clean_name = re.sub(r"[\(\[（].*?[\)\]）]", " ", clean_name)
            clean_name = re.sub(r"\s+\d{4}$", "", clean_name).strip()
            if not clean_name:
                clean_name = show_name

            missing_eps = []
            if rec.missing_episodes_json:
                try:
                    missing_eps = json.loads(rec.missing_episodes_json)
                except Exception:
                    pass

            total_eps = rec.total_episodes or (max(missing_eps) if missing_eps else 0)

            # 如果没有缺失集号，且状态不是 download_needed，跳过
            if not missing_eps and rec.status != "download_needed":
                errors.append(f"{clean_name} S{season_num:02d}: 未记录缺失集号")
                continue

            # 【tv-workflow-win 工业级机制：实时下载队列对齐】
            in_progress_eps: set[int] = set()
            for dl in active_downloads:
                dl_name = str(dl.get("title") or dl.get("name") or "")
                # 剧名或 TMDB ID 匹配
                if clean_name.lower() in dl_name.lower() or (tmdb_id_str and tmdb_id_str in dl_name):
                    dl_season = dl.get("season")
                    if dl_season is None:
                        s_match = re.search(r"S(\d{1,2})", dl_name, re.I)
                        dl_season = int(s_match.group(1)) if s_match else None
                    if dl_season is None or dl_season == season_num:
                        ep_field = str(dl.get("episode") or "")
                        in_progress_eps.update(parse_active_episodes(ep_field or dl_name, total_eps))

            actually_needed = [ep for ep in missing_eps if ep not in in_progress_eps]
            if in_progress_eps and not actually_needed:
                log.info("%s S%02d 所缺集数 %s 已在 MoviePilot 活跃下载队列中，跳过重复提交", clean_name, season_num, missing_eps)
                rec.status_detail = f"缺集 {missing_eps} 已在下载队列中，无需重复提交"
                continue
            elif in_progress_eps:
                log.info("%s S%02d 部分集数 %s 在下载队列中，仅补齐剩余缺失集: %s", clean_name, season_num, in_progress_eps, actually_needed)
                missing_eps = actually_needed

            tmdb_id_num = int(tmdb_id_str) if (tmdb_id_str and tmdb_id_str.isdigit()) else 0

            log.info(
                "正在检索补缺资源: %s S%02d (TMDB:%s, 缺失集: %s)",
                clean_name, season_num, tmdb_id_num or "无", missing_eps
            )

            # 调用 MoviePilot 搜索资源
            try:
                raw_results = await mp.search_resources(
                    tmdb_id=tmdb_id_num,
                    season=season_num,
                    title=clean_name,
                )
            except Exception as se:
                errors.append(f"{clean_name} S{season_num:02d}: 检索资源接口失败 - {se}")
                continue

            if not raw_results or not isinstance(raw_results, list):
                errors.append(f"{clean_name} S{season_num:02d}: 未检索到任何资源")
                continue

            # 筛选做种数 >= 2 且季号匹配的合格资源
            eligible: list[dict[str, Any]] = []
            for c in raw_results:
                t = c.get("torrent_info") or {}
                m_info = c.get("media_info") or {}
                meta = c.get("meta_info") or {}
                if not t or not m_info:
                    continue
                try:
                    seeds = int(t.get("seeders") or 0)
                except (ValueError, TypeError):
                    seeds = 0
                if seeds < 2:
                    continue
                seasons = meta.get("season_list") or ([meta.get("begin_season")] if meta.get("begin_season") else [])
                if seasons and seasons[0] != season_num:
                    continue
                eligible.append(c)

            if not eligible:
                errors.append(f"{clean_name} S{season_num:02d}: 检索到 {len(raw_results)} 条，但无做种数≥2且季号匹配的资源")
                continue

            # 选种决策
            selected = select_best_candidates(eligible, missing_eps, total_eps, season_num)
            if not selected:
                errors.append(f"{clean_name} S{season_num:02d}: 候选资源无法覆盖缺失集号")
                continue

            # 提交下载
            for cand, covered_eps, is_sp in selected:
                cand_t = cand.get("torrent_info") or {}
                cand_m = cand.get("media_info") or {}
                title = str(cand_t.get("title") or "")
                site = str(cand_t.get("site_name") or "")
                seeds = int(cand_t.get("seeders") or 0)
                plan_desc = f"整季包覆盖(S{season_num:02d})" if is_sp else f"部分集覆盖(E{','.join(map(str, sorted(covered_eps)))})"

                # 【核心流程：在发起下载前，清理 MoviePilot 媒体整理记录，避免入库被跳过】
                cleared_info: dict[str, Any] = {"cleared_count": 0, "records": []}
                try:
                    target_eps_for_clean = None if is_sp else set(covered_eps)
                    clean_res = await mp.clear_transfer_history_for_episodes(
                        title=clean_name,
                        season=season_num,
                        episodes=target_eps_for_clean,
                    )
                    cleared_info = clean_res
                    if clean_res.get("cleared_count", 0) > 0:
                        log.info(
                            "[%s S%02d] 下载前已联动 MoviePilot 检查媒体整理：共仅删除 %d 条冲突转移记录（未删除任何实体文件，确保新补缺集正常入库）",
                            clean_name, season_num, clean_res["cleared_count"]
                        )
                except Exception as clean_err:
                    log.warning("检查/清理 MoviePilot 转移历史记录异常（不阻断下载）: %s", clean_err)

                log.info("向 MoviePilot 提交下载 [%s]: [%s] %s (做种: %d)", plan_desc, site, title, seeds)
                try:
                    reply = await mp.submit_download(cand_m, cand_t)
                    is_ok = bool(reply.get("success", False)) if isinstance(reply, dict) else True
                    msg = reply.get("message", "已提交") if isinstance(reply, dict) else "已提交"

                    submitted_results.append({
                        "show_name": clean_name,
                        "season": season_num,
                        "site": site,
                        "title": title,
                        "seeds": seeds,
                        "covered_episodes": covered_eps,
                        "is_season_pack": is_sp,
                        "plan_desc": plan_desc,
                        "cleared_transfer_records_count": cleared_info.get("cleared_count", 0),
                        "cleared_transfer_records": cleared_info.get("records", []),
                        "status": "success" if is_ok else "failed",
                        "message": msg,
                    })

                    # 记录操作日志
                    op_log = OperationLog(
                        task_id=rec.task_id,
                        season_record_id=rec.id,
                        action="download_gap_fill",
                        status="completed" if is_ok else "failed",
                        detail_json=json.dumps({
                            "show": clean_name,
                            "season": season_num,
                            "title": title,
                            "site": site,
                            "seeds": seeds,
                            "covered": covered_eps,
                            "is_season_pack": is_sp,
                            "plan_desc": plan_desc,
                            "cleared_transfer_records_count": cleared_info.get("cleared_count", 0),
                            "cleared_transfer_records": cleared_info.get("records", []),
                            "reply": reply,
                        }, ensure_ascii=False),
                    )
                    db.add(op_log)

                    if is_ok:
                        clean_tip = f" (已前置清理 {cleared_info.get('cleared_count', 0)} 条历史整理记录)" if cleared_info.get("cleared_count", 0) > 0 else ""
                        rec.status_detail = f"已推送 MP 下载: [{site}] {title[:50]} ({plan_desc}{clean_tip})"

                except Exception as de:
                    errors.append(f"提交下载失败 [{title}]: {de}")

        await db.commit()

    finally:
        await mp.close()

    success_count = sum(1 for s in submitted_results if s["status"] == "success")
    total_cleared = sum(s.get("cleared_transfer_records_count", 0) for s in submitted_results)
    summary_msg = f"补缺推送完成：共提交 {success_count} 个下载任务"
    if total_cleared > 0:
        summary_msg += f"（已自动仅清除 {total_cleared} 条 MP 冲突转移记录，确保自动入库）"
    if errors:
        summary_msg += f"，遇到 {len(errors)} 项提醒: {'; '.join(errors[:2])}"

    # F03 契约增强：同时提供 details 与 submitted，双向兼容前端读取
    details_list = [
        {
            "success": s["status"] == "success",
            "seeders": s.get("seeds", 0),
            "reason": s.get("plan_desc") or s.get("message", ""),
            "show_name": s.get("show_name", ""),
            "title": s.get("title", ""),
            "site": s.get("site", ""),
            "message": s.get("message", ""),
        }
        for s in submitted_results
    ]

    return {
        "ok": success_count > 0,
        "message": summary_msg,
        "submitted": submitted_results,
        "details": details_list,
        "total_cleared_transfer_records": total_cleared,
        "errors": errors,
    }
