# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
import time
from unittest.mock import patch

from core.roxybrowser_client import RoxyBrowserClient


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
