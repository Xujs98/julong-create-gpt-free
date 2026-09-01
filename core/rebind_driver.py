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
from datetime import datetime
from pathlib import Path
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

    def close(self, *, failed: bool = False) -> None:
        if self.closed:
            return
        self.closed = True
        for closer in reversed(self.closers):
            try:
                try:
                    parameters = inspect.signature(closer).parameters
                except (TypeError, ValueError):
                    parameters = {}
                accepts_failed = "failed" in parameters or any(
                    item.kind is inspect.Parameter.VAR_KEYWORD
                    for item in parameters.values()
                )
                if accepts_failed:
                    closer(failed=failed)
                else:
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


def _pick_rebind_pool_proxy(failed_proxy: str | None = None) -> str:
    """Pick one rebind route from PROXY_POOL, preferring a different entry."""
    from config import proxy as proxy_cfg

    failed = str(failed_proxy or "").strip()
    selected = str(proxy_cfg.pick_proxy() or "").strip()
    if selected and selected != failed:
        return selected
    candidates: list[str] = []
    for raw in list(getattr(proxy_cfg, "PROXY_POOL", []) or []):
        value = str(raw or "").strip()
        if value and value not in candidates:
            candidates.append(value)
    for candidate in candidates:
        if candidate != failed:
            return candidate
    # 单出口代理池仍允许同一入口再试一次；其上游可能会自动轮换真实出口 IP。
    return selected or (candidates[0] if candidates else "")


def _resolve_rebind_proxy(account: Mapping[str, Any], explicit_proxy: str | None) -> str:
    """Use an explicit task proxy or select a fresh PROXY_POOL route.

    Account-level ``live_check_proxy_used``/``proxy_used`` values intentionally
    do not participate: rebind always starts from the current proxy pool rather
    than inheriting an old account route.
    """
    selected = str(explicit_proxy or "").strip() or _pick_rebind_pool_proxy()
    if not selected:
        raise RebindDriverError("换绑需要代理池出口，但当前 PROXY_POOL 为空")
    return selected


def _browser_error_text(exc: BaseException | None) -> str:
    """Collect short root-cause text from wrapped browser exceptions."""
    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(parts) < 4:
        seen.add(id(current))
        text = str(current).strip()
        if text:
            parts.append(text.splitlines()[0][:240])
        current = current.__cause__ or current.__context__
    return " | ".join(parts)


def _is_browser_network_error(exc: BaseException | None) -> bool:
    text = f"{type(exc).__name__} {_browser_error_text(exc)}".lower()
    return any(marker in text for marker in (
        "err_socks_connection_failed", "err_proxy_connection_failed", "proxy",
        "net::err_", "connection refused", "connection reset", "timed out",
        "network is unreachable", "name_not_resolved",
    ))


def _rebind_proxy_fallbacks(failed_proxy: str | None) -> list[str]:
    """Return one pool route for retry; rebind never falls back to direct."""
    try:
        selected = _pick_rebind_pool_proxy(failed_proxy)
    except Exception:
        selected = ""
    return [selected] if selected else []


def _protocol_preflight_with_fallback(
    email: str,
    proxy: str | None,
    *,
    log: Callable[[str], None] | None,
) -> tuple[Any, str]:
    """Run protocol preflight and rotate only within the configured pool."""
    from core.account_liveness import _network_preflight_with_retry

    candidates: list[str | None] = [proxy]
    if proxy:
        candidates.extend(_rebind_proxy_fallbacks(proxy))
    unique: list[str | None] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = "<none>" if candidate is None else str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)

    errors: list[str] = []
    for index, candidate in enumerate(unique):
        if index:
            _safe_log(
                log,
                "协议登录出口连接异常，轮换代理池出口（模式=proxy_pool）",
            )
        try:
            return _network_preflight_with_retry(
                email,
                candidate,
                max_attempts=2,
                rotate_proxy_on_retry=True,
            )
        except Exception as exc:
            if not _is_browser_network_error(exc):
                raise
            errors.append(_browser_error_text(exc) or type(exc).__name__)

    detail = "; ".join(item for item in errors if item)[:500]
    raise RebindDriverError(f"协议登录网络出口均失败：{detail or '代理连接失败'}")


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


def _validate_login_identity(
    context: RebindContext,
    source_email: str,
    *,
    require_token: bool = True,
) -> None:
    """Require the login transport to identify the selected source account."""
    observed = _extract_email(context.session_info)
    if not observed:
        raise RebindDriverError("登录态未返回原账号邮箱，已停止换绑")
    if observed.casefold() != source_email.casefold():
        raise RebindDriverError("登录态账号与所选原账号不一致，已停止换绑")
    if require_token and not _extract_token(context.session_info):
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
    """Run a fresh protocol login and retain the resulting authenticated session."""
    from core.account_export import fetch_session, follow_oauth_callback
    from core.account_liveness import (
        _auth_payload_value,
        _is_email_verification_state,
        _navigate_auth_step,
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
    login_password = str(account.get("registration_password") or "").strip()
    try:
        _safe_log(log, "协议登录步骤 1/7：创建全新协议会话，不复用旧 Session/Cookie")
        session, authorize_url = _protocol_preflight_with_fallback(email, proxy, log=log)
        _safe_log(log, "协议登录步骤 2/7：网络预检通过，打开授权登录流程")
        final_url = follow_authorize(session, authorize_url, allow_password_page=bool(login_password))

        if login_password:
            _safe_log(log, "协议登录步骤 3/7：原账号保存了密码，进入邮箱和密码验证")
            if _is_email_verification_state(final_url):
                raise RebindDriverError("保存密码账号进入邮箱验证码页，登录状态不一致")
            if "/log-in/password" not in final_url.lower():
                payload = continue_authorize_with_email(session, email)
                if _is_email_verification_state(payload=payload):
                    raise RebindDriverError("密码登录推进返回邮箱验证码页")
                next_url = _auth_payload_value(payload, "continue_url", "external_url", "redirect_url", "url")
                if next_url and "password" in next_url.lower():
                    final_url = _navigate_auth_step(session, next_url, final_url)
            _safe_log(log, "协议登录步骤 4/7：提交账号密码")
            auth_result = verify_login_password(session, login_password)
            if _is_email_verification_state(payload=auth_result):
                raise RebindDriverError("密码校验返回邮箱验证码页")
            factor = extract_totp_factor(auth_result)
            if factor:
                secret = str(account.get("totp_secret") or "").replace(" ", "").strip()
                if not secret:
                    raise RebindDriverError("账号要求 TOTP，但未保存 TOTP secret")
                _safe_log(log, "协议登录步骤 5/7：检测到 2FA，生成并提交动态验证码")
                challenge = issue_mfa_challenge(session, factor)
                challenge_id = _auth_payload_value(challenge, "mfa_request_id")
                verify_factor = dict(factor)
                if challenge_id:
                    verify_factor["metadata"] = {**(factor.get("metadata") or {}), "mfa_request_id": challenge_id}
                auth_result = verify_mfa_code(session, verify_factor, pyotp.TOTP(secret).now())
                _safe_log(log, "协议登录步骤 5/7：2FA 验证已通过")
            continue_url = _auth_payload_value(auth_result, "continue_url", "external_url", "redirect_url", "url", "location")
            if not continue_url:
                raise RebindDriverError("密码登录未返回 OAuth continue_url")
            follow_oauth_callback(session, continue_url, referer="https://auth.openai.com/log-in/password")
        else:
            _safe_log(log, "协议登录步骤 3/7：账号未保存密码，切换原邮箱验证码登录")
            # Capture the boundary before sending the message so fast mailboxes
            # cannot race between the send request and OTP polling.
            otp_after_ts = time.time()
            send_email_otp(session)
            _safe_log(log, "协议登录步骤 4/7：原邮箱登录验证码已发送，开始取码")
            code = otp_getter(email=email, after_ts=otp_after_ts, target={"email": email})
            _safe_log(log, "协议登录步骤 5/7：已取得原邮箱验证码并提交")
            validate_result = validate_email_otp(session, code)
            continue_url = _auth_payload_value(validate_result, "continue_url", "external_url", "redirect_url", "url", "location")
            if not continue_url:
                raise RebindDriverError("邮箱 OTP 登录未返回 OAuth continue_url")
            follow_oauth_callback(session, continue_url, referer="https://auth.openai.com/email-verification")

        _safe_log(log, "协议登录步骤 6/7：OAuth 回调已完成，刷新远端 Session")
        info = _fetch_protocol_session(session)
    except Exception:
        _close_resource(session)
        raise
    _safe_log(log, "协议登录步骤 7/7：已完成完整登录并建立新登录态")
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
    _safe_log(log, f"{driver_name} 登录准备：启动独立指纹浏览器环境")
    driver, closer = _open_browser_builtin(driver_name, proxy, headless, hooks, stage="login", log=log)
    if driver is None:
        if callable(closer):
            try:
                closer()
            except Exception:
                logger.debug("浏览器驱动为空时资源关闭失败", exc_info=True)
        raise RebindDriverError("浏览器驱动工厂未返回 driver")
    _safe_log(log, f"{driver_name} 登录准备：浏览器环境已启动，开始完整账号登录")
    try:
        # 换绑必须重新证明账号登录能力，不复用浏览器工厂、自身账号记录中
        # 携带的 Session/Cookie。直接复用查活的完整登录实现：有密码时执行
        # 邮箱 -> 密码 -> TOTP，无密码时切换到邮箱验证码登录。
        from core.browser_liveness import _browser_login

        info = _browser_login(
            driver,
            account,
            _email(account.get("email"), "原账号邮箱"),
            headless=bool(headless),
            restore_saved_session=False,
            progress=lambda message: _safe_log(log, f"{driver_name} {message}"),
            require_session=False,
        )
        if not isinstance(info, Mapping) or not _as_bool(info.get("loginConfirmed"), False):
            raise RebindDriverError("浏览器登录后页面未确认")
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
        detail = _browser_error_text(exc)
        suffix = f": {detail}" if detail else ""
        raise RebindDriverError(f"{driver_name} 登录失败：{type(exc).__name__}{suffix}") from exc
    _safe_log(log, f"{driver_name} 登录：已重新完成原账号登录流程；Session 将在换绑成功后刷新")
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


def _browser_page_state(driver: Any) -> dict:
    """Return a small, secret-free snapshot used by the account-settings flow."""
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const attrs = el => [el.type, el.name, el.id, el.placeholder, el.autocomplete,
          el.getAttribute('data-testid'), el.getAttribute('aria-label')].filter(Boolean).join(' ').toLowerCase();
        return {
          url: location.href,
          text: String(document.body?.innerText || '').replace(/\s+/g, ' ').slice(-3000),
          inputs: [...document.querySelectorAll('input')].filter(visible).map(el => ({
            type: el.type || '', name: el.name || '', id: el.id || '', attrs: attrs(el), valueLength: String(el.value || '').length
          })),
          buttons: [...document.querySelectorAll('button,[role=button],input[type=submit]')].filter(visible).map(el => ({
            text: String(el.innerText || el.textContent || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 120),
            attrs: attrs(el), disabled: !!el.disabled || String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
          }))
        };
        """) or {}
    except Exception as exc:
        return {"url": str(getattr(driver, "current_url", "") or ""), "error": f"{type(exc).__name__}: {exc}"}


def _wait_browser_state(driver: Any, predicate: Callable[[dict], bool], *, timeout: float = 30.0) -> dict:
    end = time.time() + max(1.0, float(timeout))
    last: dict = {}
    while time.time() < end:
        last = _browser_page_state(driver)
        try:
            if predicate(last):
                return last
        except Exception:
            logger.debug("浏览器换绑页面状态判断失败", exc_info=True)
        time.sleep(0.35)
    return last


def _browser_state_diagnostic(state: Mapping[str, Any] | None) -> str:
    """Return a compact page-shape diagnostic without input values."""
    item = dict(state or {})
    safe = {
        "url": str(item.get("url") or "")[:300],
        "inputs": [
            {
                "type": str(entry.get("type") or "")[:40],
                "attrs": str(entry.get("attrs") or "")[:180],
                "valueLength": int(entry.get("valueLength") or 0),
            }
            for entry in (item.get("inputs") or [])[:12]
            if isinstance(entry, Mapping)
        ],
        "buttons": [
            {
                "text": str(entry.get("text") or "")[:100],
                "attrs": str(entry.get("attrs") or "")[:160],
                "disabled": bool(entry.get("disabled")),
            }
            for entry in (item.get("buttons") or [])[:16]
            if isinstance(entry, Mapping)
        ],
    }
    return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))[:1800]


def _save_rebind_browser_screenshot(driver: Any, stage: str) -> str | None:
    """Persist a local screenshot for a failed browser stage."""
    try:
        root = Path(__file__).resolve().parents[1] / "注册日志" / "rebind-diagnostics"
        root.mkdir(parents=True, exist_ok=True)
        safe_stage = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(stage or "failure"))[:40] or "failure"
        path = root / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe_stage}.png"
        saver = getattr(driver, "save_screenshot", None)
        if callable(saver) and saver(str(path)):
            return str(path)
    except Exception:
        logger.debug("换绑失败截图保存失败", exc_info=True)
    return None


def _clear_rebind_resource_timings(driver: Any) -> None:
    try:
        driver.execute_script("performance.clearResourceTimings(); return true;")
    except Exception:
        logger.debug("清空换绑资源计时失败", exc_info=True)


def _rebind_resource_diagnostic(driver: Any) -> list[dict]:
    """Read recent fetch/XHR paths without query strings or request bodies."""
    try:
        rows = driver.execute_script(r"""
        return performance.getEntriesByType('resource')
          .filter(x => ['fetch','xmlhttprequest'].includes(String(x.initiatorType || '').toLowerCase()))
          .slice(-30)
          .map(x => {
            try {
              const u = new URL(x.name, location.href);
              return {origin:u.origin, path:u.pathname, type:x.initiatorType, duration:Math.round(x.duration || 0)};
            } catch (_) {
              return {origin:'', path:String(x.name || '').split('?')[0].slice(0,300), type:x.initiatorType, duration:Math.round(x.duration || 0)};
            }
          });
        """) or []
        return [dict(row) for row in rows if isinstance(row, Mapping)]
    except Exception:
        logger.debug("读取换绑网络诊断失败", exc_info=True)
        return []


def _submit_browser_email_form(driver: Any, *, timeout: float = 12.0) -> dict:
    """Submit the visible new-email form without depending on localization."""
    end = time.time() + max(1.0, float(timeout))
    last: dict = {"ok": False, "reason": "missing_email_submit"}
    while time.time() < end:
        try:
            result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const enabled = el => visible(el) && !el.disabled
          && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
        const email = [...document.querySelectorAll('input')].find(el => {
          if (!enabled(el)) return false;
          const attrs = [el.type, el.name, el.id, el.placeholder, el.autocomplete, el.getAttribute('aria-label')]
            .filter(Boolean).join(' ').toLowerCase();
          return String(el.value || '').includes('@') && /email|mail|username/.test(attrs);
        });
        if (!email) return {ok:false, reason:'missing_email_input'};
        email.setAttribute('data-rebind-email-input', '1');
        email.dispatchEvent(new Event('input', {bubbles:true}));
        email.dispatchEvent(new Event('change', {bubbles:true}));
        const form = email.closest('form');
        const dialog = email.closest('[role="dialog"],[data-radix-dialog-content]');
        // The current Edit email UI puts its action footer outside the inner
        // form.  Scoping to form first leaves no Send button and the old global
        // fallback can hit an unrelated background control.  Prefer the
        // enclosing dialog so Cancel + Send verification email are evaluated
        // together.
        let localScope = dialog || form;
        if (!localScope) {
          let parent = email.parentElement;
          while (parent && parent !== document.body) {
            if (parent.querySelector('button,input[type="submit"],[role="button"]')) {
              localScope = parent;
              break;
            }
            parent = parent.parentElement;
          }
        }
        const attrs = el => [el.type, el.name, el.id, el.className,
          el.getAttribute('data-testid'), el.getAttribute('data-dd-action-name'),
          el.getAttribute('data-variant'), el.getAttribute('data-color'),
          el.getAttribute('aria-label')].filter(Boolean).join(' ').toLowerCase();
        const selector = 'button,input[type=submit],[role=button]';
        // Never leave the Edit email container. A global lookup can select the
        // chat composer or a settings-background control and makes the dialog
        // look as if it ignored submission.
        const candidates = localScope
          ? [...localScope.querySelectorAll(selector)].filter(enabled)
          : [];
        const usable = candidates.filter(el => !/close|dismiss|cancel|back|secondary|ghost/.test(attrs(el)));
        let submit = usable.find(el => String(el.type || '').toLowerCase() === 'submit')
          || usable.find(el => /submit|continue|verify|primary|confirm/.test(attrs(el)));
        if (!submit && dialog) {
          // Current UI: close icon, then Cancel, then the primary footer action.
          // The final enabled action inside the dialog is the structural primary
          // control regardless of language or visible label.
          submit = usable[usable.length - 1] || null;
        }
        if (submit) {
          submit.scrollIntoView({block:'center', inline:'nearest'});
          if (form && typeof form.requestSubmit === 'function' && form.contains(submit)) form.requestSubmit(submit);
          else submit.click();
          return {
            ok:true,
            strategy:'dialog_primary_element',
            element:{tag:String(submit.tagName || ''), type:String(submit.type || ''), attrs:attrs(submit).slice(0,180)}
          };
        }
        if (form && typeof form.requestSubmit === 'function') {
          try {
            form.requestSubmit();
            return {ok:true, strategy:'form_request_submit', text:''};
          } catch (_) {}
        }
        return {
          ok:false,
          reason:'missing_email_submit',
          hasForm:!!form,
          hasLocalScope:!!localScope,
          candidates:candidates.slice(0,12).map(el => ({tag:String(el.tagName || ''), attrs:attrs(el).slice(0,180), disabled:!!el.disabled}))
        };
        """) or {}
            last = result if isinstance(result, dict) else {"ok": False, "reason": "invalid_submit_result"}
            if last.get("ok"):
                return last
        except Exception as exc:
            last = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        time.sleep(0.35)

    # Some React dialogs handle Enter on the input while exposing no semantic
    # submit button/form. Use the same marked input as a final Selenium fallback.
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        fields = driver.find_elements(By.CSS_SELECTOR, '[data-rebind-email-input="1"]')
        if fields:
            fields[0].send_keys(Keys.ENTER)
            return {"ok": True, "strategy": "input_enter", "text": ""}
    except Exception as exc:
        last["enterFallback"] = f"{type(exc).__name__}: {exc}"
    return last


def _browser_ui_action(
    context: RebindContext,
    *,
    otp_getter: Callable[..., str],
    log: Callable[[str], None] | None,
) -> dict:
    """Change the mailbox through the ChatGPT account-settings UI.

    The site endpoint is intentionally not hard-coded: the browser executes
    the account-settings flow while the verification stage still reads a fresh
    remote Session before local replacement.
    """
    driver = context.driver
    if driver is None:
        raise RebindDriverError("浏览器换绑缺少 driver")
    target_email = _email(context.target.get("email"), "目标邮箱")
    try:
        from core.roxy_registration import (
            _clear_otp_inputs,
            _click_continue,
            _click_resend_email_otp,
            _human_click,
            _safe_get,
            _type_email_address,
            _type_otp,
            _wait_after_email_otp_submit,
        )
        from core.browser_liveness import (
            _fill_login_password,
            _is_totp_page,
            _submit_totp_with_stable_window,
        )
    except Exception as exc:
        raise RebindDriverError(f"浏览器换绑组件加载失败：{type(exc).__name__}") from exc

    def _has_email_input(item: dict) -> bool:
        return any(
            str(entry.get("type") or "").lower() == "email"
            or re.search(r"email|mail|username", str(entry.get("attrs") or ""), re.I)
            for entry in item.get("inputs") or []
        )

    def _is_logged_in_page(item: dict) -> bool:
        """Recognize the post-MFA ChatGPT shell before reopening settings."""
        url = str(item.get("url") or "").lower()
        button_text = " ".join(
            " ".join(str(entry.get(key) or "") for key in ("text", "attrs"))
            for entry in item.get("buttons") or []
        ).lower()
        text = f"{item.get('text') or ''} {button_text}".lower()
        return "chatgpt.com" in url and any(marker in text for marker in (
            "new chat", "skip to content", "open sidebar", "新建聊天", "跳转到内容",
        ))

    def _log_failure_state(stage: str, state: Mapping[str, Any] | None = None) -> None:
        snapshot = dict(state or _browser_page_state(driver))
        _safe_log(log, f"浏览器换绑诊断[{stage}]：{_browser_state_diagnostic(snapshot)}")
        screenshot = _save_rebind_browser_screenshot(driver, stage)
        if screenshot:
            _safe_log(log, f"浏览器换绑诊断截图：{screenshot}")

    def _open_email_settings_entry() -> dict:
        _safe_log(log, "浏览器换绑步骤 1/8：打开账号设置页")
        try:
            driver.get("https://chatgpt.com/#settings/Account")
        except Exception as exc:
            raise RebindDriverError(f"浏览器打开账号设置失败：{type(exc).__name__}") from exc

        def _has_email_settings_entry(item: Mapping[str, Any]) -> bool:
            return any(
                "account-info-email" in str(entry.get("attrs") or "")
                or re.search(
                    r"email|e-mail|邮箱|郵箱|メール|correo",
                    f"{entry.get('text') or ''} {entry.get('attrs') or ''}",
                    re.I,
                )
                for entry in item.get("buttons") or []
                if isinstance(entry, Mapping)
            )

        state = _wait_browser_state(
            driver,
            _has_email_settings_entry,
            timeout=8,
        )
        if not _has_email_settings_entry(state):
            # The current ChatGPT shell sometimes ignores direct hash
            # navigation.  Open Settings through the already-authenticated
            # profile menu, then select the Account tab.  This mirrors the
            # manual UI path and works across English/Chinese localizations.
            _safe_log(log, "浏览器换绑步骤 1/8：设置直达链接未展开，改走个人资料菜单")
            try:
                # Fully return to the shell before opening the menu. Merely
                # replacing the stale hash leaves React's settings router in
                # a half-open state where the profile control receives the
                # click but no menu is mounted.
                _safe_get(
                    driver,
                    "https://chatgpt.com/",
                    timeout=35,
                    attempts=2,
                    accept_hosts=("chatgpt.com",),
                )
                _wait_browser_state(driver, _is_logged_in_page, timeout=15)
                driver.execute_script("history.replaceState(null, '', '/'); return true;")
            except Exception:
                logger.debug("从失效设置路由返回 ChatGPT 首页失败", exc_info=True)
            profile_result: Mapping[str, Any] = {}
            opened_profile = False
            try:
                from selenium.webdriver.common.by import By

                native_candidates = driver.find_elements(
                    By.CSS_SELECTOR,
                    '.accounts-profile-button,[data-testid="accounts-profile-button"],'
                    '[data-testid*="profile"],button[aria-label*="profile" i],'
                    'button[aria-label*="个人资料"],button[aria-label*="账户"]',
                )
                native_candidates = [
                    item for item in native_candidates
                    if getattr(item, "is_displayed", lambda: False)()
                ]
                if native_candidates:
                    # The full-width sidebar control is the final visible
                    # duplicate. Reuse the registration click implementation:
                    # its CDP pointer events are reliable in Roxy Chrome where
                    # ActionChains/element.click can leave the menu closed.
                    button = native_candidates[-1]
                    _human_click(driver, button, label="rebind_profile_menu")
                    opened_profile = True
                    profile_result = {"ok": True, "candidates": len(native_candidates), "strategy": "webdriver"}
            except Exception:
                opened_profile = False
            try:
                if not opened_profile:
                    profile_result = driver.execute_script(r"""
                const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                  && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
                const label = el => [el.innerText, el.textContent, el.className,
                  el.getAttribute('data-testid'), el.getAttribute('aria-label')]
                  .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
                const selectors = [
                  '[data-testid="accounts-profile-button"]', '.accounts-profile-button',
                  '[data-testid*="profile"]', 'button[aria-label*="profile" i]',
                  'button[aria-label*="个人资料"]', 'button[aria-label*="账户"]'
                ];
                const candidates = [...new Set([
                  ...selectors.flatMap(selector => [...document.querySelectorAll(selector)]),
                  ...[...document.querySelectorAll('button,[role=button]')]
                    .filter(el => /accounts-profile-button|profile menu|个人资料.*菜单|账户.*菜单/.test(label(el)))
                ])].filter(visible);
                if (!candidates.length) return {ok:false, candidates:0};
                // Prefer the bottom/sidebar profile control carrying visible
                // account text over compact duplicate controls in the shell.
                candidates.sort((a, b) => {
                  const score = el => (/accounts-profile-button/.test(label(el)) ? 1000 : 0)
                    + (/open profile menu|个人资料.*菜单|账户.*菜单/.test(label(el)) ? 500 : 0)
                    + (String(el.innerText || el.textContent || '').trim() ? 100 : 0)
                    + Math.max(0, el.getBoundingClientRect().top || 0);
                  return score(a) - score(b);
                });
                const button = candidates[candidates.length - 1];
                button.scrollIntoView({block:'center'});
                try { button.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true, pointerType:'mouse'})); } catch (_) {}
                button.click();
                try { button.dispatchEvent(new PointerEvent('pointerup', {bubbles:true, pointerType:'mouse'})); } catch (_) {}
                return {ok:true, candidates:candidates.length};
                """) or {}
                    opened_profile = bool(isinstance(profile_result, Mapping) and profile_result.get("ok"))
            except Exception:
                profile_result = {}
                opened_profile = False
            _safe_log(
                log,
                "浏览器换绑步骤 1/8：个人资料菜单"
                f"{'已点击' if opened_profile else '未找到'}（候选={int((profile_result or {}).get('candidates') or 0)}）",
            )
            if opened_profile:
                settings_clicked = False
                end = time.time() + 12.0
                while time.time() < end and not settings_clicked:
                    try:
                        from selenium.webdriver.common.by import By

                        native_settings = driver.find_elements(
                            By.CSS_SELECTOR,
                            'button,a,[role="button"],[role="menuitem"],'
                            '[role="menuitemradio"],[data-radix-collection-item]',
                        )
                        keywords = (
                            "settings", "设置", "設定", "设定", "preferences",
                            "偏好设置", "paramètres", "configuración", "einstellungen",
                        )
                        native_settings = [
                            item for item in native_settings
                            if getattr(item, "is_displayed", lambda: False)()
                            and any(
                                marker in str(
                                    getattr(item, "text", "")
                                    or item.get_attribute("aria-label")
                                    or ""
                                ).strip().lower()
                                for marker in keywords
                            )
                        ]
                        if native_settings:
                            _human_click(driver, native_settings[0], label="rebind_settings_menu")
                            settings_clicked = True
                            settings_result = {"ok": True, "candidates": len(native_settings), "strategy": "webdriver"}
                    except Exception:
                        settings_clicked = False
                    try:
                        if not settings_clicked:
                            settings_result = driver.execute_script(r"""
                        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
                        const text = el => String(el.innerText || el.textContent || el.getAttribute('aria-label') || '')
                          .replace(/\s+/g, ' ').trim().toLowerCase();
                        const candidates = [...document.querySelectorAll(
                          'button,a,[role=button],[role=menuitem],[role=menuitemradio],[data-radix-collection-item]'
                        )].filter(visible);
                        let item = candidates.find(el => /settings|设置|設定|设定|preferences|偏好设置|paramètres|configuración|einstellungen/.test(text(el)));
                        if (!item) {
                          const child = [...document.querySelectorAll('span,div')].filter(visible)
                            .find(el => /^(settings|设置|設定|设定|preferences|偏好设置|paramètres|configuración|einstellungen)$/.test(text(el)));
                          item = child && child.closest('button,a,[role=button],[role=menuitem],[data-radix-collection-item]');
                        }
                        if (!item) return {ok:false, candidates:candidates.length};
                        item.scrollIntoView({block:'center'});
                        try { item.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true, pointerType:'mouse'})); } catch (_) {}
                        item.click();
                        try { item.dispatchEvent(new PointerEvent('pointerup', {bubbles:true, pointerType:'mouse'})); } catch (_) {}
                        return {ok:true, candidates:candidates.length};
                        """) or {}
                            settings_clicked = bool(isinstance(settings_result, Mapping) and settings_result.get("ok"))
                    except Exception:
                        settings_result = {}
                        settings_clicked = False
                    if not settings_clicked:
                        time.sleep(0.3)
                _safe_log(
                    log,
                    "浏览器换绑步骤 1/8：设置菜单"
                    f"{'已点击' if settings_clicked else '未找到'}（候选={int((settings_result or {}).get('candidates') or 0)}）",
                )
                if not settings_clicked:
                    screenshot = _save_rebind_browser_screenshot(driver, "settings-menu-not-found")
                    if screenshot:
                        _safe_log(log, f"浏览器换绑诊断截图：{screenshot}")
                if settings_clicked:
                    # Account is not always the default settings section. Use
                    # its stable tab class/id instead of localized visible text.
                    end = time.time() + 12.0
                    while time.time() < end:
                        try:
                            current = _browser_page_state(driver)
                            if _has_email_settings_entry(current):
                                ready = True
                                break
                            from selenium.webdriver.common.by import By

                            account_tabs = driver.find_elements(
                                By.CSS_SELECTOR,
                                '.account-tab,[id*="trigger-account"],'
                                '[data-testid="account-tab"],[data-testid*="settings-account"]',
                            )
                            account_tabs = [
                                item for item in account_tabs
                                if getattr(item, "is_displayed", lambda: False)()
                            ]
                            if account_tabs:
                                _human_click(driver, account_tabs[0], label="rebind_account_tab")
                                ready = False
                            else:
                                ready = False
                        except Exception:
                            ready = False
                        if ready:
                            break
                        time.sleep(0.35)
            state = _wait_browser_state(driver, _has_email_settings_entry, timeout=25)
        _safe_log(log, "浏览器换绑步骤 2/8：账号设置页已加载，定位邮箱变更入口")
        try:
            clicked = bool(driver.execute_script(r"""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
            const entry = document.querySelector('[data-testid="account-info-email"]')
              || document.querySelector('[data-testid*="account-info-email"]')
              || document.querySelector('.account-info-email');
            if (!visible(entry)) return false;
            entry.scrollIntoView({block:'center', inline:'nearest'});
            entry.click();
            return true;
            """))
        except Exception:
            clicked = False
        if not clicked:
            _log_failure_state("missing-email-settings-entry", state)
            raise RebindDriverError(f"账号设置中未找到邮箱变更入口：{state.get('url') or ''}")
        _safe_log(log, "浏览器换绑步骤 3/8：已点击账号邮箱变更入口")
        return state

    _open_email_settings_entry()

    def _has_totp_challenge(item: dict) -> bool:
        attrs = " ".join(str(entry.get("attrs") or "") for entry in item.get("inputs") or []).lower()
        text = f"{item.get('url') or ''} {item.get('text') or ''} {attrs}".lower()
        if not re.search(r"one.?time|otp|mfa|2fa|totp|verification|numeric|code|tel", attrs):
            # Auth pages can render the input with only a generic type and
            # expose the MFA semantics through the live DOM/body text.  Reuse
            # the liveness detector instead of relying on a snapshot alone.
            try:
                return bool(_is_totp_page(driver, expect_totp=bool(context.account.get("totp_secret"))))
            except Exception:
                return False
        if any(marker in text for marker in (
            "email-verification", "email verification", "code sent to", "check your email",
            "verify your email", "邮箱验证码", "邮件验证码",
        )):
            return False
        return any(marker in text for marker in (
            "authenticator", "authentication app", "verification app", "one-time password",
            "security code", "/mfa", "mfa", "2fa", "totp", "双重验证", "验证器",
        ))

    def _submit_current_password_totp(secret: str) -> dict:
        """Complete the account-settings MFA gate, then return the next page state."""
        def _wait_after_submit(_code: str) -> dict:
            state = _wait_browser_state(
                driver,
                lambda item: _has_email_input(item)
                or _is_logged_in_page(item)
                or _has_totp_challenge(item),
                timeout=20,
            )
            if _has_email_input(state) or _is_logged_in_page(state):
                _safe_log(log, "浏览器换绑：当前密码后的 TOTP 验证已通过")
                return state
            raise RuntimeError(str(state.get("text") or "TOTP 页面未进入下一步")[:240])

        try:
            _safe_log(log, "浏览器换绑步骤 5/8：提交当前密码后的 2FA 动态验证码")
            return _submit_totp_with_stable_window(
                driver,
                secret,
                on_submitted=_wait_after_submit,
                max_attempts=2,
            )
        except Exception as exc:
            raise RebindDriverError(
                f"当前密码后的 TOTP 验证失败：{str(exc or 'unknown')[:400]}"
            ) from exc

    password_state = _wait_browser_state(
        driver,
        lambda item: any(str(entry.get("type") or "").lower() == "password" for entry in item.get("inputs") or []),
        timeout=15,
    )
    if any(str(entry.get("type") or "").lower() == "password" for entry in password_state.get("inputs") or []):
        _safe_log(log, "浏览器换绑步骤 4/8：检测到当前密码验证页")
        password = str(context.account.get("registration_password") or context.account.get("password") or "").strip()
        if not password:
            raise RebindDriverError("邮箱变更需要当前密码，请先在账号资料中保存登录密码")
        email_state = password_state
        for password_attempt in range(2):
            try:
                _fill_login_password(driver, password)
            except Exception as exc:
                raise RebindDriverError(f"当前密码验证失败：{type(exc).__name__}") from exc
            _safe_log(log, "浏览器换绑步骤 4/8：当前密码已提交，等待下一验证步骤")
            email_state = _wait_browser_state(
                driver,
                lambda item: _has_email_input(item)
                or _has_totp_challenge(item)
                or "timed out" in str(item.get("text") or "").lower()
                or "network error" in str(item.get("text") or "").lower()
                or "operation timed out" in str(item.get("text") or "").lower(),
                timeout=30,
            )
            if _has_totp_challenge(email_state):
                totp_secret = str(context.account.get("totp_secret") or "").replace(" ", "").strip()
                if not totp_secret:
                    raise RebindDriverError("当前密码验证要求认证器验证码，但账号没有保存 2FA secret")
                email_state = _submit_current_password_totp(totp_secret)
                if _is_logged_in_page(email_state) and not _has_email_input(email_state):
                    # Auth may finish by returning to the main ChatGPT shell.
                    # Reopen Account settings now that the fresh login is
                    # established instead of trying to type another OTP into
                    # the already-authenticated page.
                    _safe_log(log, "浏览器换绑：TOTP 登录已完成，重新打开账号邮箱变更入口")
                    _open_email_settings_entry()
                    email_state = _wait_browser_state(driver, _has_email_input, timeout=25)
            if _has_email_input(email_state):
                break
            error_text = str(email_state.get("text") or "").lower()
            if password_attempt == 0 and any(marker in error_text for marker in ("timed out", "network error", "operation timed out")):
                try:
                    retried = bool(driver.execute_script(r"""
                    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    const button = [...document.querySelectorAll('button,[role=button],a')].find(el => visible(el)
                      && /retry|try again|again|erneut versuchen|wiederholen|重试|再次/.test(String(el.innerText || el.textContent || '').toLowerCase()));
                    if (!button) return false; button.click(); return true;
                    """))
                except Exception:
                    retried = False
                if retried:
                    password_state = _wait_browser_state(
                        driver,
                        lambda item: any(str(entry.get("type") or "").lower() == "password" for entry in item.get("inputs") or []),
                        timeout=15,
                    )
                    if any(str(entry.get("type") or "").lower() == "password" for entry in password_state.get("inputs") or []):
                        continue
            if any(marker in error_text for marker in ("timed out", "network error", "operation timed out")):
                raise RebindDriverError("当前密码验证页面网络超时，请检查浏览器代理后重试")
            break
    else:
        _safe_log(log, "浏览器换绑步骤 4/8：页面未要求再次输入当前密码")
        email_state = password_state

    if not _has_email_input(email_state):
        email_state = _wait_browser_state(driver, _has_email_input, timeout=20)
    if not _has_email_input(email_state):
        _log_failure_state("missing-new-email-input", email_state)
        raise RebindDriverError("当前密码验证后未出现新邮箱输入框")
    _safe_log(log, "浏览器换绑步骤 6/8：新邮箱输入框已就绪")
    try:
        _type_email_address(driver, target_email, timeout=20)
    except Exception as exc:
        _log_failure_state("fill-new-email-failed")
        raise RebindDriverError(f"新邮箱填写失败：{type(exc).__name__}") from exc
    _safe_log(log, "浏览器换绑步骤 6/8：新邮箱已填写，等待提交控件可用")

    otp_after_ts = time.time()
    _clear_rebind_resource_timings(driver)
    submitted = _submit_browser_email_form(driver)
    if not submitted.get("ok"):
        _safe_log(
            log,
            "浏览器换绑提交诊断："
            + json.dumps({
                "reason": submitted.get("reason"),
                "hasForm": submitted.get("hasForm"),
                "candidates": submitted.get("candidates") or [],
            }, ensure_ascii=False, separators=(",", ":"))[:1400],
        )
        _log_failure_state("missing-new-email-submit")
        raise RebindDriverError(f"新邮箱提交按钮未找到：{submitted.get('reason') or 'unknown'}")
    _safe_log(
        log,
        f"浏览器换绑步骤 7/8：已提交新邮箱（方式={submitted.get('strategy') or 'button'}"
        f"，元素={json.dumps(submitted.get('element') or {}, ensure_ascii=False, separators=(',', ':'))[:240]}），等待页面推进",
    )

    otp_state = _wait_browser_state(
        driver,
        lambda item: any(
            re.search(r"one.?time|otp|verification|numeric|code", str(entry.get("attrs") or ""), re.I)
            for entry in item.get("inputs") or []
        ) or "verification code" in str(item.get("text") or "").lower()
        or "验证码" in str(item.get("text") or "")
        or not _has_email_input(item),
        timeout=20,
    )
    has_otp = any(
        re.search(r"one.?time|otp|verification|numeric|code", str(entry.get("attrs") or ""), re.I)
        for entry in otp_state.get("inputs") or []
    )
    resources = _rebind_resource_diagnostic(driver)
    if resources:
        _safe_log(
            log,
            "浏览器换绑网络步骤："
            + json.dumps(resources[-12:], ensure_ascii=False, separators=(",", ":"))[:1800],
        )
    if _has_email_input(otp_state) and not has_otp:
        _log_failure_state("new-email-submit-stalled", otp_state)
        raise RebindDriverError("新邮箱提交后页面未进入验证码或完成状态")
    if has_otp:
        _safe_log(log, "浏览器换绑步骤 8/8：检测到目标邮箱验证码页面，开始取码")
        used: set[str] = set()
        outcome = "stalled"
        for attempt in range(2):
            code = otp_getter(email=target_email, after_ts=otp_after_ts, exclude_codes=used, target=context.target)
            used.add(code)
            _safe_log(log, f"浏览器换绑步骤 8/8：已取得目标邮箱验证码，提交第 {attempt + 1}/2 次")
            _clear_otp_inputs(driver)
            _type_otp(driver, code, timeout=20)
            try:
                _click_continue(driver)
            except Exception:
                pass
            outcome = _wait_after_email_otp_submit(driver, timeout=15)
            if outcome == "accepted":
                break
            if attempt == 0:
                try:
                    _click_resend_email_otp(driver, timeout=20)
                except Exception as exc:
                    raise RebindDriverError("邮箱验证码校验失败且无法重新发送") from exc
                otp_after_ts = time.time()
        if outcome != "accepted":
            _log_failure_state("target-email-otp-failed")
            raise RebindDriverError("邮箱验证码校验失败")
        _safe_log(log, "浏览器换绑步骤 8/8：目标邮箱验证码已通过")
    else:
        _safe_log(log, "浏览器换绑步骤 8/8：页面未要求目标邮箱验证码，进入远端验证")
    return {"ok": True, "browser_ui": True, "submitted_email": target_email}


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
    headers: dict[str, Any] = {}
    header_factory = getattr(session, "get_chatgpt_headers", None)
    if callable(header_factory) and str(urlparse(target_url).hostname or "").lower() in {
        "chatgpt.com",
        "www.chatgpt.com",
    }:
        try:
            generated = header_factory(referer="https://chatgpt.com/")
            if isinstance(generated, Mapping):
                headers.update(dict(generated))
        except Exception:
            logger.debug("协议换绑生成 ChatGPT 请求头失败", exc_info=True)
    if isinstance(spec.get("headers"), Mapping):
        headers.update(dict(spec.get("headers") or {}))
    headers.setdefault("accept", "application/json")
    headers.setdefault("content-type", "application/json")
    headers.setdefault("origin", "https://chatgpt.com")
    headers.setdefault("referer", "https://chatgpt.com/")
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


def _builtin_chatgpt_protocol_action(
    context: RebindContext,
    *,
    otp_getter: Callable[..., str],
    log: Callable[[str], None] | None,
) -> dict:
    """Use ChatGPT's account email endpoints without opening Settings DOM."""
    spec = {
        "base_url": "https://chatgpt.com",
        "otp_attempts": _DEFAULT_OTP_ATTEMPTS,
    }
    if context.hybrid and context.driver is not None:
        # Keep the authenticated browser's network stack/proxy, but use direct
        # HTTP endpoints instead of loading Settings.  The pre-change session
        # is read only as an in-memory bearer credential and is never persisted.
        from core.account_export import _browser_device_id, _browser_session_info

        source_email = _email(context.account.get("email"), "原账号邮箱")

        def refresh_browser_headers() -> None:
            auth_info = _browser_session_info(context.driver)
            observed_email = _extract_email(auth_info)
            if not observed_email or observed_email.casefold() != source_email.casefold():
                raise RebindDriverError("浏览器协议换绑登录态与原账号不一致")
            access_token = _extract_token(auth_info)
            if not access_token:
                raise RebindDriverError("浏览器协议换绑未取得临时授权")
            spec["headers"] = {
                "authorization": f"Bearer {access_token}",
                "oai-device-id": _browser_device_id(context.driver),
                "oai-language": str(
                    context.driver.execute_script("return navigator.language || 'en-US';") or "en-US"
                ),
            }

        refresh_browser_headers()
        request = _browser_request
        transport = context.driver
        _safe_log(log, "协议换绑：复用已登录浏览器网络栈，不打开 Settings DOM")
    else:
        if context.session is None:
            raise RebindDriverError("协议换绑缺少登录态")
        request = _protocol_request
        transport = context.session
    target_email = _email(context.target.get("email"), "目标邮箱")
    _safe_log(log, "协议换绑步骤 1/4：检查当前账号邮箱换绑资格")
    eligibility_response = request(
        transport,
        spec,
        method="GET",
        url="/backend-api/accounts/change_email/eligibility",
    )
    eligibility = eligibility_response.get("data") if isinstance(eligibility_response.get("data"), Mapping) else {}
    if not _as_bool(eligibility.get("eligible"), False):
        raise RebindDriverError("当前账号未通过邮箱换绑资格检查")
    eligibility_type = str(eligibility.get("eligibility_type") or "").strip().lower()
    social_user = eligibility_type in {"social", "social_password"}

    payload: dict[str, Any] = {"email": target_email}
    if social_user:
        payload["remove_social_subs"] = True
    otp_after_ts = time.time()
    _safe_log(log, "协议换绑步骤 2/4：提交目标邮箱并发送验证码")
    try:
        begin = request(
            transport,
            spec,
            method="POST",
            url="/backend-api/accounts/change_email/begin",
            payload=payload,
        )
    except RebindHttpError as exc:
        if exc.status != 401 or not (context.hybrid and context.driver is not None):
            raise
        # The email endpoint requires pwd_auth_time within five minutes of the
        # change request.  A long-running task can therefore outlive an
        # otherwise valid login; repeat the existing full login flow once and
        # immediately retry the protocol request.
        from core.browser_liveness import _browser_login

        _safe_log(log, "协议换绑：近期密码授权已过期，复用完整登录流程重新验证一次")
        _browser_login(
            context.driver,
            context.account,
            source_email,
            headless=False,
            restore_saved_session=False,
            progress=lambda message: _safe_log(log, f"混合重认证 {message}"),
            require_session=False,
        )
        refresh_browser_headers()
        begin = request(
            transport,
            spec,
            method="POST",
            url="/backend-api/accounts/change_email/begin",
            payload=payload,
        )
    _safe_log(log, f"协议换绑步骤 2/4：目标邮箱已提交 HTTP {begin.get('status', 0)}")

    _safe_log(log, "协议换绑步骤 3/4：等待目标邮箱验证码")
    verify_payload = dict(payload)
    verify_payload["code"] = "{otp}"
    verified = _request_with_otp_retry(
        request,
        transport,
        spec,
        method="POST",
        url="/backend-api/accounts/change_email/verify",
        payload=verify_payload,
        variables={"target_email": target_email, "new_email": target_email, "email": target_email},
        requires_otp=True,
        target=context.target,
        otp_getter=otp_getter,
        log=log,
        after_ts=otp_after_ts,
    )
    _safe_log(log, f"协议换绑步骤 3/4：目标邮箱验证码已通过 HTTP {verified.get('status', 0)}")
    _safe_log(log, "协议换绑步骤 4/4：邮箱变更已提交，刷新最终 Session")
    data = verified.get("data") if isinstance(verified.get("data"), Mapping) else {}
    return {
        "ok": True,
        **dict(data or {}),
        "responses": [eligibility_response, begin, verified],
        "protocol_builtin": True,
    }


def _browser_request(driver: Any, spec: Mapping[str, Any], *, method: str, url: str, payload: Any = None) -> dict:
    from core.account_export import _browser_fetch

    target_url = _resolve_rebind_url(spec, url)
    headers = dict(spec.get("headers") or {}) if isinstance(spec.get("headers"), Mapping) else {}
    headers.setdefault("accept", "application/json")
    headers.setdefault("content-type", "application/json")
    request_method = str(method or "GET").upper()
    if request_method in {"GET", "HEAD"}:
        if payload:
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
                method=request_method,
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
                method=request_method,
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
            _safe_log(log, f"协议换绑验证码步骤：等待目标邮箱验证码（第 {attempt + 1}/{attempts} 次）")
            last_code = otp_getter(
                email=str(target.get("email") or ""),
                after_ts=started,
                exclude_codes=excluded,
                target=dict(target),
            )
            rendered.update({"otp": last_code, "code": last_code})
            _safe_log(log, f"协议换绑验证码步骤：已取得验证码，准备第 {attempt + 1}/{attempts} 次提交")
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
        if action_driver == "protocol":
            _safe_log(log, "协议换绑：使用内置账号邮箱接口，不等待 Settings DOM")
            return _builtin_chatgpt_protocol_action(context, otp_getter=otp_getter, log=log)
        # Browser drivers retain the account-settings path when explicitly
        # selected as the action driver.
        if context.driver is not None:
            _safe_log(log, "浏览器换绑：使用账号设置页面执行邮箱变更")
            return _browser_ui_action(context, otp_getter=otp_getter, log=log)
        raise RebindDriverError("未配置邮箱变更端点或提交钩子")
    request = _protocol_request if action_driver == "protocol" else _browser_request
    transport = context.session if action_driver == "protocol" else context.driver
    if transport is None:
        raise RebindDriverError("换绑提交阶段缺少登录传输")
    _safe_log(log, f"{action_driver} 换绑步骤 1：已加载邮箱变更端点配置")

    variables = {
        "target_email": context.target.get("email"),
        "new_email": context.target.get("email"),
        "email": context.target.get("email"),
    }
    steps = spec.get("steps")
    responses: list[dict] = []
    if isinstance(steps, list) and steps:
        valid_steps = [raw for raw in steps if isinstance(raw, Mapping)]
        _safe_log(log, f"{action_driver} 换绑步骤 2：开始执行 {len(valid_steps)} 个协议请求步骤")
        for index, raw_step in enumerate(valid_steps, start=1):
            if not isinstance(raw_step, Mapping):
                continue
            step = dict(raw_step)
            requires_otp = _as_bool(step.get("requires_otp") or step.get("otp"), False)
            _safe_log(
                log,
                f"{action_driver} 换绑协议步骤 {index}/{len(valid_steps)}："
                f"提交邮箱变更请求（需要验证码={requires_otp}）",
            )
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
            _safe_log(
                log,
                f"{action_driver} 换绑协议步骤 {index}/{len(valid_steps)}："
                f"请求完成 HTTP {response.get('status', 0)}",
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
    _safe_log(log, f"{action_driver} 换绑步骤 2：提交目标邮箱变更请求")
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
    _safe_log(log, f"{action_driver} 换绑步骤 2：邮箱变更请求完成 HTTP {first.get('status', 0)}")
    data = first.get("data") if isinstance(first.get("data"), Mapping) else {}
    verify_url = spec.get("verify_url") or (data or {}).get("verify_url") or (data or {}).get("verification_url")
    otp_required = _as_bool(spec.get("otp_required"), bool(verify_url or (data or {}).get("otp_required") or (data or {}).get("verification_required")))
    if otp_required:
        if not verify_url:
            raise RebindDriverError("变更响应要求 OTP，但未提供验证端点")
        code_field = str(spec.get("code_field") or "code")
        verify_payload = spec.get("verify_payload") or {email_field: context.target.get("email"), code_field: "{otp}"}
        _safe_log(log, f"{action_driver} 换绑步骤 3：服务端要求目标邮箱验证码，开始验证")
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
        _safe_log(log, f"{action_driver} 换绑步骤 3：目标邮箱验证码请求完成 HTTP {second.get('status', 0)}")
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
    _safe_log(log, "远端验证步骤 1/2：刷新账号 Session，核对当前邮箱")
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
        if context.session is not None:
            verified = _fetch_protocol_session(context.session)
        elif context.driver is not None:
            from core.roxy_registration import _fetch_chatgpt_session

            try:
                verified = _fetch_chatgpt_session(
                    context.driver,
                    timeout=90,
                    auto_jump_wait=2,
                )
            except Exception as exc:
                raise RebindVerificationError(f"混合换绑最终 Session 验证失败：{type(exc).__name__}") from exc
        else:
            raise RebindVerificationError("协议验证缺少登录态")
        context.session_info = dict(verified)
    else:
        if context.driver is None:
            raise RebindVerificationError("浏览器验证缺少登录态")
        from core.roxy_registration import _fetch_chatgpt_session

        try:
            verified = _fetch_chatgpt_session(
                context.driver,
                timeout=90,
                auto_jump_wait=5,
            )
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
    _safe_log(log, "远端验证步骤 2/2：目标邮箱与新登录态已确认")
    return result


def _refresh_session_after_builtin_rebind(
    context: RebindContext,
    *,
    target_email: str,
    otp_getter: Callable[..., str],
    hooks: dict,
    headless: bool,
    log: Callable[[str], None] | None,
) -> None:
    """Sign in with the new email after change_email/verify revokes sessions."""
    updated_account = dict(context.account)
    updated_account["email"] = target_email
    _safe_log(log, "换绑后登录：邮箱已验证，旧会话已撤销，使用新邮箱获取最终 Session")
    if context.driver is not None:
        from core.browser_liveness import _browser_login

        info = _browser_login(
            context.driver,
            updated_account,
            target_email,
            headless=bool(headless),
            restore_saved_session=False,
            progress=lambda message: _safe_log(log, f"换绑后登录 {message}"),
            require_session=True,
        )
        context.session_info = dict(info or {})
        _safe_log(log, "换绑后登录：已通过新邮箱、密码和 2FA 获取最终浏览器 Session")
        return
    if context.session is None:
        raise RebindVerificationError("换绑后重新登录缺少协议传输")
    old_session = context.session
    session, info = _protocol_login_builtin(
        updated_account,
        proxy=context.proxy,
        otp_getter=otp_getter,
        log=lambda message: _safe_log(log, f"换绑后登录 {message}"),
        hooks=hooks,
    )
    context.session = session
    context.session_info = dict(info or {})
    context.add_closer(lambda session=session: _close_resource(session))
    _close_resource(old_session)
    _safe_log(log, "换绑后登录：已通过新邮箱、密码和 2FA 获取最终协议 Session")


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
    effective_proxy = _resolve_rebind_proxy(account, proxy)
    _safe_log(
        log,
        "换绑网络出口："
        f"使用{'指定代理' if str(proxy or '').strip() else '代理池出口'}",
    )
    context = RebindContext(
        account=account,
        target=target,
        login_driver=login_name,
        action_driver=action_name,
        hybrid=mixed,
        proxy=effective_proxy,
    )
    # The task service passes proxy separately; browser action transport uses it
    # when it has to open a second browser after protocol login.
    account.setdefault("rebind_proxy", effective_proxy)
    completed = False
    try:
        _safe_log(log, f"登录阶段开始：driver={login_name}，强制建立全新登录态")
        login_hook = hook_map.get("login_protocol" if login_name == "protocol" else "login_browser")
        if login_hook:
            raw_login = _invoke(
                login_hook,
                account=account,
                target=target,
                target_email=target_email,
                context=context,
                proxy=effective_proxy,
                driver=login_name,
                driver_name=login_name,
                headless=selected_login_headless,
                log=log,
                get_otp=otp_getter,
            )
            _normalize_hook_result(context, raw_login)
            _require_stage_success(raw_login, "登录")
        elif login_name == "protocol":
            session, info = _protocol_login_builtin(account, proxy=effective_proxy, otp_getter=otp_getter, log=log, hooks=hook_map)
            context.session = session
            context.session_info = info
            actual_proxy = str(getattr(session, "proxy", "") or "").strip()
            if actual_proxy and actual_proxy != str(effective_proxy or "").strip():
                effective_proxy = actual_proxy
                context.proxy = actual_proxy
                account["rebind_proxy"] = actual_proxy
                _safe_log(log, "协议登录：已切换到可用网络出口，提交阶段复用该 Session")
            context.add_closer(lambda session=session: _close_resource(session))
        else:
            try:
                browser, closer, info = _browser_login_builtin(
                    account,
                    driver_name=login_name,
                    proxy=effective_proxy,
                    headless=selected_login_headless,
                    hooks=hook_map,
                    log=log,
                )
            except RebindDriverError as first_exc:
                if not effective_proxy or not _is_browser_network_error(first_exc):
                    raise
                browser = closer = info = None
                fallback_errors: list[str] = []
                for fallback_proxy in _rebind_proxy_fallbacks(effective_proxy):
                    _safe_log(log, f"{login_name} 登录出口连接异常，轮换代理池出口（模式=proxy_pool）")
                    try:
                        browser, closer, info = _browser_login_builtin(
                            account,
                            driver_name=login_name,
                            proxy=fallback_proxy,
                            headless=selected_login_headless,
                            hooks=hook_map,
                            log=log,
                        )
                        effective_proxy = fallback_proxy
                        context.proxy = fallback_proxy
                        account["rebind_proxy"] = fallback_proxy
                        break
                    except RebindDriverError as fallback_exc:
                        fallback_errors.append(_browser_error_text(fallback_exc))
                if (
                    browser is None
                    or not isinstance(info, Mapping)
                    or not _as_bool(info.get("loginConfirmed"), False)
                ):
                    detail = "; ".join(item for item in fallback_errors if item)[:500]
                    raise RebindDriverError(
                        f"{login_name} 登录网络出口均失败：{detail or _browser_error_text(first_exc)}"
                    ) from first_exc
            context.driver = browser
            context.driver_kind = login_name
            context.add_closer(closer or (lambda browser=browser: _close_resource(browser)))
            context.session_info = info

        # Never let a hook silently switch the account context.  Built-in
        # logins already return the identity; injected hooks must expose the
        # same email through ``session_info``/their result before submission.
        _validate_login_identity(
            context,
            source_email,
            require_token=login_name == "protocol",
        )
        _safe_log(
            log,
            "登录阶段完成：原账号身份和登录页面已确认；换绑成功后再刷新并保存 Session",
        )

        submit_hook = hook_map.get("submit_protocol" if action_name == "protocol" else "submit_browser")
        _safe_log(log, f"换绑提交阶段开始：action_driver={action_name}")
        # A built-in submitter needs a concrete transport.  A site-specific
        # hook may own its transport (for example, a remote automation service
        # that returns only a verified session), so do not force a local
        # browser/protocol conversion before invoking that hook.
        if not submit_hook:
            if action_name == "protocol":
                # The built-in mixed path sends protocol HTTP requests through
                # the already-authenticated browser network stack.  A custom
                # endpoint contract, or pure-protocol login, keeps using the
                # standalone protocol transport.
                if _api_spec(account, target) or context.driver is None:
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
                proxy=effective_proxy,
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

        if (
            not hook_map.get("verify")
            and (
                _as_bool(context.action_result.get("protocol_builtin"), False)
                or _as_bool(context.action_result.get("browser_ui"), False)
            )
        ):
            _refresh_session_after_builtin_rebind(
                context,
                target_email=target_email,
                otp_getter=otp_getter,
                hooks=hook_map,
                headless=selected_headless,
                log=log,
            )

        _safe_log(log, "换绑提交阶段完成：开始远端结果验证")
        result = _verify_remote(context, hooks=hook_map, target_email=target_email, log=log)
        completed = True
        return result
    finally:
        if completed:
            _safe_log(log, "资源清理：换绑流程已结束，关闭本次协议会话和指纹浏览器环境")
        else:
            _safe_log(log, "资源清理：换绑失败，强制关闭并删除本次临时指纹浏览器环境")
        context.close(failed=not completed)


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
