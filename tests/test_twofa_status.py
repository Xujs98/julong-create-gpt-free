from core import db
from webui import app as web_app


def test_insert_account_persists_twofa_failure_without_secret(monkeypatch):
    accounts = []
    outlook = []
    icloud = []
    monkeypatch.setattr(db, "_load_accounts", lambda: accounts)
    monkeypatch.setattr(db, "_load_outlook", lambda: outlook)
    monkeypatch.setattr(db, "_load_icloud_emails", lambda: icloud)
    monkeypatch.setattr(db, "_save_accounts", lambda rows: None)
    monkeypatch.setattr(db, "_save_outlook", lambda rows: None)
    monkeypatch.setattr(db, "_save_icloud_emails", lambda rows: None)

    row_id = db.insert_account(
        email="user@example.test",
        access_token="ACCESS_TOKEN",
        extra={
            "twofa": {
                "requested": True,
                "status": "failed",
                "error": "TypeError: stale worker signature",
                "failure_code": "twofa_failed",
                "failure_stage": "request",
                "failure_status": 0,
                "attempts": 3,
            }
        },
    )

    assert row_id == 1
    assert accounts[0]["totp_secret"] is None
    assert accounts[0]["twofa_requested"] is True
    assert accounts[0]["twofa_status"] == "failed"
    assert accounts[0]["twofa_error"] == "TypeError: stale worker signature"
    assert accounts[0]["twofa_failure_code"] == "twofa_failed"
    assert accounts[0]["twofa_failure_stage"] == "request"
    assert accounts[0]["twofa_attempts"] == 3


def test_compact_account_exposes_twofa_failure_reason():
    row = web_app._compact_account_for_list(
        {
            "id": 7,
            "email": "user@example.test",
            "twofa_requested": True,
            "twofa_status": "failed",
            "twofa_error": "TypeError: stale worker signature",
        }
    )

    assert row["totp_enabled"] is False
    assert row["twofa_requested"] is True
    assert row["twofa_status"] == "failed"
    assert row["twofa_error"] == "TypeError: stale worker signature"
