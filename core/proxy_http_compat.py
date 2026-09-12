"""HTTP CONNECT compatibility for read-only proxy diagnostics.

Some HTTP gateways close libcurl CONNECT requests but accept the standard
HTTP/1.0 tunnel. Retry the same proxy only after a transport error, without
changing TLS verification, destination, credentials, or health criteria.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool

logger = logging.getLogger(__name__)


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
    fallback = session.__dict__.get('_proxy_http_compat')
    if fallback is not None:
        return fallback.get(url, **kwargs)
    try:
        return session.get(url, **kwargs)
    except Exception as exc:
        proxy = (session.proxies or {}).get('https' if url.startswith('https:') else 'http')
        code = getattr(exc, 'code', None)
        if not isinstance(proxy, str) or urlsplit(proxy).scheme != 'http' or code not in {5, 7, 28, 35, 56, 97}:
            raise
        fallback = requests.Session()
        fallback.trust_env = False
        fallback.proxies = {'http': proxy, 'https': proxy}
        fallback.mount('https://', _TunnelAdapter(max_retries=0))
        session.__dict__['_proxy_http_compat'] = fallback
        logger.info('[代理检测] HTTP CONNECT 兼容重试，保持同一出口；原因=%s', safe_transport_error(exc))
        return fallback.get(url, **kwargs)


def close_diagnostic_session(session):
    try:
        fallback = session.__dict__.get('_proxy_http_compat')
        if fallback is not None:
            fallback.close()
    finally:
        session.close()
