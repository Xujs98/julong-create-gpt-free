import unittest
from unittest.mock import Mock, patch

from core.roxy_registration import (
    _cloudflare_challenge_state,
    _wait_for_cloudflare_challenge,
)


class CloudflareChallengeWaitTests(unittest.TestCase):
    def test_detects_challenge_state(self):
        driver = Mock()
        driver.execute_script.return_value = {
            "challenge": True,
            "title": "Just a moment...",
            "url": "https://example.test/auth/login",
            "markers": ["#challenge-stage"],
        }
        self.assertTrue(_cloudflare_challenge_state(driver)["challenge"])

    def test_detects_chinese_verify_human_page(self):
        """识别截图中的“正在进行安全验证 / 请验证您是真人”页面。"""
        driver = Mock()
        driver.execute_script.return_value = {
            "challenge": True,
            "title": "正在进行安全验证",
            "url": "https://auth.example.test/",
            "markers": ['iframe[title*="验证"]'],
            "visibleText": "正在进行安全验证 请验证您是真人 CLOUDFLARE",
            "textChallenge": True,
        }
        state = _cloudflare_challenge_state(driver)
        self.assertTrue(state["challenge"])
        self.assertTrue(state["textChallenge"])

    def test_detects_japanese_widget_by_structure_not_copy(self):
        """日语或其它语言下只依赖可见 Cloudflare 结构，不依赖按钮文案。"""
        driver = Mock()
        driver.execute_script.return_value = {
            "challenge": True,
            "title": "しばらくお待ちください...",
            "url": "https://auth.example.test/",
            "markers": ['iframe[src*="challenges.cloudflare.com"]'],
            "visibleText": "私はロボットではありません",
            "textChallenge": False,
        }
        state = _cloudflare_challenge_state(driver)
        self.assertTrue(state["challenge"])
        self.assertFalse(state["textChallenge"])
        self.assertTrue(state["markers"])

    def test_email_verification_page_is_not_cloudflare_wait(self):
        """正常邮箱验证码页不得被误判为 Cloudflare 验证页。"""
        driver = Mock()
        driver.execute_script.return_value = {
            "challenge": True,  # 模拟旧逻辑已经产生的误判
            "title": "受信箱を確認してください - OpenAI",
            "url": "https://auth.openai.com/email-verification",
            "markers": [],
            "textChallenge": False,
            "authFlow": True,
            "otpOrPasswordForm": True,
        }
        state = _cloudflare_challenge_state(driver)
        assert state["challenge"] is False
        assert state["normalAuthPage"] is True

    def test_auth_page_with_visible_turnstile_still_waits(self):
        """认证页若确有可见 Turnstile，仍按验证盾处理。"""
        driver = Mock()
        driver.execute_script.return_value = {
            "challenge": True,
            "title": "Verify",
            "url": "https://auth.openai.com/email-verification",
            "markers": ['iframe[src*="challenges.cloudflare.com"]'],
            "textChallenge": False,
            "authFlow": True,
            "otpOrPasswordForm": True,
        }
        state = _cloudflare_challenge_state(driver)
        assert state["challenge"] is True
        assert state["normalAuthPage"] is False

    def test_headless_mode_fails_with_actionable_message(self):
        driver = Mock()
        driver.execute_script.return_value = {"challenge": True, "title": "Just a moment..."}
        with self.assertRaisesRegex(RuntimeError, "关闭 Cloak无头"):
            _wait_for_cloudflare_challenge(driver, headless=True)

    @patch("core.roxy_registration.time.sleep")
    def test_visible_mode_continues_after_manual_verification(self, sleep):
        driver = Mock()
        driver.execute_script.side_effect = [
            {"challenge": True, "title": "Just a moment..."},
            {"challenge": True, "title": "Just a moment..."},
            {"challenge": False, "title": "Log in"},
        ]
        self.assertTrue(_wait_for_cloudflare_challenge(driver, timeout=30, headless=False))
        self.assertTrue(sleep.called)

    @patch("core.roxy_registration.time.sleep")
    def test_takeover_agent_handles_challenge_before_waiting(self, sleep):
        """检测到验证盾后应立即调用 Agent，而不是先进入固定等待。"""
        driver = Mock()
        driver.execute_script.side_effect = [
            {"challenge": True, "title": "Just a moment..."},
            {"challenge": False, "title": "Log in"},
        ]
        agent = Mock()
        agent.snapshot.return_value = {
            "challenge_frames": [{"selector": "#challenge-frame", "tag": "iframe"}]
        }
        result = Mock()
        result.executed = 1
        result.executed_actions = [{"type": "click", "selector": "#challenge-frame"}]
        agent.assist.return_value = result

        self.assertTrue(
            _wait_for_cloudflare_challenge(
                driver,
                timeout=30,
                headless=False,
                agent=agent,
            )
        )

        agent.snapshot.assert_called_once_with(driver)
        self.assertEqual(agent.assist.call_args.args[1:4], ("challenge", {}))
        self.assertTrue(agent.assist.call_args.kwargs["force"])


if __name__ == "__main__":
    unittest.main()
