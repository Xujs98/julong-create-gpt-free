# -*- coding: utf-8 -*-
"""注册流量采集与展示数据归一化。"""
from __future__ import annotations

import json
import threading
from typing import Any


def _size(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, bytearray):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except Exception:
        return len(str(value).encode("utf-8"))


def _headers_size(headers: Any) -> int:
    if not headers:
        return 0
    try:
        return sum(_size(f"{key}: {value}\r\n") for key, value in headers.items())
    except Exception:
        return _size(headers)


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class TrafficMeter:
    """线程安全的 HTTP 请求/响应流量计。"""

    def __init__(self, source: str = "browser_session") -> None:
        self.source = str(source or "browser_session")
        self._lock = threading.Lock()
        self._request_bytes = 0
        self._response_bytes = 0
        self._request_count = 0
        self._response_count = 0

    def record_request(self, url: str, headers: Any = None, kwargs: dict[str, Any] | None = None) -> None:
        options = kwargs or {}
        body = options.get("data")
        if body is None:
            body = options.get("json")
        if body is None:
            body = options.get("params")
        amount = _size(url) + _headers_size(headers) + _size(body)
        with self._lock:
            self._request_bytes += amount
            self._request_count += 1

    def record_response(self, response: Any) -> None:
        responses = list(getattr(response, "history", ()) or ()) + [response]
        amount = 0
        for item in responses:
            try:
                body = getattr(item, "content", b"")
                if body is None:
                    body = getattr(item, "text", "")
            except Exception:
                body = getattr(item, "text", "") or ""
            headers = getattr(item, "headers", None)
            declared = None
            try:
                declared = headers.get("content-length") if headers else None
            except Exception:
                declared = None
            body_bytes = _nonnegative_int(declared) if declared not in (None, "") else _size(body)
            amount += _headers_size(headers) + body_bytes
        with self._lock:
            self._response_bytes += amount
            self._response_count += len(responses)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            request_bytes = int(self._request_bytes)
            response_bytes = int(self._response_bytes)
            request_count = int(self._request_count)
            response_count = int(self._response_count)
        return {
            "request_bytes": request_bytes,
            "response_bytes": response_bytes,
            "total_bytes": request_bytes + response_bytes,
            "request_count": request_count,
            "response_count": response_count,
            "source": self.source,
        }


def normalize_snapshot(value: Any) -> dict[str, Any]:
    """归一化 BrowserSession/浏览器性能采集结果，避免把无效值写入账号。"""
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("request_bytes", "response_bytes", "total_bytes", "request_count", "response_count", "resource_count"):
        try:
            number = int(value.get(key) or 0)
        except (TypeError, ValueError):
            number = 0
        if number >= 0:
            out[key] = number
    total = out.get("total_bytes", 0)
    if not total:
        total = out.get("request_bytes", 0) + out.get("response_bytes", 0)
        out["total_bytes"] = total
    source = str(value.get("source") or "").strip()
    if source:
        out["source"] = source[:80]
    measurement = str(value.get("measurement") or "").strip()
    if measurement:
        out["measurement"] = measurement[:80]
    return out if total > 0 or out.get("request_count", 0) or out.get("response_count", 0) else {}


def browser_performance_snapshot(target: Any) -> dict[str, Any]:
    """读取 Selenium/Playwright 页面 Resource Timing 的传输字节数。"""
    if target is None:
        return {}
    rows = None
    script = """() => performance.getEntriesByType('navigation').concat(performance.getEntriesByType('resource')).map(e => ({transferSize: Number(e.transferSize || 0), encodedBodySize: Number(e.encodedBodySize || 0), decodedBodySize: Number(e.decodedBodySize || 0)}))"""
    evaluate = getattr(target, "evaluate", None)
    if callable(evaluate):
        try:
            rows = evaluate(script)
        except Exception:
            rows = None
    if rows is None:
        execute_script = getattr(target, "execute_script", None)
        if callable(execute_script):
            try:
                rows = execute_script("return performance.getEntriesByType('navigation').concat(performance.getEntriesByType('resource')).map(e => ({transferSize:Number(e.transferSize||0),encodedBodySize:Number(e.encodedBodySize||0),decodedBodySize:Number(e.decodedBodySize||0)}));")
            except Exception:
                rows = None
    if not isinstance(rows, list):
        return {}
    total = 0
    used_transfer = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        transfer = _nonnegative_int(row.get("transferSize"))
        encoded = _nonnegative_int(row.get("encodedBodySize"))
        decoded = _nonnegative_int(row.get("decodedBodySize"))
        if transfer:
            total += transfer
            used_transfer = True
        elif encoded:
            total += encoded
        else:
            total += decoded
    if total <= 0:
        return {}
    return {
        "request_bytes": 0,
        "response_bytes": total,
        "total_bytes": total,
        "request_count": 0,
        "response_count": len(rows),
        "resource_count": len(rows),
        "source": "browser_performance",
        "measurement": "transferSize" if used_transfer else "encodedBodySize",
    }
