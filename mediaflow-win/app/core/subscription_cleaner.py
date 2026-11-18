"""MoviePilot 订阅清理决策引擎 — 基于媒体类型（电影 vs 电视剧）分离的差异化退订与安全保护算法。

严格对齐：
- 《MediaFlow剧集订阅智能清理需求与交互方案-v2.5.39.md》
- 《MediaFlow电影订阅智能清理需求与交互方案-v2.5.40.md》
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
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
    state: str  # "R" (运行中) | "P" (待定) | "S" (暂停)
    username: str

    # 算法评估
    clean_eligible: bool  # 是否符合清理条件
    clean_confidence: str  # "high" (安全可退订) | "medium" (建议退订/复核) | "low" (保护不可退订)
    clean_rule: str  # 规则标识：completed / stalled / zombie / upcoming_protect / short_tv_stalled / protected / target_pending / manual_preserve
    clean_rule_name: str  # 规则名称，如 "全季已齐完结"、"电影已入库完成"、"待映/院线窗口期保护"
    clean_reason: str  # 详细判断依据
    days_inactive: int  # 沉寂天数
    created_days_ago: int  # 创建距今天数
    is_protected: bool  # 是否受白名单或待映期绝对保护
    is_short_tv: bool = False  # 是否为短剧/微短剧特征
    is_douban_sync: bool = False  # 是否为豆瓣榜单自动同步
    release_stage: Optional[str] = None  # 电影发行阶段: unannounced | upcoming_theatrical | in_theaters | digital_pending | released
    in_library_status: Optional[str] = None  # 入库状态: in_library | not_in_library | unknown
    version_target_satisfied: Optional[bool] = None  # 目标版本是否满足


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

    核心准则（依据 v2.5.40 方案）：
    - 身份与媒体类型严格隔离，无季集属性（不补 S01）。
    - 状态代码不能代替事实：P 属于待定，不能等同于已完成；R 状态下若已核实入库，也可安全退订。
    - 入库完成核验：同时具备稳定身份、完整电影内容、可用文件或可验证已入库证据，且满足目标版本。
    - 年份与发行阶段区分：未上映、院线中或未到数字/实体发行期的电影绝对锁定保护。
    - 往年老片长期无源：自动来源且长期无资源进入候选死种；手动添加项默认保留收藏意图。
    """
    if now is None:
        now = datetime.now()

    sub_id = int(sub.get("id") or sub.get("sub_id") or 0)
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

    # 外部实物与发行证据注入口
    in_library_evidence = sub.get("in_library")
    not_in_library = sub.get("not_in_library") is True
    release_stage = sub.get("release_stage")  # unannounced | upcoming_theatrical | in_theaters | digital_pending | released
    has_target_version = sub.get("has_target_version")
    digital_date = sub.get("digital_date")
    theatrical_date = sub.get("theatrical_date")

    # 门禁 M1：洗版与留存白名单保护
    if best_ver:
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=0, lack_episodes=None, state=state, username=username,
            clean_eligible=False, clean_confidence="low",
            clean_rule="protected", clean_rule_name="电影洗版监控中",
            clean_reason="开启了最佳版本/洗版监控，持续守候更高画质（4K REMUX/原盘），严禁退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
            is_douban_sync=is_douban, release_stage=release_stage,
            in_library_status="in_library" if in_library_evidence else ("not_in_library" if not_in_library else "unknown"),
        )

    if PROTECTED_KEYWORDS.search(custom_words) or PROTECTED_KEYWORDS.search(name):
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=0, lack_episodes=None, state=state, username=username,
            clean_eligible=False, clean_confidence="low",
            clean_rule="protected", clean_rule_name="收藏白名单保护",
            clean_reason="命中用户收藏/留存关键字，禁止退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
            is_douban_sync=is_douban, release_stage=release_stage,
            in_library_status="in_library" if in_library_evidence else ("not_in_library" if not_in_library else "unknown"),
        )

    # 门禁 M2：实物已核实入库（不论新片还是老片，只要入库且满足目标，均可安全退订，场景 F01, F02, F10）
    # 当明确有入库证据，或者状态为 P 且无明确未入库反证时（兼容历史 MP 完成标记）
    is_in_library = bool(in_library_evidence) or (
        state == "P" and not not_in_library and release_stage != "in_theaters" and release_stage != "upcoming_theatrical"
    )

    if is_in_library and not not_in_library:
        # 核查目标版本是否满足（场景 F08）
        if has_target_version is False or sub.get("version_target_satisfied") is False:
            return SubscriptionDiagnosis(
                sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
                total_episodes=1, completed_episodes=1, lack_episodes=0, state=state, username=username,
                clean_eligible=False, clean_confidence="low",
                clean_rule="target_pending", clean_rule_name="目标版本仍待满足",
                clean_reason="媒体库已有其他版本，但用户指定的目标版本（4K/原盘/音轨）仍待满足，继续保留",
                days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
                is_douban_sync=is_douban, release_stage=release_stage,
                in_library_status="in_library", version_target_satisfied=False,
            )

        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=1, lack_episodes=0, state=state, username=username,
            clean_eligible=True, clean_confidence="high",
            clean_rule="completed", clean_rule_name="电影已入库完成",
            clean_reason="已核实媒体库中完整文件满足收藏目标，建议安全退订释放订阅",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_douban_sync=is_douban, release_stage=release_stage or "released",
            in_library_status="in_library", version_target_satisfied=True,
        )

    # 门禁 M3：待映/院线/家庭发行窗口期绝对保护（场景 F01, F07）
    # 显式处于发行前期阶段，或者当年/未来年份且无数字发行已发布证据
    is_upcoming_stage = release_stage in ("unannounced", "upcoming_theatrical", "in_theaters", "digital_pending")
    is_recent_year = (movie_year >= now.year) and (release_stage != "released")

    if is_upcoming_stage or is_recent_year:
        reason_desc = ""
        if release_stage == "upcoming_theatrical":
            reason_desc = f"影片已定档待上映（定档 {theatrical_date or year_str}），处于院线窗口期"
        elif release_stage == "in_theaters":
            reason_desc = "院线上映已确认，数字发行日期待公布，暂无流媒体资源属正常现象"
        elif release_stage == "digital_pending":
            reason_desc = f"数字版已定档待发售（发售日 {digital_date or '待公布'}），等待家庭发行"
        elif release_stage == "unannounced":
            reason_desc = "影片处于待映前期阶段，公映排期未定，严密锁定保护"
        else:
            year_tag = f"{movie_year}年待映/热映" if movie_year >= now.year else f"{movie_year}年新片"
            reason_desc = f"{year_tag}新片，尚处于院线或家庭发行窗口期，无做种属正常等待，自动保护锁定"

        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=0, lack_episodes=1, state=state, username=username,
            clean_eligible=False, clean_confidence="low",
            clean_rule="upcoming_protect", clean_rule_name="待映/院线窗口期保护",
            clean_reason=reason_desc,
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
            is_douban_sync=is_douban, release_stage=release_stage or "upcoming",
            in_library_status="not_in_library" if not_in_library else "unknown",
        )

    # 门禁 M4：往年老片长期无源死种（Medium 建议退订，场景 F03, F14）
    if movie_year > 0 and movie_year < (now.year - 1) and created_days >= 60:
        # 手动添加的老片，默认保留用户收藏意图（Section 5.2），除非显式允许或来自榜单
        if not is_douban and not sub.get("allow_manual_stalled"):
            return SubscriptionDiagnosis(
                sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
                total_episodes=1, completed_episodes=0, lack_episodes=1, state=state, username=username,
                clean_eligible=False, clean_confidence="low",
                clean_rule="manual_preserve", clean_rule_name="手动收藏意图保留",
                clean_reason=f"用户手动添加的 {movie_year} 年老片，已添加 {created_days} 天，默认保留收藏意图",
                days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
                is_douban_sync=False, release_stage=release_stage or "released",
                in_library_status="not_in_library" if not_in_library else "unknown",
            )

        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=0, lack_episodes=1, state=state, username=username,
            clean_eligible=True, clean_confidence="medium",
            clean_rule="zombie", clean_rule_name="往年老片长期无源死种",
            clean_reason=f"{movie_year} 年老片，订阅添加已逾 {created_days} 天且无入库进展，站点多轮无有效资源",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_douban_sync=is_douban, release_stage=release_stage or "released",
            in_library_status="not_in_library" if not_in_library else "unknown",
        )

    # 门禁 M5：豆瓣榜单自动推选超过 90 天未下载（Medium 清理项）
    if is_douban and created_days >= 90:
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=0, lack_episodes=1, state=state, username=username,
            clean_eligible=True, clean_confidence="medium",
            clean_rule="douban_stale", clean_rule_name="豆瓣推选长期无源",
            clean_reason=f"豆瓣榜单自动同步项，添加已逾 {created_days} 天无下载进展，可选择批量清理",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_douban_sync=True, release_stage=release_stage or "released",
            in_library_status="not_in_library" if not_in_library else "unknown",
        )

    # 兜底：常规监控中
    return SubscriptionDiagnosis(
        sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
        total_episodes=1, completed_episodes=0, lack_episodes=None, state=state, username=username,
        clean_eligible=False, clean_confidence="low",
        clean_rule="active", clean_rule_name="电影追更监控中",
        clean_reason="电影处于正常巡检监控期，暂不执行退订",
        days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
        is_douban_sync=is_douban, release_stage=release_stage or "unknown",
        in_library_status="not_in_library" if not_in_library else "unknown",
    )


# ─────────────────────────────────────────────────────────────
# 2. 📺 电视剧（TV Series）专属清理决策算法
# ─────────────────────────────────────────────────────────────
def diagnose_tv_subscription(sub: Dict[str, Any], now: Optional[datetime] = None) -> SubscriptionDiagnosis:
    """对【电视剧】订阅进行专属事实诊断与清理决策。

    核心准则（依据 v2.5.39 方案）：
    - 识别真实总集数与 99 占位符的区别，不因纯 99 集误判短剧。
    - 状态 P 属于待定，不能直接当作完结；全季完结必须结合集数齐备与季播完结证据。
    - 连载在播剧集（未播完、近 30 天有集数更新）严格锁定保护。
    - 区分“从未收到正片死种”与“中途断更停滞”。
    """
    if now is None:
        now = datetime.now()

    sub_id = int(sub.get("id") or sub.get("sub_id") or 0)
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

    # 连载在播与完结真实证据
    season_finished = sub.get("season_finished")
    is_airing = sub.get("is_airing") is True

    # 短剧微短剧特征识别：季数 > 5 且集数为 99/80+，或总数 99 且缺乏剧集正规信息
    is_short_drama = (
        (season is not None and season > 5 and comp_ep == 0 and lack_ep in (99, None))
        or (total_ep == 99 and sub.get("is_short_drama") is True)
        or (total_ep == 99 and "短剧" in name)
    )

    # 门禁 T1：洗版与关键字白名单保护
    if best_ver:
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
            clean_eligible=False, clean_confidence="low",
            clean_rule="protected", clean_rule_name="剧集洗版监控中",
            clean_reason="用户开启了洗版/最佳版本监控，持续守候整季最高画质，严禁退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
            is_short_tv=is_short_drama, is_douban_sync=is_douban,
        )

    if PROTECTED_KEYWORDS.search(custom_words) or PROTECTED_KEYWORDS.search(name):
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
            clean_eligible=False, clean_confidence="low",
            clean_rule="protected", clean_rule_name="收藏白名单保护",
            clean_reason="命中用户收藏/留存关键字，禁止退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
            is_short_tv=is_short_drama, is_douban_sync=is_douban,
        )

    # 门禁 T2：全季集数齐备入库（High Confidence 安全清理）
    is_episodes_full = (lack_ep == 0 and comp_ep > 0) or (total_ep and total_ep > 0 and comp_ep >= total_ep)
    if is_episodes_full:
        # 如果明确证实该季仍在连载更新中，严禁当作已完结退订（场景 T08）
        if is_airing or season_finished is False:
            return SubscriptionDiagnosis(
                sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
                total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
                clean_eligible=False, clean_confidence="low",
                clean_rule="airing_protect", clean_rule_name="连载在播保护",
                clean_reason=f"当前已播集已齐 ({comp_ep}/{total_ep or comp_ep} 集)，但本季仍在排播连载中，锁定保留",
                days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
                is_short_tv=is_short_drama, is_douban_sync=is_douban,
            )

        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
            clean_eligible=True, clean_confidence="high",
            clean_rule="completed", clean_rule_name="全季已齐完结",
            clean_reason=f"整季集数全部已齐 ({comp_ep}/{total_ep or comp_ep} 集)，下载并入库完毕，安全退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_short_tv=is_short_drama, is_douban_sync=is_douban,
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
            is_short_tv=True, is_douban_sync=is_douban,
        )

    # 门禁 T4：中途断更停滞（Medium Confidence）
    if (state in ("P", "S") or inactive_days >= 60) and comp_ep > 0 and (lack_ep is None or lack_ep > 0):
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
            clean_eligible=True, clean_confidence="medium",
            clean_rule="stalled", clean_rule_name="中途断更停滞",
            clean_reason=f"订阅已停滞，{inactive_days} 天无新入库；已收录 {comp_ep}/{total_ep or '?'}，资源待核查",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_short_tv=is_short_drama, is_douban_sync=is_douban,
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
            is_short_tv=is_short_drama, is_douban_sync=is_douban,
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
        is_short_tv=is_short_drama, is_douban_sync=is_douban,
    )


def diagnose_subscription(sub: Dict[str, Any], now: Optional[datetime] = None) -> SubscriptionDiagnosis:
    """统一调度入口：根据 media_type / mtype / type 自动分流到电影或电视剧策略。"""
    raw_type = str(sub.get("type") or sub.get("mtype") or sub.get("media_type") or "").strip().lower()
    # 严格判断是否为电影
    is_movie = (
        raw_type in ("movie", "电影")
        or "电影" in raw_type
        or "movie" in raw_type
        or (sub.get("season") is None and not sub.get("total_episode") and sub.get("year"))
    )
    if is_movie:
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
