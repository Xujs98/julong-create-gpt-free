# -*- coding: utf-8 -*-
"""Checkout 支付渠道资格检测引擎。

实现参考开源项目 ``yeying-xingchen/qualification-test``（commit
``d98bf731adc114323155c98dc4d6d4e70a0de095``）的检测思路：为每个国家/币种
创建一个只读 Checkout，读取 OpenAI Checkout 或 Stripe 初始化响应中的支付
渠道，不调用 confirm/start，也不发起实际支付。

本项目复用现有 BrowserSession、Sentinel runner、账号 Cookie 与代理。外部调用
继续返回 ``country_qualification_*`` 字段，因而账号列表、队列和历史数据保持兼容。
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, Iterable

from core.openai_auth import build_sentinel_header, request_sentinel_token

logger = logging.getLogger(__name__)

CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
STRIPE_API = "https://api.stripe.com"
STRIPE_VERSION_BASE = "2025-03-31.basil"
STRIPE_VERSION_FULL = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)

# 这些是公开 publishable key，仅用于 Stripe payment_pages/init；优先使用
# Checkout 响应里动态返回的 key，静态列表只作为兼容不同账号/部署的兜底。
KNOWN_PUBLISHABLE_KEYS = (
    "pk_live_51Pj377KslHRdbaPgTJYjThzH3f5dt1N1vK7LUp0qh0yNSarhfZ6nfbG7FFlh8KLxVkvdMWN5o6Mc4Vda6NHaSnaV00C2Sbl8Zs",
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n",
)

COUNTRY_LABELS = {
    "PH": "菲律宾", "GB": "英国", "NL": "荷兰", "VN": "越南",
    "ID": "印度尼西亚", "IN": "印度", "PL": "波兰", "BR": "巴西",
}

# 与开源工具的内置预设保持一致。每个预设表示一个“国家 + 币种 + 目标
# 支付渠道”，避免把同一 Checkout 的渠道误当成另一个国家资格。
QUALIFICATION_PRESETS: tuple[dict[str, str], ...] = (
    {"name": "菲律宾·GCash", "channel": "gcash", "country": "PH", "currency": "PHP"},
    {"name": "菲律宾·银行卡", "channel": "card", "country": "PH", "currency": "PHP"},
    {"name": "英国·PayPal", "channel": "paypal", "country": "GB", "currency": "GBP"},
    {"name": "荷兰·PayPal", "channel": "paypal", "country": "NL", "currency": "EUR"},
    {"name": "荷兰·iDEAL", "channel": "ideal", "country": "NL", "currency": "EUR"},
    {"name": "越南·MoMo", "channel": "momo", "country": "VN", "currency": "VND"},
    {"name": "印度尼西亚·GoPay", "channel": "gopay", "country": "ID", "currency": "IDR"},
    {"name": "印度·UPI", "channel": "upi", "country": "IN", "currency": "INR"},
    {"name": "波兰·BLIK", "channel": "blik", "country": "PL", "currency": "PLN"},
    {"name": "巴西·PIX", "channel": "pix", "country": "BR", "currency": "BRL"},
)

_CURRENCY_BY_COUNTRY = {item["country"]: item["currency"] for item in QUALIFICATION_PRESETS}
_PROFILE_LOCALE = {
    "GB": "en-GB", "NL": "nl-NL", "PH": "en-PH", "VN": "vi-VN",
    "ID": "id-ID", "IN": "en-IN", "PL": "pl-PL", "BR": "pt-BR",
}
_PROFILE_TIMEZONE = {
    "GB": "Europe/London", "NL": "Europe/Amsterdam", "PH": "Asia/Manila",
    "VN": "Asia/Ho_Chi_Minh", "ID": "Asia/Jakarta", "IN": "Asia/Kolkata",
    "PL": "Europe/Warsaw", "BR": "America/Sao_Paulo",
}
_CHANNEL_ALIASES = {
    "card": ("card", "link"),
    "paypal": ("paypal",),
    "ideal": ("ideal", "ideal_bank"),
    "momo": ("momo",),
    "gopay": ("gopay", "go_pay"),
    "gcash": ("gcash",),
    "twint": ("twint",),
    "pix": ("pix",),
    "upi": ("upi",),
    "blik": ("blik", "blik_bank"),
    "kakao": ("kakao", "kakaopay", "kakao_pay"),
}
_KNOWN_GCASH_METHOD_IDS = {"cpmt_1TOgstC6h1nxGoI3WUVEY2cJ"}
# Checkout sessions have appeared as ``cs_live_*``, ``cs_test_*`` and opaque
# ``cs_*`` values across deployments.  Keep the prefix permissive so a valid
# session is not discarded merely because Stripe changes the environment tag.
_SESSION_RE = re.compile(r"(?<![A-Za-z0-9_])(oaics_|cs_)[A-Za-z0-9_-]+")


class QualificationTestError(RuntimeError):
    """Checkout qualification protocol error."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _token(value: str) -> str:
    text = str(value or "").strip().strip('"').strip("'")
    if text.lower().startswith("bearer "):
        return text[7:].strip()
    return text


def _walk(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for child in value.values():
            yield child
            yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield child
            yield from _walk(child)


def _session_id(payload: Any) -> str:
    values = [payload]
    values.extend(_walk(payload))
    for value in values:
        text = str(value or "")
        match = _SESSION_RE.search(text)
        if match:
            return match.group(0)
    raise QualificationTestError("Checkout 未返回可识别会话（oaics_ 或 cs_*）")


def _headers(env: Any, token: str, *, sentinel: str | None = None) -> dict[str, str]:
    try:
        headers = dict(env.get_chatgpt_headers(referer="https://chatgpt.com/"))
    except Exception:
        headers = {}
    navigator_language = getattr(env, "navigator_language", "en-US")
    if callable(navigator_language):
        try:
            navigator_language = navigator_language()
        except Exception:
            navigator_language = "en-US"
    navigator_language = str(navigator_language or "en-US")
    headers.update({
        "Authorization": f"Bearer {_token(token)}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "OAI-Language": navigator_language,
        "OAI-Device-Id": str(getattr(env, "device_id", "") or ""),
    })
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    return headers


def _sentinel_headers(env: Any) -> dict[str, str]:
    challenge = request_sentinel_token(env, "chatgpt_checkout")
    sentinel_header, so_header = build_sentinel_header(env, challenge, "chatgpt_checkout")
    headers = {"openai-sentinel-token": sentinel_header}
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
    return headers


def _checkout_payload(preset: dict[str, str]) -> dict[str, Any]:
    country = str(preset.get("country") or "PH").upper()
    currency = str(preset.get("currency") or _CURRENCY_BY_COUNTRY.get(country, "USD")).upper()
    payload: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": str(preset.get("plan_name") or "chatgptplusplan"),
        "price_interval": "month",
        "seat_quantity": 1,
        "billing_details": {"country": country, "currency": currency},
        "cancel_url": "https://chatgpt.com/",
        "checkout_ui_mode": "custom",
    }
    # MoMo 的 Checkout 对 check_card_proxy=True 兼容性较差，沿用开源检测器
    # 的条件；其他渠道需要该字段来获取完整支付方式集合。
    if str(preset.get("channel") or "").lower() != "momo":
        payload["check_card_proxy"] = True
    return payload


def _create_checkout(env: Any, token: str, preset: dict[str, str], timeout: float) -> tuple[str, dict[str, Any]]:
    response = env.post(
        CHECKOUT_URL,
        headers={**_headers(env, token), **_sentinel_headers(env)},
        json=_checkout_payload(preset),
        allow_redirects=False,
        timeout=timeout,
    )
    status = int(getattr(response, "status_code", 0) or 0)
    body = getattr(response, "text", "") or ""
    if not 200 <= status < 300:
        raise QualificationTestError(
            f"OpenAI Checkout HTTP {status}: {body[:240]}", status_code=status
        )
    try:
        data = response.json() or {}
    except Exception as exc:
        raise QualificationTestError(f"Checkout 返回非 JSON：{body[:200]}", status_code=status) from exc
    # Some deployments put the session only in a redirect URL or raw response
    # body rather than under ``id``.  Search both decoded JSON and the body so
    # those valid Checkout responses follow the same provider path.
    sid = _session_id({"json": data, "text": body})
    return sid, {
        "processor_entity": str(data.get("processor_entity") or "openai_ie"),
        "publishable_key": str(data.get("publishable_key") or ""),
        "checkout_provider": str(data.get("checkout_provider") or ""),
    }


def _method_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for key in ("type", "name", "display_name", "payment_method_type", "provider", "label", "id"):
        text = str(value.get(key) or "").strip()
        if text:
            return text
    return ""


def _method_id(value: Any) -> str:
    return str(value.get("id") or "").strip() if isinstance(value, dict) else str(value or "").strip()


def _available_channels(methods: Any) -> list[str]:
    if not isinstance(methods, list):
        return []
    out: list[str] = []
    for method in methods:
        method_id = _method_id(method)
        name = "gcash" if method_id in _KNOWN_GCASH_METHOD_IDS else _method_text(method)
        if not name:
            continue
        if name.lower() == "gcash" and method_id not in _KNOWN_GCASH_METHOD_IDS:
            name = method_id or "unknown"
        if name.lower() not in {item.lower() for item in out}:
            out.append(name)
    return out


def _channel_available(methods: Any, target: str) -> bool:
    target = str(target or "").lower().strip()
    if not isinstance(methods, list):
        return False
    if target == "gcash":
        return any(_method_id(method) in _KNOWN_GCASH_METHOD_IDS for method in methods)
    aliases = _CHANNEL_ALIASES.get(target, (target,))
    for method in methods:
        if target == "card" and isinstance(method, dict) and _method_id(method).startswith("cpmt_"):
            continue
        text = json.dumps(method, ensure_ascii=False).lower() if isinstance(method, dict) else str(method).lower()
        if any(alias in text for alias in aliases):
            return True
    return False


def _method_details(methods: Any) -> list[dict[str, str]]:
    if not isinstance(methods, list):
        return []
    out = []
    for method in methods:
        name = "gcash" if _method_id(method) in _KNOWN_GCASH_METHOD_IDS else _method_text(method)
        if name:
            out.append({"name": name, "id": _method_id(method), "raw_type": str((method or {}).get("type") or "") if isinstance(method, dict) else ""})
    return out


def _stripe_payment_method_types(value: Any) -> list[str]:
    found: list[str] = []

    def add(item: Any) -> None:
        text = str(item or "").lower().strip()
        if text and text not in found:
            found.append(text)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("type", "name", "display_name", "payment_method_type", "provider", "id"):
                add(node.get(key))
            for key in ("payment_method_types", "payment_method_specs", "ordered_payment_method_types", "external_payment_method_specs", "elements_options"):
                if key in node:
                    walk(node.get(key))
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            add(node)

    for key in ("payment_method_types", "payment_method_types_preference", "payment_method_specs", "ordered_payment_method_types", "external_payment_method_specs", "elements_options"):
        walk(value.get(key) if isinstance(value, dict) else None)
    return found


def _stripe_headers() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    }


def _stripe_init(env: Any, sid: str, preferred_key: str, country: str, timeout: float) -> dict[str, Any]:
    keys = [preferred_key] if preferred_key else []
    keys.extend(key for key in KNOWN_PUBLISHABLE_KEYS if key not in keys)
    locale = _PROFILE_LOCALE.get(country, "en-US")
    timezone = _PROFILE_TIMEZONE.get(country, "America/New_York")
    last_error = ""
    for key in keys:
        for version in (STRIPE_VERSION_BASE, STRIPE_VERSION_FULL):
            data = {
                "browser_locale": locale,
                "browser_timezone": timezone,
                "elements_session_client[elements_init_source]": "custom_checkout",
                "elements_session_client[referrer_host]": "chatgpt.com",
                "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
                "elements_session_client[locale]": locale,
                "elements_session_client[is_aggregation_expected]": "false",
                "key": key,
                "_stripe_version": version,
            }
            if version == STRIPE_VERSION_FULL:
                data["elements_session_client[client_betas][0]"] = "custom_checkout_server_updates_1"
                data["elements_session_client[client_betas][1]"] = "custom_checkout_manual_approval_1"
            try:
                response = env.post(
                    f"{STRIPE_API}/v1/payment_pages/{sid}/init",
                    data=data,
                    headers=_stripe_headers(),
                    timeout=timeout,
                )
                status = int(getattr(response, "status_code", 0) or 0)
                if status == 200:
                    payload = response.json() or {}
                    total = payload.get("total_summary") or {}
                    return {
                        "currency": str(payload.get("currency") or "").upper(),
                        "checkout_amount": total.get("due") if total.get("due") is not None else (payload.get("invoice") or {}).get("amount_due"),
                        "payment_method_types": _stripe_payment_method_types(payload),
                    }
                last_error = f"{status}: {(getattr(response, 'text', '') or '')[:180]}"
                # Older Stripe API versions reject the beta parameters.  The
                # upstream checker retries the base version whenever the
                # response identifies a beta incompatibility, regardless of
                # which version was attempted first.
                if status == 400 and "beta" in (getattr(response, "text", "") or "").lower():
                    continue
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                break
    raise QualificationTestError(f"读取 Stripe Checkout 失败：{last_error}")


def _custom_state(env: Any, token: str, sid: str, processor: str, timeout: float) -> dict[str, Any]:
    response = env.get(
        f"https://chatgpt.com/backend-api/payments/checkout/{processor}/{sid}",
        headers=_headers(env, token),
        timeout=timeout,
    )
    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200:
        raise QualificationTestError(f"读取 Checkout 失败：HTTP {status}", status_code=status)
    return response.json() or {}


def _custom_methods(env: Any, token: str, sid: str, processor: str, timeout: float) -> tuple[list[Any], dict[str, Any]]:
    state: dict[str, Any] = {}
    methods: list[Any] = []
    for attempt in range(4):
        state = _custom_state(env, token, sid, processor, timeout)
        methods = state.get("custom_payment_methods") if isinstance(state, dict) else []
        if isinstance(methods, list) and methods:
            break
        if attempt < 3:
            time.sleep(0.8)
    return methods if isinstance(methods, list) else [], state


def check_payment_channel(
    env: Any,
    token: str,
    preset: dict[str, str],
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """执行一个国家/渠道 Checkout 检测，返回可持久化的标准结果。"""
    target = str(preset.get("channel") or "").lower().strip()
    country = str(preset.get("country") or "PH").upper().strip()
    sid, meta = _create_checkout(env, token, preset, timeout)
    processor = str(meta.get("processor_entity") or "openai_ie")
    if sid.startswith("cs_"):
        state = _stripe_init(env, sid, str(meta.get("publishable_key") or ""), country, timeout)
        methods = list(state.get("payment_method_types") or [])
        available = _channel_available(methods, target)
        channels = methods
        details = [{"name": item, "id": item, "raw_type": "stripe"} for item in methods]
        currency = str(state.get("currency") or preset.get("currency") or "").upper()
        amount = state.get("checkout_amount")
        provider = "stripe"
    else:
        methods, state = _custom_methods(env, token, sid, processor, timeout)
        available = _channel_available(methods, target)
        channels = _available_channels(methods)
        details = _method_details(methods)
        currency = str(preset.get("currency") or "").upper()
        amount = None
        provider = "open_ai"
        processor = processor or "openai_ie"
    return {
        "country": country,
        "country_name": COUNTRY_LABELS.get(country, country),
        "channel": target,
        "target_channel": target,
        "currency": currency,
        "status": "eligible" if available else "not_eligible",
        "eligible": bool(available),
        "message": f"{country} {target} channel published" if available else f"{country} Checkout 未发布 {target}",
        "checkout_session_id": sid,
        "processor_entity": processor if available else "",
        "payment_method_type": target if available else "",
        "checkout_amount": amount,
        "available_channels": channels,
        "channel_details": details,
        "channel_availability": {target: bool(available)},
        "checkout_provider": provider,
    }


def query_country_qualification(
    env: Any,
    access_token: str,
    *,
    timeout: float = 30.0,
    presets: Iterable[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """按开源资格检测器的预设集合查询各国支付渠道资格。"""
    token = _token(access_token)
    if not token:
        raise ValueError("access token is empty")
    selected = list(presets or QUALIFICATION_PRESETS)
    results: list[dict[str, Any]] = []
    for preset in selected:
        try:
            results.append(check_payment_channel(env, token, dict(preset), timeout=timeout))
        except Exception as exc:
            country = str(preset.get("country") or "").upper()
            channel = str(preset.get("channel") or "").lower()
            results.append({
                "country": country,
                "country_name": COUNTRY_LABELS.get(country, country),
                "channel": channel,
                "target_channel": channel,
                "currency": str(preset.get("currency") or "").upper(),
                "status": "failed",
                "eligible": None,
                "http_status": getattr(exc, "status_code", None),
                "message": f"{type(exc).__name__}: {str(exc)[:180]}",
                "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                "available_channels": [],
                "channel_details": [],
                "channel_availability": {},
            })
            logger.warning("资格检测失败 country=%s channel=%s: %s", country, channel, exc)
    succeeded = [item for item in results if item.get("status") != "failed"]
    return {
        "country_qualification_results": results,
        "country_qualification_eligible": any(item.get("eligible") is True for item in results),
        "country_qualification_query_count": len(results),
        "country_qualification_status": "success" if succeeded else "failed",
        "country_qualification_source": "qualification-test",
        "country_qualification_engine": "qualification-test@d98bf731",
    }
