from pathlib import Path


TEMPLATE = Path(__file__).parents[1] / "webui" / "templates" / "index.html"


def test_account_txt_export_modal_has_requested_fields_and_defaults():
    source = TEMPLATE.read_text(encoding="utf-8")
    assert 'id="accountExportModal"' in source
    for field in ("email", "password", "totp", "url", "access_token"):
        assert f'data-account-export-field="{field}"' in source
    assert 'data-account-export-field="url"><span>URL</span>' in source
    assert "api('/api/accounts/export-txt'" in source
    assert "至少选择一个导出字段" in source
    assert 'id="btnDownloadSelectedTxtV2"' in source


def test_account_txt_export_has_no_native_prompt_calls():
    source = TEMPLATE.read_text(encoding="utf-8")
    assert "window.prompt" not in source
    assert "window.confirm" not in source
    assert "window.alert" not in source


def test_account_password_is_an_on_demand_copy_action_in_email_cell():
    source = TEMPLATE.read_text(encoding="utf-8")
    assert "password_available" in source
    assert 'data-account-copy-secret="password"' in source
    assert "acc-v2-password-copy" in source
    assert "密码已复制" in source
    assert "未设置密码" in source
    assert "该账号没有保存 TOTP" in source
