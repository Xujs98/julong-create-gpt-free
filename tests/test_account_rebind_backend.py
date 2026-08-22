# -*- coding: utf-8 -*-
import json
import time
from pathlib import Path
from unittest.mock import patch

from core import db, rebind_service
from webui.app import create_app


def _storage(tmp_path: Path):
    values = {
        "_DATA_DIR": tmp_path,
        "_LOG_DIR": tmp_path / "logs",
        "_SQLITE_PATH": tmp_path / "registration.sqlite3",
        "_DEFAULT_SQLITE_PATH": tmp_path / "registration.sqlite3",
        "_OUTLOOK_JSON": tmp_path / "outlook.json",
        "_DEFAULT_OUTLOOK_JSON": tmp_path / "outlook.json",
        "_GENERIC_API_EMAIL_JSON": tmp_path / "generic.json",
        "_DEFAULT_GENERIC_API_EMAIL_JSON": tmp_path / "generic.json",
        "_ICLOUD_EMAIL_JSON": tmp_path / "icloud.json",
        "_DEFAULT_ICLOUD_EMAIL_JSON": tmp_path / "icloud.json",
        "_DOMAIN_EMAIL_JSON": tmp_path / "domain.json",
        "_DEFAULT_DOMAIN_EMAIL_JSON": tmp_path / "domain.json",
        "_ACCOUNTS_JSON": tmp_path / "accounts.json",
        "_DEFAULT_ACCOUNTS_JSON": tmp_path / "accounts.json",
        "_JOBS_JSON": tmp_path / "jobs.json",
        "_DEFAULT_JOBS_JSON": tmp_path / "jobs.json",
        "_LEGACY_JOBS_JSON": tmp_path / "legacy-jobs.json",
        "_REGISTRATION_BATCHES_JSON": tmp_path / "batches.json",
        "_DEFAULT_REGISTRATION_BATCHES_JSON": tmp_path / "batches.json",
        "_GROUPS_JSON": tmp_path / "groups.json",
        "_DEFAULT_GROUPS_JSON": tmp_path / "groups.json",
        "_CODEX_EXPORT_STATE": tmp_path / "codex.json",
        "_DEFAULT_CODEX_EXPORT_STATE": tmp_path / "codex.json",
        "_SQLITE_READY_PATH": None,
        "_SQLITE_STORE_INSTANCE": None,
    }
    for key, value in values.items():
        if key.endswith("JSON") or key.endswith("_JOBS_JSON"):
            pass
    return patch.multiple(db, **values)


def _seed(tmp_path: Path):
    (tmp_path / "accounts.json").write_text(json.dumps([
        {"id": 1, "email": "old@example.com", "email_source": "icloud", "group_name": "默认分组", "access_token": "old-token"},
    ]), encoding="utf-8")
    (tmp_path / "icloud.json").write_text(json.dumps([
        {"id": 1, "email": "new@example.com", "code_url": "https://mail.test/new", "status": "available"},
    ]), encoding="utf-8")
    (tmp_path / "groups.json").write_text(json.dumps([
        {"id": 1, "name": "默认分组", "is_default": True},
        {"id": 2, "name": "换绑完成", "is_default": False},
    ]), encoding="utf-8")
    for name in ("jobs.json", "batches.json", "outlook.json", "generic.json", "domain.json", "codex.json"):
        (tmp_path / name).write_text("[]" if name.endswith(".json") and name not in {"codex.json"} else "{}", encoding="utf-8")


def _wait_terminal(job_id: int, *, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    job = db.get_job(job_id) or {}
    while time.monotonic() < deadline and job.get("status") not in {"success", "failed", "stopped", "cancelled"}:
        time.sleep(0.01)
        job = db.get_job(job_id) or {}
    return job


def test_rebind_reservation_and_finalize_replaces_old_account(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "json")
    _seed(tmp_path)
    with _storage(tmp_path):
        reserved = db.reserve_rebind_emails(["icloud"], 1, reservation_id="res-1")
        assert reserved[0]["email"] == "new@example.com"
        job = db.create_rebind_job(
            source_account_id=1,
            source_email="old@example.com",
            target=reserved[0],
            group={"id": 2, "name": "换绑完成"},
            reservation_id="res-1",
            driver="protocol",
            headless=False,
        )
        replacement = db.finalize_rebind_account(
            1,
            target_email="new@example.com",
            target_source="icloud",
            target_pool_id=1,
            reservation_id="res-1",
            group_id=2,
            job_id=job["id"],
            result={"access_token": "new-token", "verified_email": "new@example.com", "ok": True},
        )
        assert replacement["email"] == "new@example.com"
        assert replacement["rebind_from_email"] == "old@example.com"
        assert db.get_account(1) is None
        assert db.get_account(replacement["id"])["group_name"] == "换绑完成"
        assert db.get_icloud_email_by_email("new@example.com")["status"] == "used"


def test_rebind_service_success_keeps_task_in_shared_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "json")
    _seed(tmp_path)
    callback_calls = []

    def executor(account, target, **kwargs):
        callback_calls.append((account["email"], target["email"], kwargs["driver"], kwargs["headless"]))
        return {"ok": True, "access_token": "refreshed-token", "verified_email": target["email"]}

    with _storage(tmp_path):
        rebind_service.set_rebind_executor(executor)
        try:
            result = rebind_service.submit_rebind(
                [1], pool_sources=["icloud"], group_id=2, workers=1, driver="protocol", headless=True
            )
            job_id = result["jobs"][0]["id"]
            for _ in range(50):
                job = db.get_job(job_id)
                if job and job.get("status") in {"success", "failed", "stopped"}:
                    break
                time.sleep(0.02)
            assert job["job_type"] == "rebind"
            assert job["status"] == "success"
            assert callback_calls == [("old@example.com", "new@example.com", "protocol", True)]
            assert db.get_account_by_email("old@example.com") is None
            assert db.get_account_by_email("new@example.com")["access_token"] == "refreshed-token"
        finally:
            rebind_service.set_rebind_executor(None)
            rebind_service.shutdown_executor(wait=True)


def test_rebind_api_requires_pool_and_group_and_exposes_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "json")
    _seed(tmp_path)
    with _storage(tmp_path):
        app = create_app(auth_code="rebind-test")
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "rebind-test"
        summary = client.get("/api/rebind/pools")
        assert summary.status_code == 200
        assert summary.get_json()["pools"]["icloud"]["available"] == 1
        missing = client.post("/api/accounts/rebind", json={"account_ids": [1], "pool_sources": []})
        assert missing.status_code == 400
        missing_group = client.post("/api/accounts/rebind", json={"account_ids": [1], "pool_sources": ["icloud"]})
        assert missing_group.status_code == 400


def test_rebind_success_contract_requires_verified_email_and_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "json")
    _seed(tmp_path)
    with _storage(tmp_path):
        for result in (
            {"ok": True, "access_token": "token-only"},
            {"ok": True, "verified_email": "new@example.com"},
            {"ok": True, "verified_email": "other@example.com", "access_token": "token"},
            {"ok": "false", "verified_email": "new@example.com", "access_token": "token"},
        ):
            try:
                rebind_service._validated_success_result(result, "new@example.com")
            except rebind_service.RebindExecutionError:
                pass
            else:
                raise AssertionError(f"unexpectedly accepted incomplete result: {result}")
        accepted = rebind_service._validated_success_result(
            {
                "ok": True,
                "verified_email": "new@example.com",
                "session": {"user": {"email": "new@example.com", "accessToken": "session-token"}},
            },
            "new@example.com",
        )
        assert accepted["access_token"] == "session-token"


def test_finalize_rejects_string_false_success_without_touching_account(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "json")
    _seed(tmp_path)
    with _storage(tmp_path):
        reserved = db.reserve_rebind_emails(["icloud"], 1, reservation_id="false-success")
        with patch.object(db, "_save_accounts") as save_accounts:
            try:
                db.finalize_rebind_account(
                    1,
                    target_email="new@example.com",
                    target_source="icloud",
                    target_pool_id=reserved[0]["id"],
                    reservation_id="false-success",
                    group_id=2,
                    result={"ok": "false", "verified_email": "new@example.com", "access_token": "token"},
                )
            except ValueError as exc:
                assert "未确认成功" in str(exc)
            else:
                raise AssertionError("string false must not finalize account")
            save_accounts.assert_not_called()
        assert db.get_account_by_email("old@example.com") is not None


def test_rebind_string_boolean_values_are_normalized_in_plan_and_pool_api(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "json")
    _seed(tmp_path)
    from config import live_check

    monkeypatch.setattr(live_check, "REBIND_HYBRID_MODE", "false")
    monkeypatch.setattr(live_check, "LIVE_CHECK_HEADLESS", "true")
    with _storage(tmp_path):
        plan = rebind_service._rebind_plan(
            login_driver="cloak",
            action_driver="protocol",
            hybrid="false",
            headless="false",
            login_headless="true",
        )
        assert plan == {
            "driver": "protocol",
            "action_driver": "protocol",
            "login_driver": "protocol",
            "hybrid": False,
            "headless": False,
            "login_headless": True,
        }
        app = create_app(auth_code="rebind-bool-test")
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "rebind-bool-test"
        payload = client.get("/api/rebind/pools").get_json()
        assert payload["hybrid"] is False
        assert payload["headless"] is True


def test_rebind_compact_job_api_preserves_false_execution_switches(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "json")
    _seed(tmp_path)
    with _storage(tmp_path):
        reserved = db.reserve_rebind_emails(["icloud"], 1, reservation_id="compact-switches")
        job = db.create_rebind_job(
            source_account_id=1,
            source_email="old@example.com",
            target=reserved[0],
            group={"id": 2, "name": "换绑完成"},
            reservation_id="compact-switches",
            driver="protocol",
            login_driver="cloak",
            action_driver="protocol",
            hybrid=False,
            headless=False,
            login_headless=False,
        )
        app = create_app(auth_code="rebind-compact-test")
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "rebind-compact-test"
        response = client.get("/api/jobs?paged=1&page_size=10")
        assert response.status_code == 200
        item = next(row for row in response.get_json()["items"] if row["id"] == job["id"])
        assert item["rebind_hybrid_mode"] is False
        assert item["rebind_headless"] is False
        assert item["rebind_login_headless"] is False


def test_rebind_failure_releases_target_and_keeps_old_account(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "json")
    _seed(tmp_path)

    def executor(account, target, **kwargs):
        return {"ok": True, "verified_email": "wrong@example.com", "access_token": "bad-token"}

    with _storage(tmp_path):
        rebind_service.set_rebind_executor(executor)
        try:
            result = rebind_service.submit_rebind([1], pool_sources=["icloud"], group_id=2, workers=1)
            job = _wait_terminal(result["jobs"][0]["id"])
            assert job["status"] == "failed"
            assert db.get_account_by_email("old@example.com") is not None
            target = db.get_icloud_email_by_email("new@example.com")
            assert target["status"] == "available"
            assert target["rebind_status"] == "failed"
            assert "bad-token" not in str(job.get("error_message") or "")
        finally:
            rebind_service.set_rebind_executor(None)
            rebind_service.shutdown_executor(wait=True)


def test_pending_rebind_cancellation_releases_target_and_worker_skips_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "json")
    _seed(tmp_path)
    callback_calls = []

    def executor(*args, **kwargs):
        callback_calls.append(True)
        return {"ok": True, "verified_email": "new@example.com", "access_token": "token"}

    with _storage(tmp_path):
        reserved = db.reserve_rebind_emails(["icloud"], 1, reservation_id="cancel-race")
        job = db.create_rebind_job(
            source_account_id=1,
            source_email="old@example.com",
            target=reserved[0],
            group={"id": 2, "name": "换绑完成"},
            reservation_id="cancel-race",
            driver="protocol",
            headless=False,
        )
        rebind_service.set_rebind_executor(executor)
        try:
            stopped = rebind_service.request_stop_rebind_job(job["id"])
            assert stopped["state"] == "cancelled"
            target = db.get_icloud_email_by_email("new@example.com")
            assert target["status"] == "available"
            assert "rebind_reservation_id" not in target
            # Simulate the worker waking after cancellation.  The semaphore
            # slot mirrors a queued batch slot and is returned by _run_one.
            assert rebind_service._queue_slots.acquire(blocking=False)
            rebind_service._run_one(job["id"])
            assert callback_calls == []
            assert db.get_job(job["id"])["status"] == "cancelled"
        finally:
            rebind_service.set_rebind_executor(None)
            rebind_service.shutdown_executor(wait=True)


def test_rebind_public_jobs_and_logs_redact_worker_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "json")
    _seed(tmp_path)
    with _storage(tmp_path):
        reserved = db.reserve_rebind_emails(["icloud"], 1, reservation_id="secret-reservation")
        job = db.create_rebind_job(
            source_account_id=1,
            source_email="old@example.com",
            target=reserved[0],
            group={"id": 2, "name": "换绑完成"},
            reservation_id="secret-reservation",
            driver="protocol",
            headless=False,
        )
        db.update_job_fields(
            job["id"],
            error=(
                "password=pool-password access_token=old-token "
                "code_url=https://mail.test/new reservation_id=secret-reservation"
            ),
        )
        Path(job["log_file"]).write_text(
            "password=pool-password access_token=old-token code_url=https://mail.test/new "
            "reservation_id=secret-reservation\n",
            encoding="utf-8",
        )
        app = create_app(auth_code="rebind-redact-test")
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "rebind-redact-test"
        jobs = client.get("/api/jobs").get_json()
        rendered = json.dumps(jobs, ensure_ascii=False)
        assert "secret-reservation" not in rendered
        assert "https://mail.test/new" not in rendered
        assert "old-token" not in rendered
        log_payload = client.get(f"/api/accounts/rebind-log?job_id={job['id']}").get_json()
        rendered_log = json.dumps(log_payload, ensure_ascii=False)
        assert "pool-password" not in rendered_log
        assert "secret-reservation" not in rendered_log
        assert "https://mail.test/new" not in rendered_log


def test_rebind_jobs_are_rejected_by_registration_retry_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "json")
    _seed(tmp_path)
    with _storage(tmp_path):
        reserved = db.reserve_rebind_emails(["icloud"], 1, reservation_id="retry-reservation")
        job = db.create_rebind_job(
            source_account_id=1,
            source_email="old@example.com",
            target=reserved[0],
            group={"id": 2, "name": "换绑完成"},
            reservation_id="retry-reservation",
            driver="protocol",
            headless=False,
        )
        db.update_job_fields(job["id"], status="failed", rebind_status="failed", error="driver failed")
        app = create_app(auth_code="rebind-retry-test")
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "rebind-retry-test"
        single = client.post(f"/api/jobs/{job['id']}/retry", json={})
        assert single.status_code == 409
        assert "换绑任务" in single.get_json()["error"]
        bulk = client.post("/api/jobs/retry-bulk", json={"job_ids": [job["id"]]})
        assert bulk.status_code == 200
        assert bulk.get_json()["skipped"][0]["id"] == job["id"]
