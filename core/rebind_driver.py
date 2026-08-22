# -*- coding: utf-8 -*-
"""Two-stage account rebind adapter.

The task service deliberately keeps the site-specific email-change workflow out
of its queue/database code.  This module supplies the runtime adapter and a
small hook surface for deployments that know the current account-settings API.

The adapter never treats an HTTP 2xx response (or a callback ``ok`` flag) as a
successful rebind by itself.  Before returning it must observe the target email
from a refreshed remote session and an access token from that same session.
Deployments may provide ``submit_protocol``/``submit_browser`` and ``verify``
hooks, or describe their endpoint in ``target['rebind_api']``.  No endpoint is
invented when the site-specific contract is absent.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlencode, urljoin, urlparse

logger = logging.getLogger(__name__)

SUPPORTED_DRIVERS = ("protocol", "cloak", "roxy")
_DRIVER_ALIASES = {
    "api": "protocol",
    "http": "protocol",
    "browser": "cloak",
    "cloakbrowser": "cloak",
    "roxybrowser": "roxy",
}
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_OTP_RE = re.compile(r"^\d{4,8}$")
_DEFAULT_REBIND_ALLOWED_HOSTS = (
    "chatgpt.com",
    "www.chatgpt.com",
    "chat.openai.com",
    "auth.openai.com",
)
_MAX_REBIND_REDIRECTS = 3
_DEFAULT_OTP_ATTEMPTS = 2
_MAX_OTP_ATTEMPTS = 5


class RebindDriverError(RuntimeError):
    """A login, submission, or remote verification stage failed."""


class RebindVerificationError(RebindDriverError):
    """The remote account did not prove it now uses the target mailbox."""


class RebindHttpError(RebindDriverError):
    """A rebind request returned a non-success HTTP status.

    Keeping the status and parsed response separate lets the OTP verifier
    retry only explicit code failures without parsing a localized exception
    string.  The exception message intentionally contains no response body or
    request URL, because those values can include mailbox/session material.
    """

    def __init__(self, status: int, *, data: Mapping[str, Any] | None = None):
        self.status = int(status or 0)
        self.data = dict(data or {})
        super().__init__(f"换绑请求被拒绝：HTTP {self.status}")


@dataclass
class RebindHooks:
    """Optional site-specific hooks.

    Hooks receive keyword arguments (``account``, ``target``, ``context``,
    ``get_otp``, ``log`` and stage-specific values).  The invocation helper is
    intentionally tolerant of older short signatures, so a hook taking only
    ``(account, target)`` remains usable.
    """

    login_protocol: Callable[..., Any] | None = None
    login_browser: Callable[..., Any] | None = None
    submit_protocol: Callable[..., Any] | None = None
    submit_browser: Callable[..., Any] | None = None
    verify: Callable[..., Any] | None = None
    otp: Callable[..., Any] | None = None
    protocol_session_factory: Callable[..., Any] | None = None
    browser_factory: Callable[..., Any] | None = None

    def as_dict(self) -> dict[str, Callable[..., Any]]:
        return {
            key: value
            for key, value in vars(self).items()
            if callable(value)
        }


_HOOKS: dict[str, Callable[..., Any]] = {}
_HOOKS_LOCK = threading.RLock()


def set_rebind_hooks(hooks: Mapping[str, Callable[..., Any]] | RebindHooks | None = None, **kwargs: Any) -> None:
    """Install process-local hooks for integration tests or a deployment.

    Passing ``None`` with no keyword arguments clears the registry.  Hooks are
    kept in memory only; credentials and mailbox material are never persisted.
    """
    values: dict[str, Callable[..., Any]] = {}
    if isinstance(hooks, RebindHooks):
        values.update(hooks.as_dict())
    elif isinstance(hooks, Mapping):
        values.update({str(k): v for k, v in hooks.items() if callable(v)})
    values.update({str(k): v for k, v in kwargs.items() if callable(v)})
    with _HOOKS_LOCK:
        _HOOKS.clear()
        _HOOKS.update(values)


def clear_rebind_hooks() -> None:
    set_rebind_hooks(None)


def get_rebind_hooks() -> dict[str, Callable[..., Any]]:
    with _HOOKS_LOCK:
        return dict(_HOOKS)


@dataclass
class RebindContext:
    """Live transport state shared by login, submission and verification."""

    account: dict
    target: dict
    login_driver: str
    action_driver: str
    hybrid: bool
    proxy: str | None = None
    session: Any = None  # curl_cffi BrowserSession, not a JSON session dict
    driver: Any = None  # Selenium-compatible Cloak/Roxy adapter
    driver_kind: str | None = None
    session_info: dict = field(default_factory=dict)
    action_result: dict = field(default_factory=dict)
    closers: list[Callable[[], Any]] = field(default_factory=list)
    closed: bool = False

    @property
    def access_token(self) -> str:
        return _extract_token(self.session_info) or _extract_token(self.action_result)

    def add_closer(self, closer: Any) -> None:
        if callable(closer):
            self.closers.append(closer)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for closer in reversed(self.closers):
            try:
                closer()
            except Exception:
                logger.debug("换绑资源关闭失败", exc_info=True)
        self.closers.clear()


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y", "是"}
    return bool(value)


def _normalize_driver(value: Any, default: str = "protocol") -> str:
    raw = str(value or "").strip().lower() or default
    raw = _DRIVER_ALIASES.get(raw, raw)
    if raw not in SUPPORTED_DRIVERS:
        raise RebindDriverError(f"换绑驱动无效：{raw}")
    return raw


def _email(value: Any, label: str = "邮箱") -> str:
    result = str(value or "").strip()
    if not result or not _EMAIL_RE.match(result):
        raise RebindDriverError(f"{label}格式无效")
    return result


def _safe_log(log: Callable[[str], None] | None, message: str) -> None:
    """Emit stage-only diagnostics; callers must not pass passwords/tokens/codes."""
    if not callable(log):
        return
    try:
        log(str(message))
    except Exception:
        logger.debug("换绑回调日志失败", exc_info=True)


def _close_resource(resource: Any) -> None:
    """Best-effort close for injected protocol/browser resources."""
    if resource is None:
        return
    seen: set[int] = set()
    candidates = [resource, getattr(resource, "session", None)]
    for item in candidates:
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        for name in ("close", "quit", "shutdown"):
            closer = getattr(item, name, None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    logger.debug("换绑资源关闭失败", exc_info=True)
                break


def _extract_token(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    # Only explicit Access Token fields count.  Generic ``token`` values are
    # often CSRF, verification, or transaction tokens and must never be
    # persisted as a refreshed account credential.
    for key in ("access_token", "accessToken"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    for key in ("session", "session_info", "data", "result", "payload", "user"):
        nested = value.get(key)
        token = _extract_token(nested)
        if token:
            return token
    return ""


def _extract_email(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    for key in ("verified_email", "current_email", "account_email", "email"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    for key in ("user", "session", "session_info", "data", "result", "payload", "account"):
        nested = value.get(key)
        found = _extract_email(nested)
        if found:
            return found
    return ""


def _session_info(value: Any) -> dict:
    if not isinstance(value, Mapping):
        return {}
    for key in ("session_info", "session", "data", "result"):
        nested = value.get(key)
        if isinstance(nested, Mapping) and (_extract_token(nested) or _extract_email(nested)):
            return dict(nested)
    if _extract_token(value) or _extract_email(value):
        return dict(value)
    return {}


def _aliases(name: str) -> tuple[str, ...]:
    low = str(name or "").lower()
    aliases = {
        "ctx": ("context",),
        "context": ("ctx",),
        "new_email": ("target_email", "email"),
        "target_email": ("new_email", "email"),
        "email": ("target_email", "new_email"),
        "code": ("otp",),
        "otp": ("code",),
        "http_session": ("session",),
        "browser": ("driver",),
    }
    return aliases.get(low, ())


def _invoke(fn: Callable[..., Any], **kwargs: Any) -> Any:
    """Invoke a hook while supporting concise legacy positional signatures."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(**kwargs)
    params = list(sig.parameters.values())
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
        return fn(**kwargs)

    args: list[Any] = []
    consumed: set[str] = set()
    positional = [p for p in params if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    for param in positional:
        name = param.name
        key = name if name in kwargs else next((a for a in _aliases(name) if a in kwargs), None)
        if key is None:
            if param.default is not inspect.Parameter.empty:
                break
            # Common short names used by integration hooks.
            fallback = ("account", "target", "context", "session", "driver", "target_email")
            key = next((candidate for candidate in fallback if candidate in kwargs and candidate not in consumed), None)
            if key is None:
                raise TypeError(f"换绑钩子参数缺失: {name}")
        args.append(kwargs[key])
        consumed.add(key)

    accepted = {
        p.name: kwargs[p.name]
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        and p.name in kwargs and p.name not in consumed
    }
    return fn(*args, **accepted)


def _hook_map(hooks: Mapping[str, Callable[..., Any]] | RebindHooks | None) -> dict[str, Callable[..., Any]]:
    result = get_rebind_hooks()
    if isinstance(hooks, RebindHooks):
        result.update(hooks.as_dict())
    elif isinstance(hooks, Mapping):
        result.update({str(k): v for k, v in hooks.items() if callable(v)})
    return result


def _normalize_hook_result(context: RebindContext, raw: Any) -> dict:
    """Merge a hook result without mistaking a JSON session for a transport."""
    if isinstance(raw, RebindContext):
        context.session = raw.session or context.session
        context.driver = raw.driver or context.driver
        context.session_info.update(raw.session_info or {})
        context.action_result.update(raw.action_result or {})
        context.closers.extend(raw.closers)
        return {"ok": True, **context.action_result}
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise RebindDriverError(f"换绑驱动返回类型无效：{type(raw).__name__}")
    result = dict(raw)
    for key in ("driver", "browser"):
        if result.get(key) is not None and context.driver is None:
            context.driver = result[key]
    transport = result.get("transport") or result.get("http_session")
    if transport is not None and not isinstance(transport, Mapping):
        context.session = transport
    candidate_session = result.get("session_transport")
    if candidate_session is not None and not isinstance(candidate_session, Mapping):
        context.session = candidate_session
    info = _session_info(result)
    if info:
        context.session_info.update(info)
    closer = result.get("close") or result.get("cleanup")
    context.add_closer(closer)
    context.action_result.update(result)
    return result


def _require_stage_success(raw: Any, stage: str) -> None:
    """Reject explicit negative hook results without copying their payloads."""
    if not isinstance(raw, Mapping):
        return
    negative = (
        ("ok" in raw and raw.get("ok") is not None and not _as_bool(raw.get("ok"), False))
        or ("success" in raw and raw.get("success") is not None and not _as_bool(raw.get("success"), False))
        or _as_bool(raw.get("failed"), False)
        or raw.get("error") not in (None, "")
    )
    if negative:
        raise RebindDriverError(f"{stage}阶段未确认成功")


def _validate_login_identity(context: RebindContext, source_email: str) -> None:
    """Require the login transport to identify the selected source account."""
    observed = _extract_email(context.session_info)
    if not observed:
        raise RebindDriverError("登录态未返回原账号邮箱，已停止换绑")
    if observed.casefold() != source_email.casefold():
        raise RebindDriverError("登录态账号与所选原账号不一致，已停止换绑")
    if not _extract_token(context.session_info):
        raise RebindDriverError("登录态未返回 Access Token，已停止换绑")


def _make_otp_getter(
    provider: Callable[..., Any] | None,
    *,
    default_target: dict,
    log: Callable[[str], None] | None,
) -> Callable[..., str]:
    """Create an OTP callback that validates shape and never logs the code."""
    if provider is None:
        from core.email_provider import wait_for_otp

        provider = wait_for_otp

    def get_otp(
        email: str | None = None,
        after_ts: float | None = None,
        exclude_codes: Iterable[str] | None = None,
        target: dict | None = None,
    ) -> str:
        address = _email(email or default_target.get("email"), "验证码邮箱")
        stamp = float(after_ts if after_ts is not None else time.time())
        excluded = {str(item).strip() for item in (exclude_codes or ()) if str(item).strip()}
        for attempt in range(3):
            value = _invoke(
                provider,
                email=address,
                target=target or default_target,
                after_ts=stamp,
                exclude_codes=excluded,
                log=log,
            )
            code = str(value or "").strip()
            if not _OTP_RE.match(code):
                raise RebindDriverError("邮箱验证码格式无效")
            if code not in excluded:
                return code
            excluded.add(code)
            if attempt < 2:
                time.sleep(0.05)
        raise RebindDriverError("邮箱验证码重复")

    return get_otp


def _add_session_cookies(session: Any, saved: Mapping[str, Any]) -> int:
    cookies = saved.get("cookies") if isinstance(saved, Mapping) else []
    jar = getattr(getattr(session, "session", None), "cookies", None)
    setter = getattr(jar, "set", None)
    if not callable(setter):
        return 0
    count = 0
    for cookie in cookies or []:
        if not isinstance(cookie, Mapping) or not cookie.get("name"):
            continue
        try:
            setter(
                str(cookie.get("name")),
                str(cookie.get("value") or ""),
                domain=str(cookie.get("domain") or ".chatgpt.com"),
                path=str(cookie.get("path") or "/"),
                secure=bool(cookie.get("secure")),
            )
            count += 1
        except Exception:
            logger.debug("保存 Cookie 写入失败", exc_info=True)
    return count


def _protocol_session(account: dict, proxy: str | None, hooks: dict, log: Callable[[str], None] | None) -> Any:
    factory = hooks.get("protocol_session_factory")
    if factory:
        raw = _invoke(factory, account=account, proxy=proxy, log=log)
        if isinstance(raw, tuple) and raw:
            session = raw[0]
            if len(raw) > 1 and isinstance(raw[1], Mapping):
                try:
                    session._rebind_session_info = dict(raw[1])
                except Exception:
                    pass
            return session
        if raw is not None:
            return raw
    from core.session import BrowserSession

    return BrowserSession(
        proxy=proxy,
        detect_exit_geo=False,
        fingerprint_key=str(account.get("email") or "").strip() or None,
    )


def _fetch_protocol_session(session: Any) -> dict:
    from core.account_export import fetch_session

    data = fetch_session(session)
    if not isinstance(data, Mapping):
        raise RebindDriverError("协议 Session 响应格式无效")
    result = dict(data)
    if not _extract_token(result):
        raise RebindDriverError("协议 Session 未返回 Access Token")
    return result


def _protocol_login_builtin(
    account: dict,
    *,
    proxy: str | None,
    otp_getter: Callable[..., str],
    log: Callable[[str], None] | None,
    hooks: dict,
) -> tuple[Any, dict]:
    """Login using the project's existing auth protocol while retaining session."""
    from core.account_export import fetch_session, follow_oauth_callback
    from core.account_liveness import (
        _auth_payload_value,
        _is_email_verification_state,
        _navigate_auth_step,
        _network_preflight_with_retry,
    )
    from core.openai_auth import (
        continue_authorize_with_email,
        extract_totp_factor,
        follow_authorize,
        issue_mfa_challenge,
        send_email_otp,
        validate_email_otp,
        verify_login_password,
        verify_mfa_code,
    )
    import pyotp

    email = _email(account.get("email"), "原账号邮箱")
    session = _protocol_session(account, proxy, hooks, log)

    # A factory may hand us an already-authenticated protocol session together
    # with its JSON snapshot.  This is useful for desktop integrations and
    # avoids a second login round trip.
    initial_info = getattr(session, "_rebind_session_info", {})
    if isinstance(initial_info, Mapping) and _extract_token(initial_info):
        _safe_log(log, "协议登录：复用注入的登录态")
        return session, dict(initial_info)

    # A saved Session Cookie is the least disruptive path and retains the same
    # account context for the subsequent email-change request.
    try:
        from core.session_state import extract_saved_session

        saved = extract_saved_session(account) or {}
        if _add_session_cookies(session, saved):
            info = _fetch_protocol_session(session)
            _safe_log(log, "协议登录：复用已保存 Session")
            return session, info
    except Exception as exc:
        _safe_log(log, f"协议登录：保存 Session 不可用，转入重新登录（{type(exc).__name__}）")

    # The auth helpers construct a fresh BrowserSession themselves so that
    # network retries can rotate an unhealthy proxy.  Close the provisional
    # factory session before adopting the returned one.
    _close_resource(session)

    login_password = str(account.get("registration_password") or "").strip()
    try:
        session, authorize_url = _network_preflight_with_retry(
            email,
            proxy,
            max_attempts=2,
            rotate_proxy_on_retry=False,
        )
        final_url = follow_authorize(session, authorize_url, allow_password_page=bool(login_password))

        if login_password:
            if _is_email_verification_state(final_url):
                raise RebindDriverError("保存密码账号进入邮箱验证码页，登录状态不一致")
            if "/log-in/password" not in final_url.lower():
                payload = continue_authorize_with_email(session, email)
                if _is_email_verification_state(payload=payload):
                    raise RebindDriverError("密码登录推进返回邮箱验证码页")
                next_url = _auth_payload_value(payload, "continue_url", "external_url", "redirect_url", "url")
                if next_url and "password" in next_url.lower():
                    final_url = _navigate_auth_step(session, next_url, final_url)
            auth_result = verify_login_password(session, login_password)
            if _is_email_verification_state(payload=auth_result):
                raise RebindDriverError("密码校验返回邮箱验证码页")
            factor = extract_totp_factor(auth_result)
            if factor:
                secret = str(account.get("totp_secret") or "").replace(" ", "").strip()
                if not secret:
                    raise RebindDriverError("账号要求 TOTP，但未保存 TOTP secret")
                challenge = issue_mfa_challenge(session, factor)
                challenge_id = _auth_payload_value(challenge, "mfa_request_id")
                verify_factor = dict(factor)
                if challenge_id:
                    verify_factor["metadata"] = {**(factor.get("metadata") or {}), "mfa_request_id": challenge_id}
                auth_result = verify_mfa_code(session, verify_factor, pyotp.TOTP(secret).now())
            continue_url = _auth_payload_value(auth_result, "continue_url", "external_url", "redirect_url", "url", "location")
            if not continue_url:
                raise RebindDriverError("密码登录未返回 OAuth continue_url")
            follow_oauth_callback(session, continue_url, referer="https://auth.openai.com/log-in/password")
        else:
            # Capture the boundary before sending the message so fast mailboxes
            # cannot race between the send request and OTP polling.
            otp_after_ts = time.time()
            send_email_otp(session)
            code = otp_getter(email=email, after_ts=otp_after_ts, target={"email": email})
            validate_result = validate_email_otp(session, code)
            continue_url = _auth_payload_value(validate_result, "continue_url", "external_url", "redirect_url", "url", "location")
            if not continue_url:
                raise RebindDriverError("邮箱 OTP 登录未返回 OAuth continue_url")
            follow_oauth_callback(session, continue_url, referer="https://auth.openai.com/email-verification")

        info = _fetch_protocol_session(session)
    except Exception:
        _close_resource(session)
        raise
    _safe_log(log, "协议登录：已建立原账号登录态")
    return session, info


def _open_browser_builtin(driver_name: str, proxy: str | None, headless: bool, hooks: dict, *, stage: str, log: Callable[[str], None] | None) -> tuple[Any, Callable[[], Any] | None]:
    factory = hooks.get("browser_factory")
    if factory:
        raw = _invoke(factory, driver=driver_name, driver_name=driver_name, proxy=proxy, headless=headless, stage=stage, log=log)
        if isinstance(raw, tuple):
            browser = raw[0]
            if len(raw) > 2 and isinstance(raw[2], Mapping) and browser is not None:
                try:
                    browser._rebind_session_info = dict(raw[2])
                except Exception:
                    pass
            if len(raw) > 3 and raw[3] and browser is not None:
                try:
                    browser._rebind_proxy_used = raw[3]
                except Exception:
                    pass
            return browser, raw[1] if len(raw) > 1 and callable(raw[1]) else None
        if isinstance(raw, Mapping):
            browser = raw.get("driver") or raw.get("browser")
            if browser is not None:
                try:
                    if isinstance(raw.get("session_info"), Mapping):
                        browser._rebind_session_info = dict(raw["session_info"])
                    if raw.get("proxy_used"):
                        browser._rebind_proxy_used = raw["proxy_used"]
                except Exception:
                    pass
            return browser, raw.get("close") or raw.get("cleanup")
        return raw, None
    from core import browser_liveness

    opener = browser_liveness._open_cloak if driver_name == "cloak" else browser_liveness._open_roxy
    driver, proxy_used, closer = opener(proxy, bool(headless))
    try:
        if driver is not None and proxy_used:
            driver._rebind_proxy_used = proxy_used
    except Exception:
        pass
    return driver, closer


def _browser_login_builtin(
    account: dict,
    *,
    driver_name: str,
    proxy: str | None,
    headless: bool,
    hooks: dict,
    log: Callable[[str], None] | None,
) -> tuple[Any, Callable[[], Any] | None, dict]:
    driver, closer = _open_browser_builtin(driver_name, proxy, headless, hooks, stage="login", log=log)
    if driver is None:
        if callable(closer):
            try:
                closer()
            except Exception:
                logger.debug("浏览器驱动为空时资源关闭失败", exc_info=True)
        raise RebindDriverError("浏览器驱动工厂未返回 driver")
    try:
        info = getattr(driver, "_rebind_session_info", None)
        if not isinstance(info, Mapping) or not _extract_token(info):
            from core.browser_liveness import _browser_login

            info = _browser_login(
                driver,
                account,
                _email(account.get("email"), "原账号邮箱"),
                headless=bool(headless),
                restore_saved_session=True,
            )
        if not isinstance(info, Mapping) or not _extract_token(info):
            raise RebindDriverError("浏览器登录未返回 Access Token")
    except Exception as exc:
        if callable(closer):
            try:
                closer()
            except Exception:
                logger.debug("浏览器登录失败后的资源关闭失败", exc_info=True)
        else:
            _close_resource(driver)
        if isinstance(exc, RebindDriverError):
            raise
        raise RebindDriverError(f"{driver_name} 登录失败：{type(exc).__name__}") from exc
    _safe_log(log, f"{driver_name} 登录：已建立原账号登录态")
    return driver, closer, dict(info)


def _ensure_protocol_transport(context: RebindContext, hooks: dict, log: Callable[[str], None] | None) -> None:
    if context.session is not None:
        return
    if context.driver is None:
        raise RebindDriverError("协议提交缺少登录态")
    from core.account_export import browser_session_from_driver

    try:
        context.session = browser_session_from_driver(
            context.driver,
            proxy=context.proxy or getattr(context.driver, "_rebind_proxy_used", None),
            fingerprint_key=str(context.account.get("email") or "").strip() or None,
        )
        context.add_closer(lambda session=context.session: _close_resource(session))
        context.session_info = _fetch_protocol_session(context.session)
    except Exception as exc:
        raise RebindDriverError(f"浏览器登录态转协议失败：{type(exc).__name__}") from exc
    _safe_log(log, "已把浏览器登录态同步到协议提交阶段")


def _copy_protocol_cookies_to_browser(context: RebindContext, driver: Any) -> None:
    from core.session_state import capture_http_cookies

    driver.get("https://chatgpt.com/")
    delete = getattr(driver, "delete_all_cookies", None)
    if callable(delete):
        delete()
    for cookie in capture_http_cookies(context.session):
        try:
            driver.add_cookie(cookie)
        except Exception:
            logger.debug("协议 Cookie 写入浏览器失败", exc_info=True)
    driver.get("https://chatgpt.com/")


def _copy_browser_cookies(source: Any, target: Any) -> None:
    """Transfer a login browser's cookies when mixed browser drivers differ."""
    getter = getattr(source, "get_cookies", None)
    if not callable(getter):
        return
    try:
        try:
            cookies = getter()
        except TypeError:
            cookies = getter(["https://chatgpt.com/", "https://auth.openai.com/"])
        target.get("https://chatgpt.com/")
        delete = getattr(target, "delete_all_cookies", None)
        if callable(delete):
            delete()
        for cookie in cookies or []:
            try:
                target.add_cookie(cookie)
            except Exception:
                logger.debug("浏览器 Cookie 转移失败", exc_info=True)
        target.get("https://chatgpt.com/")
    except Exception:
        logger.debug("混合浏览器 Cookie 转移失败", exc_info=True)


def _ensure_browser_transport(context: RebindContext, hooks: dict, headless: bool, log: Callable[[str], None] | None) -> None:
    if context.driver is not None and context.driver_kind == context.action_driver:
        return
    previous_driver = context.driver
    driver, closer = _open_browser_builtin(
        context.action_driver,
        context.proxy or context.account.get("rebind_proxy"),
        headless,
        hooks,
        stage="action",
        log=log,
    )
    if driver is None:
        raise RebindDriverError("浏览器提交缺少 driver")
    context.driver = driver
    context.driver_kind = context.action_driver
    context.add_closer(closer or (lambda driver=driver: _close_resource(driver)))
    if context.session is not None:
        _copy_protocol_cookies_to_browser(context, driver)
    elif previous_driver is not None:
        _copy_browser_cookies(previous_driver, driver)
    from core.account_export import _browser_session_info

    try:
        context.session_info = _browser_session_info(driver)
    except Exception as exc:
        raise RebindDriverError(f"浏览器提交登录态校验失败：{type(exc).__name__}") from exc


def _api_spec(account: dict, target: dict) -> dict:
    for owner in (target, account):
        for key in ("rebind_api", "email_change", "email_change_spec", "rebind_endpoint"):
            value = owner.get(key) if isinstance(owner, Mapping) else None
            if isinstance(value, Mapping):
                return dict(value)
            if isinstance(value, str) and value.strip():
                return {"endpoint": value.strip()}
    endpoint = str(os.getenv("REBIND_EMAIL_CHANGE_ENDPOINT") or "").strip()
    if endpoint:
        return {"endpoint": endpoint}
    return {}


def _as_host_list(value: Any) -> list[str]:
    """Normalize an endpoint host allowlist.

    Values may be supplied as a comma-separated environment string, a list,
    or URL-shaped entries.  A leading ``*.``/``.`` permits a subdomain while
    preserving a label boundary (``evilchatgpt.com`` never matches
    ``*.chatgpt.com``).
    """
    if isinstance(value, str):
        values: Iterable[Any] = re.split(r"[,;|]", value)
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        values = value
    else:
        values = [value]
    out: list[str] = []
    for item in values:
        raw = str(item or "").strip().lower().rstrip(".")
        if not raw:
            continue
        parsed = urlparse(raw if "://" in raw else f"//{raw}")
        host = str(parsed.hostname or "").strip().lower().rstrip(".")
        if not host:
            continue
        if raw.startswith("*.") or raw.startswith("."):
            host = "*." + host.lstrip("*.")
        if host not in out:
            out.append(host)
    return out


def _rebind_allowed_hosts(spec: Mapping[str, Any]) -> list[str]:
    configured = _as_host_list(spec.get("allowed_hosts") or spec.get("allowlist"))
    if configured:
        return configured
    env_value = _as_host_list(os.getenv("REBIND_ALLOWED_HOSTS"))
    if env_value:
        return env_value
    hosts = list(_DEFAULT_REBIND_ALLOWED_HOSTS)
    for key in ("base_url", "origin", "endpoint", "submit_url", "start_url", "verify_url", "otp_resend_url", "resend_url"):
        parsed = urlparse(str(spec.get(key) or ""))
        if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
            host = parsed.hostname.lower().rstrip(".")
            if host not in hosts:
                hosts.append(host)
    steps = spec.get("steps")
    if isinstance(steps, list):
        for step in steps:
            parsed = urlparse(str(step.get("url") or "")) if isinstance(step, Mapping) else None
            if parsed is not None and parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
                host = parsed.hostname.lower().rstrip(".")
                if host not in hosts:
                    hosts.append(host)
    return hosts


def _host_matches_allowlist(host: str, allowed_hosts: Iterable[str]) -> bool:
    normalized = str(host or "").strip().lower().rstrip(".")
    if not normalized:
        return False
    for pattern in allowed_hosts:
        value = str(pattern or "").strip().lower().rstrip(".")
        if not value:
            continue
        if value.startswith("*."):
            suffix = value[2:]
            if normalized.endswith("." + suffix) and normalized != suffix:
                return True
        elif normalized == value:
            return True
    return False


def _rebind_allow_http(spec: Mapping[str, Any]) -> bool:
    raw = spec.get("allow_insecure_http")
    if raw is None:
        raw = os.getenv("REBIND_ALLOW_HTTP")
    return _as_bool(raw, False)


def _rebind_base_url(spec: Mapping[str, Any]) -> str:
    """Resolve and validate the origin used by relative endpoint paths."""
    raw = str(spec.get("base_url") or spec.get("origin") or os.getenv("REBIND_EMAIL_CHANGE_BASE_URL") or "").strip()
    if not raw:
        for key in ("endpoint", "submit_url", "start_url", "verify_url"):
            candidate = str(spec.get(key) or "").strip()
            parsed_candidate = urlparse(candidate)
            if parsed_candidate.scheme.lower() in {"http", "https"} and parsed_candidate.hostname:
                raw = f"{parsed_candidate.scheme}://{parsed_candidate.netloc}/"
                break
    if not raw:
        raw = "https://chatgpt.com"
    parsed = urlparse(raw)
    if not parsed.scheme:
        raise RebindDriverError("换绑 API base_url 必须包含 http(s) 协议")
    if parsed.scheme.lower() not in {"http", "https"}:
        raise RebindDriverError("换绑 API base_url 协议无效")
    if parsed.scheme.lower() == "http" and not _rebind_allow_http(spec):
        raise RebindDriverError("换绑 API 仅允许 HTTPS")
    if parsed.username or parsed.password or not parsed.hostname:
        raise RebindDriverError("换绑 API base_url 无效")
    if not _host_matches_allowlist(parsed.hostname, _rebind_allowed_hosts(spec)):
        raise RebindDriverError("换绑 API base_url 不在允许域名列表")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/"


def _resolve_rebind_url(
    spec: Mapping[str, Any],
    raw_url: Any,
    *,
    current_url: str | None = None,
) -> str:
    """Resolve a configured/request-provided URL under the endpoint policy."""
    raw = str(raw_url or "").strip()
    if not raw:
        raise RebindDriverError("协议换绑请求 URL 为空")
    base = current_url or _rebind_base_url(spec)
    if raw.startswith("//"):
        # Scheme-relative URLs can silently downgrade a request; require an
        # explicit scheme instead.
        raise RebindDriverError("换绑 API URL 必须显式包含 http(s) 协议")
    parsed_raw = urlparse(raw)
    if not parsed_raw.scheme:
        resolved = urljoin(base, raw)
    else:
        resolved = raw
    parsed = urlparse(resolved)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise RebindDriverError("换绑 API URL 协议无效")
    if scheme == "http" and not _rebind_allow_http(spec):
        raise RebindDriverError("换绑 API 仅允许 HTTPS")
    if parsed.username or parsed.password or not parsed.hostname:
        raise RebindDriverError("换绑 API URL 无效")
    allowed = _rebind_allowed_hosts(spec)
    if not _host_matches_allowlist(parsed.hostname, allowed):
        raise RebindDriverError("换绑 API URL 不在允许域名列表")
    if current_url:
        previous_host = str(urlparse(current_url).hostname or "").lower().rstrip(".")
        next_host = str(parsed.hostname or "").lower().rstrip(".")
        cross_host = previous_host != next_host
        if cross_host and not _as_bool(spec.get("allow_cross_host_redirects"), False):
            raise RebindDriverError("换绑 API 重定向跨越未允许的域名")
    return resolved


def _response_header(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None)
    if isinstance(headers, Mapping):
        wanted = str(name).lower()
        for key, value in headers.items():
            if str(key).lower() == wanted:
                return str(value or "").strip()
    return ""


def _response_otp_failure(status: int, data: Mapping[str, Any] | None) -> bool:
    """Recognize a code rejection without treating auth/rate errors as OTP."""
    code = int(status or 0)
    if code not in {400, 401, 403, 409, 422}:
        return False
    try:
        text = json.dumps(dict(data or {}), ensure_ascii=False).lower()
    except Exception:
        text = str(data or "").lower()
    return any(
        marker in text
        for marker in (
            "otp", "one-time", "one_time", "verification code", "verification_code",
            "invalid code", "invalid_code", "expired code", "expired_code",
            "code invalid", "code_invalid", "验证码",
        )
    )


def _otp_attempt_count(spec: Mapping[str, Any]) -> int:
    raw = spec.get("otp_attempts")
    if raw is None:
        raw = spec.get("otp_retries")
        if raw is not None:
            try:
                raw = int(raw) + 1
            except (TypeError, ValueError):
                raw = _DEFAULT_OTP_ATTEMPTS
    try:
        value = int(raw) if raw is not None else _DEFAULT_OTP_ATTEMPTS
    except (TypeError, ValueError):
        value = _DEFAULT_OTP_ATTEMPTS
    return max(1, min(_MAX_OTP_ATTEMPTS, value))


def _otp_retry_delay(spec: Mapping[str, Any]) -> float:
    try:
        value = float(spec.get("otp_retry_delay", 0.25))
    except (TypeError, ValueError):
        value = 0.25
    return max(0.0, min(10.0, value))


def _render(value: Any, variables: Mapping[str, Any]) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _render(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, variables) for v in value]
    if not isinstance(value, str):
        return value
    # Support both ``{target_email}`` and ``${target_email}`` without raising
    # on unrelated braces in a URL/query string.
    result = value
    for key, raw in variables.items():
        text = str(raw if raw is not None else "")
        result = result.replace("${" + key + "}", text).replace("{" + key + "}", text)
    return result


def _response_data(response: Any) -> dict:
    if isinstance(response, Mapping):
        return dict(response)
    try:
        data = response.json()
    except Exception:
        data = {}
    return dict(data) if isinstance(data, Mapping) else {}


def _verified_session_snapshot(value: Mapping[str, Any], observed: str, token: str) -> dict:
    """Choose a verified snapshot that carries the same email and token."""
    candidate = _session_info(value)
    if candidate:
        candidate_email = _extract_email(candidate)
        candidate_token = _extract_token(candidate)
        if (
            candidate_email
            and candidate_email.casefold() == observed.casefold()
            and candidate_token
            and candidate_token == token
        ):
            return candidate
    # Some APIs return ``verified_email``/``access_token`` beside a stale
    # nested session.  Do not persist that stale session; retain only the
    # explicitly paired proof returned by the verifier.
    return {"user": {"email": observed}, "accessToken": token}


def _protocol_request(session: Any, spec: Mapping[str, Any], *, method: str, url: str, payload: Any = None) -> dict:
    target_url = _resolve_rebind_url(spec, url)
    headers = dict(spec.get("headers") or {}) if isinstance(spec.get("headers"), Mapping) else {}
    headers.setdefault("accept", "application/json")
    headers.setdefault("content-type", "application/json")
    token = _extract_token(getattr(session, "_rebind_session_info", {}))
    if token:
        headers.setdefault("authorization", f"Bearer {token}")
    request = getattr(session, str(method or "GET").lower(), None)
    if not callable(request):
        raise RebindDriverError("协议 Session 不支持 HTTP 请求")
    current_method = str(method or "GET").upper()
    current_payload = payload
    for hop in range(_MAX_REBIND_REDIRECTS + 1):
        try:
            kwargs: dict[str, Any] = {"headers": dict(headers), "allow_redirects": False}
            if current_method in {"GET", "HEAD"}:
                if current_payload:
                    kwargs["params"] = current_payload
            else:
                kwargs["data"] = json.dumps(current_payload or {}, ensure_ascii=False)
            response = request(target_url, **kwargs)
        except RebindDriverError:
            raise
        except Exception as exc:
            raise RebindDriverError(f"协议换绑请求失败：{type(exc).__name__}") from exc
        status = int(getattr(response, "status_code", 0) or 0)
        if 300 <= status < 400:
            location = _response_header(response, "location")
            if not location or not _as_bool(spec.get("allow_redirects"), True):
                raise RebindDriverError("协议换绑请求重定向未获允许")
            next_url = _resolve_rebind_url(spec, urljoin(target_url, location), current_url=target_url)
            previous_host = str(urlparse(target_url).hostname or "").lower()
            next_host = str(urlparse(next_url).hostname or "").lower()
            if previous_host != next_host:
                # Even an explicitly allowlisted cross-host redirect does not
                # receive the account bearer token by default.
                headers = {
                    key: value for key, value in headers.items()
                    if str(key).lower() != "authorization"
                }
            target_url = next_url
            if status in {301, 302, 303}:
                current_method = "GET"
                current_payload = None
            continue
        response_url = str(getattr(response, "url", target_url) or target_url)
        _resolve_rebind_url(spec, response_url, current_url=target_url)
        data = _response_data(response)
        if not 200 <= status < 300:
            raise RebindHttpError(status, data=data)
        return {"status": status, "url": response_url, "data": data}
    raise RebindDriverError("协议换绑请求重定向次数过多")


def _browser_request(driver: Any, spec: Mapping[str, Any], *, method: str, url: str, payload: Any = None) -> dict:
    from core.account_export import _browser_fetch

    target_url = _resolve_rebind_url(spec, url)
    headers = dict(spec.get("headers") or {}) if isinstance(spec.get("headers"), Mapping) else {}
    headers.setdefault("accept", "application/json")
    headers.setdefault("content-type", "application/json")
    if str(method).upper() in {"GET", "HEAD"} and payload:
        query = urlencode(payload, doseq=True)
        target_url = f"{target_url}{'&' if '?' in target_url else '?'}{query}"
        body = None
    else:
        body = json.dumps(payload or {}, ensure_ascii=False)
    try:
        try:
            result = _browser_fetch(
                driver,
                target_url,
                method=str(method or "GET").upper(),
                headers=headers,
                body=body,
                stage="rebind_email_change",
                allow_redirects=False,
            )
        except TypeError as exc:
            # Older integrations may not expose the optional redirect switch;
            # retain compatibility and validate the final URL below.
            if "allow_redirects" not in str(exc):
                raise
            result = _browser_fetch(
                driver,
                target_url,
                method=str(method or "GET").upper(),
                headers=headers,
                body=body,
                stage="rebind_email_change",
            )
    except Exception as exc:
        if isinstance(exc, RebindDriverError):
            raise
        raise RebindDriverError(f"浏览器换绑请求失败：{type(exc).__name__}") from exc
    status = int(result.get("status") or 0)
    final_url = str(result.get("url") or target_url)
    _resolve_rebind_url(spec, final_url, current_url=target_url)
    if not 200 <= status < 300:
        data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
        raise RebindHttpError(status, data=data)
    return result


def _request_with_otp_retry(
    request: Callable[..., dict],
    transport: Any,
    spec: Mapping[str, Any],
    *,
    method: str,
    url: Any,
    payload: Any,
    variables: Mapping[str, Any],
    requires_otp: bool,
    target: Mapping[str, Any],
    otp_getter: Callable[..., str],
    log: Callable[[str], None] | None,
    after_ts: float | None = None,
) -> dict:
    """Issue one configured request, refreshing a rejected OTP when needed."""
    attempts = _otp_attempt_count(spec) if requires_otp else 1
    excluded: set[str] = set()
    started = float(after_ts if after_ts is not None else time.time())
    last_code = ""
    for attempt in range(attempts):
        rendered = dict(variables)
        if requires_otp:
            last_code = otp_getter(
                email=str(target.get("email") or ""),
                after_ts=started,
                exclude_codes=excluded,
                target=dict(target),
            )
            rendered.update({"otp": last_code, "code": last_code})
        rendered_payload = _render(payload, rendered)
        try:
            return request(
                transport,
                spec,
                method=method,
                url=_render(url, rendered),
                payload=rendered_payload,
            )
        except RebindHttpError as exc:
            if not requires_otp or not _response_otp_failure(exc.status, exc.data) or attempt + 1 >= attempts:
                raise
            if last_code:
                excluded.add(last_code)
            resend_url = spec.get("otp_resend_url") or spec.get("resend_url")
            if resend_url:
                resend_method = str(spec.get("otp_resend_method") or "POST")
                resend_payload = _render(spec.get("otp_resend_payload") or {}, {**rendered, "otp": "", "code": ""})
                request(
                    transport,
                    spec,
                    method=resend_method,
                    url=_render(resend_url, rendered),
                    payload=resend_payload,
                )
            _safe_log(log, f"验证码校验失败，等待新验证码重试（第 {attempt + 2}/{attempts} 次）")
            delay = _otp_retry_delay(spec)
            if delay:
                time.sleep(delay)
            started = time.time()
    raise RebindDriverError("邮箱验证码重试失败")


def _generic_action(
    context: RebindContext,
    *,
    action_driver: str,
    otp_getter: Callable[..., str],
    log: Callable[[str], None] | None,
) -> dict:
    spec = _api_spec(context.account, context.target)
    if not spec:
        raise RebindDriverError("未配置邮箱变更端点或提交钩子")
    request = _protocol_request if action_driver == "protocol" else _browser_request
    transport = context.session if action_driver == "protocol" else context.driver
    if transport is None:
        raise RebindDriverError("换绑提交阶段缺少登录传输")

    variables = {
        "target_email": context.target.get("email"),
        "new_email": context.target.get("email"),
        "email": context.target.get("email"),
    }
    steps = spec.get("steps")
    responses: list[dict] = []
    if isinstance(steps, list) and steps:
        for raw_step in steps:
            if not isinstance(raw_step, Mapping):
                continue
            step = dict(raw_step)
            requires_otp = _as_bool(step.get("requires_otp") or step.get("otp"), False)
            response = _request_with_otp_retry(
                request,
                transport,
                spec,
                method=str(step.get("method") or "POST"),
                url=step.get("url") or spec.get("endpoint"),
                payload=step.get("payload") or step.get("body") or {},
                variables=variables,
                requires_otp=requires_otp,
                target=context.target,
                otp_getter=otp_getter,
                log=log,
                after_ts=time.time(),
            )
            responses.append(response)
            data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
            if data:
                variables.update({key: value for key, value in data.items() if isinstance(key, str) and isinstance(value, (str, int, float, bool))})
        final = responses[-1] if responses else {}
        return {"ok": True, **(final.get("data") if isinstance(final.get("data"), Mapping) else {}), "responses": responses}

    endpoint = spec.get("submit_url") or spec.get("endpoint") or spec.get("start_url")
    if not endpoint:
        raise RebindDriverError("邮箱变更端点未配置")
    email_field = str(spec.get("email_field") or "email")
    payload = spec.get("submit_payload") or spec.get("payload") or {email_field: context.target.get("email")}
    otp_start = time.time()
    first = _request_with_otp_retry(
        request,
        transport,
        spec,
        method=str(spec.get("submit_method") or spec.get("method") or "POST"),
        url=endpoint,
        payload=payload,
        variables=variables,
        requires_otp=_as_bool(spec.get("submit_requires_otp"), False),
        target=context.target,
        otp_getter=otp_getter,
        log=log,
        after_ts=otp_start,
    )
    responses.append(first)
    data = first.get("data") if isinstance(first.get("data"), Mapping) else {}
    verify_url = spec.get("verify_url") or (data or {}).get("verify_url") or (data or {}).get("verification_url")
    otp_required = _as_bool(spec.get("otp_required"), bool(verify_url or (data or {}).get("otp_required") or (data or {}).get("verification_required")))
    if otp_required:
        if not verify_url:
            raise RebindDriverError("变更响应要求 OTP，但未提供验证端点")
        code_field = str(spec.get("code_field") or "code")
        verify_payload = spec.get("verify_payload") or {email_field: context.target.get("email"), code_field: "{otp}"}
        second = _request_with_otp_retry(
            request,
            transport,
            spec,
            method=str(spec.get("verify_method") or "POST"),
            url=verify_url,
            payload=verify_payload,
            variables=variables,
            requires_otp=True,
            target=context.target,
            otp_getter=otp_getter,
            log=log,
            after_ts=otp_start,
        )
        responses.append(second)
        data = second.get("data") if isinstance(second.get("data"), Mapping) else data
    _safe_log(log, f"{action_driver} 提交：邮箱变更请求已返回 HTTP {responses[-1].get('status', 0)}")
    return {"ok": True, **dict(data or {}), "responses": responses}


def _verify_remote(
    context: RebindContext,
    *,
    hooks: dict,
    target_email: str,
    log: Callable[[str], None] | None,
) -> dict:
    verifier = hooks.get("verify")
    if verifier:
        raw = _invoke(
            verifier,
            account=context.account,
            target=context.target,
            target_email=target_email,
            context=context,
            session=context.session,
            driver=context.driver,
            action_result=context.action_result,
            log=log,
        )
        verified = _normalize_hook_result(context, raw)
    elif context.action_driver == "protocol":
        if context.session is None:
            raise RebindVerificationError("协议验证缺少登录态")
        verified = _fetch_protocol_session(context.session)
        context.session_info = dict(verified)
    else:
        if context.driver is None:
            raise RebindVerificationError("浏览器验证缺少登录态")
        from core.account_export import _browser_session_info

        try:
            verified = _browser_session_info(context.driver)
        except Exception as exc:
            raise RebindVerificationError(f"浏览器远端 Session 验证失败：{type(exc).__name__}") from exc
        context.session_info = dict(verified)

    # Both proof values must come from the verification response itself.  A
    # login token or submit response is stale evidence and must not authorize
    # deleting/replacing the local account.
    observed = _extract_email(verified)
    token = _extract_token(verified)
    if not observed or observed.casefold() != target_email.casefold():
        raise RebindVerificationError("远端当前邮箱与目标邮箱不一致")
    if not token:
        raise RebindVerificationError("远端验证未返回 Access Token")
    session_info = _verified_session_snapshot(verified, observed, token)
    result = {
        "ok": True,
        "verified_email": observed,
        "access_token": token,
        "session": session_info,
        "login_driver": context.login_driver,
        "action_driver": context.action_driver,
        "hybrid": context.hybrid,
    }
    _safe_log(log, "远端验证：目标邮箱与新登录态已确认")
    return result


def rebind_account(
    account: dict,
    target: dict | str,
    *,
    driver: str | None = None,
    login_driver: str | None = None,
    action_driver: str | None = None,
    hybrid: Any = True,
    headless: Any = False,
    login_headless: Any = None,
    proxy: str | None = None,
    log: Callable[[str], None] | None = None,
    hooks: Mapping[str, Callable[..., Any]] | RebindHooks | None = None,
) -> dict:
    """Execute login → email change → remote verification.

    ``driver=`` remains a compatibility shorthand selecting both stages.  New
    callers should pass ``login_driver`` and ``action_driver`` independently.
    The return value intentionally matches ``rebind_service``'s strict success
    contract: ``ok``, target ``verified_email``, ``access_token`` and ``session``.
    """
    if not isinstance(account, Mapping):
        raise RebindDriverError("原账号记录格式无效")
    account = dict(account)
    target = {"email": target} if isinstance(target, str) else dict(target or {})
    source_email = _email(account.get("email"), "原账号邮箱")
    target_email = _email(target.get("email"), "目标邮箱")
    if source_email.casefold() == target_email.casefold():
        raise RebindDriverError("目标邮箱必须不同于原账号邮箱")

    explicit_stage = login_driver is not None or action_driver is not None
    if driver is not None and not explicit_stage:
        login_name = action_name = _normalize_driver(driver)
        mixed = False
    else:
        login_name = _normalize_driver(login_driver, default="cloak")
        action_name = _normalize_driver(action_driver, default="protocol")
        mixed = _as_bool(hybrid, True)
        if not mixed:
            login_name = action_name
    selected_login_headless = _as_bool(headless, False) if login_headless is None else _as_bool(login_headless, False)
    selected_headless = _as_bool(headless, False)
    hook_map = _hook_map(hooks)
    otp_getter = _make_otp_getter(hook_map.get("otp"), default_target=target, log=log)
    context = RebindContext(
        account=account,
        target=target,
        login_driver=login_name,
        action_driver=action_name,
        hybrid=mixed,
        proxy=proxy,
    )
    # The task service passes proxy separately; browser action transport uses it
    # when it has to open a second browser after protocol login.
    account.setdefault("rebind_proxy", proxy)
    try:
        login_hook = hook_map.get("login_protocol" if login_name == "protocol" else "login_browser")
        if login_hook:
            raw_login = _invoke(
                login_hook,
                account=account,
                target=target,
                target_email=target_email,
                context=context,
                proxy=proxy,
                driver=login_name,
                driver_name=login_name,
                headless=selected_login_headless,
                log=log,
                get_otp=otp_getter,
            )
            _normalize_hook_result(context, raw_login)
            _require_stage_success(raw_login, "登录")
        elif login_name == "protocol":
            session, info = _protocol_login_builtin(account, proxy=proxy, otp_getter=otp_getter, log=log, hooks=hook_map)
            context.session = session
            context.session_info = info
            context.add_closer(lambda session=session: _close_resource(session))
        else:
            browser, closer, info = _browser_login_builtin(
                account,
                driver_name=login_name,
                proxy=proxy,
                headless=selected_login_headless,
                hooks=hook_map,
                log=log,
            )
            context.driver = browser
            context.driver_kind = login_name
            context.add_closer(closer or (lambda browser=browser: _close_resource(browser)))
            context.session_info = info

        # Never let a hook silently switch the account context.  Built-in
        # logins already return the identity; injected hooks must expose the
        # same email through ``session_info``/their result before submission.
        _validate_login_identity(context, source_email)

        submit_hook = hook_map.get("submit_protocol" if action_name == "protocol" else "submit_browser")
        # A built-in submitter needs a concrete transport.  A site-specific
        # hook may own its transport (for example, a remote automation service
        # that returns only a verified session), so do not force a local
        # browser/protocol conversion before invoking that hook.
        if not submit_hook:
            if action_name == "protocol":
                _ensure_protocol_transport(context, hook_map, log)
            else:
                _ensure_browser_transport(context, hook_map, selected_headless, log)

        # The generic protocol request builder reads this private, in-memory
        # hint to attach the freshly observed Bearer token.  It is never
        # serialized or included in task/log responses.
        if context.session is not None:
            try:
                context.session._rebind_session_info = dict(context.session_info or {})
            except Exception:
                pass

        if submit_hook:
            raw_action = _invoke(
                submit_hook,
                account=account,
                target=target,
                target_email=target_email,
                context=context,
                session=context.session,
                driver=context.driver,
                login_driver=login_name,
                action_driver=action_name,
                hybrid=mixed,
                headless=selected_headless,
                login_headless=selected_login_headless,
                proxy=proxy,
                get_otp=otp_getter,
                otp_provider=otp_getter,
                log=log,
            )
            _normalize_hook_result(context, raw_action)
            _require_stage_success(raw_action, "提交")
        else:
            raw_action = _generic_action(
                context,
                action_driver=action_name,
                otp_getter=otp_getter,
                log=log,
            )
            _normalize_hook_result(context, raw_action)
            _require_stage_success(raw_action, "提交")

        return _verify_remote(context, hooks=hook_map, target_email=target_email, log=log)
    finally:
        context.close()


__all__ = [
    "SUPPORTED_DRIVERS",
    "RebindDriverError",
    "RebindVerificationError",
    "RebindHttpError",
    "RebindHooks",
    "RebindContext",
    "set_rebind_hooks",
    "clear_rebind_hooks",
    "get_rebind_hooks",
    "rebind_account",
]
