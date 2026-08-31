import json
import unittest
from unittest.mock import ANY, Mock, patch

from core.account_export import (
    Browser2FARequestError,
    _activate_totp,
    _enroll_totp,
    _browser_activate_totp,
    _browser_enroll_totp,
    _browser_fetch,
    _browser_session_info,
    _browser_reauthenticate,
    _totp_code_with_margin,
    _validate_reauth_otp,
    browser_session_from_driver,
    setup_2fa,
    setup_2fa_from_browser,
)
from core.openai_auth import EmailOtpInvalidError


class Browser2FABridgeTests(unittest.TestCase):
    @patch("core.account_export.BrowserSession")
    def test_copies_cookies_device_and_browser_profile(self, session_class):
        session = Mock()
        session.browser_profile = {}
        session_class.return_value = session
        context = Mock()
        context.cookies.return_value = [
            {"name": "oai-did", "value": "browser-device", "domain": ".example.test", "path": "/", "secure": True},
            {"name": "session", "value": "cookie", "domain": "example.test", "path": "/"},
        ]
        driver = Mock(context=context, page=None)
        driver.execute_script.return_value = {
            "userAgent": "Mozilla/5.0 Chrome/145.0.7632.109 Safari/537.36",
            "language": "en-US", "languages": ["en-US", "en"], "acceptLanguage": "en-US,en",
            "platform": "MacIntel", "vendor": "Google Inc.",
            "userAgentData": {
                "platform": "macOS", "mobile": False,
                "brands": [{"brand": "Google Chrome", "version": "145"}],
            },
            "screenWidth": 1440, "screenHeight": 900, "devicePixelRatio": 2,
            "hardwareConcurrency": 8, "deviceMemory": 8,
            "timezone": "Asia/Tokyo", "timezoneOffset": -540,
        }

        result = browser_session_from_driver(driver, proxy="socks5://proxy.test:1080")

        self.assertIs(result, session)
        self.assertEqual(session.device_id, "browser-device")
        self.assertEqual(session.browser_profile["user_agent"], "Mozilla/5.0 Chrome/145.0.7632.109 Safari/537.36")
        self.assertEqual(session.browser_profile["chrome_major"], "145")
        self.assertEqual(session.browser_profile["chrome_full_version"], "145.0.7632.109")
        self.assertEqual(session.browser_profile["navigator_language"], "en-US")
        self.assertEqual(session.browser_profile["navigator_languages"], ["en-US", "en"])
        self.assertEqual(session.browser_profile["navigator_platform"], "MacIntel")
        self.assertEqual(session.browser_profile["user_agent_data_platform"], "macOS")
        self.assertEqual(session.browser_profile["screen_width"], 1440)
        self.assertEqual(session.browser_profile["hardware_concurrency"], 8)
        self.assertEqual(session.browser_profile["timezone_iana"], "Asia/Tokyo")
        self.assertEqual(session.session.cookies.set.call_count, 2)

    @patch("core.account_export._browser_activate_totp")
    @patch("core.account_export._browser_enroll_totp", return_value=("TOTPSECRET", "SESSION_ID"))
    def test_setup_from_browser_reuses_existing_access_token(self, enroll, activate):
        driver = Mock()
        result = setup_2fa_from_browser(
            driver,
            "user@example.test",
            proxy="proxy",
            previous_otp="683938",
            access_token="ACCESS_TOKEN",
        )
        self.assertEqual(result, "TOTPSECRET")
        enroll.assert_called_once_with(driver, "ACCESS_TOKEN")
        activate.assert_called_once_with(driver, "ACCESS_TOKEN", "TOTPSECRET", "SESSION_ID")

    @patch("core.account_export._browser_activate_totp")
    @patch("core.account_export._browser_reauthenticate", return_value="FRESH_TOKEN")
    @patch("core.account_export._browser_enroll_totp")
    def test_setup_from_browser_reauthenticates_when_enroll_requires_fresh_login(self, enroll, reauth, activate):
        driver = Mock()
        enroll.side_effect = [
            Browser2FARequestError("enroll", 403, "fresh authentication required"),
            ("TOTPSECRET", "SESSION_ID"),
        ]

        result = setup_2fa_from_browser(
            driver,
            "user@example.test",
            previous_otp="683938",
            access_token="OLD_TOKEN",
        )

        self.assertEqual(result, "TOTPSECRET")
        self.assertEqual(enroll.call_args_list[0].args, (driver, "OLD_TOKEN"))
        self.assertEqual(enroll.call_args_list[1].args, (driver, "FRESH_TOKEN"))
        reauth.assert_called_once_with(driver, "user@example.test", previous_otp="683938")
        activate.assert_called_once_with(driver, "FRESH_TOKEN", "TOTPSECRET", "SESSION_ID")

    def test_setup_2fa_excludes_registration_otp_when_waiting_for_reauth_code(self):
        session = Mock()
        with patch("config.email.USE_EMAIL_SERVICE", True), patch(
            "core.account_export._trigger_reauth", return_value="https://auth.example/reauth"
        ), patch("core.account_export._follow_reauth"), patch(
            "core.account_export.human_delay"
        ), patch("core.email_provider.wait_for_otp", return_value="294617") as wait_for_otp, patch(
            "core.account_export._validate_reauth_otp", return_value="https://chatgpt.example/callback"
        ), patch("core.account_export._exchange_new_token", return_value="NEW_TOKEN"), patch(
            "core.account_export._enroll_totp", return_value=("TOTPSECRET", "SESSION_ID")
        ), patch("core.account_export._activate_totp", return_value=True):
            result = setup_2fa(session, "user@example.test", previous_otp="683938")

        self.assertEqual(result, "TOTPSECRET")
        kwargs = wait_for_otp.call_args.kwargs
        self.assertEqual(kwargs["exclude_codes"], {"683938"})
        self.assertIsInstance(kwargs["after_ts"], float)

    def test_setup_2fa_resends_and_excludes_invalid_reauth_otp(self):
        session = Mock()
        seen_exclusions = []
        codes = iter(["111111", "222222"])

        def wait_for_new_otp(*_args, **kwargs):
            seen_exclusions.append(set(kwargs["exclude_codes"]))
            return next(codes)

        with patch("config.email.USE_EMAIL_SERVICE", True), patch(
            "core.account_export._trigger_reauth", return_value="https://auth.example/reauth"
        ), patch("core.account_export._follow_reauth"), patch(
            "core.account_export.human_delay"
        ), patch("core.email_provider.wait_for_otp", side_effect=wait_for_new_otp) as wait_for_otp, patch(
            "core.account_export._validate_reauth_otp",
            side_effect=[EmailOtpInvalidError("expired"), "https://chatgpt.example/callback"],
        ), patch("core.openai_auth.send_email_otp") as resend, patch(
            "core.account_export._exchange_new_token", return_value="NEW_TOKEN"
        ), patch("core.account_export._enroll_totp", return_value=("TOTPSECRET", "SESSION_ID")), patch(
            "core.account_export._activate_totp", return_value=True
        ):
            result = setup_2fa(session, "user@example.test", previous_otp="683938")

        self.assertEqual(result, "TOTPSECRET")
        resend.assert_called_once_with(session)
        self.assertEqual(wait_for_otp.call_count, 2)
        self.assertEqual(seen_exclusions, [{"683938"}, {"683938", "111111"}])

    def test_validate_reauth_otp_classifies_http_401_as_invalid_code(self):
        session = Mock()
        session.get_auth_headers.return_value = {"content-type": "application/json"}
        session.post.return_value = Mock(status_code=401, text='{"code":"invalid_code"}')

        with self.assertRaises(EmailOtpInvalidError):
            _validate_reauth_otp(session, "111111")

    @patch("core.account_export._browser_session_info", return_value={"accessToken": "FRESH_TOKEN"})
    @patch("core.account_export._wait_for_browser_host")
    @patch("core.account_export._browser_trigger_reauth", return_value="https://auth.example/reauth")
    @patch("core.account_export._browser_fetch")
    def test_browser_reauth_resends_after_invalid_otp(self, browser_fetch, trigger, wait_host, session_info):
        browser_fetch.side_effect = [
            {"ok": True, "status": 401, "body": '{"code":"invalid_code"}'},
            {"ok": True, "status": 200, "body": "resent"},
            {
                "ok": True,
                "status": 200,
                "data": {"continue_url": "https://chatgpt.example/callback"},
                "body": '{"continue_url":"https://chatgpt.example/callback"}',
            },
        ]
        driver = Mock()
        seen_exclusions = []
        codes = iter(["111111", "222222"])

        def wait_for_new_otp(*_args, **kwargs):
            seen_exclusions.append(set(kwargs["exclude_codes"]))
            return next(codes)

        with patch("config.email.USE_EMAIL_SERVICE", True), patch(
            "core.email_provider.wait_for_otp", side_effect=wait_for_new_otp
        ) as wait_for_otp, patch("core.account_export.human_delay"):
            result = _browser_reauthenticate(driver, "user@example.test", previous_otp="683938")

        self.assertEqual(result, "FRESH_TOKEN")
        self.assertEqual(browser_fetch.call_count, 3)
        self.assertEqual(browser_fetch.call_args_list[1].kwargs["stage"], "reauth_otp_resend_fetch")
        self.assertEqual(wait_for_otp.call_count, 2)
        self.assertEqual(seen_exclusions, [{"683938"}, {"683938", "111111"}])

    def test_activate_totp_sends_required_factor_type(self):
        session = Mock()
        session.device_id = "device"
        session.navigator_language.return_value = "ja-JP"
        session.get_chatgpt_headers.return_value = {"content-type": "application/json"}
        response = Mock(status_code=200)
        response.json.return_value = {"success": True}
        session.post.return_value = response

        with patch("core.account_export.pyotp.TOTP") as totp_factory:
            totp_factory.return_value.now.return_value = "123456"
            assert _activate_totp(session, "ACCESS", "SECRET", "ENROLL_SESSION") is True

        kwargs = session.post.call_args.kwargs
        assert kwargs["headers"]["origin"] == "https://chatgpt.com"
        assert json.loads(kwargs["data"]) == {
            "code": "123456",
            "factor_type": "totp",
            "session_id": "ENROLL_SESSION",
        }

    def test_browser_fetch_aborts_hung_requests_inside_browser(self):
        driver = Mock()
        driver.execute_async_script.return_value = {
            "ok": False,
            "status": 0,
            "timedOut": True,
            "error": "AbortError: twofa_fetch_timeout",
        }

        with self.assertRaises(Browser2FARequestError) as caught:
            _browser_fetch(driver, "/backend-api/test", stage="activate_fetch", timeout_ms=4321)

        self.assertEqual(caught.exception.stage, "activate_fetch")
        self.assertEqual(caught.exception.status, 0)
        script, *args = driver.execute_async_script.call_args.args
        self.assertIn("AbortController", script)
        self.assertEqual(args[-1], 4321)
        driver.set_script_timeout.assert_called_once_with(25.0)

    def test_browser_fetch_normalizes_driver_script_timeout(self):
        driver = Mock()
        driver.execute_async_script.side_effect = TimeoutError("script timeout")

        with self.assertRaises(Browser2FARequestError) as caught:
            _browser_fetch(driver, "/backend-api/test", stage="activate_fetch")

        self.assertEqual(caught.exception.stage, "activate_fetch")
        self.assertEqual(caught.exception.status, 0)
        self.assertIn("异步脚本超时", caught.exception.detail)
        driver.set_script_timeout.assert_called_once_with(25.0)

    def test_browser_session_info_recovers_transient_failed_to_fetch(self):
        driver = Mock()
        driver.current_url = "https://chatgpt.com/"
        failed = Browser2FARequestError("session_fetch", 0, "TypeError: Failed to fetch")
        success = {
            "ok": True,
            "status": 200,
            "url": "https://chatgpt.com/api/auth/session",
            "data": {"accessToken": "fresh-token", "user": {"email": "target@example.test"}},
        }
        with patch("core.account_export._browser_fetch", side_effect=[failed, success]) as fetch:
            result = _browser_session_info(driver)
        self.assertEqual(result["accessToken"], "fresh-token")
        self.assertEqual(fetch.call_count, 2)

    def test_browser_session_info_stabilizes_auth_origin_before_fetch(self):
        driver = Mock()
        driver.current_url = "https://auth.openai.com/email-verification"
        success = {
            "ok": True,
            "status": 200,
            "url": "https://chatgpt.com/api/auth/session",
            "data": {"accessToken": "fresh-token", "user": {"email": "target@example.test"}},
        }
        with patch("core.account_export._browser_fetch", return_value=success) as fetch, patch(
            "core.roxy_registration._safe_get", return_value=True
        ) as safe_get:
            result = _browser_session_info(driver)
        self.assertEqual(result["accessToken"], "fresh-token")
        safe_get.assert_called_once()
        fetch.assert_called_once()

    @patch("core.account_export._totp_code_with_margin", side_effect=["111111", "222222"])
    @patch("core.account_export._browser_fetch")
    def test_browser_activate_retries_invalid_code_in_next_window(self, browser_fetch, fresh_code):
        browser_fetch.side_effect = [
            {"ok": True, "status": 403, "body": '{"code":"invalid_code","message":"Invalid code"}'},
            {"ok": True, "status": 200, "data": {"success": True}, "body": '{"success":true}'},
        ]
        driver = Mock()
        driver.get_cookies.return_value = [{"name": "oai-did", "value": "device"}]
        driver.execute_script.return_value = "en-US"

        _browser_activate_totp(driver, "ACCESS", "SECRET", "ENROLL_SESSION")

        self.assertEqual(browser_fetch.call_count, 2)
        first = json.loads(browser_fetch.call_args_list[0].kwargs["body"])
        second = json.loads(browser_fetch.call_args_list[1].kwargs["body"])
        self.assertEqual(first["code"], "111111")
        self.assertEqual(second["code"], "222222")
        fresh_code.assert_called_with(ANY, force_next=True)

    @patch("core.account_export._totp_code_with_margin", return_value="123456")
    @patch("core.account_export._browser_fetch")
    def test_browser_activate_retries_generic_invalid_request_without_factor_type(self, browser_fetch, fresh_code):
        """部分后端对带 factor_type 的激活只返回通用 invalid_request。"""
        browser_fetch.side_effect = [
            {"ok": True, "status": 400, "body": '{"code":"invalid_request"}'},
            {"ok": True, "status": 200, "data": {"success": True}, "body": '{"success":true}'},
        ]
        driver = Mock()
        driver.get_cookies.return_value = [{"name": "oai-did", "value": "device"}]
        driver.execute_script.return_value = "en-US"

        _browser_activate_totp(driver, "ACCESS", "SECRET", "ENROLL_SESSION")

        self.assertEqual(browser_fetch.call_count, 2)
        first = json.loads(browser_fetch.call_args_list[0].kwargs["body"])
        second = json.loads(browser_fetch.call_args_list[1].kwargs["body"])
        self.assertEqual(first["factor_type"], "totp")
        self.assertNotIn("factor_type", second)
        fresh_code.assert_called_once()

    @patch("core.account_export.human_delay")
    @patch("core.account_export._browser_activate_totp")
    @patch("core.account_export._browser_enroll_totp", side_effect=[("SECRET_A", "SESSION_A"), ("SECRET_B", "SESSION_B")])
    def test_setup_2fa_reenrolls_after_invalid_activation(self, enroll, activate, delay):
        activate.side_effect = [
            Browser2FARequestError("activate", 400, '{"code":"invalid_request"}'),
            None,
        ]
        driver = Mock()

        result = setup_2fa_from_browser(
            driver,
            "user@example.test",
            previous_otp="111111",
            access_token="ACCESS",
        )

        self.assertEqual(result, "SECRET_B")
        self.assertEqual(enroll.call_count, 2)
        self.assertEqual(activate.call_args_list[0].args[3], "SESSION_A")
        self.assertEqual(activate.call_args_list[1].args[3], "SESSION_B")

    @patch("core.account_export._browser_mfa_enabled", return_value=True)
    @patch("core.account_export._browser_fetch")
    def test_browser_activate_accepts_timeout_when_session_confirms_mfa(self, browser_fetch, mfa_enabled):
        browser_fetch.side_effect = Browser2FARequestError("activate_fetch", 0, "timeout")
        driver = Mock()
        driver.get_cookies.return_value = [{"name": "oai-did", "value": "device"}]
        driver.execute_script.return_value = "en-US"

        with patch("core.account_export._totp_code_with_margin", return_value="123456"):
            _browser_activate_totp(driver, "ACCESS", "SECRET", "ENROLL_SESSION")

        mfa_enabled.assert_called_once_with(driver)

    @patch("core.account_export.time.sleep")
    @patch("core.account_export._browser_fetch")
    def test_browser_enroll_retries_transient_server_errors(self, browser_fetch, sleep):
        browser_fetch.side_effect = [
            {"ok": True, "status": 500, "body": '{"detail":"Internal Server Error"}'},
            {"ok": True, "status": 200, "data": {"secret": "SECRET", "session_id": "SESSION"}, "body": "{}"},
        ]
        driver = Mock()
        driver.get_cookies.return_value = []
        driver.execute_script.return_value = "en-US"

        self.assertEqual(_browser_enroll_totp(driver, "ACCESS"), ("SECRET", "SESSION"))
        self.assertEqual(browser_fetch.call_count, 2)
        sleep.assert_called_once_with(2.0)

    def test_protocol_enroll_retries_transient_server_errors(self):
        session = Mock()
        session.device_id = "device"
        session.navigator_language.return_value = "en-US"
        session.get_chatgpt_headers.return_value = {"content-type": "application/json"}
        first = Mock(status_code=500, text='{"detail":"Internal Server Error"}')
        second = Mock(status_code=200, text='{}')
        second.json.return_value = {"secret": "SECRET", "session_id": "SESSION"}
        session.post.side_effect = [first, second]

        with patch("core.account_export.time.sleep"):
            self.assertEqual(_enroll_totp(session, "ACCESS"), ("SECRET", "SESSION"))

        self.assertEqual(session.post.call_count, 2)

    def test_totp_code_waits_out_near_expiry_window(self):
        totp = Mock(interval=30)
        totp.now.return_value = "654321"
        with patch("core.account_export.time.time", return_value=29.5), patch(
            "core.account_export.time.sleep"
        ) as sleep:
            code = _totp_code_with_margin(totp)

        self.assertEqual(code, "654321")
        sleep.assert_called_once()
        self.assertGreaterEqual(sleep.call_args.args[0], 0.8)


if __name__ == "__main__":
    unittest.main()
