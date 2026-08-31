# -*- coding: utf-8 -*-
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.live_check_proxy import build_proxy_api_url, fetch_proxy_api, parse_proxy_api_response
from core.live_check_service import _live_check_routes


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


def test_live_check_routes_follow_registration_api_pool_priority():
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
        live_cfg, "LIVE_CHECK_PROXY_API_ENABLED", True
    ), patch.object(live_cfg, "LIVE_CHECK_PROXY_API_URL", "https://api.example?region={region}"), patch(
        "core.live_check_service.fetch_proxy_api", return_value=["http://api.test:8100", "http://api.test:8101"]
    ) as fetch, patch("core.live_check_service.resolve_plan_check_route", return_value=pool_route):
        routes = _live_check_routes(account)

    assert [route["source"] for route in routes] == ["registration", "proxy_api", "proxy_api", "proxy_pool"]
    assert [route["proxy"] for route in routes] == [
        "http://registration.test:8000",
        "http://api.test:8100",
        "http://api.test:8101",
        "http://pool.test:9000",
    ]
    fetch.assert_called_once()
    assert fetch.call_args.args == ("JP",)


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
        live_cfg, "LIVE_CHECK_PROXY_API_ENABLED", False
    ), patch("core.live_check_service.resolve_plan_check_route", return_value=pool_route):
        routes = _live_check_routes({"proxy_used": "http://registration.test:8000", "proxy_country_code": "US"})
    assert [route["source"] for route in routes] == ["proxy_pool"]
