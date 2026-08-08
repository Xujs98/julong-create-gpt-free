import asyncio
import threading
from unittest.mock import patch
from unittest.mock import Mock

from core import cloakbrowser_registration
from core import registration_service


def test_cloak_otp_state_defers_password_guard_until_after_otp():
    driver = object()
    with patch("config.register.ENABLE_CREATE_PASSWORD", True), patch(
        "core.cloakbrowser_registration._fill_password_page_if_present",
        return_value="CreatedPassword",
    ) as fill:
        result = cloakbrowser_registration._ensure_password_after_otp(
            driver, "user@example.test", "otp", None
        )

    assert result == "CreatedPassword"
    fill.assert_called_once_with(driver, "user@example.test", timeout=20)


def test_cloak_otp_state_still_reports_missing_password_after_probe():
    with patch("config.register.ENABLE_CREATE_PASSWORD", True), patch(
        "core.cloakbrowser_registration._fill_password_page_if_present",
        return_value=None,
    ):
        try:
            cloakbrowser_registration._ensure_password_after_otp(
                object(), "user@example.test", "otp", None
            )
        except RuntimeError as exc:
            assert "CloakBrowser" in str(exc)
            assert "未检测到创建密码页" in str(exc)
        else:
            raise AssertionError("missing password page must remain an explicit registration error")


def test_create_password_entry_uses_href_and_waits_for_password_page():
    driver = Mock()
    password_link = Mock()
    password_link.is_displayed.return_value = True
    password_link.is_enabled.return_value = True
    driver.find_elements.return_value = [password_link]
    with patch(
        "core.roxy_registration._is_signup_password_page",
        side_effect=[False, True],
    ), patch("core.roxy_registration._has_access_token", return_value=False), patch(
        "core.roxy_registration._human_click"
    ) as click, patch.object(
        driver,
        "execute_script",
        return_value={
            "readyState": "complete",
            "url": "https://auth.example.test/email-verification",
            "passwordHrefCount": 1,
        },
    ):
        from core.roxy_registration import _click_create_password_entry_if_present

        result = _click_create_password_entry_if_present(driver, timeout=3)

    assert result["ok"] is True
    assert result["reason"] == "clicked_create_password_entry"
    click.assert_called_once_with(driver, password_link, label="create_password_entry_href")
    assert result["selector"] == 'a[href="/create-account/password"]'


def test_create_password_entry_clicks_password_branch_from_otp_page():
    driver = Mock()
    password_link = Mock()
    password_link.is_displayed.return_value = True
    password_link.is_enabled.return_value = True
    driver.find_elements.return_value = [password_link]
    with patch(
        "core.roxy_registration._is_signup_password_page",
        side_effect=[False, True],
    ), patch("core.roxy_registration._has_access_token", return_value=False), patch(
        "core.roxy_registration._human_click"
    ) as click, patch.object(
        driver,
        "execute_script",
        return_value={
            "readyState": "complete",
            "url": "https://auth.example.test/email-verification",
            "passwordHrefCount": 1,
        },
    ):
        from core.roxy_registration import _click_create_password_entry_if_present

        result = _click_create_password_entry_if_present(driver, timeout=3)

    assert result["ok"] is True
    assert result["reason"] == "clicked_create_password_entry"
    click.assert_called_once_with(driver, password_link, label="create_password_entry_href")


def test_create_password_entry_waits_until_html_is_loaded():
    """HTML 仍在 loading 时不查元素，DOM 完成后再按唯一 href 点击。"""
    driver = Mock()
    password_link = Mock()
    password_link.is_displayed.return_value = True
    password_link.is_enabled.return_value = True
    driver.find_elements.return_value = [password_link]
    driver.execute_script.side_effect = [
        {
            "readyState": "loading",
            "url": "https://auth.example.test/email-verification",
            "passwordHrefCount": 0,
        },
        {
            "readyState": "complete",
            "url": "https://auth.example.test/email-verification",
            "passwordHrefCount": 1,
        },
    ]

    with patch(
        "core.roxy_registration._is_signup_password_page",
        side_effect=[False, False, True],
    ), patch("core.roxy_registration._has_access_token", return_value=False), patch(
        "core.roxy_registration._human_click"
    ) as click, patch("core.roxy_registration.time.sleep"):
        from core.roxy_registration import _click_create_password_entry_if_present

        result = _click_create_password_entry_if_present(driver, timeout=3)

    assert result["ok"] is True
    assert result["reason"] == "clicked_create_password_entry"
    assert driver.find_elements.call_count == 1
    click.assert_called_once_with(driver, password_link, label="create_password_entry_href")


def test_password_branch_is_completed_before_otp_fetch():
    from core.roxy_registration import _ensure_password_before_otp

    order = []
    with patch("config.register.ENABLE_CREATE_PASSWORD", True), patch(
        "core.roxy_registration._click_create_password_entry_if_present",
        side_effect=lambda *args, **kwargs: order.append("click") or {"ok": True},
    ), patch(
        "core.roxy_registration._fill_password_page_if_present",
        side_effect=lambda *args, **kwargs: order.append("fill") or "CreatedPassword",
    ), patch(
        "core.roxy_registration._require_password_if_enabled",
        side_effect=lambda *args, **kwargs: order.append("require"),
    ):
        result = _ensure_password_before_otp(object(), "user@example.test", "otp", driver_name="CloakBrowser")

    assert result == "CreatedPassword"
    assert order == ["click", "fill", "require"]


def test_password_branch_missing_entry_stops_before_otp_fetch():
    from core.roxy_registration import _ensure_password_before_otp

    with patch("config.register.ENABLE_CREATE_PASSWORD", True), patch(
        "core.roxy_registration._click_create_password_entry_if_present",
        return_value={"ok": False, "reason": "missing_create_password_entry"},
    ), patch("core.roxy_registration._fill_password_page_if_present") as fill:
        try:
            _ensure_password_before_otp(object(), "user@example.test", "otp", driver_name="CloakBrowser")
        except RuntimeError as exc:
            assert "使用密码继续" in str(exc)
        else:
            raise AssertionError("missing password branch must stop before OTP")
    fill.assert_not_called()


def test_password_branch_respects_disabled_feature_switch():
    from core.roxy_registration import _ensure_password_before_otp

    with patch("config.register.ENABLE_CREATE_PASSWORD", False), patch(
        "core.roxy_registration._click_create_password_entry_if_present"
    ) as click:
        result = _ensure_password_before_otp(object(), "user@example.test", "otp")

    assert result is None
    click.assert_not_called()


def test_password_page_uses_real_elements_without_language_copy():
    """任意页面语言下都通过结构选择真实密码框和提交按钮。"""
    from core.roxy_registration import _fill_password_page_if_present

    driver = Mock()
    password_input = Mock()
    password_input.is_displayed.return_value = True
    password_input.is_enabled.return_value = True
    submit_button = Mock()
    submit_button.is_displayed.return_value = True
    submit_button.is_enabled.return_value = True
    driver.execute_script.return_value = {
        "ok": True,
        "reason": "password_targets",
        "inputSelector": '[data-codex-password-input="marker"]',
        "buttonSelector": '[data-codex-password-submit="marker"]',
    }
    driver.find_elements.side_effect = [[password_input], [submit_button]]

    with patch("core.roxy_registration._has_access_token", return_value=False), patch(
        "core.roxy_registration._is_signup_password_page", side_effect=[True, True]
    ), patch("core.roxy_registration._is_login_password_page", return_value=False), patch(
        "core.roxy_registration._password_page_state", return_value={"url": "/create-account/password"}
    ), patch("core.roxy_registration._create_password_enabled", return_value=True), patch(
        "core.roxy_registration._registration_password", return_value="Universal1!Password"
    ), patch("core.roxy_registration._human_type_text") as type_text, patch(
        "core.roxy_registration._human_click"
    ) as click, patch("core.roxy_registration.human_delay"), patch(
        "core.roxy_registration._is_email_verification_page", return_value=True
    ):
        result = _fill_password_page_if_present(driver, "user@example.test", timeout=3)

    assert result == "Universal1!Password"
    type_text.assert_called_once_with(driver, password_input, "Universal1!Password", clear=True)
    click.assert_called_once_with(driver, submit_button, label="password_submit")


def test_cloak_flow_isolated_from_asyncio_loop_and_inherits_job_context():
    """Playwright Sync API must run in the same isolated thread as page calls."""
    marker = {}
    parent_name = threading.current_thread().name
    registration_service._THREAD_CTX.job_id = 321

    def probe():
        marker["thread_name"] = threading.current_thread().name
        marker["job_id"] = getattr(registration_service._THREAD_CTX, "job_id", None)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            marker["has_running_loop"] = False
        else:
            marker["has_running_loop"] = True
        return "ok"

    try:
        async def invoke_from_running_loop():
            return cloakbrowser_registration._run_in_isolated_thread(probe)

        result = asyncio.run(invoke_from_running_loop())
    finally:
        del registration_service._THREAD_CTX.job_id

    assert result == "ok"
    assert marker == {
        "thread_name": parent_name,
        "job_id": 321,
        "has_running_loop": False,
    }
