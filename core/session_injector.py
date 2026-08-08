# -*- coding: utf-8 -*-
"""把已保存的 ChatGPT 浏览器登录态注入新的本地指纹浏览器。"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from core.cloakbrowser_driver import build_cloak_driver
from core.proxy_utils import masked_proxy_url
from core.session_state import extract_saved_session

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_OPEN_SESSIONS: dict[int, tuple[Any, Any]] = {}


@dataclass
class _InjectedSessionControl:
    """每个植入窗口的线程控制块，保证 Playwright 对象始终由创建线程持有。"""

    account_id: int
    stop_event: threading.Event = field(default_factory=threading.Event)
    ready_event: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None
    thread: threading.Thread | None = None


_SESSION_CONTROLS: dict[int, _InjectedSessionControl] = {}


def _read_session_from_browser(driver) -> dict:
    """在已注入的浏览器内验证 /api/auth/session。"""
    result = driver.execute_async_script(r"""
    const done = arguments[0];
    fetch('/api/auth/session', {credentials: 'include'})
      .then(async r => done({status: r.status, data: await r.json()}))
      .catch(e => done({status: 0, error: String(e)}));
    """) or {}
    data = result.get("data") if isinstance(result, dict) else None
    return data if isinstance(data, dict) else {}


def _browser_is_closed(driver: Any) -> bool:
    """兼容 Cloak 适配器和 Playwright 页面，判断窗口是否已被用户关闭。"""
    try:
        page = getattr(driver, "page", None)
        if page is None:
            return _browser_transport_closed(driver)
        checker = getattr(page, "is_closed", None)
        return bool(callable(checker) and checker())
    except Exception:
        # 导航/刷新期间页面对象可能短暂抛异常，交给 transport 状态二次判断。
        return _browser_transport_closed(driver)


def _browser_transport_closed(driver: Any) -> bool:
    """读取 Playwright Browser 的连接状态，区分刷新异常与真正断开。"""
    for owner in (getattr(driver, "browser", None), getattr(driver, "context", None)):
        checker = getattr(owner, "is_connected", None)
        if not callable(checker):
            continue
        try:
            return checker() is False
        except Exception:
            continue
    return False


def _keep_browser_owned(driver: Any, stop_event: threading.Event) -> None:
    """在创建 driver 的线程中保持事件循环，避免线程结束导致浏览器自动退出。"""
    while not stop_event.wait(1.0):
        if _browser_is_closed(driver):
            break
        try:
            # 轻量心跳让 Playwright 连接保持活跃，同时检测远端 browser 是否已断开。
            driver.execute_script("return document.readyState || 'unknown';")
        except Exception as exc:
            # 刷新/跨站导航会短暂销毁当前 execution context；保留宿主线程并重试，
            # 只有 page.is_closed 或 browser.is_connected=False 才结束窗口生命周期。
            if _browser_is_closed(driver) or _browser_transport_closed(driver):
                break
            logger.debug("[登录态植入] 页面刷新/导航期间心跳暂不可用，继续保持窗口：%s", str(exc)[:160])


def _inject_one(
    account: dict,
    *,
    keep_open: bool = False,
    stop_event: threading.Event | None = None,
    on_ready=None,
) -> dict:
    """启动一个随机指纹/代理环境，植入 cookies 并验证登录态。"""
    account_id = int(account.get("id") or 0)
    email = str(account.get("email") or "")
    saved = extract_saved_session(account)
    cookies = list((saved or {}).get("cookies") or []) if saved else []
    if not cookies:
        return {"ok": False, "id": account_id, "email": email, "reason": "该账号没有保存浏览器 cookies，请重新注册后再植入"}

    driver = None
    opened = None
    try:
        # proxy=None 触发 PROXY_POOL；Cloak 配置负责每次生成新指纹。
        # 登录态植入必须让用户能看到并继续使用浏览器；注册流程的无头配置不应覆盖这里。
        driver, opened = build_cloak_driver(proxy=None, headless=False)
        driver.get("https://chatgpt.com/")
        driver.delete_all_cookies()
        added = 0
        for cookie in cookies:
            try:
                driver.add_cookie(cookie)
                added += 1
            except Exception as exc:
                logger.warning("[登录态植入] cookie 写入跳过：id=%s name=%s err=%s", account_id, cookie.get("name"), str(exc)[:160])
        driver.get("https://chatgpt.com/")
        session = _read_session_from_browser(driver)
        token = str(session.get("accessToken") or "").strip()
        if not token:
            raise RuntimeError(f"植入后 /api/auth/session 未返回 accessToken，已写入 {added}/{len(cookies)} 个 cookie")
        with _LOCK:
            old = _OPEN_SESSIONS.pop(account_id, None)
            if old:
                try:
                    old[0].quit()
                except Exception:
                    pass
            _OPEN_SESSIONS[account_id] = (driver, opened)
        proxy = masked_proxy_url(getattr(driver, "upstream_proxy_url", None)) or "无"
        result = {"ok": True, "id": account_id, "email": email, "cookies": added, "proxy": proxy, "profile_id": getattr(opened, "profile_id", "")}
        logger.info("[登录态植入] 成功：id=%s email=%s cookies=%s proxy=%s", account_id, email, added, proxy)
        if callable(on_ready):
            on_ready(result)
        if keep_open:
            _keep_browser_owned(driver, stop_event or threading.Event())
            with _LOCK:
                current = _OPEN_SESSIONS.get(account_id)
                if current and current[0] is driver:
                    _OPEN_SESSIONS.pop(account_id, None)
            try:
                driver.quit()
            except Exception:
                pass
        return result
    except Exception as exc:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        logger.error("[登录态植入] 失败：id=%s email=%s %s: %s", account_id, email, type(exc).__name__, str(exc)[:240])
        result = {"ok": False, "id": account_id, "email": email, "reason": f"{type(exc).__name__}: {str(exc)[:240]}"}
        if callable(on_ready):
            on_ready(result)
        return result


def _stop_control(control: _InjectedSessionControl, timeout: float = 12.0) -> None:
    """通知宿主线程关闭浏览器，并等待其在同一线程内执行 quit。"""
    control.stop_event.set()
    thread = control.thread
    if thread and thread is not threading.current_thread() and thread.is_alive():
        thread.join(timeout=max(1.0, float(timeout)))


def _start_injected_session(account: dict) -> dict:
    """启动常驻宿主线程，等待登录态验证完成后返回结果。"""
    account_id = int(account.get("id") or 0)
    old_control = None
    with _LOCK:
        old_control = _SESSION_CONTROLS.pop(account_id, None)
    if old_control:
        _stop_control(old_control)

    control = _InjectedSessionControl(account_id=account_id)

    def _owner() -> None:
        try:
            result = _inject_one(
                account,
                keep_open=True,
                stop_event=control.stop_event,
                on_ready=lambda value: _publish_ready(control, value),
            )
            if not control.ready_event.is_set():
                _publish_ready(control, result)
        except BaseException as exc:  # noqa: BLE001 - 将宿主线程错误转成接口结果
            _publish_ready(control, {
                "ok": False,
                "id": account_id,
                "email": str(account.get("email") or ""),
                "reason": f"{type(exc).__name__}: {str(exc)[:240]}",
            })
        finally:
            with _LOCK:
                if _SESSION_CONTROLS.get(account_id) is control:
                    _SESSION_CONTROLS.pop(account_id, None)

    control.thread = threading.Thread(
        target=_owner,
        name=f"session-inject-owner-{account_id}",
        # WebUI 运行期间线程持续存在；服务主动退出时不阻塞进程关闭。
        daemon=True,
    )
    with _LOCK:
        _SESSION_CONTROLS[account_id] = control
    control.thread.start()
    # 注入期间可能需要等待页面加载/挑战，但不让 WebUI 无限挂死。
    control.ready_event.wait(timeout=300)
    if not control.ready_event.is_set():
        _stop_control(control, timeout=2.0)
        return {
            "ok": False,
            "id": account_id,
            "email": str(account.get("email") or ""),
            "reason": "登录态植入超过 300 秒仍未完成验证",
        }
    return dict(control.result or {
        "ok": False,
        "id": account_id,
        "email": str(account.get("email") or ""),
        "reason": "宿主线程未返回植入结果",
    })


def _publish_ready(control: _InjectedSessionControl, result: dict) -> None:
    """线程安全发布首次验证结果，之后保持窗口线程继续运行。"""
    with _LOCK:
        if control.result is None:
            control.result = dict(result or {})
            control.ready_event.set()


def inject_sessions(account_ids: list[int], max_workers: int = 3) -> dict:
    """批量植入多个账号；每个账号独立浏览器、独立代理、独立随机指纹。"""
    accounts = []
    skipped = []
    seen = set()
    from core import db

    for raw in account_ids:
        try:
            account_id = int(raw)
        except (TypeError, ValueError):
            skipped.append({"id": raw, "reason": "ID 非法"})
            continue
        if account_id in seen:
            continue
        seen.add(account_id)
        account = db.get_account(account_id)
        if not account:
            skipped.append({"id": account_id, "reason": "账号不存在"})
        else:
            accounts.append(account)

    results = []
    workers = max(1, min(int(max_workers or 3), len(accounts) or 1))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="session-inject") as pool:
        futures = [pool.submit(_start_injected_session, account) for account in accounts]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: int(item.get("id") or 0))
    return {
        "ok": True,
        "results": results,
        "success": [x for x in results if x.get("ok")],
        "failed": [x for x in results if not x.get("ok")],
        "skipped": skipped,
    }


def close_injected_sessions(account_ids: list[int] | None = None) -> int:
    """关闭已由本功能保持打开的浏览器窗口。"""
    controls: list[_InjectedSessionControl] = []
    pairs: list[tuple[Any, Any]] = []
    with _LOCK:
        ids = list(set(_SESSION_CONTROLS) | set(_OPEN_SESSIONS)) if account_ids is None else [int(x) for x in account_ids]
        for account_id in ids:
            control = _SESSION_CONTROLS.pop(account_id, None)
            if control:
                controls.append(control)
                continue
            pair = _OPEN_SESSIONS.pop(account_id, None)
            if pair:
                pairs.append(pair)
    # 不持有全局锁等待宿主线程，否则宿主线程清理映射时会相互等待。
    for control in controls:
        _stop_control(control)
    for pair in pairs:
        try:
            pair[0].quit()
        except Exception:
            pass
    return len(controls) + len(pairs)
