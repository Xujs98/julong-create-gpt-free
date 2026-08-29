# -*- coding: utf-8 -*-
"""通过官方站点浏览器上下文执行各国资格查询。

Turnstile 令牌绑定生成令牌时的 hostname。WebUI 通常运行在 localhost，
直接在本地页面渲染 tools.oai9.com 的 site key 会收到 110200（域名未授权）。
本模块让官方页面自己生成令牌并提交请求，再把 JSON 结果交回后台队列。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any

from config import webui as webui_config
from core.oaics_checker import (
    COUNTRY_QUALIFICATION_SITE_ORIGIN,
    CountryQualificationError,
    parse_country_qualification_response,
)

logger = logging.getLogger(__name__)

_BROWSER_LOCK = threading.RLock()
_PLAYWRIGHT = None
_BROWSER = None


def _timeout_seconds(value: Any = None) -> float:
    try:
        value = float(value if value is not None else getattr(
            webui_config, "COUNTRY_QUALIFICATION_BROWSER_TIMEOUT", 240
        ))
    except (TypeError, ValueError):
        value = 240.0
    # The official challenge occasionally spends more than a minute loading
    # challenge-platform resources before the POST response is available.
    return max(10.0, min(300.0, value))


def _headless_mode() -> bool:
    configured = str(getattr(webui_config, "COUNTRY_QUALIFICATION_BROWSER_HEADLESS", "auto") or "auto").strip().lower()
    if configured in {"true", "1", "yes", "on", "headless"}:
        return True
    if configured in {"false", "0", "no", "off", "headed"}:
        return False
    # macOS can launch a visible browser without DISPLAY; Linux servers usually
    # need headless mode unless a display/Wayland session is present.
    # ``os.name`` is ``posix`` on macOS, so use ``sys.platform`` here.  A
    # headed system Chrome is required for Turnstile on macOS; forcing the
    # bundled headless Chromium causes the challenge to stall.
    if sys.platform == "darwin":
        return False
    return not bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _close_browser() -> None:
    global _BROWSER, _PLAYWRIGHT
    try:
        if _BROWSER is not None:
            _BROWSER.close()
    except Exception:
        logger.debug("关闭资格查询浏览器失败", exc_info=True)
    _BROWSER = None
    try:
        if _PLAYWRIGHT is not None:
            _PLAYWRIGHT.stop()
    except Exception:
        logger.debug("关闭 Playwright 失败", exc_info=True)
    _PLAYWRIGHT = None


def _get_browser():
    global _BROWSER, _PLAYWRIGHT
    if _BROWSER is not None:
        return _BROWSER
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - exercised on minimal installs
        raise RuntimeError("各国资格浏览器验证需要 playwright 依赖") from exc
    _PLAYWRIGHT = sync_playwright().start()
    headless = _headless_mode()
    args = ["--disable-blink-features=AutomationControlled"]
    # Playwright normally adds ``--enable-automation``.  Turnstile's browser
    # challenge treats that marker as a bot signal and leaves the trial request
    # pending indefinitely.  Keep the real Chrome fingerprint while still
    # controlling the page through Playwright.
    launch_options = {
        "headless": headless,
        "args": args,
        "ignore_default_args": ["--enable-automation"],
    }
    # Use the installed Chrome channel when available; Cloudflare's browser
    # challenge is less likely to classify a bundled test Chromium as a bot.
    if sys.platform == "darwin":
        launch_options["channel"] = "chrome"
    try:
        _BROWSER = _PLAYWRIGHT.chromium.launch(**launch_options)
    except Exception:
        launch_options.pop("channel", None)
        if not headless:
            logger.warning("可见资格验证窗口启动失败，回退无头浏览器")
            launch_options["headless"] = True
            _BROWSER = _PLAYWRIGHT.chromium.launch(**launch_options)
        else:
            _close_browser()
            raise
    return _BROWSER


def _protocol_error(response: Any) -> CountryQualificationError:
    status = int(getattr(response, "status", 0) or 0) or None
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or payload.get("error") or "").strip()
    except Exception:
        pass
    suffix = f": {detail[:180]}" if detail else ""
    requires_turnstile = status == 403 and any(
        marker in detail.lower() for marker in ("turnstile", "安全验证", "验证失败", "captcha")
    )
    message = f"country qualification HTTP {status or 0}{suffix}"
    if requires_turnstile:
        message += "；官方浏览器验证未取得有效令牌"
    return CountryQualificationError(
        message,
        status_code=status,
        detail=detail,
        requires_turnstile=requires_turnstile,
    )


def _extend_turnstile_guard_timeout(page: Any, timeout_ms: int) -> bool:
    """Patch the official guard's 30s client timeout for slow challenges.

    The official page occasionally needs over 30 seconds to finish the
    challenge-platform handshake.  Its fixed timeout then displays
    ``安全验证暂时不可用`` without ever sending the API request.  The page is
    still loaded from the official origin; only the local wait budget changes.
    """
    route = getattr(page, "route", None)
    if not callable(route):
        return False

    def handle(route_obj: Any) -> None:
        try:
            response = route_obj.fetch()
            body = response.body()
            text = body.decode("utf-8")
            patched = text.replace("timeoutMs = 30000", f"timeoutMs = {int(timeout_ms)}")
            if patched != text:
                route_obj.fulfill(response=response, body=patched.encode("utf-8"))
            else:
                route_obj.fulfill(response=response)
        except Exception:
            try:
                route_obj.continue_()
            except Exception:
                logger.debug("继续加载 Turnstile guard 失败", exc_info=True)

    route("**/turnstile-guard/turnstile-guard.js**", handle)
    return True


def query_country_qualification_browser(
    access_token: str,
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """在 tools.oai9.com 页面内完成 Turnstile + 查询，返回标准化结果。"""
    token = str(access_token or "").strip()
    if not token:
        raise ValueError("access token is empty")
    timeout_seconds = _timeout_seconds(timeout)
    with _BROWSER_LOCK:
        browser = _get_browser()
        try:
            page = browser.new_page()
        except Exception:
            # The shared browser may have been closed by the operator or by
            # a renderer crash.  Recreate the official context once.
            _close_browser()
            browser = _get_browser()
            page = browser.new_page()
        try:
            _extend_turnstile_guard_timeout(page, int(timeout_seconds * 1000))
            wait_timeout_seconds = min(300.0, timeout_seconds + 15.0)
            page.goto(
                f"{COUNTRY_QUALIFICATION_SITE_ORIGIN}/",
                wait_until="domcontentloaded",
                timeout=int(wait_timeout_seconds * 1000),
            )
            page.locator("#access-token").fill(token)
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.rstrip("/").endswith("/api/trial/check"),
                timeout=int(wait_timeout_seconds * 1000),
            ) as response_info:
                page.locator("#trial-check-btn").click(timeout=int(wait_timeout_seconds * 1000))
            response = response_info.value
            if not 200 <= int(response.status) < 300:
                raise _protocol_error(response)
            try:
                payload = response.json()
            except Exception as exc:
                raise ValueError(f"country qualification returned invalid JSON: {type(exc).__name__}") from exc
            return parse_country_qualification_response(payload)
        except CountryQualificationError:
            raise
        except Exception as exc:
            raise RuntimeError(f"官方浏览器验证失败: {type(exc).__name__}: {str(exc)[:160]}") from exc
        finally:
            try:
                page.close()
            except Exception:
                pass


def reset_country_qualification_browser() -> None:
    """测试/配置变更时关闭共享浏览器；下次查询会重新建立官方上下文。"""
    with _BROWSER_LOCK:
        _close_browser()
