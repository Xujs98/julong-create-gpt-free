# -*- coding: utf-8 -*-
import gzip
import json
from unittest.mock import patch

import main
from config import openai_protocol
from webui.config_editor import EDITABLE_FIELDS
from core.chatgpt_bootstrap import browser_like_registration_bootstrap


class _Response:
    status_code = 200
    text = ""

    def __init__(self, url):
        self.url = url


class _BootstrapSession:
    device_id = "device-id"
    oai_session_id = "session-id"

    def __init__(self):
        self.calls = []

    def get_chatgpt_headers(self, referer=""):
        return {"referer": referer, "content-type": "application/json"}

    def get_chatgpt_navigate_headers(self, referer="", user_initiated=True):
        return {"referer": referer, "sec-fetch-user": "?1" if user_initiated else ""}

    def get_auth_navigate_headers(self, referer="", user_initiated=True):
        return {"referer": referer, "sec-fetch-user": "?1" if user_initiated else ""}

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return _Response(url)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _Response(url)


def test_browser_like_bootstrap_navigates_login_and_flushes_frontend_context():
    session = _BootstrapSession()
    browser_like_registration_bootstrap(session)

    urls = [call[1] for call in session.calls]
    assert urls[:3] == ["https://chatgpt.com/", "https://chatgpt.com/auth/login", "https://auth.openai.com/log-in"]
    assert any("/ces/v1/rgstr?" in url for url in urls)
    assert "https://chatgpt.com/ces/v1/projects/oai/settings" in urls

    method, url, kwargs = next(call for call in session.calls if call[0] == "POST")
    assert "/ces/v1/rgstr?" in url
    assert kwargs["headers"]["content-encoding"] == "gzip"
    assert kwargs["headers"]["statsig-event-count"] == "1"
    payload = json.loads(gzip.decompress(kwargs["data"]).decode("utf-8"))
    assert payload["events"][0]["eventName"] == "page_view"
    assert payload["events"][0]["value"] == "login"


def test_browser_like_bootstrap_is_best_effort_by_default():
    session = _BootstrapSession()
    session.get = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("preheat failed"))
    browser_like_registration_bootstrap(session)


def test_protocol_browser_like_switch_defaults_off_and_is_editable():
    assert openai_protocol.PROTOCOL_BROWSER_LIKE_FLOW is False
    field = next(item for item in EDITABLE_FIELDS if item["key"] == "PROTOCOL_BROWSER_LIKE_FLOW")
    assert field["group"] == "功能开关"
    assert "login_or_signup" in field["help"]


def test_protocol_signin_hint_preserves_legacy_and_web_like_branches():
    assert main._protocol_signin_screen_hint(False, False) is None
    assert main._protocol_signin_screen_hint(True, False) == "signup"
    assert main._protocol_signin_screen_hint(False, True) == "login_or_signup"
    assert main._protocol_signin_screen_hint(True, True) == "login_or_signup"


def test_protocol_browser_like_switch_reads_env_on_reload():
    from config import env_loader
    old_loaded = env_loader._LOADED
    with patch.dict("os.environ", {"PROTOCOL_BROWSER_LIKE_FLOW": "True"}, clear=False):
        with patch.object(env_loader, "load_env"):
            namespace = {"PROTOCOL_BROWSER_LIKE_FLOW": False}
            env_loader._LOADED = True
            env_loader.apply_env_overrides(namespace, {"PROTOCOL_BROWSER_LIKE_FLOW": "bool"})
    env_loader._LOADED = old_loaded
    assert namespace["PROTOCOL_BROWSER_LIKE_FLOW"] is True
