# -*- coding: utf-8 -*-
import io
import json
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from core import account_transfer, db
from webui.app import create_app


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "webui" / "templates" / "index.html"


@contextmanager
def _storage(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    values = {
        "_PROJECT_ROOT": root,
        "_DATA_DIR": root,
        "_LOG_DIR": root / "logs",
        "_OUTLOOK_JSON": root / "outlook.json",
        "_DEFAULT_OUTLOOK_JSON": root / "outlook.json",
        "_OUTLOOK_TXT": root / "outlook.txt",
        "_GENERIC_API_EMAIL_JSON": root / "generic.json",
        "_DEFAULT_GENERIC_API_EMAIL_JSON": root / "generic.json",
        "_GENERIC_API_EMAIL_TXT": root / "generic.txt",
        "_ICLOUD_EMAIL_JSON": root / "icloud.json",
        "_DEFAULT_ICLOUD_EMAIL_JSON": root / "icloud.json",
        "_ICLOUD_EMAIL_TXT": root / "icloud.txt",
        "_DOMAIN_EMAIL_JSON": root / "domain.json",
        "_DEFAULT_DOMAIN_EMAIL_JSON": root / "domain.json",
        "_ACCOUNTS_JSON": root / "accounts.json",
        "_DEFAULT_ACCOUNTS_JSON": root / "accounts.json",
        "_ACCOUNTS_TXT": root / "accounts.txt",
        "_TOKENS_TXT": root / "tokens.txt",
        "_GROUPS_JSON": root / "groups.json",
        "_DEFAULT_GROUPS_JSON": root / "groups.json",
        "_CODEX_DIR": root / "codex_accounts",
        "_CODEX_EXPORT_STATE": root / "codex_export_state.json",
        "_DEFAULT_CODEX_EXPORT_STATE": root / "codex_export_state.json",
    }
    with patch.multiple(db, **values), patch.object(db, "_render_static_viewer", return_value=root / "viewer.html"), patch.object(
        db, "_schedule_static_viewer_refresh", return_value=None
    ), patch.dict("os.environ", {"TURB_STORAGE_BACKEND": "json"}):
        yield


@contextmanager
def _sqlite_storage(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    values = {
        "_PROJECT_ROOT": root,
        "_DATA_DIR": root,
        "_LOG_DIR": root / "logs",
        "_SQLITE_PATH": root / "registration.sqlite3",
        "_DEFAULT_SQLITE_PATH": root / "registration.sqlite3",
        "_SQLITE_READY_PATH": None,
        "_SQLITE_STORE_INSTANCE": None,
        "_OUTLOOK_JSON": root / "outlook.json",
        "_DEFAULT_OUTLOOK_JSON": root / "outlook.json",
        "_OUTLOOK_TXT": root / "outlook.txt",
        "_GENERIC_API_EMAIL_JSON": root / "generic.json",
        "_DEFAULT_GENERIC_API_EMAIL_JSON": root / "generic.json",
        "_GENERIC_API_EMAIL_TXT": root / "generic.txt",
        "_ICLOUD_EMAIL_JSON": root / "icloud.json",
        "_DEFAULT_ICLOUD_EMAIL_JSON": root / "icloud.json",
        "_ICLOUD_EMAIL_TXT": root / "icloud.txt",
        "_DOMAIN_EMAIL_JSON": root / "domain.json",
        "_DEFAULT_DOMAIN_EMAIL_JSON": root / "domain.json",
        "_ACCOUNTS_JSON": root / "accounts.json",
        "_DEFAULT_ACCOUNTS_JSON": root / "accounts.json",
        "_ACCOUNTS_TXT": root / "accounts.txt",
        "_TOKENS_TXT": root / "tokens.txt",
        "_GROUPS_JSON": root / "groups.json",
        "_DEFAULT_GROUPS_JSON": root / "groups.json",
        "_JOBS_JSON": root / "jobs.json",
        "_DEFAULT_JOBS_JSON": root / "jobs.json",
        "_REGISTRATION_BATCHES_JSON": root / "batches.json",
        "_DEFAULT_REGISTRATION_BATCHES_JSON": root / "batches.json",
        "_CODEX_DIR": root / "codex_accounts",
        "_CODEX_EXPORT_STATE": root / "codex_export_state.json",
        "_DEFAULT_CODEX_EXPORT_STATE": root / "codex_export_state.json",
        "_ACCOUNT_ROWS_CACHE": None,
        "_ACCOUNT_ROWS_CACHE_SIGNATURE": None,
    }
    with patch.multiple(db, **values), patch.object(db, "_render_static_viewer", return_value=root / "viewer.html"), patch.object(
        db, "_schedule_static_viewer_refresh", return_value=None
    ), patch.dict("os.environ", {"TURB_STORAGE_BACKEND": "sqlite"}):
        db.initialize_sqlite_storage(force=True)
        yield


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _seed_source(root: Path):
    agent_path = root / "codex_agent_accounts" / "agent-user.json"
    _write_json(agent_path, {"agent_identity": {"token": "agent-secret"}})
    codex_path = root / "codex_accounts" / "codex-user@example.test-plus.json"
    _write_json(codex_path, {"email": "user@example.test", "access_token": "codex-secret"})
    account = {
        "id": 7,
        "email": "user@example.test",
        "access_token": "account-token",
        "extra_json": json.dumps({"Session": {"accessToken": "session-secret"}}),
        "current_plan_type": "plus",
        "totp_secret": "TOTPSECRET",
        "note": "portable-note",
        "sms_completed": True,
        "registration_traffic_bytes": 321,
        "email_source": "icloud",
        "group_name": "来源分组",
        "codex_agent_status": "success",
        "codex_agent_token": json.dumps({"token": "agent-secret"}),
        "codex_agent_auth_path": str(agent_path),
    }
    _write_json(root / "accounts.json", [account])
    _write_json(root / "icloud.json", [{
        "id": 4,
        "email": "user@example.test",
        "auth_token": "mail-token",
        "code_url": "https://mail.example.test/pickup",
        "status": "used",
        "registered_account_id": 7,
    }])
    _write_json(root / "groups.json", [{"id": 1, "name": "来源分组", "is_default": True}])
    for name in ("outlook.json", "generic.json", "domain.json"):
        _write_json(root / name, [])


def _seed_destination(root: Path):
    _write_json(root / "accounts.json", [{"id": 20, "email": "existing@example.test", "group_name": "目标组"}])
    _write_json(root / "groups.json", [
        {"id": 1, "name": "默认分组", "is_default": True},
        {"id": 9, "name": "目标组", "is_default": False},
    ])
    for name in ("outlook.json", "generic.json", "icloud.json", "domain.json"):
        _write_json(root / name, [])


def test_complete_archive_round_trip_preserves_raw_fields_pool_and_attachments(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _seed_source(source)
    with _storage(source):
        content, filename, summary = account_transfer.build_account_archive([7], app_version="2026.09.11.14")
    assert filename.endswith(".zip")
    assert summary == {"account_count": 1, "attachment_count": 2, "skipped": []}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        exported = manifest["accounts"][0]
        assert exported["account"]["extra_json"] == json.dumps({"Session": {"accessToken": "session-secret"}})
        assert exported["email_pool"]["record"]["auth_token"] == "mail-token"
        assert all(len(item["sha256"]) == 64 for item in exported["attachments"])

    _seed_destination(destination)
    with _storage(destination):
        result = account_transfer.import_account_archive(content, group_id=9)
        rows = json.loads((destination / "accounts.json").read_text(encoding="utf-8"))
        pools = json.loads((destination / "icloud.json").read_text(encoding="utf-8"))
    assert result["imported_count"] == 1
    assert result["attachment_count"] == 2
    imported = next(row for row in rows if row["email"] == "user@example.test")
    assert imported["id"] == 21
    assert imported["group_name"] == "目标组"
    assert imported["access_token"] == "account-token"
    assert imported["current_plan_type"] == "plus"
    assert imported["totp_secret"] == "TOTPSECRET"
    assert imported["note"] == "portable-note"
    assert imported["registration_traffic_bytes"] == 321
    assert json.loads(imported["extra_json"])["Session"]["accessToken"] == "session-secret"
    assert Path(imported["codex_agent_auth_path"]).is_file()
    assert pools[0]["registered_account_id"] == 21
    assert list((destination / "codex_accounts").glob("codex-*.json"))


def test_sqlite_import_replaces_account_and_pool_collections_in_one_transaction(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _seed_source(source)
    with _storage(source):
        content, _, _ = account_transfer.build_account_archive([7], app_version="test")
    _seed_destination(destination)
    _write_json(destination / "jobs.json", [])
    _write_json(destination / "batches.json", [])
    _write_json(destination / "codex_export_state.json", {})
    with _sqlite_storage(destination):
        store = db._sqlite_store()
        with patch.object(store, "replace_all", wraps=store.replace_all) as replace_all:
            result = account_transfer.import_account_archive(content, group_id=9)
        accounts = store.load_records("registered_accounts")
        pools = store.load_records("icloud_email_pool")
    assert result["imported_count"] == 1
    assert replace_all.call_count == 1
    collections = replace_all.call_args.args[0]
    assert set(collections) == {
        "registered_accounts",
        "outlook_pool",
        "generic_api_email_pool",
        "icloud_email_pool",
        "domain_email_pool",
    }
    imported = next(row for row in accounts if row["email"] == "user@example.test")
    assert imported["group_name"] == "目标组"
    assert pools[0]["registered_account_id"] == imported["id"]


def test_batch_export_skips_missing_and_import_skips_duplicate_email(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _seed_source(source)
    rows = json.loads((source / "accounts.json").read_text(encoding="utf-8"))
    rows.append({"id": 8, "email": "second@example.test", "access_token": "second-token", "group_name": "来源分组"})
    _write_json(source / "accounts.json", rows)
    with _storage(source):
        content, _, summary = account_transfer.build_account_archive([7, 8, 999, 7], app_version="test")
    assert summary["account_count"] == 2
    assert summary["skipped"] == [{"id": 999, "reason": "账号不存在"}]
    _seed_destination(destination)
    rows = json.loads((destination / "accounts.json").read_text(encoding="utf-8"))
    rows.append({"id": 21, "email": "user@example.test", "note": "keep-local", "group_name": "目标组"})
    _write_json(destination / "accounts.json", rows)
    with _storage(destination):
        result = account_transfer.import_account_archive(content, group_id=9)
    saved = json.loads((destination / "accounts.json").read_text(encoding="utf-8"))
    assert result["imported_count"] == 1
    assert result["skipped"][0]["reason"] == "同邮箱账号已存在"
    assert next(row for row in saved if row["email"] == "user@example.test")["note"] == "keep-local"
    assert next(row for row in saved if row["email"] == "second@example.test")["access_token"] == "second-token"
    assert not list((destination / "codex_accounts").glob("*.json"))


def _archive_with_manifest(manifest: dict, extras: dict[str, bytes] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, content in (extras or {}).items():
            archive.writestr(name, content)
    return output.getvalue()


def test_archive_validation_rejects_traversal_and_bad_attachment_hash():
    attachment = b'{"token":"value"}'
    descriptor = {
        "kind": "codex_credential",
        "account_field": None,
        "archive_path": "attachments/account-1/value.json",
        "filename": "value.json",
        "size": len(attachment),
        "sha256": "0" * 64,
    }
    manifest = {
        "format": account_transfer.FORMAT_ID,
        "schema_version": 1,
        "account_count": 1,
        "accounts": [{
            "transfer_id": "account-1",
            "account": {"email": "user@example.test"},
            "email_pool": None,
            "attachments": [descriptor],
        }],
    }
    with pytest.raises(account_transfer.AccountTransferError, match="哈希校验失败"):
        account_transfer.parse_account_archive(_archive_with_manifest(manifest, {descriptor["archive_path"]: attachment}))
    with pytest.raises(account_transfer.AccountTransferError, match="非法路径"):
        account_transfer.parse_account_archive(_archive_with_manifest(manifest, {"../value.json": attachment}))


def test_archive_validation_rejects_invalid_format_and_uncompressed_limit(monkeypatch):
    with pytest.raises(account_transfer.AccountTransferError, match="不是账号完整迁移包"):
        account_transfer.parse_account_archive(_archive_with_manifest({"format": "other", "schema_version": 1}))
    monkeypatch.setattr(account_transfer, "MAX_UNCOMPRESSED_BYTES", 20)
    with pytest.raises(account_transfer.AccountTransferError, match="解压后超过"):
        account_transfer.parse_account_archive(_archive_with_manifest({"padding": "x" * 50}))


def _client():
    client = create_app(auth_code="transfer-test").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "transfer-test"
    return client


def test_transfer_endpoints_return_zip_and_validate_multipart_group():
    client = _client()
    with patch("webui.app.account_transfer.build_account_archive", return_value=(b"PK-test", "accounts.zip", {"account_count": 2})) as build:
        response = client.post("/api/accounts/transfer/export", json={"account_ids": [1, 2]})
    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    assert "accounts.zip" in response.headers["Content-Disposition"]
    assert response.headers["X-Account-Count"] == "2"
    build.assert_called_once()

    response = client.post(
        "/api/accounts/transfer/import",
        data={"file": (io.BytesIO(b"PK-test"), "accounts.zip")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "请选择目标分组"

    expected = {"ok": True, "imported_count": 2, "skipped_count": 0}
    with patch("webui.app.account_transfer.import_account_archive", return_value=expected) as load:
        response = client.post(
            "/api/accounts/transfer/import",
            data={"group_id": "9", "file": (io.BytesIO(b"PK-test"), "accounts.zip")},
            content_type="multipart/form-data",
        )
    assert response.status_code == 200
    assert response.get_json() == expected
    assert load.call_args.kwargs["group_id"] == 9


def test_account_page_has_complete_zip_transfer_controls_and_drag_drop_modal():
    source = TEMPLATE.read_text(encoding="utf-8")
    for marker in (
        'id="btnImportAccountsZipV2"',
        'id="btnExportAccountsZipV2" disabled',
        'id="accountTransferImportModal"',
        'id="accountTransferDropzone"',
        'id="accountTransferGroupSelect"',
        "dragenter",
        "dragover",
        "dragleave",
        "event.dataTransfer?.files?.[0]",
        "FormData()",
        "'/api/accounts/transfer/import'",
        "'/api/accounts/transfer/export'",
        "'btnExportAccountsZipV2',",
    ):
        assert marker in source
