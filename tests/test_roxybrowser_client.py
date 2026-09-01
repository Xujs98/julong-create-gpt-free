# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
import time
from unittest.mock import Mock, patch

import pytest

from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult


class _Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload
        self.text = ""

    def json(self):
        return self.payload


def test_profile_create_retries_only_roxy_busy_response_with_backoff():
    client = RoxyBrowserClient(api_base="http://roxy.test", token="")
    responses = iter([
        _Response({"code": 400, "msg": "正在创建中，请稍等！"}),
        _Response({"code": 0, "data": {"id": "PROFILE-2"}}),
    ])
    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return next(responses)

    client.http.request = request
    with patch("core.roxybrowser_client._cfg.ROXY_CREATE_RETRIES", 3), patch(
        "core.roxybrowser_client._cfg.ROXY_API_RETRY_DELAY", 0.5
    ), patch("core.roxybrowser_client.time.sleep") as sleep:
        result = client.request("POST", "/browser/create", json_body={"workspaceId": "W"})

    assert result["data"]["id"] == "PROFILE-2"
    assert len(calls) == 2
    sleep.assert_called_once_with(0.5)


def test_profile_create_requests_are_serialized_across_clients():
    client_a = RoxyBrowserClient(api_base="http://roxy.test", token="")
    client_b = RoxyBrowserClient(api_base="http://roxy.test", token="")
    active = 0
    overlap = False
    state_lock = threading.Lock()

    def request(*args, **kwargs):
        nonlocal active, overlap
        with state_lock:
            active += 1
            overlap = overlap or active > 1
        time.sleep(0.02)
        with state_lock:
            active -= 1
        return _Response({"code": 0, "data": {"id": "PROFILE"}})

    client_a.http.request = request
    client_b.http.request = request
    with patch("core.roxybrowser_client._cfg.ROXY_CREATE_RETRIES", 1):
        first = threading.Thread(target=client_a.request, args=("POST", "/browser/create"), kwargs={"json_body": {}})
        second = threading.Thread(target=client_b.request, args=("POST", "/browser/create"), kwargs={"json_body": {}})
        first.start()
        second.start()
        first.join(timeout=2)
        second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert overlap is False


def test_request_uses_longer_timeout_for_browser_open():
    client = RoxyBrowserClient(api_base="http://roxy.test", token="")
    calls = []

    def request(*args, **kwargs):
        calls.append(kwargs)
        return _Response({"code": 0, "data": {"debuggerAddress": "127.0.0.1:9222"}})

    client.http.request = request
    with patch("core.roxybrowser_client._cfg.ROXY_OPEN_TIMEOUT", 180), patch(
        "core.roxybrowser_client._cfg.ROXY_API_TIMEOUT", 12
    ):
        client.request("POST", "/browser/open", json_body={})
        client.request("GET", "/browser/workspace")

    assert [item["timeout"] for item in calls] == [180, 12]


def test_open_profile_cleans_created_profile_when_open_fails():
    client = RoxyBrowserClient(api_base="http://roxy.test", token="")
    client.create_profile = Mock(return_value="PROFILE")
    client.request = Mock(side_effect=RuntimeError("下载内核失败"))
    with patch.object(client, "cleanup_profile") as cleanup:
        with pytest.raises(RuntimeError, match="下载内核失败"):
            client.open_profile()

    cleanup.assert_called_once()
    opened = cleanup.call_args.args[0]
    assert opened.profile_id == "PROFILE"
    assert opened.created_by_run is True


def test_open_profile_cleans_created_profile_when_debugger_address_missing():
    client = RoxyBrowserClient(api_base="http://roxy.test", token="")
    client.create_profile = Mock(return_value="PROFILE")
    client.request = Mock(return_value={"code": 0, "data": {}})
    with patch.object(client, "cleanup_profile") as cleanup:
        with pytest.raises(RuntimeError, match="未返回 Selenium/调试地址"):
            client.open_profile()

    cleanup.assert_called_once()
    assert cleanup.call_args.kwargs == {"force": True}


def test_failed_cleanup_ignores_keep_open_and_deletes_created_profile():
    client = RoxyBrowserClient(api_base="http://roxy.test", token="")
    opened = RoxyOpenResult("PROFILE", {}, created_by_run=True)
    with patch("core.roxybrowser_client._cfg.ROXY_KEEP_BROWSER_OPEN", True), patch(
        "core.roxybrowser_client._cfg.ROXY_ONE_PROFILE_PER_ACCOUNT", False
    ), patch(
        "core.roxybrowser_client._cfg.ROXY_DELETE_PROFILE_AFTER_RUN", False
    ), patch.object(client, "close_profile") as close, patch.object(client, "delete_profile") as delete:
        client.cleanup_profile(opened, force=True)

    close.assert_called_once_with("PROFILE")
    delete.assert_called_once_with("PROFILE")


def test_success_cleanup_still_honors_keep_open_setting():
    client = RoxyBrowserClient(api_base="http://roxy.test", token="")
    opened = RoxyOpenResult("PROFILE", {}, created_by_run=True)
    with patch("core.roxybrowser_client._cfg.ROXY_KEEP_BROWSER_OPEN", True), patch.object(
        client, "close_profile"
    ) as close, patch.object(client, "delete_profile") as delete:
        client.cleanup_profile(opened, force=False)

    close.assert_not_called()
    delete.assert_not_called()
