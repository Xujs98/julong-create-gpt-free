import inspect
from unittest.mock import patch

from core import codex_retry_service
from core.codex_oauth import run_codex_oauth


def test_codex_retry_settings_are_exposed_in_config_editor():
    from webui import config_editor

    keys = {item["key"] for item in config_editor.EDITABLE_FIELDS if item.get("file") == "codex.py"}
    assert {"CODEX_RETRY_FOLLOW_LIVE_CHECK", "CODEX_RETRY_DRIVER", "CODEX_RETRY_HEADLESS"} <= keys


def test_codex_oauth_accepts_retry_runtime_overrides():
    params = inspect.signature(run_codex_oauth).parameters
    assert "driver_override" in params
    assert "headless_override" in params


def test_retry_worker_passes_live_driver_and_headless_to_oauth():
    import config.codex as codex_cfg
    import config.live_check as live_cfg

    email = "retry@example.test"
    assert codex_retry_service.reserve(email)
    old = (
        codex_cfg.CODEX_RETRY_FOLLOW_LIVE_CHECK,
        codex_cfg.CODEX_RETRY_DRIVER,
        codex_cfg.CODEX_RETRY_HEADLESS,
        live_cfg.LIVE_CHECK_DRIVER,
    )
    try:
        codex_cfg.CODEX_RETRY_FOLLOW_LIVE_CHECK = True
        codex_cfg.CODEX_RETRY_DRIVER = "protocol"
        codex_cfg.CODEX_RETRY_HEADLESS = True
        live_cfg.LIVE_CHECK_DRIVER = "roxy"
        with patch("core.codex_retry_service.db.get_account_by_email", return_value={"email": email, "codex_status": "retrying"}), patch(
            "core.codex_retry_service.db.update_account_codex_status"
        ), patch("config.reload_all"), patch("core.codex_oauth.run_codex_oauth", return_value={"status": "success", "ok": True}) as oauth:
            result = codex_retry_service.run_worker(email, clear_log=False)
        assert result["ok"] is True
        assert oauth.call_args.kwargs["driver_override"] == "roxy"
        assert oauth.call_args.kwargs["headless_override"] is True
    finally:
        codex_cfg.CODEX_RETRY_FOLLOW_LIVE_CHECK, codex_cfg.CODEX_RETRY_DRIVER, codex_cfg.CODEX_RETRY_HEADLESS, live_cfg.LIVE_CHECK_DRIVER = old
        codex_retry_service.release(email)
