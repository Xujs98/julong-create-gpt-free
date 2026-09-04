# -*- coding: utf-8 -*-
from unittest.mock import Mock, patch

from core.openai_auth import network_preflight
from core.traffic import normalize_snapshot
from config.traffic import (
    effective_protocol_bootstrap_enabled,
    effective_protocol_browser_like_enabled,
    effective_protocol_preflight_mode,
    normalize_registration_traffic_mode,
)
from core.traffic_optimizer import (
    blocked_url_patterns,
    install_playwright_network_optimization,
    install_selenium_network_optimization,
    should_block_url,
)


def test_traffic_optimizer_blocks_optional_hosts_but_keeps_core_and_challenge_urls():
    with patch("config.traffic.REGISTRATION_TRAFFIC_MODE", "stable"):
        assert should_block_url("https://browser-intake-datadoghq.com/api/v2", resource_type="xhr")
        assert not should_block_url("https://ab.chatgpt.com/v1/initialize", resource_type="xhr")
        assert should_block_url("https://auth.openai.com/assets/logo.svg", resource_type="image")
        assert should_block_url("https://auth-cdn.oaistatic.com/assets/logo.webp", resource_type="image")
        assert not should_block_url("https://auth-cdn.oaistatic.com/assets/login.js", resource_type="script")
        assert not should_block_url("https://auth.openai.com/api/accounts/email-otp/validate", resource_type="xhr")
        assert not should_block_url("https://challenges.cloudflare.com/turnstile/v0/api.js", resource_type="script")
        assert blocked_url_patterns()

    with patch("config.traffic.REGISTRATION_TRAFFIC_MODE", "throttle"):
        assert should_block_url("https://ab.chatgpt.com/v1/initialize", resource_type="xhr")


def test_default_mode_keeps_original_browser_requests():
    driver = Mock()
    with patch("config.traffic.REGISTRATION_TRAFFIC_MODE", "default"):
        assert blocked_url_patterns() == []
        assert not should_block_url("https://browser-intake-datadoghq.com/api/v2", resource_type="xhr")
        handle = install_selenium_network_optimization(driver, label="Roxy")

    assert handle.enabled is False
    assert handle.method == "disabled"
    assert handle.mode == "default"
    driver.execute_cdp_cmd.assert_not_called()


def test_selenium_install_uses_cdp_and_keeps_cache_enabled():
    driver = Mock()
    with patch("config.traffic.REGISTRATION_TRAFFIC_MODE", "stable"), patch(
        "config.traffic.REGISTRATION_TRAFFIC_OPTIMIZATION", True
    ):
        handle = install_selenium_network_optimization(driver, label="Roxy")

    assert handle.enabled is True
    assert handle.method == "cdp"
    assert handle.mode == "stable"
    driver.execute_cdp_cmd.assert_any_call("Network.enable", {})
    driver.execute_cdp_cmd.assert_any_call("Network.setCacheDisabled", {"cacheDisabled": False})
    blocked = next(call for call in driver.execute_cdp_cmd.call_args_list if call.args[0] == "Network.setBlockedURLs")
    assert blocked.args[1]["urls"]


def test_playwright_install_covers_new_pages_without_route_interception():
    context = Mock()
    page = Mock()
    cdp = Mock()
    context.new_cdp_session.return_value = cdp

    with patch("config.traffic.REGISTRATION_TRAFFIC_MODE", "throttle"):
        handle = install_playwright_network_optimization(context, page, label="Cloak")

    assert handle.enabled is True
    assert handle.method == "cdp"
    assert handle.mode == "throttle"
    cdp.send.assert_any_call("Network.enable")
    cdp.send.assert_any_call("Network.setCacheDisabled", {"cacheDisabled": False})
    context.on.assert_called_once()
    assert getattr(page, "_registration_traffic_optimization") is handle


def test_optimizer_fail_open_when_cdp_is_unavailable():
    driver = Mock()
    driver.execute_cdp_cmd.side_effect = RuntimeError("cdp unavailable")

    with patch("config.traffic.REGISTRATION_TRAFFIC_MODE", "stable"):
        handle = install_selenium_network_optimization(driver)

    assert handle.enabled is False
    assert handle.method == "disabled"
    assert "cdp unavailable" in handle.error


def test_normalize_snapshot_preserves_only_safe_optimization_metadata():
    result = normalize_snapshot({
        "total_bytes": 10,
        "optimization": {
            "enabled": True,
            "method": "cdp",
            "mode": "stable",
            "label": "Cloak",
            "blocked_pattern_count": "4",
            "blocked_patterns": ["secret"],
        },
    })

    assert result["optimization"] == {
        "enabled": True,
        "method": "cdp",
        "mode": "stable",
        "label": "Cloak",
        "blocked_pattern_count": 4,
    }


class _PreflightSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Mock(status_code=200)

    def get_chatgpt_navigate_headers(self, **kwargs):
        return kwargs

    def get_auth_navigate_headers(self, **kwargs):
        return kwargs


def test_protocol_preflight_minimal_checks_one_endpoint():
    session = _PreflightSession()
    with patch("config.traffic.REGISTRATION_TRAFFIC_MODE", "default"), patch(
        "config.openai_protocol.PROTOCOL_PREFLIGHT_MODE", "minimal"
    ):
        network_preflight(session)
    assert [url for url, _ in session.calls] == ["https://chatgpt.com/login"]


def test_protocol_preflight_off_skips_all_requests():
    session = _PreflightSession()
    with patch("config.traffic.REGISTRATION_TRAFFIC_MODE", "default"), patch(
        "config.openai_protocol.PROTOCOL_PREFLIGHT_MODE", "off"
    ):
        network_preflight(session)
    assert session.calls == []


def test_stable_and_throttle_use_minimal_protocol_strategy():
    for mode in ("stable", "throttle"):
        session = _PreflightSession()
        with patch("config.traffic.REGISTRATION_TRAFFIC_MODE", mode), patch(
            "config.openai_protocol.PROTOCOL_PREFLIGHT_MODE", "full"
        ):
            network_preflight(session)
            assert effective_protocol_preflight_mode("off") == "minimal"
            assert effective_protocol_bootstrap_enabled(True) is False
            assert effective_protocol_browser_like_enabled(True) is False
        assert [url for url, _ in session.calls] == ["https://chatgpt.com/login"]


def test_registration_mode_normalization_and_default_protocol_passthrough():
    assert normalize_registration_traffic_mode("balanced") == "stable"
    assert normalize_registration_traffic_mode("low-traffic") == "throttle"
    assert normalize_registration_traffic_mode("unknown") == "default"
    with patch("config.traffic.REGISTRATION_TRAFFIC_MODE", "default"):
        assert effective_protocol_preflight_mode("full") == "full"
        assert effective_protocol_bootstrap_enabled(True) is True
        assert effective_protocol_browser_like_enabled(True) is True
