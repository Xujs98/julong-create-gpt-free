# -*- coding: utf-8 -*-
"""OAICS 结账会话与各国资格协议工具。"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable


COUNTRY_QUALIFICATION_CHECK_URL = "https://tools.oai9.com/api/trial/check"
COUNTRY_QUALIFICATION_SITE_ORIGIN = "https://tools.oai9.com"
COUNTRY_QUALIFICATION_TURNSTILE_ACTION = "trial_check"
# 旧调用方兼容常量；网站接口实际返回各国资格，不是 OAICS checkout。
OAICS_CHECK_URL = COUNTRY_QUALIFICATION_CHECK_URL
OAICS_SITE_ORIGIN = COUNTRY_QUALIFICATION_SITE_ORIGIN


SUPPORTED_SESSION_PREFIXES = ("oaics_", "cs_")
_SESSION_PATTERN = re.compile(r"(?<![A-Za-z0-9_])(oaics_|cs_)[A-Za-z0-9_-]+")


class CountryQualificationError(RuntimeError):
    """HTTP/protocol error from the country qualification endpoint."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: str = "",
        requires_turnstile: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = str(detail or "")
        self.requires_turnstile = bool(requires_turnstile)


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


def parse_country_qualification_response(payload: Any) -> dict[str, Any]:
    """解析 tools.oai9.com 返回的各国资格结果。"""
    if not isinstance(payload, dict):
        raise ValueError("country qualification response must be a JSON object")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list) or not raw_results:
        raise ValueError("country qualification response did not contain results")
    results = []
    for item in raw_results:
        normalized = _country_result(item)
        if normalized is None:
            raise ValueError("country qualification response contained an invalid country result")
        results.append(normalized)
    return {
        "country_qualification_results": results,
        "country_qualification_eligible": any(item["status"] == "eligible" for item in results),
        "country_qualification_query_count": payload.get("query_count"),
        "country_qualification_source": "tools.oai9.com",
    }


def check_country_qualification_protocol(
    session: Any,
    access_token: str,
    *,
    timeout: float = 15.0,
    turnstile_token: str | None = None,
) -> dict[str, Any]:
    """调用各国资格接口。

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
        "Origin": COUNTRY_QUALIFICATION_SITE_ORIGIN,
        "Referer": f"{COUNTRY_QUALIFICATION_SITE_ORIGIN}/",
        # Keep the request close to the browser request made by gpt-trial.js.
        # The Turnstile token remains the authoritative anti-abuse proof.
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if turnstile_token:
        headers["X-Turnstile-Token"] = str(turnstile_token).strip()
    response = session.post(
        COUNTRY_QUALIFICATION_CHECK_URL,
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
        requires_turnstile = status_code == 403 and any(
            marker in detail.lower()
            for marker in ("turnstile", "安全验证", "验证失败", "captcha")
        )
        message = f"country qualification HTTP {status_code}{suffix}"
        if requires_turnstile:
            message += "；需要有效的 Turnstile token"
        error = CountryQualificationError(
            message,
            status_code=status_code,
            detail=detail,
            requires_turnstile=requires_turnstile,
        )
        raise error
    try:
        payload = response.json()
    except Exception as exc:
        raise ValueError(f"country qualification returned invalid JSON: {type(exc).__name__}") from exc
    return parse_country_qualification_response(payload)


def query_country_qualification(
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
        result = check_country_qualification_protocol(
            client,
            access_token,
            timeout=timeout,
            turnstile_token=turnstile_token,
        )
        result["country_qualification_status"] = "success"
        result["country_qualification_http_status"] = 200
        result["country_qualification_error"] = None
        result["country_qualification_requires_turnstile"] = False
        result["country_qualification_checked_at"] = oaics_result_timestamp()
        return result
    except Exception as exc:
        return {
            "country_qualification_status": "failed",
            "country_qualification_http_status": getattr(exc, "status_code", None),
            "country_qualification_error": f"{type(exc).__name__}: {str(exc)[:180]}",
            "country_qualification_requires_turnstile": bool(getattr(exc, "requires_turnstile", False)),
            "country_qualification_checked_at": oaics_result_timestamp(),
            "country_qualification_results": [],
        }
    finally:
        try:
            client.close()
        except Exception:
            pass


def oaics_result_timestamp() -> str:
    """Return a compact local timestamp for persisted protocol results."""
    return datetime.now().isoformat(timespec="seconds")


# Backward-compatible names retained for integrations written against the first
# protocol draft.  Their result includes the legacy oaics_* aliases, while new
# code should use the explicit country_qualification_* names above.
def parse_oaics_protocol_response(payload: Any) -> dict[str, Any]:
    result = parse_country_qualification_response(payload)
    return {
        **result,
        "oaics_country_results": result["country_qualification_results"],
        "oaics_eligible": result["country_qualification_eligible"],
        "oaics_query_count": result["country_qualification_query_count"],
        "oaics_session_kind": "website_protocol",
        "oaics_processor_entity": "tools.oai9.com",
    }


def check_oaics_protocol(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return parse_oaics_protocol_response(
        _protocol_payload_from_result(check_country_qualification_protocol(*args, **kwargs))
    )


def _protocol_payload_from_result(result: dict[str, Any]) -> dict[str, Any]:
    """Convert canonical parsed output into the legacy parser shape."""
    return {
        "query_count": result.get("country_qualification_query_count"),
        "results": result.get("country_qualification_results") or [],
    }


def query_oaics_countries(*args: Any, **kwargs: Any) -> dict[str, Any]:
    result = query_country_qualification(*args, **kwargs)
    if result.get("country_qualification_status") == "success":
        return {
            **result,
            "oaics_check_status": "success",
            "oaics_check_http_status": result.get("country_qualification_http_status"),
            "oaics_check_error": result.get("country_qualification_error"),
            "oaics_checked_at": result.get("country_qualification_checked_at"),
            "oaics_country_results": result.get("country_qualification_results") or [],
            "oaics_query_count": result.get("country_qualification_query_count"),
            "oaics_eligible": result.get("country_qualification_eligible"),
        }
    return {
        **result,
        "oaics_check_status": "failed",
        "oaics_check_http_status": result.get("country_qualification_http_status"),
        "oaics_check_error": result.get("country_qualification_error"),
        "oaics_checked_at": result.get("country_qualification_checked_at"),
        "oaics_country_results": result.get("country_qualification_results") or [],
    }
