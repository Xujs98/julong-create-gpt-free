from unittest.mock import patch

from webui.app import create_app


def test_job_log_api_returns_incremental_delta_and_reuses_job_row():
    app = create_app(auth_code="job-log-test")
    client = app.test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "job-log-test"
    job = {"id": 7, "status": "running", "log_file": "/tmp/job.log"}
    delta = {
        "content": "new line\n",
        "offset": 42,
        "size": 42,
        "mtime_ns": 123,
        "reset": False,
        "changed": True,
        "exists": True,
    }

    with patch("webui.app.db.get_job", return_value=job) as get_job, patch(
        "webui.app.svc.read_job_log_delta", return_value=delta
    ) as read_delta:
        response = client.get("/api/jobs/7/log?offset=35")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["log"] == "new line\n"
    assert payload["log_delta"] == delta
    assert payload["offset"] == 42
    assert payload["size"] == 42
    assert payload["reset"] is False
    assert payload["log_changed"] is True
    get_job.assert_called_once_with(7)
    read_delta.assert_called_once_with(7, offset=35, job=job)
