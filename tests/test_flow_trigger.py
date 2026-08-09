from unittest.mock import patch

from config import flow_trigger as cfg
from core import flow_trigger


class _Response:
    status_code = 200
    text = '{"flow":{"flow_id":"flow-1"}}'

    def json(self):
        return {"flow": {"flow_id": "flow-1"}}


def test_flow_injects_access_token_into_json_body():
    with patch.object(cfg, "ENABLE_FLOW_TRIGGER", True), patch.object(
        cfg, "FLOW_TRIGGER_URL", "https://flow.example.test/trigger"
    ), patch.object(cfg, "FLOW_TRIGGER_PAYLOAD", {"email": "user@example.com"}), patch(
        "core.flow_trigger.requests.post", return_value=_Response()
    ) as post:
        result = flow_trigger.trigger_flow("ACCESS_TOKEN")

    assert result["status"] == "success"
    assert result["flow_id"] == "flow-1"
    assert post.call_args.kwargs["json"] == {
        "email": "user@example.com",
        "access_token": "ACCESS_TOKEN",
    }


def test_flow_skips_invalid_empty_url_without_missing_schema():
    with patch.object(cfg, "ENABLE_FLOW_TRIGGER", True), patch.object(cfg, "FLOW_TRIGGER_URL", ""):
        with patch("core.flow_trigger.requests.post") as post:
            result = flow_trigger.trigger_flow("ACCESS_TOKEN")

    assert result["status"] == "skipped"
    assert "FLOW_TRIGGER_URL" in result["message"]
    post.assert_not_called()


def test_flow_skips_without_access_token():
    with patch.object(cfg, "ENABLE_FLOW_TRIGGER", True), patch.object(
        cfg, "FLOW_TRIGGER_URL", "https://flow.example.test/trigger"
    ):
        with patch("core.flow_trigger.requests.post") as post:
            result = flow_trigger.trigger_flow("")

    assert result["status"] == "skipped"
    assert result["message"] == "access_token 为空"
    post.assert_not_called()
