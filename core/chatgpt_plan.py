# -*- coding: utf-8 -*-
"""ChatGPT 账号套餐/试用资格查询。"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import socket
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlparse

from config import proxy as proxy_cfg
from config import webui as webui_config
from core.session import BrowserSession
from core.proxy_utils import rotate_proxy_session
from core.oaics_checker import (
    check_country_qualification_protocol,
    detect_oaics_checkout,
    oaics_result_timestamp,
)

logger = logging.getLogger(__name__)

ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
CHECKOUT_PATH = "/backend-api/payments/checkout"
_CHECKOUT_CURRENCY_BY_COUNTRY = {
    "AU": "AUD", "AT": "EUR", "BE": "EUR", "CA": "CAD", "CH": "CHF",
    "DE": "EUR", "DK": "DKK", "ES": "EUR", "FI": "EUR", "FR": "EUR",
    "GB": "GBP", "IE": "EUR", "IT": "EUR", "JP": "JPY", "LU": "EUR",
    "NL": "EUR", "NO": "NOK", "PT": "EUR", "SE": "SEK", "US": "USD",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_token(token: str) -> str:
    token = (token or "").strip().strip('"').strip("'")
    if token.lower().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _mask_proxy(proxy: str) -> str:
    """返回可用于日志/API 结果的代理摘要，不泄露用户名和密码。"""
    value = str(proxy or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        auth = "***:***@" if parsed.username or parsed.password else ""
        return f"{scheme}{auth}{host}{port}" or "***"
    except Exception:
        return "***"


def _local_proxy_status(proxy: str) -> tuple[bool, bool, str | None]:
    """检查回环代理端口；非本地代理不做预探测，避免额外网络请求。"""
    value = str(proxy or "").strip()
    if not value:
        return False, False, None
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        host = parsed.hostname or ""
        is_loopback = host.lower() == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            return False, True, None
        if not parsed.port:
            return True, False, "本地代理未配置端口"
        try:
            with socket.create_connection((host, parsed.port), timeout=0.5):
                return True, True, None
        except OSError as exc:
            return True, False, f"本地代理 {host}:{parsed.port} 未监听（{type(exc).__name__}）"
    except Exception as exc:
        return False, False, f"代理地址解析失败（{type(exc).__name__}）"


def resolve_plan_check_route(explicit_proxy: Optional[str] = None) -> dict:
    """解析套餐查询的实际网络路径。

    explicit_proxy 不是 None 时表示 API 调用方明确覆盖配置；空字符串代表直连。
    """
    if explicit_proxy is not None:
        selected = str(explicit_proxy or "").strip()
        return {
            "proxy": selected,
            "proxy_mode": "request",
            "network_route": "proxy" if selected else "direct",
            "proxy_used": _mask_proxy(selected) or None,
            "proxy_fallback_reason": None,
        }

    mode = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "auto") or "auto").strip().lower()
    if mode not in {"auto", "proxy", "direct"}:
        raise ValueError(f"PLAN_CHECK_PROXY_MODE={mode!r} 无效，可选 auto / proxy / direct")
    if mode == "direct":
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct",
            "proxy_used": None,
            "proxy_fallback_reason": None,
        }

    selected = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY", "") or "").strip()
    if not selected:
        selected = str(proxy_cfg.pick_proxy() or "").strip()
    if not selected:
        if mode == "proxy":
            raise ValueError("套餐查询网络模式为 proxy，但未配置 PLAN_CHECK_PROXY 或 PROXY_POOL")
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct",
            "proxy_used": None,
            "proxy_fallback_reason": "未配置套餐查询代理或代理池",
        }

    is_local, available, reason = _local_proxy_status(selected)
    if mode == "auto" and is_local and not available:
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct_fallback",
            "proxy_used": _mask_proxy(selected),
            "proxy_fallback_reason": reason,
        }
    return {
        "proxy": selected,
        "proxy_mode": mode,
        "network_route": "proxy",
        "proxy_used": _mask_proxy(selected),
        "proxy_fallback_reason": None,
    }


def decode_jwt_payload_unverified(token: str) -> dict:
    """仅本地解析 JWT payload，不校验签名。"""
    token = normalize_token(token)
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def token_claims(token: str) -> dict:
    payload = decode_jwt_payload_unverified(token)
    auth = payload.get("https://api.openai.com/auth") or {}
    profile = payload.get("https://api.openai.com/profile") or {}
    exp = payload.get("exp")
    exp_iso = None
    expired = None
    if isinstance(exp, (int, float)):
        exp_iso = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        expired = datetime.now(tz=timezone.utc).timestamp() >= float(exp)
    return {
        "payload": payload,
        "email": profile.get("email"),
        "user_name": profile.get("name"),
        "user_id": auth.get("chatgpt_user_id") or auth.get("user_id"),
        "account_id": auth.get("chatgpt_account_id"),
        "claim_plan_type": auth.get("chatgpt_plan_type"),
        "exp": exp,
        "token_expires_at": exp_iso,
        "token_expired": expired,
    }


def _common_headers(
    env: BrowserSession,
    token: str,
    *,
    account_id: str | None = None,
    device_id: str | None = None,
) -> dict[str, str]:
    """构造已登录 accounts/check 请求头，补齐账号与设备上下文。"""
    claims = token_claims(token)
    active_account_id = str(account_id or claims.get("account_id") or "").strip()
    active_device_id = str(device_id or env.device_id or "").strip()
    headers = env.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers.update({
        "accept": "*/*",
        "authorization": f"Bearer {normalize_token(token)}",
        "oai-device-id": active_device_id,
        "oai-language": env.navigator_language(),
        "referer": "https://chatgpt.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-openai-target-path": ACCOUNTS_CHECK_PATH,
        "x-openai-target-route": "/backend-api/accounts/check/{version}",
    })
    if active_account_id:
        headers["chatgpt-account-id"] = active_account_id
    return headers


def _checkout_headers(
    env: BrowserSession,
    token: str,
    *,
    account_id: str | None = None,
    device_id: str | None = None,
) -> dict[str, str]:
    headers = _common_headers(
        env,
        token,
        account_id=account_id,
        device_id=device_id,
    )
    headers.update({
        "origin": "https://chatgpt.com",
        "x-openai-target-path": CHECKOUT_PATH,
        "x-openai-target-route": CHECKOUT_PATH,
    })
    return headers


def check_oaics_eligibility(
    env: BrowserSession,
    token: str,
    *,
    account_id: str | None = None,
    device_id: str | None = None,
    billing_country: str = "",
    promo_campaign_id: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """仅通过 ChatGPT checkout 会话检测 OAICS 资格。"""
    checked_at = now_iso()
    billing_code = str(billing_country or "").strip().upper()
    billing_details = None
    if len(billing_code) == 2:
        billing_details = {
            "country": billing_code,
            "currency": _CHECKOUT_CURRENCY_BY_COUNTRY.get(billing_code, "USD"),
        }
    payload = {
        # This mirrors the current web checkout request shape.  The previous
        # flat promo_campaign_id + embedded payload is accepted by some older
        # deployments but is rejected by newer checkout risk checks.
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": billing_details,
        "promo_campaign": {
            "promo_campaign_id": promo_campaign_id or "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": "hosted",
    }
    resp = None
    try:
        resp = env.post(
            f"https://chatgpt.com{CHECKOUT_PATH}",
            headers=_checkout_headers(
                env,
                token,
                account_id=account_id,
                device_id=device_id,
            ),
            json=payload,
            allow_redirects=False,
            timeout=timeout,
        )
        http_status = int(resp.status_code)
        if not 200 <= http_status < 300:
            detail = ""
            try:
                body = resp.json()
                if isinstance(body, dict):
                    detail = str(body.get("detail") or body.get("error") or "").strip()
            except Exception:
                detail = ""
            suffix = f": {detail[:240]}" if detail else ""
            return {
                "oaics_check_status": "failed",
                "oaics_checked_at": checked_at,
                "oaics_check_http_status": http_status,
                "oaics_check_error": f"OAICS checkout HTTP {http_status}{suffix}",
                "oaics_check_retryable": bool(
                    http_status in {408, 409, 425, 429}
                    or http_status >= 500
                    or "unusual activity" in detail.lower()
                ),
            }
        data = resp.json()
        # Legacy integrations briefly pointed the OAICS checker at the public
        # country endpoint.  If a checkout response unmistakably has that old
        # shape, preserve a read-only compatibility result; normal checkout
        # responses never enter this branch and OAICS remains checkout-only.
        if isinstance(data, dict) and isinstance(data.get("results"), list) and not any(
            data.get(key) for key in ("checkout_session_id", "session_id", "id", "url")
        ):
            protocol_session = getattr(env, "session", None)
            if not callable(getattr(protocol_session, "post", None)):
                protocol_session = env
            legacy = check_country_qualification_protocol(protocol_session, token, timeout=timeout)
            return {
                "oaics_check_status": "success",
                "oaics_checked_at": checked_at,
                "oaics_check_http_status": http_status,
                "oaics_eligible": bool(legacy.get("country_qualification_eligible")),
                "oaics_session_kind": "website_protocol",
                "oaics_processor_entity": "tools.oai9.com",
                "oaics_check_error": None,
                "oaics_country_results": legacy.get("country_qualification_results") or [],
                "oaics_query_count": legacy.get("country_qualification_query_count"),
            }
        detected = detect_oaics_checkout(data, billing_country=billing_country)
        return {
            "oaics_check_status": "success",
            "oaics_checked_at": checked_at,
            "oaics_check_http_status": http_status,
            "oaics_eligible": bool(detected["is_oaics"]),
            "oaics_session_kind": detected["session_kind"],
            "oaics_processor_entity": detected["processor_entity"],
            "oaics_check_error": None,
            "oaics_country_results": [],
        }
    except Exception as exc:
        return {
            "oaics_check_status": "failed",
            "oaics_checked_at": checked_at,
            "oaics_check_http_status": int(resp.status_code) if resp is not None else None,
            "oaics_check_error": f"{type(exc).__name__}: {str(exc)[:180]}",
            "oaics_check_retryable": True,
            "oaics_country_results": [],
        }


def _check_country_qualification(
    env: BrowserSession,
    token: str,
    *,
    timeout: float = 15.0,
    turnstile_token: str | None = None,
) -> dict[str, Any]:
    """查询 tools.oai9.com 的各国资格，不触碰 OAICS checkout 字段。"""
    checked_at = now_iso()
    # A token generated by a localhost WebUI is rejected by the production
    # site key (Turnstile error 110200).  When no browser token was supplied,
    # execute the official page in a browser context so the token and POST
    # request share tools.oai9.com as their origin.
    if (
        not str(turnstile_token or "").strip()
        and bool(getattr(webui_config, "COUNTRY_QUALIFICATION_BROWSER_RELAY_ENABLED", True))
    ):
        try:
            from core.country_qualification_browser import query_country_qualification_browser

            try:
                configured_browser_timeout = float(
                    getattr(webui_config, "COUNTRY_QUALIFICATION_BROWSER_TIMEOUT", 240) or 240
                )
            except (TypeError, ValueError):
                configured_browser_timeout = 240.0
            browser_timeout = max(float(timeout or 0), configured_browser_timeout)
            result = query_country_qualification_browser(token, timeout=browser_timeout)
            result.update({
                "country_qualification_status": "success",
                "country_qualification_checked_at": checked_at,
                "country_qualification_http_status": 200,
                "country_qualification_error": None,
                "country_qualification_requires_turnstile": False,
                "country_qualification_source": "tools.oai9.com/browser",
            })
            return result
        except Exception as exc:
            logger.warning("各国资格官方浏览器中继失败: %s", exc)
            return {
                "country_qualification_status": "failed",
                "country_qualification_checked_at": checked_at,
                "country_qualification_http_status": getattr(exc, "status_code", None),
                "country_qualification_error": f"{type(exc).__name__}: {str(exc)[:180]}",
                "country_qualification_requires_turnstile": True,
                "country_qualification_results": [],
                "country_qualification_query_count": None,
                "country_qualification_eligible": None,
                "country_qualification_source": "tools.oai9.com/browser",
            }
    protocol_session = getattr(env, "session", None)
    if not callable(getattr(protocol_session, "post", None)):
        protocol_session = env
    try:
        result = check_country_qualification_protocol(
            protocol_session,
            token,
            timeout=timeout,
            turnstile_token=turnstile_token,
        )
        result.update({
            "country_qualification_status": "success",
            "country_qualification_checked_at": checked_at,
            "country_qualification_http_status": 200,
            "country_qualification_error": None,
            "country_qualification_requires_turnstile": False,
        })
        return result
    except Exception as exc:
        return {
            "country_qualification_status": "failed",
            "country_qualification_checked_at": checked_at,
            "country_qualification_http_status": getattr(exc, "status_code", None),
            "country_qualification_error": f"{type(exc).__name__}: {str(exc)[:180]}",
            "country_qualification_requires_turnstile": bool(getattr(exc, "requires_turnstile", False)),
            "country_qualification_results": [],
            "country_qualification_query_count": None,
            "country_qualification_eligible": None,
        }


# Public name for direct integrations/tests; the implementation stays private
# so the ``check_country_qualification`` flag on check_account_plan is callable.
check_country_qualification = _check_country_qualification
check_country_qualification_query = _check_country_qualification


def _restore_plan_session_context(
    env: BrowserSession,
    *,
    device_id: str | None = None,
    session_cookies: list[dict] | None = None,
) -> None:
    """复用查活保存的 oai-did 与 Session Cookie，避免套餐查询变成全新匿名环境。"""
    active_device_id = str(device_id or "").strip()
    if active_device_id:
        env.device_id = active_device_id
    for item in session_cookies or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or ".chatgpt.com")
        path = str(item.get("path") or "/") or "/"
        env.session.cookies.set(
            name,
            value,
            domain=domain,
            path=path,
            secure=bool(item.get("secure")),
        )
        if name.lower() == "oai-did" and value and not active_device_id:
            env.device_id = value
    if env.device_id:
        for domain in ("chatgpt.com", "auth.openai.com", "sentinel.openai.com"):
            env.session.cookies.set("oai-did", env.device_id, domain=domain, path="/")


def parse_accounts_check(data: dict, *, token: str = "") -> dict:
    """从 accounts/check 响应提取套餐和 Plus 试用资格。"""
    claims = token_claims(token) if token else {}
    claim_account_id = claims.get("account_id")
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        raise ValueError("响应缺少 accounts 对象")

    item = None
    account_key = None
    if claim_account_id and isinstance(accounts.get(claim_account_id), dict):
        item = accounts.get(claim_account_id)
        account_key = claim_account_id
    elif isinstance(accounts.get("default"), dict):
        item = accounts.get("default")
        account = item.get("account") or {}
        account_key = account.get("account_id") or "default"
    else:
        for k, v in accounts.items():
            if k != "default" and isinstance(v, dict):
                item = v
                account_key = k
                break
    if not isinstance(item, dict):
        raise ValueError("未找到可解析的账号条目")

    account = item.get("account") or {}
    entitlement = item.get("entitlement") or {}
    last_sub = item.get("last_active_subscription") or {}
    eligible_promo_campaigns = item.get("eligible_promo_campaigns") or {}
    plus_campaign = eligible_promo_campaigns.get("plus") if isinstance(eligible_promo_campaigns, dict) else None
    plus_meta = (plus_campaign or {}).get("metadata") or {}
    discount = plus_meta.get("discount") or {}
    duration = plus_meta.get("duration") or {}

    plan_type = account.get("plan_type") or claims.get("claim_plan_type") or ""
    subscription_plan = entitlement.get("subscription_plan") or ""
    has_active_subscription = bool(entitlement.get("has_active_subscription"))
    is_free = str(plan_type).lower() == "free" or str(subscription_plan).lower() == "chatgptfreeplan"
    plus_trial_eligible = bool(is_free and plus_campaign)

    offers = ((item.get("eligible_offers") or {}).get("offers") or [])
    eligible_offer_ids = [o.get("id") for o in offers if isinstance(o, dict) and o.get("id")]

    result = {
        "ok": True,
        "checked_at": now_iso(),
        "account_id": account.get("account_id") or account_key or claim_account_id,
        "account_user_role": account.get("account_user_role"),
        "current_plan_type": plan_type,
        "subscription_plan": subscription_plan,
        "has_active_subscription": has_active_subscription,
        "is_active_subscription_gratis": bool(entitlement.get("is_active_subscription_gratis")),
        "expires_at": entitlement.get("expires_at"),
        "renews_at": entitlement.get("renews_at"),
        "cancels_at": entitlement.get("cancels_at"),
        "billing_period": entitlement.get("billing_period"),
        "billing_currency": entitlement.get("billing_currency"),
        "is_delinquent": bool(entitlement.get("is_delinquent")),
        "discount_type": (entitlement.get("discount") or {}).get("discount_type"),
        "discount_amount": (entitlement.get("discount") or {}).get("amount"),
        "discount_duration_num_periods": (entitlement.get("discount") or {}).get("duration_num_periods"),
        "discount_expires_at": (entitlement.get("discount") or {}).get("discount_expires_at"),
        "discount_cancellation_policy": (entitlement.get("discount") or {}).get("cancellation_policy"),
        "discount_promo_campaign_id": (entitlement.get("discount") or {}).get("promo_campaign_id"),
        "last_purchase_origin_platform": last_sub.get("purchase_origin_platform"),
        "last_will_renew": bool(last_sub.get("will_renew")),
        "plus_trial_eligible": plus_trial_eligible,
        "plus_trial_campaign_id": (plus_campaign or {}).get("id"),
        "plus_trial_title": plus_meta.get("title"),
        "plus_trial_summary": plus_meta.get("summary"),
        "plus_trial_discount_percentage": discount.get("percentage"),
        "plus_trial_duration_num_periods": duration.get("num_periods"),
        "plus_trial_duration_period": duration.get("period"),
        "plus_trial_promotion_type_label": plus_meta.get("promotion_type_label"),
        "eligible_offer_ids": eligible_offer_ids,
        "features_count": len(item.get("features") or []),
        "can_access_with_session": bool(item.get("can_access_with_session")),
        "raw_account_plan_type": account.get("plan_type"),
    }
    result.update({k: v for k, v in claims.items() if k != "payload" and v is not None})
    return result


def _plan_check_settings(
    timeout: float | None,
    max_attempts: int | None,
    retry_delay: float | None,
) -> tuple[float, int, float]:
    from config import proxy as proxy_cfg

    timeout_value = timeout if timeout is not None else getattr(proxy_cfg, "PLAN_CHECK_TIMEOUT", 15.0)
    attempts_value = max_attempts if max_attempts is not None else getattr(proxy_cfg, "PLAN_CHECK_MAX_ATTEMPTS", 2)
    delay_value = retry_delay if retry_delay is not None else getattr(proxy_cfg, "PLAN_CHECK_RETRY_DELAY", 1.5)
    return (
        max(1.0, min(60.0, float(timeout_value or 15.0))),
        max(1, min(4, int(attempts_value or 1))),
        max(0.0, min(30.0, float(delay_value or 0.0))),
    )


def _retryable_plan_error(http_status: int | None) -> bool:
    if http_status is None:
        return True
    return http_status in {408, 409, 425, 429} or http_status >= 500


def _retry_wait_seconds(resp: Any, base_delay: float, attempt: int) -> float:
    try:
        retry_after = (getattr(resp, "headers", {}) or {}).get("retry-after")
        if retry_after is not None:
            return max(0.0, min(30.0, float(retry_after)))
    except (TypeError, ValueError):
        pass
    return max(0.0, min(30.0, base_delay * attempt))


def check_account_plan(
    token: str,
    *,
    proxy: Optional[str] = None,
    timezone_offset_min: str = "-",
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
    account_id: str | None = None,
    device_id: str | None = None,
    session_cookies: list[dict] | None = None,
    session: BrowserSession | None = None,
    billing_country: str = "",
    check_oaics: bool = False,
    check_country_qualification: bool = False,
    country_qualification_turnstile_token: str | None = None,
    preserve_proxy_session: bool = False,
) -> dict:
    token = normalize_token(token)
    if not token:
        return {"ok": False, "checked_at": now_iso(), "error": "token 为空"}
    claims = token_claims(token)
    if claims.get("token_expired") is True:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": "AT已过期/失效，请手动查活刷新",
            "needs_live_check": True,
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    try:
        route = resolve_plan_check_route(proxy)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"套餐查询网络配置错误: {exc}",
            **{k: v for k, v in claims.items() if k != "payload"},
        }
    route_meta = {k: v for k, v in route.items() if k != "proxy"}
    url = f"https://chatgpt.com{ACCOUNTS_CHECK_PATH}?timezone_offset_min={quote(str(timezone_offset_min))}"
    try:
        timeout_seconds, attempts, base_delay = _plan_check_settings(timeout, max_attempts, retry_delay)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"套餐查询重试配置错误: {exc}",
            "retryable": False,
            **route_meta,
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    last_result: dict | None = None
    for attempt in range(1, attempts + 1):
        env = session
        owns_session = session is None
        resp = None
        try:
            # 独立查询创建协议会话；查活后的即时校验可借用原会话保持 Cookie/IP 连续性。
            if env is None:
                session_proxy = route["proxy"] if preserve_proxy_session else rotate_proxy_session(route["proxy"])
                env = BrowserSession(proxy=session_proxy, detect_exit_geo=False)
            _restore_plan_session_context(
                env,
                device_id=device_id,
                session_cookies=session_cookies,
            )
            resp = env.get(
                url,
                headers=_common_headers(
                    env,
                    token,
                    account_id=account_id,
                    device_id=device_id,
                ),
                allow_redirects=False,
                timeout=timeout_seconds,
            )
            response_text = resp.text or ""
            http_status = int(resp.status_code)
            if not (200 <= http_status < 300):
                is_auth_expired = http_status == 401
                server_error_code = ""
                try:
                    error_payload = resp.json()
                    error_obj = error_payload.get("error") if isinstance(error_payload, dict) else None
                    if isinstance(error_obj, dict):
                        server_error_code = str(error_obj.get("code") or "")
                    if not server_error_code and isinstance(error_payload, dict):
                        detail = error_payload.get("detail")
                        if isinstance(detail, dict):
                            server_error_code = str(detail.get("code") or "")
                except Exception:
                    pass
                last_result = {
                    "ok": False,
                    "checked_at": now_iso(),
                    "http_status": http_status,
                    "error": "套餐接口认证失败（401），需要刷新登录态后重试" if is_auth_expired else f"HTTP {http_status}",
                    "response_preview": response_text[:500],
                    "retryable": _retryable_plan_error(http_status),
                    "token_expired": True if is_auth_expired else claims.get("token_expired"),
                    "needs_live_check": True if is_auth_expired else False,
                    "server_error_code": server_error_code or None,
                }
            else:
                try:
                    data: Any = resp.json()
                except Exception:
                    data = json.loads(response_text) if response_text.strip().startswith(("{", "[")) else None
                if not isinstance(data, dict):
                    last_result = {
                        "ok": False,
                        "checked_at": now_iso(),
                        "http_status": http_status,
                        "error": "响应不是 JSON 对象",
                        "response_preview": response_text[:500],
                        "retryable": True,
                    }
                else:
                    parsed = parse_accounts_check(data, token=token)
                    parsed["http_status"] = http_status
                    parsed["attempt_count"] = attempt
                    parsed["max_attempts"] = attempts
                    parsed["request_timeout"] = timeout_seconds
                    parsed["retryable"] = False
                    parsed.update(route_meta)
                    if check_oaics and str(parsed.get("current_plan_type") or "").lower() == "free":
                        parsed.update(check_oaics_eligibility(
                            env,
                            token,
                            account_id=account_id,
                            device_id=device_id,
                            billing_country=billing_country,
                            promo_campaign_id=parsed.get("plus_trial_campaign_id"),
                            timeout=timeout_seconds,
                        ))
                    elif check_oaics:
                        parsed.update({
                            "oaics_check_status": "skipped",
                            "oaics_checked_at": now_iso(),
                            "oaics_check_error": None,
                        })
                    if check_country_qualification:
                        qualification_checker = globals().get("check_country_qualification") or _check_country_qualification
                        parsed.update(qualification_checker(
                            env,
                            token,
                            timeout=timeout_seconds,
                            turnstile_token=country_qualification_turnstile_token,
                        ))
                    return parsed
        except Exception as exc:
            logger.debug("套餐查询失败: %s: %s", type(exc).__name__, exc, exc_info=True)
            last_result = {
                "ok": False,
                "checked_at": now_iso(),
                "http_status": int(resp.status_code) if resp is not None and getattr(resp, "status_code", None) else None,
                "error": f"{type(exc).__name__}: {exc}",
                "retryable": True,
            }
        finally:
            if owns_session and env is not None:
                try:
                    env.session.close()
                except Exception:
                    pass

        last_result = last_result or {"ok": False, "checked_at": now_iso(), "error": "未知错误", "retryable": True}
        last_result.update({
            "attempt_count": attempt,
            "max_attempts": attempts,
            "request_timeout": timeout_seconds,
            **route_meta,
            **{k: v for k, v in claims.items() if k != "payload"},
        })
        if not last_result.get("retryable") or attempt >= attempts:
            return last_result

        wait_seconds = _retry_wait_seconds(resp, base_delay, attempt)
        logger.warning(
            "套餐查询临时失败，第 %s/%s 次，%.1fs 后重试: %s",
            attempt,
            attempts,
            wait_seconds,
            last_result.get("error"),
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    return last_result or {
        "ok": False,
        "checked_at": now_iso(),
        "http_status": None,
        "error": "套餐查询未执行",
        "retryable": False,
        **route_meta,
        **{k: v for k, v in claims.items() if k != "payload"},
    }
