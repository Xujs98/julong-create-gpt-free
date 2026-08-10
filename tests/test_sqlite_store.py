# -*- coding: utf-8 -*-
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from core import db
from core.sqlite_store import SQLiteStore


def test_sqlite_store_round_trips_full_payload_and_unicode(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite3")
    source = [{
        "id": 7,
        "email": "用户@example.com",
        "status": "available",
        "extra_json": json.dumps({"备注": "保留", "nested": [1, None, True]}, ensure_ascii=False),
        "custom": {"any": "field"},
    }]

    store.replace_records("accounts", source)

    assert store.load_records("accounts") == source
    assert store.counts() == {"accounts": 1}
    assert store.integrity_check() == "ok"


def test_replace_records_rolls_back_when_payload_is_not_serializable(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite3")
    original = [{"id": 1, "email": "before@example.com"}]
    store.replace_records("accounts", original)

    with pytest.raises(TypeError):
        store.replace_records("accounts", [{"id": 2, "bad": object()}])

    assert store.load_records("accounts") == original


def test_replace_all_is_atomic_and_preserves_documents(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite3")
    collections = {
        "accounts": [{"id": 4, "email": "a@example.com"}],
        "jobs": [{"id": 9, "status": "success"}],
    }
    documents = {"codex_export_state": {"file.json": {"exported_count": 3}}}

    store.replace_all(
        collections,
        documents=documents,
        metadata={"migration": "done"},
    )
    store.replace_all(
        collections,
        documents=documents,
        metadata={"migration": "done"},
    )

    assert store.load_records("accounts") == collections["accounts"]
    assert store.load_records("jobs") == collections["jobs"]
    assert store.load_document("codex_export_state", {}) == documents["codex_export_state"]
    assert store.get_metadata("migration") == "done"


def _sqlite_db_patch(root: Path):
    sqlite_path = root / "data" / "registration.sqlite3"
    paths = {
        "_DATA_DIR": root,
        "_LOG_DIR": root / "logs",
        "_SQLITE_PATH": sqlite_path,
        "_DEFAULT_SQLITE_PATH": sqlite_path,
        "_OUTLOOK_JSON": root / "outlook.json",
        "_DEFAULT_OUTLOOK_JSON": root / "outlook.json",
        "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
        "_OUTLOOK_TXT": root / "outlook.txt",
        "_GENERIC_API_EMAIL_JSON": root / "api.json",
        "_DEFAULT_GENERIC_API_EMAIL_JSON": root / "api.json",
        "_GENERIC_API_EMAIL_TXT": root / "api.txt",
        "_ICLOUD_EMAIL_JSON": root / "icloud.json",
        "_DEFAULT_ICLOUD_EMAIL_JSON": root / "icloud.json",
        "_ICLOUD_EMAIL_TXT": root / "icloud.txt",
        "_ACCOUNTS_JSON": root / "accounts.json",
        "_DEFAULT_ACCOUNTS_JSON": root / "accounts.json",
        "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
        "_ACCOUNTS_TXT": root / "accounts.txt",
        "_TOKENS_TXT": root / "tokens.txt",
        "_JOBS_JSON": root / "jobs.json",
        "_DEFAULT_JOBS_JSON": root / "jobs.json",
        "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
        "_REGISTRATION_BATCHES_JSON": root / "registration-batches.json",
        "_DEFAULT_REGISTRATION_BATCHES_JSON": root / "registration-batches.json",
        "_DOMAIN_EMAIL_JSON": root / "domain.json",
        "_DEFAULT_DOMAIN_EMAIL_JSON": root / "domain.json",
        "_GROUPS_JSON": root / "groups.json",
        "_DEFAULT_GROUPS_JSON": root / "groups.json",
        "_CODEX_EXPORT_STATE": root / "codex-state.json",
        "_DEFAULT_CODEX_EXPORT_STATE": root / "codex-state.json",
        "_VIEWER_HTML": root / "viewer.html",
        "_SQLITE_READY_PATH": None,
        "_SQLITE_STORE_INSTANCE": None,
    }
    return patch.multiple(db, **paths)


def test_db_bootstraps_sqlite_once_and_keeps_json_mirror(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "sqlite")
    accounts = [{"id": 11, "email": "old@example.com", "note": "原数据"}]
    (tmp_path / "accounts.json").write_text(
        json.dumps(accounts, ensure_ascii=False),
        encoding="utf-8",
    )

    with _sqlite_db_patch(tmp_path):
        loaded = db._load_accounts()
        assert loaded[0]["id"] == accounts[0]["id"]
        assert loaded[0]["email"] == accounts[0]["email"]
        assert loaded[0]["note"] == accounts[0]["note"]
        assert loaded[0]["group_name"] == db.DEFAULT_ACCOUNT_GROUP
        (tmp_path / "accounts.json").write_text("[]", encoding="utf-8")
        assert db._load_accounts()[0]["email"] == accounts[0]["email"]

        updated = [{**accounts[0], "note": "SQLite 更新"}]
        db._save_accounts(updated)

        assert db._load_accounts()[0]["note"] == "SQLite 更新"
        mirrored = json.loads((tmp_path / "accounts.json").read_text(encoding="utf-8"))
        assert mirrored[0]["note"] == "SQLite 更新"
        assert db.storage_paths()["backend"] == "sqlite"


def test_concurrent_icloud_claims_do_not_return_same_record(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "sqlite")
    rows = [
        {"id": 1, "email": "one@example.com", "status": "available"},
        {"id": 2, "email": "two@example.com", "status": "available"},
    ]
    (tmp_path / "icloud.json").write_text(json.dumps(rows), encoding="utf-8")

    with _sqlite_db_patch(tmp_path):
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda _: db.claim_next_icloud_email(), range(2)))

        assert {row["email"] for row in claimed} == {"one@example.com", "two@example.com"}
        assert db.icloud_email_pool_summary()["available"] == 0


def test_json_backend_remains_available_for_rollback(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "json")
    source = [{"id": 3, "email": "json@example.com"}]
    (tmp_path / "accounts.json").write_text(json.dumps(source), encoding="utf-8")

    with _sqlite_db_patch(tmp_path):
        assert db._load_accounts() == source
        assert not (tmp_path / "data" / "registration.sqlite3").exists()
        assert db.storage_paths()["backend"] == "json"


def test_account_groups_default_assignment_move_rename_and_delete_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "sqlite")
    source = [
        {"id": 1, "email": "one@example.com"},
        {"id": 2, "email": "two@example.com", "group_name": "历史组"},
    ]
    (tmp_path / "accounts.json").write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")

    with _sqlite_db_patch(tmp_path):
        groups = db.list_account_groups()
        assert [(item["name"], item["count"]) for item in groups] == [
            ("全部", 2), ("默认分组", 1), ("历史组", 1)
        ]
        created = db.create_account_group("空组")
        assert created["count"] == 0
        updated, skipped = db.move_accounts_to_group([1, 999], created["id"])
        assert [item["id"] for item in updated] == [1]
        assert skipped == [{"id": 999, "reason": "账号不存在"}]
        assert db.list_accounts(group_filter="空组")[0]["id"] == 1

        renamed = db.rename_account_group(created["id"], "新组")
        assert renamed["name"] == "新组"
        assert {item["id"] for item in db.list_accounts(group_filter=json.dumps(["新组", "历史组"], ensure_ascii=False))} == {1, 2}
        with pytest.raises(ValueError, match="有账号"):
            db.delete_account_group(created["id"])
        db.move_accounts_to_group([1], 1)
        assert db.delete_account_group(created["id"])["name"] == "新组"
        with pytest.raises(ValueError, match="默认分组"):
            db.rename_account_group(1, "不允许")
        with pytest.raises(ValueError, match="默认分组"):
            db.delete_account_group(1)
