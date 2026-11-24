"""豆瓣主备通道网关：支持 MoviePilot 主通道熔断切换备选（Cloudflare 反代 / Cookie 直连）与恢复自动回切。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import get_settings
from app.core.douban_cookie import mask_cookie_summary, parse_douban_cookie
from app.core.douban_lookup import (
    lookup_error_reason,
    normalize_detail,
    search_items,
    search_page_candidates,
    subject_page_detail,
)

log = logging.getLogger("mediaflow.douban_gateway")

_DEFAULT_COOLDOWN_SECONDS = 300
_CF_PROXY_MIN_INTERVAL = 1.1
_CF_PROXY_BACKOFF_SECONDS = 20.0
_DIRECT_COOKIE_MIN_INTERVAL = 1.5
_CACHE_TTL_SECONDS = 600.0
_CF_PROXY_LOCK = asyncio.Lock()
_LAST_CF_REQUEST_AT = 0.0
_DIRECT_COOKIE_LOCK = asyncio.Lock()
_LAST_DIRECT_REQUEST_AT = 0.0

# 进程内轻量 TTL 缓存：同轮初检内避免对同一剧名或同一 subject_id 重复触发豆瓣风控
_SEARCH_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_DETAIL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_SUGGEST_META_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

# 进程内熔断状态（单测可通过 reset_state_for_tests() 复位）
_STATE: dict[str, Any] = {
    "mp_cooldown_until": 0.0,
    "mp_last_error": "",
    "mp_last_checked_at": 0.0,
    "last_active_channel": "moviepilot",  # moviepilot | cf_proxy | direct_cookie | none
    "last_fallback_error": "",
    "cf_backoff_until": 0.0,
    "cf_backoff_reason": "",
}


def reset_state_for_tests() -> None:
    """重置进程内熔断状态、缓存与节流时间戳（专供单元测试隔离使用）。"""
    global _LAST_DIRECT_REQUEST_AT, _LAST_CF_REQUEST_AT
    _STATE.update(
        {
            "mp_cooldown_until": 0.0,
            "mp_last_error": "",
            "mp_last_checked_at": 0.0,
            "last_active_channel": "moviepilot",
            "last_fallback_error": "",
            "cf_backoff_until": 0.0,
            "cf_backoff_reason": "",
        }
    )
    _LAST_DIRECT_REQUEST_AT = 0.0
    _LAST_CF_REQUEST_AT = 0.0
    _SEARCH_CACHE.clear()
    _DETAIL_CACHE.clear()
    _SUGGEST_META_CACHE.clear()


def _config_file_path() -> Path:
    return Path(get_settings().data_dir) / "douban_gateway.json"


def _normalize_proxy_base(raw: str) -> str:
    val = str(raw or "").strip().rstrip("/")
    if not val:
        return ""
    parsed = urlparse(val)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("反代地址必须以 http:// 或 https:// 开头")
    return val


def _load_file_config() -> dict[str, Any]:
    cfg_path = _config_file_path()
    if not cfg_path.exists():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("读取豆瓣网关配置失败 (%s): %s", cfg_path, exc)
        return {}


def _write_file_config(data: dict[str, Any]) -> None:
    cfg_path = _config_file_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cfg_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, cfg_path)


def _effective_config(draft: dict[str, Any] | None = None) -> dict[str, Any]:
    """合并顺序：环境默认值 (.env) < 运行时文件 (douban_gateway.json) < 临时草稿 (draft)。"""
    settings = get_settings()
    file_cfg = _load_file_config()

    proxy_base = str(
        file_cfg["proxy_base"]
        if "proxy_base" in file_cfg
        else (getattr(settings, "douban_proxy_base", "") or "")
    ).strip().rstrip("/")

    proxy_token = str(
        file_cfg["proxy_token"]
        if "proxy_token" in file_cfg
        else (getattr(settings, "douban_proxy_token", "") or "")
    ).strip()

    cookie_header = str(
        file_cfg["cookie_header"]
        if "cookie_header" in file_cfg
        else (getattr(settings, "douban_cookie", "") or "")
    ).strip()
    cookie_format = str(file_cfg.get("cookie_format") or "")
    cookie_updated_at = str(file_cfg.get("cookie_updated_at") or "")

    cooldown = int(
        file_cfg.get("failover_cooldown_seconds")
        or getattr(settings, "douban_failover_cooldown_seconds", _DEFAULT_COOLDOWN_SECONDS)
        or _DEFAULT_COOLDOWN_SECONDS
    )

    if isinstance(draft, dict):
        if "proxy_base" in draft and draft["proxy_base"] is not None:
            proxy_base = _normalize_proxy_base(str(draft["proxy_base"]))
        if draft.get("clear_proxy_token"):
            proxy_token = ""
        elif str(draft.get("proxy_token") or "").strip():
            proxy_token = str(draft["proxy_token"]).strip()
        if draft.get("clear_cookie"):
            cookie_header = ""
            cookie_format = ""
        elif str(draft.get("raw_cookie") or "").strip():
            parsed = parse_douban_cookie(str(draft["raw_cookie"]))
            if parsed["header"]:
                cookie_header = parsed["header"]
                cookie_format = parsed["format_detected"]
        if draft.get("failover_cooldown_seconds") is not None:
            cooldown = max(30, min(3600, int(draft["failover_cooldown_seconds"])))

    return {
        "proxy_base": proxy_base,
        "proxy_token": proxy_token,
        "cookie_header": cookie_header,
        "cookie_format": cookie_format,
        "cookie_updated_at": cookie_updated_at,
        "failover_cooldown_seconds": max(30, min(3600, cooldown)),
    }


def has_fallback_configured(draft: dict[str, Any] | None = None) -> bool:
    """检查是否至少配置了一个可用的豆瓣备选通道（CF 反代或个人 Cookie）。"""
    cfg = _effective_config(draft)
    return bool(cfg["proxy_base"] or cfg["cookie_header"])


def should_skip_mp() -> bool:
    """仅当已配置备选通道且 MP 仍在熔断冷却期内时跳过 MP，避免无效等待；否则始终尝试 MP。"""
    if not has_fallback_configured():
        return False
    return time.time() < float(_STATE.get("mp_cooldown_until") or 0.0)


def record_mp_success() -> None:
    """MP 恢复正常响应时立即解除熔断并自动切回 MP 主通道。"""
    was_in_cooldown = float(_STATE.get("mp_cooldown_until") or 0.0) > time.time()
    _STATE["mp_cooldown_until"] = 0.0
    _STATE["mp_last_error"] = ""
    _STATE["mp_last_checked_at"] = time.time()
    _STATE["last_active_channel"] = "moviepilot"
    if was_in_cooldown:
        log.info("MoviePilot 豆瓣主通道已恢复正常，已自动切回主通道")


def record_mp_failure(reason: str = "") -> None:
    """MP 豆瓣主通道异常或空返回时触发冷却，期间自动由备选通道承接。"""
    cfg = _effective_config()
    now = time.time()
    _STATE["mp_last_checked_at"] = now
    _STATE["mp_last_error"] = str(reason or "MoviePilot 豆瓣搜索未返回结果")[:160]
    if cfg["proxy_base"] or cfg["cookie_header"]:
        _STATE["mp_cooldown_until"] = now + cfg["failover_cooldown_seconds"]


def get_runtime_status() -> dict[str, Any]:
    """返回当前主备切换状态（脱敏，可直接对前端输出）。"""
    now = time.time()
    cooldown_until = float(_STATE.get("mp_cooldown_until") or 0.0)
    retry_in = max(0, int(cooldown_until - now)) if cooldown_until > now else 0
    return {
        "active_channel": str(_STATE.get("last_active_channel") or "moviepilot"),
        "mp_state": "cooldown" if retry_in > 0 else ("error" if _STATE.get("mp_last_error") else "ok"),
        "mp_retry_in_seconds": retry_in,
        "mp_last_error": str(_STATE.get("mp_last_error") or ""),
        "last_fallback_error": str(_STATE.get("last_fallback_error") or ""),
        "has_fallback": has_fallback_configured(),
    }


def get_public_config() -> dict[str, Any]:
    """返回脱敏后的豆瓣网关配置与状态，永不向调用方回传明文 Cookie 或 Proxy Token。"""
    cfg = _effective_config()
    cookie_summary = mask_cookie_summary(
        cfg["cookie_header"],
        format_detected=cfg["cookie_format"],
        updated_at=cfg["cookie_updated_at"],
    )
    return {
        "proxy_base": cfg["proxy_base"],
        "has_proxy_token": bool(cfg["proxy_token"]),
        "cookie_summary": cookie_summary,
        "failover_cooldown_seconds": cfg["failover_cooldown_seconds"],
        "status": get_runtime_status(),
    }


def save_runtime_config(payload: dict[str, Any]) -> dict[str, Any]:
    """校验、解析并持久化豆瓣反代与 Cookie-Editor 配置。"""
    current = _load_file_config()
    eff = _effective_config()

    proxy_base = _normalize_proxy_base(
        payload["proxy_base"] if "proxy_base" in payload and payload["proxy_base"] is not None else eff["proxy_base"]
    )
    if payload.get("clear_proxy_token"):
        proxy_token = ""
    elif str(payload.get("proxy_token") or "").strip():
        proxy_token = str(payload["proxy_token"]).strip()
    else:
        proxy_token = str(current.get("proxy_token") if "proxy_token" in current else eff["proxy_token"])

    parse_result: dict[str, Any] | None = None
    cookie_header = str(current.get("cookie_header") if "cookie_header" in current else eff["cookie_header"])
    cookie_format = str(current.get("cookie_format") if "cookie_format" in current else eff["cookie_format"])
    cookie_updated_at = str(current.get("cookie_updated_at") or "")

    if payload.get("clear_cookie"):
        cookie_header = ""
        cookie_format = ""
        cookie_updated_at = ""
    elif "raw_cookie" in payload and str(payload.get("raw_cookie") or "").strip():
        parse_result = parse_douban_cookie(str(payload["raw_cookie"]))
        if not parse_result["header"]:
            raise ValueError(
                "; ".join(parse_result["warnings"]) or "未能解析有效的豆瓣 Cookie，请检查粘贴内容"
            )
        cookie_header = parse_result["header"]
        cookie_format = parse_result["format_detected"]
        cookie_updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    cooldown = eff["failover_cooldown_seconds"]
    if payload.get("failover_cooldown_seconds") is not None:
        cooldown = max(30, min(3600, int(payload["failover_cooldown_seconds"])))

    to_save = {
        "proxy_base": proxy_base,
        "proxy_token": proxy_token,
        "cookie_header": cookie_header,
        "cookie_format": cookie_format,
        "cookie_updated_at": cookie_updated_at,
        "failover_cooldown_seconds": cooldown,
    }
    _write_file_config(to_save)
    # 配置变更后清除上次备选报错、退避窗口与缓存，允许立即可测
    _STATE["last_fallback_error"] = ""
    _STATE["cf_backoff_until"] = 0.0
    _STATE["cf_backoff_reason"] = ""
    _SEARCH_CACHE.clear()
    _DETAIL_CACHE.clear()
    _SUGGEST_META_CACHE.clear()

    public_parse_result = (
        {
            "format_detected": parse_result["format_detected"],
            "has_dbcl2": parse_result["has_dbcl2"],
            "uid_masked": parse_result["uid_masked"],
            "has_bid": parse_result["has_bid"],
            "bid_masked": parse_result["bid_masked"],
            "cookie_count": parse_result["cookie_count"],
            "auto_generated_bid": parse_result["auto_generated_bid"],
            "warnings": parse_result["warnings"],
        }
        if parse_result is not None
        else None
    )

    return {
        "ok": True,
        "config": get_public_config(),
        "parse_result": public_parse_result,
    }


def _build_cf_headers(cfg: dict[str, Any], *, include_cookie: bool = True) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json, text/plain, */*"}
    if cfg["proxy_token"]:
        headers["X-Proxy-Token"] = cfg["proxy_token"]
    if include_cookie and cfg["cookie_header"]:
        headers["X-Douban-Cookie"] = cfg["cookie_header"]
    return headers


def _build_direct_headers(kind: str, cookie_header: str, subject_id: str = "", query: str = "") -> dict[str, str]:
    if kind in ("rexxar", "rexxar_search"):
        from urllib.parse import quote

        referer = (
            f"https://m.douban.com/search/?query={quote(query)}&type=movie"
            if kind == "rexxar_search"
            else (f"https://m.douban.com/movie/subject/{subject_id}/" if subject_id else "https://movie.douban.com/")
        )
        return {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Cookie": cookie_header,
        }
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Referer": "https://www.douban.com/search?cat=1002" if kind == "classic_search" else "https://movie.douban.com/",
        "Accept": "application/json, text/plain, */*" if kind in ("suggest", "classic_search") else "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie_header,
    }


def _extract_flexible_search_items(payload: Any) -> list[dict[str, Any]]:
    """兼容 /suggest 数组、CF /search 标准数组、Rexxar 嵌套 subjects/items/target 与经典 j/search HTML 片段。"""
    if isinstance(payload, list):
        return [it for it in payload if isinstance(it, dict)]
    if not isinstance(payload, dict):
        return []

    raw_items: list[Any] = []
    subjects = payload.get("subjects")
    if isinstance(subjects, dict) and isinstance(subjects.get("items"), list):
        raw_items.extend(subjects["items"])
    elif isinstance(subjects, list):
        raw_items.extend(subjects)
    if isinstance(payload.get("items"), list):
        raw_items.extend(payload["items"])
    if isinstance(payload.get("data"), list):
        raw_items.extend(payload["data"])

    # 兼容 www.douban.com/j/search 返回的 HTML 片段列表
    if raw_items and all(isinstance(x, str) for x in raw_items):
        cand_map, _ = search_page_candidates("\n".join(raw_items))
        return [{"id": sid, "title": title} for sid, title in cand_map.items()]

    out: list[dict[str, Any]] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        item = entry["target"] if isinstance(entry.get("target"), dict) else entry
        sid = str(item.get("id") or "").strip()
        title = str(item.get("title") or item.get("name") or "").strip()
        if sid.isdigit() and title:
            ep = item.get("episodes_count") or item.get("episode") or ""
            out.append(
                {
                    "id": sid,
                    "title": title,
                    "sub_title": str(item.get("original_title") or item.get("sub_title") or ""),
                    "year": str(item.get("year") or ""),
                    "episode": str(ep) if str(ep).isdigit() else "",
                    "type": str(item.get("type") or entry.get("target_type") or ""),
                }
            )
    return out


def _is_blocked_response(status_code: int, text: str) -> bool:
    if status_code in (401, 403, 302):
        return True
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in ("error code: 004", "sec.douban.com", "captcha", "访问受限", "异常请求"))


def is_cf_proxy_in_backoff() -> bool:
    """检查 CF 反代是否处于刚触发 302/403 后的短暂风控退避窗口。"""
    return time.monotonic() < float(_STATE.get("cf_backoff_until") or 0.0)


def _record_cf_backoff(status_code: int) -> str:
    _STATE["cf_backoff_until"] = time.monotonic() + _CF_PROXY_BACKOFF_SECONDS
    if status_code == 302:
        reason = "CF 反代触发豆瓣临时风控 (HTTP 302)，已自动退避限流（建议在顶栏「豆瓣」导入 Cookie-Editor 提升稳定性）"
    elif status_code == 403:
        reason = "CF 反代被豆瓣拦截 (HTTP 403)，已自动退避（建议导入豆瓣 Cookie-Editor）"
    else:
        reason = f"CF 反代 HTTP {status_code}"
    _STATE["cf_backoff_reason"] = reason
    _STATE["last_fallback_error"] = reason
    return reason


def _cache_get(store: dict[str, tuple[float, Any]], key: str) -> Any | None:
    entry = store.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if time.monotonic() > expires_at:
        store.pop(key, None)
        return None
    return value


def _cache_set(store: dict[str, tuple[float, Any]], key: str, value: Any, ttl: float = _CACHE_TTL_SECONDS) -> None:
    if len(store) > 512:
        now = time.monotonic()
        expired = [k for k, (exp, _) in store.items() if exp <= now]
        for k in expired:
            store.pop(k, None)
        if len(store) > 512:
            store.clear()
    store[key] = (time.monotonic() + ttl, value)


def _cache_suggest_items_meta(hits: list[dict[str, Any]]) -> None:
    """缓存 /suggest 返回的剧集集数与年份元数据，减少不必要的二次详情请求。"""
    for it in hits:
        did = str(it.get("id") or "").strip()
        ep_raw = str(it.get("episode") or "").strip()
        if not did.isdigit() or not ep_raw.isdigit() or int(ep_raw) <= 0:
            continue
        meta = normalize_detail(
            {
                "id": did,
                "douban_id": did,
                "title": str(it.get("title") or ""),
                "sub_title": str(it.get("sub_title") or ""),
                "year": str(it.get("year") or ""),
                "number_of_episodes": int(ep_raw),
                "type": "电视剧",
                "source_url": f"https://movie.douban.com/subject/{did}/",
            }
        )
        _cache_set(_SUGGEST_META_CACHE, did, meta)


def get_suggest_cached_detail(subject_id: str) -> dict[str, Any]:
    """读取 /suggest 阶段已缓存的轻量剧集详情（含集数与年份），命中可免发详情请求。"""
    sid = str(subject_id or "").strip()
    if not sid.isdigit():
        return {}
    cached = _cache_get(_SUGGEST_META_CACHE, sid)
    return dict(cached) if isinstance(cached, dict) else {}


async def _throttle_cf_proxy(min_interval: float = _CF_PROXY_MIN_INTERVAL) -> None:
    global _LAST_CF_REQUEST_AT
    if min_interval <= 0:
        return
    async with _CF_PROXY_LOCK:
        now = time.monotonic()
        wait = (_LAST_CF_REQUEST_AT + min_interval) - now
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_CF_REQUEST_AT = time.monotonic()


async def _throttle_direct_cookie(min_interval: float = _DIRECT_COOKIE_MIN_INTERVAL) -> None:
    global _LAST_DIRECT_REQUEST_AT
    if min_interval <= 0:
        return
    async with _DIRECT_COOKIE_LOCK:
        now = time.monotonic()
        wait = (_LAST_DIRECT_REQUEST_AT + min_interval) - now
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_DIRECT_REQUEST_AT = time.monotonic()


async def search_fallback(
    query: str,
    http_client: httpx.AsyncClient,
    *,
    diagnostics: list[dict[str, Any]] | None = None,
    throttle_seconds: float = _DIRECT_COOKIE_MIN_INTERVAL,
    cf_throttle_seconds: float = _CF_PROXY_MIN_INTERVAL,
) -> dict[str, str]:
    """双引擎备选检索 {subject_id: title}：优先全文检索（覆盖多季/新季）并结合 Suggest 联想，规避跨区 Cookie 静默空列表。"""
    cfg = _effective_config()
    diag = diagnostics if diagnostics is not None else []
    q_norm = str(query or "").strip()
    if not q_norm:
        return {}

    cached_hits = _cache_get(_SEARCH_CACHE, q_norm)
    if isinstance(cached_hits, dict):
        diag.append(
            {
                "source": "cf_proxy_search",
                "query": q_norm,
                "status": "returned" if cached_hits else "empty",
                "candidate_count": len(cached_hits),
                "reason": "命中本次豆瓣检索缓存" if cached_hits else "命中缓存（空列表）",
            }
        )
        return dict(cached_hits)

    found: dict[str, str] = {}

    # 1. Cloudflare Workers 反代通道：优先 /search 全文检索 → 回退 /suggest 下拉联想
    # 注意：公开检索默认不附带跨区国内登录态 Cookie，防止“海外 CF IP + 国内 dbcl2”导致豆瓣静默返回空列表 []；
    # 仅在匿名请求遇 302/403 且配置了 Cookie 时重试一次。
    if cfg["proxy_base"]:
        if is_cf_proxy_in_backoff():
            reason = str(_STATE.get("cf_backoff_reason") or "CF 反代风控退避中，已跳过连续请求")
            diag.append({"source": "cf_proxy_search", "query": q_norm, "status": "blocked", "reason": reason})
        else:
            cf_steps = [
                ("cf_proxy_search", "/search", "CF 反代全文搜索"),
                ("cf_proxy_suggest", "/suggest", "CF 反代联想"),
            ]
            for src_name, ep_path, label in cf_steps:
                if is_cf_proxy_in_backoff():
                    break
                try:
                    await _throttle_cf_proxy(cf_throttle_seconds)
                    resp = await http_client.get(
                        f"{cfg['proxy_base']}{ep_path}",
                        params={"q": q_norm},
                        headers=_build_cf_headers(cfg, include_cookie=False),
                    )
                    # 旧版 Worker 尚未部署 /search 时静默回落 /suggest
                    if resp.status_code == 404 and ep_path == "/search":
                        continue
                    if resp.status_code in (302, 403) and cfg["cookie_header"]:
                        await _throttle_cf_proxy(cf_throttle_seconds)
                        resp = await http_client.get(
                            f"{cfg['proxy_base']}{ep_path}",
                            params={"q": q_norm},
                            headers=_build_cf_headers(cfg, include_cookie=True),
                        )
                    if resp.status_code == 200:
                        hits = _extract_flexible_search_items(resp.json())
                        _cache_suggest_items_meta(hits)
                        diag.append(
                            {
                                "source": src_name,
                                "query": q_norm,
                                "status": "returned" if hits else "empty",
                                "candidate_count": len(hits),
                                "reason": f"{label}返回候选" if hits else f"{label}返回空列表",
                            }
                        )
                        for it in hits:
                            did = str(it.get("id") or "")
                            if did.isdigit():
                                found[did] = it.get("title") or ""
                        if found:
                            _STATE["last_active_channel"] = "cf_proxy"
                            _STATE["last_fallback_error"] = ""
                            _STATE["cf_backoff_until"] = 0.0
                            # 全文搜索命中即直接返回，避免多余 /suggest 请求
                            if ep_path == "/search":
                                break
                    elif resp.status_code in (302, 403):
                        reason = _record_cf_backoff(resp.status_code)
                        diag.append({"source": src_name, "query": q_norm, "status": "blocked", "reason": reason})
                        break
                    else:
                        reason = f"CF 反代 HTTP {resp.status_code}"
                        _STATE["last_fallback_error"] = reason
                        diag.append({"source": src_name, "query": q_norm, "status": "failed", "reason": reason})
                except Exception as exc:
                    reason = lookup_error_reason(exc)
                    _STATE["last_fallback_error"] = reason
                    diag.append(
                        {
                            "source": src_name,
                            "query": q_norm,
                            "status": "failed",
                            "reason": reason,
                            "error_type": type(exc).__name__,
                        }
                    )

            if found:
                _cache_set(_SEARCH_CACHE, q_norm, dict(found))
                return found

    # 2. 本机 Cookie 直连通道：优先 Rexxar 全文检索 → 经典影视 j/search → Suggest 下拉联想
    if cfg["cookie_header"]:
        direct_steps = [
            (
                "douban_cookie_rexxar_search",
                "https://m.douban.com/rexxar/api/v2/search/movie",
                {"q": q_norm, "start": 0, "count": 20},
                "rexxar_search",
                "Cookie 全文搜索",
            ),
            (
                "douban_cookie_j_search",
                "https://www.douban.com/j/search",
                {"q": q_norm, "start": 0, "cat": "1002"},
                "classic_search",
                "Cookie 影视搜索",
            ),
            (
                "douban_cookie_suggest",
                "https://movie.douban.com/j/subject_suggest",
                {"q": q_norm},
                "suggest",
                "Cookie 联想",
            ),
        ]
        for src_name, url, params, hdr_kind, label in direct_steps:
            try:
                await _throttle_direct_cookie(throttle_seconds)
                resp = await http_client.get(
                    url,
                    params=params,
                    headers=_build_direct_headers(hdr_kind, cfg["cookie_header"], query=q_norm),
                )
                if resp.status_code == 200 and not _is_blocked_response(resp.status_code, resp.text):
                    hits = _extract_flexible_search_items(resp.json())
                    _cache_suggest_items_meta(hits)
                    diag.append(
                        {
                            "source": src_name,
                            "query": q_norm,
                            "status": "returned" if hits else "empty",
                            "candidate_count": len(hits),
                            "reason": f"{label}返回候选" if hits else f"{label}返回空列表",
                        }
                    )
                    for it in hits:
                        did = str(it.get("id") or "")
                        if did.isdigit():
                            found[did] = it.get("title") or ""
                    if found:
                        _STATE["last_active_channel"] = "direct_cookie"
                        _STATE["last_fallback_error"] = ""
                        _cache_set(_SEARCH_CACHE, q_norm, dict(found))
                        return found
                else:
                    reason = "Cookie 已失效或触发豆瓣验证/004拦截" if _is_blocked_response(resp.status_code, resp.text) else f"HTTP {resp.status_code}"
                    _STATE["last_fallback_error"] = reason
                    diag.append({"source": src_name, "query": q_norm, "status": "blocked", "reason": reason})
                    if _is_blocked_response(resp.status_code, resp.text):
                        break
            except Exception as exc:
                reason = lookup_error_reason(exc)
                _STATE["last_fallback_error"] = reason
                diag.append(
                    {
                        "source": src_name,
                        "query": q_norm,
                        "status": "failed",
                        "reason": reason,
                        "error_type": type(exc).__name__,
                    }
                )

    _cache_set(_SEARCH_CACHE, q_norm, dict(found))
    return found


async def fetch_detail_fallback(
    subject_id: str,
    http_client: httpx.AsyncClient,
    *,
    diagnostics: list[dict[str, Any]] | None = None,
    throttle_seconds: float = _DIRECT_COOKIE_MIN_INTERVAL,
    cf_throttle_seconds: float = _CF_PROXY_MIN_INTERVAL,
) -> dict[str, Any]:
    """按优先级通过备选通道拉取豆瓣单条目详情（Rexxar 优先 + TTL 缓存 + Suggest 元数据兜底）。"""
    cfg = _effective_config()
    diag = diagnostics if diagnostics is not None else []
    sid = str(subject_id or "").strip()
    if not sid.isdigit():
        return {}

    def _is_usable_detail(d: dict[str, Any]) -> bool:
        return bool(
            d.get("seasons")
            or d.get("number_of_episodes")
            or (d.get("title") and str(d.get("type") or "").casefold() in ("电影", "movie"))
        )

    cached_detail = _cache_get(_DETAIL_CACHE, sid)
    if isinstance(cached_detail, dict) and _is_usable_detail(cached_detail):
        return dict(cached_detail)

    # 1. Cloudflare Workers 反代（优先 Rexxar；404 直接终止；仅在配置了 Cookie 时才尝试网页 /subject 兜底，避免无 Cookie 必出 302）
    if cfg["proxy_base"]:
        if is_cf_proxy_in_backoff():
            reason = str(_STATE.get("cf_backoff_reason") or "CF 反代风控退避中")
            diag.append({"source": "cf_proxy_rexxar", "subject_id": sid, "status": "blocked", "reason": reason})
        else:
            cf_channels: list[tuple[str, str, bool]] = [("cf_proxy_rexxar", f"/rexxar/tv/{sid}", False)]
            if cfg["cookie_header"]:
                cf_channels.append(("cf_proxy_subject", f"/subject/{sid}", True))
            for ch_name, path, is_html in cf_channels:
                try:
                    await _throttle_cf_proxy(cf_throttle_seconds)
                    resp = await http_client.get(
                        f"{cfg['proxy_base']}{path}",
                        headers=_build_cf_headers(cfg, include_cookie=is_html),
                    )
                    if not is_html and resp.status_code in (302, 403) and cfg["cookie_header"]:
                        await _throttle_cf_proxy(cf_throttle_seconds)
                        resp = await http_client.get(
                            f"{cfg['proxy_base']}{path}",
                            headers=_build_cf_headers(cfg, include_cookie=True),
                        )
                    if resp.status_code == 404:
                        diag.append({"source": ch_name, "subject_id": sid, "status": "unavailable", "reason": "非剧集条目 (HTTP 404)"})
                        break
                    if resp.status_code in (302, 403):
                        reason = _record_cf_backoff(resp.status_code)
                        diag.append({"source": ch_name, "subject_id": sid, "status": "blocked", "reason": reason})
                        break
                    if resp.status_code != 200:
                        diag.append({"source": ch_name, "subject_id": sid, "status": "failed", "reason": f"HTTP {resp.status_code}"})
                        continue
                    detail = subject_page_detail(resp.text, sid) if is_html else normalize_detail(resp.json())
                    if _is_usable_detail(detail):
                        _STATE["last_active_channel"] = "cf_proxy"
                        _STATE["last_fallback_error"] = ""
                        _STATE["cf_backoff_until"] = 0.0
                        _cache_set(_DETAIL_CACHE, sid, dict(detail))
                        return detail
                    diag.append({"source": ch_name, "subject_id": sid, "status": "unavailable", "reason": "详情缺少季集数"})
                except Exception as exc:
                    diag.append({"source": ch_name, "subject_id": sid, "status": "failed", "reason": lookup_error_reason(exc)})

    # 2. 本机 Cookie 直连（Rexxar → Subject HTML）
    if cfg["cookie_header"]:
        for ch_name, url, kind, is_html in (
            ("douban_cookie_rexxar", f"https://m.douban.com/rexxar/api/v2/tv/{sid}", "rexxar", False),
            ("douban_cookie_subject", f"https://movie.douban.com/subject/{sid}/", "subject", True),
        ):
            try:
                await _throttle_direct_cookie(throttle_seconds)
                resp = await http_client.get(url, headers=_build_direct_headers(kind, cfg["cookie_header"], sid))
                if _is_blocked_response(resp.status_code, resp.text):
                    diag.append({"source": ch_name, "subject_id": sid, "status": "blocked", "reason": "Cookie 已失效或触发豆瓣验证/004拦截"})
                    break
                if resp.status_code != 200:
                    continue
                detail = subject_page_detail(resp.text, sid) if is_html else normalize_detail(resp.json())
                if _is_usable_detail(detail):
                    _STATE["last_active_channel"] = "direct_cookie"
                    _STATE["last_fallback_error"] = ""
                    _cache_set(_DETAIL_CACHE, sid, dict(detail))
                    return detail
            except Exception as exc:
                diag.append({"source": ch_name, "subject_id": sid, "status": "failed", "reason": lookup_error_reason(exc)})

    # 3. /suggest 阶段已返回的明确剧集元数据兜底（当详情接口短暂退避或被限流时仍可输出准确集数与年份）
    suggest_detail = get_suggest_cached_detail(sid)
    if _is_usable_detail(suggest_detail):
        _cache_set(_DETAIL_CACHE, sid, dict(suggest_detail))
        return suggest_detail

    return {}


async def _probe_cf_proxy(cfg: dict[str, Any], http_client: httpx.AsyncClient) -> dict[str, Any]:
    if not cfg["proxy_base"]:
        return {"configured": False, "ok": False, "http_status": 0, "latency_ms": 0, "sample_title": "", "reason": "未配置反代地址"}
    started = time.monotonic()
    try:
        resp = await http_client.get(f"{cfg['proxy_base']}/probe", headers=_build_cf_headers(cfg))
        latency = int((time.monotonic() - started) * 1000)
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results") if isinstance(data, dict) else {}
            suggest_title = ((results or {}).get("suggest") or {}).get("sample_title") or ""
            rexxar_title = ((results or {}).get("rexxar_tv") or {}).get("sample_title") or ""
            ok = bool(data.get("ok"))
            return {
                "configured": True,
                "ok": ok,
                "http_status": 200,
                "latency_ms": latency,
                "sample_title": suggest_title or rexxar_title,
                "results": results,
                "reason": "CF 反代在线" if ok else "CF 反代上游探测全部失败",
            }
        reason = "Proxy Token 鉴权失败 (401)" if resp.status_code == 401 else f"HTTP {resp.status_code}"
        return {"configured": True, "ok": False, "http_status": resp.status_code, "latency_ms": latency, "sample_title": "", "reason": reason}
    except Exception as exc:
        latency = int((time.monotonic() - started) * 1000)
        return {"configured": True, "ok": False, "http_status": 0, "latency_ms": latency, "sample_title": "", "reason": lookup_error_reason(exc)}


async def _probe_direct_cookie(
    cfg: dict[str, Any],
    http_client: httpx.AsyncClient,
    *,
    throttle_seconds: float = 0.0,
) -> dict[str, Any]:
    if not cfg["cookie_header"]:
        return {"configured": False, "ok": False, "http_status": 0, "latency_ms": 0, "sample_title": "", "reason": "未导入豆瓣 Cookie"}
    started = time.monotonic()
    try:
        await _throttle_direct_cookie(throttle_seconds)
        resp = await http_client.get(
            "https://movie.douban.com/j/subject_suggest",
            params={"q": "庆余年"},
            headers=_build_direct_headers("suggest", cfg["cookie_header"]),
        )
        latency = int((time.monotonic() - started) * 1000)
        if resp.status_code == 200 and not _is_blocked_response(resp.status_code, resp.text):
            hits = search_items(resp.json())
            if hits:
                return {
                    "configured": True,
                    "ok": True,
                    "http_status": 200,
                    "latency_ms": latency,
                    "sample_title": str(hits[0].get("title") or ""),
                    "reason": "Cookie 直连在线",
                }
            return {"configured": True, "ok": False, "http_status": 200, "latency_ms": latency, "sample_title": "", "reason": "返回空列表"}
        reason = "Cookie 已过期或触发豆瓣 004/验证码拦截" if _is_blocked_response(resp.status_code, resp.text) else f"HTTP {resp.status_code}"
        return {"configured": True, "ok": False, "http_status": resp.status_code, "latency_ms": latency, "sample_title": "", "reason": reason}
    except Exception as exc:
        latency = int((time.monotonic() - started) * 1000)
        return {"configured": True, "ok": False, "http_status": 0, "latency_ms": latency, "sample_title": "", "reason": lookup_error_reason(exc)}


async def check_health(
    mp_client: Any = None,
    *,
    mp_online: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """统一执行豆瓣主备健康检查，支持故障切备选与冷却期后自动探测回切 MP。"""
    cfg = _effective_config()
    now = time.time()
    in_cooldown = bool((cfg["proxy_base"] or cfg["cookie_header"]) and now < float(_STATE.get("mp_cooldown_until") or 0.0))

    # 1. 探测 MP 主通道（冷却期未到且非强制探测时暂时跳过 MP，直接用备选承接）
    if mp_online and mp_client is not None and (force or not in_cooldown):
        try:
            mp_res = await mp_client.search_media("庆余年", source="douban")
            if isinstance(mp_res, list) and len(mp_res) > 0:
                record_mp_success()
                return {"status": "online", **get_runtime_status()}
            record_mp_failure("MoviePilot 豆瓣搜索返回空结果")
        except Exception as exc:
            record_mp_failure(f"MoviePilot 豆瓣请求异常: {type(exc).__name__}")
    elif not mp_online and not (cfg["proxy_base"] or cfg["cookie_header"]):
        return {"status": "not_configured", **get_runtime_status()}

    # 2. MP 处于冷却或不可用时，探测已配置的备选通道
    if cfg["proxy_base"] or cfg["cookie_header"]:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            if cfg["proxy_base"]:
                cf_res = await _probe_cf_proxy(cfg, http_client)
                if cf_res["ok"]:
                    _STATE["last_active_channel"] = "cf_proxy"
                    _STATE["last_fallback_error"] = ""
                    return {"status": "fallback:cf_proxy", **get_runtime_status()}
                _STATE["last_fallback_error"] = cf_res.get("reason") or "CF 反代不可用"

            if cfg["cookie_header"]:
                ck_res = await _probe_direct_cookie(cfg, http_client, throttle_seconds=0.0)
                if ck_res["ok"]:
                    _STATE["last_active_channel"] = "direct_cookie"
                    _STATE["last_fallback_error"] = ""
                    return {"status": "fallback:cookie", **get_runtime_status()}
                _STATE["last_fallback_error"] = ck_res.get("reason") or "Cookie 通道不可用"

        return {"status": "error", **get_runtime_status()}

    return {"status": "error", **get_runtime_status()}


async def test_channels(
    mp_client: Any = None,
    *,
    draft: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """对 MP 主通道、CF 反代、Cookie 直连分别做实测（支持草稿参数测试，不写入磁盘）。"""
    parse_preview = None
    if isinstance(draft, dict) and str(draft.get("raw_cookie") or "").strip():
        parse_preview = parse_douban_cookie(str(draft["raw_cookie"]))

    cfg = _effective_config(draft)
    results: dict[str, Any] = {}

    # 1. MoviePilot 主通道
    if mp_client is not None:
        started = time.monotonic()
        try:
            mp_hits = await mp_client.search_media("庆余年", source="douban")
            latency = int((time.monotonic() - started) * 1000)
            if isinstance(mp_hits, list) and len(mp_hits) > 0:
                results["moviepilot"] = {
                    "configured": True,
                    "ok": True,
                    "http_status": 200,
                    "latency_ms": latency,
                    "sample_title": str(mp_hits[0].get("title") or ""),
                    "reason": "MoviePilot 豆瓣主通道正常",
                }
            else:
                results["moviepilot"] = {
                    "configured": True,
                    "ok": False,
                    "http_status": 200,
                    "latency_ms": latency,
                    "sample_title": "",
                    "reason": "MoviePilot 返回空列表（内置 Frodo 被 004 拦截）",
                }
        except Exception as exc:
            results["moviepilot"] = {
                "configured": True,
                "ok": False,
                "http_status": 0,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "sample_title": "",
                "reason": lookup_error_reason(exc),
            }
    else:
        results["moviepilot"] = {
            "configured": False,
            "ok": False,
            "http_status": 0,
            "latency_ms": 0,
            "sample_title": "",
            "reason": "MoviePilot 未配置",
        }

    # 2 & 3. Cloudflare Workers 反代 + 本机 Cookie 直连
    async with httpx.AsyncClient(timeout=12.0) as http_client:
        results["cf_proxy"] = await _probe_cf_proxy(cfg, http_client)
        results["direct_cookie"] = await _probe_direct_cookie(cfg, http_client, throttle_seconds=0.0)

    return {
        "ok": any(bool(v.get("ok")) for v in results.values()),
        "results": results,
        "parse_preview": {
            "format_detected": parse_preview["format_detected"],
            "has_dbcl2": parse_preview["has_dbcl2"],
            "uid_masked": parse_preview["uid_masked"],
            "bid_masked": parse_preview["bid_masked"],
            "cookie_count": parse_preview["cookie_count"],
            "warnings": parse_preview["warnings"],
        }
        if parse_preview
        else None,
    }
