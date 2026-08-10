# -*- coding: utf-8 -*-
from pathlib import Path

from core import db
from webui.app import _account_registration_method, _compact_account_for_list


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "webui" / "templates" / "index.html"


def test_registration_method_is_persisted_and_exposed_in_compact_account(monkeypatch):
    accounts = []
    monkeypatch.setattr(db, "_load_accounts", lambda: accounts)
    monkeypatch.setattr(db, "_load_outlook", lambda: [])
    monkeypatch.setattr(db, "_load_icloud_emails", lambda: [])
    monkeypatch.setattr(db, "_save_accounts", lambda rows: None)
    monkeypatch.setattr(db, "_save_outlook", lambda rows: None)
    monkeypatch.setattr(db, "_save_icloud_emails", lambda rows: None)

    row_id = db.insert_account(
        email="cloak@example.test",
        access_token="ACCESS_TOKEN",
        registration_method="cloak",
    )

    assert row_id == 1
    assert accounts[0]["registration_method"] == "cloak"
    compact = _compact_account_for_list(accounts[0])
    assert compact["registration_method"] == "cloak"


def test_registration_method_infers_historical_driver_markers():
    assert _account_registration_method({"extra_json": '{"roxybrowser": {}}'}) == "roxy"
    assert _account_registration_method({"extra_json": '{"cloakbrowser": {}}'}) == "cloak"
    assert _account_registration_method({"extra_json": '{"skyvern": {}}'}) == "skyvern"
    assert _account_registration_method({"extra_json": '{"browser_use": {}}'}) == "browser_use"
    assert _account_registration_method({}) == "protocol"


def test_accounts_template_contains_colored_registration_method_column():
    source = TEMPLATE.read_text(encoding="utf-8")
    assert 'data-account-column="registration"' in source
    assert '<th class="col-registration">注册方式</th>' in source
    assert 'class="col-registration">${_accountRegistrationMethodCell(r)}</td>' in source
    for cls in ("is-protocol", "is-roxy", "is-cloak", "is-browser-use", "is-skyvern"):
        assert f"acc-v2-registration-pill.{cls}" in source
