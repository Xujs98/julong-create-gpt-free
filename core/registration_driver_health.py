# -*- coding: utf-8 -*-
"""注册驱动名称归一化与启动前配置检查。"""
from __future__ import annotations

import importlib.util
import socket
import time
from urllib.parse import urlsplit

import requests


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


def _canonical_roxy_base(value: str | None) -> str | None:
    from core.roxy_selenium import canonicalize_roxy_api_base
    return canonicalize_roxy_api_base(value)


def _roxy_http_error_kind(exc: Exception) -> str:
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "HTTP connect timeout"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "HTTP read timeout"
    if isinstance(exc, requests.exceptions.Timeout):
        return "HTTP timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "HTTP connection error"
    if isinstance(exc, requests.exceptions.InvalidURL):
        return "HTTP invalid URL"
    if isinstance(exc, requests.exceptions.RequestException):
        return f"HTTP {type(exc).__name__}"
    return type(exc).__name__


def roxy_api_runtime_check(
    value: str | None = None,
    *,
    timeout: float = 0.8,
    probe_http: bool = False,
) -> dict:
    """探测 RoxyBrowser API 端点是否有服务监听。

    默认只建立一个很短的 TCP 连接。运行时预检会额外执行只读的工作区 GET，
    用来区分“端口监听”与“API 请求可返回”。返回值同时保留 TCP/HTTP 分层结果。
    """
    if value is None:
        from config import roxybrowser as cfg
        value = getattr(cfg, "ROXY_API_BASE", "")
    configured = str(value or "").strip()
    # 注册主流程会在 Docker 内把宿主机 Roxy 的 loopback 地址改写为
    # host.docker.internal；补跑预检必须复用同一规则，否则会错误探测容器
    # 自身的 127.0.0.1 并降级到 Cloak。
    try:
        from core.roxy_selenium import normalize_api_base
        raw = str(normalize_api_base(configured) or configured).strip()
    except Exception:  # pragma: no cover - 地址改写失败时仍保留原探测行为
        raw = configured
    result = {
        "reachable": False,
        "tcp_reachable": False,
        "http_checked": False,
        "http_reachable": None,
        "http_status": None,
        "http_elapsed_ms": None,
        "http_error": None,
        "http_probe_path": None,
        "host": "",
        "port": None,
        "error": None,
        "api_base": raw,
        "configured_api_base": configured,
        "runtime_rewritten": raw != configured,
    }
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            result["error"] = "ROXY_API_BASE 不是有效 HTTP 地址"
            return result
        # urlsplit.port 在端口非法时抛 ValueError，转换成可读错误而不是让补跑
        # 在真正请求阶段才出现难以定位的异常。
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        host = parsed.hostname
        result["host"] = host
        result["port"] = port
        with socket.create_connection((host, port), timeout=max(0.1, float(timeout))):
            pass
        result["tcp_reachable"] = True
        result["reachable"] = True
        if probe_http:
            from config import roxybrowser as cfg

            path = str(getattr(cfg, "ROXY_WORKSPACE_LIST_PATH", "/browser/workspace") or "/browser/workspace").strip()
            if not path.startswith("/"):
                path = f"/{path}"
            result["http_checked"] = True
            result["http_probe_path"] = path
            endpoint = f"{raw.rstrip('/')}{path}"
            headers = {"Accept": "application/json"}
            token = str(getattr(cfg, "ROXY_API_TOKEN", "") or "").strip()
            if token:
                headers.update({"token": token, "Authorization": f"Bearer {token}"})
            started = time.monotonic()
            try:
                response = requests.get(
                    endpoint,
                    headers=headers,
                    timeout=max(0.1, float(timeout)),
                )
                result["http_elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
                result["http_status"] = int(response.status_code)
                result["http_reachable"] = bool(response.ok)
                if not response.ok:
                    result["http_error"] = f"HTTP status {response.status_code}"
                else:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    if isinstance(payload, dict):
                        code = payload.get("code")
                        ok = payload.get("ok")
                        success = payload.get("success")
                        if code not in (None, 0, 200, "0", "200") and ok is not True and success is not True:
                            result["http_error"] = f"Roxy API 返回失败 code={code}: {str(payload.get('msg') or payload.get('message') or '')[:240]}"
                            result["http_reachable"] = False
                if result["http_error"]:
                    result["error"] = result["http_error"]
            except Exception as exc:
                result["http_elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
                result["http_reachable"] = False
                result["http_error"] = f"{_roxy_http_error_kind(exc)}: {exc}"
                result["error"] = result["http_error"]
            result["reachable"] = bool(result["tcp_reachable"] and result["http_reachable"])
    except ValueError as exc:
        result["error"] = f"Roxy API 地址解析失败: {exc}"
    except OSError as exc:
        result["error"] = f"TCP {type(exc).__name__}: {exc}"
    except Exception as exc:  # pragma: no cover - 防止探测影响主流程
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


# 语义化别名，便于其他任务/旧调用方复用同一探测实现。
check_roxy_api_reachable = roxy_api_runtime_check


def registration_driver_runtime_preflight(value: str | None = None, *, timeout: float = 0.8) -> dict:
    """在静态配置检查基础上，补充 Roxy 本地 API 的运行时连通性检查。"""
    result = registration_driver_preflight(value)
    if result["driver"] != "roxy":
        result["details"]["runtime_checked"] = False
        return result

    probe = roxy_api_runtime_check(result["details"].get("api_base"), timeout=timeout, probe_http=True)
    api_reachable = bool(probe.get("reachable")) and (
        not probe.get("http_checked") or bool(probe.get("http_reachable"))
    )
    result["details"].update({
        "runtime_checked": True,
        "reachable": api_reachable,
        "host": probe.get("host", ""),
        "port": probe.get("port"),
        "error": probe.get("error"),
        "configured_api_base": probe.get("configured_api_base", ""),
        "api_base": probe.get("api_base", ""),
        "runtime_rewritten": bool(probe.get("runtime_rewritten")),
        "tcp_reachable": bool(probe.get("tcp_reachable")),
        "http_checked": bool(probe.get("http_checked")),
        "http_reachable": probe.get("http_reachable"),
        "http_status": probe.get("http_status"),
        "http_elapsed_ms": probe.get("http_elapsed_ms"),
        "http_error": probe.get("http_error"),
        "http_probe_path": probe.get("http_probe_path"),
    })
    if not api_reachable:
        error = str(probe.get("error") or "未知连接错误")
        result["errors"].append(f"Roxy API 不可达: {error}")
        result["ok"] = False
    return result


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
        configured_base = str(getattr(cfg, "ROXY_API_BASE", "") or "")
        if not _canonical_roxy_base(configured_base):
            errors.append("ROXY_API_BASE 不是有效 HTTP 地址")
        if not str(getattr(cfg, "ROXY_API_TOKEN", "") or "").strip():
            errors.append("ROXY_API_TOKEN 为空")
        profile = str(getattr(cfg, "ROXY_PROFILE_ID", "") or "").strip()
        if not profile and not str(getattr(cfg, "ROXY_WORKSPACE_ID", "") or "").strip():
            errors.append("创建 Roxy 环境需要 ROXY_WORKSPACE_ID")
        if importlib.util.find_spec("selenium") is None:
            errors.append("缺少 selenium 依赖")
        details["configured_api_base"] = configured_base
        details["api_base"] = _canonical_roxy_base(configured_base) or configured_base
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


def require_registration_driver_ready(value: str | None = None, *, runtime: bool = False) -> dict:
    """所选注册方式未就绪时抛出带具体配置项的错误。"""
    driver = normalize_registration_driver(value)
    result = (
        registration_driver_runtime_preflight(value)
        if runtime and driver == "roxy"
        else registration_driver_preflight(value)
    )
    if not result["ok"]:
        raise RuntimeError(f"{result['label']} 未就绪：" + "；".join(result["errors"]))
    return result
