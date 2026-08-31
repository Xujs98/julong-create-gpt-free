# -*- coding: utf-8 -*-
import json
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


def _storage_patch(root: Path):
    sqlite_path = root / "data" / "registration.sqlite3"
    paths = {
        "_SQLITE_PATH": sqlite_path,
        "_DEFAULT_SQLITE_PATH": sqlite_path,
        "_JOBS_JSON": root / "jobs.json",
        "_DEFAULT_JOBS_JSON": root / "jobs.json",
        "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
        "_ACCOUNTS_JSON": root / "accounts.json",
        "_DEFAULT_ACCOUNTS_JSON": root / "accounts.json",
        "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
        "_OUTLOOK_JSON": root / "outlook.json",
        "_DEFAULT_OUTLOOK_JSON": root / "outlook.json",
        "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
        "_GENERIC_API_EMAIL_JSON": root / "generic.json",
        "_DEFAULT_GENERIC_API_EMAIL_JSON": root / "generic.json",
        "_ICLOUD_EMAIL_JSON": root / "icloud.json",
        "_DEFAULT_ICLOUD_EMAIL_JSON": root / "icloud.json",
        "_REGISTRATION_BATCHES_JSON": root / "batches.json",
        "_DEFAULT_REGISTRATION_BATCHES_JSON": root / "batches.json",
        "_DOMAIN_EMAIL_JSON": root / "domain.json",
        "_DEFAULT_DOMAIN_EMAIL_JSON": root / "domain.json",
        "_GROUPS_JSON": root / "groups.json",
        "_DEFAULT_GROUPS_JSON": root / "groups.json",
        "_CODEX_EXPORT_STATE": root / "codex-state.json",
        "_DEFAULT_CODEX_EXPORT_STATE": root / "codex-state.json",
        "_SQLITE_READY_PATH": None,
        "_SQLITE_STORE_INSTANCE": None,
    }
    return patch.multiple(db, **paths)


def test_sqlite_job_page_only_decodes_requested_slice(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "sqlite")
    rows = [
        {
            "id": index,
            "status": "success" if index % 2 else "failed",
            "email": f"u{index}@example.com",
            "batch_id": 1 if index > 190 else None,
        }
        for index in range(1, 201)
    ]
    (tmp_path / "jobs.json").write_text(json.dumps(rows), encoding="utf-8")
    (tmp_path / "batches.json").write_text(
        json.dumps([{
            "id": 1,
            "status": "running",
            "requested_count": 10,
            "sealed_at": "2026-08-17T00:00:00",
            "job_ids": list(range(191, 201)),
            "started_at": "2026-08-17T00:00:00",
        }]),
        encoding="utf-8",
    )

    with _storage_patch(tmp_path):
        # 初始化后禁止走兼容层全量读取，分页查询仍应正常工作。
        db.initialize_sqlite_storage()
        with patch.object(db, "_load_jobs", side_effect=AssertionError("unexpected full job load")):
            page = db.list_jobs_page(limit=10, offset=20)
            latest_batch = db.get_latest_registration_batch()

    assert [row["id"] for row in page["items"]] == list(range(180, 170, -1))
    assert page["total"] == 200
    assert page["status_counts"] == {"failed": 100, "success": 100, "active": 0}
    assert latest_batch["submitted_count"] == 10
    assert latest_batch["success_count"] == 5
    assert latest_batch["failed_count"] == 5


def test_sqlite_backfills_legacy_rebind_jobs_into_batch_history(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "sqlite")
    jobs = [{
        "id": 459,
        "job_type": "rebind",
        "email_source": "icloud",
        "email": "target@example.com",
        "status": "failed",
        "started_at": "2026-08-31T10:00:00",
        "completed_at": "2026-08-31T10:01:00",
        "created_at": "2026-08-31T10:00:00",
        "batch_id": None,
        "rebind_source_email": "source@example.com",
        "rebind_target_email": "target@example.com",
    }]
    (tmp_path / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
    (tmp_path / "batches.json").write_text("[]", encoding="utf-8")

    with _storage_patch(tmp_path):
        db.initialize_sqlite_storage()
        batches = db.list_registration_batches()
        latest = db.get_latest_registration_batch()
        migrated_batch_id = db._load_jobs()[0]["batch_id"]

    assert len(batches) == 1
    assert batches[0]["task_type"] == "rebind"
    assert batches[0]["job_ids"] == [459]
    assert batches[0]["failed_count"] == 1
    assert batches[0]["status"] == "completed"
    assert latest["task_type"] == "rebind"
    assert latest["job_ids"] == [459]
    assert migrated_batch_id == batches[0]["id"]


def test_retry_info_batch_loads_each_json_collection_once(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "json")
    jobs = [
        {"id": 1, "status": "failed", "email": "new@example.com"},
        {"id": 2, "status": "failed", "email": "known@example.com", "account_id": 10},
        {"id": 3, "status": "failed", "email": "retried@example.com"},
        {"id": 4, "status": "success", "root_job_id": 3, "parent_job_id": 3},
    ]
    accounts = [{"id": 10, "email": "known@example.com", "codex_status": "failed"}]
    (tmp_path / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
    (tmp_path / "accounts.json").write_text(json.dumps(accounts), encoding="utf-8")

    with _storage_patch(tmp_path), patch.object(db, "_load_jobs", wraps=db._load_jobs) as load_jobs, patch.object(
        db, "_load_accounts", wraps=db._load_accounts
    ) as load_accounts:
        result = db.get_job_retry_info_batch(jobs)

    assert load_jobs.call_count == 1
    assert load_accounts.call_count == 1
    assert result[1]["retry_action"] == "registration"
    assert result[2]["retry_action"] == "codex"
    assert result[2]["display_status"] == "partial_success"
    assert result[3]["successful_retry_job_id"] == 4
    assert result[3]["retryable"] is False


def test_paged_jobs_api_uses_page_and_batch_retry_queries():
    app = create_app(auth_code="job-query-test")
    client = app.test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "job-query-test"
    page = {
        "items": [{"id": 9, "status": "failed", "email": "one@example.com"}],
        "total": 200,
        "offset": 50,
        "limit": 25,
        "status_counts": {"failed": 200, "active": 0},
    }
    retry = {
        9: {
            "retryable": True,
            "retry_action": "registration",
            "retry_label": "重试",
            "retry_reason": None,
            "display_status": "failed",
        }
    }

    with patch("webui.app.svc.schedule_registration_job_retention", return_value=True) as schedule, patch(
        "webui.app.db.list_jobs_page", return_value=page
    ) as list_page, patch("webui.app.db.list_jobs") as list_jobs, patch(
        "webui.app.db.get_job_retry_info_batch", return_value=retry
    ) as retry_batch, patch("webui.app.svc.get_retry_info") as retry_one, patch(
        "webui.app.db.get_latest_registration_batch", return_value=None
    ):
        first = client.get("/api/jobs?paged=1&page=3&page_size=25")
        second = client.get("/api/jobs?paged=1&page=3&page_size=25")

    assert first.status_code == 200
    assert first.get_json()["total"] == 200
    assert first.get_json()["items"][0]["retry_action"] == "registration"
    assert second.status_code == 200
    assert schedule.call_count == 1
    assert list_page.call_count == 2
    list_page.assert_called_with(limit=25, offset=50)
    retry_batch.assert_called_with(page["items"])
    list_jobs.assert_not_called()
    retry_one.assert_not_called()
