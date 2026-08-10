import json
import unittest
from unittest.mock import Mock, patch

from core.account_export import _activate_totp, browser_session_from_driver, setup_2fa, setup_2fa_from_browser


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

    @patch("core.account_export.setup_2fa", return_value="TOTPSECRET")
    @patch("core.account_export.fetch_session")
    @patch("core.account_export.browser_session_from_driver")
    def test_setup_from_browser_closes_protocol_session(self, bridge, fetch, setup):
        session = bridge.return_value
        result = setup_2fa_from_browser(
            Mock(),
            "user@example.test",
            proxy="proxy",
            previous_otp="683938",
        )
        self.assertEqual(result, "TOTPSECRET")
        fetch.assert_called_once_with(session)
        setup.assert_called_once_with(session, "user@example.test", previous_otp="683938")
        session.session.close.assert_called_once_with()

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

    def test_activate_totp_uses_minimal_payload(self):
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
            "session_id": "ENROLL_SESSION",
        }


if __name__ == "__main__":
    unittest.main()
