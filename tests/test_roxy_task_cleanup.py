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
