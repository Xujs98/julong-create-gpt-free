# -*- coding: utf-8 -*-
from pathlib import Path
from unittest.mock import patch

from config import live_check
from core.roxybrowser_client import RoxyBrowserClient
from webui.config_editor import EDITABLE_FIELDS


ROOT = Path(__file__).parents[1]


def test_live_check_driver_config_is_independent_from_registration_browser_settings():
    """查活驱动和无头设置应使用独立配置键。"""
    source = (ROOT / "config" / "live_check.py").read_text(encoding="utf-8")
    fields = {item["key"]: item for item in EDITABLE_FIELDS}

    assert 'LIVE_CHECK_DRIVER: str = "cloak"' in source
    assert "LIVE_CHECK_HEADLESS: bool = False" in source
    assert fields["LIVE_CHECK_DRIVER"]["choices"] == ["cloak", "roxy", "protocol"]
    assert fields["LIVE_CHECK_DRIVER"]["choice_labels"]["cloak"] == "本地指纹浏览器（CloakBrowser）"
    assert fields["LIVE_CHECK_DRIVER"]["group"] == "账号查活"
    assert fields["LIVE_CHECK_HEADLESS"]["group"] == "账号查活"
    assert fields["LIVE_CHECK_DRIVER"]["file"] == "live_check.py"
    assert fields["LIVE_CHECK_HEADLESS"]["file"] == "live_check.py"
    assert isinstance(live_check.LIVE_CHECK_DRIVER, str)
    assert isinstance(live_check.LIVE_CHECK_HEADLESS, bool)


def test_live_check_proxy_api_settings_are_exposed_with_registration_priority():
    source = (ROOT / "config" / "live_check.py").read_text(encoding="utf-8")
    fields = {item["key"]: item for item in EDITABLE_FIELDS}

    assert "LIVE_CHECK_USE_REGISTRATION_PROXY: bool = True" in source
    assert "LIVE_CHECK_PROXY_API_ENABLED: bool = False" in source
    assert "{region}" in source
    assert "num=2" in source
    assert fields["LIVE_CHECK_USE_REGISTRATION_PROXY"]["type"] == "bool"
    assert fields["LIVE_CHECK_PROXY_API_ENABLED"]["type"] == "bool"
    assert fields["LIVE_CHECK_PROXY_API_URL"]["type"] == "str"
    assert fields["LIVE_CHECK_PROXY_API_TIMEOUT"]["type"] == "float"
    assert all(fields[key]["group"] == "账号查活" for key in (
        "LIVE_CHECK_USE_REGISTRATION_PROXY",
        "LIVE_CHECK_PROXY_API_ENABLED",
        "LIVE_CHECK_PROXY_API_URL",
        "LIVE_CHECK_PROXY_API_TIMEOUT",
    ))
    assert isinstance(live_check.LIVE_CHECK_USE_REGISTRATION_PROXY, bool)
    assert isinstance(live_check.LIVE_CHECK_PROXY_API_ENABLED, bool)


def test_roxy_api_and_open_timeouts_are_separate_editable_settings():
    source = (ROOT / "config" / "roxybrowser.py").read_text(encoding="utf-8")
    fields = {item["key"]: item for item in EDITABLE_FIELDS}
    assert "ROXY_API_TIMEOUT: int = 30" in source
    assert "ROXY_OPEN_TIMEOUT: int = 180" in source
    assert fields["ROXY_API_TIMEOUT"]["type"] == "int"
    assert fields["ROXY_OPEN_TIMEOUT"]["type"] == "int"
    assert fields["ROXY_API_TIMEOUT"]["group"] == "RoxyBrowser"
    assert fields["ROXY_OPEN_TIMEOUT"]["group"] == "RoxyBrowser"


def test_rebind_hybrid_driver_config_defaults_to_browser_login_protocol_action():
    source = (ROOT / "config" / "live_check.py").read_text(encoding="utf-8")
    fields = {item["key"]: item for item in EDITABLE_FIELDS}

    assert 'REBIND_LOGIN_DRIVER: str = "cloak"' in source
    assert 'REBIND_ACTION_DRIVER: str = "protocol"' in source
    assert "REBIND_HYBRID_MODE: bool = True" in source
    assert fields["REBIND_LOGIN_DRIVER"]["choices"] == ["cloak", "roxy", "protocol"]
    assert fields["REBIND_ACTION_DRIVER"]["choices"] == ["protocol", "cloak", "roxy"]
    assert fields["REBIND_HYBRID_MODE"]["type"] == "bool"
    assert live_check.REBIND_LOGIN_DRIVER in {"cloak", "roxy", "protocol"}
    assert live_check.REBIND_ACTION_DRIVER in {"cloak", "roxy", "protocol"}
    assert isinstance(live_check.REBIND_HYBRID_MODE, bool)


def test_roxy_open_headless_override_does_not_change_registration_default():
    """查活打开 Roxy 时可按本次参数覆盖无头，不写回注册配置。"""
    client = RoxyBrowserClient(api_base="http://127.0.0.1:50100", token="")
    response = {"data": {"debuggerAddress": "127.0.0.1:9222"}}
    with patch("core.roxybrowser_client._cfg.ROXY_OPEN_HEADLESS", False), patch(
        "core.roxybrowser_client._cfg.ROXY_ONE_PROFILE_PER_ACCOUNT", False
    ), patch.object(client, "request", return_value=response) as request:
        opened = client.open_profile(profile_id="123", headless=True)

    assert opened.profile_id == "123"
    body = request.call_args.kwargs.get("json_body") or request.call_args.kwargs.get("params")
    assert body["headless"] is True
    assert live_check.LIVE_CHECK_HEADLESS in {True, False}
