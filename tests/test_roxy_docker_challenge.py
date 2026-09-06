from unittest.mock import patch

from core import roxy_registration


def test_docker_roxy_keeps_cloudflare_resources_unblocked():
    driver = object()
    with patch("core.roxy_selenium._running_in_container", return_value=True), patch(
        "core.traffic_optimizer.install_selenium_network_optimization"
    ) as install:
        roxy_registration._install_registration_traffic_optimization(driver)

    install.assert_not_called()


def test_docker_roxy_email_submit_waits_before_authorize_fallback():
    with patch("core.roxy_registration._is_docker_roxy_session", return_value=True):
        assert roxy_registration._email_submit_recovery_delay(object()) == 25.0
        assert roxy_registration._email_submit_wait_timeout(object(), 20) == 45


def test_native_roxy_email_submit_timing_is_unchanged():
    with patch("core.roxy_registration._is_docker_roxy_session", return_value=False):
        assert roxy_registration._email_submit_recovery_delay(object()) == 2.0
        assert roxy_registration._email_submit_wait_timeout(object(), 20) == 20


def test_docker_roxy_authorize_fallback_uses_retryable_remote_navigation():
    driver = object()
    result = {
        "ok": True,
        "stage": "authorize_url",
        "status": 200,
        "url": "https://auth.openai.com/authorize?id=1",
    }
    with patch("core.roxy_registration._submit_email_via_browser_nextauth", return_value=result), patch(
        "core.roxy_registration._is_docker_roxy_session", return_value=True
    ), patch("core.roxy_registration._safe_get") as safe_get:
        diagnostic = roxy_registration._recover_email_authorize_once(driver, "user@example.test")

    assert diagnostic["ok"] is True
    safe_get.assert_called_once_with(
        driver,
        result["url"],
        timeout=45,
        attempts=2,
        accept_hosts=("auth.openai.com", "chatgpt.com"),
    )
