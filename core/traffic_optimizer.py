# -*- coding: utf-8 -*-
"""浏览器注册阶段的低风险请求拦截。

实现使用 Chromium CDP ``Network.setBlockedURLs``，不安装 Playwright route，
避免 route interception 关闭 HTTP cache。默认只阻断明确的分析主机，以及
登录表单不依赖的媒体扩展；核心请求和挑战资源保持原样。
"""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from config import traffic as _cfg

logger = logging.getLogger(__name__)


@dataclass
class TrafficOptimizationHandle:
    """记录一次拦截安装结果，便于日志、账号流量诊断和测试。"""

    enabled: bool = False
    method: str = "disabled"
    label: str = "registration"
    blocked_patterns: list[str] = field(default_factory=list)
    error: str = ""

    def snapshot(self) -> dict:
        out = {
            "enabled": bool(self.enabled),
            "method": str(self.method or "disabled"),
            "blocked_pattern_count": len(self.blocked_patterns),
        }
        if self.label:
            out["label"] = self.label[:80]
        if self.error:
            out["error"] = self.error[:240]
        return out


def _enabled() -> bool:
    return bool(getattr(_cfg, "REGISTRATION_TRAFFIC_OPTIMIZATION", True))


def _host_matches(host: str, pattern: str) -> bool:
    host = str(host or "").strip().lower().rstrip(".")
    pattern = str(pattern or "").strip().lower().rstrip(".")
    if not host or not pattern:
        return False
    return fnmatch.fnmatchcase(host, pattern)


def _host_patterns() -> list[str]:
    if not bool(getattr(_cfg, "REGISTRATION_BLOCK_ANALYTICS", True)):
        return []
    return [str(item).strip() for item in (getattr(_cfg, "REGISTRATION_ANALYTICS_HOSTS", ()) or ()) if str(item).strip()]


def _media_patterns() -> list[str]:
    if not bool(getattr(_cfg, "REGISTRATION_BLOCK_MEDIA", True)):
        return []
    hosts = [str(item).strip() for item in (getattr(_cfg, "REGISTRATION_MEDIA_HOSTS", ()) or ()) if str(item).strip()]
    extensions = [str(item).strip().lower() for item in (getattr(_cfg, "REGISTRATION_MEDIA_EXTENSIONS", ()) or ()) if str(item).strip()]
    return [f"*://{host}/*{extension}*" for host in hosts for extension in extensions]


def blocked_url_patterns() -> list[str]:
    """生成 Chromium/CDP URL pattern；顺序稳定，便于审计和测试。"""
    if not _enabled():
        return []
    patterns: list[str] = []
    for host in _host_patterns():
        patterns.extend((f"*://{host}/*", f"*://{host}:*/*"))
    patterns.extend(_media_patterns())
    # 保持去重，避免重复规则增加 DevTools payload。
    return list(dict.fromkeys(patterns))


def should_block_url(url: str, *, resource_type: str | None = None) -> bool:
    """判断单个请求是否属于可拦截的可选流量。

    ``resource_type`` 由 Playwright 提供；CDP/Selenium 没有该字段时依靠 URL
    pattern 规则。空/非法 URL 一律放行，避免影响页面导航。
    """
    if not _enabled():
        return False
    try:
        parsed = urlsplit(str(url or ""))
        host = (parsed.hostname or "").lower().rstrip(".")
        path = (parsed.path or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if any(_host_matches(host, pattern) for pattern in _host_patterns()):
        return True
    if not bool(getattr(_cfg, "REGISTRATION_BLOCK_MEDIA", True)):
        return False
    if host not in {
        str(item).strip().lower().rstrip(".")
        for item in (getattr(_cfg, "REGISTRATION_MEDIA_HOSTS", ()) or ())
    }:
        return False
    media_types = {"image", "media", "font", "texttrack"}
    if resource_type and str(resource_type).lower() in media_types:
        return True
    return any(path.endswith(extension) for extension in getattr(_cfg, "REGISTRATION_MEDIA_EXTENSIONS", ()) or ())


def _store_handle(target, handle: TrafficOptimizationHandle) -> TrafficOptimizationHandle:
    try:
        setattr(target, "_registration_traffic_optimization", handle)
    except Exception:
        pass
    return handle


def optimization_snapshot(target) -> dict:
    """读取 driver/page/context 上最近一次安装结果。"""
    handle = getattr(target, "_registration_traffic_optimization", None)
    if isinstance(handle, TrafficOptimizationHandle):
        return handle.snapshot()
    return {}


def install_selenium_network_optimization(driver, *, label: str = "Roxy") -> TrafficOptimizationHandle:
    """通过 Selenium CDP 为 Roxy/Chromium 会话安装 URL 阻断规则。"""
    patterns = blocked_url_patterns()
    handle = TrafficOptimizationHandle(
        enabled=bool(_enabled() and patterns),
        method="cdp" if patterns else "disabled",
        label=label,
        blocked_patterns=patterns,
    )
    if not patterns:
        return _store_handle(driver, handle)
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": patterns})
        # 明确保持缓存开启；setBlockedURLs 本身不会像 Playwright route 那样关闭缓存。
        try:
            driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": False})
        except Exception:
            pass
        logger.info("[%s] 注册流量优化已启用：阻断 %s 条 URL 规则", label, len(patterns))
    except Exception as exc:
        handle.enabled = False
        handle.method = "disabled"
        handle.error = f"{type(exc).__name__}: {exc}"
        logger.warning("[%s] 注册流量优化安装失败，保持原始请求：%s", label, handle.error[:180])
    return _store_handle(driver, handle)


def _install_playwright_page(context, page, patterns: list[str], label: str) -> TrafficOptimizationHandle:
    """在一个 Playwright page 对应的 CDP session 上安装规则。"""
    try:
        cdp = context.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.send("Network.setBlockedURLs", {"urls": patterns})
        try:
            cdp.send("Network.setCacheDisabled", {"cacheDisabled": False})
        except Exception:
            pass
        handle = TrafficOptimizationHandle(True, "cdp", label, list(patterns))
        logger.info("[%s] 注册流量优化已启用：阻断 %s 条 URL 规则", label, len(patterns))
    except Exception as exc:
        handle = TrafficOptimizationHandle(False, "disabled", label, list(patterns), f"{type(exc).__name__}: {exc}")
        logger.warning("[%s] 注册流量优化安装失败，保持原始请求：%s", label, handle.error[:180])
    _store_handle(page, handle)
    return handle


def install_playwright_network_optimization(context, page=None, *, label: str = "Browser") -> TrafficOptimizationHandle:
    """为 Cloak/Browser Use/Skyvern context 安装 CDP URL 阻断规则。

    监听后续新页面（例如 Cloudflare challenge/授权弹窗），避免只优化首个
    page。若 CDP 不可用则 fail-open，不改变注册流程。
    """
    patterns = blocked_url_patterns()
    if not patterns:
        handle = TrafficOptimizationHandle(False, "disabled", label)
        _store_handle(context, handle)
        if page is not None:
            _store_handle(page, handle)
        return handle
    if context is None or page is None:
        handle = TrafficOptimizationHandle(False, "disabled", label, patterns, "context/page unavailable")
        return _store_handle(context, handle) if context is not None else handle

    handle = _install_playwright_page(context, page, patterns, label)
    _store_handle(context, handle)
    try:
        def on_page(new_page):
            _install_playwright_page(context, new_page, patterns, label)

        context.on("page", on_page)
    except Exception as exc:
        logger.debug("[%s] 新页面流量优化监听安装跳过：%s", label, exc)
    return handle
