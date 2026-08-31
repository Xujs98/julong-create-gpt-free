# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from config import register as register_config
from core import registration_service
from webui.app import create_app
from webui.config_editor import EDITABLE_FIELDS


class ICloudWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_config_exposes_icloud_common_fields(self):
        timeout = next(item for item in EDITABLE_FIELDS if item["key"] == "ICLOUD_REQUEST_TIMEOUT")
        verify = next(item for item in EDITABLE_FIELDS if item["key"] == "ICLOUD_VERIFY_TLS")
        self.assertEqual(timeout["group"], "邮箱 / OTP")
        self.assertEqual(timeout["type"], "int")
        self.assertEqual(verify["type"], "bool")

    def test_registration_page_exposes_icloud_source_selector(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="regEmailSourceV2"', html)
        self.assertIn('<option value="icloud">iCloud 邮箱</option>', html)

    def test_registration_driver_status_lists_all_five_modes(self):
        response = self.client.get("/api/registration-drivers/status")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(
            [item["driver"] for item in body["items"]],
            ["protocol", "roxy", "cloak", "browser_use", "skyvern"],
        )

    @patch("webui.app.db.import_icloud_emails", return_value=(1, 0))
    def test_import_route_accepts_icloud_email_and_url(self, import_icloud):
        response = self.client.post(
            "/api/outlook/import",
            json={
                "source": "icloud",
                "as_registered": False,
                "text": "sample@icloud.com----https://mail.example/s/token/sample@icloud.com",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["inserted"], 1)
        self.assertEqual(response.get_json()["as_registered"], False)
        import_icloud.assert_called_once_with([
            {
                "email": "sample@icloud.com",
                "code_url": "https://mail.example/s/token/sample@icloud.com",
                "access_token": "",
                "totp_secret": "",
            }
        ])

    @patch("webui.app.db.import_icloud_emails")
    def test_import_route_rejects_invalid_material_before_writing(self, import_icloud):
        """待修正素材会整批阻断，且不会调用数据库导入。"""
        response = self.client.post(
            "/api/outlook/import",
            json={
                "source": "icloud",
                "text": "bad-email----not-a-url\nvalid@icloud.com----https://mail.example/code",
            },
        )

        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertEqual(body["input_count"], 2)
        self.assertEqual(body["valid_count"], 1)
        self.assertEqual(body["invalid_count"], 1)
        self.assertIn("待修正", body["error"])
        import_icloud.assert_not_called()

    def test_import_dialog_defaults_to_pool_mode_and_shows_validation_counts(self):
        """导入弹窗默认按邮箱池模式导入，并展示三类格式检查统计。"""
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<input id="importAsRegisteredV2" type="checkbox">', html)
        self.assertIn('id="importCheckResultV2"', html)
        self.assertIn("有效邮箱 <strong>${check.validCount}</strong> 条", html)
        self.assertIn("待修正 <strong>${check.invalidCount}</strong> 条", html)
        self.assertIn("submitBtn.disabled = check.inputCount === 0 || check.invalidCount > 0", html)

    @patch("core.proxy_test.test_proxy")
    def test_proxy_route_returns_ip_and_location(self, test_proxy):
        test_proxy.return_value = {
            "ok": True,
            "proxy": "http://***:***@proxy.example:8080",
            "dns_mode": "default",
            "ip": "203.0.113.8",
            "country": "Japan",
            "region": "Tokyo",
            "city": "Tokyo",
        }
        response = self.client.post("/api/proxy/test", json={"proxy": "http://user:pass@proxy.example:8080"})

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["ip"], "203.0.113.8")
        self.assertEqual(body["city"], "Tokyo")

    @patch("webui.app.db.icloud_email_pool_summary", return_value={"total": 1, "available": 1, "used": 0, "failed": 0})
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_explicit_icloud_source_skips_global_mailnest(self, submit_registration, _icloud_summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "outlook,generic_api,mailnest"
        ), patch.object(email_config, "MAIL_NEST_API_KEY", "", create=True):
            response = self.client.post(
                "/api/jobs",
                json={"count": 1, "workers": 1, "email_source": "icloud"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["email_source"], "icloud")
        submit_registration.assert_called_once_with(count=1, workers=1, email_source="icloud")

    @patch("webui.app.db.domain_email_pool_summary", return_value={"total": 1, "available": 1, "used": 0, "failed": 0})
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_explicit_domain_source_uses_imported_url_pool(self, submit_registration, _domain_summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            response = self.client.post(
                "/api/jobs",
                json={"count": 2, "workers": 1, "email_source": "cloudflare_domain"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("邮箱 + 接码URL", response.get_json()["warning"])
        self.assertIn("不足的会失败", response.get_json()["warning"])
        submit_registration.assert_called_once_with(
            count=2, workers=1, email_source="cloudflare_domain"
        )

    @patch("webui.app.db.domain_email_pool_summary", return_value={"total": 0, "available": 0, "used": 0, "failed": 0})
    @patch("webui.app.db.icloud_email_pool_summary", return_value={"total": 1, "available": 1, "used": 0, "failed": 0})
    @patch("webui.app.db.generic_api_email_pool_summary", return_value={"total": 0, "available": 0, "used": 0, "failed": 0})
    @patch("webui.app.db.outlook_pool_summary", return_value={"total": 0, "available": 0, "used": 0, "failed": 0})
    @patch("webui.app.db.count_accounts", return_value=0)
    def test_summary_counts_icloud_outside_global_email_source(self, *_mocks):
        with patch.object(email_config, "EMAIL_SOURCE", "outlook,generic_api,mailnest"):
            response = self.client.get("/api/summary")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["outlook_total"], 1)
        self.assertEqual(body["outlook_available"], 1)
        self.assertEqual(body["pool_by_source"]["icloud"]["available"], 1)

    @patch("core.email_provider.acquire_email", return_value="sample@icloud.com")
    def test_registration_args_acquire_from_explicit_icloud_source(self, acquire_email):
        with patch.object(register_config, "REGISTER_EMAIL", ""), patch.object(
            register_config, "REGISTER_NAME", "Sample User"
        ), patch.object(email_config, "USE_EMAIL_SERVICE", True):
            email, _name, _birthday = registration_service._prepare_registration_args("icloud")

        self.assertEqual(email, "sample@icloud.com")
        acquire_email.assert_called_once_with("icloud")


if __name__ == "__main__":
    unittest.main()
