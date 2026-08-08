import json

from core.session_state import build_saved_session, extract_saved_session


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
