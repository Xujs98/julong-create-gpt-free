# -*- coding: utf-8 -*-
"""代理连通性与出口位置测试。"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from curl_cffi.requests import Session

from config import browser as _browser_cfg
from core.proxy_utils import masked_proxy_url, normalize_proxy_url

__test__ = False


class ProxyTestError(RuntimeError):
    """代理测试请求失败。"""


def _masked_proxy(proxy_url: str) -> str:
    """遮蔽代理 URL 中的认证信息，避免接口结果泄漏密码。"""
    return masked_proxy_url(proxy_url)


def _normalize_geo(data: dict) -> dict:
    """兼容 ipinfo、ipapi 和 ipwho.is 的位置字段。"""
    timezone = data.get("timezone")
    if isinstance(timezone, dict):
        timezone = timezone.get("id") or timezone.get("name")
    connection = data.get("connection") if isinstance(data.get("connection"), dict) else {}
    return {
        "ip": data.get("ip") or data.get("query") or "",
        "country": data.get("country_name") or data.get("country") or "",
        "country_code": str(data.get("country_code") or data.get("countryCode") or "").upper(),
        "region": data.get("region") or data.get("regionName") or "",
        "city": data.get("city") or "",
        "timezone": timezone or "",
        "org": data.get("org") or data.get("organization") or connection.get("org") or connection.get("isp") or "",
    }


def test_proxy(proxy_url: str, timeout: float | None = None) -> dict:
    """通过指定代理访问 GeoIP 服务，成功时返回出口 IP 和位置。"""
    try:
        proxy_url = normalize_proxy_url(proxy_url, default_scheme="auto")
    except ValueError as exc:
        raise ProxyTestError(str(exc)) from exc
    if not proxy_url:
        raise ProxyTestError("当前表单中没有可测试的代理")
    timeout = max(1.0, min(30.0, float(timeout or getattr(_browser_cfg, "IP_GEO_TIMEOUT", 6) or 6)))
    endpoints = list(getattr(_browser_cfg, "IP_GEO_ENDPOINTS", []) or [])
    if not endpoints:
        raise ProxyTestError("未配置 IP 地理信息端点")

    # curl_cffi 的 socks5:// 会在本机解析 DNS；部分代理仅接受代理端解析，
    # 因此 SOCKS5 失败时自动用等价 socks5h:// 再试，不改动用户保存的配置。
    candidates = [proxy_url]
    parsed = urlsplit(proxy_url)
    if parsed.scheme.lower() == "socks5":
        remote_dns = urlunsplit(("socks5h", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
        candidates = [remote_dns, proxy_url]
    headers = {"Accept": "application/json", "User-Agent": getattr(_browser_cfg, "USER_AGENT", "Mozilla/5.0")}
    errors = []
    for candidate in candidates:
        session = Session(impersonate=getattr(_browser_cfg, "IMPERSONATE", "chrome"))
        session.proxies = {"http": candidate, "https": candidate}
        for endpoint in endpoints:
            try:
                response = session.get(endpoint, headers=headers, timeout=timeout)
                if response.status_code != 200:
                    errors.append(f"{endpoint}: HTTP {response.status_code}")
                    continue
                payload = response.json()
                if not isinstance(payload, dict):
                    errors.append(f"{endpoint}: 响应不是 JSON 对象")
                    continue
                geo = _normalize_geo(payload)
                if not geo.get("ip"):
                    errors.append(f"{endpoint}: 响应缺少 IP")
                    continue
                return {
                    "ok": True,
                    "proxy": _masked_proxy(candidate),
                    "endpoint": endpoint,
                    "dns_mode": "proxy" if urlsplit(candidate).scheme.lower() == "socks5h" else "default",
                    **geo,
                }
            except Exception as exc:
                errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
    raise ProxyTestError("代理测试失败；" + " | ".join(errors[-3:]))
