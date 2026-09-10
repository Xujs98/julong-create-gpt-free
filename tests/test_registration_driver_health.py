# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

import requests

from config import browser_use, roxybrowser, skyvern
from core.registration_driver_health import (
    normalize_registration_driver,
    registration_driver_preflight,
    registration_driver_runtime_preflight,
    require_registration_driver_ready,
    roxy_api_runtime_check,
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

    @patch("core.registration_driver_health.socket.create_connection")
    def test_roxy_runtime_check_reports_reachable_endpoint(self, create_connection):
        create_connection.return_value.__enter__.return_value = object()
        result = roxy_api_runtime_check("http://127.0.0.1:50000")
        self.assertTrue(result["reachable"], result)
        self.assertEqual(result["host"], "127.0.0.1")
        self.assertEqual(result["port"], 50000)
        create_connection.assert_called_once_with(("127.0.0.1", 50000), timeout=0.8)

    @patch("core.registration_driver_health.socket.create_connection")
    @patch("core.roxy_selenium.normalize_api_base", return_value="http://192.168.65.254:50003")
    def test_roxy_runtime_check_uses_docker_rewritten_api_address(self, normalize, create_connection):
        create_connection.return_value.__enter__.return_value = object()

        result = roxy_api_runtime_check("http://127.0.0.1:50003")

        self.assertTrue(result["reachable"], result)
        self.assertEqual(result["configured_api_base"], "http://127.0.0.1:50003")
        self.assertEqual(result["api_base"], "http://192.168.65.254:50003")
        self.assertTrue(result["runtime_rewritten"])
        self.assertEqual(result["host"], "192.168.65.254")
        normalize.assert_called_once_with("http://127.0.0.1:50003")
        create_connection.assert_called_once_with(("192.168.65.254", 50003), timeout=0.8)

    @patch("core.registration_driver_health.requests.get")
    @patch("core.registration_driver_health.socket.create_connection")
    def test_roxy_runtime_check_probes_read_only_workspace_api(self, create_connection, get):
        create_connection.return_value.__enter__.return_value = object()
        response = get.return_value
        response.status_code = 200
        response.ok = True
        response.json.return_value = {"code": 0, "data": {"rows": []}}

        result = roxy_api_runtime_check("127.0.0.1:50003", probe_http=True)

        self.assertTrue(result["reachable"], result)
        self.assertTrue(result["tcp_reachable"])
        self.assertTrue(result["http_checked"])
        self.assertTrue(result["http_reachable"])
        self.assertEqual(result["http_status"], 200)
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], "http://127.0.0.1:50003/browser/workspace")

    @patch("core.registration_driver_health.requests.get", side_effect=requests.exceptions.ReadTimeout("slow Roxy"))
    @patch("core.registration_driver_health.socket.create_connection")
    def test_roxy_runtime_check_distinguishes_http_read_timeout(self, create_connection, _get):
        create_connection.return_value.__enter__.return_value = object()

        result = roxy_api_runtime_check("http://127.0.0.1:50003", probe_http=True)

        self.assertTrue(result["tcp_reachable"], result)
        self.assertFalse(result["http_reachable"], result)
        self.assertEqual(result["http_attempts"], 2)
        self.assertIn("HTTP read timeout", result["error"])

    @patch("core.registration_driver_health.time.sleep")
    @patch("core.registration_driver_health.requests.get")
    @patch("core.registration_driver_health.socket.create_connection")
    def test_roxy_runtime_check_recovers_from_cold_http_probe(self, create_connection, get, sleep):
        create_connection.return_value.__enter__.return_value = object()
        response = Mock(status_code=200, ok=True)
        response.json.return_value = {"code": 0, "data": {"rows": []}}
        get.side_effect = [requests.exceptions.ReadTimeout("cold Roxy"), response]

        with patch.object(roxybrowser, "ROXY_API_TIMEOUT", 30), patch.object(
            roxybrowser, "ROXY_API_RETRIES", 3
        ):
            result = roxy_api_runtime_check("http://127.0.0.1:50003", probe_http=True)

        self.assertTrue(result["reachable"], result)
        self.assertEqual(result["http_attempts"], 2)
        self.assertEqual(result["http_timeout"], 5.0)
        self.assertTrue(result["http_recovered_after_retry"])
        self.assertIsNone(result["error"])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["timeout"], (0.8, 5.0))
        sleep.assert_called_once_with(0.2)

    @patch("core.registration_driver_health.requests.get")
    @patch("core.registration_driver_health.socket.create_connection")
    def test_roxy_runtime_check_rejects_http_error_status(self, create_connection, get):
        create_connection.return_value.__enter__.return_value = object()
        get.return_value.status_code = 401
        get.return_value.ok = False

        result = roxy_api_runtime_check("http://127.0.0.1:50003", probe_http=True)

        self.assertTrue(result["tcp_reachable"], result)
        self.assertFalse(result["reachable"], result)
        self.assertFalse(result["http_reachable"], result)
        self.assertEqual(result["http_error"], "HTTP status 401")

    def test_roxy_static_preflight_canonicalizes_bare_api_base(self):
        with patch.object(roxybrowser, "ROXY_API_BASE", "127.0.0.1:50003"), patch.object(
            roxybrowser, "ROXY_API_TOKEN", "key"
        ):
            result = registration_driver_preflight("roxy")
        self.assertNotIn("ROXY_API_BASE 不是有效 HTTP 地址", result["errors"])
        self.assertEqual(result["details"]["api_base"], "http://127.0.0.1:50003")

    @patch("core.registration_driver_health.registration_driver_runtime_preflight")
    def test_require_roxy_driver_uses_runtime_preflight(self, runtime):
        runtime.return_value = {
            "driver": "roxy",
            "label": "RoxyBrowser",
            "ok": True,
            "errors": [],
            "details": {},
        }

        result = require_registration_driver_ready("roxy", runtime=True)

        self.assertTrue(result["ok"])
        runtime.assert_called_once_with("roxy")

    @patch(
        "core.registration_driver_health.socket.create_connection",
        side_effect=ConnectionRefusedError(61, "Connection refused"),
    )
    def test_roxy_runtime_preflight_reports_connection_failure(self, _create_connection):
        with patch.object(roxybrowser, "ROXY_API_TOKEN", "key"):
            result = registration_driver_runtime_preflight("roxy")
        self.assertFalse(result["ok"])
        self.assertFalse(result["details"]["reachable"])
        self.assertIn("Roxy API 不可达", result["errors"][-1])

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
