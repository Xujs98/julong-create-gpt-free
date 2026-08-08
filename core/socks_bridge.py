# -*- coding: utf-8 -*-
"""把 Chromium 可用的本地 HTTP 代理桥接到带认证的上游 SOCKS5。"""
from __future__ import annotations

import logging
import select
import socket
import socketserver
import threading
from urllib.parse import unquote, urlsplit

import socks

from core.proxy_utils import masked_proxy_url

logger = logging.getLogger(__name__)


class _BridgeServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _BridgeHandler(socketserver.BaseRequestHandler):
    """处理 Chromium 发来的 HTTP CONNECT 或普通 HTTP 代理请求。"""

    def handle(self) -> None:
        upstream = None
        try:
            header = self._read_header()
            if not header:
                return
            first_line = header.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
            method, target, version = first_line.split(" ", 2)
            if method.upper() == "CONNECT":
                host, port = self._split_host_port(target, 443)
                upstream = self.server.bridge.connect(host, port)  # type: ignore[attr-defined]
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                parsed = urlsplit(target)
                host = parsed.hostname or ""
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                if not host:
                    raise ValueError("HTTP 代理请求缺少目标主机")
                upstream = self.server.bridge.connect(host, port)  # type: ignore[attr-defined]
                path = parsed.path or "/"
                if parsed.query:
                    path = f"{path}?{parsed.query}"
                rest = header.split(b"\r\n", 1)[1]
                upstream.sendall(f"{method} {path} {version}\r\n".encode("latin-1") + rest)
            self._relay(self.request, upstream)
        except Exception as exc:
            logger.debug("[SOCKS桥接] 连接结束：%s: %s", type(exc).__name__, exc)
            try:
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass

    def _read_header(self) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = self.request.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    @staticmethod
    def _split_host_port(target: str, default_port: int) -> tuple[str, int]:
        if target.startswith("[") and "]:" in target:
            host, port = target[1:].split("]:", 1)
            return host, int(port)
        if ":" in target:
            host, port = target.rsplit(":", 1)
            return host, int(port)
        return target, default_port

    @staticmethod
    def _relay(client: socket.socket, upstream: socket.socket) -> None:
        sockets = (client, upstream)
        while True:
            readable, _, _ = select.select(sockets, (), (), 600)
            if not readable:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                target = upstream if source is client else client
                target.sendall(data)


class AuthenticatedSocksBridge:
    """维护一个仅监听本机的 HTTP 代理，并转发到认证 SOCKS5。"""

    def __init__(self, upstream_url: str):
        parsed = urlsplit(str(upstream_url or ""))
        if parsed.scheme.lower() not in {"socks5", "socks5h"}:
            raise ValueError("SOCKS 桥接只接受 socks5/socks5h 上游")
        if not parsed.hostname or not parsed.port:
            raise ValueError("SOCKS 桥接上游缺少主机或端口")
        self.upstream_url = upstream_url
        self.host = parsed.hostname
        self.port = parsed.port
        self.username = unquote(parsed.username or "")
        self.password = unquote(parsed.password or "")
        self._server: _BridgeServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def proxy_url(self) -> str:
        if self._server is None:
            raise RuntimeError("SOCKS 桥接尚未启动")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "AuthenticatedSocksBridge":
        if self._server is not None:
            return self
        server = _BridgeServer(("127.0.0.1", 0), _BridgeHandler)
        server.bridge = self  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, name="cloak-socks-bridge", daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        logger.info("[SOCKS桥接] 已启动：local=%s upstream=%s", self.proxy_url, masked_proxy_url(self.upstream_url))
        return self

    def connect(self, target_host: str, target_port: int) -> socket.socket:
        """通过上游认证 SOCKS5 建立到目标的远端 DNS 连接。"""
        conn = socks.socksocket()
        conn.set_proxy(
            socks.SOCKS5,
            self.host,
            self.port,
            rdns=True,
            username=self.username or None,
            password=self.password or None,
        )
        conn.settimeout(30)
        conn.connect((target_host, int(target_port)))
        conn.settimeout(None)
        return conn

    def close(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        logger.info("[SOCKS桥接] 已关闭")


def needs_authenticated_socks_bridge(proxy_url: str | None) -> bool:
    """判断代理是否为 Chromium 原生不支持的带认证 SOCKS5。"""
    if not proxy_url:
        return False
    parsed = urlsplit(proxy_url)
    return parsed.scheme.lower() in {"socks5", "socks5h"} and parsed.username is not None
