import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.account_liveness import _refresh_with_password_protocol, _restore_saved_session, check_account_liveness
from core.live_check_service import _check_existing_access_token


def test_valid_access_token_completes_live_check_without_email_otp():
    account = {
        "access_token": "TOKEN",
        "user_id": "user-1",
        "user_name": "User",
        "plan_type": "free",
        "extra_json": json.dumps({"session": {"cookies": [{"name": "sid", "value": "COOKIE"}]}}),
    }
    checked = {"ok": True, "checked_at": "2026-08-08T10:00:00", "current_plan_type": "free"}
    with patch("core.live_check_service.check_account_plan", return_value=checked), patch(
        "core.live_check_service._append_log"
    ):
        result = _check_existing_access_token(account, proxy="PROXY", email="user@example.com")

    assert result["ok"] is True
    assert result["status"] == "live"
    assert result["check_method"] == "access_token"
    assert result["access_token"] == "TOKEN"


def test_valid_access_token_without_session_cookies_requests_login_refresh():
    """AT 可用但登录态不可植入时，查活应继续登录并补齐 Cookie。"""
    account = {
        "access_token": "TOKEN",
        "extra_json": json.dumps({"session": {"accessToken": "TOKEN", "cookies": []}}),
    }
    checked = {"ok": True, "checked_at": "2026-08-08T10:00:00", "current_plan_type": "free"}
    with patch("core.live_check_service.check_account_plan", return_value=checked), patch(
        "core.live_check_service._append_log"
    ) as append_log:
        result = _check_existing_access_token(account, proxy="PROXY", email="user@example.com")

    assert result is None
    assert any("缺少可植入 Session Cookie" in call.args[1] for call in append_log.call_args_list)


def test_expired_access_token_requests_login_refresh():
    account = {"access_token": "TOKEN"}
    checked = {"ok": False, "http_status": 401, "needs_live_check": True, "token_expired": True}
    with patch("core.live_check_service.check_account_plan", return_value=checked), patch(
        "core.live_check_service._append_log"
    ):
        result = _check_existing_access_token(account, proxy="PROXY", email="user@example.com")

    assert result is None


def test_network_failure_does_not_mark_account_deactivated():
    account = {"access_token": "TOKEN"}
    checked = {"ok": False, "http_status": 403, "error": "HTTP 403"}
    with patch("core.live_check_service.check_account_plan", return_value=checked), patch(
        "core.live_check_service._append_log"
    ):
        result = _check_existing_access_token(account, proxy="PROXY", email="user@example.com")

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "403" in result["error"]


def test_network_failure_uses_selected_browser_fallback():
    """选择指纹浏览器时，协议 AT 校验的 CF 403 应继续浏览器确认。"""
    account = {"access_token": "TOKEN"}
    checked = {"ok": False, "http_status": 403, "error": "HTTP 403"}
    with patch("core.live_check_service.check_account_plan", return_value=checked), patch(
        "core.live_check_service._append_log"
    ) as append_log:
        result = _check_existing_access_token(
            account,
            proxy="PROXY",
            email="user@example.com",
            browser_fallback=True,
        )

    assert result is None
    assert any("指纹浏览器确认" in call.args[1] for call in append_log.call_args_list)


def test_missing_access_token_requests_login_refresh():
    with patch("core.live_check_service._append_log"):
        result = _check_existing_access_token({}, proxy="PROXY", email="user@example.com")

    assert result is None


def test_saved_password_uses_password_login_without_email_otp(tmp_path):
    """账号已保存密码时直接进入账密登录，邮箱 OTP 协议链不应启动。"""
    expected = {
        "ok": True,
        "status": "live",
        "access_token": "NEW_TOKEN",
        "check_method": "password_totp",
    }
    account = {
        "email": "user@example.com",
        "registration_password": "PASSWORD",
        "totp_secret": "JBSWY3DPEHPK3PXP",
    }
    with patch("core.account_liveness.log_path", return_value=tmp_path / "live.log"), patch(
        "core.account_liveness._restore_saved_session", return_value=None
    ), patch(
        "core.account_liveness._refresh_with_password_protocol", return_value=expected
    ) as password_login, patch(
        "core.account_liveness._network_preflight_with_retry"
    ) as email_otp_login:
        result = check_account_liveness(
            "user@example.com",
            proxy="PROXY",
            account=account,
        )

    assert result == expected
    password_login.assert_called_once()
    email_otp_login.assert_not_called()


def test_browser_driver_delegates_without_protocol_login(tmp_path):
    """浏览器查活直接委派给选定驱动，并传递独立无头参数。"""
    expected = {"ok": True, "status": "live", "access_token": "NEW", "check_method": "cloak_browser"}
    account = {"email": "user@example.com", "registration_password": "PASSWORD"}
    with patch("core.account_liveness.log_path", return_value=tmp_path / "live.log"), patch(
        "core.browser_liveness.check_account_liveness_browser", return_value=expected
    ) as browser_check, patch(
        "core.account_liveness._restore_saved_session"
    ) as protocol_session, patch(
        "core.account_liveness._refresh_with_password_protocol"
    ) as protocol_password:
        result = check_account_liveness(
            "user@example.com",
            proxy="PROXY",
            account=account,
            driver="cloak",
            headless=True,
        )

    assert result == expected
    browser_check.assert_called_once_with(
        "user@example.com",
        account,
        proxy="PROXY",
        driver_name="cloak",
        headless=True,
    )
    protocol_session.assert_not_called()
    protocol_password.assert_not_called()


def test_legacy_account_explicitly_sends_email_otp_before_waiting(tmp_path):
    """旧账号邮箱 OTP 兜底必须先触发发信，再开始轮询验证码。"""
    session = SimpleNamespace(
        session=SimpleNamespace(close=MagicMock()),
        device_id="DEVICE",
        proxy="PROXY",
    )
    session_info = {
        "accessToken": "NEW_TOKEN",
        "user": {"id": "user-1"},
        "account": {"planType": "free"},
    }
    events = []
    with patch("core.account_liveness.log_path", return_value=tmp_path / "live.log"), patch(
        "core.account_liveness._restore_saved_session", return_value=None
    ), patch(
        "core.account_liveness._refresh_with_password_protocol"
    ) as password_login, patch(
        "core.account_liveness._network_preflight_with_retry", return_value=(session, "AUTH_URL")
    ), patch(
        "core.account_liveness.follow_authorize", return_value="https://auth.openai.com/email-verification"
    ), patch(
        "core.account_liveness.detect_account_unusable_text", return_value=None
    ), patch(
        "core.account_liveness.send_email_otp", side_effect=lambda *_: events.append("send")
    ), patch(
        "core.account_liveness._validate_with_retry",
        side_effect=lambda *_args, **_kwargs: events.append("wait") or {"continue_url": "CONTINUE"},
    ), patch(
        "core.account_liveness.follow_oauth_callback"
    ), patch(
        "core.account_liveness.fetch_session", return_value=session_info
    ), patch(
        "core.account_liveness.capture_http_cookies",
        return_value=[{"name": "sid", "value": "COOKIE", "domain": ".chatgpt.com"}],
    ):
        result = check_account_liveness(
            "old@example.com",
            proxy="PROXY",
            account={"email": "old@example.com"},
        )

    assert events == ["send", "wait"]
    assert result["ok"] is True
    assert result["check_method"] == "email_otp"
    assert result["session"]["cookies"][0]["name"] == "sid"
    password_login.assert_not_called()
    session.session.close.assert_called_once()


def test_password_protocol_submits_totp_fetches_session_and_closes_http_session():
    """账密分支遇到 MFA 时必须走协议提交 TOTP，禁止构造后台指纹浏览器。"""
    session = SimpleNamespace(
        session=SimpleNamespace(close=MagicMock()),
        device_id="DEVICE",
        proxy="PROXY",
    )
    account = {
        "registration_password": "PASSWORD",
        "totp_secret": "JBSWY3DPEHPK3PXP",
    }
    session_info = {
        "accessToken": "NEW_TOKEN",
        "user": {"id": "user-1"},
        "account": {"planType": "free"},
    }

    factor = {"id": "factor-1", "factor_type": "totp"}
    with patch("core.account_liveness._network_preflight_with_retry", return_value=(session, "AUTH_URL")), patch(
        "core.account_liveness.follow_authorize", return_value="https://auth.openai.com/log-in/password"
    ), patch(
        "core.account_liveness.verify_login_password",
        return_value={"page": {"type": "mfa_challenge", "payload": {"factors": [factor]}}},
    ), patch(
        "core.account_liveness.issue_mfa_challenge", return_value={"challenge": "issued"}
    ) as issue_challenge, patch(
        "core.account_liveness.verify_mfa_code", return_value={"continue_url": "CONTINUE"}
    ) as verify_mfa, patch(
        "core.account_liveness.follow_oauth_callback"
    ), patch(
        "core.account_liveness.fetch_session", return_value=session_info
    ), patch(
        "core.account_liveness.capture_http_cookies",
        return_value=[{"name": "sid", "value": "COOKIE", "domain": ".chatgpt.com"}],
    ), patch("core.account_liveness.pyotp.TOTP") as totp_factory, patch(
        "core.cloakbrowser_driver.build_cloak_driver"
    ) as build_driver:
        totp_factory.return_value.now.return_value = "123456"
        result = _refresh_with_password_protocol(
            account,
            "user@example.com",
            "PROXY",
            "2026-08-08T10:00:00",
        )

    assert result["ok"] is True
    assert result["check_method"] == "password_totp"
    assert result["access_token"] == "NEW_TOKEN"
    issue_challenge.assert_called_once_with(session, factor)
    verify_mfa.assert_called_once_with(session, factor, "123456")
    totp_factory.assert_called_once_with("JBSWY3DPEHPK3PXP")
    build_driver.assert_not_called()
    session.session.close.assert_called_once()


def test_saved_session_token_must_pass_backend_validation():
    """Session 接口回传 AT 后仍需 accounts/check 验证；401 时继续账密登录。"""
    cookie_jar = MagicMock()
    session = SimpleNamespace(
        session=SimpleNamespace(cookies=cookie_jar, close=MagicMock()),
        device_id="DEVICE",
        proxy="PROXY",
    )
    account = {
        "account_id": "account-1",
        "extra_json": '{"session":{"cookies":[{"name":"sid","value":"COOKIE","domain":".chatgpt.com"}]}}',
    }
    with patch("core.account_liveness.BrowserSession", return_value=session), patch(
        "core.account_liveness.fetch_session", return_value={"accessToken": "STALE_TOKEN", "account": {"planType": "free"}}
    ), patch(
        "core.account_liveness.capture_http_cookies",
        return_value=[{"name": "sid", "value": "COOKIE", "domain": ".chatgpt.com"}],
    ), patch(
        "core.account_liveness.check_account_plan",
        return_value={"ok": False, "http_status": 401, "needs_live_check": True},
    ) as validate_token:
        result = _restore_saved_session(
            account,
            "user@example.com",
            "PROXY",
            "2026-08-08T10:00:00",
        )

    assert result is None
    validate_token.assert_called_once()
    session.session.close.assert_called_once()
