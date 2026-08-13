from unittest.mock import patch

from webui.app import create_app


def _client():
    client = create_app(auth_code="twofa-api-test").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "twofa-api-test"
    return client


def test_oaics_bulk_endpoint_queues_plan_context_check():
    client = _client()
    account = {"id": 8, "email": "user@example.test", "access_token": "TOKEN"}
    with patch("webui.app.db.get_account", return_value=account), patch(
        "webui.app.plan_check_service.enqueue_account_plan_check",
        return_value={"accepted": True, "busy": False, "status": "queued"},
    ) as enqueue:
        response = client.post("/api/accounts/check-oaics-bulk", json={"account_ids": [8]})

    assert response.status_code == 202
    assert response.get_json()["started_count"] == 1
    assert enqueue.call_args.kwargs["trigger"] == "oaics_manual_bulk"
    assert enqueue.call_args.kwargs["access_token"] == "TOKEN"


def test_twofa_retry_endpoint_queues_saved_password_account():
    client = _client()
    account = {"id": 8, "email": "user@example.test", "registration_password": "PASSWORD"}
    with patch("webui.app.db.get_account", return_value=account), patch(
        "webui.app.twofa_setup_service.enqueue_account_twofa_setup",
        return_value={"accepted": True, "busy": False, "status": "queued"},
    ) as enqueue:
        response = client.post("/api/accounts/8/setup-2fa", json={})

    assert response.status_code == 202
    assert response.get_json()["started"] is True
    assert enqueue.call_args.kwargs["trigger"] == "manual_retry"
    assert enqueue.call_args.kwargs["account_id"] == 8


def test_twofa_retry_endpoint_rejects_missing_saved_password():
    client = _client()
    with patch(
        "webui.app.db.get_account",
        return_value={"id": 8, "email": "user@example.test", "registration_password": ""},
    ), patch("webui.app.twofa_setup_service.enqueue_account_twofa_setup") as enqueue:
        response = client.post("/api/accounts/8/setup-2fa", json={})

    assert response.status_code == 400
    assert "未保存注册密码" in response.get_json()["error"]
    enqueue.assert_not_called()
