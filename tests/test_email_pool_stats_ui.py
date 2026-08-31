# -*- coding: utf-8 -*-
"""邮箱池统计卡片的 API 与模板契约测试。"""

from unittest.mock import patch

from webui.app import create_app


def _client():
    client = create_app(auth_code="test-auth").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
    return client


def test_summary_exposes_aggregate_aliases_and_source_breakdown():
    client = _client()
    common = {"total": 4, "available": 2, "used": 1, "failed": 1}
    with patch("webui.app.db.count_accounts", return_value=12), patch(
        "webui.app.db.outlook_pool_summary", return_value=common
    ), patch(
        "webui.app.db.generic_api_email_pool_summary", return_value={"total": 3, "available": 3, "used": 0, "failed": 0}
    ), patch(
        "webui.app.db.icloud_email_pool_summary", return_value={"total": 2, "available": 1, "used": 1, "failed": 0}
    ), patch(
        "webui.app.db.domain_email_pool_summary", return_value={"total": 1, "available": 0, "used": 1, "failed": 0, "missing_url": 1}
    ):
        response = client.get("/api/summary")

    assert response.status_code == 200
    body = response.get_json()
    assert body["email_pool_total"] == 10
    assert body["email_pool_available"] == 6
    assert body["email_pool_used"] == 3
    assert body["email_pool_failed"] == 1
    assert body["email_pool_missing_url"] == 1
    assert body["pool_by_source"]["cloudflare_domain"]["total"] == 1


def test_commercial_stats_markup_contains_register_breakdown_and_pool_cards():
    html = _client().get("/").get_data(as_text=True)
    assert 'id="statAvailableBreakdown"' in html
    assert 'id="outlookPoolStatsV2"' in html
    assert "POOL_SOURCE_META" in html
    assert "renderPoolStats(s)" in html
    for source in ("outlook", "generic_api", "icloud", "cloudflare_domain"):
        assert f"id: '{source}'" in html
