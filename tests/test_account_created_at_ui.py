from pathlib import Path


TEMPLATE = Path(__file__).parents[1] / "webui" / "templates" / "index.html"


def test_account_created_at_uses_two_line_date_and_time_layout():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "function _accountCreatedAtCell(value)" in source
    assert 'class="acc-v2-created-date"' in source
    assert 'class="acc-v2-created-meta"' in source
    assert 'class="acc-v2-created-time"' in source
    assert "${_accountCreatedAtCell(r.created_at)}" in source


def test_account_created_at_relative_label_is_limited_to_seven_days():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "dayDiff === 0 ? '今天'" in source
    assert "dayDiff >= 1 && dayDiff <= 7" in source
    assert "`${dayDiff}天前`" in source
    assert "dayLabel ?" in source
