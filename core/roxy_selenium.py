# -*- coding: utf-8 -*-
"""Selenium helpers for attaching to a RoxyBrowser profile from local or Docker."""
from __future__ import annotations

import io
import logging
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import threading
import zipfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests

from config import roxybrowser as _cfg

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_DRIVER_DOWNLOAD_LOCK = threading.Lock()


def _running_in_container() -> bool:
    return Path("/.dockerenv").exists() or os.getenv("CONTAINER") == "docker"


def _parse_host_port(value: str) -> tuple[str, int | None]:
    text = str(value or "").strip()
    if not text:
        return "", None
    parsed = urlparse(text if "://" in text else f"http://{text}")
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    return host, port


def _format_host(host: str) -> str:
    text = str(host or "").strip()
    if ":" in text and not text.startswith("["):
        return f"[{text}]"
    return text


def _effective_debugger_host() -> str:
    """Return the host used to reach Roxy's debugger from this runtime.

    A Docker-specific ``ROXY_DEBUGGER_HOST`` is commonly kept in the shared
    ``.env`` file.  Reusing that value in a native macOS process rewrites
    Roxy's loopback debugger to an unreachable container gateway (the failure
    seen as ``cannot connect to chrome at 198.18.x.x``). Native runs always
    keep loopback addresses; only a container applies the gateway override.
    """
    configured = str(getattr(_cfg, "ROXY_DEBUGGER_HOST", "") or "").strip()
    in_container = _running_in_container()
    if not in_container:
        # Native Roxy + native Selenium share the same loopback namespace. A
        # LAN API address does not change that: the debugger address is still
        # relative to the native Roxy process, so never apply Docker gateway
        # rewriting to a native run.
        return ""
    if configured:
        return configured
    if in_container:
        # Docker Desktop exposes the host through this gateway. The hostname is
        # resolved to an IP below because Roxy rejects a non-IP Host header.
        return "host.docker.internal"
    return ""


def _resolve_host(host: str) -> str:
    value = str(host or "").strip()
    if not value or not bool(getattr(_cfg, "ROXY_DEBUGGER_RESOLVE_HOST", True)):
        return value
    if value in _LOOPBACK_HOSTS or re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", value):
        return value
    try:
        return socket.gethostbyname(value)
    except OSError:
        logger.warning("[Roxy] 无法解析调试主机 %s，将保留原主机名", value)
        return value


def normalize_debugger_address(address: str | None) -> str | None:
    """Rewrite Roxy's host-local debugger address for a Docker client.

    Roxy commonly returns ``127.0.0.1:<port>`` even when its API was called
    through a LAN address. In a container that loopback is the container, so a
    configured host (or Docker Desktop's host gateway) is substituted.
    """
    if not address:
        return None
    raw_host, raw_port = _parse_host_port(str(address))
    if not raw_host or raw_port is None:
        return str(address).strip()

    override = _effective_debugger_host() if raw_host.lower() in _LOOPBACK_HOSTS else ""
    host, port = raw_host, raw_port
    if override:
        override_host, override_port = _parse_host_port(override)
        if override_host:
            host = override_host
        if override_port:
            port = override_port
    host = _resolve_host(host)
    normalized = f"{_format_host(host)}:{port}"
    return normalized


def normalize_webdriver_url(url: str | None) -> str | None:
    """Apply the same host rewrite to a Roxy-provided Selenium endpoint."""
    if not url:
        return None
    text = str(url).strip()
    parsed = urlparse(text if "://" in text else f"http://{text}")
    host = parsed.hostname or ""
    if host.lower() not in _LOOPBACK_HOSTS:
        return text
    override = _effective_debugger_host()
    if not override:
        return text
    override_host, override_port = _parse_host_port(override)
    if not override_host:
        return text
    port = override_port or parsed.port
    netloc = _format_host(_resolve_host(override_host))
    if port:
        netloc = f"{netloc}:{port}"
    return urlunparse((parsed.scheme or "http", netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def normalize_api_base(base: str | None) -> str | None:
    """Make a loopback Roxy API URL reachable from Docker when needed."""
    if not base:
        return None
    text = str(base).strip()
    parsed = urlparse(text if "://" in text else f"http://{text}")
    host = parsed.hostname or ""
    if not _running_in_container() or host.lower() not in _LOOPBACK_HOSTS:
        return text
    override = _effective_debugger_host()
    override_host, override_port = _parse_host_port(override)
    if not override_host:
        return text
    port = parsed.port or override_port
    netloc = _format_host(_resolve_host(override_host))
    if port:
        netloc = f"{netloc}:{port}"
    normalized = urlunparse((parsed.scheme or "http", netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    if normalized != text:
        logger.info("[Roxy] 已按运行环境重写 API 地址：%s -> %s", text, normalized)
    return normalized


def _version_major(version: str | None) -> str:
    match = re.search(r"(?:^|\D)(\d{2,3})(?:\.|$)", str(version or ""))
    return match.group(1) if match else ""


def _browser_version(debugger_address: str | None) -> str:
    if not debugger_address:
        return ""
    url = f"http://{debugger_address.strip().rstrip('/')}/json/version"
    try:
        # Roxy validates Host and accepts localhost/IP values. An explicit
        # localhost header also keeps compatibility if DNS resolution failed.
        response = requests.get(url, headers={"Host": "127.0.0.1"}, timeout=5)
        response.raise_for_status()
        payload = response.json() if response.content else {}
        browser = str(payload.get("Browser") or payload.get("browser") or "")
        match = re.search(r"(\d+\.\d+\.\d+\.\d+)", browser)
        return match.group(1) if match else browser
    except Exception as exc:
        logger.debug("[Roxy] 读取远端 Chrome 版本失败：%s", exc)
        return ""


def _driver_version(path: Path) -> str:
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    text = " ".join(part for part in (result.stdout, result.stderr) if part)
    match = re.search(r"(\d+\.\d+\.\d+\.\d+)", text)
    return match.group(1) if match else ""


def _usable_driver(path: str | Path, expected_major: str) -> tuple[bool, str]:
    candidate = Path(path).expanduser()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        return False, ""
    version = _driver_version(candidate)
    if expected_major and version and _version_major(version) != expected_major:
        logger.warning(
            "[Roxy] 忽略版本不匹配的 Chromedriver：%s（driver=%s，Chrome=%s）",
            candidate,
            version,
            expected_major,
        )
        return False, version
    return True, version


def _cache_dir() -> Path:
    configured = str(getattr(_cfg, "ROXY_DRIVER_CACHE_DIR", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if Path("/app/runtime").is_dir() and os.access("/app/runtime", os.W_OK):
        return Path("/app/runtime/roxy-drivers")
    return Path.home() / ".cache" / "turb-gpt-free-register" / "roxy-drivers"


def _linux_driver_platform() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "linux64"
    raise RuntimeError(f"Docker Roxy 自动下载 Chromedriver 暂不支持 CPU 架构：{machine}")


def _download_driver(remote_version: str, expected_major: str) -> Path:
    if not expected_major:
        raise RuntimeError(
            "无法确定 Roxy Chrome 主版本，且本机没有可用 Chromedriver；"
            "请设置 ROXY_CHROMEDRIVER_PATH 或检查调试端口连通性"
        )

    platform_name = _linux_driver_platform() if platform.system().lower() == "linux" else {
        "darwin": "mac-x64",
        "windows": "win64",
    }.get(platform.system().lower(), "")
    if not platform_name:
        raise RuntimeError(f"当前系统暂不支持自动下载 Chromedriver：{platform.system()}")

    versions: list[str] = []
    exact = str(remote_version or "").strip()
    if exact.count(".") >= 2:
        versions.append(exact)
    try:
        latest = requests.get(
            f"https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_{expected_major}",
            timeout=20,
        )
        if latest.ok and latest.text.strip():
            versions.append(latest.text.strip())
    except Exception as exc:
        logger.debug("[Roxy] 查询 Chrome for Testing 版本失败：%s", exc)
    if not versions:
        versions.append(expected_major)

    cache_root = _cache_dir()
    errors: list[str] = []
    with _DRIVER_DOWNLOAD_LOCK:
        for version in dict.fromkeys(versions):
            target_dir = cache_root / version
            target = target_dir / "chromedriver"
            usable, _ = _usable_driver(target, expected_major)
            if usable:
                return target
            url = (
                "https://storage.googleapis.com/chrome-for-testing-public/"
                f"{version}/{platform_name}/chromedriver-{platform_name}.zip"
            )
            try:
                logger.info("[Roxy] 下载 Linux Chromedriver：version=%s platform=%s", version, platform_name)
                response = requests.get(url, timeout=90)
                response.raise_for_status()
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    member = next(
                        name for name in archive.namelist()
                        if name.endswith("/chromedriver") or name == "chromedriver"
                    )
                    data = archive.read(member)
                target_dir.mkdir(parents=True, exist_ok=True)
                temp = target_dir / f".chromedriver.{os.getpid()}.tmp"
                temp.write_bytes(data)
                temp.chmod(temp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                os.replace(temp, target)
                usable, driver_version = _usable_driver(target, expected_major)
                if usable:
                    logger.info("[Roxy] Chromedriver 已缓存：%s (%s)", target, driver_version or version)
                    return target
                errors.append(f"{version}: 下载后版本校验失败")
            except Exception as exc:
                errors.append(f"{version}: {type(exc).__name__}: {exc}")
                try:
                    temp.unlink(missing_ok=True)
                except Exception:
                    pass

    detail = "; ".join(errors[-3:])
    raise RuntimeError(
        "Docker 连接 Roxy 需要 Linux Chromedriver，自动下载失败。"
        + (f" 详情：{detail}" if detail else "")
    )


def resolve_chromedriver(raw: dict | None, debugger_address: str | None) -> str:
    """Return a driver executable that can attach to the remote Roxy Chrome."""
    raw_data = raw.get("data") if isinstance(raw, dict) else {}
    raw_data = raw_data if isinstance(raw_data, dict) else {}
    remote_version = _browser_version(debugger_address)
    if not remote_version:
        remote_version = str(raw_data.get("coreVersion") or raw_data.get("browserVersion") or "")
    expected_major = _version_major(remote_version)

    configured = str(getattr(_cfg, "ROXY_CHROMEDRIVER_PATH", "") or "").strip()
    returned = str(
        raw_data.get("driver")
        or raw_data.get("driverPath")
        or raw_data.get("driver_path")
        or ""
    ).strip()
    candidates = [configured, returned, shutil.which("chromedriver") or "", "/usr/local/bin/chromedriver", "/usr/bin/chromedriver"]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        usable, version = _usable_driver(candidate, expected_major)
        if usable:
            if candidate == returned and configured and candidate != configured:
                logger.info("[Roxy] 使用 Roxy 返回的 Chromedriver：%s", candidate)
            return str(Path(candidate).expanduser())
        if version and not expected_major:
            expected_major = _version_major(version)

    return str(_download_driver(remote_version, expected_major))
