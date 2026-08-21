# -*- coding: utf-8 -*-
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from core import extract_link_registry as registry
from core import extract_link_service
from core import pp_extract_protocol as pp
from webui.app import create_app
from webui import config_editor


@pytest.fixture()
def auth_client():
    client = create_app(auth_code="extract-link-test").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "extract-link-test"
    return client


def test_registry_persists_masks_and_deletes_api_services():
    env = {
        registry.API_SERVICES_ENV: "",
        "EXTRACT_LINK_API_BASE": "",
        "EXTRACT_LINK_CDK": "",
    }

    def persist(updates):
        os.environ.update({key: str(value) for key, value in updates.items()})
        return list(updates)

    with patch.dict(os.environ, env, clear=False), patch.object(registry, "load_env"), patch.object(
        registry, "write_env_values", side_effect=persist
    ) as writer:
        saved = registry.save_api_service({
            "name": "主提链 API",
            "api_base": "https://extract.example.test/",
            "cdk": "secret-cdk",
            "link_type": "upi",
            "workers": 7,
        })
        services = registry.list_services(mask_secrets=True)
        service = next(item for item in services if item["id"] == saved["id"])

        assert service["mode"] == "api"
        assert service["api_base"] == "https://extract.example.test"
        assert service["link_type"] == "upi"
        assert service["workers"] == 7
        assert service["has_cdk"] is True
        assert "cdk" not in service
        resolved = registry.resolve_service(mode="api", provider=saved["id"])
        assert resolved["cdk"] == "secret-cdk"
        persisted = json.loads(writer.call_args_list[0].args[0][registry.API_SERVICES_ENV])
        assert persisted[0]["cdk"] == "secret-cdk"

        assert registry.delete_api_service(saved["id"]) is True
        assert [item["id"] for item in registry.list_services()] == ["pp"]
        assert registry.delete_api_service("pp") is False


def test_registry_exposes_builtin_pp_without_cdk():
    with patch.object(registry, "_raw_api_services", return_value=[]), patch.object(
        registry, "_legacy_api_service", return_value=None
    ):
        service = registry.resolve_service(mode="protocol", provider="pp")

    assert service["name"] == "PP提链"
    assert service["requires_cdk"] is False
    assert service["protocol"] == "pp"


def test_extract_link_service_endpoints(auth_client):
    public_service = {
        "id": "api-one", "name": "API One", "mode": "api",
        "api_base": "https://extract.example.test", "has_cdk": True,
        "link_type": "pix", "workers": 3,
    }
    with patch("webui.app.extract_link_registry.list_services", return_value=[public_service]), patch(
        "webui.app.extract_link_service.queue_settings", return_value={"workers": 10, "queue_limit": 500, "retries": 5}
    ):
        response = auth_client.get("/api/extract-link/services")
    assert response.status_code == 200
    assert response.get_json()["items"] == [public_service]

    with patch("webui.app.extract_link_registry.save_api_service", return_value=public_service) as save:
        response = auth_client.post("/api/extract-link/services", json={"name": "API One"})
    assert response.status_code == 200
    save.assert_called_once_with({"name": "API One"})

    with patch("webui.app.extract_link_registry.delete_api_service", return_value=True) as delete:
        response = auth_client.delete("/api/extract-link/services/api-one")
    assert response.status_code == 200
    delete.assert_called_once_with("api-one")


def test_pp_zero_amount_guard_rejects_nonzero_and_missing_paypal():
    with pytest.raises(pp.PPProtocolError) as nonzero:
        pp._zero_amount_guard({"invoice": {"amount_due": 100}, "payment_method_types": ["paypal"]}, "paypal")
    assert nonzero.value.code == "non_zero_amount"
    assert nonzero.value.retryable is False

    with pytest.raises(pp.PPProtocolError) as unavailable:
        pp._zero_amount_guard({"invoice": {"amount_due": 0}, "payment_method_types": ["card"]}, "paypal")
    assert unavailable.value.code == "payment_method_unavailable"
    assert unavailable.value.retryable is False


def test_pp_protocol_runs_checkout_update_and_returns_authorize_url():
    redirect = "https://pm-redirects.stripe.com/authorize/test-session"
    checkout = {
        "checkout_session_id": "cs_test", "publishable_key": "pk_test",
        "processor_entity": "openai_ie", "hosted_checkout_url": "https://pay.example.test/cs_test",
        "currency": "GBP",
    }
    init = {
        "invoice": {"amount_due": 0, "currency": "gbp"},
        "payment_method_types": ["paypal"], "currency": "gbp",
        "config_id": "cfg_test", "init_checksum": "sum_test",
        "url": checkout["hosted_checkout_url"],
    }

    class FakeSession:
        closed = False

        def close(self):
            self.closed = True

    session = FakeSession()
    progress = []
    with patch.object(pp.chatgpt_plan, "normalize_token", return_value="token"), patch.object(
        pp.chatgpt_plan, "resolve_plan_check_route", return_value={"proxy": "", "network_route": "direct", "proxy_used": False}
    ), patch.object(pp, "_chatgpt_session", return_value=session), patch.object(
        pp, "_create_checkout", return_value=checkout
    ), patch.object(pp, "_stripe_init", return_value=init), patch.object(
        pp, "_create_payment_method", return_value="pm_test"
    ), patch.object(
        pp, "_confirm", return_value={"submission_attempt": {"state": "requires_approval"}}
    ), patch.object(pp, "_approve", return_value={"next_action": {"redirect_to_url": {"url": redirect}}}) as approve, patch.object(
        pp, "_poll_redirect"
    ) as poll:
        result = pp.extract_pp_link("raw-token", progress=lambda message, percent: progress.append((message, percent)))

    assert result["long_url"] == redirect
    assert result["paypal_authorize_url"] == redirect
    assert result["amount_due"] == 0
    assert result["zero_verified"] is True
    assert progress[-1] == ("PP 提链成功", 100)
    approve.assert_called_once()
    poll.assert_not_called()
    assert session.closed is True


def test_api_provider_job_stream_returns_copyable_result():
    service = {"api_base": "https://extract.example.test", "cdk": "cdk", "link_type": "pix"}
    events = [
        ("log", {"message": "正在创建链接", "progress": 55}),
        ("result", {"result": {"long_url": "https://pay.example.test/link"}}),
    ]
    with patch.object(extract_link_service, "_create_api_job", return_value={"job_id": "job-1", "cdk_remaining": 8}), patch.object(
        extract_link_service, "_iter_api_events", return_value=iter(events)
    ), patch.object(extract_link_service, "_append_log"), patch.object(
        extract_link_service.db, "update_account_extract"
    ) as update:
        output = extract_link_service._run_api_once(access_token="token", service=service, account_id=7)

    assert output["job_id"] == "job-1"
    assert output["result"]["long_url"] == "https://pay.example.test/link"
    assert output["logs"] == ["正在创建链接"]
    assert any(call.args[1].get("progress") == 55 for call in update.call_args_list)
    assert extract_link_service._validate_extract_result(output["result"]) == output["result"]


def test_success_result_requires_link_or_qr():
    with pytest.raises(RuntimeError, match="没有可复制的链接或二维码"):
        extract_link_service._validate_extract_result({"status": "ok"})


def test_extract_link_log_writer_redacts_credentials(tmp_path):
    bearer_value = "abc" * 10
    token_value = "secret" + "-token"
    cdk_value = "secret" + "-cdk"
    with patch.object(extract_link_service, "_LOG_DIR", tmp_path):
        extract_link_service._append_log(
            "user@example.test",
            f"Bearer {bearer_value} access_token={token_value} cdk={cdk_value}",
            clear=True,
        )
        content = extract_link_service.log_path("user@example.test").read_text(encoding="utf-8")

    assert "Bearer ***" in content
    assert "access_token=***" in content
    assert "cdk=***" in content
    assert token_value not in content
    assert cdk_value not in content


def test_extract_link_log_endpoint_returns_latest_account_log(auth_client, tmp_path):
    path = tmp_path / "extract-link.log"
    path.write_text("12:00:00 [INFO] 提链任务已入队\n", encoding="utf-8")
    account = {
        "id": 7,
        "email": "user@example.test",
        "extract_link_status": "running",
        "extract_link_progress": 48,
        "extract_link_service_name": "PP提链",
    }
    with patch("webui.app.db.get_account", return_value=account), patch(
        "webui.app.extract_link_service.log_path", return_value=path
    ):
        response = auth_client.get("/api/accounts/7/extract-link-log")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["running"] is True
    assert payload["progress"] == 48
    assert payload["service_name"] == "PP提链"
    assert "提链任务已入队" in payload["log"]


def test_queue_limit_uses_runtime_setting():
    with patch.object(extract_link_service, "_QUEUED_TASKS", 0), patch.object(
        extract_link_service, "queue_settings", return_value={"workers": 10, "queue_limit": 1, "retries": 5}
    ):
        assert extract_link_service._try_acquire_queue_slot() is True
        assert extract_link_service._try_acquire_queue_slot() is False
        extract_link_service._release_queue_slot()
        assert extract_link_service._try_acquire_queue_slot() is True
        extract_link_service._release_queue_slot()


def test_common_extract_link_ui_contains_requested_controls():
    html = (Path(__file__).parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")

    for text in (
        "通用提链", "添加提链 API", "API提链", "协议提链", "PP提链",
    ):
        assert text in html
    for field_id in ("extractApiName", "extractApiBase", "extractApiCdk", "extractApiType", "extractApiWorkers"):
        assert f'id="{field_id}"' in html
    assert "flex-direction: column" in html
    assert "data-copy-extract-link" in html
    assert 'data-account-extract-log="${esc(r.id)}"' in html
    assert "提链日志" in html
    assert 'id="extractLinkLogPanel"' in html
    assert "/extract-link-log`" in html
    assert "openExtractLinkLog" in html


def test_common_extract_link_config_fields_and_defaults():
    fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
    expected = {
        "EXTRACT_LINK_MODE": "提链方式",
        "EXTRACT_LINK_PROVIDER": "提链服务",
        "EXTRACT_LINK_BILLING_COUNTRY": "账单国家",
        "EXTRACT_LINK_PAYMENT_METHOD": "支付方式",
        "EXTRACT_LINK_AUTO_ENTER_PAYPAL": "提链成功后自动进入 PAYPAL",
        "EXTRACT_LINK_CHECKOUT_UPDATE": "执行 Checkout Update",
        "EXTRACT_LINK_WORKERS": "当前用户并发",
        "EXTRACT_LINK_RETRIES": "提链重试次数",
    }
    for key, label in expected.items():
        assert fields[key]["label"] == label

    source = (Path(__file__).parents[1] / "config" / "extract_link.py").read_text(encoding="utf-8")
    assert config_editor._parse_value_from_source(source, "EXTRACT_LINK_MODE", "str") == "protocol"
    assert config_editor._parse_value_from_source(source, "EXTRACT_LINK_PROVIDER", "str") == "pp"
    assert config_editor._parse_value_from_source(source, "EXTRACT_LINK_AUTO_ENTER_PAYPAL", "bool") is True
    assert config_editor._parse_value_from_source(source, "EXTRACT_LINK_CHECKOUT_UPDATE", "bool") is True
    assert config_editor._parse_value_from_source(source, "EXTRACT_LINK_WORKERS", "int") == 10
    assert config_editor._parse_value_from_source(source, "EXTRACT_LINK_RETRIES", "int") == 5
