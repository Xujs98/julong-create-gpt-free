# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from core.cloakbrowser_driver import _normalize_proxy as normalize_cloak_proxy
from core.proxy_test import test_proxy as run_proxy_test


class ProxyTestTests(unittest.TestCase):
    def test_cloak_uses_remote_dns_for_explicit_socks5(self):
        self.assertEqual(
            normalize_cloak_proxy("socks5://user:pass@proxy.example:3000"),
            "socks5h://user:pass@proxy.example:3000",
        )

    @patch("core.proxy_test.Session")
    def test_socks5_prefers_remote_dns(self, session_class):
        session = MagicMock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {
            "ip": "203.0.113.9",
            "country": "US",
            "region": "Georgia",
            "city": "Milledgeville",
        }
        session_class.return_value = session

        with patch("core.proxy_test._browser_cfg.IP_GEO_ENDPOINTS", ["https://geo.example/json"]):
            result = run_proxy_test("socks5://user:pass@proxy.example:3000", timeout=2)

        self.assertTrue(result["ok"])
        self.assertEqual(result["dns_mode"], "proxy")
        self.assertEqual(result["proxy"], "socks5h://***:***@proxy.example:3000")
        self.assertEqual(
            session.proxies,
            {
                "http": "socks5h://user:pass@proxy.example:3000",
                "https": "socks5h://user:pass@proxy.example:3000",
            },
        )


if __name__ == "__main__":
    unittest.main()
