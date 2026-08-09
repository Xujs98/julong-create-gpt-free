import json
from contextlib import ExitStack
from unittest.mock import Mock, patch

import main
import pytest
from core import openai_auth


class _Response:
    status_code = 200
    text = ""

    def __init__(self, url, payload):
        self.url = url
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _Session:
    def __init__(self):
        self.calls = []

    def get_auth_navigate_headers(self, **kwargs):
        return {"referer": kwargs.get("referer", "")}

    def get_auth_headers(self, **kwargs):
        return {"referer": kwargs.get("referer", "")}

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return _Response("https://auth.openai.com/create-account/password", {})

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _Response(
            url,
            {"continue_url": "https://auth.openai.com/api/accounts/email-otp/send", "page": {"type": "email_otp_send"}},
        )


class _SentinelSession:
    device_id = "device"
    sentinel_sid = "sid"
    browser_profile = {}

    def __init__(self):
        self.calls = 0

    def get_sentinel_headers(self):
        return {"accept": "application/json"}

    def post(self, url, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("Failed to perform, curl: (35) TLS connect error: invalid library")
        return _Response(url, {"persona": "chatgpt-noauth"})


class _InvalidAuthResponse(_Response):
    status_code = 400

    def __init__(self, url):
        super().__init__(url, {"error": {"code": "invalid_auth_step", "message": "Invalid authorization step."}})
        self.text = json.dumps(self._payload)

    def raise_for_status(self):
        raise RuntimeError("HTTP Error 400")


def test_protocol_password_helpers_send_password_and_keep_otp_step():
    session = _Session()
    assert openai_auth.get_create_account_page(session).endswith("/create-account/password")
    result = openai_auth.register_user_with_password(session, "user@example.com", "StrongPassword", "sentinel")
    assert result["page"]["type"] == "email_otp_send"
    method, url, kwargs = session.calls[-1]
    assert method == "POST"
    assert url.endswith("/api/accounts/user/register")
    assert '"password": "StrongPassword"' in kwargs["data"]
    assert kwargs["headers"]["openai-sentinel-token"] == "sentinel"


def test_sentinel_token_retries_transient_tls_failure():
    session = _SentinelSession()
    with patch.object(openai_auth, "generate_requirements_token", return_value="p"), patch.object(
        openai_auth, "build_sentinel_request_body", return_value="{}"
    ), patch.object(openai_auth.time, "sleep") as sleep:
        result = openai_auth.request_sentinel_token(session, "oauth_create_account")

    assert result["persona"] == "chatgpt-noauth"
    assert session.calls == 2
    assert sleep.call_count == 1


def test_password_register_invalid_auth_step_has_structured_error():
    session = _Session()
    session.post = Mock(return_value=_InvalidAuthResponse("https://auth.openai.com/api/accounts/user/register"))

    with pytest.raises(openai_auth.InvalidAuthorizationStepError) as caught:
        openai_auth.register_user_with_password(session, "user@example.com", "StrongPassword", "sentinel")

    assert "invalid_auth_step" in str(caught.value)


def test_protocol_login_email_posts_authorize_continue_payload():
    """协议账密登录在页面未直达密码页时，用 authorize/continue 推进邮箱状态。"""
    session = _Session()
    with patch.object(openai_auth, "request_sentinel_token", return_value={"token": "challenge"}), patch.object(
        openai_auth, "build_sentinel_header", return_value=("sentinel", "so")
    ):
        result = openai_auth.continue_authorize_with_email(session, "user@example.com")

    assert result["page"]["type"] == "email_otp_send"
    method, url, kwargs = session.calls[-1]
    assert method == "POST"
    assert url.endswith("/api/accounts/authorize/continue")
    assert '"kind": "email"' in kwargs["data"]
    assert '"value": "user@example.com"' in kwargs["data"]
    assert kwargs["headers"]["openai-sentinel-token"] == "sentinel"
    assert kwargs["headers"]["openai-sentinel-so-token"] == "so"


def test_protocol_mfa_helpers_send_factor_and_code_payloads():
    """协议 MFA challenge/verify 请求体必须包含因子 id、类型和验证码。"""
    session = _Session()
    factor = {"id": "factor-1", "factor_type": "totp", "metadata": {"mfa_request_id": "req-1"}}
    with patch.object(openai_auth, "request_sentinel_token", return_value={"token": "challenge"}), patch.object(
        openai_auth, "build_sentinel_header", return_value=("sentinel", "so")
    ):
        openai_auth.issue_mfa_challenge(session, factor)
        openai_auth.verify_mfa_code(session, factor, "123456")

    issue = session.calls[-2]
    verify = session.calls[-1]
    assert issue[1].endswith("/api/accounts/mfa/issue_challenge")
    assert '"id": "factor-1"' in issue[2]["data"]
    assert '"type": "totp"' in issue[2]["data"]
    assert '"mfa_request_id": "req-1"' in issue[2]["data"]
    assert verify[1].endswith("/api/accounts/mfa/verify")
    assert '"code": "123456"' in verify[2]["data"]


def test_protocol_password_setting_prefers_configured_password():
    with patch.object(main._register_cfg, "REGISTER_PASSWORD", "ConfiguredPassword"):
        assert main._protocol_registration_password() == "ConfiguredPassword"


def test_protocol_password_branch_requires_real_password_landing():
    assert main._is_protocol_password_landing("https://auth.openai.com/create-account/password")
    assert main._is_protocol_password_landing("https://auth.openai.com/api/accounts/user/register")
    assert not main._is_protocol_password_landing("https://auth.openai.com/email-verification")


def test_protocol_authorize_continue_can_request_signup_screen():
    session = _Session()
    with patch.object(openai_auth, "request_sentinel_token", return_value={"token": "challenge"}), patch.object(
        openai_auth, "build_sentinel_header", return_value=("sentinel", "so")
    ):
        openai_auth.continue_authorize_with_email(session, "user@example.com", screen_hint="signup")

    method, url, kwargs = session.calls[-1]
    assert method == "POST"
    assert url.endswith("/api/accounts/authorize/continue")
    assert '"screen_hint": "signup"' in kwargs["data"]


def test_protocol_password_setting_generates_policy_compliant_value():
    with patch.object(main._register_cfg, "REGISTER_PASSWORD", ""):
        value = main._protocol_registration_password()
    assert len(value) == 14
    assert any(ch.isupper() for ch in value)
    assert any(ch.islower() for ch in value)
    assert any(ch.isdigit() for ch in value)
    assert any(ch in "!@#$%^&*?_-+=" for ch in value)


def test_protocol_password_switch_rejects_silent_otp_fallback():
    with patch.object(main._register_cfg, "ENABLE_CREATE_PASSWORD", True):
        with patch.object(main._register_cfg, "REGISTER_PASSWORD", ""):
            with patch.object(main, "_protocol_registration_password", return_value=""):
                try:
                    main._require_protocol_password("", "user@example.com")
                except RuntimeError as exc:
                    assert "未保存账号密码" in str(exc)
                else:
                    raise AssertionError("password switch must reject an empty saved password")


def test_run_registration_protocol_password_branch_persists_password():
    session = Mock(proxy="", device_id="device", auth_session_logging_id="log", sentinel_sid="sid", browser_profile={})
    events = []

    def mark(name, value=None):
        events.append((name, value))

    def fake_validate(*args, **kwargs):
        mark("validate")
        return {"page": {"type": "external_url"}, "external_url": "https://auth.openai.com/authorize/continue"}

    def fake_finalize(*args, **kwargs):
        mark("finalize")
        return ({"user": {}, "account": {}}, "access-token")

    def fake_save(**kwargs):
        mark("save", kwargs.get("extra", {}).get("registration_password"))
        return 42

    with ExitStack() as stack:
        for target, value in (
            ("ENABLE_CREATE_PASSWORD", True),
            ("REGISTER_PASSWORD", "ConfiguredPassword"),
        ):
            stack.enter_context(patch.object(main._register_cfg, target, value))
        for target, value in (
            ("USE_EMAIL_SERVICE", False),
            ("ENABLE_2FA", False),
        ):
            owner = main._email_cfg if target == "USE_EMAIL_SERVICE" else main._twofa_cfg
            stack.enter_context(patch.object(owner, target, value))
        for target in ("CHATGPT_ANON_BOOTSTRAP_ENABLED", "CHATGPT_AUTH_BOOTSTRAP_ENABLED"):
            stack.enter_context(patch.object(main._protocol_cfg, target, False))
        stack.enter_context(patch.object(main._roxy_cfg, "REGISTRATION_DRIVER", "protocol"))
        for target, kwargs in (
            ("BrowserSession", {"return_value": session}),
            ("network_preflight", {"side_effect": lambda *_: mark("preflight")}),
            ("get_providers", {"side_effect": lambda *_: mark("providers") or {}}),
            ("get_csrf_token", {"side_effect": lambda *_: mark("csrf") or "csrf"}),
            ("signin_openai", {"side_effect": lambda *_: mark("signin") or "authorize"}),
            ("follow_authorize", {"side_effect": lambda *args, **kw: mark("authorize", kw.get("allow_password_page")) or "https://auth.openai.com/email-verification"}),
            ("get_create_account_page", {"side_effect": lambda *_: mark("password_page") or "https://auth.openai.com/create-account/password"}),
            ("request_sentinel_token", {"side_effect": lambda *args: mark("sentinel", args[-1]) or {"token": "x"}}),
            ("build_sentinel_header", {"side_effect": lambda *args: mark("sentinel_header", args[-1]) or ("sentinel", "so")}),
            ("register_user_with_password", {"side_effect": lambda *args: mark("register", args[2]) or {"page": {"type": "email_otp_send"}}}),
            ("send_email_otp", {"side_effect": lambda *args, **kw: mark("send_otp")}),
            ("validate_email_otp", {"side_effect": fake_validate}),
            ("_finalize_registration_session", {"side_effect": fake_finalize}),
            ("save_account_data", {"side_effect": fake_save}),
            ("human_delay", {}),
        ):
            stack.enter_context(patch.object(main, target, **kwargs))
        stack.enter_context(patch("core.email_provider.resolve_email_source", return_value="icloud"))
        stack.enter_context(patch("core.flow_trigger.trigger_flow", return_value={"status": "skipped", "ok": False, "message": "disabled"}))
        result = main.run_registration("user@example.com", "Sample User", "1990-01-01", proxy="", otp_code="123456")

    assert result["registration_password"] == "ConfiguredPassword"
    assert events.index(("password_page", None)) < events.index(("register", "ConfiguredPassword"))
    assert events.index(("register", "ConfiguredPassword")) < events.index(("send_otp", None))
    assert ("sentinel", "username_password_create") in events
    assert ("save", "ConfiguredPassword") in events


def test_protocol_failure_after_password_registration_quarantines_email():
    session = Mock(proxy="", device_id="device", auth_session_logging_id="log", sentinel_sid="sid", browser_profile={})
    released = Mock()

    def fake_sentinel(_session, flow):
        if flow == "oauth_create_account":
            raise RuntimeError("Failed to perform, curl: (35) TLS connect error: invalid library")
        return {"token": "challenge"}

    with ExitStack() as stack:
        stack.enter_context(patch.object(main._register_cfg, "ENABLE_CREATE_PASSWORD", True))
        stack.enter_context(patch.object(main._register_cfg, "REGISTER_PASSWORD", "ConfiguredPassword"))
        stack.enter_context(patch.object(main._email_cfg, "USE_EMAIL_SERVICE", False))
        stack.enter_context(patch.object(main._twofa_cfg, "ENABLE_2FA", False))
        stack.enter_context(patch.object(main._protocol_cfg, "CHATGPT_ANON_BOOTSTRAP_ENABLED", False))
        stack.enter_context(patch.object(main._protocol_cfg, "CHATGPT_AUTH_BOOTSTRAP_ENABLED", False))
        stack.enter_context(patch.object(main._roxy_cfg, "REGISTRATION_DRIVER", "protocol"))
        stack.enter_context(patch.object(main, "BrowserSession", return_value=session))
        stack.enter_context(patch.object(main, "network_preflight"))
        stack.enter_context(patch.object(main, "get_providers", return_value={}))
        stack.enter_context(patch.object(main, "get_csrf_token", return_value="csrf"))
        stack.enter_context(patch.object(main, "signin_openai", return_value="authorize"))
        stack.enter_context(patch.object(main, "follow_authorize", return_value="https://auth.openai.com/email-verification"))
        stack.enter_context(patch.object(main, "get_create_account_page", return_value="https://auth.openai.com/create-account/password"))
        stack.enter_context(patch.object(main, "request_sentinel_token", side_effect=fake_sentinel))
        stack.enter_context(patch.object(main, "build_sentinel_header", return_value=("sentinel", "so")))
        stack.enter_context(patch.object(main, "register_user_with_password", return_value={"page": {"type": "email_otp_send"}}))
        stack.enter_context(patch.object(main, "send_email_otp"))
        stack.enter_context(patch.object(main, "validate_email_otp", return_value={"page": {"type": "about_you"}, "continue_url": "https://auth.openai.com/about-you"}))
        stack.enter_context(patch.object(main, "navigate_about_you"))
        stack.enter_context(patch.object(main, "human_delay"))
        stack.enter_context(patch("core.email_provider.release_email", released))

        result = main.run_registration("user@example.com", "Sample User", "1990-01-01", proxy="", otp_code="123456")

    assert result["success"] is False
    released.assert_called_once()
    assert released.call_args.kwargs["status"] == "failed"
