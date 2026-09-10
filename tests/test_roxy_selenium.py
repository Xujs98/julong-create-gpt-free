# -*- coding: utf-8 -*-
from pathlib import Path

import pytest

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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("127.0.0.1:50003", "http://127.0.0.1:50003"),
        ("http://127.0.0.1:50003", "http://127.0.0.1:50003"),
        ("http:///127.0.0.1:50003///", "http://127.0.0.1:50003"),
        ("192.168.31.123:50003", "http://192.168.31.123:50003"),
        ("[::1]:50003", "http://[::1]:50003"),
        ("http://[2001:db8::1]:50003/", "http://[2001:db8::1]:50003"),
    ],
)
def test_canonicalize_roxy_api_base_accepts_common_user_inputs(raw, expected):
    assert roxy_selenium.canonicalize_roxy_api_base(raw) == expected


def test_canonicalize_roxy_api_base_rejects_missing_host_and_bad_port():
    assert roxy_selenium.canonicalize_roxy_api_base("http://") is None
    assert roxy_selenium.canonicalize_roxy_api_base("http://127.0.0.1:not-a-port") is None


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


def test_driver_platform_supports_linux_arm64(monkeypatch):
    monkeypatch.setattr(roxy_selenium.platform, "system", lambda: "Linux")
    monkeypatch.setattr(roxy_selenium.platform, "machine", lambda: "aarch64")

    assert roxy_selenium._driver_platform() == "linux-arm64"


def test_linux_arm64_download_failure_explains_host_bridge_requirement(monkeypatch):
    class NotFoundResponse:
        ok = False
        text = ""

        def raise_for_status(self):
            raise RuntimeError("404")

    monkeypatch.setattr(roxy_selenium, "_driver_platform", lambda: "linux-arm64")
    monkeypatch.setattr(roxy_selenium, "_cache_dir", lambda: Path("/tmp/roxy-driver-test-cache"))
    monkeypatch.setattr(roxy_selenium.requests, "get", lambda *args, **kwargs: NotFoundResponse())

    try:
        roxy_selenium._download_driver("152.0.7977.65", "152")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected Linux ARM64 driver download to fail")

    assert "macOS 宿主机运行 ./tools/roxy-chromedriver-bridge.sh" in message
    assert "host.docker.internal:9515/status" in message


def test_driver_platform_supports_macos_intel_and_apple_silicon(monkeypatch):
    monkeypatch.setattr(roxy_selenium.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(roxy_selenium.platform, "machine", lambda: "x86_64")
    assert roxy_selenium._driver_platform() == "mac-x64"

    monkeypatch.setattr(roxy_selenium.platform, "machine", lambda: "arm64")
    assert roxy_selenium._driver_platform() == "mac-arm64"


def test_driver_platform_supports_windows_x64(monkeypatch):
    monkeypatch.setattr(roxy_selenium.platform, "system", lambda: "Windows")
    monkeypatch.setattr(roxy_selenium.platform, "machine", lambda: "AMD64")

    assert roxy_selenium._driver_platform() == "win64"
