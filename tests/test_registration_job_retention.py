# -*- coding: utf-8 -*-
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import db, registration_service


class RegistrationJobRetentionTests(unittest.TestCase):
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

    def _terminal_job(self, created_at: str, status: str = "success", *, batch_id: int | None = None):
        row = db.create_job("icloud", batch_id=batch_id)
        row["created_at"] = created_at
        row["completed_at"] = created_at
        row["status"] = status
        log_path = Path(row["log_file"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"job {row['id']}\n", encoding="utf-8")
        rows = db._load_jobs()
        stored = next(item for item in rows if item["id"] == row["id"])
        stored.update(row)
        db._save_jobs(rows)
        return row

    def test_prune_keeps_latest_terminal_rows_and_active_rows(self):
        oldest = self._terminal_job("2026-08-01T00:00:00")
        middle = self._terminal_job("2026-08-02T00:00:00", status="failed")
        latest = self._terminal_job("2026-08-03T00:00:00")
        newest = self._terminal_job("2026-08-04T00:00:00", status="cancelled")
        active = db.create_job("icloud")

        result = db.prune_registration_jobs(2)

        self.assertEqual(result["removed"], 2)
        self.assertEqual(result["removed_job_ids"], [oldest["id"], middle["id"]])
        self.assertEqual(result["retention_count"], 2)
        remaining_ids = {row["id"] for row in db._load_jobs()}
        self.assertEqual(remaining_ids, {latest["id"], newest["id"], active["id"]})
        self.assertFalse(Path(oldest["log_file"]).exists())
        self.assertFalse(Path(middle["log_file"]).exists())
        self.assertTrue(Path(latest["log_file"]).exists())

    def test_prune_uses_the_same_id_order_as_the_registration_page(self):
        first = self._terminal_job("2026-08-10T00:00:00")
        second = self._terminal_job("2026-08-01T00:00:00")

        result = db.prune_registration_jobs(1)

        self.assertEqual(result["removed_job_ids"], [first["id"]])
        self.assertEqual([row["id"] for row in db._load_jobs()], [second["id"]])

    def test_prune_protects_terminal_rows_in_active_batch(self):
        batch = db.create_registration_batch(requested_count=2, workers=1, email_source="icloud")
        first = db.create_job("icloud", batch_id=batch["id"])
        second = db.create_job("icloud", batch_id=batch["id"])

        protected = db.prune_registration_jobs(0)

        self.assertEqual(protected["removed"], 0)
        self.assertEqual({row["id"] for row in db._load_jobs()}, {first["id"], second["id"]})

        db.seal_registration_batch(batch["id"], [first["id"], second["id"]])
        rows = db._load_jobs()
        for index, row in enumerate(rows, start=1):
            completed_at = f"2026-08-0{index}T00:00:00"
            row.update({"status": "success", "created_at": completed_at, "completed_at": completed_at})
            log_path = Path(row["log_file"])
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(f"job {row['id']}\n", encoding="utf-8")
        db._save_jobs(rows)

        # update_job 不负责固化批次快照，原始批次行仍是 running；清理逻辑需实时推导已完成。
        self.assertEqual(db._load_registration_batches()[0]["status"], "running")

        pruned = db.prune_registration_jobs(1)

        self.assertEqual(pruned["removed"], 1)
        self.assertEqual(pruned["removed_job_ids"], [first["id"]])
        self.assertFalse(Path(first["log_file"]).exists())
        self.assertTrue(Path(second["log_file"]).exists())
        preserved_batch = db.get_registration_batch(batch["id"])
        self.assertEqual(preserved_batch["status"], "completed")
        self.assertEqual(preserved_batch["success_count"], 2)
        self.assertEqual(preserved_batch["failed_count"], 0)

    def test_default_retention_reads_webui_config(self):
        for index in range(3):
            self._terminal_job(f"2026-08-0{index + 1}T00:00:00")

        with patch("config.webui.WEBUI_REGISTRATION_JOB_RETENTION_COUNT", 1):
            result = db.prune_registration_jobs()

        self.assertEqual(result["retention_count"], 1)
        self.assertEqual(result["removed"], 2)

    def test_prune_protects_an_active_retry_chain(self):
        root = self._terminal_job("2026-08-01T00:00:00", status="failed")
        unrelated = self._terminal_job("2026-08-02T00:00:00")
        retry, created = db.create_retry_job(
            root["id"],
            job_type="registration_retry",
            email_source="icloud",
        )

        result = db.prune_registration_jobs(0)

        self.assertTrue(created)
        self.assertEqual(result["removed_job_ids"], [unrelated["id"]])
        self.assertEqual({row["id"] for row in db._load_jobs()}, {root["id"], retry["id"]})
        self.assertTrue(Path(root["log_file"]).exists())

    def test_prune_rejects_a_log_path_outside_the_task_log_directory(self):
        job = self._terminal_job("2026-08-01T00:00:00")
        outside = Path(self.temp_dir.name) / "outside" / f"{job['job_uuid']}.log"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("keep\n", encoding="utf-8")
        rows = db._load_jobs()
        rows[0]["log_file"] = str(outside)
        db._save_jobs(rows)

        result = db.prune_registration_jobs(0)

        self.assertTrue(outside.exists())
        self.assertEqual(result["deleted_log_files"], 0)
        self.assertEqual(result["failed_log_files"], 1)

    def test_prune_accepts_a_historical_absolute_task_log_path(self):
        job = self._terminal_job("2026-08-01T00:00:00")
        old_log_dir = Path(self.temp_dir.name) / db._PROJECT_ROOT.name / db._LOG_DIR.name
        old_log_dir.mkdir(parents=True, exist_ok=True)
        historical = old_log_dir / f"{job['job_uuid']}.log"
        historical.write_text("old log\n", encoding="utf-8")
        rows = db._load_jobs()
        rows[0]["log_file"] = str(historical)
        db._save_jobs(rows)

        result = db.prune_registration_jobs(0)

        self.assertFalse(historical.exists())
        self.assertEqual(result["deleted_log_files"], 1)
        self.assertEqual(result["failed_log_files"], 0)

def test_prune_registration_jobs_keeps_sqlite_and_json_mirror_in_sync(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "data" / "registration.sqlite3"
    paths = {
        "_DATA_DIR": tmp_path,
        "_LOG_DIR": tmp_path / "logs",
        "_SQLITE_PATH": sqlite_path,
        "_DEFAULT_SQLITE_PATH": sqlite_path,
        "_OUTLOOK_JSON": tmp_path / "outlook.json",
        "_DEFAULT_OUTLOOK_JSON": tmp_path / "outlook.json",
        "_LEGACY_OUTLOOK_JSON": tmp_path / "legacy-outlook.json",
        "_GENERIC_API_EMAIL_JSON": tmp_path / "generic-api.json",
        "_DEFAULT_GENERIC_API_EMAIL_JSON": tmp_path / "generic-api.json",
        "_ICLOUD_EMAIL_JSON": tmp_path / "icloud.json",
        "_DEFAULT_ICLOUD_EMAIL_JSON": tmp_path / "icloud.json",
        "_ACCOUNTS_JSON": tmp_path / "accounts.json",
        "_DEFAULT_ACCOUNTS_JSON": tmp_path / "accounts.json",
        "_LEGACY_ACCOUNTS_JSON": tmp_path / "legacy-accounts.json",
        "_JOBS_JSON": tmp_path / "jobs.json",
        "_DEFAULT_JOBS_JSON": tmp_path / "jobs.json",
        "_LEGACY_JOBS_JSON": tmp_path / "legacy-jobs.json",
        "_REGISTRATION_BATCHES_JSON": tmp_path / "registration-batches.json",
        "_DEFAULT_REGISTRATION_BATCHES_JSON": tmp_path / "registration-batches.json",
        "_DOMAIN_EMAIL_JSON": tmp_path / "domain.json",
        "_DEFAULT_DOMAIN_EMAIL_JSON": tmp_path / "domain.json",
        "_GROUPS_JSON": tmp_path / "groups.json",
        "_DEFAULT_GROUPS_JSON": tmp_path / "groups.json",
        "_CODEX_EXPORT_STATE": tmp_path / "codex-export-state.json",
        "_DEFAULT_CODEX_EXPORT_STATE": tmp_path / "codex-export-state.json",
        "_SQLITE_READY_PATH": None,
        "_SQLITE_STORE_INSTANCE": None,
    }
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "sqlite")

    with patch.multiple(db, **paths):
        jobs = [db.create_job("icloud") for _ in range(3)]
        rows = db._load_jobs()
        for index, row in enumerate(rows, start=1):
            value = f"2026-08-0{index}T00:00:00"
            row.update({"status": "success", "created_at": value, "completed_at": value})
        db._save_jobs(rows)

        result = db.prune_registration_jobs(1, delete_logs=False)

        assert result["removed_job_ids"] == [jobs[0]["id"], jobs[1]["id"]]
        assert [row["id"] for row in db._load_jobs()] == [jobs[2]["id"]]
        assert db._sqlite_store().integrity_check() == "ok"
        assert '"id": 3' in (tmp_path / "jobs.json").read_text(encoding="utf-8")


def test_retention_scheduler_replays_a_trigger_received_while_pruning():
    first_started = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def prune():
        nonlocal calls
        with calls_lock:
            calls += 1
            current = calls
        if current == 1:
            first_started.set()
            release_first.wait(timeout=3)
        elif current == 2:
            second_finished.set()
        return {"failed_log_files": 0}

    with registration_service._RETENTION_TRIGGER_LOCK:
        registration_service._RETENTION_TRIGGER_PENDING = False
        registration_service._RETENTION_TRIGGER_RERUN = False
    try:
        with patch.object(registration_service.db, "prune_registration_jobs", side_effect=prune):
            assert registration_service.schedule_registration_job_retention() is True
            assert first_started.wait(timeout=3)
            assert registration_service.schedule_registration_job_retention() is False
            release_first.set()
            assert second_finished.wait(timeout=3)

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with registration_service._RETENTION_TRIGGER_LOCK:
                    if not registration_service._RETENTION_TRIGGER_PENDING:
                        break
                time.sleep(0.01)
    finally:
        release_first.set()
        with registration_service._RETENTION_TRIGGER_LOCK:
            registration_service._RETENTION_TRIGGER_PENDING = False
            registration_service._RETENTION_TRIGGER_RERUN = False

    assert calls == 2


def test_bulk_cancel_and_pending_stop_trigger_retention():
    jobs = [{"id": 1, "status": "pending"}, {"id": 2, "status": "success"}]
    with patch.object(registration_service.db, "list_jobs", return_value=jobs), patch.object(
        registration_service.db, "update_job"
    ) as update_job, patch.object(
        registration_service, "schedule_registration_job_retention"
    ) as schedule:
        assert registration_service.cancel_pending_jobs() == 1
        schedule.assert_called_once_with()
        update_job.assert_called_once()

    pending = {"id": 3, "status": "pending"}
    with patch.object(registration_service.db, "get_job", return_value=pending), patch.object(
        registration_service.db, "update_job"
    ), patch.object(registration_service, "_append_job_log"), patch.object(
        registration_service, "schedule_registration_job_retention"
    ) as schedule:
        result = registration_service.request_stop_job(3)

    assert result["state"] == "cancelled"
    schedule.assert_called_once_with()


def test_cancelled_workers_trigger_retention_before_early_return():
    cancelled = {"id": 7, "status": "cancelled"}
    with patch.object(registration_service.db, "get_job", return_value=cancelled), patch.object(
        registration_service, "_activate_job"
    ), patch.object(registration_service, "_deactivate_job"), patch.object(
        registration_service, "schedule_registration_job_retention"
    ) as schedule:
        registration_service._run_one_job(7, "/tmp/unused.log")
    schedule.assert_called_once_with()

    with patch.object(registration_service.db, "get_job", return_value=cancelled), patch.object(
        registration_service.codex_retry_service, "release"
    ), patch.object(registration_service, "_activate_job"), patch.object(
        registration_service, "_deactivate_job"
    ), patch.object(registration_service, "schedule_registration_job_retention"
    ) as schedule:
        registration_service._run_codex_retry_job(7, "/tmp/unused.log", "u@example.com", 9)
    schedule.assert_called_once_with()


def test_submission_failures_trigger_retention_after_terminal_update():
    executor = Mock()
    executor.submit.side_effect = RuntimeError("queue closed")
    job = {"id": 11, "status": "pending", "log_file": "/tmp/job.log"}
    with patch.object(registration_service, "get_executor", return_value=executor), patch.object(
        registration_service, "get_executor_workers", return_value=1
    ), patch.object(
        registration_service.db, "create_registration_batch", return_value={"id": 4}
    ), patch.object(registration_service.db, "create_job", return_value=job), patch.object(
        registration_service.db, "update_job"
    ) as update_job, patch.object(
        registration_service.db, "get_job", return_value={**job, "status": "failed"}
    ), patch.object(registration_service.db, "seal_registration_batch") as seal, patch.object(
        registration_service, "schedule_registration_job_retention"
    ) as schedule:
        registration_service.submit_registration(count=1, email_source="icloud", workers=1)

    update_job.assert_called_once()
    seal.assert_called_once_with(4, [11])
    schedule.assert_called_once_with()

    source = {"id": 20, "status": "failed", "email_source": "icloud"}
    retry = {"id": 21, "status": "pending", "log_file": "/tmp/retry.log"}
    with patch.object(registration_service.db, "get_job", side_effect=[source, {**retry, "status": "failed"}]), patch.object(
        registration_service, "get_retry_info", return_value={"retryable": True, "retry_action": "registration"}
    ), patch.object(registration_service, "_account_for_job", return_value=None), patch.object(
        registration_service.db, "create_retry_job", return_value=(retry, True)
    ), patch.object(registration_service, "get_executor", return_value=executor), patch.object(
        registration_service.db, "update_job"
    ) as update_job, patch.object(
        registration_service, "schedule_registration_job_retention"
    ) as schedule:
        result = registration_service.retry_job(20, workers=1)

    assert result["status"] == 500
    update_job.assert_called_once()
    schedule.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
