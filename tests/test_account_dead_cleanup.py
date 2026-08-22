from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


TEMPLATE = Path(__file__).parents[1] / "webui" / "templates" / "index.html"


def test_delete_deactivated_accounts_only_removes_dead_rows(monkeypatch):
    accounts = [
        {"id": 1, "email": "dead-1@example.test", "codex_status": "deactivated"},
        {"id": 2, "email": "live@example.test", "codex_status": "success"},
        {"id": 3, "email": "dead-2@example.test", "codex_status": "DEACTIVATED"},
    ]
    saved = []
    monkeypatch.setattr(db, "_load_accounts", lambda: accounts)
    monkeypatch.setattr(db, "_save_accounts", lambda rows: saved.append(list(rows)))

    deleted = db.delete_deactivated_accounts()

    assert deleted == [
        {"id": 1, "email": "dead-1@example.test"},
        {"id": 3, "email": "dead-2@example.test"},
    ]
    assert saved == [[{"id": 2, "email": "live@example.test", "codex_status": "success"}]]


def test_delete_deactivated_accounts_does_not_rewrite_when_empty(monkeypatch):
    monkeypatch.setattr(db, "_load_accounts", lambda: [{"id": 1, "codex_status": "success"}])
    saved = []
    monkeypatch.setattr(db, "_save_accounts", lambda rows: saved.append(rows))

    assert db.delete_deactivated_accounts() == []
    assert saved == []


def test_delete_deactivated_accounts_api_returns_deleted_count():
    client = create_app(auth_code="test-auth").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
    deleted = [
        {"id": 8, "email": "dead-1@example.test"},
        {"id": 9, "email": "dead-2@example.test"},
    ]

    with patch("webui.app.db.delete_deactivated_accounts", return_value=deleted) as cleanup:
        response = client.post("/api/accounts/delete-deactivated", json={})

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "deleted": deleted, "deleted_count": 2}
    cleanup.assert_called_once_with()


def test_accounts_ui_exposes_dead_cleanup_button_and_count_alert():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert 'id="btnDeleteDeadAccountsV2"' in source
    assert "async function deleteAllDeadAccounts()" in source
    assert "api('/api/accounts/delete-deactivated'" in source
    assert "已清理全部废号，一共有 ${r.deleted_count || 0} 个废号" in source
    assert "bind('btnDeleteDeadAccountsV2', deleteAllDeadAccounts)" in source
