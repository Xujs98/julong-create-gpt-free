from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
TEMPLATES = (
    ROOT / "webui" / "templates" / "index.html",
    ROOT / "webui" / "templates" / "index_legacy.html",
)


@pytest.mark.parametrize("template", TEMPLATES)
def test_account_copy_toolbar_is_selected_only(template):
    source = template.read_text(encoding="utf-8")

    assert "选中复制" in source
    assert "只复制表格中已勾选账号的 Token" in source
    assert "只复制表格中已勾选账号的完整整行" in source
    assert "copyCurrentPageTokens" not in source
    assert "copyCurrentPageLines" not in source
    assert "当前页没有 Token" not in source
    assert "当前页没有账号" not in source


@pytest.mark.parametrize("template", TEMPLATES)
def test_selected_token_and_line_copy_use_selected_id_set(template):
    source = template.read_text(encoding="utf-8")
    token_fn = source.split("async function copySelectedAccountTokens() {", 1)[1].split("\n}", 1)[0]
    line_fn = source.split("async function copySelectedAccountLines() {", 1)[1].split("\n}", 1)[0]

    assert "Array.from(ACCOUNT_SELECTED).map(Number)" in token_fn
    assert "fetchAccountSecrets(ids, 'access_token')" in token_fn
    assert "Array.from(ACCOUNT_SELECTED).map(Number)" in line_fn
    assert "fetchAccountSecrets(ids, 'copy_line')" in line_fn


def test_modern_and_legacy_copy_buttons_follow_selection_disabled_state():
    modern = TEMPLATES[0].read_text(encoding="utf-8")
    legacy = TEMPLATES[1].read_text(encoding="utf-8")

    assert 'id="btnCopySelectedTokensV2" disabled' in modern
    assert "'btnCopySelectedTokensV2', 'btnCopySelectedLinesV2'" in modern
    assert "bind('btnCopySelectedTokensV2', copySelectedAccountTokens);" in modern
    assert 'id="btnCopySelectedTokens" disabled' in legacy
    assert "copySelectedTokenBtn.disabled = ACCOUNT_SELECTED.size === 0" in legacy
    assert "$('#btnCopySelectedTokens').addEventListener('click', copySelectedAccountTokens);" in legacy
