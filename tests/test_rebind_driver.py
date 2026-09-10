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


def test_rebind_uses_proxy_pool_instead_of_account_saved_route(monkeypatch):
    captured = []
    monkeypatch.setattr("config.proxy.pick_proxy", lambda: "socks5h://pool.example:4000")

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

    assert captured == ["socks5h://pool.example:4000"]


def test_explicit_empty_rebind_proxy_still_uses_proxy_pool(monkeypatch):
    captured = []
    monkeypatch.setattr("config.proxy.pick_proxy", lambda: "socks5h://pool.example:4000")

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

    assert captured == ["socks5h://pool.example:4000"]


def test_explicit_rebind_proxy_overrides_pool(monkeypatch):
    captured = []
    monkeypatch.setattr("config.proxy.pick_proxy", lambda: "socks5h://pool.example:4000")

    def login_protocol(account, proxy, context, **_kwargs):
        captured.append(proxy)
        context.session = FakeTransport()
        context.session_info = {"user": {"email": OLD}, "accessToken": "old-token"}

    rebind_driver.rebind_account(
        _account(),
        _target(),
        driver="protocol",
        proxy="socks5h://custom.example:5000",
        hooks={
            "login_protocol": login_protocol,
            "submit_protocol": lambda **_kwargs: {"ok": True},
            "verify": lambda target_email, **_kwargs: {
                "session": {"user": {"email": target_email}, "accessToken": "fresh-token"}
            },
        },
    )

    assert captured == ["socks5h://custom.example:5000"]


def test_rebind_requires_non_empty_proxy_pool(monkeypatch):
    monkeypatch.setattr("config.proxy.pick_proxy", lambda: "")
    monkeypatch.setattr("config.proxy.PROXY_POOL", [])

    with pytest.raises(rebind_driver.RebindDriverError, match="PROXY_POOL 为空"):
        rebind_driver.rebind_account(
            _account(),
            _target(),
            driver="protocol",
            hooks={
                "login_protocol": lambda **_kwargs: None,
                "submit_protocol": lambda **_kwargs: {"ok": True},
            },
        )


def test_browser_login_retries_with_fallback_after_proxy_connection_failure(monkeypatch):
    calls = []
    driver = object()
    closer = lambda: None
    info = {"user": {"email": OLD}, "loginConfirmed": True, "loginConfirmation": "browser_ui"}

    def browser_login(account, *, driver_name, proxy, **_kwargs):
        calls.append(proxy)
        if len(calls) == 1:
            raise rebind_driver.RebindDriverError(
                "roxy 登录失败：WebDriverException: net::ERR_SOCKS_CONNECTION_FAILED"
            )
        return driver, closer, info

    monkeypatch.setattr(rebind_driver, "_browser_login_builtin", browser_login)
    monkeypatch.setattr(rebind_driver, "_rebind_proxy_fallbacks", lambda _failed: ["POOL"])

    result = rebind_driver.rebind_account(
        _account(),
        _target(),
        driver="roxy",
        proxy="socks5h://user:pass@dead.example:3000",
        hooks={
            "submit_browser": lambda **_kwargs: {"ok": True},
            "verify": lambda target_email, **_kwargs: {
                "session": {"user": {"email": target_email}, "accessToken": "fresh-token"}
            },
        },
    )

    assert result["verified_email"] == TARGET
    assert calls == ["socks5h://user:pass@dead.example:3000", "POOL"]


def test_browser_rebind_forces_fresh_full_login_and_ignores_factory_session(monkeypatch):
    driver = MagicMock()
    driver._rebind_session_info = {"user": {"email": OLD}, "accessToken": "stale-token"}
    closer = MagicMock()
    fresh = {"user": {"email": OLD}, "loginConfirmed": True, "loginConfirmation": "browser_ui"}
    monkeypatch.setattr(
        rebind_driver,
        "_open_browser_builtin",
        lambda *_args, **_kwargs: (driver, closer),
    )

    with patch("core.browser_liveness._browser_login", return_value=fresh) as browser_login:
        actual_driver, actual_closer, info = rebind_driver._browser_login_builtin(
            _account(),
            driver_name="roxy",
            proxy="POOL",
            headless=True,
            hooks={},
            log=None,
        )

    assert actual_driver is driver
    assert actual_closer is closer
    assert info == fresh
    browser_login.assert_called_once_with(
        driver,
        _account(),
        OLD,
        headless=True,
        restore_saved_session=False,
        progress=ANY,
        require_session=False,
    )


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


def test_rebind_failure_cleans_browser_resources_even_in_debug_mode(monkeypatch):
    closed = []
    monkeypatch.setenv("REBIND_DEBUG_KEEP_BROWSER_ON_FAILURE", "1")

    def close(*, failed=False):
        closed.append(failed)

    def login(context, **_kwargs):
        context.driver = object()
        context.driver_kind = "roxy"
        context.session_info = {
            "user": {"email": OLD},
            "loginConfirmed": True,
        }
        return {"close": close}

    with pytest.raises(rebind_driver.RebindVerificationError, match="邮箱"):
        rebind_driver.rebind_account(
            _account(),
            _target(),
            login_driver="roxy",
            action_driver="roxy",
            hybrid=False,
            hooks={
                "login_browser": login,
                "submit_browser": lambda **_kwargs: {"ok": True},
                "verify": lambda **_kwargs: {
                    "session": {
                        "user": {"email": "wrong@example.test"},
                        "accessToken": "fresh-token",
                    }
                },
            },
        )

    assert closed == [True]


class FakeBuiltinEmailChangeSession(FakeTransport):
    def __init__(self, *, eligibility_type="password"):
        super().__init__()
        self.eligibility_type = eligibility_type
        self.requests = []
        self._rebind_session_info = {}

    def get_chatgpt_headers(self, referer):
        return {"x-test-browser-profile": "1", "referer": referer}

    def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        return FakeResponse(
            200,
            {"eligible": True, "eligibility_type": self.eligibility_type},
            url,
        )

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return FakeResponse(200, {"ok": True}, url)


def test_builtin_protocol_rebind_uses_email_change_endpoints_without_site_config():
    transport = FakeBuiltinEmailChangeSession()
    logs = []

    def login(context, **_kwargs):
        context.session = transport
        context.session_info = {"user": {"email": OLD}, "accessToken": "old-token"}

    result = rebind_driver.rebind_account(
        _account(),
        _target(),
        driver="protocol",
        log=logs.append,
        hooks={
            "login_protocol": login,
            "otp": lambda **_kwargs: "654321",
            "verify": lambda target_email, **_kwargs: {
                "session": {"user": {"email": target_email}, "accessToken": "fresh-token"}
            },
        },
    )

    assert result["verified_email"] == TARGET
    assert [item[1].rsplit("/", 1)[-1] for item in transport.requests] == [
        "eligibility",
        "begin",
        "verify",
    ]
    assert TARGET in transport.requests[1][2]["data"]
    assert "654321" in transport.requests[2][2]["data"]
    assert transport.requests[0][2]["headers"]["x-test-browser-profile"] == "1"
    assert all("654321" not in line for line in logs)


def test_protocol_preflight_rotates_failed_proxy_within_pool(monkeypatch):
    calls = []
    logs = []

    def preflight(email, proxy, **kwargs):
        calls.append((email, proxy, kwargs))
        if proxy == "DEAD":
            raise RuntimeError("ProxyError: curl: (97) SOCKS5 connection failed")
        return "live-session", "authorize-url"

    monkeypatch.setattr(rebind_driver, "_rebind_proxy_fallbacks", lambda _failed: ["POOL"])
    monkeypatch.setattr(
        "core.account_liveness._network_preflight_with_retry",
        preflight,
    )

    result = rebind_driver._protocol_preflight_with_fallback(
        OLD,
        "DEAD",
        log=logs.append,
    )

    assert result == ("live-session", "authorize-url")
    assert [item[1] for item in calls] == ["DEAD", "POOL"]
    assert all(
        item[2]
        == {
            "max_attempts": 2,
            "rotate_proxy_on_retry": True,
            "screen_hint": "login",
        }
        for item in calls
    )
    assert any("轮换代理池出口" in line for line in logs)


@pytest.mark.parametrize("message", [
    "HTTP Error 403: ",
    "HTTP Error 429: Too Many Requests",
    "HTTP Error 503: Service Unavailable",
])
def test_protocol_preflight_rotates_challenged_proxy_within_pool(monkeypatch, message):
    calls = []

    def preflight(_email, proxy, **_kwargs):
        calls.append(proxy)
        if proxy == "CHALLENGED":
            raise RuntimeError(message)
        return "live-session", "authorize-url"

    monkeypatch.setattr(rebind_driver, "_rebind_proxy_fallbacks", lambda _failed: ["POOL"])
    monkeypatch.setattr("core.account_liveness._network_preflight_with_retry", preflight)

    result = rebind_driver._protocol_preflight_with_fallback(
        OLD,
        "CHALLENGED",
        log=None,
    )

    assert result == ("live-session", "authorize-url")
    assert calls == ["CHALLENGED", "POOL"]


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


def test_new_email_submit_scopes_actions_to_dialog_before_inner_form():
    driver = MagicMock()

    def execute(script, *_args):
        assert "let localScope = dialog || form" in script
        assert "submit = usable[usable.length - 1]" in script
        return {
            "ok": True,
            "strategy": "dialog_primary_element",
            "element": {"tag": "BUTTON", "type": "", "attrs": "btn-primary"},
        }

    driver.execute_script.side_effect = execute

    result = rebind_driver._submit_browser_email_form(driver, timeout=1)

    assert result == {
        "ok": True,
        "strategy": "dialog_primary_element",
        "element": {"tag": "BUTTON", "type": "", "attrs": "btn-primary"},
    }


def test_browser_protocol_get_does_not_send_json_body(monkeypatch):
    captured = {}

    def browser_fetch(_driver, _url, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "status": 200, "url": "https://chatgpt.com/check", "data": {"eligible": True}}

    monkeypatch.setattr("core.account_export._browser_fetch", browser_fetch)
    result = rebind_driver._browser_request(
        object(),
        {"base_url": "https://chatgpt.com"},
        method="GET",
        url="/check",
    )

    assert result["status"] == 200
    assert captured["method"] == "GET"
    assert captured["body"] is None


def test_protocol_action_without_endpoint_uses_browser_http_and_skips_settings_dom(monkeypatch):
    calls = []
    transport = FakeBuiltinEmailChangeSession()

    class FakeDriver:
        def execute_script(self, _script):
            return "en-US"

    driver = FakeDriver()

    def login(context, **_kwargs):
        context.driver = driver
        context.driver_kind = "roxy"
        context.session_info = {"user": {"email": OLD}, "loginConfirmed": True}

    def browser_request(actual_driver, spec, **kwargs):
        calls.append(("http", actual_driver is driver, kwargs["url"]))
        return rebind_driver._protocol_request(transport, spec, **kwargs)

    monkeypatch.setattr(
        "core.account_export._browser_session_info",
        lambda _driver: {"user": {"email": OLD}, "accessToken": "old-token"},
    )
    monkeypatch.setattr("core.account_export._browser_device_id", lambda _driver: "device-id")
    monkeypatch.setattr(rebind_driver, "_browser_request", browser_request)
    monkeypatch.setattr(
        rebind_driver,
        "_ensure_protocol_transport",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("mixed built-in path must not bridge")),
    )
    monkeypatch.setattr(
        rebind_driver,
        "_browser_ui_action",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Settings DOM must not be used")),
    )
    result = rebind_driver.rebind_account(
        _account(),
        _target(),
        login_driver="roxy",
        action_driver="protocol",
        hybrid=True,
        hooks={
            "login_browser": login,
            "otp": lambda **_kwargs: "654321",
            "verify": lambda target_email, **_kwargs: {
                "session": {"user": {"email": target_email}, "accessToken": "fresh-token"}
            },
        },
    )

    assert result["verified_email"] == TARGET
    assert [item[2].rsplit("/", 1)[-1] for item in calls] == ["eligibility", "begin", "verify"]
    assert all(item[1] for item in calls)
    assert [item[1].rsplit("/", 1)[-1] for item in transport.requests] == [
        "eligibility",
        "begin",
        "verify",
    ]


def test_mixed_protocol_reauthenticates_once_when_begin_returns_401(monkeypatch):
    calls = []

    class FakeDriver:
        def execute_script(self, _script):
            return "en-US"

    driver = FakeDriver()

    def login(context, **_kwargs):
        context.driver = driver
        context.driver_kind = "roxy"
        context.session_info = {"user": {"email": OLD}, "loginConfirmed": True}

    def browser_request(_driver, _spec, *, method, url, payload=None):
        calls.append((method, url))
        if url.endswith("/eligibility"):
            return {"status": 200, "data": {"eligible": True, "eligibility_type": "password"}}
        if url.endswith("/begin") and sum(item[1].endswith("/begin") for item in calls) == 1:
            raise rebind_driver.RebindHttpError(401, data={"error": "reauth_required"})
        return {"status": 200, "data": {"ok": True}}

    monkeypatch.setattr(
        "core.account_export._browser_session_info",
        lambda _driver: {"user": {"email": OLD}, "accessToken": "old-token"},
    )
    monkeypatch.setattr("core.account_export._browser_device_id", lambda _driver: "device-id")
    monkeypatch.setattr(rebind_driver, "_browser_request", browser_request)
    monkeypatch.setattr(
        "core.browser_liveness._browser_login",
        lambda *_args, **_kwargs: calls.append(("LOGIN", "reauth")) or {"loginConfirmed": True},
    )
    result = rebind_driver.rebind_account(
        _account(),
        _target(),
        login_driver="roxy",
        action_driver="protocol",
        hybrid=True,
        hooks={
            "login_browser": login,
            "otp": lambda **_kwargs: "654321",
            "verify": lambda target_email, **_kwargs: {
                "session": {"user": {"email": target_email}, "accessToken": "fresh-token"}
            },
        },
    )

    assert result["verified_email"] == TARGET
    assert calls.count(("POST", "/backend-api/accounts/change_email/begin")) == 2
    assert calls.count(("LOGIN", "reauth")) == 1


def test_builtin_mixed_rebind_logs_in_with_new_email_before_final_verification(monkeypatch):
    captured = {}
    driver = object()
    context = rebind_driver.RebindContext(
        account=_account(),
        target=_target(),
        login_driver="roxy",
        action_driver="protocol",
        hybrid=True,
        driver=driver,
    )

    def browser_login(actual_driver, account, email, **kwargs):
        captured.update({"driver": actual_driver, "account": account, "email": email, **kwargs})
        return {"user": {"email": TARGET}, "accessToken": "fresh-token"}

    monkeypatch.setattr("core.browser_liveness._browser_login", browser_login)
    rebind_driver._refresh_session_after_builtin_rebind(
        context,
        target_email=TARGET,
        otp_getter=lambda **_kwargs: "654321",
        hooks={},
        headless=False,
        log=None,
    )

    assert captured["driver"] is driver
    assert captured["account"]["email"] == TARGET
    assert captured["email"] == TARGET
    assert captured["restore_saved_session"] is False
    assert captured["require_session"] is True
    assert context.session_info["user"]["email"] == TARGET


def test_builtin_pure_protocol_rebind_replaces_revoked_session(monkeypatch):
    old_session = FakeTransport()
    new_session = FakeTransport()
    context = rebind_driver.RebindContext(
        account=_account(),
        target=_target(),
        login_driver="protocol",
        action_driver="protocol",
        hybrid=False,
        session=old_session,
        proxy="POOL",
    )
    monkeypatch.setattr(
        rebind_driver,
        "_protocol_login_builtin",
        lambda account, **_kwargs: (
            new_session,
            {"user": {"email": account["email"]}, "accessToken": "fresh-token"},
        ),
    )

    rebind_driver._refresh_session_after_builtin_rebind(
        context,
        target_email=TARGET,
        otp_getter=lambda **_kwargs: "654321",
        hooks={},
        headless=False,
        log=None,
    )

    assert old_session.closed is True
    assert context.session is new_session
    assert context.session_info["user"]["email"] == TARGET


class FakeResponse:
    def __init__(self, status_code, data, url, *, headers=None, text=""):
        self.status_code = status_code
        self._data = data
        self.url = url
        self.headers = dict(headers or {})
        self.text = text

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


def test_protocol_login_accepts_legacy_password_and_totp_fields(monkeypatch):
    calls = []
    logs = []
    fake_session = FakeTransport()

    monkeypatch.setattr(
        rebind_driver,
        "_protocol_preflight_with_fallback",
        lambda email, proxy, log=None: (fake_session, "https://auth.example.test/authorize"),
    )
    monkeypatch.setattr(
        "core.openai_auth.follow_authorize",
        lambda *_args, **_kwargs: "https://auth.openai.com/log-in",
    )
    monkeypatch.setattr(
        "core.openai_auth.continue_authorize_with_email",
        lambda _session, email, **kwargs: calls.append(("email", email, kwargs.get("screen_hint")))
        or {
            "continue_url": "https://auth.openai.com/log-in/password",
            "page": {"type": "login_password"},
        },
    )
    monkeypatch.setattr(
        "core.account_liveness._navigate_auth_step",
        lambda *_args, **_kwargs: "https://auth.openai.com/log-in/password",
    )
    monkeypatch.setattr(
        "core.openai_auth.verify_login_password",
        lambda _session, password: calls.append(("password", password))
        or {"mfa_factors": [{"id": "factor", "factor_type": "totp"}]},
    )
    monkeypatch.setattr(
        "core.openai_auth.issue_mfa_challenge",
        lambda *_args, **_kwargs: {"mfa_request_id": "challenge"},
    )
    monkeypatch.setattr(
        "core.openai_auth.verify_mfa_code",
        lambda _session, _factor, code: calls.append(("totp", code))
        or {"continue_url": "https://auth.openai.com/authorize/continue"},
    )
    monkeypatch.setattr("core.account_export.follow_oauth_callback", lambda *_args, **_kwargs: "https://chatgpt.com/")
    monkeypatch.setattr(
        rebind_driver,
        "_fetch_protocol_session",
        lambda _session: {"user": {"email": OLD}, "accessToken": "login-token"},
    )

    session, info = rebind_driver._protocol_login_builtin(
        {
            "email": OLD,
            "password": "legacy-chatgpt-password",
            "twofa": "JBSWY3DPEHPK3PXP",
        },
        proxy="POOL",
        otp_getter=lambda **_kwargs: calls.append("get_email_otp") or "654321",
        log=logs.append,
        hooks={},
    )

    assert session is fake_session
    assert info["user"]["email"] == OLD
    assert calls[0] == ("email", OLD, "login")
    assert calls[1] == ("password", "legacy-chatgpt-password")
    assert calls[2][0] == "totp"
    assert "get_email_otp" not in calls
    assert not any("原邮箱验证码" in message for message in logs)


def test_protocol_login_password_failure_does_not_fall_back_to_source_email_otp(monkeypatch):
    calls = []
    fake_session = FakeTransport()

    monkeypatch.setattr(
        rebind_driver,
        "_protocol_preflight_with_fallback",
        lambda email, proxy, log=None: (fake_session, "https://auth.example.test/authorize"),
    )
    monkeypatch.setattr(
        "core.openai_auth.follow_authorize",
        lambda *_args, **_kwargs: "https://auth.openai.com/log-in/password",
    )

    def verify_password(_session, _password):
        calls.append("password")
        raise RuntimeError("auth step is email verification")

    monkeypatch.setattr("core.openai_auth.verify_login_password", verify_password)
    monkeypatch.setattr("core.openai_auth.send_email_otp", lambda *_args, **_kwargs: calls.append("send_otp"))
    with pytest.raises(rebind_driver.RebindDriverError, match="未切换原邮箱验证码"):
        rebind_driver._protocol_login_builtin(
            {"email": OLD, "registration_password": "pw"},
            proxy="POOL",
            otp_getter=lambda **_kwargs: calls.append("get_otp") or "123456",
            log=None,
            hooks={},
        )

    assert calls == ["password"]
    assert fake_session.closed is True


def test_protocol_login_uses_source_email_otp_only_when_server_explicitly_requests_it(monkeypatch):
    calls = []
    fake_session = FakeTransport()
    monkeypatch.setattr(
        rebind_driver,
        "_protocol_preflight_with_fallback",
        lambda email, proxy, log=None: (fake_session, "https://auth.example.test/authorize"),
    )
    monkeypatch.setattr(
        "core.openai_auth.follow_authorize",
        lambda *_args, **_kwargs: "https://auth.openai.com/email-verification",
    )
    monkeypatch.setattr("core.openai_auth.verify_login_password", lambda *_args: calls.append("password"))
    monkeypatch.setattr("core.openai_auth.send_email_otp", lambda *_args, **_kwargs: calls.append("send_otp"))
    monkeypatch.setattr(
        "core.openai_auth.validate_email_otp",
        lambda _session, code: calls.append(("validate_otp", code))
        or {"continue_url": "https://auth.openai.com/authorize/continue"},
    )
    monkeypatch.setattr("core.account_export.follow_oauth_callback", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        rebind_driver,
        "_fetch_protocol_session",
        lambda _session: {"user": {"email": OLD}, "accessToken": "login-token"},
    )

    session, _info = rebind_driver._protocol_login_builtin(
        {"email": OLD, "registration_password": "pw"},
        proxy="POOL",
        otp_getter=lambda **_kwargs: calls.append("get_otp") or "123456",
        log=None,
        hooks={},
    )

    assert session is fake_session
    assert calls == ["send_otp", "get_otp", ("validate_otp", "123456")]


def test_protocol_login_rejects_unknown_auth_state_without_sending_otp(monkeypatch):
    calls = []
    fake_session = FakeTransport()
    monkeypatch.setattr(
        rebind_driver,
        "_protocol_preflight_with_fallback",
        lambda email, proxy, log=None: (fake_session, "https://auth.example.test/authorize"),
    )
    monkeypatch.setattr("core.openai_auth.follow_authorize", lambda *_args, **_kwargs: "https://auth.openai.com/log-in")
    monkeypatch.setattr("core.openai_auth.continue_authorize_with_email", lambda *_args, **_kwargs: {"page": {"type": "mystery"}})
    monkeypatch.setattr("core.openai_auth.send_email_otp", lambda *_args, **_kwargs: calls.append("send_otp"))

    with pytest.raises(rebind_driver.RebindDriverError, match="未识别服务端登录步骤"):
        rebind_driver._protocol_login_builtin(
            {"email": OLD, "registration_password": "pw"},
            proxy="POOL",
            otp_getter=lambda **_kwargs: calls.append("get_otp") or "123456",
            log=None,
            hooks={},
        )

    assert calls == []
    assert fake_session.closed is True


def test_builtin_protocol_rebind_falls_back_to_add_email_routes(monkeypatch):
    transport = FakeBuiltinEmailChangeSession()
    calls = []

    def request(_transport, _spec, *, method, url, payload=None):
        calls.append((method, url, payload))
        if url.endswith("/eligibility"):
            raise rebind_driver.RebindHttpError(404, data={"error": "missing"})
        if url.endswith("change_email/begin") or url.endswith("change_email/verify"):
            raise rebind_driver.RebindHttpError(404, data={"error": "missing"})
        if url.endswith("add_email/begin"):
            return {"status": 200, "data": {"ok": True}}
        return {"status": 200, "data": {"verified": True}}

    monkeypatch.setattr(rebind_driver, "_protocol_request", request)
    context = rebind_driver.RebindContext(
        account=_account(),
        target=_target(),
        login_driver="protocol",
        action_driver="protocol",
        hybrid=False,
        session=transport,
        session_info={"user": {"email": OLD}, "accessToken": "old-token"},
    )
    result = rebind_driver._builtin_chatgpt_protocol_action(
        context,
        otp_getter=lambda **_kwargs: "654321",
        log=None,
    )

    assert result["ok"] is True
    assert [url for _, url, _ in calls if url.endswith("/begin")] == [
        "/backend-api/accounts/change_email/begin",
        "/backend-api/accounts/add_email/begin",
    ]
    assert any(url.endswith("/add_email/verify") for _, url, _ in calls)


def test_builtin_protocol_rebind_retries_target_email_send_with_sentinel(monkeypatch):
    transport = FakeBuiltinEmailChangeSession()
    transport.blocked_until = 0.0
    transport.blocked_reason = ""
    requests = []
    otp_emails = []

    def request(_transport, spec, *, method, url, payload=None):
        has_sentinel = bool((spec.get("headers") or {}).get("openai-sentinel-token"))
        requests.append((method, url, dict(payload or {}), has_sentinel))
        if url.endswith("/eligibility"):
            return {"status": 200, "data": {"eligible": True, "eligibility_type": "password"}}
        if url.endswith("/begin") and not has_sentinel:
            transport.blocked_until = 9999999999.0
            transport.blocked_reason = "HTTP 403"
            raise rebind_driver.RebindHttpError(403, data={"error": {"code": "challenge_required"}})
        return {"status": 200, "data": {"ok": True}}

    def sentinel(active_transport, _flow):
        assert active_transport.blocked_until == 0.0
        assert active_transport.blocked_reason == ""
        return {"token": "challenge"}

    monkeypatch.setattr(rebind_driver, "_protocol_request", request)
    monkeypatch.setattr("core.openai_auth.request_sentinel_token", sentinel)
    monkeypatch.setattr(
        "core.openai_auth.build_sentinel_header",
        lambda *_args: ("sentinel-proof", "sentinel-so-proof"),
    )
    context = rebind_driver.RebindContext(
        account=_account(),
        target=_target(),
        login_driver="protocol",
        action_driver="protocol",
        hybrid=False,
        session=transport,
        session_info={"user": {"email": OLD}, "accessToken": "old-token"},
    )

    result = rebind_driver._builtin_chatgpt_protocol_action(
        context,
        otp_getter=lambda email, **_kwargs: otp_emails.append(email) or "654321",
        log=None,
    )

    begin_requests = [item for item in requests if item[1].endswith("/begin")]
    assert result["ok"] is True
    assert len(begin_requests) == 2
    assert begin_requests[0][2] == begin_requests[1][2] == {"email": TARGET}
    assert [item[3] for item in begin_requests] == [False, True]
    assert otp_emails == [TARGET]


def test_builtin_protocol_rebind_retries_target_code_without_fetching_a_new_code(monkeypatch):
    transport = FakeBuiltinEmailChangeSession()
    requests = []
    otp_emails = []

    def request(_transport, spec, *, method, url, payload=None):
        has_sentinel = bool((spec.get("headers") or {}).get("openai-sentinel-token"))
        requests.append((method, url, dict(payload or {}), has_sentinel))
        if url.endswith("/eligibility"):
            return {"status": 200, "data": {"eligible": True, "eligibility_type": "password"}}
        if url.endswith("/verify") and not has_sentinel:
            raise rebind_driver.RebindHttpError(403, data={"error": {"code": "challenge_required"}})
        return {"status": 200, "data": {"ok": True}}

    monkeypatch.setattr(rebind_driver, "_protocol_request", request)
    monkeypatch.setattr("core.openai_auth.request_sentinel_token", lambda *_args: {"token": "challenge"})
    monkeypatch.setattr("core.openai_auth.build_sentinel_header", lambda *_args: ("sentinel-proof", None))
    context = rebind_driver.RebindContext(
        account=_account(),
        target=_target(),
        login_driver="protocol",
        action_driver="protocol",
        hybrid=False,
        session=transport,
        session_info={"user": {"email": OLD}, "accessToken": "old-token"},
    )

    result = rebind_driver._builtin_chatgpt_protocol_action(
        context,
        otp_getter=lambda email, **_kwargs: otp_emails.append(email) or "654321",
        log=None,
    )

    verify_requests = [item for item in requests if item[1].endswith("/verify")]
    assert result["ok"] is True
    assert len(verify_requests) == 2
    assert verify_requests[0][2] == verify_requests[1][2] == {"email": TARGET, "code": "654321"}
    assert [item[3] for item in verify_requests] == [False, True]
    assert otp_emails == [TARGET]


def test_builtin_protocol_rebind_runs_full_begin_matrix_with_fresh_sentinel(monkeypatch):
    transport = FakeBuiltinEmailChangeSession()
    transport.blocked_until = 0.0
    transport.blocked_reason = ""
    requests = []
    sentinel_count = 0
    otp_emails = []

    def request(_transport, spec, *, method, url, payload=None):
        headers = dict(spec.get("headers") or {})
        sentinel = headers.get("openai-sentinel-token", "")
        body = dict(payload or {})
        requests.append((method, url, body, sentinel))
        if url.endswith("/eligibility"):
            return {"status": 200, "data": {"eligible": True, "eligibility_type": "password"}}
        if url.endswith("add_email/begin") and body.get("remove_social_subs") is False and sentinel:
            return {"status": 200, "data": {"ok": True}}
        if url.endswith("/begin"):
            transport.blocked_until = 9999999999.0
            transport.blocked_reason = "HTTP 403"
            raise rebind_driver.RebindHttpError(
                403,
                data={"error": {"code": "challenge_required"}},
                diagnostic={"code": "challenge_required", "content_type": "application/json"},
            )
        return {"status": 200, "data": {"ok": True}}

    def sentinel(active_transport, _flow):
        nonlocal sentinel_count
        assert active_transport.blocked_until == 0.0
        assert active_transport.blocked_reason == ""
        sentinel_count += 1
        return {"token": f"challenge-{sentinel_count}"}

    monkeypatch.setattr(rebind_driver, "_protocol_request", request)
    monkeypatch.setattr("core.openai_auth.request_sentinel_token", sentinel)
    monkeypatch.setattr(
        "core.openai_auth.build_sentinel_header",
        lambda _transport, challenge, _flow: (f"proof-{challenge['token']}", None),
    )
    context = rebind_driver.RebindContext(
        account=_account(),
        target=_target(),
        login_driver="protocol",
        action_driver="protocol",
        hybrid=False,
        session=transport,
        session_info={"user": {"email": OLD}, "accessToken": "old-token"},
    )

    result = rebind_driver._builtin_chatgpt_protocol_action(
        context,
        otp_getter=lambda email, **_kwargs: otp_emails.append(email) or "654321",
        log=None,
    )

    begin_requests = [item for item in requests if item[1].endswith("/begin")]
    assert result["ok"] is True
    assert len(begin_requests) == 8
    assert [bool(item[3]) for item in begin_requests] == [False, True] * 4
    assert len({item[3] for item in begin_requests if item[3]}) == 4
    assert otp_emails == [TARGET]


def test_builtin_protocol_rebind_reuses_one_target_code_across_full_verify_matrix(monkeypatch):
    transport = FakeBuiltinEmailChangeSession()
    transport.blocked_until = 0.0
    transport.blocked_reason = ""
    requests = []
    sentinel_count = 0
    otp_emails = []

    def request(_transport, spec, *, method, url, payload=None):
        sentinel = str((spec.get("headers") or {}).get("openai-sentinel-token") or "")
        body = dict(payload or {})
        requests.append((method, url, body, sentinel))
        if url.endswith("/eligibility"):
            return {"status": 200, "data": {"eligible": True, "eligibility_type": "password"}}
        if url.endswith("/begin"):
            return {"status": 200, "data": {"ok": True}}
        if url.endswith("add_email/verify") and body.get("remove_social_subs") is False and sentinel:
            return {"status": 200, "data": {"ok": True}}
        transport.blocked_until = 9999999999.0
        transport.blocked_reason = "HTTP 403"
        raise rebind_driver.RebindHttpError(
            403,
            data={"error": {"code": "challenge_required"}},
            diagnostic={"code": "challenge_required", "content_type": "application/json"},
        )

    def sentinel(active_transport, _flow):
        nonlocal sentinel_count
        assert active_transport.blocked_until == 0.0
        assert active_transport.blocked_reason == ""
        sentinel_count += 1
        return {"token": f"challenge-{sentinel_count}"}

    monkeypatch.setattr(rebind_driver, "_protocol_request", request)
    monkeypatch.setattr("core.openai_auth.request_sentinel_token", sentinel)
    monkeypatch.setattr(
        "core.openai_auth.build_sentinel_header",
        lambda _transport, challenge, _flow: (f"proof-{challenge['token']}", None),
    )
    context = rebind_driver.RebindContext(
        account=_account(),
        target=_target(),
        login_driver="protocol",
        action_driver="protocol",
        hybrid=False,
        session=transport,
        session_info={"user": {"email": OLD}, "accessToken": "old-token"},
    )

    result = rebind_driver._builtin_chatgpt_protocol_action(
        context,
        otp_getter=lambda email, **_kwargs: otp_emails.append(email) or "654321",
        log=None,
    )

    verify_requests = [item for item in requests if item[1].endswith("/verify")]
    assert result["ok"] is True
    assert len(verify_requests) == 8
    assert {item[2]["code"] for item in verify_requests} == {"654321"}
    assert [bool(item[3]) for item in verify_requests] == [False, True] * 4
    assert len({item[3] for item in verify_requests if item[3]}) == 4
    assert otp_emails == [TARGET]


def test_protocol_request_omits_target_headers_and_classifies_403():
    class DecoratedSession(FakeTransport):
        def __init__(self):
            super().__init__()
            self._rebind_session_info = {"accessToken": "token"}
            self.kwargs = {}

        def _attach_openai_target_headers_for_url(self, url, headers):
            raise AssertionError("email-change request must skip target headers")

        def get_chatgpt_headers(self, referer):
            return {"referer": referer}

        def post(self, url, **kwargs):
            self.kwargs = kwargs
            return FakeResponse(
                403,
                {"error": {"code": "challenge_required"}, "email": TARGET},
                url,
                headers={"content-type": "text/html; charset=UTF-8", "server": "cloudflare", "cf-ray": "fixture"},
                text=f"Just a moment {TARGET}",
            )

    session = DecoratedSession()
    with pytest.raises(rebind_driver.RebindHttpError) as captured:
        rebind_driver._protocol_request(
            session,
            {"base_url": "https://chatgpt.com"},
            method="POST",
            url="/backend-api/accounts/change_email/begin",
            payload={"email": TARGET},
        )

    assert session.kwargs["_attach_target_headers"] is False
    assert captured.value.diagnostic == {
        "code": "challenge_required",
        "content_type": "text/html",
        "edge": "cloudflare",
        "challenge": True,
    }
    assert TARGET not in str(captured.value)


def test_builtin_protocol_rebind_rejects_account_too_new(monkeypatch):
    def request(_transport, _spec, *, method, url, payload=None):
        if url.endswith("/eligibility"):
            return {"status": 200, "data": {"eligible": True}}
        raise rebind_driver.RebindHttpError(
            400,
            data={"error": {"code": "email_change_account_too_new"}},
        )

    monkeypatch.setattr(rebind_driver, "_protocol_request", request)
    context = rebind_driver.RebindContext(
        account=_account(),
        target=_target(),
        login_driver="protocol",
        action_driver="protocol",
        hybrid=False,
        session=FakeTransport(),
        session_info={"user": {"email": OLD}, "accessToken": "old-token"},
    )
    with pytest.raises(rebind_driver.RebindDriverError, match="注册时间过短"):
        rebind_driver._builtin_chatgpt_protocol_action(
            context,
            otp_getter=lambda **_kwargs: "654321",
            log=None,
        )


def test_builtin_protocol_reauth_attaches_new_session_token(monkeypatch):
    old_session = FakeTransport()
    new_session = FakeTransport()
    requests = []
    login_calls = []

    def request(transport, _spec, *, method, url, payload=None):
        requests.append((transport, method, url, dict(getattr(transport, "_rebind_session_info", {}))))
        if url.endswith("/eligibility") and transport is old_session:
            raise rebind_driver.RebindHttpError(401, data={"error": "reauth_required"})
        if url.endswith("/eligibility"):
            return {"status": 200, "data": {"eligible": True}}
        if url.endswith("/begin"):
            return {"status": 200, "data": {"ok": True}}
        return {"status": 200, "data": {"verified": True}}

    def login(account, **_kwargs):
        login_calls.append(account["email"])
        return new_session, {"user": {"email": OLD}, "accessToken": "refreshed-token"}

    monkeypatch.setattr(rebind_driver, "_protocol_request", request)
    monkeypatch.setattr(rebind_driver, "_protocol_login_builtin", login)
    context = rebind_driver.RebindContext(
        account=_account(),
        target=_target(),
        login_driver="protocol",
        action_driver="protocol",
        hybrid=False,
        session=old_session,
        proxy="POOL",
        session_info={"user": {"email": OLD}, "accessToken": "old-token"},
    )
    old_session._rebind_session_info = dict(context.session_info)

    result = rebind_driver._builtin_chatgpt_protocol_action(
        context,
        otp_getter=lambda **_kwargs: "654321",
        log=None,
    )

    assert result["ok"] is True
    assert login_calls == [OLD]
    assert requests[0][0] is old_session
    assert requests[1][0] is new_session
    assert requests[1][3]["accessToken"] == "refreshed-token"
