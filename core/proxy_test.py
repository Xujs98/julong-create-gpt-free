# -*- coding: utf-8 -*-
"""代理连通性与出口位置测试。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import random
import re
import threading
import time
from typing import Callable
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
_ANONYMITY_LEAK_HEADERS = (
    "via",
    "forwarded",
    "x-forwarded-for",
    "x-real-ip",
    "client-ip",
    "true-client-ip",
    "proxy-connection",
)
_HIGH_RISK_REPUTATION_FLAGS = (
    "is_tor",
    "is_bogon",
    "is_abuser",
    "is_abuse",
    "is_blacklisted",
    "is_spam",
    "is_bot",
)
_NETWORK_REPUTATION_FLAGS = (
    "is_proxy",
    "is_vpn",
    "is_datacenter",
    "is_hosting",
    "is_cloud_provider",
)


def _geo_error_retryable(exc: Exception) -> bool:
    """Return True for transient transport failures worth retrying once."""
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if any(token in name for token in ("timeout", "proxyerror", "connectionerror", "connecterror")):
        return True
    return any(token in message for token in (
        "timed out", "connection timed out", "connect tunnel failed",
        "could not connect", "connection reset", "operation timed out",
        "curl: (28)", "curl: (35)", "curl: (56)",
    ))


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
    retry_budget = max(0, min(2, int(getattr(_browser_cfg, "IP_GEO_RETRIES", 1) or 0)))
    for candidate in candidates:
        session = Session(impersonate=getattr(_browser_cfg, "IMPERSONATE", "chrome"))
        session.proxies = {"http": candidate, "https": candidate}
        for endpoint in endpoints:
            endpoint_retried = False
            while True:
                try:
                    response = session.get(endpoint, headers=headers, timeout=timeout)
                    if response.status_code != 200:
                        errors.append(f"{endpoint}: HTTP {response.status_code}")
                        break
                    payload = response.json()
                    if not isinstance(payload, dict):
                        errors.append(f"{endpoint}: 响应不是 JSON 对象")
                        break
                    geo = _normalize_geo(payload)
                    if not geo.get("ip"):
                        errors.append(f"{endpoint}: 响应缺少 IP")
                        break
                    return {
                        "ok": True,
                        "proxy": _masked_proxy(candidate),
                        "endpoint": endpoint,
                        "dns_mode": "proxy" if urlsplit(candidate).scheme.lower() == "socks5h" else "default",
                        **geo,
                    }
                except Exception as exc:
                    errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
                    if not endpoint_retried and retry_budget > 0 and _geo_error_retryable(exc):
                        endpoint_retried = True
                        retry_budget -= 1
                        time.sleep(0.2)
                        continue
                    break
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


def _request_json(session: Session, url: str, *, timeout: float) -> tuple[dict, float]:
    """请求 JSON 接口，并返回对象与耗时。"""
    started = time.monotonic()
    response = session.get(
        url,
        headers={"Accept": "application/json", "User-Agent": getattr(_browser_cfg, "USER_AGENT", "Mozilla/5.0")},
        timeout=timeout,
    )
    elapsed = max(0.0, time.monotonic() - started)
    if int(response.status_code or 0) != 200:
        raise ProxyTestError(f"HTTP {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ProxyTestError("响应不是 JSON 对象")
    return payload, elapsed


def _sample_proxy_exits(proxy_url: str, *, timeout: float, samples: int) -> tuple[dict, list[str]]:
    """用彼此独立的新连接采样出口，识别按连接轮换的代理入口。"""
    sample_count = max(1, min(5, int(samples or 1)))
    first_result: dict | None = None
    exits: list[str] = []
    for _ in range(sample_count):
        result = test_proxy(proxy_url, timeout=timeout)
        if first_result is None:
            first_result = result
        exit_ip = str(result.get("ip") or "").strip()
        if not exit_ip:
            raise ProxyTestError("出口稳定性采样未返回 IP")
        exits.append(exit_ip)
    return first_result or {}, exits


def _walk_reputation_values(payload: dict) -> dict[str, bool]:
    """递归收集常见 IP 信誉布尔字段，兼容多种返回结构。"""
    found: dict[str, bool] = {}

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key or "").strip().lower().replace("-", "_")
                if isinstance(item, bool):
                    found[normalized] = item
                elif isinstance(item, (dict, list, tuple)):
                    walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(payload)
    aliases = {
        "proxy": "is_proxy",
        "vpn": "is_vpn",
        "tor": "is_tor",
        "hosting": "is_hosting",
        "datacenter": "is_datacenter",
        "bogon": "is_bogon",
        "abuser": "is_abuser",
        "abuse": "is_abuse",
    }
    for alias, canonical in aliases.items():
        if alias in found and canonical not in found:
            found[canonical] = found[alias]
    return found


def _reputation_assessment(payload: dict) -> dict:
    """把信誉接口结果归一化为风险信号与分数扣减。"""
    flags = _walk_reputation_values(payload)
    high_risk = [name for name in _HIGH_RISK_REPUTATION_FLAGS if flags.get(name) is True]
    network_risk = [name for name in _NETWORK_REPUTATION_FLAGS if flags.get(name) is True]
    known = any(name in flags for name in (*_HIGH_RISK_REPUTATION_FLAGS, *_NETWORK_REPUTATION_FLAGS))
    penalty = min(60, len(high_risk) * 35 + len(network_risk) * 12)
    return {
        "known": known,
        "high_risk": high_risk,
        "network_risk": network_risk,
        "penalty": penalty,
        "clean": not high_risk and not network_risk,
    }


def _anonymity_assessment(payload: dict, expected_ip: str) -> dict:
    """检查回显地址与代理头，识别透明代理或来源泄漏。"""
    headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    leaks = sorted(name for name in _ANONYMITY_LEAK_HEADERS if lowered.get(name))
    origin = str(payload.get("origin") or payload.get("ip") or "").strip()
    origin_ips = [part.strip().split(":", 1)[0] for part in re.split(r"[,\s]+", origin) if part.strip()]
    origin_verified = bool(origin_ips)
    exit_consistent = bool(origin_verified and (not expected_ip or expected_ip in origin_ips))
    return {
        "origin": origin,
        "origin_verified": origin_verified,
        "leak_headers": leaks,
        "exit_consistent": exit_consistent,
        "anonymous": origin_verified and not leaks and exit_consistent,
    }


def test_proxy_health(
    proxy_url: str,
    timeout: float | None = None,
    *,
    health_url: str | None = None,
    reputation_url: str | None = None,
    anonymity_url: str | None = None,
    min_clean_score: int | None = None,
    max_latency: float | None = None,
    exit_samples: int | None = None,
) -> dict:
    """综合检查出口稳定性、IP 信誉、匿名性、延迟和业务入口挑战。"""
    timeout = max(2.0, min(45.0, float(timeout or 12.0)))
    endpoint = str(health_url or "https://chatgpt.com/auth/login").strip()
    reputation_endpoint = str(reputation_url if reputation_url is not None else "https://us.ipapi.is/?q={ip}").strip()
    anonymity_endpoint = str(anonymity_url if anonymity_url is not None else "https://echo.free.beeceptor.com,https://httpbin.io/get").strip()
    anonymity_endpoints = [item.strip() for item in re.split(r"[,\r\n]+", anonymity_endpoint) if item.strip()]
    clean_threshold = max(0, min(100, int(min_clean_score if min_clean_score is not None else 80)))
    latency_limit = max(0.0, float(max_latency if max_latency is not None else 8.0))
    sample_count = max(1, min(5, int(exit_samples if exit_samples is not None else 3)))
    geo, sampled_exit_ips = _sample_proxy_exits(proxy_url, timeout=timeout, samples=sample_count)
    stable_exit = len(set(sampled_exit_ips)) == 1
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
            steps = [
                {"name": "出口 IP 检查", "ok": True, "detail": geo.get("ip", "") or "已获取"},
                {
                    "name": "出口稳定性检查",
                    "ok": stable_exit,
                    "detail": (
                        f"{sample_count}/{sample_count} 次出口一致"
                        if stable_exit
                        else f"发现 {len(set(sampled_exit_ips))} 个不同出口，按连接轮换"
                    ),
                },
            ]
            reputation = {"known": False, "high_risk": [], "network_risk": [], "penalty": 0, "clean": not reputation_endpoint}
            reputation_verified = not reputation_endpoint
            if reputation_endpoint:
                try:
                    reputation_payload, _ = _request_json(
                        session,
                        reputation_endpoint.replace("{ip}", str(geo.get("ip") or "")),
                        timeout=timeout,
                    )
                    reputation = _reputation_assessment(reputation_payload)
                    reputation_verified = bool(reputation["known"])
                    risks = [*reputation["high_risk"], *reputation["network_risk"]]
                    detail = "未发现代理/VPN/Tor/机房/滥用风险" if not risks else ", ".join(risks)
                    if not reputation["known"]:
                        detail = "接口未返回可识别的信誉字段"
                    steps.append({"name": "IP 信誉检查", "ok": reputation["clean"], "detail": detail})
                except Exception as exc:
                    reputation = {"known": False, "high_risk": [], "network_risk": [], "penalty": 10, "clean": False, "error": type(exc).__name__}
                    steps.append({"name": "IP 信誉检查", "ok": False, "detail": "信誉接口请求失败"})

            anonymity = {"origin": "", "leak_headers": [], "exit_consistent": True, "anonymous": True}
            anonymity_verified = not anonymity_endpoints
            anonymity_errors = []
            for anonymity_check_url in anonymity_endpoints:
                try:
                    anonymity_payload, _ = _request_json(session, anonymity_check_url, timeout=timeout)
                    anonymity = _anonymity_assessment(anonymity_payload, str(geo.get("ip") or ""))
                    anonymity["endpoint"] = anonymity_check_url
                    anonymity_verified = True
                    problems = list(anonymity["leak_headers"])
                    if not anonymity["exit_consistent"]:
                        problems.append("回显出口与 GeoIP 不一致")
                    steps.append({
                        "name": "代理匿名性检查",
                        "ok": anonymity["anonymous"],
                        "detail": "未发现来源泄漏" if not problems else ", ".join(problems),
                    })
                    break
                except Exception as exc:
                    anonymity_errors.append(type(exc).__name__)
            if anonymity_endpoints and not anonymity_verified:
                anonymity = {"origin": "", "leak_headers": [], "exit_consistent": False, "anonymous": False, "error": ",".join(anonymity_errors[-3:])}
                steps.append({"name": "代理匿名性检查", "ok": False, "detail": f"{len(anonymity_endpoints)} 个匿名性接口均请求失败"})

            started = time.monotonic()
            response = session.get(endpoint, headers=headers, timeout=timeout, allow_redirects=True)
            latency = max(0.0, time.monotonic() - started)
            challenge, markers = _challenge_evidence(response)
            status = int(response.status_code or 0)
            final_url = str(getattr(response, "url", "") or endpoint)
            business_ok = 200 <= status < 400
            latency_ok = not latency_limit or latency <= latency_limit
            steps.extend([
                {"name": "业务入口可达性", "ok": business_ok, "detail": f"HTTP {status}"},
                {"name": "出口延迟检查", "ok": latency_ok, "detail": f"{latency:.2f}s / 上限 {latency_limit:.2f}s"},
                {"name": "Cloudflare 挑战识别", "ok": not challenge, "detail": ", ".join(markers) if markers else "未发现挑战特征"},
            ])
            score = 100
            if not stable_exit:
                score -= 100
            score -= int(reputation.get("penalty") or 0)
            if not anonymity.get("anonymous"):
                score -= 25
            if not business_ok:
                score -= 35
            if challenge:
                score -= 45
            if not latency_ok:
                score -= 15
            score = max(0, min(100, score))
            verification_complete = reputation_verified and anonymity_verified
            critical_ok = (
                verification_complete
                and stable_exit
                and reputation.get("clean") is True
                and anonymity.get("anonymous") is True
                and business_ok
                and latency_ok
                and not challenge
            )
            healthy = critical_ok and score >= clean_threshold
            reasons = []
            if not stable_exit:
                reasons.append("rotating_exit")
            if reputation.get("high_risk") or reputation.get("network_risk"):
                reasons.append("ip_reputation_risk")
            if not anonymity.get("anonymous"):
                reasons.append("anonymity_leak")
            if not business_ok:
                reasons.append(f"http_{status}")
            if not latency_ok:
                reasons.append("high_latency")
            if challenge:
                reasons.append("cloudflare_challenge")
            if score < clean_threshold:
                reasons.append("clean_score_below_threshold")
            if not verification_complete:
                reasons.append("cleanliness_check_incomplete")
            definitive_dirty = bool(
                not stable_exit
                or reputation.get("high_risk")
                or reputation.get("network_risk")
                or (anonymity_verified and not anonymity.get("anonymous"))
                or not business_ok
                or not latency_ok
                or challenge
            )
            steps.append({"name": "综合干净度评分", "ok": healthy, "detail": f"{score}/100，门槛 {clean_threshold}"})
            return {
                "ok": healthy,
                "healthy": healthy,
                "clean": healthy,
                "clean_score": score,
                "clean_threshold": clean_threshold,
                "verification_complete": verification_complete,
                "inconclusive": not verification_complete and not definitive_dirty,
                "removable": definitive_dirty,
                "proxy": _masked_proxy(candidate),
                "ip": geo.get("ip", ""),
                "country": geo.get("country", ""),
                "country_code": geo.get("country_code", ""),
                "status": status,
                "latency_seconds": round(latency, 3),
                "health_url": endpoint,
                "final_url": final_url,
                "challenge_detected": challenge,
                "challenge_markers": markers,
                "exit_samples": sampled_exit_ips,
                "stable_exit": stable_exit,
                "reputation": reputation,
                "anonymity": anonymity,
                "reason": "clean" if healthy else ",".join(dict.fromkeys(reasons)) or "not_clean",
                "steps": steps,
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
    reputation_url: str | None = None,
    anonymity_url: str | None = None,
    min_clean_score: int | None = None,
    max_latency: float | None = None,
    exit_samples: int | None = None,
    max_workers: int = 4,
    recheck_clean: bool = False,
    progress_callback: Callable[[dict], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict:
    """并发预热代理池；可对首轮健康出口再完整复查一次。"""
    if recheck_clean:
        # 先不截断首轮结果，确保复查失败时可从其余首轮健康出口中补足目标数量。
        raw_values = [str(item or "").strip() for item in (proxy_urls or [])]
        unique_count = len(dict.fromkeys(value for value in raw_values if value))

        def _initial_progress(event: dict) -> None:
            if not progress_callback:
                return
            payload = dict(event)
            payload.update({"phase": "initial", "completed": int(event.get("completed") or 0), "total": unique_count})
            progress_callback(payload)

        first = warmup_proxy_pool(
            proxy_urls,
            target_clean=0,
            timeout=timeout,
            health_url=health_url,
            reputation_url=reputation_url,
            anonymity_url=anonymity_url,
            min_clean_score=min_clean_score,
            max_latency=max_latency,
            exit_samples=exit_samples,
            max_workers=max_workers,
            recheck_clean=False,
            progress_callback=_initial_progress if progress_callback else None,
            cancel_event=cancel_event,
        )
        candidates = list(first.get("healthy_proxy_urls") or [])
        if cancel_event and cancel_event.is_set():
            raise ProxyTestError("预热已终止")
        if not candidates:
            first.update({
                "recheck_enabled": True,
                "recheck_candidate_count": 0,
                "recheck_checked_total": 0,
                "recheck_healthy_total": 0,
                "recheck_results": [],
                "recheck_failures": [],
                "checked_operations_total": first.get("checked_total", 0),
            })
            return first

        def _recheck_progress(event: dict) -> None:
            if not progress_callback:
                return
            payload = dict(event)
            payload.update({
                "phase": "recheck",
                "completed": unique_count + int(event.get("completed") or 0),
                "total": unique_count + len(candidates),
                "phase_completed": int(event.get("completed") or 0),
                "phase_total": len(candidates),
            })
            progress_callback(payload)

        second = warmup_proxy_pool(
            candidates,
            target_clean=target_clean,
            timeout=timeout,
            health_url=health_url,
            reputation_url=reputation_url,
            anonymity_url=anonymity_url,
            min_clean_score=min_clean_score,
            max_latency=max_latency,
            exit_samples=exit_samples,
            max_workers=max_workers,
            recheck_clean=False,
            progress_callback=_recheck_progress if progress_callback else None,
            cancel_event=cancel_event,
        )
        dirty_urls = list(dict.fromkeys([
            *(first.get("unhealthy_proxy_urls") or []),
            *(second.get("unhealthy_proxy_urls") or []),
        ]))
        inconclusive_urls = list(dict.fromkeys([
            *(first.get("inconclusive_proxy_urls") or []),
            *(second.get("inconclusive_proxy_urls") or []),
        ]))
        combined_failures = [*(first.get("failures") or []), *(second.get("failures") or [])]
        first.update({
            "ok": bool(second.get("clean_proxy_urls")),
            "checked_total": int(first.get("checked_total") or 0) + int(second.get("checked_total") or 0),
            "checked_operations_total": int(first.get("checked_total") or 0) + int(second.get("checked_total") or 0),
            "available": second.get("available", 0),
            "healthy_total": second.get("healthy_total", 0),
            "clean": second.get("clean", 0),
            "selected_clean_count": second.get("selected_clean_count", 0),
            "failed": len(combined_failures),
            "dirty": len(dirty_urls),
            "inconclusive": len(inconclusive_urls),
            "target_clean": second.get("target_clean", int(target_clean or 0)),
            "failures": combined_failures,
            "clean_proxy_urls": second.get("clean_proxy_urls") or [],
            "healthy_proxy_urls": second.get("healthy_proxy_urls") or [],
            "unhealthy_proxy_urls": dirty_urls,
            "inconclusive_proxy_urls": inconclusive_urls,
            "recheck_enabled": True,
            "recheck_candidate_count": len(candidates),
            "recheck_checked_total": second.get("checked_total", 0),
            "recheck_healthy_total": second.get("healthy_total", 0),
            "recheck_results": second.get("results") or [],
            "recheck_failures": second.get("failures") or [],
        })
        return first
    raw_values = [str(item or "").strip() for item in (proxy_urls or [])]
    input_count = sum(1 for value in raw_values if value)
    proxies = []
    seen = set()
    for value in raw_values:
        if value and value not in seen:
            proxies.append(value)
            seen.add(value)
    if not proxies:
        raise ProxyTestError("代理池为空，请先配置至少一个代理")
    workers = max(1, min(int(max_workers or 4), len(proxies), 16))
    results: list[dict | None] = [None] * len(proxies)
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="proxy-warmup")
    cancelled = False
    try:
        futures = {
            executor.submit(
                test_proxy_health,
                proxy,
                timeout,
                health_url=health_url,
                reputation_url=reputation_url,
                anonymity_url=anonymity_url,
                min_clean_score=min_clean_score,
                max_latency=max_latency,
                exit_samples=exit_samples,
            ): (index, proxy)
            for index, proxy in enumerate(proxies)
        }
        for future in as_completed(futures):
            if cancel_event and cancel_event.is_set():
                cancelled = True
                for pending in futures:
                    if pending is not future:
                        pending.cancel()
                break
            index, proxy = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                results[index] = {
                    "ok": False,
                    "healthy": False,
                    "removable": True,
                    "inconclusive": False,
                    "proxy": _masked_proxy(proxy),
                    "reason": f"{type(exc).__name__}: 代理多维检查失败",
                    "challenge_detected": False,
                    "steps": [{"name": "代理多维检查", "ok": False, "detail": "出口连接或检测请求失败"}],
                }
            if progress_callback and not (cancel_event and cancel_event.is_set()):
                try:
                    progress_callback({
                        "index": index,
                        "completed": sum(1 for item in results if isinstance(item, dict)),
                        "total": len(proxies),
                        "result": results[index],
                    })
                except Exception:
                    # 日志回调属于旁路能力，不能影响代理检查本身。
                    pass
    finally:
        if cancel_event and cancel_event.is_set():
            cancelled = True
        executor.shutdown(wait=not cancelled, cancel_futures=cancelled)
    if cancelled:
        raise ProxyTestError("预热已终止")
    # 所有唯一代理都已提交检查并等待完成；目标数量只用于从完整结果中
    # 选择干净出口，不会提前停止检查。
    healthy_indexes_all = [i for i, result in enumerate(results) if isinstance(result, dict) and result.get("healthy")]
    healthy_indexes = list(healthy_indexes_all)
    target = int(target_clean or 0)
    if target > 0:
        healthy_indexes = healthy_indexes[:target]
    clean_proxy_urls = [proxies[i] for i in healthy_indexes]
    failures = [result for result in results if isinstance(result, dict) and not result.get("healthy")]
    dirty_results = [result for result in failures if result.get("removable", True)]
    inconclusive_results = [result for result in failures if not result.get("removable", True)]
    return {
        "ok": bool(clean_proxy_urls),
        "checked_all": True,
        "checked_total": len(proxies),
        "input_count": input_count,
        "duplicate_count": max(0, input_count - len(proxies)),
        "total": len(proxies),
        "available": len(healthy_indexes_all),
        "healthy_total": len(healthy_indexes_all),
        "clean": len(clean_proxy_urls),
        "selected_clean_count": len(clean_proxy_urls),
        "failed": len(failures),
        "dirty": len(dirty_results),
        "inconclusive": len(inconclusive_results),
        "target_clean": target,
        "results": [result for result in results if isinstance(result, dict)],
        "failures": failures,
        "clean_proxy_urls": clean_proxy_urls,
        "healthy_proxy_urls": [proxies[i] for i in healthy_indexes_all],
        "unhealthy_proxy_urls": [proxies[i] for i, result in enumerate(results) if isinstance(result, dict) and not result.get("healthy") and result.get("removable", True)],
        "inconclusive_proxy_urls": [proxies[i] for i, result in enumerate(results) if isinstance(result, dict) and not result.get("healthy") and not result.get("removable", True)],
        "recheck_enabled": False,
        "recheck_candidate_count": 0,
        "recheck_checked_total": 0,
        "recheck_healthy_total": 0,
        "recheck_results": [],
        "recheck_failures": [],
        "checked_operations_total": len(proxies),
    }


def choose_healthy_proxy(
    proxy_urls: list[str] | tuple[str, ...] | None,
    *,
    preferred: str | None = None,
    timeout: float | None = None,
    health_url: str | None = None,
    reputation_url: str | None = None,
    anonymity_url: str | None = None,
    min_clean_score: int | None = None,
    max_latency: float | None = None,
    exit_samples: int | None = None,
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
            result = test_proxy_health(
                proxy,
                timeout=timeout,
                health_url=health_url,
                reputation_url=reputation_url,
                anonymity_url=anonymity_url,
                min_clean_score=min_clean_score,
                max_latency=max_latency,
                exit_samples=exit_samples,
            )
        except Exception as exc:
            result = {"ok": False, "healthy": False, "removable": True, "proxy": _masked_proxy(proxy), "reason": f"{type(exc).__name__}: 代理多维检查失败"}
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
