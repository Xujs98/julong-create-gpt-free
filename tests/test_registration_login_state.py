import unittest
from unittest.mock import Mock, patch

from core.roxy_registration import (
    _click_resend_email_otp,
    _recover_email_authorize_once,
    _submit_email_and_wait_next,
)


class RegistrationLoginStateTests(unittest.TestCase):
    @patch("core.roxy_registration._has_access_token", return_value=True)
    @patch("core.roxy_registration._type_email_address")
    def test_existing_session_skips_email_entry(self, type_email, has_token):
        result = _submit_email_and_wait_next(Mock(), "user@example.test")
        self.assertEqual(result, "logged_in")
        type_email.assert_not_called()
        has_token.assert_called_once()

    @patch("core.roxy_registration._is_email_verification_page", return_value=False)
    def test_resend_wait_accepts_completed_navigation(self, is_verification):
        result = _click_resend_email_otp(Mock(), timeout=1)
        self.assertEqual(result["reason"], "already_accepted")
        is_verification.assert_called_once()

    @patch("core.roxy_registration._submit_email_via_browser_nextauth")
    def test_authorize_diagnostic_classifies_http_429_as_risk_control(self, submit):
        driver = Mock()
        submit.return_value = {
            "ok": False,
            "stage": "signin",
            "status": 429,
            "body": "Too Many Requests",
        }

        result = _recover_email_authorize_once(driver, "user@example.test")

        self.assertTrue(result["riskSignal"])
        self.assertEqual(result["status"], 429)
        driver.get.assert_not_called()

    @patch("core.roxy_registration._submit_email_via_browser_nextauth")
    def test_authorize_diagnostic_navigates_once_on_http_success(self, submit):
        driver = Mock()
        submit.return_value = {
            "ok": True,
            "stage": "authorize_url",
            "status": 200,
            "url": "https://auth.example.test/authorize?id=1",
        }

        result = _recover_email_authorize_once(driver, "user@example.test")

        self.assertFalse(result["riskSignal"])
        driver.get.assert_called_once_with("https://auth.example.test/authorize?id=1")

    @patch("core.roxy_registration._wait_email_submit_next_state", return_value="risk_control")
    @patch("core.roxy_registration._submit_email_step")
    @patch("core.roxy_registration.human_delay")
    @patch("core.roxy_registration._email_input_value_state")
    @patch("core.roxy_registration._type_email_address")
    @patch("core.roxy_registration._has_access_token", return_value=False)
    def test_risk_control_stops_without_repeated_email_submissions(
        self, has_token, type_email, input_state, delay, submit_step, wait_state
    ):
        driver = Mock()
        driver._last_email_submit_diagnostic = {"status": 403, "riskSignal": True}
        input_state.return_value = {
            "url": "https://chatgpt.example.test/auth/login",
            "inputs": [{"value": "user@example.test"}],
        }

        with self.assertRaisesRegex(RuntimeError, "风控/限流"):
            _submit_email_and_wait_next(driver, "user@example.test", attempts=3)

        type_email.assert_called_once()
        submit_step.assert_called_once()

    @patch("core.roxy_registration._email_entry_state")
    @patch("core.roxy_registration._wait_for_runtime_challenge_if_present", return_value=False)
    @patch("core.roxy_registration._has_access_token", return_value=False)
    def test_browser_network_error_is_classified_separately(self, has_token, challenge, entry_state):
        driver = Mock()
        driver.current_url = "chrome-error://chromewebdata/"
        entry_state.return_value = {
            "errorCode": "ERR_PROXY_CONNECTION_CLOSED",
            "bodyText": "This site can't be reached ERR_PROXY_CONNECTION_CLOSED",
        }

        from core.roxy_registration import _wait_email_submit_next_state

        result = _wait_email_submit_next_state(driver, "user@example.test", timeout=1)

        self.assertEqual(result, "network_error")
        self.assertEqual(driver._last_email_submit_diagnostic["kind"], "browser_network_error")


if __name__ == "__main__":
    unittest.main()
