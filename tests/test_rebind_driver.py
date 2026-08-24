# -*- coding: utf-8 -*-
"""Focused contract tests for the two-stage rebind adapter."""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import ANY, MagicMock, patch

import pytest

from core import rebind_driver


OLD = "source@example.test"
TARGET = "target@example.test"


@dataclass
class FakeTransport:
    closed: bool = False

    def close(self):
        self.closed = True


def _account():
    return {"id": 7, "email": OLD, "registration_password": "fixture-password"}


def _target(**extra):
    value = {"email": TARGET, "source": "cloudflare_domain", "id": 11}
    value.update(extra)
    return value


def test_rebind_prefers_latest_account_proxy_when_task_has_no_override():
    captured = []

    def login_protocol(account, proxy, context, **_kwargs):
        captured.append(proxy)
        context.session = FakeTransport()
        context.session_info = {"user": {"email": OLD}, "accessToken": "old-token"}

    rebind_driver.rebind_account(
        {
            **_account(),
            "live_check_proxy_used": "socks5h://user:pass@live.example:3000",
        },
        _target(),
        driver="protocol",
        hooks={
            "login_protocol": login_protocol,
            "submit_protocol": lambda **_kwargs: {"ok": True},
            "verify": lambda target_email, **_kwargs: {
                "session": {"user": {"email": target_email}, "accessToken": "fresh-token"}
            },
        },
    )

    assert captured == ["socks5h://user:pass@live.example:3000"]


def test_explicit_empty_rebind_proxy_disables_saved_route():
    captured = []

    def login_protocol(account, proxy, context, **_kwargs):
        captured.append(proxy)
        context.session = FakeTransport()
        context.session_info = {"user": {"email": OLD}, "accessToken": "old-token"}

    rebind_driver.rebind_account(
        {
            **_account(),
            "live_check_proxy_used": "socks5h://user:pass@live.example:3000",
        },
        _target(),
        driver="protocol",
        proxy="",
        hooks={
            "login_protocol": login_protocol,
            "submit_protocol": lambda **_kwargs: {"ok": True},
            "verify": lambda target_email, **_kwargs: {
                "session": {"user": {"email": target_email}, "accessToken": "fresh-token"}
            },
        },
    )

    assert captured == [""]


def test_browser_login_retries_with_fallback_after_proxy_connection_failure(monkeypatch):
    calls = []
    driver = object()
    closer = lambda: None
    info = {"user": {"email": OLD}, "accessToken": "old-token"}

    def browser_login(account, *, driver_name, proxy, **_kwargs):
        calls.append(proxy)
        if len(calls) == 1:
            raise rebind_driver.RebindDriverError(
                "roxy 登录失败：WebDriverException: net::ERR_SOCKS_CONNECTION_FAILED"
            )
        return driver, closer, info

    monkeypatch.setattr(rebind_driver, "_browser_login_builtin", browser_login)
    monkeypatch.setattr(rebind_driver, "_rebind_proxy_fallbacks", lambda _failed: ["POOL", ""])

    result = rebind_driver.rebind_account(
        {**_account(), "live_check_proxy_used": "socks5h://user:pass@dead.example:3000"},
        _target(),
        driver="roxy",
        hooks={
            "submit_browser": lambda **_kwargs: {"ok": True},
            "verify": lambda target_email, **_kwargs: {
                "session": {"user": {"email": target_email}, "accessToken": "fresh-token"}
            },
        },
    )

    assert result["verified_email"] == TARGET
    assert calls == ["socks5h://user:pass@dead.example:3000", "POOL"]


def test_hooked_protocol_rebind_reads_target_otp_and_verifies_remote_session():
    calls = []
    transport = FakeTransport()

    def otp(email, after_ts, **_kwargs):
        calls.append(("otp", email, after_ts > 0))
        return "123456"

    def login_protocol(account, target, context, **_kwargs):
        calls.append(("login", account["email"], target["email"]))
        context.session = transport
        context.session_info = {"user": {"email": account["email"]}, "accessToken": "login-token"}

    def submit_protocol(target, get_otp, **_kwargs):
        calls.append(("submit", target["email"], get_otp(email=target["email"], after_ts=1)))
        return {"ok": True}

    def verify(target_email, **_kwargs):
        calls.append(("verify", target_email))
        return {"ok": True, "session": {"user": {"email": target_email}, "accessToken": "fresh-token"}}

    result = rebind_driver.rebind_account(
        _account(),
        _target(),
        login_driver="protocol",
        action_driver="protocol",
        hooks={
            "otp": otp,
            "login_protocol": login_protocol,
            "submit_protocol": submit_protocol,
            "verify": verify,
        },
    )

    assert result["ok"] is True
    assert result["verified_email"] == TARGET
    assert result["access_token"] == "fresh-token"
    assert result["session"]["user"]["email"] == TARGET
    assert calls == [
        ("login", OLD, TARGET),
        ("otp", TARGET, True),
        ("submit", TARGET, "123456"),
        ("verify", TARGET),
    ]
    assert transport.closed is False  # hooks own an injected transport unless they register a closer


def test_remote_verification_rejects_wrong_email_and_closes_registered_resource():
    transport = FakeTransport()
    closed = []

    def login(context, **_kwargs):
        context.session = transport
        context.session_info = {"user": {"email": OLD}, "accessToken": "old-token"}
        return {"close": lambda: closed.append(True)}

    with pytest.raises(rebind_driver.RebindVerificationError, match="邮箱"):
        rebind_driver.rebind_account(
            _account(),
            _target(),
            driver="protocol",
            hooks={
                "login_protocol": login,
                "submit_protocol": lambda **_kwargs: {"ok": True},
                "verify": lambda **_kwargs: {
                    "ok": True,
                    "session": {"user": {"email": "wrong@example.test"}, "accessToken": "fresh-token"},
                },
            },
        )
    assert closed == [True]


def test_missing_site_endpoint_fails_without_claiming_success():
    transport = FakeTransport()

    def login(context, **_kwargs):
        context.session = transport
        context.session_info = {"user": {"email": OLD}, "accessToken": "old-token"}

    with pytest.raises(rebind_driver.RebindDriverError, match="端点或提交钩子"):
        rebind_driver.rebind_account(_account(), _target(), driver="protocol", hooks={"login_protocol": login})


def test_browser_submission_without_endpoint_uses_account_settings_fallback(monkeypatch):
    calls = []

    def login(context, **_kwargs):
        context.driver = object()
        context.driver_kind = "roxy"
        context.session_info = {"user": {"email": OLD}, "accessToken": "old-token"}

    def browser_ui(context, otp_getter, log):
        calls.append((context.target["email"], callable(otp_getter), log is None))
        return {"ok": True, "browser_ui": True}

    monkeypatch.setattr(rebind_driver, "_browser_ui_action", browser_ui)
    result = rebind_driver.rebind_account(
        _account(),
        _target(),
        login_driver="roxy",
        action_driver="roxy",
        hybrid=False,
        hooks={
            "login_browser": login,
            "verify": lambda target_email, **_kwargs: {
                "session": {"user": {"email": target_email}, "accessToken": "fresh-token"}
            },
        },
    )

    assert result["verified_email"] == TARGET
    assert calls == [(TARGET, True, True)]


def test_protocol_action_without_endpoint_reuses_browser_login_without_bridge(monkeypatch):
    calls = []

    def login(context, **_kwargs):
        context.driver = object()
        context.driver_kind = "roxy"
        context.session_info = {"user": {"email": OLD}, "accessToken": "old-token"}

    def browser_ui(context, otp_getter, log):
        calls.append((context.action_driver, context.target["email"], callable(otp_getter), log is None))
        return {"ok": True, "browser_ui": True}

    def unexpected_protocol_bridge(*_args, **_kwargs):
        raise AssertionError("protocol bridge should not run without a protocol endpoint")

    monkeypatch.setattr(rebind_driver, "_browser_ui_action", browser_ui)
    monkeypatch.setattr(rebind_driver, "_ensure_protocol_transport", unexpected_protocol_bridge)
    result = rebind_driver.rebind_account(
        _account(),
        _target(),
        login_driver="roxy",
        action_driver="protocol",
        hybrid=True,
        hooks={
            "login_browser": login,
            "verify": lambda target_email, **_kwargs: {
                "session": {"user": {"email": target_email}, "accessToken": "fresh-token"}
            },
        },
    )

    assert result["verified_email"] == TARGET
    assert calls == [("protocol", TARGET, True, True)]


class FakeResponse:
    def __init__(self, status_code, data, url):
        self.status_code = status_code
        self._data = data
        self.url = url

    def json(self):
        return self._data


class FakeHttpSession(FakeTransport):
    def __init__(self):
        super().__init__()
        self.requests = []
        self._rebind_session_info = {}

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if url.endswith("/change"):
            return FakeResponse(200, {"verification_required": True, "verify_url": "/verify"}, url)
        return FakeResponse(200, {"verified": True}, url)


def test_configured_protocol_steps_send_target_and_otp_without_logging_code():
    transport = FakeHttpSession()
    logs = []

    def login(context, **_kwargs):
        context.session = transport
        context.session_info = {"user": {"email": OLD}, "accessToken": "old-token"}

    result = rebind_driver.rebind_account(
        _account(),
        _target(
            rebind_api={
                "endpoint": "/change",
                "otp_required": True,
                "verify_url": "/verify",
            }
        ),
        driver="protocol",
        log=logs.append,
        hooks={
            "login_protocol": login,
            "otp": lambda **_kwargs: "654321",
            "verify": lambda **_kwargs: {
                "session": {"user": {"email": TARGET}, "accessToken": "fresh-token"}
            },
        },
    )

    assert result["verified_email"] == TARGET
    assert [item[0].rsplit("/", 1)[-1] for item in transport.requests] == ["change", "verify"]
    assert TARGET in transport.requests[0][1]["data"]
    assert "654321" in transport.requests[1][1]["data"]
    assert all("654321" not in line for line in logs)


def test_browser_stage_hooks_are_supported_and_cleanup_is_called():
    cleaned = []

    def login(context, **_kwargs):
        context.driver = object()
        context.session_info = {"user": {"email": OLD}, "accessToken": "old-token"}
        return {"close": lambda: cleaned.append("login")}

    result = rebind_driver.rebind_account(
        _account(),
        _target(),
        login_driver="cloak",
        action_driver="roxy",
        hybrid=True,
        hooks={
            "login_browser": login,
            "submit_browser": lambda **_kwargs: {"ok": True},
            "verify": lambda **_kwargs: {
                "verified_email": TARGET,
                "access_token": "fresh-token",
                "session": {"user": {"email": TARGET}, "accessToken": "fresh-token"},
            },
        },
    )

    assert result["login_driver"] == "cloak"
    assert result["action_driver"] == "roxy"
    assert result["hybrid"] is True
    assert cleaned == ["login"]


def test_non_hybrid_stage_config_uses_one_action_driver_for_login_and_submit():
    calls = []

    def login_protocol(context, **_kwargs):
        calls.append("protocol-login")
        context.session_info = {"user": {"email": OLD}, "accessToken": "old-token"}

    def submit_protocol(**_kwargs):
        calls.append("protocol-submit")
        return {"ok": True}

    result = rebind_driver.rebind_account(
        _account(),
        _target(),
        login_driver="cloak",
        action_driver="protocol",
        hybrid=False,
        hooks={
            "login_protocol": login_protocol,
            "submit_protocol": submit_protocol,
            "verify": lambda **_kwargs: {
                "verified_email": TARGET,
                "access_token": "fresh-token",
                "session": {"user": {"email": TARGET}, "accessToken": "fresh-token"},
            },
        },
    )

    assert result["login_driver"] == "protocol"
    assert result["action_driver"] == "protocol"
    assert result["hybrid"] is False
    assert calls == ["protocol-login", "protocol-submit"]


def test_browser_settings_totp_gate_is_completed_before_new_email_form():
    context = rebind_driver.RebindContext(
        account={**_account(), "totp_secret": "JBSWY3DPEHPK3PXP"},
        target=_target(),
        login_driver="roxy",
        action_driver="roxy",
        hybrid=False,
        driver=MagicMock(),
    )
    states = [
        {"buttons": [{"attrs": "account-info-email"}]},
        {"inputs": [{"type": "password", "attrs": "current password"}]},
        {
            "text": "Check your authenticator app Enter the one-time authentication code",
            "inputs": [{"type": "text", "attrs": "numeric code"}],
        },
        {"inputs": [{"type": "email", "attrs": "new email"}]},
        {"text": "Enter verification code", "inputs": [{"type": "text", "attrs": "verification code"}]},
    ]
    with patch("core.rebind_driver._wait_browser_state", side_effect=states), patch(
        "core.rebind_driver._submit_browser_email_form", return_value={"ok": True}
    ), patch("core.browser_liveness._fill_login_password"), patch(
        "core.roxy_registration._clear_otp_inputs"
    ) as clear_otp, patch("core.roxy_registration._type_otp") as type_otp, patch(
        "core.roxy_registration._click_continue"
    ) as click_continue, patch(
        "core.roxy_registration._wait_after_email_otp_submit", return_value="accepted"
    ), patch("core.account_export._totp_code_with_margin", return_value="123456") as totp_code, patch(
        "core.roxy_registration._type_email_address"
    ):
        result = rebind_driver._browser_ui_action(
            context,
            otp_getter=lambda **_kwargs: "654321",
            log=None,
        )

    assert result["submitted_email"] == TARGET
    totp_code.assert_called_once_with(ANY, force_next=False)
    assert [call.args[1] for call in type_otp.call_args_list] == ["123456", "654321"]
    assert clear_otp.call_count == 2
    assert click_continue.call_count == 2


def test_browser_settings_reopens_after_totp_returns_to_logged_in_shell():
    """A successful re-auth can land on the ChatGPT shell before settings reloads."""
    context = rebind_driver.RebindContext(
        account={**_account(), "totp_secret": "JBSWY3DPEHPK3PXP"},
        target=_target(),
        login_driver="roxy",
        action_driver="roxy",
        hybrid=False,
        driver=MagicMock(),
    )
    states = [
        {"buttons": [{"attrs": "account-info-email"}]},
        {"inputs": [{"type": "password", "attrs": "current password"}]},
        {
            "text": "Check your authenticator app Enter the one-time authentication code",
            "inputs": [{"type": "text", "attrs": "numeric code"}],
        },
        {"url": "https://chatgpt.com/", "text": "Skip to content Open sidebar New chat", "buttons": []},
        {"buttons": [{"attrs": "account-info-email"}]},
        {"inputs": [{"type": "email", "attrs": "new email"}]},
        {"text": "Enter verification code", "inputs": [{"type": "text", "attrs": "verification code"}]},
    ]
    with patch("core.rebind_driver._wait_browser_state", side_effect=states), patch(
        "core.rebind_driver._submit_browser_email_form", return_value={"ok": True}
    ), patch("core.browser_liveness._fill_login_password"), patch(
        "core.roxy_registration._clear_otp_inputs"
    ), patch("core.roxy_registration._type_otp") as type_otp, patch(
        "core.roxy_registration._click_continue"
    ), patch("core.roxy_registration._wait_after_email_otp_submit", return_value="accepted"), patch(
        "core.account_export._totp_code_with_margin", return_value="123456"
    ), patch("core.roxy_registration._type_email_address"):
        result = rebind_driver._browser_ui_action(
            context,
            otp_getter=lambda **_kwargs: "654321",
            log=None,
        )

    assert result["submitted_email"] == TARGET
    assert [call.args[1] for call in type_otp.call_args_list] == ["123456", "654321"]
