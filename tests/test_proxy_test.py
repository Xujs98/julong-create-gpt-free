# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from core.cloakbrowser_driver import _normalize_proxy as normalize_cloak_proxy
from core.proxy_test import ProxyTestError, test_proxy as run_proxy_test, test_proxy_pool as run_proxy_pool_test


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

    @patch("core.proxy_test.test_proxy")
    def test_pool_preflight_requires_every_proxy_to_pass(self, test_one):
        """代理池中任一项失败时，注册前检查整体失败。"""
        test_one.side_effect = [
            {"ok": True, "proxy": "http://proxy-a.test:8080", "ip": "203.0.113.1"},
            ProxyTestError("连接超时"),
        ]

        with self.assertRaisesRegex(ProxyTestError, "1/2 个代理不可用"):
            run_proxy_pool_test(
                ["http://proxy-a.test:8080", "http://user:secret@proxy-b.test:8080"],
                timeout=2,
                max_workers=1,
            )

        self.assertEqual(test_one.call_count, 2)

    @patch("core.proxy_test.test_proxy")
    def test_pool_preflight_returns_summary_when_all_proxies_pass(self, test_one):
        """全部代理连通时返回可用于任务启动日志的汇总。"""
        test_one.side_effect = [
            {"ok": True, "proxy": "http://proxy-a.test:8080", "ip": "203.0.113.1"},
            {"ok": True, "proxy": "http://proxy-b.test:8080", "ip": "203.0.113.2"},
        ]

        result = run_proxy_pool_test(
            ["http://proxy-a.test:8080", "http://proxy-b.test:8080"],
            timeout=2,
            max_workers=1,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["available"], 2)
        self.assertEqual(result["failed"], 0)


if __name__ == "__main__":
    unittest.main()
