import json
from types import SimpleNamespace

from core.session_state import build_saved_session, capture_http_cookies, extract_saved_session


def test_saved_session_keeps_full_payload_and_cookies():
    payload = {"accessToken": "TOKEN", "user": {"email": "user@example.test"}, "expires": "2030-01-01"}
    saved = build_saved_session(payload, [{"name": "sid", "value": "COOKIE", "domain": ".chatgpt.com"}])
    row = {"extra_json": json.dumps({"session": saved})}
    restored = extract_saved_session(row)
    assert restored["accessToken"] == "TOKEN"
    assert restored["user"]["email"] == "user@example.test"
    assert restored["cookies"][0]["name"] == "sid"


def test_saved_session_normalizes_cookie_defaults():
    saved = build_saved_session({"accessToken": "TOKEN"}, [{"name": "sid", "value": "COOKIE"}])
    assert saved["cookies"] == [{
        "name": "sid",
        "value": "COOKIE",
        "domain": ".chatgpt.com",
        "path": "/",
    }]


def test_missing_saved_session_returns_none():
    assert extract_saved_session({"extra_json": "{}"}) is None


def test_capture_http_cookies_reads_underlying_cookie_jar():
    """curl_cffi Cookies 本身迭代名称，采集时必须读取其 .jar。"""
    cookie = SimpleNamespace(
        name="sid",
        value="COOKIE",
        domain=".chatgpt.com",
        path="/",
        expires=None,
        secure=True,
    )

    class _Cookies:
        jar = [cookie]

        def __iter__(self):
            return iter(["sid"])

    browser_session = SimpleNamespace(session=SimpleNamespace(cookies=_Cookies()))
    assert capture_http_cookies(browser_session) == [{
        "name": "sid",
        "value": "COOKIE",
        "domain": ".chatgpt.com",
        "path": "/",
        "secure": True,
    }]
