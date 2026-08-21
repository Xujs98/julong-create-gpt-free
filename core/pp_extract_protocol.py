# -*- coding: utf-8 -*-
"""项目内置 PP 协议提链。

流程：创建 Hosted Checkout -> Stripe init -> 创建 PayPal payment method ->
confirm -> 可选 Checkout Update/approve -> 轮询 PayPal authorize 地址。
只接受应付金额为 0 且明确支持 PayPal 的 Checkout。
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from curl_cffi import requests as curl_requests

from core import chatgpt_plan
from core.proxy_utils import normalize_proxy_url, rotate_proxy_session


CHECKOUT_PATH = "/backend-api/payments/checkout"
APPROVE_PATH = "/backend-api/payments/checkout/approve"
STRIPE_API = "https://api.stripe.com/v1"
STRIPE_VERSION = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
STRIPE_RUNTIME_VERSION = "6f8494a281"
PAYPAL_AUTHORIZE_RE = re.compile(r"^https://pm-redirects\.stripe\.com/authorize/", re.I)
COUNTRY_CURRENCY = {
    "AT": "EUR", "BE": "EUR", "CH": "CHF", "DE": "EUR", "DK": "DKK",
    "ES": "EUR", "FI": "EUR", "FR": "EUR", "GB": "GBP", "IE": "EUR",
    "IT": "EUR", "JP": "JPY", "LU": "EUR", "NL": "EUR", "NO": "NOK",
    "PT": "EUR", "SE": "SEK", "US": "USD", "CA": "CAD", "AU": "AUD",
}
COUNTRY_BILLING = {
    "AT": ("Max Bauer", "Kaerntner Strasse 1", "Vienna", "Vienna", "1010"),
    "BE": ("Lucas Peeters", "Rue Neuve 1", "Brussels", "Brussels", "1000"),
    "CH": ("Luca Meier", "Bahnhofstrasse 1", "Zurich", "Zurich", "8001"),
    "DE": ("Leon Schmidt", "Invalidenstrasse 1", "Berlin", "Berlin", "10115"),
    "DK": ("Emil Nielsen", "Nyhavn 1", "Copenhagen", "Capital Region", "1051"),
    "ES": ("Hugo Garcia", "Calle Mayor 1", "Madrid", "Madrid", "28013"),
    "FI": ("Elias Korhonen", "Mannerheimintie 1", "Helsinki", "Uusimaa", "00100"),
    "FR": ("Jean Martin", "10 Rue de Rivoli", "Paris", "Ile-de-France", "75001"),
    "GB": ("Oliver Wilson", "10 Downing Street", "London", "England", "SW1A 2AA"),
    "IE": ("Liam Kelly", "1 O'Connell Street", "Dublin", "Dublin", "D01 F5P2"),
    "IT": ("Marco Rossi", "Via Roma 1", "Rome", "Lazio", "00184"),
    "JP": ("Taro Yamada", "1-1 Chiyoda", "Chiyoda-ku", "Tokyo", "100-0001"),
    "LU": ("Louis Weber", "Grand Rue 1", "Luxembourg", "Luxembourg", "1661"),
    "NL": ("Daan de Jong", "Damrak 1", "Amsterdam", "North Holland", "1012"),
    "NO": ("Noah Hansen", "Karl Johans gate 1", "Oslo", "Oslo", "0154"),
    "PT": ("Tiago Silva", "Rua Augusta 1", "Lisbon", "Lisbon", "1100-048"),
    "SE": ("William Andersson", "Drottninggatan 1", "Stockholm", "Stockholm", "111 51"),
    "US": ("James Smith", "350 5th Ave", "New York", "NY", "10001"),
    "CA": ("Liam Martin", "100 Queen St W", "Toronto", "ON", "M5H 2N2"),
    "AU": ("Jack Wilson", "1 Martin Place", "Sydney", "NSW", "2000"),
}


class PPProtocolError(RuntimeError):
    def __init__(self, message: str, *, code: str = "pp_protocol_failed", retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _emit(callback: Callable[[str, int], None] | None, message: str, progress: int) -> None:
    if callback:
        callback(str(message), max(0, min(100, int(progress))))


def _session(proxy: str = ""):
    session = curl_requests.Session(impersonate="chrome")
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def _json_response(response, label: str) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        data = {"raw": str(getattr(response, "text", "") or "")[:500]}
    if not 200 <= int(response.status_code) < 300:
        detail = data.get("detail") if isinstance(data, dict) else None
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            detail = error.get("message") or error.get("code") or detail
        retryable = int(response.status_code) in {408, 409, 425, 429} or int(response.status_code) >= 500
        raise PPProtocolError(
            f"{label} HTTP {response.status_code}: {str(detail or error or data)[:260]}",
            code=f"{label.lower().replace(' ', '_')}_http_{response.status_code}",
            retryable=retryable,
        )
    if not isinstance(data, dict):
        raise PPProtocolError(f"{label} 响应不是 JSON 对象", retryable=False)
    return data


def _transport_error(exc: Exception, label: str) -> PPProtocolError:
    return PPProtocolError(
        f"{label} 网络异常: {type(exc).__name__}: {str(exc)[:220]}",
        code=f"{label.lower().replace(' ', '_')}_transport",
        retryable=True,
    )


def _extract_redirect(payload: Any) -> str:
    if isinstance(payload, str):
        match = re.search(r"https://pm-redirects\.stripe\.com/authorize/[^\s\"'<>]+", payload)
        return match.group(0) if match else ""
    if isinstance(payload, dict):
        next_action = payload.get("next_action")
        if isinstance(next_action, dict):
            redirect = next_action.get("redirect_to_url")
            if isinstance(redirect, dict):
                url = str(redirect.get("url") or "").strip()
                if url:
                    return url
        for value in payload.values():
            found = _extract_redirect(value)
            if found:
                return found
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            found = _extract_redirect(value)
            if found:
                return found
    return ""


def _billing(country: str) -> dict[str, str]:
    code = str(country or "GB").strip().upper()
    if code not in COUNTRY_BILLING:
        raise PPProtocolError(f"账单国家暂未配置地址模板: {code}", code="unsupported_billing_country", retryable=False)
    name, line1, city, state, postal = COUNTRY_BILLING[code]
    email_local = re.sub(r"[^a-z0-9]+", ".", name.lower()).strip(".")
    return {
        "name": name,
        "email": f"{email_local}.{uuid.uuid4().hex[:6]}@example.com",
        "country": code,
        "line1": line1,
        "city": city,
        "state": state,
        "postal_code": postal,
    }


def _chatgpt_session(access_token: str, proxy: str):
    session = _session(proxy)
    device_id = str(uuid.uuid4())
    claims = chatgpt_plan.token_claims(access_token)
    session.headers.update({
        "Authorization": f"Bearer {chatgpt_plan.normalize_token(access_token)}",
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "oai-device-id": device_id,
        "oai-language": "en-US",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "Cookie": f"oai-did={device_id}",
    })
    account_id = str(claims.get("account_id") or "").strip()
    if account_id:
        session.headers["chatgpt-account-id"] = account_id
    return session


def _create_checkout(access_token: str, proxy: str, country: str, timeout: float, session=None) -> dict[str, Any]:
    own = session is None
    session = session or _chatgpt_session(access_token, proxy)
    currency = COUNTRY_CURRENCY.get(country, "USD")
    payload = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "promo_campaign": {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False},
        "checkout_ui_mode": "hosted",
    }
    try:
        try:
            response = session.post(
                "https://chatgpt.com" + CHECKOUT_PATH,
                json=payload,
                headers={"x-openai-target-path": CHECKOUT_PATH, "x-openai-target-route": CHECKOUT_PATH},
                timeout=timeout,
            )
        except Exception as exc:
            raise _transport_error(exc, "Checkout create") from exc
        data = _json_response(response, "Checkout create")
        checkout_session_id = str(data.get("checkout_session_id") or data.get("session_id") or data.get("id") or "")
        publishable_key = str(data.get("publishable_key") or "")
        hosted_url = str(data.get("stripe_hosted_url") or data.get("hosted_checkout_url") or data.get("url") or data.get("checkout_url") or "")
        if not checkout_session_id.startswith("cs_") or not publishable_key.startswith("pk_"):
            raise PPProtocolError("Checkout 响应缺少 Stripe session 或 publishable key", code="checkout_missing_fields")
        return {
            "checkout_session_id": checkout_session_id,
            "publishable_key": publishable_key,
            "processor_entity": str(data.get("processor_entity") or ""),
            "hosted_checkout_url": hosted_url,
            "currency": currency,
        }
    finally:
        if own:
            session.close()


def _stripe_init(proxy: str, publishable_key: str, checkout_session_id: str, timeout: float) -> dict[str, Any]:
    session = _session(proxy)
    body = {
        "browser_locale": "en-US",
        "browser_timezone": "Asia/Shanghai",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
        "elements_session_client[locale]": "en",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": publishable_key,
        "_stripe_version": STRIPE_VERSION,
    }
    try:
        response = session.post(
            f"{STRIPE_API}/payment_pages/{checkout_session_id}/init",
            data=body,
            headers={"Origin": "https://pay.openai.com", "Referer": "https://pay.openai.com/"},
            timeout=timeout,
        )
        return _json_response(response, "Stripe init")
    finally:
        session.close()


def _zero_amount_guard(init: dict[str, Any], payment_method: str) -> dict[str, Any]:
    invoice = init.get("invoice") if isinstance(init.get("invoice"), dict) else {}
    total_summary = init.get("total_summary") if isinstance(init.get("total_summary"), dict) else {}
    raw_due = invoice.get("amount_due") if "amount_due" in invoice else total_summary.get("due")
    try:
        amount_due = int(raw_due)
    except (TypeError, ValueError):
        raise PPProtocolError("Stripe init 未返回可确认的应付金额", code="amount_unknown", retryable=False)
    if amount_due != 0:
        raise PPProtocolError(f"应付金额为 {amount_due}，已停止 PP 提链", code="non_zero_amount", retryable=False)
    methods = {str(item).lower() for item in (init.get("payment_method_types") or [])}
    if payment_method.lower() not in methods:
        raise PPProtocolError(f"当前 Checkout 不支持 {payment_method}", code="payment_method_unavailable", retryable=False)
    return {"amount_due": amount_due, "currency": str(init.get("currency") or invoice.get("currency") or "").lower()}


def _stripe_context(init: dict[str, Any]) -> dict[str, str]:
    return {
        "stripe_js_id": str(uuid.uuid4()),
        "elements_session_id": f"elements_session_{uuid.uuid4().hex[:11]}",
        "elements_session_config_id": str(init.get("config_id") or uuid.uuid4()),
        "config_id": str(init.get("config_id") or ""),
        "init_checksum": str(init.get("init_checksum") or ""),
    }


def _create_payment_method(proxy: str, publishable_key: str, checkout_session_id: str, billing: dict[str, str], ctx: dict[str, str], timeout: float) -> str:
    session = _session(proxy)
    body = {
        "billing_details[name]": billing["name"],
        "billing_details[email]": billing["email"],
        "billing_details[address][country]": billing["country"],
        "billing_details[address][line1]": billing["line1"],
        "billing_details[address][city]": billing["city"],
        "billing_details[address][postal_code]": billing["postal_code"],
        "billing_details[address][state]": billing["state"],
        "type": "paypal",
        "payment_user_agent": f"stripe.js/{STRIPE_RUNTIME_VERSION}; stripe-js-v3/{STRIPE_RUNTIME_VERSION}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": "35000",
        "client_attribution_metadata[checkout_session_id]": checkout_session_id,
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_config_id]": ctx["config_id"],
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "key": publishable_key,
        "_stripe_version": STRIPE_VERSION,
    }
    try:
        response = session.post(f"{STRIPE_API}/payment_methods", data=body, timeout=timeout)
        data = _json_response(response, "Stripe payment method")
        payment_method_id = str(data.get("id") or "")
        if not payment_method_id.startswith("pm_"):
            raise PPProtocolError("Stripe payment method 响应缺少 pm_ 标识", code="payment_method_bad_response")
        return payment_method_id
    finally:
        session.close()


def _success_return_url(checkout_session_id: str, country: str, processor_entity: str) -> str:
    entity = processor_entity or ("openai_llc" if country == "US" else "openai_ie")
    return f"https://chatgpt.com/checkout/verify?stripe_session_id={checkout_session_id}&processor_entity={entity}&plan_type=plus"


def _confirm_return_url(checkout_session_id: str, init: dict[str, Any], country: str, processor_entity: str) -> str:
    hosted = str(init.get("url") or init.get("stripe_hosted_url") or "").strip()
    if not hosted:
        hosted = f"https://pay.openai.com/c/pay/{checkout_session_id}"
    parsed = urlsplit(hosted)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("success_return_url", _success_return_url(checkout_session_id, country, processor_entity))
    return urlunsplit((parsed.scheme or "https", parsed.netloc or "pay.openai.com", parsed.path, urlencode(query), parsed.fragment))


def _confirm(proxy: str, publishable_key: str, checkout_session_id: str, init: dict[str, Any], payment_method_id: str, ctx: dict[str, str], billing: dict[str, str], processor_entity: str, timeout: float) -> dict[str, Any]:
    session = _session(proxy)
    return_url = _confirm_return_url(checkout_session_id, init, billing["country"], processor_entity)
    body = {
        "guid": uuid.uuid4().hex, "muid": uuid.uuid4().hex, "sid": uuid.uuid4().hex,
        "payment_method": payment_method_id,
        "init_checksum": str(init.get("init_checksum") or ctx["init_checksum"]),
        "version": STRIPE_RUNTIME_VERSION,
        "expected_amount": "0",
        "expected_payment_method_type": "paypal",
        "return_url": return_url,
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[locale]": "en",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_session_id]": checkout_session_id,
        "client_attribution_metadata[checkout_config_id]": ctx["config_id"],
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "consent[terms_of_service]": "accepted",
        "key": publishable_key,
        "_stripe_version": STRIPE_VERSION,
    }
    try:
        response = session.post(f"{STRIPE_API}/payment_pages/{checkout_session_id}/confirm", data=body, timeout=timeout)
        return _json_response(response, "Stripe confirm")
    finally:
        session.close()


def _approve(session, checkout_session_id: str, processor_entity: str, timeout: float) -> dict[str, Any]:
    response = session.post(
        "https://chatgpt.com" + APPROVE_PATH,
        json={"checkout_session_id": checkout_session_id, "processor_entity": processor_entity},
        headers={
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{checkout_session_id}",
            "x-openai-target-path": APPROVE_PATH,
            "x-openai-target-route": APPROVE_PATH,
        },
        timeout=timeout,
    )
    return _json_response(response, "Checkout update")


def _poll_redirect(proxy: str, publishable_key: str, checkout_session_id: str, timeout: float) -> str:
    session = _session(proxy)
    deadline = time.monotonic() + max(1.0, timeout)
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": f"elements_session_{uuid.uuid4().hex[:11]}",
        "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
        "elements_session_client[locale]": "en",
        "elements_session_client[is_aggregation_expected]": "false",
        "key": publishable_key,
        "_stripe_version": STRIPE_VERSION,
    }
    try:
        while time.monotonic() < deadline:
            response = session.get(f"{STRIPE_API}/payment_pages/{checkout_session_id}", params=params, timeout=min(6.0, timeout))
            if response.status_code == 200:
                try:
                    redirect = _extract_redirect(response.json())
                except Exception:
                    redirect = _extract_redirect(response.text or "")
                if PAYPAL_AUTHORIZE_RE.match(redirect):
                    return redirect
            time.sleep(0.75)
    finally:
        session.close()
    raise PPProtocolError("等待 PayPal authorize 地址超时", code="paypal_redirect_timeout")


def extract_pp_link(
    access_token: str,
    *,
    billing_country: str = "GB",
    payment_method: str = "paypal",
    auto_enter_paypal: bool = True,
    checkout_update: bool = True,
    proxy: str | None = None,
    timeout: float = 30.0,
    progress: Callable[[str, int], None] | None = None,
) -> dict[str, Any]:
    token = chatgpt_plan.normalize_token(access_token)
    if not token:
        raise PPProtocolError("access_token 为空", code="missing_access_token", retryable=False)
    country = str(billing_country or "GB").strip().upper()
    method = str(payment_method or "paypal").strip().lower()
    if method != "paypal":
        raise PPProtocolError("PP 协议当前仅支持 PayPal", code="unsupported_payment_method", retryable=False)
    route = chatgpt_plan.resolve_plan_check_route(proxy)
    rotated_proxy = rotate_proxy_session(str(route.get("proxy") or ""))
    active_proxy = normalize_proxy_url(rotated_proxy, default_scheme="auto") if rotated_proxy else ""
    chatgpt_session = _chatgpt_session(token, active_proxy)
    try:
        _emit(progress, "创建 Plus 试用 Checkout", 12)
        checkout = _create_checkout(token, active_proxy, country, timeout, session=chatgpt_session)
        _emit(progress, "读取 Stripe Checkout 配置", 30)
        init = _stripe_init(active_proxy, checkout["publishable_key"], checkout["checkout_session_id"], timeout)
        gate = _zero_amount_guard(init, method)
        _emit(progress, "创建 PayPal 支付方式", 48)
        billing = _billing(country)
        ctx = _stripe_context(init)
        payment_method_id = _create_payment_method(
            active_proxy, checkout["publishable_key"], checkout["checkout_session_id"], billing, ctx, timeout
        )
        _emit(progress, "提交 Stripe PayPal confirm", 68)
        processor_entity = checkout["processor_entity"] or ("openai_llc" if country == "US" else "openai_ie")
        confirmed = _confirm(
            active_proxy, checkout["publishable_key"], checkout["checkout_session_id"], init,
            payment_method_id, ctx, billing, processor_entity, timeout,
        )
        redirect = _extract_redirect(confirmed)
        submission = confirmed.get("submission_attempt") if isinstance(confirmed, dict) else None
        confirm_state = str((submission or {}).get("state") or "") if isinstance(submission, dict) else ""
        if not redirect and checkout_update and confirm_state == "requires_approval":
            _emit(progress, "执行 Checkout Update", 80)
            redirect = _extract_redirect(_approve(chatgpt_session, checkout["checkout_session_id"], processor_entity, timeout))
        if not PAYPAL_AUTHORIZE_RE.match(redirect):
            _emit(progress, "等待 PayPal authorize 地址", 88)
            redirect = _poll_redirect(active_proxy, checkout["publishable_key"], checkout["checkout_session_id"], min(timeout, 25.0))
        if not PAYPAL_AUTHORIZE_RE.match(redirect):
            raise PPProtocolError("未取得有效 PayPal authorize 地址", code="paypal_authorize_missing")
        _emit(progress, "PP 提链成功", 100)
        long_url = redirect if auto_enter_paypal else checkout["hosted_checkout_url"] or redirect
        return {
            "long_url": long_url,
            "copy_paste": redirect,
            "paypal_authorize_url": redirect,
            "hosted_checkout_url": checkout["hosted_checkout_url"],
            "checkout_session_id": checkout["checkout_session_id"],
            "payment_method": "paypal",
            "payment_link_type": "protocol",
            "billing_country": country,
            "currency": gate["currency"] or checkout["currency"].lower(),
            "amount_due": gate["amount_due"],
            "zero_verified": True,
            "processor_entity": processor_entity,
            "checkout_update": bool(checkout_update),
            "auto_enter_paypal": bool(auto_enter_paypal),
            "network_route": route.get("network_route"),
            "proxy_used": route.get("proxy_used"),
        }
    finally:
        chatgpt_session.close()
