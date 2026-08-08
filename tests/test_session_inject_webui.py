from unittest.mock import patch

from webui.app import create_app


def _client():
    client = create_app(auth_code="inject-test").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "inject-test"
    return client


def test_inject_session_bulk_dispatches_selected_ids_and_workers():
    expected = {
        "results": [{"id": 2, "email": "user@example.test", "ok": True}],
        "success": [{"id": 2, "email": "user@example.test", "ok": True}],
        "failed": [],
        "skipped": [],
    }
    with patch("core.session_injector.inject_sessions", return_value=expected) as inject:
        response = _client().post(
            "/api/accounts/inject-session-bulk",
            json={"account_ids": [2], "workers": 4},
        )
    assert response.status_code == 200
    assert response.get_json()["success"][0]["id"] == 2
    inject.assert_called_once_with([2], max_workers=4)


def test_inject_session_bulk_rejects_empty_selection():
    response = _client().post("/api/accounts/inject-session-bulk", json={"account_ids": []})
    assert response.status_code == 400
