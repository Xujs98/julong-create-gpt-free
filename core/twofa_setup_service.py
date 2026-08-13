# -*- coding: utf-8 -*-
"""已注册账号的 2FA（TOTP）重设后台队列。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from core import db
from core.account_export import setup_2fa
from core.account_liveness import check_account_liveness
from core.chatgpt_plan import resolve_plan_check_route
from core.fingerprint_profile import session_fingerprint_kwargs
from core.session import BrowserSession
from core.session_state import extract_saved_session

logger = logging.getLogger(__name__)

_WORKERS = 2
_QUEUE_LIMIT = 100
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="twofa-setup")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def _restore_session_cookies(env: BrowserSession, saved_session: dict | None) -> int:
    restored = 0
    for item in list((saved_session or {}).get("cookies") or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or ".chatgpt.com")
        path = str(item.get("path") or "/") or "/"
        env.session.cookies.set(name, value, domain=domain, path=path, secure=bool(item.get("secure")))
        if name == "oai-did" and value:
            env.device_id = value
        restored += 1
    return restored


def _run_twofa_setup(*, account_id: int, email: str, proxy: str | None, trigger: str) -> dict:
    env: BrowserSession | None = None
    try:
        if not db.mark_account_twofa_setup_running(account_id):
            return {"ok": False, "status": "failed", "error": "账号不存在或 2FA 重设状态已重置"}
        account = db.get_account(account_id) or {}
        if str(account.get("totp_secret") or "").strip():
            result = {"ok": True, "status": "success", "message": "账号已有可用 2FA，无需重新设置"}
            db.update_account_twofa_setup(account_id, result)
            return result
        if not str(account.get("registration_password") or "").strip():
            raise RuntimeError("账号未保存注册密码，无法自动完成 2FA 重认证")

        route = resolve_plan_check_route(proxy)
        selected_proxy = route.get("proxy")
        refreshed = check_account_liveness(
            email,
            proxy=selected_proxy,
            clear_log=False,
            account=account,
        )
        db.update_account_liveness(account_id, refreshed)
        if not refreshed.get("ok"):
            raise RuntimeError(refreshed.get("error") or "刷新登录态失败")

        latest = db.get_account(account_id) or account
        saved_session = extract_saved_session(latest) or refreshed.get("session") or {}
        env = BrowserSession(
            proxy=selected_proxy,
            detect_exit_geo=False,
            **session_fingerprint_kwargs(email),
        )
        if not _restore_session_cookies(env, saved_session):
            raise RuntimeError("刷新登录态后未保存可用于 2FA 重认证的 Session Cookie")

        secret = setup_2fa(env, email)
        result = {
            "ok": True,
            "status": "success",
            "totp_secret": secret,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "trigger": trigger,
        }
        db.update_account_twofa_setup(account_id, result)
        logger.info("[2FA重设] 成功: %s", email)
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:360]}",
            "trigger": trigger,
        }
        try:
            db.update_account_twofa_setup(account_id, result)
        except Exception:
            logger.exception("[2FA重设] 写入失败状态异常: account_id=%s", account_id)
        logger.exception("[2FA重设] 失败: %s", email)
        return result
    finally:
        if env is not None:
            try:
                env.session.close()
            except Exception:
                pass
        _QUEUE_SLOTS.release()


def enqueue_account_twofa_setup(*, account_id: int, email: str, trigger: str = "manual", proxy: str | None = None) -> dict:
    """把账号 2FA 重设任务放入后台队列。"""
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "2FA 重设队列已满，请稍后重试"}
    try:
        if not db.claim_account_twofa_setup(account_id, trigger=trigger):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在设置 2FA"}
        _EXECUTOR.submit(
            _run_twofa_setup,
            account_id=int(account_id),
            email=str(email or "").strip(),
            proxy=proxy,
            trigger=str(trigger or "manual"),
        )
        return {"accepted": True, "busy": False, "account_id": int(account_id), "email": email, "status": "queued"}
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {"ok": False, "status": "failed", "error": f"2FA 重设入队失败: {type(exc).__name__}: {str(exc)[:180]}"}
        db.update_account_twofa_setup(int(account_id), result)
        return {"accepted": False, "busy": False, "error": result["error"]}


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
