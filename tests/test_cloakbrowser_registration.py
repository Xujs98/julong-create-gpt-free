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


def test_create_password_entry_matches_japanese_password_button_without_href():
    """日语页面用 button 渲染“密码继续”时，不依赖固定中文文案或 href。"""
    driver = Mock()
    password_button = Mock()
    password_button.is_displayed.return_value = True
    password_button.is_enabled.return_value = True
    # 三个固定 href 选择器均为空，第四次调用才返回无 href 的日语按钮标记。
    driver.find_elements.side_effect = [[], [], [], [password_button]]
    driver.execute_script.side_effect = [
        {
            "readyState": "complete",
            "url": "https://auth.example.test/email-verification",
            "passwordHrefCount": 0,
        },
        {
            "ok": True,
            "href": "",
            "selector": '[data-codex-create-password-entry="marker"]',
            "text": "パスワードで続行",
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
    click.assert_called_once_with(driver, password_button, label="create_password_entry")


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


def test_password_entry_accepts_login_password_route_after_signup_branch():
    """新版日语密码分支会落到 /log-in/password，仍需按创建密码表单处理。"""
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
        "core.roxy_registration._is_signup_password_page", return_value=False
    ), patch(
        "core.roxy_registration._is_login_password_page", return_value=True
    ), patch(
        "core.roxy_registration._has_visible_password_input", return_value=True
    ), patch(
        "core.roxy_registration._password_page_state", return_value={"url": "/log-in/password"}
    ), patch("core.roxy_registration._create_password_enabled", return_value=True), patch(
        "core.roxy_registration._registration_password", return_value="Universal1!Password"
    ), patch("core.roxy_registration._human_type_text") as type_text, patch(
        "core.roxy_registration._human_click"
    ) as click, patch("core.roxy_registration.human_delay"), patch(
        "core.roxy_registration._is_email_verification_page", return_value=True
    ):
        result = _fill_password_page_if_present(
            driver, "user@example.test", timeout=3, allow_login_password=True
        )

    assert result == "Universal1!Password"
    type_text.assert_called_once_with(driver, password_input, "Universal1!Password", clear=True)
    click.assert_called_once_with(driver, submit_button, label="password_submit")


def test_otp_single_input_uses_atomic_fill_and_verifies_six_digits():
    from core.roxy_registration import _type_otp

    driver = Mock()
    otp_input = Mock()
    otp_input.is_displayed.return_value = True
    otp_input.is_enabled.return_value = True
    driver.find_elements.return_value = [otp_input]
    driver.execute_script.side_effect = [None, "603241"]

    _type_otp(driver, "603241")

    otp_input.fill.assert_called_once_with("603241")
    assert driver.execute_script.call_count == 2


def test_otp_falls_back_to_plain_six_visible_inputs():
    from core.roxy_registration import _type_otp

    driver = Mock()
    boxes = []
    for _ in range(6):
        box = Mock()
        box.is_displayed.return_value = True
        box.is_enabled.return_value = True
        box.get_attribute.return_value = ""
        boxes.append(box)
    # Specific selectors find nothing; the generic input query returns six plain boxes.
    driver.find_elements.side_effect = [[], [], [], [], boxes]

    _type_otp(driver, "603241", timeout=1)

    for box, char in zip(boxes, "603241"):
        box.send_keys.assert_called_once_with(char)


def test_wait_for_email_otp_page_classifies_login_password_before_typing():
    from core.roxy_registration import _wait_for_email_otp_page

    driver = Mock(current_url="https://auth.openai.com/log-in/password")
    with patch("core.roxy_registration._check_manual_stop"), patch(
        "core.roxy_registration._has_access_token", return_value=False
    ), patch("core.roxy_registration._is_login_password_page", return_value=True):
        assert _wait_for_email_otp_page(driver, timeout=1) == "login_password"


def test_wait_for_email_otp_page_waits_through_cloudflare_then_accepts_otp():
    from core.roxy_registration import _wait_for_email_otp_page

    driver = Mock(current_url="https://auth.openai.com/email-verification")
    with patch("core.roxy_registration._check_manual_stop"), patch(
        "core.roxy_registration._has_access_token", return_value=False
    ), patch("core.roxy_registration._is_login_password_page", return_value=False), patch(
        "core.roxy_registration._is_email_verification_page", side_effect=[False, True]
    ), patch(
        "core.roxy_registration._cloudflare_challenge_state", side_effect=[{"challenge": True}, {"challenge": False}]
    ), patch("core.roxy_registration._wait_for_runtime_challenge_if_present", return_value=True), patch(
        "core.roxy_registration.time.sleep"
    ):
        assert _wait_for_email_otp_page(driver, timeout=1) == "otp"


def test_email_verification_url_with_profile_dom_is_not_otp_page():
    """资料页原地重渲染时 URL 仍可能是 email-verification。"""
    from core.roxy_registration import _is_email_verification_page

    driver = Mock(current_url="https://auth.example.test/email-verification")
    profile_snapshot = {
        "url": driver.current_url,
        "inputs": [{"name": "name"}, {"name": "age"}],
        "widgets": [{"role": "spinbutton"}],
    }
    with patch(
        "core.roxy_registration._email_otp_page_state",
        return_value={"inputs": [{"name": "name"}, {"name": "age"}]},
    ), patch("core.roxy_registration._page_snapshot", return_value=profile_snapshot), patch(
        "core.roxy_registration._is_profile_like", return_value=True
    ):
        assert _is_email_verification_page(driver) is False


def test_click_continue_uses_structural_otp_submit_before_generic_locator():
    from core.roxy_registration import _click_continue

    driver = Mock()
    driver.execute_script.return_value = {
        "ok": True,
        "reason": "clicked_primary_submit",
        "text": "続行",
    }

    _click_continue(driver)

    driver.execute_script.assert_called_once()
    driver.find_elements.assert_not_called()


def test_wait_after_otp_reports_stalled_when_page_has_no_validation_error():
    from core.roxy_registration import _wait_after_email_otp_submit

    driver = Mock()
    state = {
        "inputs": [{"name": "code", "ariaInvalid": "", "value": "123456"}],
        "errors": [],
    }
    with patch("core.roxy_registration._is_email_verification_page", return_value=True), patch(
        "core.roxy_registration._email_otp_page_state", return_value=state
    ), patch("core.roxy_registration.time.sleep"):
        assert _wait_after_email_otp_submit(driver, timeout=0) == "stalled"


def test_cloak_navigation_retries_transient_timeout_then_succeeds():
    from core.cloakbrowser_registration import _safe_cloak_get

    driver = Mock()
    driver.get.side_effect = [RuntimeError("Page.goto: Timeout 90000ms exceeded"), None]
    with patch("core.cloakbrowser_registration._check_manual_stop"), patch(
        "core.cloakbrowser_registration._page_ready_after_navigation", return_value=False
    ), patch("core.cloakbrowser_registration.time.sleep"):
        _safe_cloak_get(driver, "https://chatgpt.com/auth/login", attempts=2)

    assert driver.get.call_count == 2


def test_cloak_navigation_accepts_timeout_when_target_dom_is_ready():
    from core.cloakbrowser_registration import _safe_cloak_get

    driver = Mock()
    driver.get.side_effect = RuntimeError("Page.goto: Timeout")
    with patch("core.cloakbrowser_registration._check_manual_stop"), patch(
        "core.cloakbrowser_registration._page_ready_after_navigation", return_value=True
    ):
        _safe_cloak_get(driver, "https://chatgpt.com/auth/login", attempts=2)

    driver.get.assert_called_once()


def test_initial_real_browser_challenge_rejects_proxy_immediately():
    driver = Mock(upstream_proxy_url="http://proxy.test:8080")
    with patch.object(
        cloakbrowser_registration._proxy_cfg,
        "PROXY_BROWSER_CHALLENGE_AUTO_ROTATE",
        True,
    ), patch(
        "core.cloakbrowser_registration._cloudflare_challenge_state",
        return_value={"challenge": True},
    ):
        try:
            cloakbrowser_registration._reject_initial_browser_proxy_challenge(
                driver, "http://proxy.test:8080"
            )
        except RuntimeError as exc:
            assert "BrowserProxyChallenge" in str(exc)
            assert "自动换代理" in str(exc)
        else:
            raise AssertionError("real-browser challenge must reject the current proxy")


def test_initial_real_browser_challenge_keeps_manual_wait_when_auto_rotate_disabled():
    driver = Mock(upstream_proxy_url="http://proxy.test:8080")
    with patch.object(
        cloakbrowser_registration._proxy_cfg,
        "PROXY_BROWSER_CHALLENGE_AUTO_ROTATE",
        False,
    ), patch("core.cloakbrowser_registration._cloudflare_challenge_state") as challenge_state:
        cloakbrowser_registration._reject_initial_browser_proxy_challenge(
            driver, "http://proxy.test:8080"
        )

    challenge_state.assert_not_called()


def test_cloak_initial_challenge_returns_actual_auto_selected_proxy_to_service():
    driver = Mock(upstream_proxy_url="socks5h://user:pass@proxy.test:1080")
    opened = Mock(profile_id="cloak-test", raw={})
    with patch(
        "core.cloakbrowser_registration.build_cloak_driver",
        return_value=(driver, opened),
    ), patch.object(cloakbrowser_registration._cfg, "CLOAK_ENABLE_AGENT", False), patch.object(
        cloakbrowser_registration._cfg, "CLOAK_KEEP_BROWSER_OPEN_ON_ERROR", False
    ), patch.object(
        cloakbrowser_registration._cfg, "CLOAK_KEEP_BROWSER_OPEN", False
    ), patch(
        "core.cloakbrowser_registration._safe_cloak_get"
    ), patch(
        "core.cloakbrowser_registration.human_delay"
    ), patch(
        "core.cloakbrowser_registration._reject_initial_browser_proxy_challenge",
        side_effect=RuntimeError("BrowserProxyChallenge: Cloudflare 人机验证"),
    ):
        result = cloakbrowser_registration._run_cloak_registration_impl(
            "user@example.test", "Test User", "1990-01-01", proxy=None
        )

    assert result["success"] is False
    assert result["_failed_proxy_url"] == "socks5h://user:pass@proxy.test:1080"
    driver.quit.assert_called_once()


def test_cloak_browser_proxy_challenge_returns_to_service_without_same_proxy_retry():
    failed = {
        "success": False,
        "error": "RuntimeError: BrowserProxyChallenge: Cloudflare 人机验证",
    }
    with patch.object(cloakbrowser_registration._cfg, "CLOAK_NAVIGATION_RETRIES", 3), patch(
        "core.cloakbrowser_registration._run_in_isolated_thread", return_value=failed
    ) as run_once:
        result = cloakbrowser_registration.run_cloak_registration(
            "user@example.test", "Test User", "1990-01-01", proxy="http://proxy.test:8080"
        )

    assert result == failed
    run_once.assert_called_once()


def test_email_entry_falls_back_to_welcome_login_anchor():
    from core.roxy_registration import _click_email_entry_option

    driver = Mock()
    target = Mock()
    driver.execute_script.return_value = target
    with patch("core.roxy_registration._is_oauth_consent_like", return_value=False), patch(
        "core.roxy_registration._human_click"
    ) as click:
        assert _click_email_entry_option(driver) is True
    click.assert_called_once_with(driver, target, label="email_entry")


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
