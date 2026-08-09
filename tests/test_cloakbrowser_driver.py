import unittest
from unittest.mock import Mock, patch

from core.cloakbrowser_driver import CloakElement, _resolve_cloak_launch_identity


class CloakElementTests(unittest.TestCase):
    def setUp(self):
        self.page = Mock()
        self.locator = Mock()
        self.element = CloakElement(self.page, locator=self.locator)

    def test_send_keys_appends_instead_of_replacing_value(self):
        self.element.send_keys("ab")
        self.locator.press_sequentially.assert_called_once_with(
            "ab", delay=35, timeout=10000,
        )
        self.locator.fill.assert_not_called()

    def test_send_keys_maps_backspace(self):
        self.element.send_keys("\ue003")
        self.page.keyboard.press.assert_called_once_with("Backspace")
        self.locator.press_sequentially.assert_not_called()

    def test_fill_uses_atomic_locator_fill(self):
        self.element.fill("user@example.test")
        self.locator.fill.assert_called_once_with("user@example.test", timeout=10000)
        self.locator.press_sequentially.assert_not_called()

    def test_send_keys_maps_command_select_all(self):
        self.element.send_keys("\ue03d", "a")
        self.page.keyboard.press.assert_called_once_with("Meta+A")
        self.locator.press_sequentially.assert_not_called()

    def test_text_reads_playwright_inner_text_for_selenium_compatibility(self):
        self.locator.inner_text.return_value = "メールを再送信する"
        self.assertEqual(self.element.text, "メールを再送信する")
        self.locator.inner_text.assert_called_once_with(timeout=1000)


class CloakFingerprintIdentityTests(unittest.TestCase):
    def test_random_mode_generates_new_seed_and_disables_persistent_context(self):
        """每次随机模式应生成新 seed，并且不复用固定 cookies/cache 目录。"""
        with patch("config.cloakbrowser.CLOAK_RANDOMIZE_FINGERPRINT_EACH_LAUNCH", True), patch(
            "config.cloakbrowser.CLOAK_FINGERPRINT_SEED", "fixed-seed"
        ), patch("config.cloakbrowser.CLOAK_USER_DATA_DIR", "/tmp/fixed-profile"), patch(
            "core.cloakbrowser_driver.secrets.randbelow", side_effect=[100, 200]
        ):
            first = _resolve_cloak_launch_identity()
            second = _resolve_cloak_launch_identity()

        self.assertEqual(first, ("101", ""))
        self.assertEqual(second, ("201", ""))
        self.assertNotEqual(first[0], second[0])

    def test_fixed_mode_respects_configured_seed_and_user_directory(self):
        """关闭每次随机后，显式固定画像配置仍保持兼容。"""
        with patch("config.cloakbrowser.CLOAK_RANDOMIZE_FINGERPRINT_EACH_LAUNCH", False), patch(
            "config.cloakbrowser.CLOAK_FINGERPRINT_SEED", "fixed-seed"
        ), patch("config.cloakbrowser.CLOAK_USER_DATA_DIR", "/tmp/fixed-profile"):
            identity = _resolve_cloak_launch_identity()

        self.assertEqual(identity, ("fixed-seed", "/tmp/fixed-profile"))


class CloakLaunchOptionsTests(unittest.TestCase):
    def test_timezone_is_not_passed_twice_to_cloak_and_context(self):
        """Cloak 的 timezone flag 与 Playwright context 不重复设置，避免回退宿主机时区。"""
        from core import cloakbrowser_driver as module

        browser = Mock()
        context = Mock()
        page = Mock()
        browser.new_context.return_value = context
        context.new_page.return_value = page
        with patch.object(module._cfg, "CLOAK_USE_PROXY", False), patch.object(
            module._cfg, "CLOAK_HEADLESS", True
        ), patch.object(module._cfg, "CLOAK_HUMANIZE", False), patch.object(
            module._cfg, "CLOAK_GEOIP", False
        ), patch.object(module, "_build_cloak_locale_options", return_value={
            "locale": "ja-JP",
            "timezone": "Asia/Tokyo",
            "accept_language": "ja-JP,ja;q=0.9",
        }), patch.object(module, "_resolve_cloak_launch_identity", return_value=("12345", "")), patch(
            "cloakbrowser.launch", return_value=browser
        ) as launch:
            driver, _ = module.build_cloak_driver(proxy="")

        launch.assert_called_once()
        opts = launch.call_args.kwargs
        self.assertEqual(opts["timezone"], "Asia/Tokyo")
        self.assertEqual(opts["locale"], "ja-JP")
        self.assertEqual(opts["args"], ["--fingerprint=12345"])
        context_kwargs = browser.new_context.call_args.kwargs
        self.assertEqual(context_kwargs["locale"], "ja-JP")
        self.assertNotIn("timezone_id", context_kwargs)
        self.assertEqual(
            context_kwargs["extra_http_headers"],
            {"Accept-Language": "ja-JP,ja;q=0.9"},
        )
        driver.quit()


class CloakPageRecoveryTests(unittest.TestCase):
    def test_switches_to_live_page_after_challenge_navigation(self):
        """验证盾完成后旧页面关闭时，适配层自动切换到新页面。"""
        from core.cloakbrowser_driver import CloakSeleniumDriver

        old_page = Mock()
        old_page.is_closed.return_value = True
        new_page = Mock()
        new_page.is_closed.return_value = False
        new_page.url = "https://auth.openai.com/login"
        context = Mock()
        context.pages = [new_page]
        driver = CloakSeleniumDriver(browser=Mock(), context=context, page=old_page)

        self.assertEqual(driver.current_url, "https://auth.openai.com/login")
        self.assertIs(driver.page, new_page)
        new_page.bring_to_front.assert_called()


if __name__ == "__main__":
    unittest.main()
