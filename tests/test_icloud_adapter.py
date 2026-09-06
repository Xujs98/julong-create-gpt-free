# -*- coding: utf-8 -*-
from unittest.mock import Mock, patch

from core import icloud_adapter, icloud_client


def test_adapter_normalizes_markdown_url_and_matches_query_variants(tmp_path, monkeypatch):
    monkeypatch.setattr(icloud_adapter, "_ADAPTER_FILE", tmp_path / "adapters.json")

    item = icloud_adapter.add_adapter(
        "[https://remail.example/pickup?email=a%40icloud.com&token=one](https://remail.example/pickup?email=a%40icloud.com\\&token=one)"
    )
    assert item["url"] == "https://remail.example/pickup?email=a%40icloud.com&token=one"
    assert icloud_adapter.save_selector(item["id"], "#verification-code")["adapted"] is True
    assert icloud_adapter.selectors_for_url(
        "https://remail.example/pickup?email=b%40icloud.com&token=two"
    ) == ["#verification-code"]


def test_fetch_latest_otp_prefers_url_adapter_selector(monkeypatch):
    account = icloud_client.ICloudEmailAccount(
        email="sample@icloud.com", code_url="https://remail.example/pickup?token=one"
    )
    response = Mock(status_code=200, text='<span id="verification-code">482931</span><p>111111</p>')
    monkeypatch.setattr(icloud_client, "get_account_context", lambda _email: account)
    monkeypatch.setattr(icloud_client, "selectors_for_url", lambda _url: ["#verification-code"])
    monkeypatch.setattr(icloud_client.requests, "get", lambda *args, **kwargs: response)

    assert icloud_client.fetch_latest_otp(
        account.email, max_wait=2, poll_interval=1, settle_seconds=0
    ) == "482931"


def test_adapter_delete_removes_saved_url(tmp_path, monkeypatch):
    monkeypatch.setattr(icloud_adapter, "_ADAPTER_FILE", tmp_path / "adapters.json")
    item = icloud_adapter.add_adapter("https://remail.example/pickup")
    assert icloud_adapter.delete_adapter(item["id"]) is True
    assert icloud_adapter.list_adapters() == []
