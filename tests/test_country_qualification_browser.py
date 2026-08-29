from types import SimpleNamespace
from unittest.mock import patch

from core.country_qualification_browser import query_country_qualification_browser
from core import chatgpt_plan


class _Response:
    status = 200

    def __init__(self, payload):
        self._payload = payload
        self.request = SimpleNamespace(method="POST")
        self.url = "https://tools.oai9.com/api/trial/check"

    def json(self):
        return self._payload


class _ResponseContext:
    def __init__(self, response):
        self.value = response

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Locator:
    def __init__(self, page):
        self.page = page

    def fill(self, value):
        self.page.filled = value

    def click(self, **_kwargs):
        self.page.clicked = True


class _Page:
    def __init__(self, response):
        self.response = response
        self.filled = None
        self.clicked = False
        self.closed = False

    def goto(self, *_args, **_kwargs):
        return None

    def locator(self, _selector):
        return _Locator(self)

    def expect_response(self, _predicate, **_kwargs):
        return _ResponseContext(self.response)

    def close(self):
        self.closed = True


class _Browser:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page


def test_browser_relay_uses_official_page_and_parses_results():
    response = _Response({
        "query_count": 9,
        "results": [{
            "country": "JP",
            "country_name": "日本",
            "status": "eligible",
            "eligible": True,
            "message": "有资格",
        }],
    })
    page = _Page(response)
    browser = _Browser(page)
    with patch("core.country_qualification_browser._get_browser", return_value=browser):
        result = query_country_qualification_browser("TOKEN", timeout=10)

    assert result["country_qualification_eligible"] is True
    assert result["country_qualification_query_count"] == 9
    assert page.filled == "TOKEN"
    assert page.clicked is True
    assert page.closed is True


def test_plan_country_check_uses_browser_relay_without_local_token():
    env = SimpleNamespace(post=None)
    with patch.object(
        chatgpt_plan.webui_config,
        "COUNTRY_QUALIFICATION_BROWSER_RELAY_ENABLED",
        True,
    ), patch(
        "core.country_qualification_browser.query_country_qualification_browser",
        return_value={
            "country_qualification_results": [],
            "country_qualification_eligible": False,
            "country_qualification_query_count": 1,
        },
    ) as relay:
        result = chatgpt_plan._check_country_qualification(env, "TOKEN")

    relay.assert_called_once_with("TOKEN", timeout=45.0)
    assert result["country_qualification_status"] == "success"
    assert result["country_qualification_source"] == "tools.oai9.com/browser"
