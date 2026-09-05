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

    assert roxy_selenium.normalize_debugger_address("127.0.0.1:9222") == "127.0.0.1:9222"


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
