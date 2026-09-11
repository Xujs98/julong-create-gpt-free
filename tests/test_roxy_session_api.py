# -*- coding: utf-8 -*-
from unittest.mock import patch

from core.roxy_registration import (
    _fetch_chatgpt_session,
    _read_chatgpt_session_api_document,
    _session_api_first_enabled,
)


class _Driver:
    current_url = "https://chatgpt.com/auth/callback/openai"

    def set_page_load_timeout(self, _value):
        return None

    def set_script_timeout(self, _value):
        return None

    def get(self, url):
        self.current_url = url

    def execute_script(self, script):
        if "document.body" in script:
            return '{"accessToken":"token","user":{"id":"user"}}'
        return None


def test_session_api_first_only_applies_to_optimized_modes():
    with patch("config.traffic.REGISTRATION_TRAFFIC_MODE", "default"):
        assert _session_api_first_enabled() is False
    with patch("config.traffic.REGISTRATION_TRAFFIC_MODE", "throttle"), patch(
        "config.traffic.REGISTRATION_SESSION_API_FIRST", True
    ):
        assert _session_api_first_enabled() is True


def test_read_session_api_document_avoids_spa_navigation():
    driver = _Driver()
    with patch("core.roxy_registration._safe_get") as safe_get:
        result = _read_chatgpt_session_api_document(driver)

    assert result["accessToken"] == "token"
    safe_get.assert_called_once()
    assert safe_get.call_args.args[1].endswith("/api/auth/session")


def test_fetch_session_returns_from_api_first_path():
    driver = _Driver()
    with patch("core.roxy_registration._session_api_first_enabled", return_value=True), patch(
        "core.roxy_registration._read_chatgpt_session_api_document",
        return_value={"accessToken": "token"},
    ) as read_api, patch("core.roxy_registration._read_chatgpt_session_once") as read_spa:
        result = _fetch_chatgpt_session(driver, timeout=5, auto_jump_wait=3)

    assert result["accessToken"] == "token"
    read_api.assert_called_once()
    read_spa.assert_not_called()
