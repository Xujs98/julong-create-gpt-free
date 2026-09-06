# -*- coding: utf-8 -*-
from unittest.mock import Mock, patch

import config.proxy as proxy_config
from core.registration_service import _quarantine_browser_challenged_proxy, _select_registration_proxy
from webui.app import create_app


def test_proxy_api_request_url_uses_configured_parameters(monkeypatch):
    monkeypatch.setattr(proxy_config, "PROXY_API_URL", "https://api.example/white/api?region=Rand&num=9&time=1&format=n&type=txt&token=x")
    monkeypatch.setattr(proxy_config, "PROXY_API_REGION", "JP")
    monkeypatch.setattr(proxy_config, "PROXY_API_NUM", 2)
    monkeypatch.setattr(proxy_config, "PROXY_API_TIME", 30)
    monkeypatch.setattr(proxy_config, "PROXY_API_FORMAT", "rn")
    monkeypatch.setattr(proxy_config, "PROXY_API_TYPE", "json")

    url = proxy_config.build_proxy_api_request_url()
    assert url == "https://api.example/white/api?region=JP&num=2&time=30&format=rn&type=json&token=x"


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
    pick = Mock(side_effect=["http://api.example:bad", "http://api.example:good"])
    monkeypatch.setattr(proxy_config, "pick_proxy", pick)
    selection = {"ok": True, "proxy_url": "http://api.example:good", "result": {}}
    logger = Mock()
    with patch("core.proxy_test.choose_healthy_proxy", side_effect=[{"ok": False, "result": {"reason": "dirty"}}, selection]) as health:
        result = _select_registration_proxy(7, logger, excluded_proxies=set())
    assert result == "http://api.example:good"
    assert health.call_count == 2
    assert health.call_args_list[0].args == (["http://api.example:bad"],)


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
