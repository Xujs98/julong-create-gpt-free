# -*- coding: utf-8 -*-
"""账号查活后台队列：现有 AT 快速校验后，按独立配置选择协议或指纹浏览器。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from core import db
from core.account_liveness import check_account_liveness, log_path
from core.chatgpt_plan import check_account_plan, resolve_plan_check_route
from core.session_state import extract_saved_session

logger = logging.getLogger(__name__)

_WORKERS = 3
_QUEUE_LIMIT = 500
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="live-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING: set[int] = set()
_LOCK = threading.Lock()


def is_checking(email: str) -> bool:
    acc = db.get_account_by_email(email)
    if not acc:
        return False
    return str(acc.get("live_check_status") or "") in {"queued", "running"}


def _append_log(email: str, line: str, *, clear: bool = False) -> None:
    p = log_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    mode = "w" if clear else "a"
    with p.open(mode, encoding="utf-8") as f:
        f.write(f"{stamp} [INFO] {line}\n")


def _check_existing_access_token(
    account: dict,
    *,
    proxy: str | None,
    email: str,
    browser_fallback: bool = False,
    decision: dict | None = None,
) -> dict | None:
    """Return a liveness result, or None when an expired token requires re-login."""
    access_token = str(account.get("access_token") or "").strip()
    if not access_token:
        if decision is not None:
            decision["force_fresh_login"] = True
        _append_log(email, "[查活] 未保存 AT，进入重新登录刷新流程")
        return None

    _append_log(email, "[查活] 先校验现有 AT；有效时直接完成，不触发邮箱 OTP")
    checked = check_account_plan(access_token, proxy=proxy)
    if checked.get("ok"):
        saved = extract_saved_session(account) or {}
        if not list(saved.get("cookies") or []):
            _append_log(email, "[查活] 现有 AT 在线有效，但缺少可植入 Session Cookie，继续重新登录补全")
            return None
        plan_type = checked.get("current_plan_type") or account.get("plan_type")
        return {
            "ok": True,
            "status": "live",
            "checked_at": checked.get("checked_at") or datetime.now().isoformat(timespec="seconds"),
            "access_token": access_token,
            "session": {
                "user": {
                    "id": checked.get("user_id") or account.get("user_id"),
                    "name": checked.get("user_name") or account.get("user_name"),
                },
                "account": {"planType": plan_type},
            },
            "proxy_used": checked.get("proxy_used"),
            "check_method": "access_token",
        }

    if checked.get("needs_live_check") or checked.get("token_expired") or checked.get("http_status") == 401:
        if decision is not None:
            decision["force_fresh_login"] = True
        _append_log(email, "[查活] 现有 AT 已过期/失效，进入重新登录刷新流程")
        return None

    error = str(checked.get("error") or "现有 AT 在线校验失败")
    if browser_fallback and (
        checked.get("http_status") in {403, 429}
        or any(word in error.lower() for word in ("403", "429", "timeout", "connection", "proxy"))
    ):
        if decision is not None:
            decision["force_fresh_login"] = False
        _append_log(email, "[查活] 现有 AT 的协议在线校验受网络/CF 拦截，转由选定指纹浏览器确认")
        return None
    return {
        "ok": False,
        "status": "failed",
        "checked_at": checked.get("checked_at") or datetime.now().isoformat(timespec="seconds"),
        "error": f"AT 在线校验失败: {error}",
        "check_method": "access_token",
    }


def _run_live_check(*, account_id: int, email: str, proxy: str | None, trigger: str) -> dict:
    try:
        with _LOCK:
            _RUNNING.add(int(account_id))
        if not db.mark_account_live_check_running(account_id):
            _append_log(email, "[查活] 账号已删除或查活状态已被重置，取消执行")
            return {"ok": False, "status": "failed", "error": "账号已删除或查活状态已被重置"}
        route = resolve_plan_check_route(explicit_proxy=proxy)
        selected_proxy = route.get("proxy")
        from config import live_check as live_cfg
        selected_driver = str(getattr(live_cfg, "LIVE_CHECK_DRIVER", "cloak") or "cloak").strip().lower()
        if selected_driver not in {"protocol", "cloak", "roxy"}:
            raise RuntimeError(f"LIVE_CHECK_DRIVER 配置无效：{selected_driver}")
        selected_headless = bool(getattr(live_cfg, "LIVE_CHECK_HEADLESS", False))
        _append_log(
            email,
            "[查活] 开始后台执行 "
            f"trigger={trigger} network_route={route.get('network_route')} "
            f"proxy_mode={route.get('proxy_mode')} proxy_used={route.get('proxy_used') or '-'} "
            f"fallback_reason={route.get('proxy_fallback_reason') or '-'} "
            f"driver={selected_driver} headless={selected_headless if selected_driver != 'protocol' else '-'}"
        )
        account = db.get_account(account_id) or {}
        login_decision: dict = {}
        result = _check_existing_access_token(
            account,
            proxy=selected_proxy,
            email=email,
            browser_fallback=selected_driver != "protocol",
            decision=login_decision,
        )
        used_relogin = result is None
        if used_relogin:
            result = check_account_liveness(
                email,
                proxy=selected_proxy,
                clear_log=False,
                account=account,
                driver=selected_driver,
                headless=selected_headless,
                force_fresh_login=bool(login_decision.get("force_fresh_login")),
            )
        # 早期 providers/csrf 403 通常是该出口被 CF 拦截，不代表账号死亡。
        # auto/proxy 模式下如果用了代理，额外直连兜底一次，便于和套餐查询的 auto 语义保持接近。
        err_text = str(result.get("error") or "")
        if (
            not result.get("ok")
            and result.get("status") == "failed"
            and "403" in err_text
            and selected_proxy
            and used_relogin
            and str(route.get("proxy_mode") or "") == "auto"
            and str(route.get("network_route") or "") == "proxy"
        ):
            _append_log(email, "[查活] 代理出口收到 403，尝试直连兜底一次")
            result = check_account_liveness(
                email,
                proxy="",
                clear_log=False,
                account=account,
                driver=selected_driver,
                headless=selected_headless,
                force_fresh_login=bool(login_decision.get("force_fresh_login")),
            )
        db.update_account_liveness(account_id, result)
        if result.get("ok") and result.get("check_method") != "access_token":
            # 查活重新建立登录态后，旧套餐失败状态对应的是上一枚 AT。
            # 立即用新 AT 入队复查，表格会从“查询失败”切换为排队/真实套餐。
            from core import plan_check_service

            plan_queued = plan_check_service.enqueue_account_plan_check(
                account_id=account_id,
                email=email,
                access_token=str(result.get("access_token") or ""),
                trigger="live_check_refresh",
                proxy=selected_proxy,
            )
            if plan_queued.get("accepted"):
                _append_log(email, "[查活] 已使用刷新后的最新 AT 自动入队查询套餐")
            elif plan_queued.get("busy"):
                _append_log(email, "[查活] 套餐查询任务已在运行，将由查询线程读取最新 AT")
            else:
                _append_log(email, f"[查活] 自动查询套餐入队失败：{plan_queued.get('error') or '未知错误'}")
        if result.get("ok"):
            if result.get("check_method") == "access_token":
                _append_log(email, "[查活] 完成：账号正常，现有 AT 在线有效")
            elif result.get("check_method") == "session_cookie":
                _append_log(email, "[查活] 完成：账号正常，已用保存 Session 静默刷新最新 AT")
            elif result.get("check_method") in {"password", "password_totp"}:
                _append_log(email, "[查活] 完成：账号正常，已用协议账号密码/2FA 登录并刷新最新 AT")
            elif str(result.get("check_method") or "").endswith("_browser"):
                _append_log(email, f"[查活] 完成：账号正常，已用 {selected_driver} 指纹浏览器刷新最新 Session/AT")
            else:
                _append_log(email, "[查活] 完成：账号正常，已用旧账号邮箱验证码兜底并刷新最新 AT")
        elif result.get("status") == "deactivated":
            _append_log(email, f"[查活] 完成：账号已废 {result.get('error') or ''}")
        else:
            _append_log(email, f"[查活] 完成：失败 {result.get('error') or ''}")
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
        try:
            db.update_account_liveness(account_id, result)
        except Exception:
            logger.exception("[查活] 写入异常状态失败: account_id=%s", account_id)
        logger.exception("[查活] 后台异常: %s", email)
        try:
            _append_log(email, f"[查活] 后台异常：{result['error']}")
        except Exception:
            pass
        return result
    finally:
        with _LOCK:
            _RUNNING.discard(int(account_id))
        _QUEUE_SLOTS.release()


def enqueue_account_live_check(*, account_id: int, email: str, trigger: str = "manual", proxy: str | None = None) -> dict:
    account_id = int(account_id)
    email = str(email or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "查活队列已满，请稍后重试"}
    if not db.claim_account_live_check(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查活"}

    _append_log(email, f"[查活] 已入队 account_id={account_id} trigger={trigger}", clear=True)
    try:
        _EXECUTOR.submit(
            _run_live_check,
            account_id=account_id,
            email=email,
            proxy=proxy,
            trigger=str(trigger or "manual"),
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"查活入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_liveness(account_id, result)
        _append_log(email, result["error"])
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
    }


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
