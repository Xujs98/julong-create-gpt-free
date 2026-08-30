from types import SimpleNamespace
from unittest.mock import patch

from core.country_qualification_browser import query_country_qualification_browser
from core import chatgpt_plan
import core.country_qualification_browser as country_browser


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


def test_plan_country_check_uses_checkout_qualification_engine():
    env = SimpleNamespace()
    with patch(
        "core.qualification_test.query_country_qualification",
        return_value={
            "country_qualification_results": [],
            "country_qualification_eligible": False,
            "country_qualification_query_count": 10,
            "country_qualification_status": "success",
            "country_qualification_source": "qualification-test",
        },
    ) as checker:
        result = chatgpt_plan._check_country_qualification(env, "TOKEN")

    checker.assert_called_once_with(env, "TOKEN", timeout=15.0)
    assert result["country_qualification_status"] == "success"
    assert result["country_qualification_source"] == "qualification-test"


def test_browser_auto_mode_uses_headed_chrome_on_macos():
    with patch.object(country_browser.sys, "platform", "darwin"), patch.dict(
        country_browser.os.environ,
        {"DISPLAY": "", "WAYLAND_DISPLAY": ""},
        clear=False,
    ), patch.object(country_browser.webui_config, "COUNTRY_QUALIFICATION_BROWSER_HEADLESS", "auto"):
        assert country_browser._headless_mode() is False
