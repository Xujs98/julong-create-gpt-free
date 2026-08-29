# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class _Http:
    def close(self):
        pass


class _Client:
    def __init__(self, *args, **kwargs):
        pass

    def batch_redeem(self, cdks):
        return {"items": [
            {"index": 0, "status": "success", "type": "bindable", "phone": "+1"},
            {"index": 1, "status": "success", "type": "onetime", "phone": "+2"},
            {"index": 2, "status": "error", "error": "invalid cdk"},
        ]}

    def phone_availability(self):
        return {"shortTerm": "high", "longTerm": "ample"}


class CodexSmsWebUiTests(unittest.TestCase):
    def setUp(self):
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


if __name__ == "__main__":
    unittest.main()
