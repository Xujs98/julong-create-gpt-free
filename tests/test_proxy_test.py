# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from core.cloakbrowser_driver import _normalize_proxy as normalize_cloak_proxy
from core.proxy_test import (
    ProxyTestError,
    _challenge_evidence,
    choose_healthy_proxy,
    test_proxy as run_proxy_test,
    test_proxy_pool as run_proxy_pool_test,
    warmup_proxy_pool,
)


class ProxyTestTests(unittest.TestCase):
    def test_challenge_page_markers_include_html_title(self):
        response = MagicMock()
        response.text = "<html><title>Just a Moment...</title></html>"
        response.headers = {}
        detected, markers = _challenge_evidence(response)
        self.assertTrue(detected)
        self.assertIn("just a moment", markers)

    @patch("core.proxy_test.test_proxy_health")
    def test_warmup_reports_all_healthy_and_target_clean(self, health):
        health.side_effect = [
            {"healthy": True, "proxy": "http://a.test:1"},
            {"healthy": True, "proxy": "http://b.test:2"},
            {"healthy": False, "proxy": "http://c.test:3", "challenge_detected": True},
        ]
        result = warmup_proxy_pool(
            ["http://a.test:1", "http://b.test:2", "http://c.test:3"],
            target_clean=1,
            max_workers=1,
        )
        self.assertEqual(result["available"], 2)
        self.assertEqual(result["clean"], 1)
        self.assertEqual(len(result["healthy_proxy_urls"]), 2)
        self.assertEqual(len(result["unhealthy_proxy_urls"]), 1)

    @patch("core.proxy_test.test_proxy_health")
    def test_choose_healthy_proxy_keeps_preferred_first(self, health):
        health.side_effect = [
            {"healthy": False, "proxy": "http://preferred.test:1"},
            {"healthy": True, "proxy": "http://other.test:2"},
        ]
        result = choose_healthy_proxy(
            ["http://preferred.test:1", "http://other.test:2"],
            preferred="http://preferred.test:1",
        )
        self.assertEqual(result["proxy_url"], "http://other.test:2")
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
    def test_pool_preflight_keeps_passed_proxy_and_reports_failed_proxy(self, test_one):
        """代理池中失败项被标记移除，可用项继续保留。"""
        test_one.side_effect = [
            {"ok": True, "proxy": "http://proxy-a.test:8080", "ip": "203.0.113.1"},
            ProxyTestError("连接超时"),
        ]

        result = run_proxy_pool_test(
            ["http://proxy-a.test:8080", "http://user:secret@proxy-b.test:8080"],
            timeout=2,
            max_workers=1,
        )

        self.assertEqual(test_one.call_count, 2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["available"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["valid_proxy_urls"], ["http://proxy-a.test:8080"])

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
        self.assertEqual(result["valid_proxy_urls"], [
            "http://proxy-a.test:8080",
            "http://proxy-b.test:8080",
        ])

    @patch("core.proxy_test.test_proxy")
    def test_pool_preflight_reports_no_available_proxy_without_leaking_urls(self, test_one):
        """全部失败时返回失败汇总，失败信息只包含脱敏代理地址。"""
        test_one.side_effect = [RuntimeError("连接超时"), RuntimeError("拒绝连接")]

        result = run_proxy_pool_test(
            ["http://user:secret@proxy-a.test:8080", "http://proxy-b.test:8080"],
            timeout=2,
            max_workers=1,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["available"], 0)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["valid_proxy_urls"], [])
        self.assertNotIn("secret", str(result["failures"]))


if __name__ == "__main__":
    unittest.main()
