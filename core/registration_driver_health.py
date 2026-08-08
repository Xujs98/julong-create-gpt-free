# -*- coding: utf-8 -*-
"""注册驱动名称归一化与启动前配置检查。"""
from __future__ import annotations

import importlib.util
from urllib.parse import urlsplit


DRIVER_LABELS = {
    "protocol": "纯协议注册",
    "roxy": "RoxyBrowser",
    "cloak": "本地指纹浏览器",
    "browser_use": "browser_use",
    "skyvern": "skyvern",
}

_ALIASES = {
    "api": "protocol", "http": "protocol",
    "roxybrowser": "roxy", "fingerprint": "roxy", "browser": "roxy",
    "cloakbrowser": "cloak",
    "browseruse": "browser_use", "browser-use": "browser_use", "bu": "browser_use",
    "sv": "skyvern",
}


def normalize_registration_driver(value: str | None = None) -> str:
    """把配置别名转换成 WebUI 展示的五种标准注册方式。"""
    if value is None:
        from config import roxybrowser as cfg
        value = getattr(cfg, "REGISTRATION_DRIVER", "protocol")
    driver = str(value or "protocol").strip().lower()
    return _ALIASES.get(driver, driver)


def _valid_url(value: str, schemes: set[str]) -> bool:
    try:
        parsed = urlsplit(str(value or "").strip())
        return parsed.scheme.lower() in schemes and bool(parsed.hostname)
    except ValueError:
        return False


def registration_driver_preflight(value: str | None = None) -> dict:
    """检查所选注册方式的依赖和必填配置，不创建浏览器或远端会话。"""
    driver = normalize_registration_driver(value)
    errors: list[str] = []
    details: dict = {}
    if driver not in DRIVER_LABELS:
        errors.append(f"未知注册方式: {driver}")
    elif driver == "protocol":
        if importlib.util.find_spec("curl_cffi") is None:
            errors.append("缺少 curl_cffi 依赖")
        from config import proxy as proxy_cfg
        from core.proxy_utils import masked_proxy_url, normalize_proxy_url
        proxy = proxy_cfg.pick_proxy()
        if proxy:
            try:
                normalized = normalize_proxy_url(proxy, default_scheme="auto")
                details["proxy"] = masked_proxy_url(normalized)
            except ValueError as exc:
                errors.append(f"代理格式错误: {exc}")
    elif driver == "roxy":
        from config import roxybrowser as cfg
        if not _valid_url(getattr(cfg, "ROXY_API_BASE", ""), {"http", "https"}):
            errors.append("ROXY_API_BASE 不是有效 HTTP 地址")
        if not str(getattr(cfg, "ROXY_API_TOKEN", "") or "").strip():
            errors.append("ROXY_API_TOKEN 为空")
        profile = str(getattr(cfg, "ROXY_PROFILE_ID", "") or "").strip()
        if not profile and not str(getattr(cfg, "ROXY_WORKSPACE_ID", "") or "").strip():
            errors.append("创建 Roxy 环境需要 ROXY_WORKSPACE_ID")
        if importlib.util.find_spec("selenium") is None:
            errors.append("缺少 selenium 依赖")
        details["api_base"] = str(getattr(cfg, "ROXY_API_BASE", "") or "")
    elif driver == "cloak":
        if importlib.util.find_spec("cloakbrowser") is None:
            errors.append("缺少 cloakbrowser 依赖")
        if importlib.util.find_spec("socks") is None:
            errors.append("缺少 PySocks 依赖")
        from config import proxy as proxy_cfg
        from core.proxy_utils import masked_proxy_url, normalize_proxy_url
        proxy = proxy_cfg.pick_proxy()
        if proxy:
            try:
                normalized = normalize_proxy_url(proxy, default_scheme="auto")
                details["proxy"] = masked_proxy_url(normalized)
            except ValueError as exc:
                errors.append(f"代理格式错误: {exc}")
    elif driver == "browser_use":
        from config import browser_use as cfg
        if not str(getattr(cfg, "BROWSER_USE_API_KEY", "") or "").strip():
            errors.append("BROWSER_USE_API_KEY 为空")
        if not _valid_url(getattr(cfg, "BROWSER_USE_CDP_BASE", ""), {"ws", "wss"}):
            errors.append("BROWSER_USE_CDP_BASE 不是有效 WebSocket 地址")
        if importlib.util.find_spec("playwright") is None:
            errors.append("缺少 playwright 依赖")
        details["cdp_base"] = str(getattr(cfg, "BROWSER_USE_CDP_BASE", "") or "")
    elif driver == "skyvern":
        from config import skyvern as cfg
        if not str(getattr(cfg, "SKYVERN_API_KEY", "") or "").strip():
            errors.append("SKYVERN_API_KEY 为空")
        if not _valid_url(getattr(cfg, "SKYVERN_API_BASE", ""), {"http", "https"}):
            errors.append("SKYVERN_API_BASE 不是有效 HTTP 地址")
        if importlib.util.find_spec("playwright") is None:
            errors.append("缺少 playwright 依赖")
        details["api_base"] = str(getattr(cfg, "SKYVERN_API_BASE", "") or "")
    return {
        "driver": driver,
        "label": DRIVER_LABELS.get(driver, driver),
        "ok": not errors,
        "errors": errors,
        "details": details,
    }


def all_registration_driver_preflights() -> list[dict]:
    """返回五种注册方式的静态就绪状态。"""
    return [registration_driver_preflight(driver) for driver in DRIVER_LABELS]


def require_registration_driver_ready(value: str | None = None) -> dict:
    """所选注册方式未就绪时抛出带具体配置项的错误。"""
    result = registration_driver_preflight(value)
    if not result["ok"]:
        raise RuntimeError(f"{result['label']} 未就绪：" + "；".join(result["errors"]))
    return result
