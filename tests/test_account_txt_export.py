from unittest.mock import patch

from webui.app import _compact_account_for_list, create_app


def _client():
    client = create_app(auth_code="txt-export-test").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "txt-export-test"
    return client


def test_export_txt_assembles_selected_fields_in_stable_order():
    account = {
        "id": 4,
        "email": "person@icloud.com",
        "registration_password": "secret-pass",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "access_token": "at-value",
        "email_source": "icloud",
    }
    with patch("webui.app.db.get_account", return_value=account), patch(
        "webui.app.db.get_icloud_email_by_email", return_value={"code_url": "https://mail.example/code"}
    ):
        response = _client().post(
            "/api/accounts/export-txt",
            json={"account_ids": [4], "fields": ["url", "email", "access_token", "totp", "password"]},
        )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["fields"] == ["email", "password", "totp", "url", "access_token"]
    assert payload["lines"] == [
        "person@icloud.com----secret-pass----https://2fa.fb.tools/JBSWY3DPEHPK3PXP----https://mail.example/code----at-value"
    ]


def test_export_txt_keeps_empty_selected_columns_and_skips_missing_accounts():
    account = {"id": 9, "email": "person@example.com", "email_source": "outlook"}
    with patch("webui.app.db.get_account", side_effect=lambda value: account if int(value) == 9 else None):
        response = _client().post(
            "/api/accounts/export-txt",
            json={"account_ids": [9, 999], "fields": ["email", "url"]},
        )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["lines"] == ["person@example.com----"]
    assert payload["skipped"] == [{"id": 999, "reason": "账号不存在"}]


def test_export_txt_rejects_empty_field_selection():
    response = _client().post("/api/accounts/export-txt", json={"account_ids": [1], "fields": []})
    assert response.status_code == 400
    assert "至少选择一个导出字段" in response.get_json()["error"]


def test_account_list_only_exposes_password_availability_and_secret_endpoint_returns_value():
    row = {"id": 10, "email": "person@example.com", "extra_json": '{"registration_password":"pw-from-extra"}'}
    compact = _compact_account_for_list(row)
    assert compact["password_available"] is True
    assert "registration_password" not in compact
    with patch("webui.app.db.get_account", return_value=row):
        response = _client().get("/api/accounts/10/secret?field=password")
    assert response.status_code == 200
    assert response.get_json()["value"] == "pw-from-extra"
