# -*- coding: utf-8 -*-
"""查活代理 API：按账号地区获取短时代理并统一解析返回格式。"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests

from core.proxy_utils import normalize_proxy_url
from config.proxy_api import detect_provider
from core.proxy_http_compat import safe_transport_error


logger = logging.getLogger(__name__)


DEFAULT_PROXY_API_URL = (
    "https://api.cliproxy.io/white/api?region={region}&num=2&time=10&format=n&type=json"
)


def build_proxy_api_url(template: str | None, region: str) -> str:
    """把账号国家码写入 API URL，同时兼容 ``{region}`` 占位符。"""
    raw = str(template or DEFAULT_PROXY_API_URL).strip()
    if not raw:
        raw = DEFAULT_PROXY_API_URL
    value = str(region or "").strip()
    encoded = quote(value, safe="")
    raw = raw.replace("{region}", encoded).replace("{country}", encoded).replace("{country_code}", encoded)
    parsed = urlsplit(raw)
    query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() != "region"]
    if detect_provider(raw) != "b2proxy" or value.lower() != "rand":
        query.insert(0, ("region", value))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _values(payload: Any, separator: str = "") -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, (list, tuple, set)):
        out: list[Any] = []
        for item in payload:
            out.extend(_values(item, separator))
        return out
    if isinstance(payload, dict):
        host = payload.get("host") or payload.get("hostname") or payload.get("ip")
        port = payload.get("port")
        if host and port:
            username = payload.get("username") or payload.get("user") or payload.get("proxy_username")
            password = payload.get("password") or payload.get("pass") or payload.get("proxy_password")
            if username is not None or password is not None:
                return [f"{host}:{port}:{username or ''}:{password or ''}"]
            return [f"{host}:{port}"]
        out: list[Any] = []
        for key in ("data", "proxies", "proxy", "list", "result", "items", "ips"):
            if key in payload:
                out.extend(_values(payload.get(key), separator))
        return out
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            if separator:
                text = text.replace(separator, "\n")
            return [line.strip() for line in text.replace("\t", "\n").replace(",", "\n").splitlines() if line.strip()]
        return _values(decoded, separator)
    return [str(payload)]


def parse_proxy_api_response(payload: Any, *, default_scheme: str = "auto", separator: str = "") -> list[str]:
    """提取并标准化 API 返回的代理地址，去重后保留原顺序。"""
    result: list[str] = []
    seen: set[str] = set()
    for raw in _values(payload, separator):
        value = str(raw or "").strip().strip('"').strip("'")
        if not value or "***" in value:
            continue
        try:
            normalized = normalize_proxy_url(value, default_scheme=default_scheme)
        except (TypeError, ValueError):
            continue
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def fetch_proxy_api(region: str, *, api_url: str | None = None, timeout: float = 8.0) -> list[str]:
    """请求代理 API；HTTP、JSON 数组、``data/proxies/list`` 均可解析。"""
    url = build_proxy_api_url(api_url, region)
    response = requests.get(url, timeout=max(0.5, float(timeout)), headers={"Accept": "application/json,text/plain,*/*"})
    response.raise_for_status()
    try:
        payload = response.json()
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = response.text
    query = dict(parse_qsl(urlsplit(url).query))
    b2 = detect_provider(url) == "b2proxy"
    if b2 and isinstance(payload, dict) and str(payload.get("code", 200)) != "200":
        raise ValueError("b2proxy API 返回业务错误，请检查套餐和 IP 白名单")
    separator = query.get("split", "") if b2 else ""
    for escaped, actual in ((r"\r", "\r"), (r"\n", "\n"), (r"\t", "\t")):
        separator = separator.replace(escaped, actual)
    scheme = query.get("proto", "http").lower() if b2 else "auto"
    if scheme == "socks5":
        scheme = "socks5h"
    proxies = parse_proxy_api_response(payload, default_scheme=scheme, separator=separator)
    if not proxies:
        raise ValueError("代理 API 返回为空或代理格式无法识别")
    return proxies


def _api_proxy_identity(value: str) -> str:
    """Compare normalized endpoints, including the two SOCKS5 DNS variants."""
    parsed = urlsplit(str(value or "").strip())
    scheme = "socks5h" if parsed.scheme.lower() == "socks5" else parsed.scheme.lower()
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def fetch_available_proxy_api(
    region: str,
    *,
    api_url: str | None = None,
    timeout: float = 8.0,
    excluded_proxies: set[str] | None = None,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """Shared, bounded API acquisition and optional health check for all tasks.

    Each attempt fetches a fresh batch and checks one eligible endpoint. The
    provider's batch size never increases the health-check attempt budget.
    With health checks disabled, retain all eligible routes for liveness tasks.
    """
    from config import proxy as proxy_cfg

    try:
        attempts = int(proxy_cfg.PROXY_API_MAX_ATTEMPTS)
    except (TypeError, ValueError, OverflowError):
        attempts = 3
    attempts = max(1, min(attempts, 20))
    health_enabled = bool(proxy_cfg.PROXY_HEALTH_CHECK_BEFORE_REGISTRATION)
    excluded = {_api_proxy_identity(value) for value in (excluded_proxies or set())}
    emit = log if log is not None else logger.info
    last_error = "代理 API 返回空代理"
    if api_url is None and not any(item["enabled"] for item in proxy_cfg.proxy_api_entries()):
        raise ValueError("请在配置 → 代理池 → API管理中至少选中一个 API")
    for attempt in range(1, attempts + 1):
        emit(f"API代理尝试 {attempt}/{attempts}：获取动态出口")
        try:
            request_url = api_url if api_url is not None else proxy_cfg.build_proxy_api_request_url(region=region)
            provider = detect_provider(request_url)
            proxies = fetch_proxy_api(region, api_url=request_url, timeout=timeout)
            emit(f"API代理尝试 {attempt}/{attempts}：{provider} 提取成功，返回 {len(proxies)} 个出口，开始健康检查" if health_enabled else f"API代理尝试 {attempt}/{attempts}：{provider} 提取成功，返回 {len(proxies)} 个出口")
        except Exception as exc:
            # Request exceptions may contain API credentials in their URL.
            last_error = f"代理 API 提取失败（{safe_transport_error(exc)}）"
        else:
            candidates = [
                str(value).strip() for value in proxies
                if str(value or "").strip() and _api_proxy_identity(value) not in excluded
            ]
            if not candidates:
                last_error = "代理 API 重复返回已隔离出口" if proxies else "代理 API 返回空代理"
            elif not health_enabled:
                emit(f"API代理尝试 {attempt}/{attempts}：已获取动态出口（健康检查已关闭）")
                return candidates
            else:
                from core.proxy_test import choose_healthy_proxy

                try:
                    selection = choose_healthy_proxy(
                        [candidates[0]],
                        timeout=proxy_cfg.PROXY_WARMUP_TIMEOUT,
                        health_url=proxy_cfg.PROXY_WARMUP_HEALTH_URL,
                        reputation_url=proxy_cfg.PROXY_WARMUP_REPUTATION_URL,
                        anonymity_url=proxy_cfg.PROXY_WARMUP_ANONYMITY_URL,
                        min_clean_score=proxy_cfg.PROXY_WARMUP_MIN_CLEAN_SCORE,
                        max_latency=proxy_cfg.PROXY_WARMUP_MAX_LATENCY,
                        exit_samples=proxy_cfg.PROXY_WARMUP_EXIT_SAMPLES,
                    )
                except Exception as exc:
                    last_error = f"API代理健康检查异常（{type(exc).__name__}）"
                else:
                    if selection.get("ok") and selection.get("proxy_url"):
                        emit(f"API代理尝试 {attempt}/{attempts}：健康检查通过")
                        return [str(selection["proxy_url"])]
                    # choose_healthy_proxy returns result=None on failure; the
                    # actual health reason is in the last checked entry.
                    checked = selection.get("checked") or []
                    result = selection.get("result") or (checked[-1] if checked else {})
                    last_error = str(result.get("reason") or "API代理健康检查未通过")
        suffix = "，重新获取代理" if attempt < attempts else "，尝试次数已用尽"
        emit(f"API代理尝试 {attempt}/{attempts} 失败：{last_error}{suffix}")
    raise RuntimeError(f"代理 API 未能获取可用出口（已尝试 {attempts}/{attempts} 次）：{last_error}")
