from pathlib import Path


TEMPLATE = Path(__file__).parents[1] / "webui" / "templates" / "index.html"


def test_account_table_exposes_link_payment_and_sms_statuses():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert '<th class="col-completion">状态</th>' in source
    assert 'data-account-completion-status="${kind}"' in source
    assert "is-link-on" in source
    assert "is-link-failed" in source
    assert "is-link-progress" in source
    assert "is-payment-on" in source
    assert "is-sms-on" in source
    assert "data-copy-extract-link" in source
    assert "/completion-status`" in source
    assert "await appConfirm(" in source


def test_manual_link_sync_is_removed():
    source = TEMPLATE.read_text(encoding="utf-8")
    assert 'id="btnManualLinkStatusV2"' not in source
    assert "/api/accounts/link-status/sync" not in source


def test_status_segment_forwards_server_side_filter_to_list_and_polling():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert 'data-account-status-filter=""' in source
    assert 'data-account-status-filter="link"' in source
    assert 'data-account-status-filter="payment"' in source
    assert 'data-account-status-filter="sms"' in source
    assert "let ACCOUNT_STATUS_FILTER = '';" in source
    assert source.count("status=${encodeURIComponent(status)}") == 2
    assert "applyAccountStatusFilter" in source


def test_account_controls_remain_usable_on_narrow_viewports():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "--sidebar-width: 64px" in source
    assert ".sidebar-item-label" in source
    assert "flex: 1 1 100%; width: 100%; min-width: 0" in source
