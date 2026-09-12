# -*- coding: utf-8 -*-
import unittest
import threading
from unittest.mock import MagicMock, patch

from core.cloakbrowser_driver import _normalize_proxy as normalize_cloak_proxy
from core.proxy_test import (
    ProxyTestError,
    _anonymity_assessment,
    _challenge_evidence,
    _reputation_assessment,
    choose_healthy_proxy,
    test_proxy as run_proxy_test,
    probe_proxy_login_flow,
    test_proxy_health as run_proxy_health_test,
    test_proxy_pool as run_proxy_pool_test,
    warmup_proxy_pool,
)


class ProxyTestTests(unittest.TestCase):
    @patch("core.proxy_test.test_proxy")
    @patch("core.chatgpt_auth.signin_openai", return_value="https://auth.openai.com/api/accounts/authorize?test=1")
    @patch("core.chatgpt_auth.get_csrf_token", return_value="csrf")
    @patch("core.chatgpt_auth.get_providers")
    @patch("core.session.BrowserSession")
    def test_login_flow_reuses_same_proxy_after_geo_check(
        self, session_class, get_providers, get_csrf, signin, test_proxy
    ):
        test_proxy.return_value = {"ok": True, "ip": "203.0.113.9"}
        session = session_class.return_value
        result = probe_proxy_login_flow("http://proxy.example:8080", timeout=3)
        self.assertTrue(result["login_flow_ok"])
        session_class.assert_called_once_with(proxy="http://proxy.example:8080", detect_exit_geo=False)
        get_providers.assert_called_once_with(session)
        get_csrf.assert_called_once_with(session)
        signin.assert_called_once_with(session, "csrf", "proxy-check@example.test", screen_hint="login")

    @patch("core.proxy_test.test_proxy", return_value={"ok": True, "ip": "203.0.113.9"})
    @patch("core.chatgpt_auth.get_providers", side_effect=RuntimeError("HTTP Error 403"))
    @patch("core.session.BrowserSession")
    def test_login_flow_rejects_geo_reachable_edge_block(self, session_class, _get_providers, _test_proxy):
        with self.assertRaisesRegex(ProxyTestError, "完整协议登录预检不可用"):
            probe_proxy_login_flow("http://proxy.example:8080", timeout=3)
        session_class.return_value.session.close.assert_called_once()

    def test_challenge_page_markers_include_html_title(self):
        response = MagicMock()
        response.text = "<html><title>Just a Moment...</title></html>"
        response.headers = {}
        detected, markers = _challenge_evidence(response)
        self.assertTrue(detected)
        self.assertIn("just a moment", markers)

    def test_reputation_assessment_rejects_proxy_vpn_hosting_and_abuse_signals(self):
        result = _reputation_assessment({
            "is_proxy": True,
            "is_vpn": True,
            "is_datacenter": True,
            "is_abuser": True,
        })

        self.assertFalse(result["clean"])
        self.assertIn("is_proxy", result["network_risk"])
        self.assertIn("is_vpn", result["network_risk"])
        self.assertIn("is_datacenter", result["network_risk"])
        self.assertIn("is_abuser", result["high_risk"])
        self.assertGreaterEqual(result["penalty"], 59)

    def test_anonymity_assessment_detects_forwarded_header_and_exit_mismatch(self):
        result = _anonymity_assessment({
            "origin": "198.51.100.8",
            "headers": {"X-Forwarded-For": "192.0.2.10", "Via": "proxy"},
        }, "203.0.113.9")

        self.assertFalse(result["anonymous"])
        self.assertTrue(result["origin_verified"])
        self.assertFalse(result["exit_consistent"])
        self.assertEqual(result["leak_headers"], ["via", "x-forwarded-for"])

    def test_anonymity_assessment_requires_echoed_origin_ip(self):
        result = _anonymity_assessment({"headers": {}}, "203.0.113.9")

        self.assertFalse(result["origin_verified"])
        self.assertFalse(result["exit_consistent"])
        self.assertFalse(result["anonymous"])

    @patch("core.proxy_test.Session")
    @patch("core.proxy_test._request_json")
    @patch("core.proxy_test.test_proxy")
    def test_multidimensional_health_rejects_dirty_reputation_without_challenge(self, geo, request_json, session_class):
        geo.return_value = {"ip": "203.0.113.9", "country": "US", "country_code": "US"}
        request_json.side_effect = [
            ({"is_proxy": True, "is_vpn": False, "is_datacenter": True, "is_tor": False, "is_abuser": False}, 0.1),
            ({"origin": "203.0.113.9", "headers": {}}, 0.1),
        ]
        response = MagicMock(status_code=200, text="<html><title>Login</title></html>", headers={}, url="https://service.test/login")
        session_class.return_value.get.return_value = response

        result = run_proxy_health_test(
            "http://proxy.test:8080",
            health_url="https://service.test/login",
            reputation_url="https://reputation.test/{ip}",
            anonymity_url="https://echo.test/get",
        )

        self.assertFalse(result["healthy"])
        self.assertFalse(result["challenge_detected"])
        self.assertTrue(result["removable"])
        self.assertIn("ip_reputation_risk", result["reason"])
        self.assertLess(result["clean_score"], result["clean_threshold"])

    @patch("core.proxy_test.Session")
    @patch("core.proxy_test._request_json")
    @patch("core.proxy_test.test_proxy")
    def test_multidimensional_health_accepts_clean_reputation_and_anonymity(self, geo, request_json, session_class):
        geo.return_value = {"ip": "203.0.113.9", "country": "US", "country_code": "US"}
        request_json.side_effect = [
            ({"is_proxy": False, "is_vpn": False, "is_datacenter": False, "is_tor": False, "is_abuser": False}, 0.1),
            ({"origin": "203.0.113.9", "headers": {}}, 0.1),
        ]
        response = MagicMock(status_code=200, text="<html><title>Login</title></html>", headers={}, url="https://service.test/login")
        session_class.return_value.get.return_value = response

        result = run_proxy_health_test(
            "http://proxy.test:8080",
            health_url="https://service.test/login",
            reputation_url="https://reputation.test/{ip}",
            anonymity_url="https://echo.test/get",
        )

        self.assertTrue(result["healthy"])
        self.assertTrue(result["verification_complete"])
        self.assertFalse(result["removable"])
        self.assertEqual(result["clean_score"], 100)
        self.assertEqual(result["exit_samples"], ["203.0.113.9"] * 3)
        self.assertTrue(result["stable_exit"])

    @patch("core.proxy_test.Session")
    @patch("core.proxy_test._request_json")
    @patch("core.proxy_test.test_proxy")
    def test_multidimensional_health_rejects_connection_rotating_exit(self, geo, request_json, session_class):
        geo.side_effect = [
            {"ip": "203.0.113.9", "country": "US", "country_code": "US"},
            {"ip": "203.0.113.10", "country": "US", "country_code": "US"},
            {"ip": "203.0.113.9", "country": "US", "country_code": "US"},
        ]
        request_json.side_effect = [
            ({"is_proxy": False, "is_vpn": False, "is_datacenter": False, "is_tor": False, "is_abuser": False}, 0.1),
            ({"origin": "203.0.113.9", "headers": {}}, 0.1),
        ]
        response = MagicMock(status_code=200, text="<html><title>Login</title></html>", headers={}, url="https://service.test/login")
        session_class.return_value.get.return_value = response

        result = run_proxy_health_test(
            "http://proxy.test:8080",
            health_url="https://service.test/login",
            reputation_url="https://reputation.test/{ip}",
            anonymity_url="https://echo.test/get",
            exit_samples=3,
        )

        self.assertFalse(result["healthy"])
        self.assertFalse(result["stable_exit"])
        self.assertTrue(result["removable"])
        self.assertIn("rotating_exit", result["reason"])
        self.assertEqual(result["exit_samples"], ["203.0.113.9", "203.0.113.10", "203.0.113.9"])

    @patch("core.proxy_test._request_json")
    @patch("core.proxy_test._sample_proxy_exits")
    @patch("core.proxy_test.Session")
    def test_health_stops_after_business_challenge_without_extra_proxy_rounds(
        self, session_class, sample_exits, request_json
    ):
        response = MagicMock(
            status_code=403,
            text="<html><title>Just a Moment...</title></html>",
            headers={},
            url="https://service.test/login",
        )
        session_class.return_value.get.return_value = response

        result = run_proxy_health_test(
            "http://proxy.test:8080",
            health_url="https://service.test/login",
            reputation_url="https://reputation.test/{ip}",
            anonymity_url="https://echo.test/get",
        )

        self.assertFalse(result["healthy"])
        self.assertEqual(result["status"], 403)
        self.assertIn("cloudflare_challenge", result["reason"])
        sample_exits.assert_not_called()
        request_json.assert_not_called()

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
        self.assertEqual(health.call_count, 3)
        self.assertTrue(result["checked_all"])
        self.assertEqual(result["checked_total"], 3)
        self.assertEqual(result["available"], 2)
        self.assertEqual(result["healthy_total"], 2)
        self.assertEqual(result["clean"], 1)
        self.assertEqual(result["selected_clean_count"], 1)
        self.assertEqual(len(result["healthy_proxy_urls"]), 2)
        self.assertEqual(len(result["unhealthy_proxy_urls"]), 1)

    @patch("core.proxy_test.test_proxy_health")
    def test_warmup_passes_exit_stability_sample_count(self, health):
        health.return_value = {"healthy": True, "proxy": "http://a.test:1"}

        warmup_proxy_pool(["http://a.test:1"], exit_samples=5, max_workers=1)

        self.assertEqual(health.call_args.kwargs["exit_samples"], 5)

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

    @patch("core.proxy_test.test_proxy_health")
    def test_warmup_can_be_cancelled_before_all_results_finish(self, health):
        cancel = threading.Event()
        cancel.set()
        health.return_value = {"healthy": True, "proxy": "http://a.test:1"}
        with self.assertRaises(ProxyTestError):
            warmup_proxy_pool(["http://a.test:1", "http://b.test:2"], cancel_event=cancel, max_workers=1)

    @patch("core.proxy_test.test_proxy_health")
    def test_warmup_only_marks_definitive_dirty_results_for_deletion(self, health):
        health.side_effect = [
            {"healthy": True, "removable": False, "proxy": "http://clean.test:1"},
            {"healthy": False, "removable": True, "proxy": "http://dirty.test:2"},
            {"healthy": False, "removable": False, "inconclusive": True, "proxy": "http://retry.test:3"},
        ]

        result = warmup_proxy_pool(
            ["http://clean.test:1", "http://dirty.test:2", "http://retry.test:3"],
            max_workers=1,
        )

        self.assertEqual(result["dirty"], 1)
        self.assertEqual(result["inconclusive"], 1)
        self.assertEqual(result["unhealthy_proxy_urls"], ["http://dirty.test:2"])
        self.assertEqual(result["inconclusive_proxy_urls"], ["http://retry.test:3"])
    def test_cloak_keeps_playwright_supported_socks5_scheme(self):
        self.assertEqual(
            normalize_cloak_proxy("socks5://user:pass@proxy.example:3000"),
            "socks5://user:pass@proxy.example:3000",
        )

    def test_cloak_converts_socks5h_to_playwright_supported_scheme(self):
        self.assertEqual(
            normalize_cloak_proxy("socks5h://user:pass@proxy.example:3000"),
            "socks5://user:pass@proxy.example:3000",
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

    @patch("core.proxy_test.Session")
    def test_geo_test_retries_transient_transport_failure_once(self, session_class):
        session = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {"ip": "203.0.113.10", "country": "US"}
        session.get.side_effect = [RuntimeError("curl: (28) Operation timed out"), response]
        session_class.return_value = session

        with patch("core.proxy_test._browser_cfg.IP_GEO_ENDPOINTS", ["https://geo.example/json"]), patch(
            "core.proxy_test._browser_cfg.IP_GEO_RETRIES", 1
        ), patch("core.proxy_test.time.sleep") as sleep:
            result = run_proxy_test("http://proxy.example:8080", timeout=2)

        self.assertTrue(result["ok"])
        self.assertEqual(result["ip"], "203.0.113.10")
        self.assertEqual(session.get.call_count, 2)
        sleep.assert_called_once_with(0.2)

    @patch("core.proxy_utils._endpoint_supports_socks5", return_value=False)
    @patch("core.proxy_test.Session")
    def test_legacy_pool_entry_falls_back_to_socks5h_after_auto_detection_failure(
        self, session_class, _supports_socks5
    ):
        first = MagicMock()
        first.get.side_effect = RuntimeError("curl: (28) Operation timed out")
        second = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {"ip": "203.0.113.12", "country": "JP"}
        second.get.return_value = response
        session_class.side_effect = [first, second]

        with patch("core.proxy_test._browser_cfg.IP_GEO_ENDPOINTS", ["https://geo.example/json"]), patch(
            "core.proxy_test._browser_cfg.IP_GEO_RETRIES", 0
        ):
            result = run_proxy_test("proxy.example:8080:user:pass", timeout=2)

        self.assertTrue(result["ok"])
        self.assertEqual(result["proxy"], "socks5h://***:***@proxy.example:8080")
        self.assertEqual(first.proxies["https"], "http://user:pass@proxy.example:8080")
        self.assertEqual(second.proxies["https"], "socks5h://user:pass@proxy.example:8080")

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


    @patch("core.proxy_test.test_proxy_health")
    def test_warmup_rechecks_first_pass_healthy_exits_before_selection(self, health):
        health.side_effect = [
            {"healthy": True, "proxy": "http://a.test:1"},
            {"healthy": True, "proxy": "http://b.test:2"},
            {"healthy": False, "removable": True, "proxy": "http://c.test:3"},
            {"healthy": True, "proxy": "http://a.test:1"},
            {"healthy": False, "removable": True, "proxy": "http://b.test:2"},
        ]

        result = warmup_proxy_pool(
            ["http://a.test:1", "http://b.test:2", "http://c.test:3"],
            target_clean=2,
            max_workers=1,
            recheck_clean=True,
        )

        self.assertEqual(health.call_count, 5)
        self.assertTrue(result["recheck_enabled"])
        self.assertEqual(result["recheck_candidate_count"], 2)
        self.assertEqual(result["recheck_checked_total"], 2)
        self.assertEqual(result["checked_total"], 5)
        self.assertEqual(result["healthy_proxy_urls"], ["http://a.test:1"])
        self.assertEqual(result["clean_proxy_urls"], ["http://a.test:1"])
        self.assertIn("http://b.test:2", result["unhealthy_proxy_urls"])


if __name__ == "__main__":
    unittest.main()
