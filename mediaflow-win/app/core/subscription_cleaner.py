"""MoviePilot 订阅清理决策引擎 — 基于媒体类型（电影 vs 电视剧）分离的差异化退订与安全保护算法。"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
import re
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SubscriptionDiagnosis:
    """单个订阅的诊断结果。"""
    sub_id: int
    name: str
    year: str
    media_type: str  # "tv" | "movie"
    season: Optional[int]
    total_episodes: Optional[int]
    completed_episodes: Optional[int]
    lack_episodes: Optional[int]
    state: str  # "R" | "P" | "S"
    username: str
    
    # 算法评估
    clean_eligible: bool              # 是否符合清理条件
    clean_confidence: str             # "high" (安全可退订) | "medium" (建议退订/复核) | "low" (保护不可退订)
    clean_rule: str                  # 规则标识：completed / stalled / zombie / upcoming_protect / short_tv / protected
    clean_rule_name: str             # 规则名称，如 "全季已齐完结"、"2026待映预订保护"
    clean_reason: str                # 详细判断依据
    days_inactive: int               # 沉寂天数
    created_days_ago: int            # 创建距今天数
    is_protected: bool               # 是否受白名单或待映期绝对保护
    is_short_tv: bool = False        # 是否为短剧/微短剧特征
    is_douban_sync: bool = False     # 是否为豆瓣榜单自动同步


PROTECTED_KEYWORDS = re.compile(r"(?:洗版|收藏|keep|留存|4k原盘|原盘|diy)", re.IGNORECASE)


def _parse_days(date_str: Optional[str], last_up_str: Optional[str], now: datetime) -> Tuple[int, int]:
    """解析创建天数与沉寂天数。"""
    created_days = 0
    if date_str:
        try:
            dt = datetime.strptime(str(date_str)[:19], "%Y-%m-%d %H:%M:%S")
            created_days = max(0, (now - dt).days)
        except Exception:
            pass

    inactive_days = 0
    if last_up_str:
        try:
            dt = datetime.strptime(str(last_up_str)[:19], "%Y-%m-%d %H:%M:%S")
            inactive_days = max(0, (now - dt).days)
        except Exception:
            pass
    elif created_days > 0:
        inactive_days = created_days

    return created_days, inactive_days


# ─────────────────────────────────────────────────────────────
# 1. 🎬 电影（Movie）专属清理决策算法
# ─────────────────────────────────────────────────────────────
def diagnose_movie_subscription(sub: Dict[str, Any], now: Optional[datetime] = None) -> SubscriptionDiagnosis:
    """对【电影】订阅进行专属事实诊断与清理决策。
    
    电影特性：
    - 单文件交付，无集数连续追更。
    - 具有漫长的院线待映/流媒体窗口期，当年/未来年份（如 2026+）电影无源属正常现象，严禁误删。
    - 存在豆瓣榜单自动化同步产生的推介电影，与手动订阅区分。
    """
    if now is None:
        now = datetime.now()

    sub_id = int(sub.get("id") or 0)
    name = str(sub.get("name") or sub.get("title") or "未知")
    year_str = str(sub.get("year") or "").strip()
    username = str(sub.get("username") or "admin")
    state = str(sub.get("state") or "R").upper()
    best_ver = int(sub.get("best_version") or 0) == 1 or int(sub.get("best_version_full") or 0) == 1
    custom_words = str(sub.get("custom_words") or "")
    
    created_days, inactive_days = _parse_days(sub.get("date"), sub.get("last_update"), now)
    is_douban = (username == "豆瓣榜单")
    
    # 解析年份数值
    movie_year = 0
    if year_str.isdigit():
        movie_year = int(year_str)

    # 门禁 M1：洗版与留存白名单保护
    if best_ver:
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=0, lack_episodes=None, state=state, username=username,
            clean_eligible=False, clean_confidence="low",
            clean_rule="protected", clean_rule_name="电影洗版监控中",
            clean_reason="开启了最佳版本/洗版监控，持续守候更高画质（4K REMUX/原盘），严禁退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
            is_douban_sync=is_douban
        )

    if PROTECTED_KEYWORDS.search(custom_words) or PROTECTED_KEYWORDS.search(name):
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=0, lack_episodes=None, state=state, username=username,
            clean_eligible=False, clean_confidence="low",
            clean_rule="protected", clean_rule_name="收藏白名单保护",
            clean_reason="命中用户收藏/留存关键字，禁止退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
            is_douban_sync=is_douban
        )

    # 门禁 M2：院线待映 / 上映期新片保护（绝对防误删核武器）
    # 当年（2026）或未来新片，甚至上一年底上映的新片，尚在院线或未发行流媒体 WEB-DL/Blu-ray，无做种是必然的！
    if movie_year >= (now.year - 1):
        year_tag = f"{movie_year}年待映/热映" if movie_year >= now.year else f"{movie_year}年新片"
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=0, lack_episodes=None, state=state, username=username,
            clean_eligible=False, clean_confidence="low",
            clean_rule="upcoming_protect", clean_rule_name=f"{year_tag}预订保护",
            clean_reason=f"{year_tag}新片，距公映或流媒体数字版发售尚需周期，未做种属正常等待，自动保护锁定",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
            is_douban_sync=is_douban
        )

    # 门禁 M3：电影已入库完成（High Confidence 安全清理）
    if state == "P":
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=1, lack_episodes=0, state=state, username=username,
            clean_eligible=True, clean_confidence="high",
            clean_rule="completed", clean_rule_name="电影已入库完结",
            clean_reason="电影已下载并入库完毕，处于完成暂停状态，可安全退订释放追更任务",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_douban_sync=is_douban
        )

    # 门禁 M4：往年老片长期无源死种（Medium Confidence 建议退订）
    # 年份早于前年（<= 2024），且添加超过 60 天仍无任何做种进展
    if movie_year > 0 and movie_year < (now.year - 1) and created_days >= 60:
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=0, lack_episodes=1, state=state, username=username,
            clean_eligible=True, clean_confidence="medium",
            clean_rule="zombie", clean_rule_name="往年老片长期无源死种",
            clean_reason=f"{movie_year} 年影片，订阅添加 {created_days} 天；资源待核查",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_douban_sync=is_douban
        )

    # 门禁 M5：豆瓣榜单自动推选超过 90 天未下载（可作为 Medium 清理项）
    if is_douban and created_days >= 90:
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=0, lack_episodes=None, state=state, username=username,
            clean_eligible=True, clean_confidence="medium",
            clean_rule="douban_stale", clean_rule_name="豆瓣推选长期无源",
            clean_reason=f"豆瓣榜单自动同步项，添加已逾 {created_days} 天无下载进展，可选择批量清理",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_douban_sync=True
        )

    # 兜底：常规监控中
    return SubscriptionDiagnosis(
        sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
        total_episodes=1, completed_episodes=0, lack_episodes=None, state=state, username=username,
        clean_eligible=False, clean_confidence="low",
        clean_rule="active", clean_rule_name="电影追更监控中",
        clean_reason="电影处于正常巡检监控期，暂不清理",
        days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
        is_douban_sync=is_douban
    )


# ─────────────────────────────────────────────────────────────
# 2. 📺 电视剧（TV Series）专属清理决策算法
# ─────────────────────────────────────────────────────────────
def diagnose_tv_subscription(sub: Dict[str, Any], now: Optional[datetime] = None) -> SubscriptionDiagnosis:
    """对【电视剧】订阅进行专属事实诊断与清理决策。
    
    电视剧特性：
    - 多集多季连载，存在全剧完结 vs 季播完结。
    - 存在短剧/微短剧（总集数 99、虚标季数）。
    - 活跃连载追更保护（近 30 天有集数更新）。
    - 断更停滞卡死判定（处于暂停且缺集）。
    """
    if now is None:
        now = datetime.now()

    sub_id = int(sub.get("id") or 0)
    name = str(sub.get("name") or sub.get("title") or "未知")
    year_str = str(sub.get("year") or "").strip()
    season = sub.get("season")
    username = str(sub.get("username") or "admin")
    state = str(sub.get("state") or "R").upper()
    
    total_ep = sub.get("total_episode") or sub.get("total_episodes")
    comp_ep = sub.get("completed_episode") or sub.get("current_episode") or 0
    lack_ep = sub.get("lack_episode")
    
    best_ver = int(sub.get("best_version") or 0) == 1 or int(sub.get("best_version_full") or 0) == 1
    custom_words = str(sub.get("custom_words") or "")
    
    created_days, inactive_days = _parse_days(sub.get("date"), sub.get("last_update"), now)
    is_douban = (username == "豆瓣榜单")
    
    # 短剧微短剧特征识别：总集数为 99，或者季数 > 5 且集数为 99/80+
    is_short_drama = (total_ep == 99) or (season is not None and season > 5 and comp_ep == 0 and lack_ep in (99, None))

    # 门禁 T1：洗版与关键字白名单保护
    if best_ver:
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
            clean_eligible=False, clean_confidence="low",
            clean_rule="protected", clean_rule_name="剧集洗版监控中",
            clean_reason="用户开启了洗版/最佳版本监控，持续守候整季最高画质，严禁退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
            is_short_tv=is_short_drama, is_douban_sync=is_douban
        )

    if PROTECTED_KEYWORDS.search(custom_words) or PROTECTED_KEYWORDS.search(name):
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
            clean_eligible=False, clean_confidence="low",
            clean_rule="protected", clean_rule_name="收藏白名单保护",
            clean_reason="命中用户收藏/留存关键字，禁止退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
            is_short_tv=is_short_drama, is_douban_sync=is_douban
        )

    # 门禁 T2：全季集数齐备入库（High Confidence 安全清理）
    if (lack_ep == 0 and comp_ep > 0) or (total_ep and total_ep > 0 and comp_ep >= total_ep):
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
            clean_eligible=True, clean_confidence="high",
            clean_rule="completed", clean_rule_name="全季已齐完结",
            clean_reason=f"整季集数全部已齐 ({comp_ep}/{total_ep or comp_ep} 集)，下载并入库完毕，安全退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_short_tv=is_short_drama, is_douban_sync=is_douban
        )

    # 门禁 T3：短剧/微短剧虚标长期停滞（Medium Confidence）
    if is_short_drama and (state in ("P", "S") or inactive_days >= 30) and comp_ep == 0:
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
            clean_eligible=True, clean_confidence="medium",
            clean_rule="short_tv_stalled", clean_rule_name="短剧虚标长期停滞",
            clean_reason=f"短剧订阅总数待核实，{inactive_days} 天未更新；资源待核查",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_short_tv=True, is_douban_sync=is_douban
        )

    # 门禁 T4：中途断更停滞（Medium Confidence）
    if (state in ("P", "S")) and inactive_days >= 30:
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
            clean_eligible=True, clean_confidence="medium",
            clean_rule="stalled", clean_rule_name="中途断更停滞",
            clean_reason=f"订阅已暂停，{inactive_days} 天未更新；订阅记录 {comp_ep}/{total_ep or '?'}，资源待核查",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_short_tv=is_short_drama, is_douban_sync=is_douban
        )

    # 门禁 T5：首集都拉不到的无源死种剧集（Medium Confidence）
    if created_days >= 60 and comp_ep == 0 and inactive_days >= 45:
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
            clean_eligible=True, clean_confidence="medium",
            clean_rule="zombie", clean_rule_name="剧集长期无源死种",
            clean_reason=f"订阅添加 {created_days} 天，尚无完成记录；资源待核查",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_short_tv=is_short_drama, is_douban_sync=is_douban
        )

    # 门禁 T6：活跃追更连载保护（Low Confidence 严禁退订）
    act_desc = "剧集尚在连载更新中"
    if inactive_days <= 7:
        act_desc += "（近 7 天内有新集活动）"
    elif inactive_days <= 30:
        act_desc += f"（近 {inactive_days} 天内有活动）"

    return SubscriptionDiagnosis(
        sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
        total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
        clean_eligible=False, clean_confidence="low",
        clean_rule="active", clean_rule_name="活跃连载追更中",
        clean_reason=f"{act_desc}，尚有 {lack_ep or (total_ep - comp_ep if total_ep else '?')} 集待更新入库，严禁清理",
        days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
        is_short_tv=is_short_drama, is_douban_sync=is_douban
    )


def diagnose_subscription(sub: Dict[str, Any], now: Optional[datetime] = None) -> SubscriptionDiagnosis:
    """统一调度入口：根据 media_type 自动分流到电影或电视剧策略。"""
    raw_type = str(sub.get("type") or sub.get("mtype") or "")
    if "电影" in raw_type:
        return diagnose_movie_subscription(sub, now=now)
    return diagnose_tv_subscription(sub, now=now)


def evaluate_subscriptions(subscriptions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """批量评估订阅列表，支持电影与电视剧分组统计与全量聚合。"""
    now = datetime.now()
    diagnoses: List[SubscriptionDiagnosis] = []
    
    tv_items: List[SubscriptionDiagnosis] = []
    movie_items: List[SubscriptionDiagnosis] = []

    for s in subscriptions:
        diag = diagnose_subscription(s, now=now)
        diagnoses.append(diag)
        if diag.media_type == "movie":
            movie_items.append(diag)
        else:
            tv_items.append(diag)

    def _calc_stats(items: List[SubscriptionDiagnosis]) -> Dict[str, int]:
        high = sum(1 for x in items if x.clean_confidence == "high")
        med = sum(1 for x in items if x.clean_confidence == "medium")
        low = sum(1 for x in items if x.clean_confidence == "low")
        return {
            "total": len(items),
            "high": high,
            "medium": med,
            "protected": low,
            "clean_eligible": high + med,
        }

    overall_stats = _calc_stats(diagnoses)
    tv_stats = _calc_stats(tv_items)
    movie_stats = _calc_stats(movie_items)
    
    # 额外维度特征统计
    movie_stats["upcoming_protected"] = sum(1 for m in movie_items if m.clean_rule == "upcoming_protect")
    movie_stats["douban_stale"] = sum(1 for m in movie_items if m.clean_rule == "douban_stale")
    tv_stats["short_tv_stalled"] = sum(1 for t in tv_items if t.clean_rule == "short_tv_stalled")

    return {
        "overall": overall_stats,
        "tv": tv_stats,
        "movie": movie_stats,
        "items": diagnoses,
    }
