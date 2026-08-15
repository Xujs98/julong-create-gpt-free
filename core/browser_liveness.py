# -*- coding: utf-8 -*-
"""使用本地指纹浏览器恢复账号登录态并刷新 Session/AT。"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable

import pyotp

from core.email_provider import wait_for_otp
from core.openai_auth import detect_account_unusable_text
from core.session_state import build_saved_session, capture_browser_cookies, extract_saved_session

logger = logging.getLogger(__name__)


def _browser_session_once(driver) -> tuple[int, dict]:
    """在 ChatGPT 页面内读取 Session，返回 HTTP 状态与 JSON。"""
    result = driver.execute_async_script(r"""
    const done = arguments[0];
    fetch('/api/auth/session', {credentials:'include', cache:'no-store'})
      .then(async r => {
        const text = await r.text();
        let data = {};
        try { data = JSON.parse(text); } catch (_) {}
        done({status:r.status, data, body:text.slice(0,500)});
      })
      .catch(e => done({status:0, error:String(e)}));
    """) or {}
    data = result.get("data") if isinstance(result, dict) else {}
    return int(result.get("status") or 0), data if isinstance(data, dict) else {}


def _browser_token_status(driver, access_token: str) -> int:
    """在真实浏览器网络栈中校验 AT，避免协议请求因 CF 403 误判账号。"""
    result = driver.execute_async_script(r"""
    const token = String(arguments[0] || '');
    const done = arguments[arguments.length - 1];
    fetch('/backend-api/accounts/check/v4-2023-04-27?timezone_offset_min=-', {
      method:'GET', credentials:'include',
      headers:{'accept':'application/json','authorization':'Bearer ' + token}
    }).then(async r => done({status:r.status, body:(await r.text()).slice(0,500)}))
      .catch(e => done({status:0, error:String(e)}));
    """, access_token) or {}
    return int(result.get("status") or 0)


def _saved_cookies(account: dict) -> list[dict]:
    """读取账号保存的浏览器 Cookie。"""
    saved = extract_saved_session(account) or {}
    return [dict(item) for item in (saved.get("cookies") or []) if isinstance(item, dict) and item.get("name")]


def _restore_cookies(driver, account: dict) -> int:
    """向已打开的指纹浏览器写入保存 Cookie。"""
    cookies = _saved_cookies(account)
    if not cookies:
        return 0
    driver.delete_all_cookies()
    added = 0
    for cookie in cookies:
        try:
            driver.add_cookie(cookie)
            added += 1
        except Exception as exc:
            logger.debug("[查活][浏览器] Cookie 写入跳过 name=%s err=%s", cookie.get("name"), str(exc)[:160])
    return added


def _clear_browser_auth_state(driver) -> None:
    """彻底清理 ChatGPT/Auth 登录态，避免残留 Session 被误判为新登录成功。"""
    try:
        driver.delete_all_cookies()
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
    except Exception:
        pass
    for origin in ("https://chatgpt.com", "https://auth.openai.com"):
        try:
            driver.execute_cdp_cmd(
                "Storage.clearDataForOrigin",
                {"origin": origin, "storageTypes": "cookies,local_storage,session_storage"},
            )
        except Exception:
            pass
    try:
        driver.execute_script("localStorage.clear(); sessionStorage.clear();")
    except Exception:
        pass


def _session_result(driver, session_info: dict, *, driver_name: str, proxy_used: str | None) -> dict:
    """把浏览器刷新结果整理成数据库查活结构。"""
    access_token = str(session_info.get("accessToken") or "").strip()
    if not access_token:
        raise RuntimeError("指纹浏览器 Session 未返回 accessToken")
    return {
        "ok": True,
        "status": "live",
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "access_token": access_token,
        "session": build_saved_session(session_info, capture_browser_cookies(driver)),
        "device_id": next((str(c.get("value")) for c in capture_browser_cookies(driver) if c.get("name") == "oai-did"), None),
        "proxy_used": proxy_used,
        "check_method": f"{driver_name}_browser",
    }


def _password_login_page_state(driver) -> dict:
    """读取登录密码页的可提交状态与可见错误，供无头模式诊断。"""
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const input = [...document.querySelectorAll('input[type="password"],input[autocomplete="current-password"]')]
          .find(el => visible(el) && !el.disabled && !el.readOnly);
        const form = input?.closest('form') || null;
        const scope = form || document;
        const submit = [...scope.querySelectorAll('button[type="submit"],input[type="submit"]')]
          .find(visible) || null;
        const errors = [...document.querySelectorAll(
          '.react-aria-FieldError,[slot="errorMessage"],[role="alert"],[aria-live="assertive"],'
          + '[id$="-error"],[class*="error" i]'
        )].filter(visible).map(el => (el.innerText || el.textContent || '')
          .replace(/\s+/g, ' ').trim()).filter(Boolean).slice(0, 8);
        const busy = submit ? (
          String(submit.getAttribute('aria-busy') || '').toLowerCase() === 'true'
          || /loading|pending|spinner/.test(String(submit.className || '').toLowerCase())
          || !!submit.querySelector('[role="progressbar"],[class*="spinner" i],[class*="loading" i]')
        ) : false;
        return {
          url: location.href,
          passwordPresent: !!input,
          passwordLength: input ? String(input.value || '').length : 0,
          passwordInvalid: input ? String(input.getAttribute('aria-invalid') || '').toLowerCase() === 'true' : false,
          submitPresent: !!submit,
          submitDisabled: submit ? (!!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true') : false,
          submitLoading: busy,
          errors
        };
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _password_error_message(state: dict) -> str:
    """提取密码页明确展示的错误，避免把原地校验误报成页面超时。"""
    errors = [str(item or "").strip() for item in (state.get("errors") or []) if str(item or "").strip()]
    if not errors:
        return ""
    return "；".join(dict.fromkeys(errors))[:500]


def _fill_login_password(driver, password: str) -> dict:
    """稳定填写已有账号密码，选择同一表单的提交按钮并点击。"""
    marker = f"live-password-{int(time.time() * 1000)}"
    targets = driver.execute_script(r"""
    const marker = String(arguments[0] || '');
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && !el.readOnly;
    const input = [...document.querySelectorAll('input[type="password"],input[autocomplete="current-password"]')].find(visible);
    if (!input) return {ok:false, reason:'missing_password_input', url:location.href};
    const form = input.closest('form');
    const scope = form || document;
    let buttons = [...scope.querySelectorAll('button[type="submit"],input[type="submit"]')].filter(el => {
      return !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
    });
    if (!buttons.length) {
      buttons = [...scope.querySelectorAll('button:not([type]),button[type="button"]')].filter(el => {
        if (!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)) return false;
        const semantic = [el.id, el.name, el.value, el.getAttribute('aria-label'), el.getAttribute('title'), el.textContent]
          .join(' ').toLowerCase();
        return !/show|hide|reveal|forgot|reset|passwordless|one.?time|oauth|google|apple|microsoft/.test(semantic);
      });
    }
    const ir = input.getBoundingClientRect();
    const button = buttons.map((el, idx) => {
      const r = el.getBoundingClientRect();
      return {el, idx, below: r.top >= ir.bottom - 10, distance: Math.max(0, r.top - ir.bottom)};
    }).sort((a, b) => Number(b.below) - Number(a.below) || a.distance - b.distance || a.idx - b.idx)[0]?.el;
    if (!button) return {ok:false, reason:'missing_password_submit', url:location.href};
    input.setAttribute('data-live-password-input', marker);
    button.setAttribute('data-live-password-submit', marker);
    return {
      ok:true,
      inputSelector:`[data-live-password-input="${marker}"]`,
      buttonSelector:`[data-live-password-submit="${marker}"]`,
      url:location.href
    };
    """, marker) or {}
    if not targets.get("ok"):
        raise RuntimeError(f"登录密码页处理失败: {targets}")

    from selenium.webdriver.common.by import By
    inputs = driver.find_elements(By.CSS_SELECTOR, str(targets.get("inputSelector") or ""))
    buttons = driver.find_elements(By.CSS_SELECTOR, str(targets.get("buttonSelector") or ""))
    if len(inputs) != 1 or len(buttons) != 1:
        raise RuntimeError(f"登录密码元素定位异常: inputs={len(inputs)} buttons={len(buttons)}")
    target = inputs[0]
    submit = buttons[0]
    from core.roxy_registration import _human_click, _human_type_text, _set_element_value

    _human_type_text(driver, target, password, clear=True)
    fill_state = driver.execute_script(r"""
    const input = arguments[0];
    input.dispatchEvent(new Event('input', {bubbles:true}));
    input.dispatchEvent(new Event('change', {bubbles:true}));
    input.blur();
    const submit = arguments[1];
    return {
      passwordLength: String(input.value || '').length,
      submitDisabled: !!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
    };
    """, target, submit) or {}
    if int(fill_state.get("passwordLength") or 0) != len(password):
        _set_element_value(driver, target, password)

    enable_end = time.time() + 6
    submit_state = {}
    while time.time() < enable_end:
        submit_state = driver.execute_script(r"""
        const input = arguments[0], submit = arguments[1];
        return {
          passwordLength: String(input.value || '').length,
          disabled: !!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true',
          ariaInvalid: String(input.getAttribute('aria-invalid') || '').toLowerCase() === 'true'
        };
        """, target, submit) or {}
        if int(submit_state.get("passwordLength") or 0) == len(password) and not submit_state.get("disabled"):
            break
        time.sleep(0.2)
    if int(submit_state.get("passwordLength") or 0) != len(password):
        raise RuntimeError(f"登录密码填写校验失败: expected_length={len(password)} state={submit_state}")
    if submit_state.get("disabled"):
        raise RuntimeError(f"登录密码提交按钮持续不可用: state={submit_state}")

    _human_click(driver, submit, label="live_password_submit")
    return {
        "ok": True,
        "marker": marker,
        "url_before": str(targets.get("url") or ""),
        "password_length": len(password),
        "submit_mode": "click",
    }


def _resubmit_login_password_form(driver, marker: str = "") -> dict:
    """首轮点击未触发导航时，通过原生 form.requestSubmit 做一次受控兜底。"""
    return driver.execute_script(r"""
    const marker = String(arguments[0] || '');
    const input = (marker && document.querySelector(`[data-live-password-input="${marker}"]`))
      || [...document.querySelectorAll('input[type="password"],input[autocomplete="current-password"]')]
        .find(el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length));
    if (!input) return {ok:false, reason:'missing_password_input', url:location.href};
    const form = input.closest('form');
    const submit = (marker && document.querySelector(`[data-live-password-submit="${marker}"]`))
      || form?.querySelector('button[type="submit"],input[type="submit"]');
    if (!form) return {ok:false, reason:'missing_form', url:location.href};
    if (submit && (!!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true')) {
      return {ok:false, reason:'submit_disabled', url:location.href};
    }
    if (typeof form.requestSubmit === 'function') form.requestSubmit(submit || undefined);
    else if (submit) submit.click();
    else form.submit();
    return {ok:true, reason:'request_submit', url:location.href};
    """, marker) or {}


def _wait_after_password(driver, timeout: int = 45, *, submission: dict | None = None) -> str:
    """等待密码提交后的登录态、TOTP 或邮箱验证码页面。"""
    from core.roxy_registration import _has_access_token, _is_email_verification_page

    started = time.time()
    end = started + max(5, int(timeout or 45))
    last_url = ""
    last_state: dict = {}
    resubmitted = False
    while time.time() < end:
        if _has_access_token(driver):
            return "logged_in"
        try:
            last_url = str(driver.current_url or "")
        except Exception:
            last_url = ""
        lower = last_url.lower()
        if "mfa" in lower or "challenge" in lower:
            return "totp"
        if _is_email_verification_page(driver):
            return "email_otp"
        last_state = _password_login_page_state(driver)
        error_message = _password_error_message(last_state)
        if error_message:
            raise RuntimeError(f"密码登录页面返回错误：{error_message}")
        if (
            not resubmitted
            and time.time() - started >= 4
            and last_state.get("passwordPresent")
            and int(last_state.get("passwordLength") or 0) > 0
            and last_state.get("submitPresent")
            and not last_state.get("submitDisabled")
            and not last_state.get("submitLoading")
        ):
            retry = _resubmit_login_password_form(driver, str((submission or {}).get("marker") or ""))
            resubmitted = True
            logger.warning("[查活][浏览器] 密码页首轮点击未产生跳转，已执行一次 requestSubmit 兜底：%s", retry)
        time.sleep(0.5)
    diagnostic = {
        "url": str(last_state.get("url") or last_url)[:300],
        "passwordPresent": bool(last_state.get("passwordPresent")),
        "passwordLength": int(last_state.get("passwordLength") or 0),
        "passwordInvalid": bool(last_state.get("passwordInvalid")),
        "submitPresent": bool(last_state.get("submitPresent")),
        "submitDisabled": bool(last_state.get("submitDisabled")),
        "submitLoading": bool(last_state.get("submitLoading")),
        "resubmitted": resubmitted,
    }
    raise RuntimeError(f"提交密码后未进入登录态/MFA/邮箱验证码页，诊断: {diagnostic}")


def _submit_code_and_fetch_session(driver, code: str, *, code_kind: str = "") -> dict:
    """填写 TOTP/邮箱 OTP，提交后读取 ChatGPT Session。"""
    from core.roxy_registration import _click_continue, _fetch_chatgpt_session, _type_otp, _wait_after_email_otp_submit

    _type_otp(driver, code, timeout=20)
    try:
        _click_continue(driver)
    except Exception as exc:
        logger.info("[查活][浏览器] 未找到显式验证码提交按钮，继续等待页面跳转：%s", str(exc)[:160])
    if code_kind == "email_otp":
        outcome = _wait_after_email_otp_submit(driver, timeout=12)
        if outcome != "accepted":
            raise RuntimeError(f"邮箱验证码未通过：{outcome}")
    return _fetch_chatgpt_session(driver, timeout=90, auto_jump_wait=12)


def _login_with_email_otp(driver, email: str, *, after_ts: float, max_attempts: int = 3) -> dict:
    """邮箱 OTP 登录；旧码/过期码失败后重发并等待一个不同的新验证码。"""
    from core.roxy_registration import (
        _clear_otp_inputs,
        _click_continue,
        _click_resend_email_otp,
        _fetch_chatgpt_session,
        _type_otp,
        _wait_after_email_otp_submit,
    )

    used_codes: set[str] = set()
    current_after_ts = float(after_ts or time.time())
    attempts = max(1, min(3, int(max_attempts or 3)))
    last_outcome = ""
    for attempt in range(1, attempts + 1):
        logger.info("[查活][浏览器][OTP] 等待本次登录验证码（%s/%s）", attempt, attempts)
        try:
            code = wait_for_otp(
                email,
                after_ts=current_after_ts,
                exclude_codes=used_codes,
            )
        except Exception as exc:
            if attempt >= attempts:
                raise
            logger.warning(
                "[查活][浏览器][OTP] 未取得新验证码，点击重发后继续：%s: %s",
                type(exc).__name__,
                str(exc)[:180],
            )
            current_after_ts = time.time()
            resend = _click_resend_email_otp(driver, timeout=25)
            if resend.get("reason") == "already_accepted":
                return _fetch_chatgpt_session(driver, timeout=90, auto_jump_wait=12)
            continue

        code = str(code or "").strip()
        _clear_otp_inputs(driver)
        _type_otp(driver, code, timeout=20)
        try:
            _click_continue(driver)
        except Exception as exc:
            logger.info("[查活][浏览器][OTP] 未找到显式提交按钮，继续观察页面：%s", str(exc)[:160])
        last_outcome = _wait_after_email_otp_submit(driver, timeout=12)
        if last_outcome == "accepted":
            logger.info("[查活][浏览器][OTP] 邮箱验证码已通过")
            return _fetch_chatgpt_session(driver, timeout=90, auto_jump_wait=12)

        used_codes.add(code)
        if attempt >= attempts:
            break
        logger.warning(
            "[查活][浏览器][OTP] 当前验证码无效/过期（outcome=%s），已排除该验证码并请求重发（下一次 %s/%s）",
            last_outcome,
            attempt + 1,
            attempts,
        )
        current_after_ts = time.time()
        resend = _click_resend_email_otp(driver, timeout=25)
        if resend.get("reason") == "already_accepted":
            return _fetch_chatgpt_session(driver, timeout=90, auto_jump_wait=12)

    raise RuntimeError(f"邮箱验证码连续未通过，已重试 {attempts} 次：last_outcome={last_outcome or 'unknown'}")


def _browser_login(
    driver,
    account: dict,
    email: str,
    *,
    headless: bool,
    restore_saved_session: bool = True,
    stale_session_retry: bool = False,
) -> dict:
    """在当前指纹浏览器内执行 Session 恢复或账号重新登录。"""
    from core.roxy_registration import (
        _click_passwordless_signup_if_present,
        _fetch_chatgpt_session,
        _safe_get,
        _submit_email_and_wait_next,
        _wait_for_cloudflare_challenge,
    )

    if not restore_saved_session:
        _clear_browser_auth_state(driver)
        logger.info("[查活][浏览器] 已清空旧登录态，跳过保存 Session/Cookie")
    _safe_get(driver, "https://chatgpt.com/", timeout=45, attempts=2, accept_hosts=("chatgpt.com",))
    restored = _restore_cookies(driver, account) if restore_saved_session else 0
    if restored:
        logger.info("[查活][浏览器] 已写入保存 Session Cookie：%s 个", restored)
        _safe_get(driver, "https://chatgpt.com/", timeout=45, attempts=2, accept_hosts=("chatgpt.com",))
        status, session_info = _browser_session_once(driver)
        token = str(session_info.get("accessToken") or "").strip()
        if status == 200 and token:
            token_status = _browser_token_status(driver, token)
            if 200 <= token_status < 300:
                logger.info("[查活][浏览器] 保存 Session 已恢复，AT 浏览器内在线校验通过")
                return session_info
            logger.info("[查活][浏览器] 保存 Session 的 AT 校验状态=%s，继续重新登录", token_status)

    if restore_saved_session:
        _clear_browser_auth_state(driver)
        logger.info("[查活][浏览器] 保存 Session 校验未通过，已清空旧登录态")
    logger.info("[查活][浏览器] 强制重新登录获取新 Session/AT")
    _safe_get(driver, "https://chatgpt.com/auth/login", timeout=45, attempts=2, accept_hosts=("chatgpt.com", "auth.openai.com"))
    _wait_for_cloudflare_challenge(driver, timeout=300, headless=headless)
    from core.roxy_registration import _maybe_accept
    _maybe_accept(driver)
    otp_after_ts = time.time()
    state = _submit_email_and_wait_next(
        driver,
        email,
        attempts=3,
        allow_login_password=True,
        allow_existing_session=False,
    )
    password = str(account.get("registration_password") or "").strip()
    totp_secret = str(account.get("totp_secret") or "").replace(" ", "").strip()

    if state == "logged_in":
        session_info = _fetch_chatgpt_session(driver, timeout=60, auto_jump_wait=8)
        token = str(session_info.get("accessToken") or "").strip()
        token_status = _browser_token_status(driver, token) if token else 0
        if 200 <= token_status < 300:
            logger.info("[查活][浏览器] 当前 Session 的 AT 已通过浏览器内在线校验")
            return session_info
        if not stale_session_retry:
            logger.warning(
                "[查活][浏览器] 登录页检测到的 Session 实为残留失效状态（AT 校验=%s），彻底清理后重新登录一次",
                token_status,
            )
            _clear_browser_auth_state(driver)
            return _browser_login(
                driver,
                account,
                email,
                headless=headless,
                restore_saved_session=False,
                stale_session_retry=True,
            )
        raise RuntimeError(f"彻底清理登录态后仍读取到失效 Session：AT 浏览器内校验状态={token_status}")
    if state == "login_password":
        if password:
            otp_after_ts = time.time()
            logger.info("[查活][浏览器] 使用保存账号密码登录")
            submission = _fill_login_password(driver, password)
            state = _wait_after_password(driver, submission=submission)
        else:
            otp_after_ts = time.time()
            switched = _click_passwordless_signup_if_present(driver)
            if not switched.get("ok"):
                raise RuntimeError("账号没有保存密码，且登录页没有一次性验证码入口")
            logger.info("[查活][浏览器] 未保存密码，已切换邮箱验证码登录")
            state = "email_otp"

    if state == "totp":
        if not totp_secret:
            raise RuntimeError("账号要求 TOTP，但数据库没有保存 2FA secret")
        logger.info("[查活][浏览器] 提交 TOTP 动态验证码")
        return _submit_code_and_fetch_session(driver, pyotp.TOTP(totp_secret).now(), code_kind="totp")
    if state == "otp" or state == "email_otp":
        logger.info("[查活][浏览器] 等待邮箱登录验证码")
        return _login_with_email_otp(driver, email, after_ts=otp_after_ts, max_attempts=3)
    if state == "logged_in":
        return _fetch_chatgpt_session(driver, timeout=60, auto_jump_wait=8)
    if state == "password":
        raise RuntimeError("已注册账号进入创建密码页，登录状态与账号资料不一致")
    raise RuntimeError(f"指纹浏览器登录进入未知状态: {state}")


def _open_cloak(proxy: str | None, headless: bool) -> tuple[Any, str | None, Callable[[], None]]:
    """启动查活专用 CloakBrowser。"""
    from core.cloakbrowser_driver import build_cloak_driver

    driver, _opened = build_cloak_driver(proxy=proxy, headless=headless)
    driver._registration_log_prefix = "[查活][Cloak]"
    return driver, getattr(driver, "upstream_proxy_url", None) or proxy, driver.quit


def _open_roxy(proxy: str | None, headless: bool) -> tuple[Any, str | None, Callable[[], None]]:
    """启动查活专用 RoxyBrowser，并使用本次独立无头参数。"""
    del proxy  # Roxy 环境代理由 Roxy 配置管理，保持与其环境创建逻辑一致。
    from config import roxybrowser as roxy_cfg
    from core.roxy_registration import _build_driver
    from core.roxybrowser_client import RoxyBrowserClient

    client = RoxyBrowserClient()
    opened = client.open_profile(headless=headless)
    driver = _build_driver(opened)
    driver._registration_log_prefix = "[查活][Roxy]"
    try:
        driver.set_page_load_timeout(int(getattr(roxy_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90))
    except Exception:
        pass

    def _close() -> None:
        try:
            driver.quit()
        except Exception:
            pass
        client.close_profile(opened.profile_id)
        if (
            bool(getattr(roxy_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True))
            and bool(getattr(roxy_cfg, "ROXY_DELETE_PROFILE_AFTER_RUN", True))
            and bool(opened.created_by_run)
        ):
            client.delete_profile(opened.profile_id)

    return driver, None, _close


def check_account_liveness_browser(
    email: str,
    account: dict,
    *,
    proxy: str | None,
    driver_name: str,
    headless: bool,
    force_fresh_login: bool = False,
) -> dict:
    """按配置启动指纹浏览器查活，返回与协议查活一致的数据结构。"""
    selected = str(driver_name or "").strip().lower()
    opener = _open_cloak if selected == "cloak" else _open_roxy if selected == "roxy" else None
    if opener is None:
        raise ValueError(f"不支持的浏览器查活方式: {selected}")

    driver = None
    closer: Callable[[], None] | None = None
    try:
        logger.info("[查活] 使用 %s 指纹浏览器，headless=%s", selected, bool(headless))
        driver, proxy_used, closer = opener(proxy, bool(headless))
        if force_fresh_login:
            logger.info("[查活][浏览器] 现有 AT 已明确失效，本次跳过所有保存 Session/Cookie")
        session_info = _browser_login(
            driver,
            account,
            email,
            headless=bool(headless),
            restore_saved_session=not bool(force_fresh_login),
        )
        token = str(session_info.get("accessToken") or "").strip()
        token_status = _browser_token_status(driver, token) if token else 0
        if not 200 <= token_status < 300:
            raise RuntimeError(f"浏览器 Session 已返回 AT，但在线校验未通过：HTTP {token_status or 0}")
        result = _session_result(driver, session_info, driver_name=selected, proxy_used=proxy_used)
        logger.info("[查活][浏览器] 完成：driver=%s 已刷新 Session/AT", selected)
        return result
    except Exception as exc:
        page_text = ""
        try:
            from core.roxy_registration import _page_snapshot
            snapshot = _page_snapshot(driver) if driver is not None else {}
            page_text = f"{snapshot.get('url') or ''} {snapshot.get('text') or ''}"
        except Exception:
            pass
        dead_code = detect_account_unusable_text(f"{exc} {page_text}")
        if dead_code:
            logger.warning("[查活][浏览器] 已废号：%s", dead_code)
            return {"ok": False, "status": "deactivated", "checked_at": datetime.now().isoformat(timespec="seconds"), "error": dead_code}
        logger.warning("[查活][浏览器] 失败：%s: %s", type(exc).__name__, str(exc)[:400])
        return {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            "check_method": f"{selected}_browser",
        }
    finally:
        if closer is not None:
            closer()
