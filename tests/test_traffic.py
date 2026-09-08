import json
from pathlib import Path
from unittest.mock import patch

from core.account_export import save_account_data
from core import db
from core.traffic import (
    BrowserTrafficMeter, TrafficMeter, browser_performance_snapshot,
    install_playwright_traffic_meter, install_selenium_traffic_meter, merge_snapshots,
)
from core.registration_service import _job_traffic_fields


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
    assert result["upload_bytes"] == result["request_bytes"]
    assert result["response_bytes"] == len(_Response.content) + len("content-type: application/json\r\n".encode())
    assert result["download_bytes"] == result["response_bytes"]
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
    assert result["upload_bytes"] == 0
    assert result["download_bytes"] == 1500
    assert result["confidence"] == "low"


class _PlaywrightContext:
    def __init__(self):
        self.pages = []
        self.handlers = {}

    def on(self, name, callback):
        self.handlers.setdefault(name, []).append(callback)


class _PlaywrightPage:
    def __init__(self):
        self.handlers = {}

    def on(self, name, callback):
        self.handlers.setdefault(name, []).append(callback)


class _PlaywrightRequest:
    method = "POST"
    url = "https://example.test/register"
    post_data = "{}"
    post_data_buffer = b"{}"

    def all_headers(self):
        return {"content-type": "application/json"}

    def sizes(self):
        return {
            "requestHeadersSize": 140, "requestBodySize": 2,
            "responseHeadersSize": 90, "responseBodySize": 800,
        }


def test_playwright_meter_records_upload_download_and_websocket_frames():
    context = _PlaywrightContext()
    page = _PlaywrightPage()
    context.pages.append(page)
    meter = install_playwright_traffic_meter(context, page)
    request = _PlaywrightRequest()

    context.handlers["request"][0](request)
    context.handlers["requestfinished"][0](request)
    meter._record_websocket_frame("abc", sent=True)
    meter._record_websocket_frame(b"xy", sent=False)
    result = browser_performance_snapshot(page)

    assert result["upload_bytes"] == 142 + 9
    assert result["download_bytes"] == 890 + 4
    assert result["total_bytes"] == result["upload_bytes"] + result["download_bytes"]
    assert result["request_count"] == result["response_count"] == 1
    assert result["scope"] == "browser_application_http_ws"
    assert result["confidence"] == "high"


def _performance_entry(method, params):
    return {"message": json.dumps({"message": {"method": method, "params": params}})}


def test_selenium_meter_records_request_response_redirect_and_websocket():
    meter = BrowserTrafficMeter("selenium_performance")
    first_request = {
        "requestId": "r1",
        "request": {"method": "POST", "url": "https://example.test/one", "headers": {"a": "b"}, "postData": "{}"},
    }
    second_request = {
        "requestId": "r1",
        "redirectResponse": {"headers": {"location": "/two"}, "encodedDataLength": 50},
        "request": {"method": "GET", "url": "https://example.test/two", "headers": {}},
    }
    meter.record_selenium_logs([
        _performance_entry("Network.requestWillBeSent", first_request),
        _performance_entry("Network.responseReceived", {"requestId": "r1", "response": {"headers": {"location": "/two"}}}),
        _performance_entry("Network.loadingFinished", {"requestId": "r1", "encodedDataLength": 50}),
        _performance_entry("Network.requestWillBeSent", second_request),
        _performance_entry("Network.responseReceived", {"requestId": "r1", "response": {"headers": {"content-type": "text/html"}}}),
        _performance_entry("Network.dataReceived", {"requestId": "r1", "encodedDataLength": 80}),
        _performance_entry("Network.loadingFinished", {"requestId": "r1", "encodedDataLength": 100}),
        _performance_entry("Network.webSocketFrameSent", {"requestId": "ws1", "response": {"opcode": 1, "payloadData": "abc"}}),
        _performance_entry("Network.webSocketFrameReceived", {"requestId": "ws1", "response": {"opcode": 1, "payloadData": "xy"}}),
    ])
    result = meter.snapshot()

    assert result["request_count"] == 2
    assert result["response_count"] == 2
    assert result["upload_bytes"] > 9
    assert result["download_bytes"] == 50 + 100 + 4
    assert result["total_bytes"] == result["upload_bytes"] + result["download_bytes"]
    assert result["confidence"] == "medium"


def test_selenium_install_drains_preexisting_performance_events():
    class Driver:
        def __init__(self):
            self.calls = 0

        def get_log(self, name):
            assert name == "performance"
            self.calls += 1
            return []

    driver = Driver()
    meter = install_selenium_traffic_meter(driver)
    assert driver.calls == 1
    assert driver._registration_traffic_meter is meter


def test_merge_snapshots_sums_all_attempts_and_keeps_directions():
    result = merge_snapshots(
        {"upload_bytes": 10, "download_bytes": 90, "total_bytes": 100, "source": "one", "confidence": "high"},
        {"request_bytes": 20, "response_bytes": 180, "total_bytes": 200, "source": "two", "confidence": "medium"},
    )
    assert result["upload_bytes"] == 30
    assert result["download_bytes"] == 270
    assert result["total_bytes"] == 300
    assert result["confidence"] == "medium"


def test_job_traffic_fields_preserves_full_measurement_metadata():
    fields = _job_traffic_fields({
        "registration_traffic": {
            "upload_bytes": 25, "download_bytes": 75, "total_bytes": 100,
            "request_count": 2, "response_count": 2, "measurement_errors": 1,
            "source": "playwright_network", "scope": "browser_application_http_ws",
            "confidence": "medium", "measurement": "network_request_response_sizes",
        }
    })

    assert fields == {
        "registration_upload_bytes": 25,
        "registration_download_bytes": 75,
        "registration_traffic_bytes": 100,
        "registration_traffic_request_count": 2,
        "registration_traffic_response_count": 2,
        "registration_traffic_measurement_errors": 1,
        "registration_traffic_source": "playwright_network",
        "registration_traffic_scope": "browser_application_http_ws",
        "registration_traffic_confidence": "medium",
        "registration_traffic_measurement": "network_request_response_sizes",
    }


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
                    "scope": "application_http",
                    "confidence": "high",
                }
            },
        )

    assert row_id == 42
    assert insert.call_args.kwargs["registration_traffic_bytes"] == 4096
    assert insert.call_args.kwargs["registration_upload_bytes"] == 1024
    assert insert.call_args.kwargs["registration_download_bytes"] == 3072
    assert insert.call_args.kwargs["registration_traffic_source"] == "browser_session"
    assert insert.call_args.kwargs["registration_traffic_scope"] == "application_http"


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
        registration_upload_bytes=1024,
        registration_download_bytes=7168,
        registration_traffic_source="browser_performance",
        registration_traffic_scope="browser_resource_timing",
        registration_traffic_confidence="low",
    )

    assert row_id == 1
    assert accounts[0]["registration_traffic_bytes"] == 8192
    assert accounts[0]["registration_upload_bytes"] == 1024
    assert accounts[0]["registration_download_bytes"] == 7168
    assert accounts[0]["registration_traffic_source"] == "browser_performance"


def test_created_time_template_displays_registration_traffic():
    source = Path(__file__).parents[1].joinpath("webui", "templates", "index.html").read_text(encoding="utf-8")
    assert "function _formatRegistrationTraffic(value)" in source
    assert "acc-v2-created-traffic" in source
    assert "r.registration_traffic_bytes" in source
