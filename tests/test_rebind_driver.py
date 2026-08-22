# -*- coding: utf-8 -*-
"""Focused contract tests for the two-stage rebind adapter."""
from __future__ import annotations

from dataclasses import dataclass

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
