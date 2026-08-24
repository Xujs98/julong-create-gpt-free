# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch

import pytest

from core.browser_liveness import (
    _browser_login,
    _open_roxy,
    _browser_session_once,
    _browser_token_status,
    _clear_browser_auth_state,
    _fill_login_password,
    _is_totp_page,
    _login_with_email_otp,
    _password_error_message,
    _submit_totp_and_fetch_session,
    _wait_after_password,
    check_account_liveness_browser,
)


def test_browser_session_timeout_is_recoverable_and_rearms_script_deadline():
    from selenium.common.exceptions import TimeoutException

    driver = MagicMock()
    driver.execute_async_script.side_effect = TimeoutException("script timeout")

    assert _browser_session_once(driver) == (0, {})
    driver.set_script_timeout.assert_called_once_with(25)


def test_browser_token_timeout_returns_zero_instead_of_aborting_login():
    from selenium.common.exceptions import TimeoutException

    driver = MagicMock()
    driver.execute_async_script.side_effect = TimeoutException("script timeout")

    assert _browser_token_status(driver, "TOKEN") == 0
    driver.set_script_timeout.assert_called_once_with(25)


def test_browser_session_abort_response_is_recoverable():
    driver = MagicMock()
    driver.execute_async_script.return_value = {"status": 0, "timedOut": True}

    assert _browser_session_once(driver) == (0, {})


def test_roxy_opener_passes_explicit_proxy_to_profile_creation():
    opened = type("Opened", (), {"profile_id": "PROFILE", "created_by_run": True})()
    client = MagicMock()
    client.open_profile.return_value = opened
    client.last_proxy_url = "socks5h://user:pass@proxy.example:3000"
    driver = MagicMock()

    with patch("core.roxybrowser_client.RoxyBrowserClient", return_value=client), patch(
        "core.roxy_registration._build_driver", return_value=driver
    ):
        actual_driver, proxy_used, closer = _open_roxy(
            "socks5h://user:pass@proxy.example:3000", True
        )

    assert actual_driver is driver
    assert proxy_used == client.last_proxy_url
    client.open_profile.assert_called_once_with(
        headless=True, proxy="socks5h://user:pass@proxy.example:3000"
    )
    closer()
    client.close_profile.assert_called_once_with("PROFILE")


def test_fill_login_password_uses_marked_form_controls_and_verified_value():
    driver = MagicMock()
    password_input = MagicMock()
    submit_button = MagicMock()
    driver.execute_script.side_effect = [
        {
            "ok": True,
            "inputSelector": '[data-live-password-input="marker"]',
            "buttonSelector": '[data-live-password-submit="marker"]',
            "url": "https://auth.example/log-in/password",
        },
        {"passwordLength": 8, "submitDisabled": False},
        {"passwordLength": 8, "disabled": False, "ariaInvalid": False},
    ]
    driver.find_elements.side_effect = [[password_input], [submit_button]]

    with patch("core.roxy_registration._human_type_text") as type_text, patch(
        "core.roxy_registration._human_click"
    ) as human_click:
        result = _fill_login_password(driver, "PASSWORD")

    assert result["ok"] is True
    assert result["password_length"] == 8
    type_text.assert_called_once_with(driver, password_input, "PASSWORD", clear=True)
    human_click.assert_called_once_with(driver, submit_button, label="live_password_submit")
    assert driver.find_elements.call_args_list[0].args[1] == '[data-live-password-input="marker"]'
    assert driver.find_elements.call_args_list[1].args[1] == '[data-live-password-submit="marker"]'


def test_password_error_message_deduplicates_visible_errors():
    assert _password_error_message({"errors": ["密码错误", "密码错误", "请重试"]}) == "密码错误；请重试"


def test_wait_after_password_reports_in_place_error_immediately():
    driver = MagicMock(current_url="https://auth.example/log-in/password")
    with patch("core.roxy_registration._has_access_token", return_value=False), patch(
        "core.roxy_registration._is_email_verification_page", return_value=False
    ), patch(
        "core.browser_liveness._password_login_page_state",
        return_value={"url": driver.current_url, "passwordPresent": True, "errors": ["Incorrect password"]},
    ):
        with pytest.raises(RuntimeError, match="Incorrect password"):
            _wait_after_password(driver, timeout=5)


def test_wait_after_password_resubmits_once_then_detects_mfa():
    driver = MagicMock(current_url="https://auth.example/log-in/password")
    clock = MagicMock()
    clock.time.side_effect = [0, 0, 5, 5, 6]
    clock.sleep.side_effect = lambda _seconds: setattr(driver, "current_url", "https://auth.example/mfa/challenge")
    state = {
        "url": driver.current_url,
        "passwordPresent": True,
        "passwordLength": 8,
        "submitPresent": True,
        "submitDisabled": False,
        "submitLoading": False,
        "errors": [],
    }
    with patch("core.browser_liveness.time", clock), patch(
        "core.roxy_registration._has_access_token", return_value=False
    ), patch(
        "core.roxy_registration._is_email_verification_page", return_value=False
    ), patch(
        "core.browser_liveness._password_login_page_state", return_value=state
    ), patch(
        "core.browser_liveness._resubmit_login_password_form", return_value={"ok": True}
    ) as resubmit:
        assert _wait_after_password(driver, timeout=10, submission={"marker": "marker"}) == "totp"

    resubmit.assert_called_once_with(driver, "marker")


def test_wait_after_password_prefers_totp_over_generic_numeric_email_otp():
    driver = MagicMock(current_url="https://auth.example/log-in/continue")
    with patch("core.roxy_registration._has_access_token", return_value=False), patch(
        "core.roxy_registration._is_email_verification_page", return_value=True
    ), patch(
        "core.browser_liveness._is_totp_page", return_value=True
    ):
        assert _wait_after_password(driver, timeout=5, expect_totp=True) == "totp"


def test_totp_page_detector_rejects_explicit_email_verification_marker():
    driver = MagicMock()
    with patch(
        "core.browser_liveness._totp_page_state",
        return_value={
            "url": "https://auth.example/email-verification",
            "text": "Enter the code sent to your email",
            "attrs": "inputmode numeric",
            "hasNumericInput": True,
        },
    ):
        assert _is_totp_page(driver, expect_totp=True) is False


def test_totp_submission_uses_liveness_stable_window_helper():
    driver = MagicMock()
    with patch("core.account_export._totp_code_with_margin", return_value="123456") as code_factory, patch(
        "core.browser_liveness._submit_code_and_fetch_session",
        return_value={"accessToken": "TOKEN"},
    ) as submit:
        result = _submit_totp_and_fetch_session(driver, "JBSWY3DPEHPK3PXP")

    assert result["accessToken"] == "TOKEN"
    code_factory.assert_called_once()
    submit.assert_called_once_with(driver, "123456", code_kind="totp")


def test_wait_after_password_returns_logged_in_without_resubmit():
    driver = MagicMock(current_url="https://chatgpt.example/")
    with patch("core.roxy_registration._has_access_token", return_value=True), patch(
        "core.browser_liveness._resubmit_login_password_form"
    ) as resubmit:
        assert _wait_after_password(driver, timeout=5) == "logged_in"
    resubmit.assert_not_called()


def test_clear_browser_auth_state_uses_cdp_cookie_and_origin_cleanup():
    driver = MagicMock()

    _clear_browser_auth_state(driver)

    driver.delete_all_cookies.assert_called_once()
    driver.execute_cdp_cmd.assert_any_call("Network.clearBrowserCookies", {})
    assert driver.execute_cdp_cmd.call_count == 3


def test_browser_login_retries_when_existing_session_token_is_rejected():
    driver = MagicMock(current_url="https://chatgpt.example/")
    stale_session = {"accessToken": "STALE"}
    valid_session = {"accessToken": "VALID"}
    with patch("core.roxy_registration._safe_get"), patch(
        "core.browser_liveness._restore_cookies", return_value=0
    ), patch("core.browser_liveness._clear_browser_auth_state"), patch(
        "core.roxy_registration._wait_for_cloudflare_challenge"
    ), patch("core.roxy_registration._maybe_accept"), patch(
        "core.roxy_registration._submit_email_and_wait_next", return_value="logged_in"
    ), patch(
        "core.roxy_registration._fetch_chatgpt_session", side_effect=[stale_session, valid_session]
    ), patch(
        "core.browser_liveness._browser_token_status", side_effect=[401, 200]
    ), patch(
        "core.browser_liveness._browser_login", wraps=_browser_login
    ) as browser_login:
        result = browser_login(
            driver,
            {"registration_password": "PASSWORD"},
            "user@example.com",
            headless=False,
        )

    assert result == valid_session
    assert browser_login.call_count == 2
    assert browser_login.call_args_list[-1].kwargs["restore_saved_session"] is False
    assert browser_login.call_args_list[-1].kwargs["stale_session_retry"] is True


def test_browser_login_restarts_after_session_fetch_timeout():
    driver = MagicMock(current_url="https://chatgpt.example/")
    session = {"accessToken": "NEW_TOKEN"}
    with patch("core.roxy_registration._safe_get"), patch(
        "core.browser_liveness._restore_cookies", return_value=16
    ), patch("core.browser_liveness._browser_session_once", return_value=(0, {})), patch(
        "core.browser_liveness._clear_browser_auth_state"
    ) as clear_state, patch("core.roxy_registration._wait_for_cloudflare_challenge"), patch(
        "core.roxy_registration._maybe_accept"
    ), patch("core.roxy_registration._submit_email_and_wait_next", return_value="logged_in"), patch(
        "core.roxy_registration._fetch_chatgpt_session", return_value=session
    ), patch("core.browser_liveness._browser_token_status", return_value=200):
        assert _browser_login(driver, {}, "user@example.com", headless=True) == session

    clear_state.assert_called_once_with(driver)


def test_browser_liveness_forces_fresh_login_without_saved_session_reuse():
    driver = MagicMock()
    closer = MagicMock()
    session = {"accessToken": "NEW_TOKEN"}
    result = {"ok": True, "status": "live", "access_token": "NEW_TOKEN"}
    with patch(
        "core.browser_liveness._open_roxy", return_value=(driver, None, closer)
    ), patch(
        "core.browser_liveness._browser_login", return_value=session
    ) as browser_login, patch(
        "core.browser_liveness._browser_token_status", return_value=200
    ), patch(
        "core.browser_liveness._session_result", return_value=result
    ):
        actual = check_account_liveness_browser(
            "user@example.com",
            {"access_token": "OLD_TOKEN"},
            proxy=None,
            driver_name="roxy",
            headless=False,
            force_fresh_login=True,
        )

    assert actual == result
    browser_login.assert_called_once_with(
        driver,
        {"access_token": "OLD_TOKEN"},
        "user@example.com",
        headless=False,
        restore_saved_session=False,
    )
    closer.assert_called_once()


def test_email_otp_retries_with_resend_and_excludes_rejected_icloud_code():
    driver = MagicMock()
    observed_exclusions = []

    def next_code(*_args, **kwargs):
        observed_exclusions.append(set(kwargs.get("exclude_codes") or set()))
        return "381908" if len(observed_exclusions) == 1 else "492617"

    with patch("core.browser_liveness.wait_for_otp", side_effect=next_code) as wait_otp, patch(
        "core.roxy_registration._clear_otp_inputs"
    ) as clear_inputs, patch(
        "core.roxy_registration._type_otp"
    ) as type_otp, patch(
        "core.roxy_registration._click_continue"
    ), patch(
        "core.roxy_registration._wait_after_email_otp_submit", side_effect=["invalid", "accepted"]
    ), patch(
        "core.roxy_registration._click_resend_email_otp", return_value={"ok": True}
    ) as resend, patch(
        "core.roxy_registration._fetch_chatgpt_session", return_value={"accessToken": "NEW_TOKEN"}
    ):
        result = _login_with_email_otp(driver, "user@example.com", after_ts=123.0)

    assert result["accessToken"] == "NEW_TOKEN"
    assert wait_otp.call_count == 2
    assert observed_exclusions == [set(), {"381908"}]
    assert clear_inputs.call_count == 2
    assert [call.args[1] for call in type_otp.call_args_list] == ["381908", "492617"]
    resend.assert_called_once()


def test_email_otp_stops_after_three_distinct_rejected_codes():
    driver = MagicMock()
    with patch(
        "core.browser_liveness.wait_for_otp", side_effect=["111111", "222222", "333333"]
    ), patch("core.roxy_registration._clear_otp_inputs"), patch(
        "core.roxy_registration._type_otp"
    ), patch("core.roxy_registration._click_continue"), patch(
        "core.roxy_registration._wait_after_email_otp_submit", return_value="invalid"
    ), patch(
        "core.roxy_registration._click_resend_email_otp", return_value={"ok": True}
    ) as resend:
        with pytest.raises(RuntimeError, match="已重试 3 次"):
            _login_with_email_otp(driver, "user@example.com", after_ts=123.0)

    assert resend.call_count == 2
