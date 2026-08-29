# -*- coding: utf-8 -*-
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import chatgpt_plan


class _Response:
    status_code = 200
    text = ""
    headers = {}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _plus_payload(account_id="account-1"):
    return {
        "accounts": {
            account_id: {
                "account": {"account_id": account_id, "plan_type": "plus"},
                "entitlement": {
                    "subscription_plan": "chatgptplusplan",
                    "has_active_subscription": True,
                },
            }
        }
    }


def _free_payload(account_id="account-1"):
    return {
        "accounts": {
            account_id: {
                "account": {"account_id": account_id, "plan_type": "free"},
                "entitlement": {
                    "subscription_plan": "chatgptfreeplan",
                    "has_active_subscription": False,
                },
            }
        }
    }


def test_plan_headers_include_account_id_and_saved_device():
    env = SimpleNamespace(
        device_id="random-device",
        get_chatgpt_headers=lambda **_kwargs: {"base": "1"},
        navigator_language=lambda: "zh-CN",
    )
    headers = chatgpt_plan._common_headers(
        env,
        "TOKEN",
        account_id="account-1",
        device_id="saved-device",
    )

    assert headers["chatgpt-account-id"] == "account-1"
    assert headers["oai-device-id"] == "saved-device"
    assert headers["x-openai-target-route"] == "/backend-api/accounts/check/{version}"


def test_plan_query_reuses_saved_session_context_and_parses_plus():
    env = SimpleNamespace(
        device_id="random-device",
        proxy="PROXY",
        session=SimpleNamespace(cookies=MagicMock(), close=MagicMock()),
        get_chatgpt_headers=lambda **_kwargs: {},
        navigator_language=lambda: "zh-CN",
        get=MagicMock(return_value=_Response(_plus_payload())),
    )
    with patch("core.chatgpt_plan.BrowserSession", return_value=env):
        result = chatgpt_plan.check_account_plan(
            "TOKEN",
            proxy="PROXY",
            account_id="account-1",
            device_id="saved-device",
            session_cookies=[{"name": "sid", "value": "COOKIE", "domain": ".chatgpt.com"}],
            max_attempts=1,
        )

    assert result["ok"] is True
    assert result["current_plan_type"] == "plus"
    assert result["has_active_subscription"] is True
    assert env.device_id == "saved-device"
    headers = env.get.call_args.kwargs["headers"]
    assert headers["chatgpt-account-id"] == "account-1"
    assert headers["oai-device-id"] == "saved-device"
    env.session.cookies.set.assert_any_call("sid", "COOKIE", domain=".chatgpt.com", path="/", secure=False)
    env.session.close.assert_called_once()


def test_free_plan_query_checks_oaics_checkout_session_prefix():
    env = SimpleNamespace(
        device_id="saved-device",
        proxy="PROXY",
        session=SimpleNamespace(cookies=MagicMock(), close=MagicMock()),
        get_chatgpt_headers=lambda **_kwargs: {},
        navigator_language=lambda: "zh-CN",
        get=MagicMock(return_value=_Response(_free_payload())),
        post=MagicMock(return_value=_Response({"checkout_session_id": "oaics_test"})),
    )
    with patch("core.chatgpt_plan.BrowserSession", return_value=env):
        result = chatgpt_plan.check_account_plan(
            "TOKEN",
            proxy="PROXY",
            account_id="account-1",
            device_id="saved-device",
            billing_country="US",
            check_oaics=True,
            max_attempts=1,
        )

    assert result["ok"] is True
    assert result["oaics_check_status"] == "success"
    assert result["oaics_eligible"] is True
    assert result["oaics_session_kind"] == "oaics"
    assert result["oaics_processor_entity"] == "openai_llc"
    checkout = env.post.call_args
    assert checkout.args[0].endswith("/backend-api/payments/checkout")
    assert checkout.kwargs["json"]["plan_name"] == "chatgptplusplan"
    assert checkout.kwargs["json"]["entry_point"] == "all_plans_pricing_modal"
    assert checkout.kwargs["json"]["billing_details"] == {"country": "US", "currency": "USD"}
    assert checkout.kwargs["json"]["promo_campaign"]["promo_campaign_id"] == "plus-1-month-free"
    assert checkout.kwargs["json"]["checkout_ui_mode"] == "hosted"


def test_free_plan_query_uses_oaics_website_country_protocol():
    env = SimpleNamespace(
        device_id="saved-device",
        proxy="PROXY",
        session=SimpleNamespace(cookies=MagicMock(), close=MagicMock()),
        get_chatgpt_headers=lambda **_kwargs: {},
        navigator_language=lambda: "zh-CN",
        get=MagicMock(return_value=_Response(_free_payload())),
        post=MagicMock(return_value=_Response({
            "query_count": 3,
            "results": [
                {"country": "JP", "country_name": "日本", "status": "eligible", "eligible": True, "message": "有资格"},
                {"country": "PH", "country_name": "菲律宾", "status": "not_eligible", "eligible": False, "message": "无资格"},
            ],
        })),
    )
    with patch("core.chatgpt_plan.BrowserSession", return_value=env):
        result = chatgpt_plan.check_account_plan(
            "TOKEN",
            proxy="PROXY",
            billing_country="US",
            check_oaics=True,
            max_attempts=1,
        )

    assert result["ok"] is True
    assert result["oaics_check_status"] == "success"
    assert result["oaics_eligible"] is True
    assert result["oaics_query_count"] == 3
    assert [item["country"] for item in result["oaics_country_results"]] == ["JP", "PH"]
    assert env.post.call_args.args[0] == "https://tools.oai9.com/api/trial/check"


def test_free_plan_keeps_plan_success_when_oaics_check_fails():
    failed = _Response({"error": "blocked"})
    failed.status_code = 403
    env = SimpleNamespace(
        device_id="saved-device",
        proxy="PROXY",
        session=SimpleNamespace(cookies=MagicMock(), close=MagicMock()),
        get_chatgpt_headers=lambda **_kwargs: {},
        navigator_language=lambda: "zh-CN",
        get=MagicMock(return_value=_Response(_free_payload())),
        post=MagicMock(return_value=failed),
    )
    with patch("core.chatgpt_plan.BrowserSession", return_value=env):
        result = chatgpt_plan.check_account_plan("TOKEN", proxy="PROXY", check_oaics=True, max_attempts=1)

    assert result["ok"] is True
    assert result["oaics_check_status"] == "failed"
    assert "403" in result["oaics_check_error"]


def test_oaics_checkout_failure_preserves_provider_detail_and_retryability():
    failed = _Response({"detail": "Our systems have detected unusual activity. Please try again later."})
    failed.status_code = 400
    env = SimpleNamespace(
        device_id="saved-device",
        proxy="PROXY",
        session=SimpleNamespace(cookies=MagicMock(), close=MagicMock()),
        get_chatgpt_headers=lambda **_kwargs: {},
        navigator_language=lambda: "zh-CN",
        get=MagicMock(return_value=_Response(_free_payload())),
        post=MagicMock(return_value=failed),
    )
    with patch("core.chatgpt_plan.BrowserSession", return_value=env):
        result = chatgpt_plan.check_account_plan(
            "TOKEN", proxy="PROXY", billing_country="JP", check_oaics=True, max_attempts=1
        )

    assert result["ok"] is True
    assert result["oaics_check_http_status"] == 400
    assert "unusual activity" in result["oaics_check_error"]
    assert result["oaics_check_retryable"] is True


def test_http_401_reports_backend_auth_failure_for_live_refresh():
    response = _Response({"error": {"code": "token_expired"}})
    response.status_code = 401
    response.text = '{"error":{"code":"token_expired"}}'
    env = SimpleNamespace(
        device_id="saved-device",
        proxy="PROXY",
        session=SimpleNamespace(cookies=MagicMock(), close=MagicMock()),
        get_chatgpt_headers=lambda **_kwargs: {},
        navigator_language=lambda: "zh-CN",
        get=MagicMock(return_value=response),
    )
    with patch("core.chatgpt_plan.BrowserSession", return_value=env):
        result = chatgpt_plan.check_account_plan(
            "TOKEN",
            proxy="PROXY",
            account_id="account-1",
            device_id="saved-device",
            max_attempts=1,
        )

    assert result["ok"] is False
    assert result["needs_live_check"] is True
    assert result["server_error_code"] == "token_expired"
    assert "认证失败" in result["error"]
