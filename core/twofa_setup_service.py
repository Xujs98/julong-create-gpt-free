# -*- coding: utf-8 -*-
"""已注册账号的 2FA（TOTP）重设后台队列。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from core import db
from core.account_export import describe_twofa_error, setup_2fa
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
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"
_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"twofa-reset-{safe}.log"


def is_setting(email: str) -> bool:
    key = str(email or "").strip().lower()
    with _RUNNING_LOCK:
        return key in _RUNNING


def _append_log(email: str, message: str, *, clear: bool = False) -> None:
    path = log_path(email)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if clear else "a"
    stamp = datetime.now().strftime("%H:%M:%S")
    with path.open(mode, encoding="utf-8") as handle:
        handle.write(f"{stamp} [INFO] {message}\n")


def _masked_proxy_value(proxy: str | None) -> bool:
    value = str(proxy or "").strip().lower()
    return "***" in value or "%2a%2a%2a" in value


def _retryable_route_error(value) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in (
        "proxy", "socks", "curl: (97)", "connection", "connect", "timeout", "timed out",
        "403", "407", "429", "502", "503", "504", "network",
    ))


def _resolve_twofa_proxy(account: dict, explicit_proxy: str | None) -> tuple[str | None, str]:
    """优先复用账号保存的可用出口，保持登录 Cookie 与设备环境连续。"""
    if explicit_proxy is not None:
        if _masked_proxy_value(explicit_proxy):
            raise ValueError("请求中的代理是脱敏展示值，缺少真实认证信息")
        route = resolve_plan_check_route(explicit_proxy)
        return route.get("proxy"), "request"

    for key in ("live_check_proxy_used", "proxy_used"):
        saved_proxy = str((account or {}).get(key) or "").strip()
        if not saved_proxy:
            continue
        if _masked_proxy_value(saved_proxy):
            logger.warning("[2FA重设] 忽略账号保存的脱敏代理 %s=%s", key, saved_proxy)
            continue
        try:
            return normalize_proxy_url(saved_proxy, default_scheme="auto"), f"account:{key}"
        except ValueError:
            logger.warning("[2FA重设] 忽略账号保存的无效代理 %s=%s", key, masked_proxy_url(saved_proxy) or "***")

    route = resolve_plan_check_route(None)
    return route.get("proxy"), "plan_check_route"


def _resolve_twofa_routes(account: dict, explicit_proxy: str | None) -> list[tuple[str | None, str]]:
    """生成 2FA 重设出口顺序：账号出口 → 直连 → 代理池出口。"""
    selected_proxy, selected_source = _resolve_twofa_proxy(account, explicit_proxy)
    routes: list[tuple[str | None, str]] = [(selected_proxy, selected_source)]

    # 账号保存的代理通常能保持设备/IP 连续性，但历史代理可能已失效；
    # 直连和代理池出口都保留为后续独立尝试。
    if selected_proxy:
        routes.append((None, "direct_fallback"))

    try:
        pool_route = resolve_plan_check_route(None)
        pool_proxy = pool_route.get("proxy")
    except Exception as exc:
        pool_proxy = None
        logger.info("[2FA重设] 解析代理池出口失败，跳过 proxy_pool：%s", str(exc)[:180])
    if pool_proxy:
        normalized_pool = str(pool_proxy).strip()
        duplicate = any(str(route_proxy or "").strip() == normalized_pool for route_proxy, _ in routes)
        if not duplicate:
            routes.append((pool_proxy, "proxy_pool"))
    return routes


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
    file_handler: logging.FileHandler | None = None
    root_logger = logging.getLogger()
    thread_name = threading.current_thread().name
    key = str(email or "").strip().lower()
    route_attempts = 0
    try:
        path = log_path(email)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(path), encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
        file_handler.addFilter(lambda record: record.threadName == thread_name)
        root_logger.addHandler(file_handler)
        with _RUNNING_LOCK:
            _RUNNING.add(key)
        logger.info("[2FA重设] 开始：account_id=%s email=%s trigger=%s", account_id, email, trigger)
        if not db.mark_account_twofa_setup_running(account_id):
            return {"ok": False, "status": "failed", "error": "账号不存在或 2FA 重设状态已重置"}
        account = db.get_account(account_id) or {}
        if str(account.get("totp_secret") or "").strip():
            result = {"ok": True, "status": "success", "message": "账号已有可用 2FA，无需重新设置"}
            db.update_account_twofa_setup(account_id, result)
            return result
        if not str(account.get("registration_password") or "").strip():
            raise RuntimeError("账号未保存注册密码，无法自动完成 2FA 重认证")

        route_candidates = _resolve_twofa_routes(account, proxy)

        secret = ""
        effective_proxy: str | None = route_candidates[0][0] if route_candidates else None
        effective_source = route_candidates[0][1] if route_candidates else "direct"
        last_error: BaseException | None = None
        for route_index, (route_proxy, route_source) in enumerate(route_candidates, start=1):
            route_attempts = route_index
            logger.info(
                "[2FA重设] 登录态与 TOTP 尝试 %s/%s：proxy_source=%s proxy=%s",
                route_index,
                len(route_candidates),
                route_source,
                masked_proxy_url(route_proxy) or "直连",
            )
            refreshed = check_account_liveness(
                email,
                proxy=route_proxy,
                clear_log=False,
                account=account,
                preflight_attempts=1,
                rotate_proxy_on_retry=False,
            )
            if not refreshed.get("ok"):
                last_error = RuntimeError(refreshed.get("error") or "刷新登录态失败")
                details = describe_twofa_error(last_error)
                logger.warning(
                    "[2FA重设] 路由失败：route=%s stage=%s code=%s http=%s detail=%s",
                    route_source,
                    details["stage"],
                    details["code"],
                    details["status"] or "-",
                    details["detail"],
                )
                if route_index < len(route_candidates) and _retryable_route_error(last_error):
                    logger.warning("[2FA重设] 当前出口失败，切换下一出口重试")
                    continue
                raise last_error

            # 仅成功刷新时更新查活；2FA 自身的代理故障不覆盖已有“查活正常”。
            db.update_account_liveness(account_id, refreshed)
            latest = db.get_account(account_id) or account
            account = latest
            saved_session = extract_saved_session(latest) or refreshed.get("session") or {}
            try:
                env = BrowserSession(
                    proxy=route_proxy,
                    detect_exit_geo=False,
                    **session_fingerprint_kwargs(email),
                )
                saved_device_id = str(latest.get("device_id") or "").strip()
                if saved_device_id:
                    env.device_id = saved_device_id
                if not _restore_session_cookies(env, saved_session):
                    raise RuntimeError("刷新登录态后未保存可用于 2FA 重认证的 Session Cookie")
                secret = setup_2fa(env, email)
                effective_proxy = route_proxy
                effective_source = route_source
                break
            except Exception as exc:
                last_error = exc
                details = describe_twofa_error(exc)
                logger.warning(
                    "[2FA重设] 2FA 阶段失败：route=%s stage=%s code=%s http=%s detail=%s",
                    route_source,
                    details["stage"],
                    details["code"],
                    details["status"] or "-",
                    details["detail"],
                )
                if env is not None:
                    try:
                        env.session.close()
                    except Exception:
                        pass
                    env = None
                if route_index < len(route_candidates) and _retryable_route_error(exc):
                    logger.warning("[2FA重设] 当前出口在 TOTP 阶段失败，切换下一出口重试")
                    continue
                raise

        if not secret:
            raise RuntimeError(f"2FA 重设未生成 TOTP secret：{last_error or '未知错误'}")
        result = {
            "ok": True,
            "status": "success",
            "totp_secret": secret,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "trigger": trigger,
            "proxy_source": effective_source,
            "proxy_used": masked_proxy_url(effective_proxy) or None,
            "route_attempts": route_index,
        }
        db.update_account_twofa_setup(account_id, result)
        logger.info("[2FA重设] 成功: %s", email)
        return result
    except Exception as exc:
        details = describe_twofa_error(exc)
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{details['code']} stage={details['stage']} http={details['status'] or '-'}: {details['detail']}",
            "failure_code": details["code"],
            "failure_stage": details["stage"],
            "failure_status": details["status"],
            "attempts": route_attempts,
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
        with _RUNNING_LOCK:
            _RUNNING.discard(key)
        if file_handler is not None:
            root_logger.removeHandler(file_handler)
            file_handler.close()
        _QUEUE_SLOTS.release()


def enqueue_account_twofa_setup(*, account_id: int, email: str, trigger: str = "manual", proxy: str | None = None) -> dict:
    """把账号 2FA 重设任务放入后台队列。"""
    key = str(email or "").strip().lower()
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "2FA 重设队列已满，请稍后重试"}
    try:
        if not db.claim_account_twofa_setup(account_id, trigger=trigger):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在设置 2FA"}
        # 入队后立即视为运行中，避免日志面板在线程启动前误判结束并停止轮询。
        with _RUNNING_LOCK:
            _RUNNING.add(key)
        _append_log(email, f"[2FA重设] 已入队 account_id={int(account_id)} trigger={trigger}", clear=True)
        _EXECUTOR.submit(
            _run_twofa_setup,
            account_id=int(account_id),
            email=str(email or "").strip(),
            proxy=proxy,
            trigger=str(trigger or "manual"),
        )
        return {"accepted": True, "busy": False, "account_id": int(account_id), "email": email, "status": "queued"}
    except Exception as exc:
        with _RUNNING_LOCK:
            _RUNNING.discard(key)
        _QUEUE_SLOTS.release()
        result = {"ok": False, "status": "failed", "error": f"2FA 重设入队失败: {type(exc).__name__}: {str(exc)[:180]}"}
        db.update_account_twofa_setup(int(account_id), result)
        try:
            _append_log(email, result["error"])
        except Exception:
            pass
        return {"accepted": False, "busy": False, "error": result["error"]}


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
