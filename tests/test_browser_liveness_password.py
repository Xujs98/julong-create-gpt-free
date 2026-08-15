# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch

import pytest

from core.browser_liveness import (
    _fill_login_password,
    _password_error_message,
    _wait_after_password,
)


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


def test_wait_after_password_returns_logged_in_without_resubmit():
    driver = MagicMock(current_url="https://chatgpt.example/")
    with patch("core.roxy_registration._has_access_token", return_value=True), patch(
        "core.browser_liveness._resubmit_login_password_form"
    ) as resubmit:
        assert _wait_after_password(driver, timeout=5) == "logged_in"
    resubmit.assert_not_called()
