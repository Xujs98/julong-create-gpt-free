# -*- coding: utf-8 -*-
"""代理连通性与出口位置测试。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import random
import re
import threading
from urllib.parse import urlsplit, urlunsplit

from curl_cffi.requests import Session

from config import browser as _browser_cfg
from core.proxy_utils import masked_proxy_url, normalize_proxy_url

__test__ = False


class ProxyTestError(RuntimeError):
    """代理测试请求失败。"""


_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "cf-chl-",
    "/cdn-cgi/challenge-platform/",
    "challenge-form",
    "cf-turnstile",
    "turnstile-widget",
    "cf-mitigated",
)
_PROXY_POOL_WRITE_LOCK = threading.RLock()


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


def test_proxy_pool(
    proxy_urls: list[str] | tuple[str, ...] | None,
    timeout: float | None = None,
    *,
    max_workers: int = 8,
) -> dict:
    """并发检查代理池全部出口，返回可用代理和应移除的失败项。"""
    proxies = []
    seen = set()
    for item in proxy_urls or []:
        value = str(item or "").strip()
        if value and value not in seen:
            proxies.append(value)
            seen.add(value)
    if not proxies:
        raise ProxyTestError("代理池为空，请先配置至少一个代理")

    workers = max(1, min(int(max_workers or 8), len(proxies), 16))
    results: list[dict | None] = [None] * len(proxies)
    failures: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="proxy-preflight") as executor:
        futures = {
            executor.submit(test_proxy, proxy, timeout=timeout): (index, proxy)
            for index, proxy in enumerate(proxies)
        }
        for future in as_completed(futures):
            index, proxy = futures[future]
            masked = masked_proxy_url(proxy)
            try:
                results[index] = future.result()
            except Exception as exc:
                # 返回前统一使用脱敏地址；详细底层错误仅保留类型，避免代理认证信息进入弹窗。
                failures.append({
                    "index": index + 1,
                    "proxy": masked,
                    "error": f"{type(exc).__name__}: 连接测试失败",
                })

    failures.sort(key=lambda item: int(item.get("index") or 0))
    valid_proxy_urls = [
        proxies[index]
        for index, result in enumerate(results)
        if isinstance(result, dict)
    ]

    return {
        "ok": bool(valid_proxy_urls),
        "total": len(proxies),
        "available": len(valid_proxy_urls),
        "failed": len(failures),
        "results": [item for item in results if isinstance(item, dict)],
        "failures": failures,
        # 仅供后端持久化清洗后的代理池，API 返回前会移除此内部字段。
        "valid_proxy_urls": valid_proxy_urls,
    }


def _challenge_evidence(response) -> tuple[bool, list[str]]:
    """从注册入口响应中提取 Cloudflare/挑战页证据。"""
    try:
        body = str(response.text or "")[:400_000].lower()
    except Exception:
        body = ""
    try:
        title = str(response.headers.get("x-title") or "").lower()
    except Exception:
        title = ""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    html_title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    haystack = f"{title}\n{html_title}\n{body}"
    markers = [marker for marker in _CHALLENGE_MARKERS if marker in haystack]
    return bool(markers), markers


def test_proxy_health(
    proxy_url: str,
    timeout: float | None = None,
    *,
    health_url: str | None = None,
) -> dict:
    """检查代理出口并探测注册入口是否直接返回 Cloudflare 挑战页。

    这是预热/注册前选择器使用的轻量 HTTP 健康检查，不执行挑战交互，也
    不把挑战页当作可用出口。返回值包含脱敏代理地址，原始 URL 只用于
    调用方在内存中继续使用。
    """
    timeout = max(2.0, min(45.0, float(timeout or 12.0)))
    endpoint = str(health_url or "https://chatgpt.com/auth/login").strip()
    geo = test_proxy(proxy_url, timeout=timeout)
    normalized = normalize_proxy_url(proxy_url, default_scheme="auto")
    parsed = urlsplit(normalized or "")
    candidates = [normalized]
    if parsed.scheme.lower() == "socks5":
        candidates.insert(0, urlunsplit(("socks5h", parsed.netloc, parsed.path, parsed.query, parsed.fragment)))
    errors: list[str] = []
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "User-Agent": getattr(_browser_cfg, "USER_AGENT", "Mozilla/5.0"),
    }
    for candidate in candidates:
        session = Session(impersonate=getattr(_browser_cfg, "IMPERSONATE", "chrome"))
        session.proxies = {"http": candidate, "https": candidate}
        try:
            response = session.get(endpoint, headers=headers, timeout=timeout, allow_redirects=True)
            challenge, markers = _challenge_evidence(response)
            status = int(response.status_code or 0)
            final_url = str(getattr(response, "url", "") or endpoint)
            if status >= 400:
                return {
                    "ok": False,
                    "healthy": False,
                    "proxy": _masked_proxy(candidate),
                    "ip": geo.get("ip", ""),
                    "status": status,
                    "health_url": endpoint,
                    "final_url": final_url,
                    "challenge_detected": challenge,
                    "challenge_markers": markers,
                    "reason": f"HTTP {status}",
                }
            if challenge:
                return {
                    "ok": False,
                    "healthy": False,
                    "proxy": _masked_proxy(candidate),
                    "ip": geo.get("ip", ""),
                    "status": status,
                    "health_url": endpoint,
                    "final_url": final_url,
                    "challenge_detected": True,
                    "challenge_markers": markers,
                    "reason": "cloudflare_challenge",
                }
            return {
                "ok": True,
                "healthy": True,
                "proxy": _masked_proxy(candidate),
                "ip": geo.get("ip", ""),
                "country": geo.get("country", ""),
                "country_code": geo.get("country_code", ""),
                "status": status,
                "health_url": endpoint,
                "final_url": final_url,
                "challenge_detected": False,
                "challenge_markers": [],
                "reason": "clean",
            }
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")
        finally:
            try:
                session.close()
            except Exception:
                pass
    raise ProxyTestError("代理健康检查失败；" + " | ".join(errors[-3:]))


def warmup_proxy_pool(
    proxy_urls: list[str] | tuple[str, ...] | None,
    *,
    target_clean: int = 0,
    timeout: float | None = None,
    health_url: str | None = None,
    max_workers: int = 4,
) -> dict:
    """并发预热代理池，返回干净出口和挑战/连接失败明细。"""
    proxies = []
    seen = set()
    for item in proxy_urls or []:
        value = str(item or "").strip()
        if value and value not in seen:
            proxies.append(value)
            seen.add(value)
    if not proxies:
        raise ProxyTestError("代理池为空，请先配置至少一个代理")
    workers = max(1, min(int(max_workers or 4), len(proxies), 16))
    results: list[dict | None] = [None] * len(proxies)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="proxy-warmup") as executor:
        futures = {
            executor.submit(test_proxy_health, proxy, timeout, health_url=health_url): (index, proxy)
            for index, proxy in enumerate(proxies)
        }
        for future in as_completed(futures):
            index, proxy = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                results[index] = {
                    "ok": False,
                    "healthy": False,
                    "proxy": _masked_proxy(proxy),
                    "reason": f"{type(exc).__name__}: 连接或挑战检查失败",
                    "challenge_detected": False,
                }
    healthy_indexes_all = [i for i, result in enumerate(results) if isinstance(result, dict) and result.get("healthy")]
    healthy_indexes = list(healthy_indexes_all)
    target = int(target_clean or 0)
    if target > 0:
        healthy_indexes = healthy_indexes[:target]
    clean_proxy_urls = [proxies[i] for i in healthy_indexes]
    failures = [result for result in results if isinstance(result, dict) and not result.get("healthy")]
    return {
        "ok": bool(clean_proxy_urls),
        "total": len(proxies),
        "available": len(healthy_indexes_all),
        "clean": len(clean_proxy_urls),
        "failed": len(failures),
        "target_clean": target,
        "results": [result for result in results if isinstance(result, dict)],
        "failures": failures,
        "clean_proxy_urls": clean_proxy_urls,
        "healthy_proxy_urls": [proxies[i] for i in healthy_indexes_all],
        "unhealthy_proxy_urls": [proxies[i] for i, result in enumerate(results) if isinstance(result, dict) and not result.get("healthy")],
    }


def choose_healthy_proxy(
    proxy_urls: list[str] | tuple[str, ...] | None,
    *,
    preferred: str | None = None,
    timeout: float | None = None,
    health_url: str | None = None,
) -> dict:
    """先检查 preferred，再随机检查其余出口，返回一个健康代理。"""
    candidates = []
    seen = set()
    for value in ([preferred] if preferred else []) + list(proxy_urls or []):
        value = str(value or "").strip()
        if value and value not in seen:
            candidates.append(value)
            seen.add(value)
    if preferred and candidates:
        rest = candidates[1:]
        random.shuffle(rest)
        candidates = [candidates[0], *rest]
    else:
        random.shuffle(candidates)
    checked = []
    for proxy in candidates:
        try:
            result = test_proxy_health(proxy, timeout=timeout, health_url=health_url)
        except Exception as exc:
            result = {"ok": False, "healthy": False, "proxy": _masked_proxy(proxy), "reason": f"{type(exc).__name__}: 连接或挑战检查失败"}
        result["proxy_url"] = proxy
        checked.append(result)
        if result.get("healthy"):
            return {"ok": True, "proxy_url": proxy, "result": result, "checked": checked}
    return {"ok": False, "proxy_url": "", "result": None, "checked": checked}


def persist_proxy_pool(proxy_urls: list[str] | tuple[str, ...] | None) -> list[str]:
    """原子写入代理池并热加载，供预热/任务前检查共用。"""
    values = []
    seen = set()
    for item in proxy_urls or []:
        value = str(item or "").strip()
        if value and value not in seen:
            values.append(value)
            seen.add(value)
    with _PROXY_POOL_WRITE_LOCK:
        from config.env_loader import write_env_values
        write_env_values({"PROXY_POOL": "\n".join(values) if values else "[]"})
        import config as config_package
        config_package.reload_all()
    return values
