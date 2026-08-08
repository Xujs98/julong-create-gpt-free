# -*- coding: utf-8 -*-
"""代理地址解析与日志脱敏工具。"""
from __future__ import annotations

import socket
import re
import secrets
from functools import lru_cache
from urllib.parse import quote, urlsplit, urlunsplit


_SUPPORTED_SCHEMES = {"http", "https", "socks5", "socks4"}
_ROTATING_SESSION_RE = re.compile(r"(?i)(-sid-)([^:@/?#]+?)(?=-t-)")


@lru_cache(maxsize=64)
def _endpoint_supports_socks5(host: str, port: int, timeout: float = 2.0) -> bool:
    """通过 SOCKS5 方法协商探测端点协议；结果按主机和端口缓存。"""
    try:
        with socket.create_connection((host, port), timeout=timeout) as conn:
            conn.settimeout(timeout)
            conn.sendall(b"\x05\x02\x00\x02")
            response = conn.recv(2)
        return len(response) == 2 and response[0] == 5 and response[1] != 255
    except OSError:
        return False


def detect_proxy_scheme(proxy_url: str, timeout: float = 2.0) -> str:
    """为未写协议的代理探测 HTTP 或远端 DNS SOCKS5。"""
    raw = str(proxy_url or "").strip()
    parts = raw.split(":")
    try:
        if len(parts) >= 4 and parts[1].isdigit():
            host, port = parts[0], int(parts[1])
        else:
            parsed = urlsplit(raw if "://" in raw else f"http://{raw}")
            host, port = parsed.hostname or "", parsed.port
    except ValueError:
        return "http"
    if host and port and _endpoint_supports_socks5(host, port, timeout):
        return "socks5h"
    return "http"


def normalize_proxy_url(proxy_url: str | None, default_scheme: str = "http") -> str | None:
    """把常见代理池格式转换为 Playwright/HTTP 客户端可识别的 URL。"""
    raw = str(proxy_url or "").strip()
    if not raw:
        return None

    if "://" not in raw:
        scheme = detect_proxy_scheme(raw) if default_scheme == "auto" else default_scheme
        parts = raw.split(":")
        if len(parts) >= 4 and parts[1].isdigit():
            host, port, username = parts[:3]
            password = ":".join(parts[3:])
            if not all((host, port, username, password)):
                raise ValueError("四段代理格式应为 主机:端口:用户名:密码")
            auth = f"{quote(username, safe='')}:{quote(password, safe='')}"
            raw = f"{scheme}://{auth}@{host}:{port}"
        elif "@" in raw:
            raw = f"{scheme}://{raw}"
        elif len(parts) == 2:
            host, port = parts
            raw = f"{scheme}://{host}:{port}"
        else:
            raise ValueError("代理格式应为 主机:端口[:用户名:密码] 或标准代理 URL")

    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"代理端口非法: {exc}") from exc
    if parsed.scheme.lower() not in _SUPPORTED_SCHEMES | {"socks5h"}:
        raise ValueError(f"代理协议不受支持: {parsed.scheme or '<empty>'}")
    if not parsed.hostname or port is None:
        raise ValueError("代理地址缺少主机或端口")
    return raw


def masked_proxy_url(proxy_url: str | None) -> str:
    """返回适合写日志的代理地址，认证信息统一替换为星号。"""
    try:
        normalized = normalize_proxy_url(proxy_url)
        if not normalized:
            return ""
        parsed = urlsplit(normalized)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        auth = "***:***@" if parsed.username is not None or parsed.password is not None else ""
        return urlunsplit((parsed.scheme, f"{auth}{host}{port}", "", "", ""))
    except Exception:
        return "***"


def rotate_proxy_session(proxy_url: str | None, session_id: str | None = None) -> str | None:
    """Refresh provider-style ``-sid-...-t-`` usernames without touching other proxies."""
    raw = str(proxy_url or "").strip()
    if not raw:
        return proxy_url
    replacement = str(session_id or secrets.token_hex(4)).strip()
    if not replacement:
        return raw
    return _ROTATING_SESSION_RE.sub(lambda match: f"{match.group(1)}{replacement}", raw, count=1)
