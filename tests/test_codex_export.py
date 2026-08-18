import base64
import json
from pathlib import Path
from unittest.mock import patch

from core.codex_export import build_cap_records, build_cockpit_tools_records, build_sub2api_payload
from webui.app import create_app


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "webui" / "templates" / "index.html"


def _jwt(payload):
    def enc(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none'})}.{enc(payload)}.sig"


def _credential():
    auth = {"chatgpt_account_id": "acct-1", "chatgpt_user_id": "user-1", "chatgpt_plan_type": "plus", "poid": "org-1"}
    return {
        "id_token": _jwt({"email": "user@example.test", "https://api.openai.com/auth": auth}),
        "access_token": _jwt({"client_id": "client-1", "https://api.openai.com/auth": auth}),
        "refresh_token": "refresh-token",
        "expired": "2030-01-01T00:00:00Z",
        "type": "codex",
    }


def test_codex_export_adapters_match_requested_shapes():
    credential = _credential()
    cockpit = build_cockpit_tools_records([credential])
    assert isinstance(cockpit, list)
    assert cockpit[0]["email"] == "user@example.test"
    assert cockpit[0]["account_id"] == "acct-1"

    sub2 = build_sub2api_payload([credential])
    assert sub2["type"] == "sub2api-data"
    assert len(sub2["accounts"]) == 1
    account = sub2["accounts"][0]
    assert account["platform"] == "openai"
    assert account["type"] == "oauth"
    assert account["credentials"]["client_id"] == "client-1"

    cap = build_cap_records([credential])
    assert isinstance(cap, list) and cap[0]["access_token"]


def test_codex_export_endpoint_prepares_selected_format_without_listing_tokens():
    client = create_app(auth_code="codex-export-test").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "codex-export-test"
    with patch("webui.app.db.get_account", return_value={"id": 7, "email": "user@example.test", "access_token": "local-access-token"}), patch(
        "webui.app.db.list_codex_accounts", return_value=[]
    ), patch("core.codex_oauth.list_cpa_codex_auth_files", side_effect=AssertionError("sub2api export must not read CPA auth-files")), patch(
        "core.codex_oauth.download_cpa_codex_auth_text", side_effect=AssertionError("sub2api export must not download CPA files")
    ):
        response = client.post("/api/accounts/export-codex", json={"account_ids": [7], "format": "sub2api", "prepare": True})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["format"] == "sub2api"
    assert payload["download_url"].startswith("/api/downloads/")
    downloaded = client.get(payload["download_url"])
    assert downloaded.status_code == 200
    body = downloaded.get_json()
    assert body["type"] == "sub2api-data"
    assert "access_token" in body["accounts"][0]["credentials"]


def test_codex_export_menu_has_three_formats_and_renamed_button():
    source = TEMPLATE.read_text(encoding="utf-8")
    assert 'id="btnDownloadSelectedCpaV2"' in source
    assert ">导出 ▾</button>" in source
    for fmt in ("cockpit", "sub2api", "cap"):
        assert f'data-codex-export-format="{fmt}"' in source
