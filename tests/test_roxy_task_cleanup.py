# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch

from core.roxy_codex_oauth import _run_roxy_codex_oauth_once
from core.roxy_registration import run_roxy_registration
from core.roxybrowser_client import RoxyOpenResult


def test_roxy_registration_failure_forces_cleanup_even_when_keep_open_enabled():
    opened = RoxyOpenResult("PROFILE", {}, created_by_run=True)
    client = MagicMock()
    client.open_profile.return_value = opened

    with patch("core.roxy_registration.RoxyBrowserClient", return_value=client), patch(
        "core.roxy_registration._build_driver", side_effect=RuntimeError("driver failed")
    ), patch("core.email_provider.release_email"), patch(
        "core.roxy_registration._cfg.ROXY_KEEP_BROWSER_OPEN", True
    ):
        result = run_roxy_registration(
            "user@example.com",
            "Sample User",
            "1990-01-01",
            proxy="",
        )

    assert result["success"] is False
    client.cleanup_profile.assert_called_once_with(opened, force=True)


def test_roxy_codex_failure_forces_cleanup_even_when_keep_open_enabled():
    opened = RoxyOpenResult("PROFILE", {}, created_by_run=True)
    client = MagicMock()
    client.open_profile.return_value = opened

    with patch("core.roxy_codex_oauth.RoxyBrowserClient", return_value=client), patch(
        "core.roxy_codex_oauth._build_driver", side_effect=RuntimeError("driver failed")
    ), patch("core.roxy_codex_oauth._roxy_cfg.ROXY_KEEP_BROWSER_OPEN", True):
        result = _run_roxy_codex_oauth_once("user@example.com", force=True)

    assert result["status"] == "failed"
    client.cleanup_profile.assert_called_once_with(opened, force=True)


def test_persistent_registration_reuses_binding_and_refreshes_state():
    opened = RoxyOpenResult("PROFILE", {}, created_by_run=False)
    client = MagicMock()
    client.open_profile.return_value = opened
    client.last_proxy_url = "socks5h://user:pass@proxy.test:3010"
    binding = {"profile_id": "PROFILE", "status": "active"}

    with patch("core.roxy_registration.RoxyBrowserClient", return_value=client), patch(
        "core.roxy_registration._build_driver", side_effect=RuntimeError("driver failed")
    ), patch("core.roxy_registration._cfg.ROXY_PERSIST_PROFILE_PER_ACCOUNT", True), patch(
        "core.roxy_registration._cfg.ROXY_ONE_PROFILE_PER_ACCOUNT", True
    ), patch("core.roxy_registration._cfg.ROXY_KEEP_BROWSER_OPEN", False), patch(
        "core.roxy_registration._cfg.ROXY_WORKSPACE_ID", "WORKSPACE"
    ), patch("config.proxy.PROXY_MODE", "api"), patch(
        "core.db.get_roxy_profile_binding", return_value=binding
    ), patch("core.db.set_roxy_profile_binding", return_value=binding), patch(
        "core.email_provider.release_email"
    ):
        result = run_roxy_registration(
            "user@example.com", "Sample User", "1990-01-01", proxy="socks5h://user:pass@proxy.test:3010"
        )

    assert result["success"] is False
    client.create_profile.assert_not_called()
    client.open_profile.assert_called_once_with(proxy="socks5h://user:pass@proxy.test:3010", proxy_is_fresh=True, task_kind="registration")
    client.cleanup_profile.assert_called_once_with(opened, force=True)
