# -*- coding: utf-8 -*-
from pathlib import Path

from core import roxy_selenium


def _fake_driver(path: Path, version: str = "152.0.7977.65") -> Path:
    path.write_text(f"#!/bin/sh\necho 'ChromeDriver {version}'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_normalize_debugger_address_uses_resolved_container_gateway(monkeypatch):
    monkeypatch.setattr(roxy_selenium, "_running_in_container", lambda: True)
    monkeypatch.setattr(roxy_selenium._cfg, "ROXY_DEBUGGER_HOST", "host.docker.internal")
    monkeypatch.setattr(roxy_selenium, "_resolve_host", lambda host: "192.168.65.254")

    assert roxy_selenium.normalize_debugger_address("127.0.0.1:53580") == "192.168.65.254:53580"


def test_normalize_debugger_address_keeps_local_host(monkeypatch):
    monkeypatch.setattr(roxy_selenium, "_running_in_container", lambda: False)
    monkeypatch.setattr(roxy_selenium._cfg, "ROXY_DEBUGGER_HOST", "")
    monkeypatch.setattr(roxy_selenium._cfg, "ROXY_API_BASE", "http://127.0.0.1:50100")

    assert roxy_selenium.normalize_debugger_address("127.0.0.1:9222") == "127.0.0.1:9222"


def test_native_local_api_ignores_stale_container_debugger_host(monkeypatch):
    monkeypatch.setattr(roxy_selenium, "_running_in_container", lambda: False)
    monkeypatch.setattr(roxy_selenium._cfg, "ROXY_API_BASE", "http://127.0.0.1:50100")
    monkeypatch.setattr(roxy_selenium._cfg, "ROXY_DEBUGGER_HOST", "198.18.0.39")

    assert roxy_selenium.normalize_debugger_address("127.0.0.1:64670") == "127.0.0.1:64670"


def test_native_remote_api_still_keeps_loopback_debugger(monkeypatch):
    monkeypatch.setattr(roxy_selenium, "_running_in_container", lambda: False)
    monkeypatch.setattr(roxy_selenium._cfg, "ROXY_API_BASE", "http://192.0.2.10:50100")
    monkeypatch.setattr(roxy_selenium._cfg, "ROXY_DEBUGGER_HOST", "192.0.2.10")

    assert roxy_selenium.normalize_debugger_address("127.0.0.1:64670") == "127.0.0.1:64670"


def test_container_rewrites_loopback_api_base_to_host_gateway(monkeypatch):
    monkeypatch.setattr(roxy_selenium, "_running_in_container", lambda: True)
    monkeypatch.setattr(roxy_selenium._cfg, "ROXY_DEBUGGER_HOST", "host.docker.internal")
    monkeypatch.setattr(roxy_selenium, "_resolve_host", lambda host: "192.168.65.254")

    assert roxy_selenium.normalize_api_base("http://127.0.0.1:50100") == "http://192.168.65.254:50100"


def test_resolve_chromedriver_reuses_matching_returned_driver(tmp_path, monkeypatch):
    driver = _fake_driver(tmp_path / "chromedriver")
    monkeypatch.setattr(roxy_selenium, "_browser_version", lambda _address: "152.0.7977.65")

    result = roxy_selenium.resolve_chromedriver(
        {"data": {"driver": str(driver), "coreVersion": "152"}},
        "192.168.65.254:53580",
    )

    assert result == str(driver)


def test_resolve_chromedriver_downloads_when_roxy_path_is_host_only(tmp_path, monkeypatch):
    downloaded = _fake_driver(tmp_path / "downloaded-driver")
    monkeypatch.setattr(roxy_selenium, "_browser_version", lambda _address: "152.0.7977.65")
    monkeypatch.setattr(roxy_selenium, "_download_driver", lambda _version, _major: downloaded)

    result = roxy_selenium.resolve_chromedriver(
        {"data": {"driver": "/Users/host/Library/Application Support/RoxyBrowser/chromedriver"}},
        "192.168.65.254:53580",
    )

    assert result == str(downloaded)
