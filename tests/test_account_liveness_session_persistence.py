# -*- coding: utf-8 -*-
import json
from pathlib import Path
from unittest.mock import patch

from core import db


def _storage(root: Path):
    """把账号存储切到临时目录，避免测试改动真实账号文件。"""
    return patch.multiple(
        db,
        _DATA_DIR=root,
        _ACCOUNTS_JSON=root / "accounts.json",
        _LEGACY_ACCOUNTS_JSON=root / "legacy_accounts.json",
        _ACCOUNTS_TXT=root / "accounts.txt",
        _TOKENS_TXT=root / "tokens.txt",
        _VIEWER_HTML=root / "viewer.html",
    )


def test_live_check_merges_new_session_without_overwriting_other_extra(tmp_path):
    """刷新 AT 后更新完整 Session，同时保留账号原有 Codex/浏览器元数据。"""
    accounts_path = tmp_path / "accounts.json"
    accounts_path.write_text(
        json.dumps([
            {
                "id": 1,
                "email": "user@example.com",
                "access_token": "OLD_TOKEN",
                "extra_json": json.dumps({
                    "codex": {"status": "success"},
                    "cloakbrowser": {"profile_id": "old-profile"},
                    "session": {"accessToken": "OLD_TOKEN", "cookies": [{"name": "old", "value": "1"}]},
                }),
            }
        ]),
        encoding="utf-8",
    )
    refreshed_session = {
        "accessToken": "NEW_TOKEN",
        "user": {"id": "user-1", "name": "User"},
        "account": {"planType": "free"},
        "cookies": [{"name": "sid", "value": "COOKIE", "domain": ".chatgpt.com", "path": "/"}],
    }

    with _storage(tmp_path):
        assert db.update_account_liveness(1, {
            "ok": True,
            "status": "live",
            "access_token": "NEW_TOKEN",
            "session": refreshed_session,
            "check_method": "password_totp",
        })
        row = db.get_account(1)

    extra = json.loads(row["extra_json"])
    assert row["access_token"] == "NEW_TOKEN"
    assert extra["session"] == refreshed_session
    assert extra["codex"] == {"status": "success"}
    assert extra["cloakbrowser"] == {"profile_id": "old-profile"}


def test_plan_status_snapshot_includes_null_error_to_clear_stale_ui(tmp_path):
    """套餐成功后轻量轮询仍返回 plan_check_error=null，前端才能清掉旧错误。"""
    accounts_path = tmp_path / "accounts.json"
    accounts_path.write_text(
        json.dumps([
            {
                "id": 1,
                "email": "plus@example.com",
                "access_token": "TOKEN",
                "plan_type": "plus",
                "current_plan_type": "plus",
                "plan_check_status": "success",
                "plan_check_ok": True,
                "plan_check_error": None,
            }
        ]),
        encoding="utf-8",
    )

    with _storage(tmp_path):
        snapshot = db.list_account_plan_check_statuses()

    item = snapshot["items"][0]
    assert item["plan_check_status"] == "success"
    assert "plan_check_error" in item
    assert item["plan_check_error"] is None
