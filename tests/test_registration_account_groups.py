# -*- coding: utf-8 -*-
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from config import email as email_config
from config import proxy as proxy_config
from config import register as register_config
from core import db, registration_service
from webui.app import create_app


ROOT = Path(__file__).parents[1]


def _isolate_json_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "json")
    paths = {
        "_ACCOUNTS_JSON": tmp_path / "accounts.json",
        "_DEFAULT_ACCOUNTS_JSON": tmp_path / "accounts.json",
        "_LEGACY_ACCOUNTS_JSON": tmp_path / "legacy-accounts.json",
        "_GROUPS_JSON": tmp_path / "groups.json",
        "_DEFAULT_GROUPS_JSON": tmp_path / "groups.json",
        "_JOBS_JSON": tmp_path / "jobs.json",
        "_DEFAULT_JOBS_JSON": tmp_path / "jobs.json",
        "_LEGACY_JOBS_JSON": tmp_path / "legacy-jobs.json",
        "_REGISTRATION_BATCHES_JSON": tmp_path / "batches.json",
        "_DEFAULT_REGISTRATION_BATCHES_JSON": tmp_path / "batches.json",
        "_LOG_DIR": tmp_path / "logs",
    }
    for key, value in paths.items():
        monkeypatch.setattr(db, key, value)
    monkeypatch.setattr(db, "_sync_accounts_txt", lambda _rows: None)
    monkeypatch.setattr(db, "_sync_tokens_txt", lambda _rows: None)
    monkeypatch.setattr(db, "_render_static_viewer", lambda **_kwargs: None)


def _client():
    client = create_app(auth_code="test-auth").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
    return client


def test_jobs_api_validates_and_forwards_registration_group_before_preflight():
    client = _client()
    selected = {"id": 8, "name": "批次分组", "is_default": False}
    with patch("webui.app.db.get_account_group", return_value=selected) as get_group, patch(
        "webui.app.svc.submit_registration", return_value=[]
    ) as submit, patch.object(proxy_config, "PROXY_CHECK_BEFORE_REGISTRATION", False), patch.object(
        email_config, "USE_EMAIL_SERVICE", False
    ), patch.object(register_config, "REGISTER_EMAIL", "user@example.test"):
        response = client.post("/api/jobs", json={"count": 1, "workers": 2, "group_id": 8})

    assert response.status_code == 200
    assert response.get_json()["registration_group"] == selected
    get_group.assert_called_once_with(group_id=8)
    submit.assert_called_once_with(count=1, workers=2, group_id=8)

    with patch("webui.app.db.get_account_group", return_value=None), patch(
        "webui.app.svc.submit_registration"
    ) as submit:
        response = client.post("/api/jobs", json={"count": 1, "workers": 1, "group_id": 999})
    assert response.status_code == 400
    submit.assert_not_called()


def test_registration_jobs_persist_group_and_retry_inherits_it(monkeypatch, tmp_path):
    _isolate_json_storage(monkeypatch, tmp_path)
    group = db.create_account_group("批次分组")
    executor = Mock()
    with patch.object(registration_service, "get_executor", return_value=executor), patch.object(
        registration_service, "get_executor_workers", return_value=2
    ), patch.object(registration_service, "schedule_registration_job_retention"):
        jobs = registration_service.submit_registration(
            count=1,
            email_source="icloud",
            workers=2,
            group_id=group["id"],
        )

    job = jobs[0]
    assert job["registration_group_id"] == group["id"]
    assert job["registration_group_name"] == "批次分组"
    batch = db.get_registration_batch(job["batch_id"])
    assert batch["registration_group_id"] == group["id"]
    assert batch["registration_group_name"] == "批次分组"

    db.update_job(job["id"], status="failed")
    retry, created = db.create_retry_job(
        job["id"],
        job_type="registration_retry",
        email_source="icloud",
    )
    assert created is True
    assert retry["registration_group_id"] == group["id"]
    assert retry["registration_group_name"] == "批次分组"


def test_group_assignment_uses_stable_id_and_active_job_blocks_deletion(monkeypatch, tmp_path):
    _isolate_json_storage(monkeypatch, tmp_path)
    group = db.create_account_group("原名称")
    job = db.create_job(
        "icloud",
        registration_group_id=group["id"],
        registration_group_name=group["name"],
    )
    with pytest.raises(ValueError, match=f"任务 #{job['id']}"):
        db.delete_account_group(group["id"])

    renamed = db.rename_account_group(group["id"], "新名称")
    db._save_accounts([{"id": 41, "email": "user@example.test", "group_name": db.DEFAULT_ACCOUNT_GROUP}])
    assigned = db.assign_account_to_registration_group(
        41,
        group["id"],
        expected_name="原名称",
    )
    assert renamed["id"] == group["id"]
    assert assigned["group_name"] == "新名称"
    assert db.get_account(41)["group_name"] == "新名称"


@pytest.mark.parametrize("success", [True, False])
def test_registration_worker_groups_accounts_for_success_and_partial_failure(success, tmp_path):
    job = {
        "id": 156,
        "status": "pending",
        "log_file": str(tmp_path / "job.log"),
        "email_source": "outlook",
        "registration_group_id": 9,
        "registration_group_name": "目标分组",
    }
    result = {"success": success, "account_id": 77, "email": "user@example.test"}
    if not success:
        result["error"] = "Codex 阶段失败"
    with patch.object(registration_service.db, "get_job", return_value=job), patch.object(
        registration_service.db, "update_job"
    ) as update_job, patch.object(
        registration_service.db,
        "assign_account_to_registration_group",
        return_value={"id": 77, "group_name": "目标分组"},
    ) as assign, patch.object(registration_service, "_activate_job"), patch.object(
        registration_service, "_deactivate_job"
    ), patch.object(registration_service, "schedule_registration_job_retention"), patch.object(
        registration_service, "_select_registration_proxy", return_value=None
    ), patch.object(
        registration_service,
        "_prepare_registration_args",
        return_value=("user@example.test", "User", "1990-01-01"),
    ), patch.object(registration_service, "_registration_transient_retry_limit", return_value=0), patch.object(
        registration_service, "_release_unconsumed_job_email"
    ), patch.object(registration_service, "_should_disable_failed_registration_email", return_value=False), patch(
        "core.registration_driver_health.require_registration_driver_ready"
    ), patch("main.run_registration", return_value=result):
        registration_service._run_one_job(job["id"], job["log_file"])

    assign.assert_called_once_with(77, 9, expected_name="目标分组")
    terminal = [call.kwargs for call in update_job.call_args_list if call.kwargs.get("status") in {"success", "failed"}]
    assert terminal[-1]["status"] == ("success" if success else "failed")
    assert terminal[-1]["account_id"] == 77


def test_account_page_decorates_only_requested_region(monkeypatch):
    rows = [{"id": value, "email": f"u{value}@example.test"} for value in range(1, 31)]
    decorate = Mock(side_effect=lambda row, **_kwargs: dict(row))
    monkeypatch.setattr(db, "_load_accounts", lambda: rows)
    monkeypatch.setattr(db, "_decorate_account", decorate)

    result = db.list_accounts_page(offset=6, limit=4)

    assert result["total"] == 30
    assert [row["id"] for row in result["items"]] == [24, 23, 22, 21]
    assert decorate.call_count == 4


def test_accounts_api_supports_explicit_region_offset_and_limit():
    client = _client()
    with patch(
        "webui.app.db.list_accounts_page",
        return_value={"items": [], "total": 40, "offset": 5, "limit": 4, "revision": "40:"},
    ) as list_page:
        response = client.get("/api/accounts?paged=1&page=1&page_size=20&offset=5&limit=4")

    assert response.status_code == 200
    assert response.get_json()["page_size"] == 20
    assert response.get_json()["offset"] == 5
    assert response.get_json()["limit"] == 4
    assert list_page.call_args.kwargs["offset"] == 5
    assert list_page.call_args.kwargs["limit"] == 4


def test_registration_group_selector_and_region_loader_are_present():
    source = (ROOT / "webui/templates/index.html").read_text(encoding="utf-8")
    selector_index = source.index('id="regAccountGroupV2"')
    start_index = source.index('id="btnStartV2"')
    assert selector_index < start_index
    assert "data-empty-text=\"没有匹配的分组\"" in source
    assert "body.group_id = Number(registrationGroupId)" in source
    assert "accountsLoadIndicatorV2" in source
    assert "accountsLoadPulseV2" in source
    assert "visibleAccountRegionSize()" in source
    assert "waitForAccountRegionIdle()" in source
    assert "offset: String(Math.max(0" in source
    assert "function accountHasActiveOperations()" in source
    assert "accountHasActiveOperations() ? 2000 : 10000" in source
    assert "scheduleAccountStatusPoll(2000)" in source
    assert "setInterval(() => { if (!$('#tab-accounts')" not in source
    assert "const ACCOUNT_PAGE_CACHE = new Map()" in source
    assert "async function prefetchNextAccountPage" in source
    assert "restoreAccountPageCache(cacheKey)" in source
    assert "renderAccounts({appendFrom: before})" in source
    assert "accountsLoadAbortController.abort()" in source
