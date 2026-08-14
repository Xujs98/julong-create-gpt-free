# -*- coding: utf-8 -*-
"""已注册账号查活：按 Session、账号密码/TOTP、邮箱 OTP 顺序刷新 ChatGPT accessToken。"""
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import pyotp

from core.session import BrowserSession
from core.chatgpt_auth import get_providers, get_csrf_token, signin_openai
from core.openai_auth import (
    follow_authorize,
    send_email_otp,
    validate_email_otp,
    EmailOtpInvalidError,
    AccountUnusableError,
    detect_account_unusable_text,
    continue_authorize_with_email,
    extract_totp_factor,
    issue_mfa_challenge,
    verify_login_password,
    verify_mfa_code,
)
from core.account_export import follow_oauth_callback, fetch_session
from core.chatgpt_plan import check_account_plan
from core.email_provider import wait_for_otp
from core.proxy_utils import masked_proxy_url, rotate_proxy_session
from core.session_state import (
    build_saved_session,
    capture_http_cookies,
    extract_saved_session,
)

logger = logging.getLogger(__name__)
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"
_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()

# 查活网络预检失败（403/429/代理/超时等）多为出口 IP 被 CF 标记或代理池抖动，
# 视为可换新 IP 重试；账号本身问题（废号/邮箱错误等）不重试。
_RETRYABLE_NETWORK_HINTS = (
    "403", "429", "502", "503", "504",
    "proxy", "socks", "timeout", "timed out",
    "connection", "closed", "reset",
)


def _is_retryable_network_error(exc: BaseException) -> bool:
    if isinstance(exc, AccountUnusableError):
        return False
    text = str(exc or "").lower()
    return any(h in text for h in _RETRYABLE_NETWORK_HINTS)


def _network_preflight_with_retry(
    email: str,
    proxy: str | None,
    max_attempts: int = 4,
    *,
    rotate_proxy_on_retry: bool = True,
) -> tuple[BrowserSession, str]:
    """Providers → CSRF → Signin 网络预检；可选择固定已保存代理环境。"""
    session: BrowserSession | None = None
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        if session is not None:
            try:
                session.session.close()
            except Exception:
                pass
        attempt_proxy = rotate_proxy_session(proxy) if proxy and rotate_proxy_on_retry else proxy
        from core.fingerprint_profile import session_fingerprint_kwargs
        session = BrowserSession(proxy=attempt_proxy, **session_fingerprint_kwargs(email))
        logger.info(
            "[查活] 会话创建完成：proxy=%s device_id=%s（网络预检第 %s/%s 次）",
            masked_proxy_url(session.proxy) or "配置随机/直连", session.device_id, attempt, max_attempts,
        )
        try:
            get_providers(session)
            csrf = get_csrf_token(session)
            authorize_url = signin_openai(session, csrf, email)
            return session, authorize_url
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not _is_retryable_network_error(exc):
                raise
            logger.warning(
                "[查活] 网络预检失败（%s/%s），换新 IP 重试：%s",
                attempt, max_attempts, str(exc)[:200],
            )
            time.sleep(2)
    raise RuntimeError(f"网络预检多次失败：{last_exc}")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"live-check-{safe}.log"


def is_checking(email: str) -> bool:
    key = str(email or "").strip().lower()
    with _RUNNING_LOCK:
        return key in _RUNNING


def _close_http_session(session: BrowserSession | None) -> None:
    """关闭协议会话，避免批量查活时连接池持续占用工作线程资源。"""
    if session is None:
        return
    try:
        session.session.close()
    except Exception:
        pass


def _session_live_result(
    session_info: dict,
    *,
    checked_at: str,
    cookies: list[dict] | None,
    device_id: str | None,
    proxy_used: str | None,
    check_method: str,
) -> dict:
    """把刷新后的 Session 统一整理成数据库查活结果。"""
    access_token = str((session_info or {}).get("accessToken") or "").strip()
    if not access_token:
        raise RuntimeError("刷新登录态后未拿到 accessToken")
    return {
        "ok": True,
        "status": "live",
        "checked_at": checked_at,
        "access_token": access_token,
        "session": build_saved_session(session_info, cookies),
        "device_id": device_id,
        "proxy_used": proxy_used,
        "check_method": check_method,
    }


def _restore_saved_session(account: dict, email: str, proxy: str | None, checked_at: str) -> dict | None:
    """先用已保存 cookies 请求 /api/auth/session；成功时无需打开浏览器或重新登录。"""
    saved = extract_saved_session(account)
    cookies = list((saved or {}).get("cookies") or []) if saved else []
    if not cookies:
        logger.info("[查活] 未保存可复用的 ChatGPT Session Cookie")
        return None

    session: BrowserSession | None = None
    try:
        logger.info("[查活] 尝试用已保存 Session Cookie 静默刷新 AT：cookies=%s", len(cookies))
        from core.fingerprint_profile import session_fingerprint_kwargs
        session = BrowserSession(proxy=proxy, **session_fingerprint_kwargs(email))
        restored = 0
        for item in cookies:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            value = str(item.get("value") or "")
            domain = str(item.get("domain") or ".chatgpt.com")
            path = str(item.get("path") or "/") or "/"
            session.session.cookies.set(
                name,
                value,
                domain=domain,
                path=path,
                secure=bool(item.get("secure")),
            )
            if name == "oai-did" and value:
                session.device_id = value
            restored += 1
        if not restored:
            return None
        session_info = fetch_session(session)
        refreshed_cookies = capture_http_cookies(session)
        access_token = str(session_info.get("accessToken") or "").strip()
        # /api/auth/session 有时仍会回传已被后端撤销的旧 AT；必须再请求一次
        # accounts/check，只有后端接受该 AT 才把 Session Cookie 判定为刷新成功。
        token_check = check_account_plan(
            access_token,
            proxy=session.proxy or "",
            max_attempts=1,
            account_id=str(account.get("account_id") or "") or None,
            device_id=session.device_id,
            session_cookies=refreshed_cookies,
            session=session,
        )
        if token_check.get("needs_live_check") or token_check.get("http_status") == 401:
            logger.info("[查活] 保存 Session 返回的 AT 仍被套餐接口判定失效，继续协议账密登录刷新")
            return None
        if token_check.get("ok") and token_check.get("current_plan_type"):
            session_info.setdefault("account", {})["planType"] = token_check.get("current_plan_type")
        logger.info("[查活] 已保存 Session Cookie 有效，且最新 AT 已通过后端在线校验")
        return _session_live_result(
            session_info,
            checked_at=checked_at,
            cookies=refreshed_cookies,
            device_id=session.device_id,
            proxy_used=session.proxy or None,
            check_method="session_cookie",
        )
    except Exception as exc:
        logger.info("[查活] 已保存 Session Cookie 已失效或当前出口不可复用，继续重新登录：%s: %s", type(exc).__name__, str(exc)[:220])
        return None
    finally:
        _close_http_session(session)


def _auth_payload_value(payload: dict, *keys: str) -> str:
    """递归读取认证响应里的 URL/页面字段，兼容 page.payload 等包装层。"""
    queue = [payload] if isinstance(payload, dict) else []
    seen: set[int] = set()
    while queue:
        current = queue.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        for key in keys:
            value = current.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("page", "payload", "session", "oai-client-auth-session", "oai_client_auth_session"):
            value = current.get(key)
            if isinstance(value, dict):
                queue.append(value)
    return ""


def _auth_page_type(payload: dict) -> str:
    """提取认证响应页面类型，统一用于判断密码、MFA 与邮箱验证码分支。"""
    return _auth_payload_value(payload, "type", "page_type").lower().replace("-", "_")


def _is_email_verification_state(url: str = "", payload: dict | None = None) -> bool:
    """判断协议会话是否落入邮箱验证码分支；有密码账号遇到该状态直接报错。"""
    text = f"{url} {_auth_page_type(payload or {})}".lower().replace("-", "_")
    return "email_verification" in text or "email_otp" in text


def _navigate_auth_step(session: BrowserSession, url: str, referer: str) -> str:
    """跟随认证响应给出的同源下一步 URL，用于建立密码页或 OAuth 页面状态。"""
    target = str(url or "").strip()
    if not target:
        return ""
    if target.startswith("/"):
        target = "https://auth.openai.com" + target
    resp = session.get(
        target,
        headers=session.get_auth_navigate_headers(referer=referer),
        allow_redirects=True,
    )
    if getattr(resp, "status_code", 0) >= 400:
        raise RuntimeError(f"协议登录导航失败 status={resp.status_code}: {(resp.text or '')[:240]}")
    return str(getattr(resp, "url", "") or target)


def _refresh_with_password_protocol(
    account: dict,
    email: str,
    proxy: str | None,
    checked_at: str,
    *,
    preflight_attempts: int = 4,
    rotate_proxy_on_retry: bool = True,
) -> dict:
    """仅使用 HTTP 协议提交保存密码和 TOTP，完成 OAuth 回调并刷新 Session/AT。"""
    password = str(account.get("registration_password") or "").strip()
    totp_secret = str(account.get("totp_secret") or "").replace(" ", "").strip()
    if not password:
        raise RuntimeError("账号未保存注册密码")

    session: BrowserSession | None = None
    try:
        logger.info(
            "[查活] 使用纯协议邮箱 + 保存密码%s刷新 AT，全程仅发送协议请求",
            " + TOTP" if totp_secret else "",
        )
        session, authorize_url = _network_preflight_with_retry(
            email,
            proxy,
            max_attempts=max(1, int(preflight_attempts or 1)),
            rotate_proxy_on_retry=rotate_proxy_on_retry,
        )
        final_url = follow_authorize(session, authorize_url, allow_password_page=True)
        if _is_email_verification_state(final_url):
            raise RuntimeError("有密码账号的 authorize 会话落入邮箱验证码步骤，协议状态与保存资料不一致")
        if "/create-account/password" in final_url.lower():
            raise RuntimeError("保存密码账号落入创建密码页，拒绝把查活误判为注册流程")

        # login_hint 未被服务端直接消费时，显式提交一次邮箱推进到 /log-in/password。
        login_payload: dict = {}
        if "/log-in/password" not in final_url.lower():
            login_payload = continue_authorize_with_email(session, email)
            if _is_email_verification_state(payload=login_payload):
                raise RuntimeError("有密码账号提交邮箱后返回邮箱验证码步骤，未使用 OTP 兜底")
            password_url = _auth_payload_value(login_payload, "continue_url", "external_url", "redirect_url", "url")
            if password_url and "password" in password_url.lower():
                final_url = _navigate_auth_step(session, password_url, final_url or "https://auth.openai.com/log-in")
                if "/create-account/password" in final_url.lower():
                    raise RuntimeError("保存密码账号下一跳落入创建密码页，拒绝继续")

        password_result = verify_login_password(session, password)
        if _is_email_verification_state(payload=password_result):
            raise RuntimeError("协议密码校验后返回邮箱验证码步骤，未使用 OTP 兜底")

        auth_result = password_result
        factor = extract_totp_factor(password_result)
        page_type = _auth_page_type(password_result)
        if factor or "mfa" in page_type:
            if not factor:
                raise RuntimeError(f"协议密码校验要求 MFA，但响应中缺少 TOTP 因子: {password_result}")
            if not totp_secret:
                raise RuntimeError("账号启用了 MFA，但数据库未保存 TOTP secret")
            challenge_result = issue_mfa_challenge(session, factor)
            # TOTP 使用当前协议会话即时生成，避免排队阶段提前生成后跨越 30 秒窗口。
            totp_code = pyotp.TOTP(totp_secret).now()
            verify_factor = factor
            mfa_request_id = _auth_payload_value(challenge_result, "mfa_request_id")
            if mfa_request_id:
                verify_factor = dict(factor)
                verify_factor["metadata"] = {
                    **(factor.get("metadata") if isinstance(factor.get("metadata"), dict) else {}),
                    "mfa_request_id": mfa_request_id,
                }
            auth_result = verify_mfa_code(session, verify_factor, totp_code)
            if _is_email_verification_state(payload=auth_result):
                raise RuntimeError("协议 2FA 校验后返回邮箱验证码步骤，未使用 OTP 兜底")

        continue_url = _auth_payload_value(
            auth_result,
            "continue_url",
            "external_url",
            "redirect_url",
            "url",
            "location",
        )
        if not continue_url:
            raise RuntimeError(f"协议密码/2FA 登录成功但未返回 OAuth continue_url: {auth_result}")

        referer = "https://auth.openai.com/mfa-challenge" if factor else "https://auth.openai.com/log-in/password"
        follow_oauth_callback(session, continue_url, referer=referer)
        session_info = fetch_session(session)
        logger.info("[查活] 协议账号密码%s登录成功，已获取服务端签发的最新 AT", "/TOTP" if factor else "")
        return _session_live_result(
            session_info,
            checked_at=checked_at,
            cookies=capture_http_cookies(session),
            device_id=session.device_id,
            proxy_used=session.proxy or None,
            check_method="password_totp" if factor else "password",
        )
    finally:
        _close_http_session(session)


def _validate_with_retry(session: BrowserSession, email: str, otp_after_ts: float, max_otp_attempts: int = 3) -> dict:
    current_otp = None
    last_exc: Exception | None = None
    for attempt in range(1, max_otp_attempts + 1):
        try:
            if current_otp is None:
                logger.info("[查活] 等待登录 OTP：%s（第 %s/%s 次）", email, attempt, max_otp_attempts)
                current_otp = wait_for_otp(email, after_ts=otp_after_ts)
            result = validate_email_otp(session, current_otp, sentinel_header=None, so_header=None)
            return result
        except EmailOtpInvalidError as exc:
            last_exc = exc
            if attempt >= max_otp_attempts:
                break
            logger.warning("[查活] OTP 无效/过期，重新发送后再取：%s", str(exc)[:180])
            send_email_otp(session)
            # 以“重新发送请求完成后”为新基准，避免刚刚失败的上一封旧码再次被 after 容忍窗口命中。
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
        except Exception as exc:
            # 提交 OTP 后的网络抖动（连接断开/超时/代理波动）：同一会话重发验证码再验证一次。
            if attempt >= max_otp_attempts or not _is_retryable_network_error(exc):
                raise
            last_exc = exc
            logger.warning("[查活] OTP 验证网络抖动，重新发送后再取（%s/%s）：%s", attempt, max_otp_attempts, str(exc)[:180])
            try:
                send_email_otp(session)
            except Exception:
                raise
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
    raise last_exc if last_exc else RuntimeError("OTP 验证失败")


def check_account_liveness(
    email: str,
    proxy: str | None = None,
    *,
    clear_log: bool = True,
    account: dict | None = None,
    driver: str = "protocol",
    headless: bool = False,
    preflight_attempts: int = 4,
    rotate_proxy_on_retry: bool = True,
) -> dict:
    """
    重新登录账号并刷新最新 accessToken。

    返回：
      {
        ok: bool,
        status: live/deactivated/failed,
        access_token: str?,
        session: dict?,
        checked_at: ISO,
        error: str?
      }
    """
    email = str(email or "").strip()
    if not email:
        raise ValueError("email 不能为空")

    checked_at = _now()
    if account is None:
        from core import db
        account = db.get_account_by_email(email) or {}
    key = email.lower()
    path = log_path(email)
    path.parent.mkdir(parents=True, exist_ok=True)
    if clear_log:
        path.write_text("", encoding="utf-8")

    fh: logging.FileHandler | None = None
    root_logger = logging.getLogger()
    thread_name = threading.current_thread().name
    with _RUNNING_LOCK:
        _RUNNING.add(key)
    try:
        fh = logging.FileHandler(str(path), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        fh.addFilter(lambda record: record.threadName == thread_name)
        root_logger.addHandler(fh)

        logger.info("[查活] 日志文件：%s", path)
        logger.info("[查活] 开始刷新登录态：%s", email)
        selected_driver = str(driver or "protocol").strip().lower()
        logger.info("[查活] 本次查活方式：%s%s", selected_driver, f" headless={bool(headless)}" if selected_driver != "protocol" else "")
        if selected_driver != "protocol":
            from core.browser_liveness import check_account_liveness_browser
            return check_account_liveness_browser(
                email,
                account,
                proxy=proxy,
                driver_name=selected_driver,
                headless=bool(headless),
            )

        logger.info("[查活] 协议流程：保存 Session Cookie → 账号密码/TOTP → 旧账号邮箱 OTP 兜底 → Session/AT")

        restored = _restore_saved_session(account, email, proxy, checked_at)
        if restored:
            return restored

        if str(account.get("registration_password") or "").strip():
            return _refresh_with_password_protocol(
                account,
                email,
                proxy,
                checked_at,
                preflight_attempts=preflight_attempts,
                rotate_proxy_on_retry=rotate_proxy_on_retry,
            )

        logger.info("[查活] 账号未保存注册密码，使用旧账号邮箱 OTP 登录兜底")
        logger.info("[查活] OTP 流程：Providers → CSRF → Signin → Authorize → 发送邮箱 OTP → OAuth callback → Session/AT")
        session, authorize_url = _network_preflight_with_retry(
            email,
            proxy,
            max_attempts=max(1, int(preflight_attempts or 1)),
            rotate_proxy_on_retry=rotate_proxy_on_retry,
        )

        otp_after_ts = time.time()
        final_url = follow_authorize(session, authorize_url)
        dead_code = detect_account_unusable_text(final_url)
        if dead_code:
            return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": dead_code}

        # 新版授权页落到 email-verification 后不一定自动发信；显式发送一次，
        # 避免工作线程在尚未触发邮件时空等 90 秒。
        send_email_otp(session)
        otp_after_ts = time.time()
        validate_result = _validate_with_retry(session, email, otp_after_ts)
        page = validate_result.get("page") if isinstance(validate_result, dict) else {}
        page = page if isinstance(page, dict) else {}
        page_type = str(page.get("type") or "")
        continue_url = (
            validate_result.get("continue_url")
            or validate_result.get("external_url")
            or validate_result.get("url")
            or page.get("continue_url")
            or page.get("external_url")
            or page.get("url")
        )
        if not continue_url:
            raise RuntimeError(f"OTP 登录成功但没有 OAuth continue_url: {validate_result}")
        if "about-you" in str(continue_url) or page_type in {"about_you", "about-you"}:
            raise RuntimeError(f"该邮箱登录后进入资料页，疑似不是完整已注册账号: page_type={page_type}, continue_url={continue_url}")

        follow_oauth_callback(session, str(continue_url), referer="https://auth.openai.com/email-verification")
        session_info = fetch_session(session)
        access_token = str(session_info.get("accessToken") or "")
        if not access_token:
            raise RuntimeError("重新登录后未拿到 accessToken")

        user = session_info.get("user") or {}
        account = session_info.get("account") or {}
        logger.info("[查活] 正常：%s user_id=%s plan=%s", email, user.get("id"), account.get("planType"))
        return _session_live_result(
            session_info,
            checked_at=checked_at,
            cookies=capture_http_cookies(session),
            device_id=session.device_id,
            proxy_used=session.proxy or None,
            check_method="email_otp",
        )
    except AccountUnusableError as exc:
        code = getattr(exc, "error_code", "") or detect_account_unusable_text(str(exc)) or "account_deactivated"
        logger.warning("[查活] 已废号：%s %s", email, code)
        return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": code}
    except Exception as exc:
        code = detect_account_unusable_text(str(exc))
        if code:
            logger.warning("[查活] 已废号：%s %s", email, code)
            return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": code}
        logger.warning("[查活] 失败：%s %s: %s", email, type(exc).__name__, str(exc)[:260])
        return {"ok": False, "status": "failed", "checked_at": checked_at, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
    finally:
        try:
            logger.info("[查活] 结束：%s", email)
            _close_http_session(locals().get("session"))
            if fh is not None:
                root_logger.removeHandler(fh)
                fh.close()
        finally:
            with _RUNNING_LOCK:
                _RUNNING.discard(key)
