"""豆瓣 Cookie 全格式解析、安全过滤与脱敏工具。

原生支持：
1. Cookie-Editor 导出的 JSON 数组（以及 ``{"cookies": [...]}`` 包装）
2. Netscape cookies.txt 格式（7 列 Tab 分隔）
3. HTTP Header 字符串（自动剥离 ``Cookie:`` 前缀）
4. 多行 / 单条 ``dbcl2=...`` 键值对

所有存储与日志严格遵循脱敏原则：返回摘要时永不暴露完整 ``dbcl2`` 或 ``ck`` 值。
"""

from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any

_BID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_CORE_DOUBAN_KEYS = (
    "dbcl2",
    "bid",
    "ck",
    "ll",
    "frodo_retry",
    "viewed",
    "ap_v",
    "push_noty_num",
    "push_doumail_num",
    "_vwo_uuid_v2",
)
_MAX_COOKIE_PAIRS = 30


def generate_bid() -> str:
    """生成 11 位字母数字随机浏览器指纹 bid。"""
    return "".join(secrets.choice(_BID_ALPHABET) for _ in range(11))


def _is_douban_domain(domain: str) -> bool:
    dom = str(domain or "").strip().lower().lstrip(".")
    if not dom:
        return True
    return dom == "douban.com" or dom.endswith(".douban.com")


def _mask_token(val: str, prefix_len: int = 4) -> str:
    raw = str(val or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    if len(raw) <= prefix_len:
        return "*" * len(raw)
    return raw[:prefix_len] + "****"


def _mask_dbcl2_uid(val: str) -> str:
    raw = str(val or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    uid = raw.split(":", 1)[0]
    return _mask_token(uid, prefix_len=4)


def _format_cookie_pair(name: str, value: str) -> str:
    clean_name = re.sub(r"[\r\n;=]", "", str(name or "").strip())
    clean_val = re.sub(r"[\r\n;]", "", str(value or "").strip())
    return f"{clean_name}={clean_val}" if clean_name else ""


def _parse_json_cookies(raw: str, now: float) -> dict[str, str] | None:
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get("cookies"), list):
        items = parsed["cookies"]
    elif isinstance(parsed, list):
        items = parsed
    else:
        return None

    out: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or "value" not in item:
            continue
        if not _is_douban_domain(str(item.get("domain") or "")):
            continue
        exp = item.get("expirationDate")
        if exp is None:
            exp = item.get("expires")
        if exp is not None and not item.get("session"):
            try:
                exp_num = float(exp)
                if exp_num > 0 and exp_num < now:
                    continue
            except (TypeError, ValueError):
                pass
        val = str(item.get("value") or "").strip()
        if val != "":
            out[name] = val
    return out


def _parse_netscape_cookies(lines: list[str], now: float) -> dict[str, str] | None:
    matched_rows = 0
    out: dict[str, str] = {}
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) < 7:
            continue
        matched_rows += 1
        domain, _flag, _path, _secure, exp_str, name, value = parts[:7]
        if not _is_douban_domain(domain):
            continue
        try:
            exp_num = float(exp_str)
            if exp_num > 0 and exp_num < now:
                continue
        except (TypeError, ValueError):
            pass
        name = name.strip()
        if name and value.strip() != "":
            out[name] = value.strip()
    return out if matched_rows > 0 else None


def _parse_header_pairs(raw: str) -> dict[str, str]:
    text = str(raw or "").strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    out: dict[str, str] = {}
    # 同时兼容 ';' 与换行分隔的 key=value
    for chunk in re.split(r"[;\r\n]+", text):
        part = chunk.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k and v != "":
            out[k] = v
    return out


def _prioritize_cookies(kv: dict[str, str]) -> tuple[dict[str, str], bool]:
    ordered: dict[str, str] = {}
    for key in _CORE_DOUBAN_KEYS:
        if key in kv and kv[key] != "":
            ordered[key] = kv[key]
    for key, val in kv.items():
        if key not in ordered and val != "":
            if len(ordered) >= _MAX_COOKIE_PAIRS:
                break
            ordered[key] = val

    auto_generated_bid = False
    if "bid" not in ordered or not ordered["bid"].strip():
        ordered["bid"] = generate_bid()
        auto_generated_bid = True
    return ordered, auto_generated_bid


def parse_douban_cookie(raw_text: str, *, now: float | None = None) -> dict[str, Any]:
    """解析任意导出格式的豆瓣 Cookie，返回标准化 Header 与脱敏诊断。"""
    text = str(raw_text or "").strip()
    ts = float(now if now is not None else time.time())
    if not text:
        return {
            "header": "",
            "format_detected": "empty",
            "has_dbcl2": False,
            "uid_masked": "",
            "has_bid": False,
            "bid_masked": "",
            "cookie_count": 0,
            "auto_generated_bid": False,
            "warnings": [],
        }

    fmt = "header_string"
    kv: dict[str, str] | None = None

    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        kv = _parse_json_cookies(text, ts)
        if kv is not None:
            fmt = "cookie_editor_json"

    if kv is None and "\t" in text:
        lines = text.splitlines()
        ns_kv = _parse_netscape_cookies(lines, ts)
        if ns_kv is not None:
            kv = ns_kv
            fmt = "netscape"

    if kv is None:
        kv = _parse_header_pairs(text)
        fmt = "header_string"

    warnings: list[str] = []
    if not kv:
        warnings.append("未能从输入中提取任何有效豆瓣 Cookie（请确认导出的是 douban.com 域，且条目未全部过期）")
        return {
            "header": "",
            "format_detected": fmt,
            "has_dbcl2": False,
            "uid_masked": "",
            "has_bid": False,
            "bid_masked": "",
            "cookie_count": 0,
            "auto_generated_bid": False,
            "warnings": warnings,
        }

    ordered, auto_bid = _prioritize_cookies(kv)
    pairs = [_format_cookie_pair(k, v) for k, v in ordered.items()]
    header = "; ".join(p for p in pairs if p)

    has_dbcl2 = bool(ordered.get("dbcl2"))
    uid_masked = _mask_dbcl2_uid(ordered.get("dbcl2", ""))
    bid_val = ordered.get("bid", "")
    bid_masked = _mask_token(bid_val, prefix_len=4)

    if not has_dbcl2:
        warnings.append("未检测到 dbcl2 登录凭据：仅含匿名 bid 在部分风控 IP 下仍可能触发豆瓣 004 登录拦截")
    if auto_bid:
        warnings.append("原导出内容缺少 bid 指纹，已自动生成 11 位随机 bid 补齐")

    return {
        "header": header,
        "format_detected": fmt,
        "has_dbcl2": has_dbcl2,
        "uid_masked": uid_masked,
        "has_bid": bool(bid_val),
        "bid_masked": bid_masked,
        "cookie_count": len(ordered),
        "auto_generated_bid": auto_bid,
        "warnings": warnings,
    }


def mask_cookie_summary(
    header: str,
    *,
    format_detected: str = "",
    updated_at: str = "",
) -> dict[str, Any]:
    """为前端和日志生成脱敏后的已存 Cookie 摘要，永不泄露原始凭据。"""
    text = str(header or "").strip()
    if not text:
        return {
            "configured": False,
            "format_detected": "",
            "has_dbcl2": False,
            "uid_masked": "",
            "has_bid": False,
            "bid_masked": "",
            "cookie_count": 0,
            "updated_at": updated_at or "",
        }
    kv = _parse_header_pairs(text)
    dbcl2_val = kv.get("dbcl2", "")
    bid_val = kv.get("bid", "")
    return {
        "configured": bool(kv),
        "format_detected": format_detected or "header_string",
        "has_dbcl2": bool(dbcl2_val),
        "uid_masked": _mask_dbcl2_uid(dbcl2_val),
        "has_bid": bool(bid_val),
        "bid_masked": _mask_token(bid_val, prefix_len=4),
        "cookie_count": len(kv),
        "updated_at": updated_at or "",
    }
