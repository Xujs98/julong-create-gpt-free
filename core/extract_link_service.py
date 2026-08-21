# -*- coding: utf-8 -*-
"""通用 Plus 试用提链后台队列。"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:  # pragma: no cover - WebUI 环境未装依赖时标准库兜底
    curl_requests = None

from config import extract_link as cfg
from core import db, extract_link_registry, pp_extract_protocol

logger = logging.getLogger(__name__)


def _runtime_setting(name: str, default=None):
    try:
        from config.env_loader import load_env
        load_env(override=True)
    except Exception:
        pass
    raw = os.getenv(name)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    return getattr(cfg, name, default)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(_runtime_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _bool_setting(name: str, default: bool) -> bool:
    value = _runtime_setting(name, default)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


_EXECUTOR = ThreadPoolExecutor(max_workers=20, thread_name_prefix="extract-link")
_QUEUE_LOCK = threading.Lock()
_QUEUED_TASKS = 0
_WORKER_COND = threading.Condition()
_ACTIVE_WORKERS = 0
_ACTIVE_BY_SERVICE: dict[str, int] = {}


def queue_settings() -> dict:
    return {
        "workers": _int_setting("EXTRACT_LINK_WORKERS", 10, 1, 20),
        "queue_limit": _int_setting("EXTRACT_LINK_QUEUE_LIMIT", 500, 1, 5000),
        "retries": _int_setting("EXTRACT_LINK_RETRIES", 5, 0, 30),
    }


def _try_acquire_queue_slot() -> bool:
    """按当前配置动态限制排队中和运行中的提链任务总数。"""
    global _QUEUED_TASKS
    limit = queue_settings()["queue_limit"]
    with _QUEUE_LOCK:
        if _QUEUED_TASKS >= limit:
            return False
        _QUEUED_TASKS += 1
        return True


def _release_queue_slot() -> None:
    global _QUEUED_TASKS
    with _QUEUE_LOCK:
        _QUEUED_TASKS = max(0, _QUEUED_TASKS - 1)


def _acquire_worker_slot(global_limit: int, service_key: str, service_limit: int) -> None:
    global _ACTIVE_WORKERS
    with _WORKER_COND:
        while (
            _ACTIVE_WORKERS >= max(1, global_limit)
            or _ACTIVE_BY_SERVICE.get(service_key, 0) >= max(1, service_limit)
        ):
            _WORKER_COND.wait(timeout=1.0)
        _ACTIVE_WORKERS += 1
        _ACTIVE_BY_SERVICE[service_key] = _ACTIVE_BY_SERVICE.get(service_key, 0) + 1


def _release_worker_slot(service_key: str) -> None:
    global _ACTIVE_WORKERS
    with _WORKER_COND:
        _ACTIVE_WORKERS = max(0, _ACTIVE_WORKERS - 1)
        remaining = max(0, _ACTIVE_BY_SERVICE.get(service_key, 0) - 1)
        if remaining:
            _ACTIVE_BY_SERVICE[service_key] = remaining
        else:
            _ACTIVE_BY_SERVICE.pop(service_key, None)
        _WORKER_COND.notify_all()


def _session():
    if curl_requests is None:
        return None
    return curl_requests.Session(impersonate="chrome")


def _api_service(service_id: str | None = None) -> dict:
    if service_id:
        service = extract_link_registry.get_service(service_id, mode="api")
    else:
        service = extract_link_registry.resolve_service(mode="api")
    if not service:
        raise ValueError("未找到提链 API 服务")
    return service


def query_cdk(*, cdk: str | None = None, service_id: str | None = None) -> dict:
    service = _api_service(service_id)
    base = str(service.get("api_base") or "").rstrip("/")
    code = str(cdk or service.get("cdk") or "").strip()
    if not base or not code:
        raise ValueError("提链 API 服务地址或 CDK 为空")
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    session = _session()
    try:
        if session is None:
            req = Request(f"{base}/api/cdk?{urlencode({'code': code})}", headers={"Accept": "application/json"})
            with urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            return payload if isinstance(payload, dict) else {}
        resp = session.get(f"{base}/api/cdk?{urlencode({'code': code})}", timeout=timeout)
        try:
            payload = resp.json()
        except Exception:
            payload = {"error": (resp.text or "")[:300]}
        if not 200 <= resp.status_code < 300:
            raise RuntimeError(payload.get("error") or f"HTTP {resp.status_code}")
        return payload if isinstance(payload, dict) else {}
    finally:
        try:
            session.close()
        except Exception:
            pass


def _create_api_job(*, token: str, service: dict) -> dict:
    base = str(service.get("api_base") or "").rstrip("/")
    cdk = str(service.get("cdk") or "").strip()
    link_type = str(service.get("link_type") or "pix").strip().lower()
    if not base or not cdk:
        raise ValueError("提链 API 服务地址或 CDK 为空")
    if link_type not in extract_link_registry.SUPPORTED_API_LINK_TYPES:
        raise ValueError("提链类型无效，仅支持 pix / upi / kakao_pay / ideal")
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    payload = {"link_type": link_type, "cdk": cdk, "token": token}
    session = _session()
    try:
        if session is None:
            req = Request(
                f"{base}/api/extract",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        else:
            resp = session.post(f"{base}/api/extract", json=payload, timeout=timeout)
            try:
                data = resp.json()
            except Exception:
                data = {"error": (resp.text or "")[:300]}
            if not 200 <= resp.status_code < 300:
                raise RuntimeError(data.get("error") or f"HTTP {resp.status_code}")
        if not isinstance(data, dict) or not data.get("job_id"):
            raise RuntimeError(f"提链服务未返回 job_id: {data}")
        return data
    finally:
        try:
            session.close()
        except Exception:
            pass


def _iter_api_events(*, job_id: str, service: dict):
    base = str(service.get("api_base") or "").rstrip("/")
    cdk = str(service.get("cdk") or "").strip()
    timeout = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 180, 30, 900)
    url = f"{base}/api/jobs/{quote(job_id, safe='')}/events?{urlencode({'cdk': cdk})}"
    session = _session()
    try:
        if session is None:
            req = Request(url, headers={"Accept": "text/event-stream"})
            with urlopen(req, timeout=timeout) as resp:
                lines = resp
                yield from _parse_sse_lines(lines)
            return
        resp = session.get(url, timeout=timeout, stream=True)
        if not 200 <= resp.status_code < 300:
            raise RuntimeError(f"监听提链事件失败 HTTP {resp.status_code}: {(resp.text or '')[:300]}")
        yield from _parse_sse_lines(resp.iter_lines())
    finally:
        try:
            session.close()
        except Exception:
            pass


def _parse_sse_lines(lines):
    event = "message"
    data_lines: list[str] = []
    for raw in lines:
        if raw is None:
            continue
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        line = line.rstrip("\r\n")
        if line == "":
            if data_lines:
                text = "\n".join(data_lines)
                try:
                    data = json.loads(text)
                except Exception:
                    data = {"raw": text}
                yield event, data
            event, data_lines = "message", []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
    if data_lines:
        text = "\n".join(data_lines)
        try:
            data = json.loads(text)
        except Exception:
            data = {"raw": text}
        yield event, data


def _extract_error_message(data) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return str(data)
    error = data.get("error")
    if isinstance(error, dict):
        for key in ("message", "detail", "reason", "error", "msg", "description"):
            if error.get(key):
                return str(error[key]).strip()
    elif error:
        return str(error).strip()
    for key in ("message", "detail", "reason", "msg", "description", "raw"):
        if data.get(key):
            return str(data[key]).strip()
    return json.dumps(data, ensure_ascii=False)[:500]


def _run_api_once(*, access_token: str, service: dict, account_id: int) -> dict:
    logs: list[str] = []
    job = _create_api_job(token=access_token, service=service)
    job_id = str(job.get("job_id") or "")
    db.update_account_extract(account_id, {
        "ok": False,
        "status": "running",
        "job_id": job_id,
        "link_type": service.get("link_type"),
        "message": "API 提链任务已创建，等待结果",
        "progress": 20,
        "cdk_remaining": job.get("cdk_remaining"),
    })
    last_event = None
    for event, data in _iter_api_events(job_id=job_id, service=service):
        last_event = {"event": event, "data": data}
        if event == "log":
            message = str((data or {}).get("message") or "")[:300]
            if message:
                logs.append(message)
                progress = int((data or {}).get("progress") or min(90, 25 + len(logs) * 8))
                db.update_account_extract(account_id, {
                    "ok": False, "status": "running", "job_id": job_id,
                    "link_type": service.get("link_type"), "message": message, "progress": progress,
                })
        elif event == "result":
            result = (data or {}).get("result") if isinstance(data, dict) else None
            if not isinstance(result, dict):
                result = {}
            return {"job_id": job_id, "result": result, "logs": logs}
        elif event == "error":
            raise RuntimeError(_extract_error_message(data) or "提链 API 任务失败")
        elif event == "done":
            break
    raise RuntimeError(f"提链事件流结束但未返回 result: {last_event}")


def _run_protocol_once(*, access_token: str, service: dict, account_id: int) -> dict:
    def progress(message: str, percent: int) -> None:
        db.update_account_extract(account_id, {
            "ok": False,
            "status": "running",
            "link_type": "paypal",
            "message": message,
            "progress": percent,
        })

    result = pp_extract_protocol.extract_pp_link(
        access_token,
        billing_country=str(_runtime_setting("EXTRACT_LINK_BILLING_COUNTRY", "GB") or "GB"),
        payment_method=str(_runtime_setting("EXTRACT_LINK_PAYMENT_METHOD", "paypal") or "paypal"),
        auto_enter_paypal=_bool_setting("EXTRACT_LINK_AUTO_ENTER_PAYPAL", True),
        checkout_update=_bool_setting("EXTRACT_LINK_CHECKOUT_UPDATE", True),
        timeout=float(_int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)),
        progress=progress,
    )
    return {"job_id": f"pp-{account_id}-{int(time.time())}", "result": result, "logs": []}


def _validate_extract_result(result: dict | None) -> dict:
    payload = result if isinstance(result, dict) else {}
    link_fields = (
        "long_url", "copy_paste", "paypal_authorize_url", "hosted_checkout_url",
        "image_url_png", "image_url_svg",
    )
    if not any(str(payload.get(key) or "").strip() for key in link_fields):
        raise RuntimeError("提链服务返回成功，但结果中没有可复制的链接或二维码")
    return payload


def _run_extract(*, account_id: int, email: str, access_token: str, service: dict, trigger: str) -> dict:
    global_limit = _int_setting("EXTRACT_LINK_WORKERS", 10, 1, 20)
    service_limit = int(service.get("workers") or global_limit) if service.get("mode") == "api" else global_limit
    service_key = f"{service.get('mode') or 'unknown'}:{service.get('id') or 'default'}"
    service_limit = max(1, min(service_limit, 20))
    _acquire_worker_slot(global_limit, service_key, service_limit)
    try:
        if not db.mark_account_extract_running(account_id):
            return {"ok": False, "error": "账号已删除或提链状态已被重置"}
        retries = _int_setting("EXTRACT_LINK_RETRIES", 5, 0, 30)
        attempts = retries + 1
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                db.update_account_extract(account_id, {
                    "ok": False,
                    "status": "running",
                    "message": f"{service.get('name')}：第 {attempt}/{attempts} 次尝试",
                    "progress": 5,
                    "service_id": service.get("id"),
                    "service_name": service.get("name"),
                    "mode": service.get("mode"),
                })
                if service.get("mode") == "protocol":
                    output = _run_protocol_once(access_token=access_token, service=service, account_id=account_id)
                else:
                    output = _run_api_once(access_token=access_token, service=service, account_id=account_id)
                extract_result = _validate_extract_result(output.get("result"))
                final = {
                    "ok": True,
                    "status": "success",
                    "job_id": output.get("job_id"),
                    "link_type": service.get("link_type") or service.get("protocol") or "paypal",
                    "service_id": service.get("id"),
                    "service_name": service.get("name"),
                    "mode": service.get("mode"),
                    "result": extract_result,
                    "logs": output.get("logs") or [],
                    "message": "提链成功",
                    "progress": 100,
                }
                db.update_account_extract(account_id, final)
                logger.info("[提链] 成功: %s service=%s", email, service.get("name"))
                return final
            except Exception as exc:
                last_exc = exc
                retryable = bool(getattr(exc, "retryable", True))
                if attempt >= attempts or not retryable:
                    break
                wait_seconds = min(10.0, 0.8 * attempt)
                db.update_account_extract(account_id, {
                    "ok": False,
                    "status": "running",
                    "message": f"第 {attempt} 次失败，{wait_seconds:.1f}s 后重试：{str(exc)[:180]}",
                    "progress": min(85, 8 + attempt * 10),
                })
                time.sleep(wait_seconds)
        reason = f"{type(last_exc).__name__}: {str(last_exc)}" if last_exc else "提链失败"
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": reason[:500],
            "message": reason[:500],
            "progress": 100,
            "service_id": service.get("id"),
            "service_name": service.get("name"),
            "mode": service.get("mode"),
        }
        db.update_account_extract(account_id, result)
        logger.warning("[提链] 失败: %s service=%s error=%s", email, service.get("name"), reason[:240])
        return result
    finally:
        _release_worker_slot(service_key)
        _release_queue_slot()


def enqueue_account_extract(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str = "manual",
    link_type: str | None = None,
    cdk: str | None = None,
    mode: str | None = None,
    provider: str | None = None,
) -> dict:
    if not _try_acquire_queue_slot():
        return {"accepted": False, "busy": False, "error": "提链队列已满"}
    claimed = False
    try:
        service = dict(extract_link_registry.resolve_service(mode=mode, provider=provider))
        if service.get("mode") == "api":
            if link_type:
                service["link_type"] = str(link_type).strip().lower()
            if cdk:
                service["cdk"] = str(cdk).strip()
        if not db.claim_account_extract(
            account_id,
            trigger=trigger,
            link_type=str(service.get("link_type") or service.get("protocol") or "paypal"),
            service_id=str(service.get("id") or ""),
            service_name=str(service.get("name") or ""),
            mode=str(service.get("mode") or ""),
        ):
            _release_queue_slot()
            return {"accepted": False, "busy": True, "error": "该账号正在提链中"}
        claimed = True
        future = _EXECUTOR.submit(
            _run_extract,
            account_id=account_id,
            email=email,
            access_token=access_token,
            service=service,
            trigger=trigger,
        )
        return {
            "accepted": True,
            "busy": False,
            "future": future,
            "link_type": service.get("link_type") or service.get("protocol"),
            "mode": service.get("mode"),
            "provider": service.get("id"),
            "service_name": service.get("name"),
        }
    except Exception as exc:
        if claimed:
            db.update_account_extract(account_id, {
                "ok": False,
                "status": "failed",
                "error": f"提链任务提交失败: {type(exc).__name__}: {str(exc)[:300]}",
                "message": "提链任务提交失败",
                "progress": 100,
                "service_id": service.get("id") if "service" in locals() else "",
                "service_name": service.get("name") if "service" in locals() else "",
                "mode": service.get("mode") if "service" in locals() else "",
            })
        _release_queue_slot()
        raise
