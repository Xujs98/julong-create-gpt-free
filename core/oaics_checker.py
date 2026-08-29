# -*- coding: utf-8 -*-
"""OAICS 结账会话检测器。"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable


OAICS_CHECK_URL = "https://tools.oai9.com/api/trial/check"
OAICS_SITE_ORIGIN = "https://tools.oai9.com"


SUPPORTED_SESSION_PREFIXES = ("oaics_", "cs_")
_SESSION_PATTERN = re.compile(r"(?<![A-Za-z0-9_])(oaics_|cs_)[A-Za-z0-9_-]+")


def _walk_values(value: Any, *, depth: int = 0) -> Iterable[Any]:
    if depth > 10:
        return
    if isinstance(value, dict):
        for item in value.values():
            yield item
            if isinstance(item, (dict, list, tuple)):
                yield from _walk_values(item, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield item
            if isinstance(item, (dict, list, tuple)):
                yield from _walk_values(item, depth=depth + 1)


def extract_checkout_session_id(payload: Any) -> str:
    """从常见字段或嵌套 URL 中提取 OAICS/Stripe 结账会话 ID。"""
    if not isinstance(payload, dict):
        raise ValueError("checkout response must be a JSON object")
    candidates = [
        payload.get("checkout_session_id"),
        payload.get("session_id"),
        payload.get("id"),
        *_walk_values(payload),
    ]
    for value in candidates:
        text = str(value or "").strip()
        if text.startswith(SUPPORTED_SESSION_PREFIXES):
            return text
        match = _SESSION_PATTERN.search(text)
        if match:
            return match.group(0)
    raise ValueError("checkout response did not contain a supported oaics_/cs_ session id")


def detect_oaics_checkout(payload: Any, *, billing_country: str = "") -> dict[str, Any]:
    """根据结账会话前缀判断账号是否具备 OAICS 资格。"""
    session_id = extract_checkout_session_id(payload)
    is_oaics = session_id.startswith("oaics_")
    processor = str(payload.get("processor_entity") or "").strip()
    if not processor:
        processor = "openai_llc" if str(billing_country).upper() == "US" else "openai_ie"
    return {
        "is_oaics": is_oaics,
        "session_kind": "oaics" if is_oaics else "stripe_cs",
        "processor_entity": processor,
    }


def _country_result(item: Any) -> dict[str, Any] | None:
    """Normalize one ``tools.oai9.com`` country result."""
    if not isinstance(item, dict):
        return None
    country = str(item.get("country") or item.get("country_code") or "").strip().upper()
    country_name = str(item.get("country_name") or item.get("name") or "").strip()
    status = str(item.get("status") or "").strip().lower()
    eligible = item.get("eligible")
    if status in {"eligible", "qualified"}:
        status = "eligible"
        eligible = True
    elif status in {"not_eligible", "ineligible", "unqualified"}:
        status = "not_eligible"
        eligible = False
    elif status in {"failed", "error", "timeout"}:
        status = "failed"
        eligible = None
    else:
        return None
    if not re.fullmatch(r"[A-Z]{2}", country) or not country_name:
        return None
    message = str(item.get("message") or item.get("error") or "").strip()
    return {
        "country": country,
        "country_name": country_name,
        "status": status,
        "eligible": eligible,
        "message": message,
    }


def parse_oaics_protocol_response(payload: Any) -> dict[str, Any]:
    """Parse the country-result response returned by the OAICS website API."""
    if not isinstance(payload, dict):
        raise ValueError("OAICS protocol response must be a JSON object")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list) or not raw_results:
        raise ValueError("OAICS protocol response did not contain results")
    results = []
    for item in raw_results:
        normalized = _country_result(item)
        if normalized is None:
            raise ValueError("OAICS protocol response contained an invalid country result")
        results.append(normalized)
    return {
        "oaics_country_results": results,
        "oaics_eligible": any(item["status"] == "eligible" for item in results),
        "oaics_query_count": payload.get("query_count"),
        "oaics_session_kind": "website_protocol",
        "oaics_processor_entity": "tools.oai9.com",
    }


def check_oaics_protocol(
    session: Any,
    access_token: str,
    *,
    timeout: float = 15.0,
    turnstile_token: str | None = None,
) -> dict[str, Any]:
    """Call the public OAICS country-check endpoint using an existing session.

    The website accepts JSON ``{"access_token": "..."}`` and optionally an
    ``X-Turnstile-Token`` header.  A caller may pass a BrowserSession or any
    compatible object exposing ``post``; this keeps proxy and cookie handling
    in the registration pipeline while making the protocol independently
    callable in tests and integrations.
    """
    token = str(access_token or "").strip()
    if not token:
        raise ValueError("access token is empty")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": OAICS_SITE_ORIGIN,
        "Referer": f"{OAICS_SITE_ORIGIN}/",
    }
    if turnstile_token:
        headers["X-Turnstile-Token"] = str(turnstile_token).strip()
    response = session.post(
        OAICS_CHECK_URL,
        headers=headers,
        json={"access_token": token},
        timeout=timeout,
        allow_redirects=False,
    )
    status_code = int(getattr(response, "status_code", 0) or 0)
    if not 200 <= status_code < 300:
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("detail") or body.get("error") or "").strip()
        except Exception:
            detail = ""
        suffix = f": {detail[:180]}" if detail else ""
        raise RuntimeError(f"OAICS protocol HTTP {status_code}{suffix}")
    try:
        payload = response.json()
    except Exception as exc:
        raise ValueError(f"OAICS protocol returned invalid JSON: {type(exc).__name__}") from exc
    return parse_oaics_protocol_response(payload)


def query_oaics_countries(
    access_token: str,
    *,
    proxy: str | None = None,
    timeout: float = 15.0,
    turnstile_token: str | None = None,
    impersonate: str = "chrome",
) -> dict[str, Any]:
    """Convenience wrapper for callers that only have a token and proxy URL."""
    from curl_cffi.requests import Session

    client = Session(impersonate=impersonate)
    selected_proxy = str(proxy or "").strip()
    if selected_proxy:
        client.proxies = {"http": selected_proxy, "https": selected_proxy}
    try:
        result = check_oaics_protocol(
            client,
            access_token,
            timeout=timeout,
            turnstile_token=turnstile_token,
        )
        result["oaics_check_status"] = "success"
        result["oaics_check_http_status"] = 200
        result["oaics_check_error"] = None
        result["oaics_checked_at"] = oaics_result_timestamp()
        return result
    except Exception as exc:
        return {
            "oaics_check_status": "failed",
            "oaics_check_http_status": None,
            "oaics_check_error": f"{type(exc).__name__}: {str(exc)[:180]}",
            "oaics_checked_at": oaics_result_timestamp(),
            "oaics_country_results": [],
        }
    finally:
        try:
            client.close()
        except Exception:
            pass


def oaics_result_timestamp() -> str:
    """Return a compact local timestamp for persisted protocol results."""
    return datetime.now().isoformat(timespec="seconds")
