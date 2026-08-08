# -*- coding: utf-8 -*-
"""注册登录态保存与读取工具。

保存 /api/auth/session 的完整 JSON，并额外保存当前浏览器的 ChatGPT cookies，
这样后续本地指纹浏览器可以恢复真实登录态，而不是只把 accessToken 当作页面数据。
"""
from __future__ import annotations

import json
from typing import Any


def _normalize_cookie(cookie: dict[str, Any]) -> dict[str, Any] | None:
    """把 Selenium/Playwright cookie 归一化为 context.add_cookies 可接受的字段。"""
    if not isinstance(cookie, dict):
        return None
    name = str(cookie.get("name") or "").strip()
    if not name:
        return None
    value = str(cookie.get("value") or "")
    out: dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": str(cookie.get("domain") or ".chatgpt.com"),
        "path": str(cookie.get("path") or "/"),
    }
    for key in ("expires", "httpOnly", "secure", "sameSite"):
        if key in cookie and cookie[key] not in (None, ""):
            out[key] = cookie[key]
    return out


def capture_browser_cookies(driver_or_page: Any) -> list[dict[str, Any]]:
    """从 Cloak Selenium 适配器或 Playwright Page/Context 读取 cookies。"""
    context = getattr(driver_or_page, "context", None)
    if context is not None and hasattr(context, "cookies"):
        try:
            cookies = context.cookies(["https://chatgpt.com/"])
            return [x for x in (_normalize_cookie(c) for c in cookies or []) if x]
        except TypeError:
            try:
                cookies = context.cookies()
                return [x for x in (_normalize_cookie(c) for c in cookies or []) if x]
            except Exception:
                pass
        except Exception:
            pass

    get_cookies = getattr(driver_or_page, "get_cookies", None)
    if callable(get_cookies):
        try:
            cookies = get_cookies() or []
            return [x for x in (_normalize_cookie(c) for c in cookies) if x]
        except Exception:
            pass
    return []


def capture_http_cookies(browser_session: Any) -> list[dict[str, Any]]:
    """从 curl_cffi BrowserSession 的 CookieJar 读取 ChatGPT cookies。"""
    jar = getattr(getattr(browser_session, "session", None), "cookies", None)
    if jar is None:
        return []
    out = []
    try:
        for cookie in jar:
            item = _normalize_cookie({
                "name": getattr(cookie, "name", ""),
                "value": getattr(cookie, "value", ""),
                "domain": getattr(cookie, "domain", ".chatgpt.com"),
                "path": getattr(cookie, "path", "/"),
                "expires": getattr(cookie, "expires", None),
                "secure": getattr(cookie, "secure", False),
            })
            if item:
                out.append(item)
    except Exception:
        return []
    return out


def build_saved_session(session_info: dict[str, Any], cookies: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """构造落盘结构：保留 session 原字段，并附加 cookies。"""
    saved = dict(session_info or {})
    saved["cookies"] = [x for x in (_normalize_cookie(c) for c in (cookies or [])) if x]
    return saved


def extract_saved_session(row: dict[str, Any]) -> dict[str, Any] | None:
    """从账号 extra_json 中取回完整 session。"""
    try:
        extra = json.loads(str(row.get("extra_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    session = extra.get("session") if isinstance(extra, dict) else None
    if not isinstance(session, dict):
        return None
    return session
