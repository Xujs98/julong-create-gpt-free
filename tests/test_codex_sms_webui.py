# -*- coding: utf-8 -*-
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from webui import app as webui_app
from webui.app import create_app


class _Http:
    def close(self):
        pass


class _Client:
    def __init__(self, *args, **kwargs):
        pass

    def batch_redeem(self, cdks):
        items = [
            {"index": 0, "status": "success", "type": "bindable", "phone": "+1"},
            {"index": 1, "status": "success", "type": "onetime", "phone": "+2"},
            {"index": 2, "status": "error", "error": "invalid cdk"},
        ]
        return {"items": items[:len(cdks)]}

    def phone_availability(self):
        return {"shortTerm": "high", "longTerm": "ample"}


class CodexSmsWebUiTests(unittest.TestCase):
    def setUp(self):
        webui_app._CODEX_SMS_CHECK_CACHE.clear()
        self.client = create_app(auth_code="codex-sms-test").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "codex-sms-test"

    @patch("core.codex_sms_client.CodexSmsClient", _Client)
    @patch("core.sms_provider._http", return_value=_Http())
    def test_batch_check_returns_redacted_counts(self, _http):
        response = self.client.post("/api/codex-sms/check", json={"cdks": ["LONG-ABC", "SHORT-XYZ", "BAD"]})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["counts"], {"total": 3, "available": 2, "long": 1, "short": 1, "failed": 1})
        self.assertEqual(payload["items"][0]["cdkHint"], "LON***ABC")
        self.assertNotIn("cdk", payload["items"][0])

    @patch("core.codex_sms_client.CodexSmsClient", _Client)
    @patch("core.sms_provider._http", return_value=_Http())
    def test_availability_route(self, _http):
        response = self.client.get("/api/codex-sms/availability")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["shortTerm"], "high")
        self.assertEqual(response.get_json()["longTerm"], "ample")

    @patch("core.codex_sms_client.CodexSmsClient", _Client)
    @patch("core.sms_provider._http", return_value=_Http())
    def test_bulk_retry_rejects_when_available_cdks_are_below_selection(self, _http):
        accounts = {
            value: {"id": value, "email": f"user-{value}@example.com", "codex_status": "failed"}
            for value in (1, 2, 3)
        }
        with patch("config.codex.SMS_PROVIDER", "codex"), \
                patch("config.codex.CODEX_SMS_CDKS", ["CDK-A", "CDK-B", "CDK-C"]), \
                patch("webui.app.db.get_account", side_effect=accounts.get), \
                patch("webui.app.codex_retry_service.reserve") as reserve:
            response = self.client.post(
                "/api/codex/retry-bulk",
                json={"account_ids": [1, 2, 3], "workers": 3},
            )
        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertEqual(payload["code"], "codex_sms_cdk_shortage")
        self.assertEqual(payload["error"], "可用数量小于选中数量")
        self.assertEqual(payload["available_count"], 2)
        self.assertEqual(payload["selected_count"], 3)
        reserve.assert_not_called()

    def test_bulk_retry_ui_checks_cdks_and_shows_shortage_dialog(self):
        source = Path(webui_app.__file__).with_name("templates").joinpath("index.html").read_text(encoding="utf-8")
        self.assertIn("api('/api/codex-sms/check'", source)
        self.assertIn("available < ids.length", source)
        self.assertIn("title: 'CDK 数量不足'", source)
        self.assertIn("可用数量小于选中数量", source)

    @patch("core.codex_sms_client.CodexSmsClient", _Client)
    @patch("core.sms_provider._http", return_value=_Http())
    def test_bulk_retry_allows_available_cdks_equal_to_selection(self, _http):
        accounts = {
            value: {"id": value, "email": f"user-{value}@example.com", "codex_status": "failed"}
            for value in (1, 2)
        }
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch("config.codex.SMS_PROVIDER", "codex"), \
                patch("config.codex.CODEX_SMS_CDKS", ["CDK-A", "CDK-B"]), \
                patch("webui.app.db.get_account", side_effect=accounts.get), \
                patch("webui.app.db.update_account_codex_status"), \
                patch("webui.app.codex_retry_service.reserve", return_value=True), \
                patch("webui.app.codex_retry_service.log_path", side_effect=lambda email: Path(temp_dir) / f"{email}.log"), \
                patch("webui.app.threading.Thread") as thread:
            response = self.client.post(
                "/api/codex/retry-bulk",
                json={"account_ids": [1, 2], "workers": 2},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["started_count"], 2)
        thread.return_value.start.assert_called_once()
        self.assertEqual(webui_app._CODEX_SMS_CHECK_CACHE, {})


if __name__ == "__main__":
    unittest.main()
