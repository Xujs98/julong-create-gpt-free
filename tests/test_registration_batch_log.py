# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


class RegistrationBatchStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.env_patch = patch.dict(os.environ, {"TURB_STORAGE_BACKEND": "json"})
        self.path_patch = patch.multiple(
            db,
            _JOBS_JSON=root / "jobs.json",
            _DEFAULT_JOBS_JSON=root / "jobs.json",
            _LEGACY_JOBS_JSON=root / "legacy-jobs.json",
            _REGISTRATION_BATCHES_JSON=root / "registration-batches.json",
            _DEFAULT_REGISTRATION_BATCHES_JSON=root / "registration-batches.json",
            _LOG_DIR=root / "logs",
        )
        self.env_patch.start()
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def test_batch_records_duration_and_success_failure_counts(self):
        batch = db.create_registration_batch(requested_count=2, workers=2, email_source="icloud")
        rows = db._load_registration_batches()
        rows[0]["started_at"] = "2026-08-10T00:00:00"
        db._save_registration_batches(rows)

        first = db.create_job("icloud", batch_id=batch["id"])
        second = db.create_job("icloud", batch_id=batch["id"])
        db.seal_registration_batch(batch["id"], [first["id"], second["id"]])
        db.update_job(
            first["id"], status="success", started_at="2026-08-10T00:00:10",
            completed_at="2026-08-10T01:02:03",
        )
        db.update_job(
            second["id"], status="failed", started_at="2026-08-10T00:00:11",
            completed_at="2026-08-10T01:01:00", error="示例失败",
        )

        summary = db.get_registration_batch(batch["id"])
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["success_count"], 1)
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(summary["elapsed_seconds"], 3723)
        self.assertEqual(summary["workers"], 2)
        self.assertEqual(summary["requested_count"], 2)

        # 删除单任务记录后，已完成批次的历史统计仍保持原值。
        self.assertTrue(db.delete_job(first["id"], delete_log=False))
        preserved = db.get_registration_batch(batch["id"])
        self.assertEqual(preserved["success_count"], 1)
        self.assertEqual(preserved["failed_count"], 1)

        cleared = db.clear_registration_batches()
        self.assertEqual(cleared, {"cleared": 1, "kept_active": 0})
        self.assertEqual(db.list_registration_batches(), [])

    def test_clear_keeps_running_batch(self):
        batch = db.create_registration_batch(requested_count=1, workers=1, email_source="icloud")
        job = db.create_job("icloud", batch_id=batch["id"])
        db.seal_registration_batch(batch["id"], [job["id"]])

        cleared = db.clear_registration_batches()

        self.assertEqual(cleared, {"cleared": 0, "kept_active": 1})
        self.assertEqual(db.list_registration_batches()[0]["status"], "running")


class RegistrationBatchWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_registration_page_contains_duration_and_task_log_controls(self):
        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('<th class="col-duration">耗时</th>', html)
        self.assertIn('id="btnRegistrationTaskLogV2"', html)
        self.assertIn('id="btnClearRegistrationTaskLog"', html)
        self.assertIn('<th>总耗时</th>', html)
        self.assertIn('function formatDurationSeconds(value)', html)

    @patch("webui.app.db.list_registration_batches")
    def test_batch_log_api_returns_persistent_history(self, list_batches):
        list_batches.return_value = [{"id": 3, "requested_count": 5, "workers": 2, "elapsed_seconds": 65}]

        response = self.client.get("/api/registration-batches")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"][0]["id"], 3)
        list_batches.assert_called_once_with(limit=200)

    @patch("webui.app.db.clear_registration_batches", return_value={"cleared": 4, "kept_active": 1})
    def test_batch_log_clear_keeps_active_history(self, clear_batches):
        response = self.client.post("/api/registration-batches/clear", json={})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["cleared"], 4)
        self.assertEqual(response.get_json()["kept_active"], 1)
        clear_batches.assert_called_once_with(keep_active=True)


if __name__ == "__main__":
    unittest.main()
