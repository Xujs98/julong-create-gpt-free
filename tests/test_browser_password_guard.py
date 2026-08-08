from unittest.mock import patch

from core import browser_use_registration, roxy_registration


def test_browser_use_password_guard_rejects_empty_password_when_enabled():
    with patch("config.register.ENABLE_CREATE_PASSWORD", True):
        try:
            browser_use_registration._require_password_if_enabled("", "user@example.com")
        except RuntimeError as exc:
            assert "未检测到创建密码页" in str(exc)
        else:
            raise AssertionError("BrowserUse must reject an empty password when enabled")


def test_roxy_password_guard_accepts_password_and_disabled_switch():
    with patch("config.register.ENABLE_CREATE_PASSWORD", True):
        roxy_registration._require_password_if_enabled("StrongPassword", "user@example.com")
    with patch("config.register.ENABLE_CREATE_PASSWORD", False):
        roxy_registration._require_password_if_enabled("", "user@example.com")
