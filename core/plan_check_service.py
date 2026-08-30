# -*- coding: utf-8 -*-
"""套餐/Plus 资格查询后台队列。"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from config import proxy as proxy_cfg
from core import db
from core.chatgpt_plan import check_account_plan
from core.session_state import extract_saved_session

logger = logging.getLogger(__name__)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(proxy_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(getattr(proxy_cfg, name, default) or 0.0)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


_WORKERS = _int_setting("PLAN_CHECK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("PLAN_CHECK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="plan-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _wait_for_rate_slot() -> None:
    """为所有查询线程分配错开的请求启动时间。"""
    global _NEXT_REQUEST_AT
    min_interval = _float_setting("PLAN_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0)
    jitter = _float_setting("PLAN_CHECK_JITTER", 0.3, 0.0, 30.0)
    with _RATE_LOCK:
        now = time.monotonic()
        scheduled = max(now, _NEXT_REQUEST_AT) + (random.uniform(0.0, jitter) if jitter else 0.0)
        _NEXT_REQUEST_AT = scheduled + min_interval
    wait_seconds = scheduled - now
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def _registration_recheck_delay() -> float:
    return _float_setting("PLAN_CHECK_REGISTRATION_RECHECK_DELAY", 2.0, 0.0, 30.0)


def _check_plan_with_account_context(
    account: dict,
    token: str,
    *,
    proxy: str | None,
    timezone_offset_min: str,
    check_oaics: bool = True,
    check_country_qualification: bool = False,
    max_attempts: int | None = None,
) -> dict:
    """使用账号保存的 device_id 与 Session Cookie 查询套餐，避免随机新环境触发 401。"""
    saved_session = extract_saved_session(account) or {}
    # Checkout 风控会把保存的 cf_clearance / __cf_bm 与出口 IP 绑定。
    # OAICS 自动查询未显式指定代理时，优先复用注册时的完整代理会话，
    # 避免“Cookie 来自出口 A、Checkout 从出口 B 发出”而返回 unusual activity。
    effective_proxy = proxy
    preserve_proxy_session = False
    if (check_oaics or check_country_qualification) and effective_proxy is None:
        saved_proxy = str(account.get("proxy_used") or "").strip()
        if saved_proxy:
            effective_proxy = saved_proxy
            preserve_proxy_session = True
    return check_account_plan(
        token,
        proxy=effective_proxy,
        timezone_offset_min=timezone_offset_min,
        max_attempts=max_attempts,
        account_id=str(account.get("account_id") or "") or None,
        device_id=str(account.get("device_id") or "") or None,
        session_cookies=list(saved_session.get("cookies") or []),
        billing_country=str(account.get("proxy_country_code") or ""),
        check_oaics=bool(check_oaics),
        check_country_qualification=bool(check_country_qualification),
        preserve_proxy_session=preserve_proxy_session,
    )


def _refresh_login_for_plan(account: dict, *, proxy: str | None) -> dict | None:
    """套餐接口认证失败时，对有保存密码的账号执行一次协议查活并返回最新登录态。"""
    if not str(account.get("registration_password") or "").strip():
        return None
    from core.account_liveness import check_account_liveness

    email = str(account.get("email") or "").strip()
    logger.info("[Plan] 套餐接口认证失败，使用保存密码协议刷新登录态后重试：%s", email)
    result = check_account_liveness(
        email,
        proxy=proxy,
        clear_log=False,
        account=account,
    )
    # 套餐查询只是查活之后的附加任务。协议刷新若被 CF/网络拦截，不应把
    # 已经由指纹浏览器确认成功的查活状态覆盖成失败；仅成功刷新时同步登录态。
    if result.get("ok"):
        db.update_account_liveness(int(account.get("id") or 0), result)
    return result


def _finalize_country_qualification_result(
    result: dict | None,
    *,
    enabled: bool,
) -> dict:
    """Close a country-qualification task when the shared plan request fails.

    The country check runs after ``accounts/check`` succeeds.  A transport
    failure (for example a rejected SOCKS5 credential) can therefore return
    before the qualification engine adds its own fields.  The queue has
    already marked the row as ``running`` at that point, so persist an
    explicit terminal failure instead of leaving the UI spinner forever.
    """
    normalized = dict(result or {})
    if not enabled or "country_qualification_status" in normalized:
        return normalized
    checked_at = str(normalized.get("checked_at") or datetime.now().isoformat(timespec="seconds"))
    error = str(normalized.get("error") or "套餐请求失败，未执行各国资格检测")
    normalized.update({
        "country_qualification_status": "failed",
        "country_qualification_checked_at": checked_at,
        "country_qualification_http_status": normalized.get("http_status"),
        "country_qualification_error": error[:240],
        "country_qualification_results": [],
        "country_qualification_query_count": 0,
        "country_qualification_eligible": None,
        "country_qualification_source": "qualification-test",
    })
    return normalized


def _run_plan_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None,
    timezone_offset_min: str,
    check_oaics: bool = False,
    check_country_qualification: bool = False,
) -> dict:
    try:
        if not db.mark_account_plan_check_running(account_id):
            return {"ok": False, "error": "账号已删除或套餐查询状态已被重置"}

        # 任务可能在队列中等待；执行时重新读取数据库，始终使用查活刚刷新的最新 AT。
        account = db.get_account(account_id) or {}
        current_token = str(account.get("access_token") or access_token or "").strip()
        _wait_for_rate_slot()
        result = _check_plan_with_account_context(
            account,
            current_token,
            proxy=proxy,
            timezone_offset_min=timezone_offset_min,
            check_oaics=check_oaics,
            check_country_qualification=check_country_qualification,
        )

        if result.get("needs_live_check") or result.get("token_expired") is True:
            # 查活与套餐查询并发时，先检查数据库是否已经写入另一枚新 AT。
            latest_account = db.get_account(account_id) or account
            latest_token = str(latest_account.get("access_token") or "").strip()
            if latest_token and latest_token != current_token:
                logger.info("[Plan] 检测到查活已写入新 AT，直接使用最新 AT 重试：%s", email)
                _wait_for_rate_slot()
                result = _check_plan_with_account_context(
                    latest_account,
                    latest_token,
                    proxy=proxy,
                    timezone_offset_min=timezone_offset_min,
                    check_oaics=check_oaics,
                    check_country_qualification=check_country_qualification,
                    max_attempts=1,
                )
                current_token = latest_token
                account = latest_account

        if (
            trigger != "live_check_refresh"
            and (result.get("needs_live_check") or result.get("token_expired") is True)
        ):
            live_result = _refresh_login_for_plan(account, proxy=proxy)
            if live_result and live_result.get("ok") and live_result.get("access_token"):
                refreshed_account = db.get_account(account_id) or {
                    **account,
                    "access_token": live_result.get("access_token"),
                    "device_id": live_result.get("device_id") or account.get("device_id"),
                }
                _wait_for_rate_slot()
                result = _check_plan_with_account_context(
                    refreshed_account,
                    str(live_result.get("access_token") or ""),
                    proxy=proxy,
                    timezone_offset_min=timezone_offset_min,
                    check_oaics=check_oaics,
                    check_country_qualification=check_country_qualification,
                    max_attempts=1,
                )
                result["live_refresh_performed"] = True
                current_token = str(live_result.get("access_token") or "")
                account = refreshed_account
            elif live_result:
                result["live_refresh_performed"] = True
                result["live_refresh_error"] = live_result.get("error") or "协议刷新登录态失败"
        elif trigger == "live_check_refresh" and (
            result.get("needs_live_check") or result.get("token_expired") is True
        ):
            # 当前 AT 刚由查活线程在真实浏览器内校验并保存，再发起协议登录只会
            # 造成重复登录，并可能用网络 403 覆盖真实的浏览器查活成功状态。
            result["live_refresh_skipped"] = "fresh_browser_session"

        recheck_delay = _registration_recheck_delay()
        should_recheck = (
            trigger == "registration_auto"
            and recheck_delay > 0
            and bool(result.get("ok"))
            and str(result.get("current_plan_type") or "").lower() == "free"
            and not bool(result.get("plus_trial_eligible"))
        )
        if should_recheck:
            logger.info("[Plan] 新账号暂未发现 Plus 试用资格，%.1fs 后复查一次: %s", recheck_delay, email)
            time.sleep(recheck_delay)
            _wait_for_rate_slot()
            recheck_result = _check_plan_with_account_context(
                account,
                current_token,
                proxy=proxy,
                timezone_offset_min=timezone_offset_min,
                check_oaics=bool(check_oaics),
                check_country_qualification=bool(check_country_qualification),
                max_attempts=1,
            )
            if recheck_result.get("ok"):
                result = recheck_result
            else:
                logger.warning(
                    "[Plan] 新账号资格复查失败，保留首次成功结果: %s, %s",
                    email,
                    recheck_result.get("error") or "未知错误",
                )

        result = _finalize_country_qualification_result(
            result,
            enabled=bool(check_country_qualification),
        )
        db.update_account_plan_check(acc_id=account_id, result=result)
        if result.get("ok"):
            logger.info(
                "[Plan] 后台查询成功: %s, plan=%s, plus_trial=%s, trigger=%s",
                email,
                result.get("current_plan_type") or "unknown",
                bool(result.get("plus_trial_eligible")),
                trigger,
            )
        else:
            logger.warning(
                "[Plan] 后台查询失败: %s, trigger=%s, error=%s",
                email,
                trigger,
                result.get("error") or "未知错误",
            )
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
        result = _finalize_country_qualification_result(
            result,
            enabled=bool(check_country_qualification),
        )
        try:
            db.update_account_plan_check(acc_id=account_id, result=result)
        except Exception:
            logger.exception("[Plan] 写入后台查询异常状态失败: account_id=%s", account_id)
        logger.exception("[Plan] 后台查询异常: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_plan_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None = None,
    timezone_offset_min: str = "-",
    check_oaics: bool = False,
    check_country_qualification: bool = False,
) -> dict:
    """把查询放入统一线程池；重复查询或队列满时不提交。"""
    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    if not access_token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "套餐查询队列已满，请稍后重试"}

    if not db.claim_account_plan_check(
        acc_id=account_id,
        trigger=trigger,
        country_qualification=bool(check_country_qualification),
    ):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查询套餐"}

    try:
        _EXECUTOR.submit(
            _run_plan_check,
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=str(trigger or "manual"),
            proxy=proxy,
            timezone_offset_min=str(timezone_offset_min or "-"),
            check_oaics=bool(check_oaics),
            check_country_qualification=bool(check_country_qualification),
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"套餐查询入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_plan_check(acc_id=account_id, result=result)
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
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "min_interval": _float_setting("PLAN_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0),
        "jitter": _float_setting("PLAN_CHECK_JITTER", 0.3, 0.0, 30.0),
    }
