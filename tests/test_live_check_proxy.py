# -*- coding: utf-8 -*-
import json
from unittest.mock import Mock, patch

import pytest
import config.proxy as proxy_config
import config.live_check as live_cfg
from urllib.parse import parse_qs, urlsplit

from core.live_check_proxy import build_proxy_api_url, fetch_proxy_api, parse_proxy_api_response
from core.live_check_service import _live_check_routes


@pytest.fixture(autouse=True)
def isolated_api_health_config(monkeypatch):
    monkeypatch.setattr(proxy_config, "PROXY_HEALTH_CHECK_BEFORE_REGISTRATION", False)
    monkeypatch.setattr(proxy_config, "PROXY_API_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(proxy_config, "PROXY_MODE", "pool")
    monkeypatch.setattr(live_cfg, "LIVE_CHECK_PROXY_API_REGION", "account")


def test_build_proxy_api_url_replaces_region_query_and_placeholder():
    url = build_proxy_api_url(
        "https://api.example/white/api?region=Rand&num=2&time=10&format=n&type=json",
        "JP",
    )
    assert "region=JP" in url
    assert "Rand" not in url
    assert "num=2" in url


def test_parse_proxy_api_response_supports_text_and_json_shapes():
    text = parse_proxy_api_response("http://proxy-a.test:8080\nproxy-b.test:8081:user:pass")
    assert text == ["http://proxy-a.test:8080", "http://user:pass@proxy-b.test:8081"]

    payload = {"data": [{"host": "proxy-c.test", "port": 8082, "username": "u", "password": "p"}]}
    assert parse_proxy_api_response(payload) == ["http://u:p@proxy-c.test:8082"]

    assert parse_proxy_api_response(json.dumps({"proxies": ["http://proxy-d.test:8083"]})) == [
        "http://proxy-d.test:8083"
    ]


def test_fetch_proxy_api_raises_when_response_has_no_proxy():
    response = Mock()
    response.json.return_value = {"data": []}
    response.raise_for_status.return_value = None
    with patch("core.live_check_proxy.requests.get", return_value=response):
        try:
            fetch_proxy_api("US", api_url="https://api.example?region={region}")
        except ValueError as exc:
            assert "返回为空" in str(exc)
        else:
            raise AssertionError("expected ValueError")


def test_live_check_routes_follow_registration_then_shared_api_mode():
    import config.live_check as live_cfg

    account = {"proxy_used": "http://registration.test:8000", "proxy_country_code": "JP"}
    pool_route = {
        "proxy": "http://pool.test:9000",
        "proxy_mode": "auto",
        "network_route": "proxy",
        "proxy_used": "http://pool.test:9000",
        "proxy_fallback_reason": None,
    }
    with patch.object(live_cfg, "LIVE_CHECK_USE_REGISTRATION_PROXY", True), patch.object(
        proxy_config, "PROXY_MODE", "api"
    ), patch.object(proxy_config, "PROXY_API_URL", "https://api.example?region={region}"), patch(
        "core.live_check_proxy.fetch_proxy_api", return_value=["http://api.test:8100", "http://api.test:8101"]
    ) as fetch, patch("core.live_check_service.resolve_plan_check_route", return_value=pool_route) as fallback:
        routes = _live_check_routes(account)

    assert [route["source"] for route in routes] == ["registration", "proxy_api", "proxy_api"]
    assert [route["proxy"] for route in routes] == [
        "http://registration.test:8000",
        "http://api.test:8100",
        "http://api.test:8101",
    ]
    fetch.assert_called_once()
    assert fetch.call_args.args == ("JP",)
    fallback.assert_not_called()


def test_live_check_routes_skip_registration_proxy_when_disabled():
    import config.live_check as live_cfg

    pool_route = {
        "proxy": "http://pool.test:9000",
        "proxy_mode": "auto",
        "network_route": "proxy",
        "proxy_used": "http://pool.test:9000",
        "proxy_fallback_reason": None,
    }
    with patch.object(live_cfg, "LIVE_CHECK_USE_REGISTRATION_PROXY", False), patch.object(
        proxy_config, "PROXY_MODE", "pool"
    ), patch("core.live_check_service.resolve_plan_check_route", return_value=pool_route):
        routes = _live_check_routes({"proxy_used": "http://registration.test:8000", "proxy_country_code": "US"})
    assert [route["source"] for route in routes] == ["proxy_pool"]


@pytest.mark.parametrize("succeeds", [False, True])
def test_live_check_api_uses_shared_settings_without_second_attempt_budget(monkeypatch, succeeds):
    import config.live_check as live_cfg
    monkeypatch.setattr(live_cfg, "LIVE_CHECK_USE_REGISTRATION_PROXY", False)
    monkeypatch.setattr(proxy_config, "PROXY_MODE", "api")
    monkeypatch.setattr(proxy_config, "PROXY_API_URL", "https://shared.example?region=US&token=test")
    monkeypatch.setattr(proxy_config, "PROXY_API_REGION", "US")
    monkeypatch.setattr(proxy_config, "PROXY_API_TIMEOUT", 4)
    monkeypatch.setattr(proxy_config, "PROXY_API_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(proxy_config, "PROXY_HEALTH_CHECK_BEFORE_REGISTRATION", True)
    proxy = "http://api.test:8100"
    failure = {"ok": False, "checked": [{"reason": "exit_unstable"}]}
    final = {"ok": True, "proxy_url": proxy} if succeeds else failure
    pool_route = {"proxy": "http://pool.test:9000", "network_route": "proxy"}
    with patch("core.live_check_proxy.fetch_proxy_api", return_value=[proxy]) as fetch, patch(
        "core.proxy_test.choose_healthy_proxy", side_effect=[failure, final]
    ) as health, patch("core.live_check_service.resolve_plan_check_route", return_value=pool_route) as fallback:
        routes = _live_check_routes({"proxy_country_code": "JP"})
    assert fetch.call_count == health.call_count == 2
    fetch.assert_called_with("JP", api_url=proxy_config.build_proxy_api_request_url(region="JP"), timeout=4)
    assert "shared.example" in fetch.call_args.kwargs["api_url"]
    assert proxy_config.PROXY_API_REGION == "US"
    assert len(routes) == 1
    fallback.assert_not_called()
    if succeeds:
        assert routes[0]["proxy"] == proxy
    else:
        assert routes[0]["network_route"] == "api_failed"
        assert "2/2" in routes[0]["proxy_fallback_reason"]
        assert "exit_unstable" in routes[0]["proxy_fallback_reason"]


@pytest.mark.parametrize("region,expected", [("account", "JP"), ("GB", "GB"), ("Rand", "Rand")])
@pytest.mark.parametrize("session_type", ["sticky", "rotating"])
def test_live_check_only_overrides_region_in_shared_api(monkeypatch, region, expected, session_type):
    monkeypatch.setattr(live_cfg, "LIVE_CHECK_USE_REGISTRATION_PROXY", False)
    monkeypatch.setattr(live_cfg, "LIVE_CHECK_PROXY_API_REGION", region)
    monkeypatch.setattr(proxy_config, "PROXY_MODE", "api")
    monkeypatch.setattr(proxy_config, "PROXY_API_REGION", "US")
    monkeypatch.setattr(proxy_config, "PROXY_API_URL", "https://shared.example/api?token=test")
    monkeypatch.setattr(proxy_config, "PROXY_API_NUM", 4)
    monkeypatch.setattr(proxy_config, "PROXY_API_TIME", 30)
    monkeypatch.setattr(proxy_config, "PROXY_API_FORMAT", "rn")
    monkeypatch.setattr(proxy_config, "PROXY_API_TYPE", "txt")
    monkeypatch.setattr(proxy_config, "PROXY_API_TIMEOUT", 6)
    monkeypatch.setattr(proxy_config, "PROXY_API_SESSION_TYPE", session_type)
    with patch("core.live_check_proxy.fetch_proxy_api", return_value=["http://api.test:8100"]) as fetch:
        routes = _live_check_routes({"proxy_country_code": "JP"})
    assert len(routes) == 1
    assert fetch.call_args.args == (expected,)
    assert fetch.call_args.kwargs["timeout"] == 6
    query = parse_qs(urlsplit(fetch.call_args.kwargs["api_url"]).query)
    assert query == dict(region=[expected], num=["4"], format=["rn"], type=["txt"], token=["test"], **({"time": ["30"]} if session_type == "sticky" else {}))
    assert proxy_config.PROXY_API_REGION == "US"
    assert "region=US" in proxy_config.build_proxy_api_request_url()


def test_live_check_missing_account_region_skips_api_without_global_region_fallback(monkeypatch):
    monkeypatch.setattr(live_cfg, "LIVE_CHECK_USE_REGISTRATION_PROXY", False)
    monkeypatch.setattr(proxy_config, "PROXY_MODE", "api")
    with patch("core.live_check_proxy.fetch_proxy_api") as fetch, patch(
        "core.live_check_service.resolve_plan_check_route"
    ) as fallback:
        routes = _live_check_routes({})
    assert len(routes) == 1 and routes[0]["network_route"] == "api_skipped"
    fetch.assert_not_called()
    fallback.assert_not_called()
