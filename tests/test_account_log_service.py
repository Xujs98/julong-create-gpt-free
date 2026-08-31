# -*- coding: utf-8 -*-
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import account_log_service
from webui.app import create_app
from webui import config_editor


class AccountLogServiceTests(unittest.TestCase):
    def _client(self):
        # API tests should not observe the process-wide scheduler's immediate
        # startup cleanup call.
        with patch("webui.app.account_log_service.start_auto_cleanup_scheduler"):
            client = create_app(auth_code="account-log-test").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "account-log-test"
        return client

    def test_only_account_log_prefixes_are_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in (
                "live-check-user@example.com.log",
                "extract-link-user@example.com.log",
                "twofa-reset-user@example.com.log",
                "codex-retry-user@example.com.log",
                "registration-job-uuid.log",
            ):
                (root / name).write_text("log", encoding="utf-8")
            found = account_log_service.account_log_paths(root)
            self.assertEqual(len(found), 4)
            self.assertNotIn(root / "registration-job-uuid.log", found)

    def test_cleanup_by_age_and_all_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "live-check-old@example.com.log"
            fresh = root / "extract-link-fresh@example.com.log"
            untouched = root / "7b2a4a2c-uuid.log"
            old.write_text("old", encoding="utf-8")
            fresh.write_text("fresh", encoding="utf-8")
            untouched.write_text("job", encoding="utf-8")
            old_time = time.time() - 3 * 24 * 60 * 60
            os.utime(old, (old_time, old_time))

            result = account_log_service.cleanup_account_logs(older_than_days=2, log_dir=root)
            self.assertEqual(result["deleted"], 1)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(untouched.exists())

            result = account_log_service.cleanup_account_logs(all_logs=True, log_dir=root)
            self.assertEqual(result["deleted"], 1)
            self.assertFalse(fresh.exists())
            self.assertTrue(untouched.exists())

    def test_config_disabled_skips_automatic_cleanup(self):
        with patch("config.webui.ACCOUNT_LOG_AUTO_CLEANUP", False), patch(
            "core.account_log_service.cleanup_account_logs"
        ) as cleanup:
            result = account_log_service.cleanup_account_logs_from_config()
        self.assertFalse(result["enabled"])
        cleanup.assert_not_called()

    def test_cleanup_api_supports_all_account_logs_action(self):
        result = {"ok": True, "mode": "all", "scanned": 4, "deleted": 4, "failed_count": 0}
        with patch("webui.app.account_log_service.cleanup_account_logs", return_value=result) as cleanup:
            response = self._client().post("/api/account-logs/cleanup", json={"all": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["deleted"], 4)
        cleanup.assert_called_once_with(all_logs=True)

    def test_cleanup_api_parses_string_false_without_selecting_all_mode(self):
        result = {"ok": True, "mode": "older_than_days", "scanned": 1, "deleted": 0, "failed_count": 0}
        with patch("webui.app.account_log_service.cleanup_account_logs", return_value=result) as cleanup:
            response = self._client().post("/api/account-logs/cleanup", json={"all": "false", "days": 7})
        self.assertEqual(response.status_code, 200)
        cleanup.assert_called_once_with(older_than_days=7)

    def test_account_log_retention_config_has_backend_range_validation(self):
        field = next(item for item in config_editor.EDITABLE_FIELDS if item["key"] == "ACCOUNT_LOG_RETENTION_DAYS")
        self.assertEqual(config_editor._validate_config_value(field["key"], 30, field), 30)
        with self.assertRaises(ValueError):
            config_editor._validate_config_value(field["key"], 0, field)
        with self.assertRaises(ValueError):
            config_editor._validate_config_value(field["key"], 3651, field)


if __name__ == "__main__":
    unittest.main()
