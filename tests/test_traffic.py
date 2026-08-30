from pathlib import Path
from unittest.mock import patch

from core.account_export import save_account_data
from core import db
from core.traffic import TrafficMeter, browser_performance_snapshot


class _Response:
    status_code = 200
    headers = {"content-type": "application/json"}
    content = b'{"ok":true}'
    history = []


def test_traffic_meter_counts_request_and_response_bytes():
    meter = TrafficMeter()
    meter.record_request("https://example.test/api", {"X-Test": "yes"}, {"data": "{}"})
    meter.record_response(_Response())
    result = meter.snapshot()

    assert result["request_count"] == 1
    assert result["response_count"] == 1
    assert result["request_bytes"] > 0
    assert result["response_bytes"] == len(_Response.content) + len("content-type: application/json\r\n".encode())
    assert result["total_bytes"] == result["request_bytes"] + result["response_bytes"]


def test_browser_performance_snapshot_uses_transfer_size():
    class Driver:
        def execute_script(self, _script):
            return [
                {"transferSize": 1200, "encodedBodySize": 900, "decodedBodySize": 2000},
                {"transferSize": 0, "encodedBodySize": 300, "decodedBodySize": 500},
            ]

    result = browser_performance_snapshot(Driver())
    assert result["total_bytes"] == 1500
    assert result["response_count"] == 2
    assert result["measurement"] == "transferSize"


def test_save_account_promotes_registration_traffic_to_top_level():
    patches = (
        patch("core.account_export._capture_proxy_geo", return_value={}),
        patch("core.account_export._append_batch_archive"),
        patch("core.db.insert_account", return_value=42),
        patch("config.register.OAICS_CHECK_AFTER_REGISTRATION", False),
        patch("core.plan_check_service.enqueue_account_plan_check"),
    )
    with patches[0], patches[1], patches[2] as insert, patches[3], patches[4]:
        row_id = save_account_data(
            "user@example.test",
            "TOKEN",
            extra={
                "registration_traffic": {
                    "total_bytes": 4096,
                    "request_bytes": 1024,
                    "response_bytes": 3072,
                    "source": "browser_session",
                }
            },
        )

    assert row_id == 42
    assert insert.call_args.kwargs["registration_traffic_bytes"] == 4096
    assert insert.call_args.kwargs["registration_traffic_source"] == "browser_session"


def test_db_persists_registration_traffic_and_compact_list_exposes_it(monkeypatch):
    accounts = []
    monkeypatch.setattr(db, "_load_accounts", lambda: accounts)
    monkeypatch.setattr(db, "_load_outlook", lambda: [])
    monkeypatch.setattr(db, "_load_icloud_emails", lambda: [])
    monkeypatch.setattr(db, "_save_accounts", lambda rows: None)
    monkeypatch.setattr(db, "_save_outlook", lambda rows: None)
    monkeypatch.setattr(db, "_save_icloud_emails", lambda rows: None)

    row_id = db.insert_account(
        email="traffic@example.test",
        access_token="TOKEN",
        registration_traffic_bytes=8192,
        registration_traffic_source="browser_performance",
    )

    assert row_id == 1
    assert accounts[0]["registration_traffic_bytes"] == 8192
    assert accounts[0]["registration_traffic_source"] == "browser_performance"


def test_created_time_template_displays_registration_traffic():
    source = Path(__file__).parents[1].joinpath("webui", "templates", "index.html").read_text(encoding="utf-8")
    assert "function _formatRegistrationTraffic(value)" in source
    assert "acc-v2-created-traffic" in source
    assert "r.registration_traffic_bytes" in source
