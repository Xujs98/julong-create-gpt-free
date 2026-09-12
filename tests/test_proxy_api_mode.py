# -*- coding: utf-8 -*-
import platform
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import config.proxy as proxy_config
from core.registration_service import _quarantine_browser_challenged_proxy, _select_registration_proxy
from webui.app import create_app
from webui import config_editor


@pytest.fixture(autouse=True)
def isolated_proxy_settings(monkeypatch):
    for key, value in {
        "PROXY_MODE": "pool", "PROXY_API_REGION": "Rand",
        "PROXY_API_NUM": 1, "PROXY_API_SESSION_TYPE": "sticky",
        "PROXY_API_MAX_ATTEMPTS": 3,
        "PROXY_HEALTH_CHECK_BEFORE_REGISTRATION": False,
    }.items():
        monkeypatch.setattr(proxy_config, key, value)


def test_proxy_api_request_url_uses_configured_parameters(monkeypatch):
    monkeypatch.setattr(proxy_config, "PROXY_API_URL", "https://api.example/white/api?region=Rand&num=9&time=1&format=n&type=txt&token=x")
    monkeypatch.setattr(proxy_config, "PROXY_API_REGION", "JP")
    monkeypatch.setattr(proxy_config, "PROXY_API_NUM", 2)
    monkeypatch.setattr(proxy_config, "PROXY_API_TIME", 30)
    monkeypatch.setattr(proxy_config, "PROXY_API_FORMAT", "rn")
    monkeypatch.setattr(proxy_config, "PROXY_API_TYPE", "json")

    url = proxy_config.build_proxy_api_request_url()
    assert url == "https://api.example/white/api?region=JP&num=2&time=30&format=rn&type=json&token=x"


def test_rotating_proxy_api_request_omits_duration(monkeypatch):
    monkeypatch.setattr(proxy_config, "PROXY_API_URL", "https://api.example/white/api?region=Rand&num=1&time=10&format=n&type=json")
    monkeypatch.setattr(proxy_config, "PROXY_API_SESSION_TYPE", "rotating")

    url = proxy_config.build_proxy_api_request_url()

    assert "time=" not in url
    assert "region=Rand" in url


def test_api_mode_fetches_a_new_proxy_and_does_not_read_pool(monkeypatch):
    monkeypatch.setattr(proxy_config, "PROXY_MODE", "api")
    monkeypatch.setattr(proxy_config, "PROXY_API_REGION", "US")
    monkeypatch.setattr(proxy_config, "PROXY_POOL", ["http://pool.example:1"])
    with patch("core.live_check_proxy.fetch_proxy_api", return_value=["http://api.example:2"]) as fetch:
        assert proxy_config.pick_proxy() == "http://api.example:2"
    fetch.assert_called_once()
    assert fetch.call_args.args == ("US",)


def test_pool_mode_keeps_existing_random_selection(monkeypatch):
    monkeypatch.setattr(proxy_config, "PROXY_MODE", "pool")
    monkeypatch.setattr(proxy_config, "PROXY_POOL", ["http://pool.example:1"])
    with patch("core.live_check_proxy.fetch_proxy_api") as fetch:
        assert proxy_config.pick_proxy() == "http://pool.example:1"
    fetch.assert_not_called()


def test_api_mode_health_switch_checks_each_dynamic_proxy(monkeypatch):
    monkeypatch.setattr(proxy_config, "PROXY_MODE", "api")
    monkeypatch.setattr(proxy_config, "PROXY_HEALTH_CHECK_BEFORE_REGISTRATION", True)
    pick = Mock(side_effect=[["http://api.example:8001"], ["http://api.example:8002"]])
    monkeypatch.setattr("core.live_check_proxy.fetch_proxy_api", pick)
    selection = {"ok": True, "proxy_url": "http://api.example:8002", "result": {}}
    logger = Mock()
    with patch("core.proxy_test.choose_healthy_proxy", side_effect=[{"ok": False, "result": {"reason": "dirty"}}, selection]) as health:
        result = _select_registration_proxy(7, logger, excluded_proxies=set())
    assert result == "http://api.example:8002"
    assert health.call_count == 2
    assert health.call_args_list[0].args == (["http://api.example:8001"],)


def test_api_mode_challenge_never_deletes_fixed_pool(monkeypatch):
    monkeypatch.setattr(proxy_config, "PROXY_MODE", "api")
    monkeypatch.setattr(proxy_config, "PROXY_DELETE_UNHEALTHY_IPS", True)
    excluded = set()
    with patch("core.registration_service._delete_proxies_from_pool") as delete:
        _quarantine_browser_challenged_proxy("http://api.example:2", excluded, 8, Mock())
    assert "http://api.example:2" in excluded
    delete.assert_not_called()


def test_api_mode_skips_fixed_pool_preflight(monkeypatch):
    app = create_app(auth_code="test-auth")
    client = app.test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
    monkeypatch.setattr(proxy_config, "PROXY_MODE", "api")
    monkeypatch.setattr(proxy_config, "PROXY_CHECK_BEFORE_REGISTRATION", True)
    with patch("webui.app.svc.submit_registration", return_value=[]), patch("core.proxy_test.test_proxy_pool") as test_pool:
        with patch("config.email.USE_EMAIL_SERVICE", False), patch("config.register.REGISTER_EMAIL", "user@example.test"):
            response = client.post("/api/jobs", json={"count": 1, "workers": 1})
    assert response.status_code == 200
    assert response.get_json()["proxy_check"]["skipped"] is True
    test_pool.assert_not_called()


@pytest.mark.parametrize("attempts", [1, 2, 5, 20])
@pytest.mark.parametrize("batch_size", [1, 20])
def test_api_health_attempt_budget_is_independent_of_batch_size(monkeypatch, attempts, batch_size):
    monkeypatch.setattr(proxy_config, "PROXY_MODE", "api")
    monkeypatch.setattr(proxy_config, "PROXY_API_MAX_ATTEMPTS", attempts)
    monkeypatch.setattr(proxy_config, "PROXY_API_NUM", batch_size)
    monkeypatch.setattr(proxy_config, "PROXY_HEALTH_CHECK_BEFORE_REGISTRATION", True)
    proxies = [f"http://api.example:{8000 + index}" for index in range(batch_size)]
    failure = {"ok": False, "result": None, "checked": [{"reason": "exit_unstable"}]}
    messages = []
    with patch("core.live_check_proxy.fetch_proxy_api", return_value=proxies) as fetch, patch(
        "core.proxy_test.choose_healthy_proxy", return_value=failure
    ) as health:
        with pytest.raises(RuntimeError, match=f"已尝试 {attempts}/{attempts} 次.*exit_unstable"):
            proxy_config.pick_proxy(log=messages.append)
    assert fetch.call_count == attempts
    assert health.call_count == attempts
    assert all(len(call.args[0]) == 1 for call in health.call_args_list)
    assert f"{attempts}/{attempts}" in messages[-1]
    assert "尝试次数已用尽" in messages[-1]


@pytest.mark.parametrize("budget,expected", [(0, 1), (-3, 1), (100, 20), (None, 3), ("invalid", 3)])
def test_api_attempt_budget_runtime_bounds(monkeypatch, budget, expected):
    monkeypatch.setattr(proxy_config, "PROXY_MODE", "api")
    monkeypatch.setattr(proxy_config, "PROXY_API_MAX_ATTEMPTS", budget)
    with patch("core.live_check_proxy.fetch_proxy_api", return_value=[]) as fetch:
        with pytest.raises(RuntimeError, match=f"已尝试 {expected}/{expected} 次"):
            proxy_config.pick_proxy()
    assert fetch.call_count == expected


def test_api_retries_acquisition_empty_and_health_exception_then_succeeds(monkeypatch):
    monkeypatch.setattr(proxy_config, "PROXY_MODE", "api")
    monkeypatch.setattr(proxy_config, "PROXY_API_MAX_ATTEMPTS", 5)
    monkeypatch.setattr(proxy_config, "PROXY_HEALTH_CHECK_BEFORE_REGISTRATION", True)
    good = "http://api.example:8002"
    messages = []
    with patch("core.live_check_proxy.fetch_proxy_api", side_effect=[
        TimeoutError("https://api.example?token=private-token"), [], [good], [good]
    ]) as fetch, patch("core.proxy_test.choose_healthy_proxy", side_effect=[
        TimeoutError("private-password"), {"ok": True, "proxy_url": good}
    ]) as health:
        assert proxy_config.pick_proxy(log=messages.append) == good
    assert fetch.call_count == 4
    assert health.call_count == 2
    assert "4/5：健康检查通过" in messages[-1]
    assert "private-token" not in " ".join(messages)
    assert "private-password" not in " ".join(messages)


def test_api_skips_excluded_proxy_without_checking_or_mutating_pool(monkeypatch):
    monkeypatch.setattr(proxy_config, "PROXY_MODE", "api")
    monkeypatch.setattr(proxy_config, "PROXY_HEALTH_CHECK_BEFORE_REGISTRATION", True)
    excluded = {"socks5://api.example:8001"}
    good = "http://api.example:8002"
    pool = list(proxy_config.PROXY_POOL)
    with patch("core.live_check_proxy.fetch_proxy_api", side_effect=[
        ["socks5h://api.example:8001"], [good]
    ]) as fetch, patch("core.proxy_test.choose_healthy_proxy", return_value={"ok": True, "proxy_url": good}) as health:
        assert _select_registration_proxy(8, Mock(), excluded_proxies=excluded) == good
    assert fetch.call_count == 2
    health.assert_called_once()
    assert health.call_args.args == ([good],)
    assert excluded == {"socks5://api.example:8001"}
    assert proxy_config.PROXY_POOL == pool


def test_registration_has_one_api_attempt_budget_and_job_logs(monkeypatch):
    monkeypatch.setattr(proxy_config, "PROXY_MODE", "api")
    monkeypatch.setattr(proxy_config, "PROXY_API_MAX_ATTEMPTS", 2)
    log = Mock()
    with patch("core.live_check_proxy.fetch_proxy_api", return_value=[]) as fetch:
        with pytest.raises(RuntimeError, match="已尝试 2/2 次"):
            _select_registration_proxy(42, log)
    assert fetch.call_count == 2
    assert all(call.args[0] == "[Job %s] %s" and call.args[1] == 42 for call in log.info.call_args_list)


@pytest.mark.parametrize("system,machine,container", [
    ("Darwin", "x86_64", False), ("Darwin", "arm64", False),
    ("Linux", "aarch64", True), ("Linux", "x86_64", True),
    ("Windows", "AMD64", False), ("Windows", "ARM64", False),
])
def test_api_settings_persist_reload_and_apply_across_platforms(monkeypatch, tmp_path, system, machine, container):
    from config import env_loader
    from core.chatgpt_plan import resolve_plan_check_route
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setenv("CONTAINER", "docker" if container else "")
    monkeypatch.setattr(env_loader, "_ENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("PROXY_API_MAX_ATTEMPTS", "3")
    monkeypatch.setattr(proxy_config, "PROXY_MODE", "api")
    monkeypatch.setattr(proxy_config, "PLAN_CHECK_PROXY_MODE", "proxy")
    monkeypatch.setattr(proxy_config, "PLAN_CHECK_PROXY", "")
    monkeypatch.setattr(proxy_config, "PROXY_HEALTH_CHECK_BEFORE_REGISTRATION", True)
    response = config_editor.update_config({"PROXY_API_MAX_ATTEMPTS": 2})
    assert "PROXY_API_MAX_ATTEMPTS" in response["updated"]
    namespace = {"PROXY_API_MAX_ATTEMPTS": 3}
    env_loader.apply_env_overrides(namespace, {"PROXY_API_MAX_ATTEMPTS": "int"})
    assert namespace["PROXY_API_MAX_ATTEMPTS"] == 2
    monkeypatch.setattr(proxy_config, "PROXY_API_MAX_ATTEMPTS", namespace["PROXY_API_MAX_ATTEMPTS"])
    good = "http://api.example:8002"
    with patch("core.live_check_proxy.fetch_proxy_api", return_value=[good]) as fetch, patch(
        "core.proxy_test.choose_healthy_proxy", side_effect=[
            {"ok": False}, {"ok": True, "proxy_url": good}
        ]
    ) as health:
        assert resolve_plan_check_route()["proxy"] == good
    assert fetch.call_count == health.call_count == 2


@pytest.mark.parametrize("value", [0, -1, 21, 1.5, True, "abc", ""])
def test_api_attempt_setting_rejects_invalid_updates(value):
    with patch("config.env_loader.write_env_values") as write:
        with pytest.raises(ValueError, match="PROXY_API_MAX_ATTEMPTS"):
            config_editor.update_config({"PROXY_API_MAX_ATTEMPTS": value})
    write.assert_not_called()


def test_api_attempt_setting_is_in_api_panel_and_not_provider_url(monkeypatch):
    field = next(f for f in config_editor.EDITABLE_FIELDS if f["key"] == "PROXY_API_MAX_ATTEMPTS")
    assert (field["type"], field["min"], field["max"]) == ("int", 1, 20)
    template = (Path(__file__).resolve().parents[1] / "webui/templates/index.html").read_text()
    panel = template.split('function renderProxyPoolSectionV2(fields) {', 1)[1].split('function renderMixedConfigSectionV2', 1)[0]
    assert "健康检查尝试次数（含首次）" in panel
    assert "renderProxyApiFieldV2(attemptsField" in panel
    url = proxy_config.build_proxy_api_request_url()
    monkeypatch.setattr(proxy_config, "PROXY_API_MAX_ATTEMPTS", 10)
    assert proxy_config.build_proxy_api_request_url() == url
