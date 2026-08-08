# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import browser_use, roxybrowser, skyvern
from core.registration_driver_health import (
    normalize_registration_driver,
    registration_driver_preflight,
)
from core.roxybrowser_client import _proxy_url_to_roxy_info
from core.session import BrowserSession


class RegistrationDriverHealthTests(unittest.TestCase):
    def test_normalizes_all_driver_aliases(self):
        self.assertEqual(normalize_registration_driver("api"), "protocol")
        self.assertEqual(normalize_registration_driver("roxybrowser"), "roxy")
        self.assertEqual(normalize_registration_driver("cloakbrowser"), "cloak")
        self.assertEqual(normalize_registration_driver("browser-use"), "browser_use")
        self.assertEqual(normalize_registration_driver("sv"), "skyvern")

    def test_protocol_is_ready_with_installed_dependencies(self):
        with patch("config.proxy.pick_proxy", return_value=""):
            result = registration_driver_preflight("protocol")
        self.assertTrue(result["ok"], result)

    def test_roxy_reports_missing_token(self):
        with patch.object(roxybrowser, "ROXY_API_TOKEN", ""):
            result = registration_driver_preflight("roxy")
        self.assertFalse(result["ok"])
        self.assertIn("ROXY_API_TOKEN 为空", result["errors"])

    def test_browser_use_reports_missing_key(self):
        with patch.object(browser_use, "BROWSER_USE_API_KEY", ""):
            result = registration_driver_preflight("browser_use")
        self.assertFalse(result["ok"])
        self.assertIn("BROWSER_USE_API_KEY 为空", result["errors"])

    def test_skyvern_reports_missing_key(self):
        with patch.object(skyvern, "SKYVERN_API_KEY", ""):
            result = registration_driver_preflight("skyvern")
        self.assertFalse(result["ok"])
        self.assertIn("SKYVERN_API_KEY 为空", result["errors"])

    def test_external_drivers_are_ready_with_required_keys(self):
        with patch.object(roxybrowser, "ROXY_API_TOKEN", "key"), patch.object(
            browser_use, "BROWSER_USE_API_KEY", "key"
        ), patch.object(skyvern, "SKYVERN_API_KEY", "key"):
            self.assertTrue(registration_driver_preflight("roxy")["ok"])
            self.assertTrue(registration_driver_preflight("browser_use")["ok"])
            self.assertTrue(registration_driver_preflight("skyvern")["ok"])

    @patch("core.proxy_utils._endpoint_supports_socks5", return_value=True)
    def test_protocol_session_normalizes_four_part_proxy(self, _supports_socks5):
        session = BrowserSession(proxy="proxy.example:3000:user:pass", detect_exit_geo=False)
        self.assertEqual(session.proxy, "socks5h://user:pass@proxy.example:3000")
        self.assertEqual(session.session.proxies["https"], session.proxy)

    @patch("core.proxy_utils._endpoint_supports_socks5", return_value=True)
    def test_roxy_accepts_four_part_proxy(self, _supports_socks5):
        info = _proxy_url_to_roxy_info("proxy.example:3000:user:pass")
        self.assertEqual(info["protocol"], "SOCKS5")
        self.assertEqual(info["host"], "proxy.example")
        self.assertEqual(info["port"], "3000")
        self.assertEqual(info["proxyUserName"], "user")
        self.assertEqual(info["proxyPassword"], "pass")


if __name__ == "__main__":
    unittest.main()
