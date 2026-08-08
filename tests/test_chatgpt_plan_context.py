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
