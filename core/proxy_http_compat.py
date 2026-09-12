"""HTTP CONNECT compatibility for read-only proxy diagnostics.

Some HTTP gateways close libcurl CONNECT requests but accept the standard
HTTP/1.0 tunnel. Retry the same proxy only after a transport error, without
changing TLS verification, destination, credentials, or health criteria.
"""
from __future__ import annotations

import logging
import re
import time
from contextvars import ContextVar
from functools import wraps
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool

logger = logging.getLogger(__name__)
_diagnostic = ContextVar('proxy_diagnostic', default=None)


class DiagnosticTimeout(TimeoutError):
    """The diagnostic exhausted its shared request budget."""


def diagnostic_run(seconds):
    """Share a deadline and transport choice, but never share sample connections."""
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            if _diagnostic.get() is not None:
                return fn(*args, **kwargs)
            token = _diagnostic.set({'deadline': time.monotonic() + seconds, 'compat': set()})
            try:
                return fn(*args, **kwargs)
            finally:
                _diagnostic.reset(token)
        return run
    return decorate


def _request_options(kwargs, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DiagnosticTimeout('代理检测时间预算已用尽')
    return {**kwargs, 'timeout': remaining}


class _HTTP10Tunnel(HTTPSConnection):
    def _tunnel(self):
        version, version_str = self._http_vsn, self._http_vsn_str
        self._http_vsn, self._http_vsn_str = 10, 'HTTP/1.0'
        try:
            return super()._tunnel()
        finally:
            self._http_vsn, self._http_vsn_str = version, version_str


class _TunnelPool(HTTPSConnectionPool):
    ConnectionCls = _HTTP10Tunnel


class _TunnelAdapter(HTTPAdapter):
    def proxy_manager_for(self, proxy, **kwargs):
        manager = super().proxy_manager_for(proxy, **kwargs)
        manager.pool_classes_by_scheme = dict(manager.pool_classes_by_scheme, https=_TunnelPool)
        return manager


def safe_transport_error(exc: Exception) -> str:
    """Only controlled error labels/codes; exception URLs may contain secrets."""
    if isinstance(exc, DiagnosticTimeout):
        return '代理检测时间预算已用尽'
    text = str(exc).lower()
    status = re.search(r'\bhttp\s+(\d{3})\b', text)
    if status:
        return f'HTTP {status.group(1)}'
    if '407' in text or 'authentication' in text or 'credentials' in text:
        return '代理认证失败（407）'
    if 'resolve' in text or 'name resolution' in text:
        return 'DNS解析失败'
    if 'timeout' in type(exc).__name__.lower() or 'timed out' in text or '超时' in text:
        return '代理连接/响应超时'
    if '隧道' in text or '中断' in text or 'connect' in text or 'connection' in text or 'reset' in text or 'closed' in text:
        return '代理CONNECT隧道中断'
    if 'ssl' in text or 'certificate' in text:
        return 'TLS证书/握手失败'
    return type(exc).__name__


def compatible_get(session, url: str, **kwargs):
    state = _diagnostic.get()
    request_timeout = float(kwargs.get('timeout') or 12)
    deadline = time.monotonic() + request_timeout
    if state is not None:
        deadline = min(deadline, state['deadline'])
    proxy = (session.proxies or {}).get('https' if url.startswith('https:') else 'http')
    fallback = session.__dict__.get('_proxy_http_compat')
    if fallback is None and state is not None and proxy in state['compat']:
        fallback = _fallback_session(session, proxy)
    if fallback is not None:
        return fallback.get(url, **_request_options(kwargs, deadline))
    try:
        options = _request_options(kwargs, deadline)
        if state is not None and isinstance(proxy, str) and urlsplit(proxy).scheme == 'http':
            # Reserve time for the compatible transport instead of spending
            # the entire operation budget on a known-problematic CONNECT.
            options['timeout'] = min(options['timeout'], 4.0, request_timeout / 2)
        return session.get(url, **options)
    except Exception as exc:
        code = getattr(exc, 'code', None)
        if not isinstance(proxy, str) or urlsplit(proxy).scheme != 'http' or code not in {5, 7, 28, 35, 56, 97}:
            raise
        options = _request_options(kwargs, deadline)
        fallback = _fallback_session(session, proxy)
        logger.info('[代理检测] HTTP CONNECT 兼容重试，保持同一出口；原因=%s', safe_transport_error(exc))
        response = fallback.get(url, **options)
        if state is not None:
            state['compat'].add(proxy)
        return response


def _fallback_session(session, proxy):
    fallback = requests.Session()
    fallback.trust_env = False
    fallback.proxies = {'http': proxy, 'https': proxy}
    fallback.mount('https://', _TunnelAdapter(max_retries=0))
    session.__dict__['_proxy_http_compat'] = fallback
    return fallback


def close_diagnostic_session(session):
    try:
        fallback = session.__dict__.get('_proxy_http_compat')
        if fallback is not None:
            fallback.close()
    finally:
        session.close()
