# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from config import proxy as proxy_config
from config import register as register_config
from core.proxy_test import ProxyTestError
from webui.app import create_app


class RegistrationProxyPreflightTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.submit_registration")
    @patch("core.proxy_test.test_proxy_pool")
    def test_failed_proxy_preflight_ends_request_without_creating_jobs(self, test_pool, submit_registration):
        """代理池检查失败时返回弹窗代码，并且不提交任何注册任务。"""
        test_pool.side_effect = ProxyTestError("代理池连通性检查未通过：1/1 个代理不可用")
        with patch.object(proxy_config, "PROXY_CHECK_BEFORE_REGISTRATION", True), patch.object(
            proxy_config, "PROXY_POOL", ["http://proxy.test:8080"]
        ):
            response = self.client.post("/api/jobs", json={"count": 2, "workers": 1})

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["code"], "proxy_pool_preflight_failed")
        self.assertTrue(payload["task_ended"])
        self.assertEqual(payload["jobs_created"], 0)
        submit_registration.assert_not_called()

    @patch("webui.app.svc.submit_registration", return_value=[])
    @patch("core.proxy_test.test_proxy_pool")
    def test_successful_proxy_preflight_allows_registration_submission(self, test_pool, submit_registration):
        """代理池全部连通后才进入注册任务提交。"""
        test_pool.return_value = {"ok": True, "total": 1, "available": 1, "failed": 0, "results": []}
        with patch.object(proxy_config, "PROXY_CHECK_BEFORE_REGISTRATION", True), patch.object(
            proxy_config, "PROXY_POOL", ["http://proxy.test:8080"]
        ), patch.object(email_config, "USE_EMAIL_SERVICE", False), patch.object(
            register_config, "REGISTER_EMAIL", "user@example.test"
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["proxy_check"]["enabled"])
        test_pool.assert_called_once_with(["http://proxy.test:8080"])
        submit_registration.assert_called_once_with(count=1, workers=1)

    @patch("webui.app.svc.submit_registration", return_value=[])
    @patch("core.proxy_test.test_proxy_pool")
    def test_disabled_proxy_preflight_skips_connectivity_check(self, test_pool, submit_registration):
        """开关关闭时保持原任务启动行为。"""
        with patch.object(proxy_config, "PROXY_CHECK_BEFORE_REGISTRATION", False), patch.object(
            email_config, "USE_EMAIL_SERVICE", False
        ), patch.object(register_config, "REGISTER_EMAIL", "user@example.test"):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["proxy_check"])
        test_pool.assert_not_called()
        submit_registration.assert_called_once_with(count=1, workers=1)


if __name__ == "__main__":
    unittest.main()
