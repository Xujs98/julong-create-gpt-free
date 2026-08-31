# -*- coding: utf-8 -*-
"""查活代理 API：按账号地区获取短时代理并统一解析返回格式。"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests

from core.proxy_utils import normalize_proxy_url


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
    query.insert(0, ("region", value))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _values(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, (list, tuple, set)):
        out: list[Any] = []
        for item in payload:
            out.extend(_values(item))
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
                out.extend(_values(payload.get(key)))
        return out
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return [line.strip() for line in text.replace(",", "\n").splitlines() if line.strip()]
        return _values(decoded)
    return [str(payload)]


def parse_proxy_api_response(payload: Any) -> list[str]:
    """提取并标准化 API 返回的代理地址，去重后保留原顺序。"""
    result: list[str] = []
    seen: set[str] = set()
    for raw in _values(payload):
        value = str(raw or "").strip().strip('"').strip("'")
        if not value or "***" in value:
            continue
        try:
            normalized = normalize_proxy_url(value, default_scheme="auto")
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
    proxies = parse_proxy_api_response(payload)
    if not proxies:
        raise ValueError("代理 API 返回为空或代理格式无法识别")
    return proxies
