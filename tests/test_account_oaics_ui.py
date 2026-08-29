from pathlib import Path


TEMPLATE = Path(__file__).parents[1] / "webui" / "templates" / "index.html"


def _source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_accounts_table_exposes_a_toggleable_qualification_column():
    source = _source()
    assert 'data-account-column="qualification"' in source
    assert '<col class="col-qualification">' in source
    assert '<th class="col-qualification">资格</th>' in source
    assert "is-col-hidden-qualification" in source


def test_qualification_cell_renders_country_state_and_query_icon():
    source = _source()
    assert "function _oaicsQualificationCell(r)" in source
    assert "OAICS_COUNTRY_LABELS" in source
    assert "JP: '日本'" in source
    assert 'data-oaics-check="${esc(r.id)}"' in source
    assert "accountV2Icons().search" in source
    assert "qualification-state" in source


def test_single_and_bulk_qualification_actions_are_wired():
    source = _source()
    assert "async function checkOneOaics" in source
    assert "'/api/accounts/check-oaics'" in source
    assert "checkSelectedOaics" in source
    assert "/api/accounts/check-oaics-bulk" in source
    assert '>资格查询</button>' in source
