# -*- coding: utf-8 -*-
"""注册流量采集与展示数据归一化。"""
from __future__ import annotations

import base64
import json
import threading
from collections import deque
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


def _websocket_payload_size(value: Any) -> int:
    """Return payload bytes for Playwright/CDP text or binary frames."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    return _size(value)


class TrafficMeter:
    """线程安全的 HTTP 请求/响应流量计。"""

    def __init__(self, source: str = "browser_session") -> None:
        self.source = str(source or "browser_session")
        self._lock = threading.Lock()
        self._request_bytes = 0
        self._response_bytes = 0
        self._request_count = 0
        self._response_count = 0
        self._pending_request_estimates: deque[int] = deque()
        self._used_transfer_sizes = False

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
            self._pending_request_estimates.append(amount)

    def record_response(self, response: Any) -> None:
        request_size = _nonnegative_int(getattr(response, "request_size", 0))
        response_size = _nonnegative_int(getattr(response, "response_size", 0))
        responses = list(getattr(response, "history", ()) or ()) + [response]
        if response_size:
            amount = response_size
            response_count = max(1, len(responses))
        else:
            amount = 0
            response_count = len(responses)
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
            estimate = self._pending_request_estimates.popleft() if self._pending_request_estimates else 0
            if request_size:
                self._request_bytes += request_size - estimate
                self._used_transfer_sizes = True
            self._request_count += max(0, response_count - 1)
            self._response_bytes += amount
            self._response_count += response_count
            if response_size:
                self._used_transfer_sizes = True

    def record_request_failure(self) -> None:
        """Close one estimated outgoing request after a transport error."""
        with self._lock:
            if self._pending_request_estimates:
                self._pending_request_estimates.popleft()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            request_bytes = int(self._request_bytes)
            response_bytes = int(self._response_bytes)
            request_count = int(self._request_count)
            response_count = int(self._response_count)
            used_transfer_sizes = bool(self._used_transfer_sizes)
        return {
            "request_bytes": request_bytes,
            "upload_bytes": request_bytes,
            "response_bytes": response_bytes,
            "download_bytes": response_bytes,
            "total_bytes": request_bytes + response_bytes,
            "request_count": request_count,
            "response_count": response_count,
            "source": self.source,
            "measurement": "curl_transfer_sizes" if used_transfer_sizes else "http_message_estimate",
            "scope": "application_http",
            "confidence": "high" if used_transfer_sizes else "medium",
        }


class BrowserTrafficMeter:
    """Accumulate browser request and response bytes for one task.

    Playwright exposes wire-oriented request/response sizes directly. Selenium
    exposes equivalent data through ChromeDriver's performance log. Both paths
    intentionally keep transport overhead separate from application bytes.
    """

    def __init__(self, source: str) -> None:
        self.source = str(source or "browser_network")
        self._lock = threading.Lock()
        self._upload_bytes = 0
        self._download_bytes = 0
        self._request_count = 0
        self._response_count = 0
        self._seen_playwright: set[int] = set()
        self._playwright_upload_estimates: dict[int, int] = {}
        self._selenium_upload_estimates: dict[str, int] = {}
        self._selenium_response_estimates: dict[str, int] = {}
        self._selenium_active_responses: set[str] = set()
        self._selenium_completed_responses: dict[str, int] = {}
        self._seen_websockets: set[int] = set()
        self._errors = 0

    def record_playwright_request(self, request: Any) -> None:
        """Count an outgoing request immediately, including failed requests."""
        marker = id(request)
        try:
            headers_getter = getattr(request, "all_headers", None)
            headers = headers_getter() if callable(headers_getter) else getattr(request, "headers", None)
            body = getattr(request, "post_data_buffer", None)
            if body is None:
                body = getattr(request, "post_data", None)
            method = str(getattr(request, "method", "GET") or "GET")
            url = str(getattr(request, "url", "") or "")
            amount = _size(f"{method} {url} HTTP/1.1\r\n") + _headers_size(headers) + _size(body)
        except Exception:
            amount = 0
            with self._lock:
                self._errors += 1
        with self._lock:
            if marker in self._playwright_upload_estimates:
                return
            self._playwright_upload_estimates[marker] = amount
            self._upload_bytes += amount
            self._request_count += 1

    def record_playwright_request_finished(self, request: Any) -> None:
        marker = id(request)
        with self._lock:
            if marker in self._seen_playwright:
                return
            self._seen_playwright.add(marker)
        try:
            sizes = request.sizes() or {}
            upload = _nonnegative_int(sizes.get("requestHeadersSize")) + _nonnegative_int(
                sizes.get("requestBodySize")
            )
            download = _nonnegative_int(sizes.get("responseHeadersSize")) + _nonnegative_int(
                sizes.get("responseBodySize")
            )
        except Exception:
            with self._lock:
                self._errors += 1
            return
        with self._lock:
            estimate = self._playwright_upload_estimates.pop(marker, None)
            if estimate is None:
                self._request_count += 1
                estimate = 0
            measured_upload = upload or estimate
            self._upload_bytes += measured_upload - estimate
            self._download_bytes += download
            self._response_count += 1

    def record_playwright_request_failed(self, request: Any) -> None:
        marker = id(request)
        failure = str(getattr(request, "failure", "") or "").lower()
        with self._lock:
            estimate = self._playwright_upload_estimates.pop(marker, None)
            if estimate is not None and any(token in failure for token in ("blocked", "aborted", "inspector")):
                self._upload_bytes -= estimate
                self._request_count = max(0, self._request_count - 1)

    def install_playwright_page(self, page: Any) -> None:
        """Attach WebSocket frame accounting to one Playwright page."""
        if page is None:
            return
        try:
            setattr(page, "_registration_traffic_meter", self)
        except Exception:
            pass
        try:
            page.on("websocket", self._install_playwright_websocket)
        except Exception:
            pass

    def _install_playwright_websocket(self, websocket: Any) -> None:
        marker = id(websocket)
        with self._lock:
            if marker in self._seen_websockets:
                return
            self._seen_websockets.add(marker)
        try:
            websocket.on("framesent", lambda payload: self._record_websocket_frame(payload, sent=True))
            websocket.on("framereceived", lambda payload: self._record_websocket_frame(payload, sent=False))
        except Exception:
            with self._lock:
                self._errors += 1

    def _record_websocket_frame(self, payload: Any, *, sent: bool) -> None:
        amount = _websocket_payload_size(payload)
        # RFC 6455 frame overhead: server frames use 2-10 bytes; browser-sent
        # frames add a four-byte masking key.  This excludes the TCP/TLS layer.
        frame_bytes = amount + (2 if amount < 126 else 4 if amount <= 65535 else 10) + (4 if sent else 0)
        with self._lock:
            if sent:
                self._upload_bytes += frame_bytes
            else:
                self._download_bytes += frame_bytes

    def record_selenium_logs(self, entries: Any) -> None:
        for entry in entries or ():
            try:
                outer = json.loads(entry.get("message") or "{}") if isinstance(entry, dict) else {}
                message = outer.get("message") or {}
                method = str(message.get("method") or "")
                params = message.get("params") or {}
                request_id = str(params.get("requestId") or "")
                if not request_id:
                    continue
                if method == "Network.requestWillBeSent":
                    request = params.get("request") or {}
                    header_text = request.get("headersText")
                    header_bytes = _size(header_text) if header_text else _headers_size(request.get("headers"))
                    body_bytes = _size(request.get("postData"))
                    line_bytes = _size(f"{request.get('method') or 'GET'} {request.get('url') or ''} HTTP/1.1\r\n")
                    amount = line_bytes + header_bytes + body_bytes
                    redirect = params.get("redirectResponse") or {}
                    if redirect:
                        redirect_encoded = _nonnegative_int(redirect.get("encodedDataLength"))
                        redirect_headers = redirect.get("headersText")
                        redirect_estimate = (
                            _size(redirect_headers) if redirect_headers else _headers_size(redirect.get("headers"))
                        )
                        with self._lock:
                            previous = self._selenium_response_estimates.pop(request_id, 0)
                            already_seen = request_id in self._selenium_active_responses
                            self._selenium_active_responses.discard(request_id)
                            completed = self._selenium_completed_responses.pop(request_id, None)
                            redirect_bytes = redirect_encoded or previous or redirect_estimate
                            if already_seen:
                                self._download_bytes += redirect_bytes - previous
                            elif completed is None:
                                self._response_count += 1
                                self._download_bytes += redirect_bytes
                    with self._lock:
                        self._request_count += 1
                        self._upload_bytes += amount
                        self._selenium_upload_estimates[request_id] = amount
                elif method == "Network.responseReceived":
                    response = params.get("response") or {}
                    header_text = response.get("headersText")
                    header_bytes = _size(header_text) if header_text else _headers_size(response.get("headers"))
                    with self._lock:
                        if request_id not in self._selenium_active_responses:
                            self._selenium_active_responses.add(request_id)
                            self._selenium_response_estimates[request_id] = header_bytes
                            self._response_count += 1
                            self._download_bytes += header_bytes
                elif method == "Network.dataReceived":
                    amount = _nonnegative_int(params.get("encodedDataLength"))
                    with self._lock:
                        if request_id not in self._selenium_active_responses:
                            self._selenium_active_responses.add(request_id)
                            self._response_count += 1
                        self._selenium_response_estimates[request_id] = (
                            self._selenium_response_estimates.get(request_id, 0) + amount
                        )
                        self._download_bytes += amount
                elif method == "Network.loadingFinished":
                    encoded = _nonnegative_int(params.get("encodedDataLength"))
                    with self._lock:
                        estimate = self._selenium_response_estimates.pop(request_id, 0)
                        if request_id in self._selenium_active_responses:
                            self._selenium_active_responses.discard(request_id)
                            if encoded:
                                self._download_bytes += encoded - estimate
                            self._selenium_completed_responses[request_id] = encoded or estimate
                        elif encoded:
                            self._response_count += 1
                            self._download_bytes += encoded
                            self._selenium_completed_responses[request_id] = encoded
                        self._selenium_upload_estimates.pop(request_id, None)
                elif method == "Network.loadingFailed":
                    blocked = str(params.get("blockedReason") or params.get("errorText") or "").lower()
                    with self._lock:
                        upload_estimate = self._selenium_upload_estimates.pop(request_id, None)
                        if upload_estimate is not None and any(
                            token in blocked for token in ("blocked", "aborted", "inspector")
                        ):
                            self._upload_bytes -= upload_estimate
                            self._request_count = max(0, self._request_count - 1)
                        self._selenium_response_estimates.pop(request_id, None)
                        self._selenium_active_responses.discard(request_id)
                elif method in {"Network.webSocketFrameSent", "Network.webSocketFrameReceived"}:
                    frame = params.get("response") or {}
                    payload = frame.get("payloadData") or ""
                    opcode = _nonnegative_int(frame.get("opcode"))
                    if opcode == 2 and isinstance(payload, str):
                        try:
                            payload = base64.b64decode(payload, validate=True)
                        except Exception:
                            pass
                    self._record_websocket_frame(payload, sent=method.endswith("Sent"))
            except Exception:
                with self._lock:
                    self._errors += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            upload = int(self._upload_bytes)
            download = int(self._download_bytes)
            request_count = int(self._request_count)
            response_count = int(self._response_count)
            errors = int(self._errors)
        return {
            "request_bytes": upload,
            "upload_bytes": upload,
            "response_bytes": download,
            "download_bytes": download,
            "total_bytes": upload + download,
            "request_count": request_count,
            "response_count": response_count,
            "source": self.source,
            "measurement": "network_request_response_sizes",
            "scope": "browser_application_http_ws",
            "confidence": (
                "high" if self.source.startswith("playwright") and not errors
                else "medium" if not errors
                else "low"
            ),
            "measurement_errors": errors,
        }


def normalize_snapshot(value: Any) -> dict[str, Any]:
    """归一化 BrowserSession/浏览器性能采集结果，避免把无效值写入账号。"""
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key in (
        "request_bytes", "upload_bytes", "response_bytes", "download_bytes", "total_bytes",
        "request_count", "response_count", "resource_count", "measurement_errors",
    ):
        try:
            number = int(value.get(key) or 0)
        except (TypeError, ValueError):
            number = 0
        if number >= 0:
            out[key] = number
    request_bytes = max(out.get("request_bytes", 0), out.get("upload_bytes", 0))
    response_bytes = max(out.get("response_bytes", 0), out.get("download_bytes", 0))
    out["request_bytes"] = out["upload_bytes"] = request_bytes
    out["response_bytes"] = out["download_bytes"] = response_bytes
    has_directional = any(
        key in value for key in ("request_bytes", "upload_bytes", "response_bytes", "download_bytes")
    )
    supplied_total = out.get("total_bytes", 0)
    directional_total = request_bytes + response_bytes
    total = directional_total if has_directional and directional_total else supplied_total
    if not total:
        total = directional_total
    out["total_bytes"] = total
    source = str(value.get("source") or "").strip()
    if source:
        out["source"] = source[:80]
    measurement = str(value.get("measurement") or "").strip()
    if measurement:
        out["measurement"] = measurement[:80]
    for key in ("scope", "confidence"):
        text = str(value.get(key) or "").strip()
        if text:
            out[key] = text[:80]
    optimization = value.get("optimization")
    if isinstance(optimization, dict):
        # 只保留优化层的低敏诊断字段，不把 URL 规则或运行时对象写入账号。
        safe_optimization = {}
        for key in ("enabled", "method", "mode", "label", "blocked_pattern_count", "error"):
            item = optimization.get(key)
            if key == "enabled":
                safe_optimization[key] = bool(item)
            elif key == "blocked_pattern_count":
                try:
                    safe_optimization[key] = max(0, int(item or 0))
                except (TypeError, ValueError):
                    safe_optimization[key] = 0
            elif item is not None and str(item).strip():
                safe_optimization[key] = str(item).strip()[:240]
        if safe_optimization:
            out["optimization"] = safe_optimization
    return out if total > 0 or out.get("request_count", 0) or out.get("response_count", 0) else {}


def merge_snapshots(*values: Any, source: str | None = None) -> dict[str, Any]:
    """Sum independent task-attempt snapshots without losing directions."""
    snapshots = [normalize_snapshot(value) for value in values]
    snapshots = [value for value in snapshots if value]
    if not snapshots:
        return {}
    upload = sum(int(value.get("upload_bytes") or 0) for value in snapshots)
    download = sum(int(value.get("download_bytes") or 0) for value in snapshots)
    total = sum(int(value.get("total_bytes") or 0) for value in snapshots)
    download += max(0, total - upload - download)
    sources = {str(value.get("source") or "").strip() for value in snapshots}
    scopes = {str(value.get("scope") or "").strip() for value in snapshots}
    confidence_order = {"high": 3, "medium": 2, "low": 1}
    confidences = [str(value.get("confidence") or "").strip().lower() for value in snapshots]
    confidences = [value for value in confidences if value]
    merged = {
        "request_bytes": upload,
        "upload_bytes": upload,
        "response_bytes": download,
        "download_bytes": download,
        # Historical total-only snapshots remain additive even though their
        # direction is unknowable; all newly recorded snapshots equal up+down.
        "total_bytes": total,
        "request_count": sum(int(value.get("request_count") or 0) for value in snapshots),
        "response_count": sum(int(value.get("response_count") or 0) for value in snapshots),
        "measurement_errors": sum(int(value.get("measurement_errors") or 0) for value in snapshots),
        "source": (source or ", ".join(sorted(item for item in sources if item)))[:80],
        "measurement": "summed_task_attempts" if len(snapshots) > 1 else snapshots[0].get("measurement", ""),
        "scope": ", ".join(sorted(item for item in scopes if item))[:80],
        "confidence": min(confidences, key=lambda item: confidence_order.get(item, 0)) if confidences else "",
    }
    optimization = snapshots[-1].get("optimization")
    if isinstance(optimization, dict):
        merged["optimization"] = optimization
    return normalize_snapshot(merged)


def attach_optimization_snapshot(snapshot: dict[str, Any], target: Any) -> dict[str, Any]:
    """把 driver/page 的优化安装状态附加到流量快照。"""
    out = dict(snapshot or {}) if isinstance(snapshot, dict) else {}
    try:
        from core.traffic_optimizer import optimization_snapshot
        details = optimization_snapshot(target)
    except Exception:
        details = {}
    if details:
        out["optimization"] = details
    return out


def install_playwright_traffic_meter(
    context: Any, page: Any = None, *, source: str = "playwright_network"
) -> BrowserTrafficMeter:
    """Install one task-wide Playwright meter before the first navigation."""
    existing = getattr(context, "_registration_traffic_meter", None)
    if isinstance(existing, BrowserTrafficMeter):
        return existing
    meter = BrowserTrafficMeter(source)
    context.on("request", meter.record_playwright_request)
    context.on("requestfinished", meter.record_playwright_request_finished)
    context.on("requestfailed", meter.record_playwright_request_failed)
    try:
        setattr(context, "_registration_traffic_meter", meter)
    except Exception:
        pass
    pages = list(getattr(context, "pages", None) or ())
    if page is not None and page not in pages:
        pages.append(page)
    for existing_page in pages:
        meter.install_playwright_page(existing_page)
    try:
        context.on("page", meter.install_playwright_page)
    except Exception:
        pass
    return meter


def install_selenium_traffic_meter(driver: Any, *, source: str = "selenium_performance") -> BrowserTrafficMeter:
    """Attach a performance-log meter to a local or remote ChromeDriver."""
    existing = getattr(driver, "_registration_traffic_meter", None)
    if isinstance(existing, BrowserTrafficMeter):
        return existing
    meter = BrowserTrafficMeter(source)
    try:
        setattr(driver, "_registration_traffic_meter", meter)
    except Exception:
        pass
    # Discard attach/startup events already queued before the task meter was
    # installed, so an existing Roxy profile does not inflate registration.
    _drain_selenium_performance_logs(driver)
    return meter


def _drain_selenium_performance_logs(driver: Any) -> list[dict[str, Any]]:
    try:
        getter = getattr(driver, "get_log", None)
        if callable(getter):
            return list(getter("performance") or [])
        executor = getattr(driver, "execute", None)
        if callable(executor):
            payload = executor("getLog", {"type": "performance"}) or {}
            return list(payload.get("value") or []) if isinstance(payload, dict) else []
    except Exception:
        return []
    return []


def browser_performance_snapshot(target: Any) -> dict[str, Any]:
    """Read bidirectional browser bytes, with download-only fallback."""
    if target is None:
        return {}
    meter = getattr(target, "_registration_traffic_meter", None)
    if not isinstance(meter, BrowserTrafficMeter):
        context = getattr(target, "context", None)
        if callable(context):
            try:
                context = context()
            except Exception:
                context = None
        meter = getattr(context, "_registration_traffic_meter", None) if context is not None else None
    if isinstance(meter, BrowserTrafficMeter):
        if meter.source == "selenium_performance":
            meter.record_selenium_logs(_drain_selenium_performance_logs(target))
        measured = normalize_snapshot(meter.snapshot())
        if measured:
            return measured
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
    return normalize_snapshot({
        "request_bytes": 0,
        "upload_bytes": 0,
        "response_bytes": total,
        "download_bytes": total,
        "total_bytes": total,
        "request_count": 0,
        "response_count": len(rows),
        "resource_count": len(rows),
        "source": "browser_performance",
        "measurement": "transferSize" if used_transfer else "encodedBodySize",
        "scope": "browser_resource_timing",
        "confidence": "low",
    })
