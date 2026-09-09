from unittest.mock import Mock, patch

from core import roxy_registration
from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult
import core.roxybrowser_client as roxy_client_module


def test_docker_roxy_installs_same_safe_traffic_rules_as_native():
    driver = object()
    with patch("core.roxy_selenium._running_in_container", return_value=True), patch(
        "core.traffic_optimizer.install_selenium_network_optimization"
    ) as install:
        roxy_registration._install_registration_traffic_optimization(driver)

    install.assert_called_once_with(driver, label="Roxy")


def test_chatgpt_callback_stops_optional_spa_downloads_before_session_read():
    driver = type("Driver", (), {})()
    driver.execute_cdp_cmd = Mock()
    driver.execute_script = Mock()

    roxy_registration._stop_chatgpt_document_loading(driver)

    driver.execute_cdp_cmd.assert_called_once_with("Page.stopLoading", {})
    driver.execute_script.assert_called_once_with("window.stop();")


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


def test_docker_roxy_bridge_keeps_host_loopback_debugger(monkeypatch):
    client = RoxyBrowserClient()
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: {
        "code": 0,
        "data": {"dirId": "profile", "http": "127.0.0.1:61234", "coreVersion": "152"},
    })
    monkeypatch.setattr("core.roxybrowser_client._running_in_container", lambda: True)
    monkeypatch.setattr("core.roxybrowser_client.normalize_debugger_address", lambda value: "192.168.65.254:61234")
    monkeypatch.setattr(client, "_docker_webdriver_available", lambda _url: True)
    monkeypatch.setattr("core.roxybrowser_client._cfg.ROXY_DOCKER_WEBDRIVER_URL", "http://host.docker.internal:9515")
    monkeypatch.setattr("core.roxybrowser_client._cfg.ROXY_ONE_PROFILE_PER_ACCOUNT", False)
    opened = client.open_profile(profile_id="profile", headless=True)

    assert opened.docker_bridge is True
    assert opened.debugger_address == "127.0.0.1:61234"
    assert opened.webdriver_url == "http://host.docker.internal:9515"


def test_native_roxy_ignores_docker_bridge_configuration(monkeypatch):
    client = RoxyBrowserClient()
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: {
        "code": 0,
        "data": {"dirId": "profile", "http": "127.0.0.1:61234", "coreVersion": "152"},
    })
    monkeypatch.setattr("core.roxybrowser_client._running_in_container", lambda: False)
    monkeypatch.setattr("core.roxybrowser_client.normalize_debugger_address", lambda value: value)
    monkeypatch.setattr(client, "_docker_webdriver_available", lambda _url: True)
    monkeypatch.setattr("core.roxybrowser_client._cfg.ROXY_DOCKER_WEBDRIVER_URL", "http://host.docker.internal:9515")
    monkeypatch.setattr("core.roxybrowser_client._cfg.ROXY_ONE_PROFILE_PER_ACCOUNT", False)
    opened = client.open_profile(profile_id="profile", headless=True)

    assert opened.docker_bridge is False
    assert opened.debugger_address == "127.0.0.1:61234"
    assert opened.webdriver_url is None


def test_docker_webdriver_probe_is_cached_for_short_burst(monkeypatch):
    roxy_client_module._DOCKER_WEBDRIVER_PROBE_CACHE.clear()
    response = Mock(ok=True)
    response.json.return_value = {"value": {"ready": True}}
    with patch("core.roxybrowser_client.requests.get", return_value=response) as get:
        assert RoxyBrowserClient._docker_webdriver_available("http://bridge.test:9515") is True
        assert RoxyBrowserClient._docker_webdriver_available("http://bridge.test:9515") is True
    get.assert_called_once_with("http://bridge.test:9515/status", timeout=2)


def test_docker_bridge_driver_enables_performance_log_before_connecting():
    opened = RoxyOpenResult(
        "profile", {}, debugger_address="127.0.0.1:61234",
        webdriver_url="http://host.docker.internal:9515", docker_bridge=True,
    )
    driver = Mock()
    driver.get_log.return_value = []
    with patch("selenium.webdriver.remote.webdriver.WebDriver", return_value=driver) as remote, patch(
        "core.roxy_registration._apply_browser_automation_mask"
    ), patch("core.roxy_registration._install_registration_traffic_optimization"):
        result = roxy_registration._build_driver(opened)

    options = remote.call_args.kwargs["options"]
    assert options.capabilities["goog:loggingPrefs"] == {"performance": "ALL"}
    assert result._registration_traffic_meter.source == "selenium_performance"
    driver.get_log.assert_called_once_with("performance")


def test_native_driver_enables_performance_log_before_connecting():
    opened = RoxyOpenResult("profile", {}, debugger_address="127.0.0.1:61234")
    driver = Mock()
    driver.get_log.return_value = []
    with patch("selenium.webdriver.Chrome", return_value=driver) as chrome, patch(
        "core.roxy_selenium.resolve_chromedriver", return_value="/tmp/chromedriver"
    ), patch("core.roxy_registration._apply_browser_automation_mask"), patch(
        "core.roxy_registration._install_registration_traffic_optimization"
    ):
        result = roxy_registration._build_driver(opened)

    options = chrome.call_args.kwargs["options"]
    assert options.capabilities["goog:loggingPrefs"] == {"performance": "ALL"}
    assert options.experimental_options["debuggerAddress"] == "127.0.0.1:61234"
    assert result._registration_traffic_meter.source == "selenium_performance"
    driver.get_log.assert_called_once_with("performance")
