from pathlib import Path
import json
from unittest.mock import patch

from core import db
from webui.app import _account_secret_value, create_app


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "webui" / "templates" / "index.html"


def test_account_email_row_has_proxy_badge_and_icloud_url_action():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "proxy_country_code" in source
    assert "acc-v2-proxy-badge" in source
    assert ".acc-v2-email:hover .acc-v2-proxy-badge" in source
    assert "table-layout: fixed" in source
    assert ".accounts-table-v2 .col-email { width: 280px; min-width: 280px; }" in source
    assert "overflow: visible; text-overflow: clip; white-space: normal" in source
    assert "data-account-copy-secret=\"icloud_code_url\"" in source
    assert "acc-v2-icloud-url-copy" in source
    assert "iCloud 接码 URL 已复制" in source


def test_proxy_country_code_accepts_geo_and_proxy_pool_formats():
    assert db._account_proxy_country_code({"proxy_geo": {"country_code": "JP"}}) == "JP"
    nested = {
        "proxy_used": "socks5h://***:***@us.proxy.example:3000",
        "extra_json": '{"cloakbrowser": {"open_result": {"locale": {"geo": {"ip": "203.0.113.9", "country": "DE", "region": "Hesse", "city": "Frankfurt"}}}}}',
    }
    assert db._account_proxy_country_code(nested) == "DE"
    assert db._account_proxy_geo(nested)["city"] == "Frankfurt"
    assert db._country_code_from_value("region-US-sid-abc-t-5") == "US"
    assert db._country_code_from_value("socks5h://***:***@jp.proxy.example:3000") == "JP"
    assert db._country_code_from_value("uk.proxy.example:3000") == "GB"
    assert db._country_code_from_value("ab.proxy.example:3000") == ""


def test_icloud_url_secret_is_scoped_to_icloud_accounts():
    client = create_app(auth_code="email-badge-test").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "email-badge-test"
    account = {"id": 7, "email": "user@icloud.com", "email_source": "icloud"}
    with patch("webui.app.db.get_account", return_value=account), patch(
        "webui.app.db.get_icloud_email_by_email",
        return_value={"code_url": "https://icloud.example/code/user"},
    ) as lookup:
        response = client.get("/api/accounts/7/secret?field=icloud_code_url")
    assert response.status_code == 200
    assert response.get_json()["value"] == "https://icloud.example/code/user"
    lookup.assert_called_once_with("user@icloud.com")

    with patch("webui.app.db.get_account", return_value={"id": 7, "email": "user@example.com", "email_source": "outlook"}), patch(
        "webui.app.db.get_icloud_email_by_email"
    ) as lookup:
        response = client.get("/api/accounts/7/secret?field=icloud_code_url")
    assert response.status_code == 200
    assert response.get_json()["value"] == ""
    lookup.assert_not_called()


def test_account_token_cell_exposes_at_and_saved_session_copy_actions():
    source = TEMPLATE.read_text(encoding="utf-8")
    assert '<th class="col-token">AT / Session</th>' in source
    assert 'data-account-copy-secret="access_token"' in source
    assert 'data-account-copy-secret="session"' in source
    assert 'title="复制完整 access token"' in source
    assert 'title="复制已保存的完整 Session"' in source
    assert "AT 已复制" in source
    assert "Session 已复制" in source


def test_saved_session_secret_returns_compact_json():
    saved = {"accessToken": "AT", "cookies": [{"name": "sid", "value": "COOKIE"}]}
    value = _account_secret_value({"extra_json": json.dumps({"session": saved})}, "session")
    assert json.loads(value) == saved
