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
from core.proxy_utils import masked_proxy_url, normalize_proxy_url
from core.session import BrowserSession
from core.session_state import extract_saved_session

logger = logging.getLogger(__name__)

_WORKERS = 2
_QUEUE_LIMIT = 100
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="twofa-setup")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def _resolve_twofa_proxy(account: dict, explicit_proxy: str | None) -> tuple[str | None, str]:
    """优先复用账号保存的可用出口，保持登录 Cookie 与设备环境连续。"""
    if explicit_proxy is not None:
        route = resolve_plan_check_route(explicit_proxy)
        return route.get("proxy"), "request"

    for key in ("live_check_proxy_used", "proxy_used"):
        saved_proxy = str((account or {}).get(key) or "").strip()
        if not saved_proxy:
            continue
        try:
            return normalize_proxy_url(saved_proxy, default_scheme="auto"), f"account:{key}"
        except ValueError:
            logger.warning("[2FA重设] 忽略账号保存的无效代理 %s=%s", key, masked_proxy_url(saved_proxy) or "***")

    route = resolve_plan_check_route(None)
    return route.get("proxy"), "plan_check_route"


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

        selected_proxy, proxy_source = _resolve_twofa_proxy(account, proxy)
        logger.info(
            "[2FA重设] 刷新登录态：proxy_source=%s proxy=%s（固定该出口，避免重设期间切换环境）",
            proxy_source,
            masked_proxy_url(selected_proxy) or "直连",
        )
        refreshed = check_account_liveness(
            email,
            proxy=selected_proxy,
            clear_log=False,
            account=account,
            preflight_attempts=1,
            rotate_proxy_on_retry=False,
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
        saved_device_id = str(latest.get("device_id") or "").strip()
        if saved_device_id:
            env.device_id = saved_device_id
        if not _restore_session_cookies(env, saved_session):
            raise RuntimeError("刷新登录态后未保存可用于 2FA 重认证的 Session Cookie")

        secret = setup_2fa(env, email)
        result = {
            "ok": True,
            "status": "success",
            "totp_secret": secret,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "trigger": trigger,
            "proxy_source": proxy_source,
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
