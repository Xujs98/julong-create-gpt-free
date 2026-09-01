# -*- coding: utf-8 -*-
"""通过 CloakBrowser + Playwright 适配层执行 ChatGPT 注册。"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from config import cloakbrowser as _cfg
from config import proxy as _proxy_cfg
from config import twofa as _twofa_cfg
from core.account_export import describe_twofa_error, save_account_data, setup_2fa_from_browser
from core.cloakbrowser_driver import build_cloak_driver
from core.email_provider import wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay
from core.session_state import build_saved_session, capture_browser_cookies
from core.page_agent import PageAgentConfigError, attach_agent

# 复用 Roxy 注册流程里已维护好的页面操作函数。
from core.roxy_registration import (  # noqa: F401
    _maybe_accept, _submit_email_and_wait_next, _fill_password_page_if_present,
    _ensure_password_before_otp,
    _clear_otp_inputs, _type_otp, _click_continue, _wait_after_email_otp_submit,
    _click_resend_email_otp, _complete_profile_page, _fetch_chatgpt_session, _check_manual_stop,
    _wait_for_cloudflare_challenge,
    _wait_for_email_otp_page,
    _require_password_if_enabled, _create_password_enabled,
    _registration_password, _has_access_token, _is_email_verification_page,
    _is_login_password_page, _is_signup_password_page, _page_snapshot, _is_profile_like,
    _cloudflare_challenge_state,
)

logger = logging.getLogger(__name__)

_CLOAK_LOGIN_URL = "https://chatgpt.com/auth/login"
_BROWSER_PROXY_CHALLENGE_MARKER = "BrowserProxyChallenge"


def _reject_initial_browser_proxy_challenge(driver, proxy: str | None) -> None:
    """真实浏览器入口出现挑战时立即把当前出口交给任务层轮换。"""
    actual_proxy = str(getattr(driver, "upstream_proxy_url", None) or proxy or "").strip()
    if not actual_proxy or not bool(getattr(_proxy_cfg, "PROXY_BROWSER_CHALLENGE_AUTO_ROTATE", True)):
        return
    state = _cloudflare_challenge_state(driver)
    if state.get("challenge"):
        raise RuntimeError(
            f"{_BROWSER_PROXY_CHALLENGE_MARKER}: Cloudflare 人机验证："
            "真实指纹浏览器出口触发挑战，立即淘汰并自动换代理"
        )


def _is_transient_cloak_error(error: object) -> bool:
    """识别可通过重建页面/浏览器恢复的瞬态错误。"""
    text = str(error or "").lower()
    if not text:
        return False
    permanent = (
        "用户手动停止",
        "邮箱可能已注册",
        "登录密码页",
        "风控/限流",
        "invalid_code",
        "验证码连续错误",
    )
    if any(marker.lower() in text for marker in permanent):
        return False
    return any(marker in text for marker in (
        "cloudflare 人机验证",
        "page.goto:",
        "net::err_",
        "targetclosederror",
        "target page, context or browser has been closed",
        "chrome-error://",
        "页面加载超时",
        "browser has been closed",
    ))


def _page_ready_after_navigation(driver, target_url: str) -> bool:
    """超时后判断目标页面是否已经可用，避免把 domcontentloaded 慢误判成失败。"""
    try:
        state = driver.execute_script(
            "return {url: String(location.href || ''), readyState: String(document.readyState || ''), body: !!document.body};"
        ) or {}
    except Exception:
        return False
    current = str(state.get("url") or "").lower()
    target_host = str(target_url or "").split("/", 3)[2].lower() if "/" in str(target_url or "") else ""
    return bool(
        state.get("body")
        and target_host
        and target_host in current
        and not current.startswith("chrome-error://")
    )


def _safe_cloak_get(driver, url: str, *, attempts: int | None = None) -> None:
    """Cloak 初始导航的有限重试与超时后 DOM 复用。"""
    try:
        max_attempts = int(attempts if attempts is not None else getattr(_cfg, "CLOAK_NAVIGATION_RETRIES", 3) or 3)
    except (TypeError, ValueError):
        max_attempts = 3
    max_attempts = max(1, min(max_attempts, 5))
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        _check_manual_stop()
        try:
            driver.get(url)
            return
        except Exception as exc:  # Playwright/Cloak 异常类型不固定
            last_exc = exc
            if _page_ready_after_navigation(driver, url):
                logger.warning(
                    "[Cloak注册] 初始导航等待超时但 DOM 已可用，继续流程：attempt=%s/%s url=%s error=%s",
                    attempt, max_attempts, url, str(exc)[:180],
                )
                return
            transient = _is_transient_cloak_error(exc)
            if not transient or attempt >= max_attempts:
                raise
            logger.warning(
                "[Cloak注册] 初始导航失败，%ss 后重试：attempt=%s/%s error=%s",
                min(4, attempt), attempt, max_attempts, str(exc)[:220],
            )
            time.sleep(min(4.0, 1.2 * attempt))
    raise last_exc or RuntimeError(f"Cloak 初始导航失败: {url}")


def _agent_checkpoint(
    agent,
    driver,
    stage: str,
    context: dict,
    *,
    force: bool = False,
    snapshot: dict | None = None,
    max_actions: int | None = None,
):
    if agent is None:
        return None
    result = agent.assist(
        driver,
        stage,
        context,
        force=force,
        snapshot=snapshot,
        max_actions=max_actions,
    )
    logger.info(
        "[Cloak注册][Agent] stage=%s mode=%s ok=%s executed=%s reason=%s",
        stage, agent.mode, result.ok, result.executed, result.reason,
    )
    return result


def _registration_password_for_agent() -> str:
    return _registration_password()


def _agent_used_value_ref(result, value_ref: str) -> bool:
    """确认某个敏感值确实由 Agent 成功写入，而不只依据返回动作计划。"""
    if result is None:
        return False
    return any(
        str(action.get("type") or "").lower() == "fill"
        and str(action.get("value_ref") or "") == value_ref
        for action in (result.executed_actions or [])
    )


def _agent_page_state(driver, snapshot: dict | None = None) -> tuple[str, dict]:
    """每轮从实际 HTML 判断页面类型，禁止仅凭 URL 或上一步动作推断状态。"""
    if _has_access_token(driver):
        return "logged_in", snapshot or {}
    if _is_email_verification_page(driver):
        snapshot = snapshot or _page_snapshot(driver)
        # OTP URL 可能先显示“使用密码继续”入口；此时先让 Agent 点击入口，
        # 不提前进入邮箱取码阶段。
        button_text = " ".join(
            " ".join(str(item.get(key) or "") for key in ("text", "aria"))
            for item in (snapshot.get("buttons") or [])
        ).lower()
        has_otp_input = any(
            any(word in " ".join(str(item.get(key) or "") for key in ("type", "name", "id", "autocomplete", "inputmode", "aria")).lower() for word in ("one-time", "otp", "verification", "numeric", "code"))
            for item in (snapshot.get("inputs") or [])
        )
        has_password_entry = any(
            word in button_text
            for word in ("use password", "continue with password", "password to continue", "使用密码", "密码继续", "パスワードで続行", "パスワードを使用")
        )
        if has_password_entry and not has_otp_input:
            return "password_entry", snapshot
        return "otp", snapshot
    if _is_signup_password_page(driver):
        return "password", snapshot or {}
    if _is_login_password_page(driver):
        return "login_password", snapshot or {}

    snapshot = snapshot or _page_snapshot(driver)
    if _is_profile_like(snapshot):
        return "profile", snapshot
    inputs = list(snapshot.get("inputs") or [])
    attrs = " ".join(
        " ".join(str(item.get(key) or "") for key in ("type", "name", "id", "autocomplete", "aria", "placeholder"))
        for item in inputs
    ).lower()
    if any(word in attrs for word in ("password", "パスワード", "密码", "密碼")):
        return "password", snapshot
    if any(word in attrs for word in ("one-time", "otp", "verification-code", "inputmode numeric")):
        return "otp", snapshot
    if any(word in attrs for word in ("email", "username")):
        return "email", snapshot
    return "unknown", snapshot


def _run_agent_takeover_until_input(
    agent,
    driver,
    *,
    email: str,
    name: str,
    birthday: str,
    password: str,
    timeout: int = 180,
) -> tuple[str, str | None]:
    """持续执行“读 HTML → 判阶段 → 单动作 → 再读 HTML”，直到真实 OTP 页出现。"""
    birthday_parts = str(birthday or "").split("-")
    context = {
        "email": email,
        "password": password,
        "name": name,
        "birthday": birthday,
        "birth_year": birthday_parts[0] if len(birthday_parts) > 0 else "",
        "birth_month": birthday_parts[1] if len(birthday_parts) > 1 else "",
        "birth_day": birthday_parts[2] if len(birthday_parts) > 2 else "",
    }
    end = time.time() + max(30, int(timeout or 180))
    last_signature = None
    idle_rounds = 0
    password_used = False

    while time.time() < end:
        _check_manual_stop()
        _wait_for_cloudflare_challenge(
            driver,
            timeout=int(getattr(_cfg, "CLOAK_CHALLENGE_TIMEOUT", 300) or 300),
            headless=bool(getattr(_cfg, "CLOAK_HEADLESS", False)),
            agent=agent,
        )
        snapshot = agent.snapshot(driver)
        state, snapshot = _agent_page_state(driver, snapshot)
        logger.info(
            "[Cloak注册][Agent] 读取 HTML 后判定阶段=%s url=%s inputs=%s buttons=%s",
            state, str(snapshot.get("url") or getattr(driver, "current_url", ""))[:180],
            len(snapshot.get("inputs") or []), len(snapshot.get("buttons") or []),
        )
        if state in {"otp", "logged_in", "profile"}:
            return state, password if password_used else None
        if state == "login_password":
            raise RuntimeError(f"Agent 检测到登录密码页，邮箱可能已注册：{email}")

        stage = state if state in {"password_entry", "password"} else "email"
        signature = (
            state,
            str(snapshot.get("url") or ""),
            tuple((item.get("selector"), item.get("valuePresent")) for item in (snapshot.get("inputs") or [])),
            tuple(item.get("selector") for item in (snapshot.get("buttons") or [])),
        )
        result = agent.assist(
            driver,
            stage,
            context,
            force=True,
            snapshot=snapshot,
            max_actions=1,
        )
        if _agent_used_value_ref(result, "password"):
            password_used = True
        if result.executed:
            idle_rounds = 0
            logger.info(
                "[Cloak注册][Agent] 单步已执行 stage=%s action=%s reason=%s",
                stage, result.executed_actions[0], result.reason,
            )
            time.sleep(0.8)
            continue

        idle_rounds = idle_rounds + 1 if signature == last_signature else 1
        last_signature = signature
        logger.info(
            "[Cloak注册][Agent] 当前 HTML 没有可执行动作，%.1fs 后重新读取（idle=%s）",
            min(2.0, 0.5 + idle_rounds * 0.25), idle_rounds,
        )
        time.sleep(min(2.0, 0.5 + idle_rounds * 0.25))

    state, snapshot = _agent_page_state(driver, agent.snapshot(driver))
    raise RuntimeError(
        f"Agent 持续接管超时：最后阶段={state} url={snapshot.get('url') or getattr(driver, 'current_url', '')}"
    )


def _run_agent_takeover_after_otp(
    agent,
    driver,
    *,
    email: str,
    otp: str | None,
    password: str,
    name: str,
    birthday: str,
    timeout: int = 120,
) -> tuple[str, bool, bool]:
    """OTP 到资料页继续逐轮读取 HTML，由 Agent 单步推进直到登录态建立。"""
    birthday_parts = str(birthday or "").split("-")
    context = {
        "email": email,
        "otp": otp,
        "password": password,
        "name": name,
        "birthday": birthday,
        "birth_year": birthday_parts[0] if len(birthday_parts) > 0 else "",
        "birth_month": birthday_parts[1] if len(birthday_parts) > 1 else "",
        "birth_day": birthday_parts[2] if len(birthday_parts) > 2 else "",
    }
    end = time.time() + max(30, int(timeout or 120))
    idle_rounds = 0
    password_used = False
    profile_seen = False
    while time.time() < end:
        _check_manual_stop()
        _wait_for_cloudflare_challenge(
            driver,
            timeout=int(getattr(_cfg, "CLOAK_CHALLENGE_TIMEOUT", 300) or 300),
            headless=bool(getattr(_cfg, "CLOAK_HEADLESS", False)),
            agent=agent,
        )
        snapshot = agent.snapshot(driver)
        state, snapshot = _agent_page_state(driver, snapshot)
        logger.info(
            "[Cloak注册][Agent] 提交后重新读取 HTML：阶段=%s url=%s",
            state, str(snapshot.get("url") or getattr(driver, "current_url", ""))[:180],
        )
        if state == "logged_in":
            return state, password_used, profile_seen
        if state == "login_password":
            raise RuntimeError(f"Agent 接管期间进入登录密码页：{email}")
        if state == "profile":
            profile_seen = True

        stage = state if state in {"otp", "password", "profile"} else "page"
        result = agent.assist(
            driver,
            stage,
            context,
            force=True,
            snapshot=snapshot,
            max_actions=1,
        )
        if _agent_used_value_ref(result, "password"):
            password_used = True
        if result.executed:
            idle_rounds = 0
            logger.info(
                "[Cloak注册][Agent] 单步已执行 stage=%s action=%s reason=%s",
                stage, result.executed_actions[0], result.reason,
            )
            time.sleep(0.8)
            continue
        idle_rounds += 1
        time.sleep(min(2.0, 0.5 + idle_rounds * 0.25))

    state, snapshot = _agent_page_state(driver, agent.snapshot(driver))
    raise RuntimeError(
        f"Agent OTP/资料页接管超时：最后阶段={state} url={snapshot.get('url') or getattr(driver, 'current_url', '')}"
    )


def _run_in_isolated_thread(fn, *args, **kwargs):
    """在没有 asyncio loop 的单独线程中运行完整的 Cloak 同步流程。

    CloakBrowser 0.5.x 的公开 API 固定使用 Playwright Sync API。只把
    ``launch`` 单独挪到另一个线程会让后续 page/locator 调用跨线程，因而
    这里把启动、页面操作、关闭都放在同一线程，并复用父线程名让任务日志
    继续落到当前 job 的日志文件。
    """
    result_box: dict[str, object] = {}
    error_box: dict[str, BaseException] = {}
    parent_thread = threading.current_thread()
    parent_thread_name = parent_thread.name

    # registration_service 的停止检查使用 threading.local；把当前 job id
    # 显式传给子线程，保留 WebUI 的“停止任务”语义。
    job_id = None
    registration_service = None
    try:
        from core import registration_service as registration_service
        job_id = getattr(registration_service._THREAD_CTX, "job_id", None)
    except Exception:
        registration_service = None

    def _target() -> None:
        inherited_job = False
        try:
            if registration_service is not None and job_id is not None:
                registration_service._THREAD_CTX.job_id = job_id
                inherited_job = True
            result_box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - 跨线程回传原始错误
            error_box["error"] = exc
        finally:
            if inherited_job:
                try:
                    del registration_service._THREAD_CTX.job_id
                except Exception:
                    pass

    thread = threading.Thread(target=_target, name=parent_thread_name, daemon=False)
    thread.start()
    thread.join()
    if "error" in error_box:
        raise error_box["error"]
    return result_box.get("value")


def _ensure_password_after_otp(driver, email: str, next_state: str, password: str | None) -> str | None:
    """OTP 优先的 Cloak 流程在验证后再处理创建密码页。"""
    if next_state == "otp" and _create_password_enabled() and not password:
        logger.info("[Cloak注册] 邮箱 OTP 已通过，继续检查创建密码页：%s", email)
        password = _fill_password_page_if_present(driver, email, timeout=20)
    _require_password_if_enabled(password, email, driver_name="CloakBrowser")
    return password


def _run_cloak_registration_impl(
    email: str,
    name: str,
    birthday: str,
    proxy: str = None,
    otp_code: str = None,
    batch_dir: Path | None = None,
    headless_override: bool | None = None,
) -> dict:
    """CloakBrowser 自动化注册入口。"""
    driver = None
    opened = None
    create_acknowledged = False
    keep_browser_on_error = False
    openai_password: str | None = None
    agent = None
    try:
        driver, opened = build_cloak_driver(proxy=proxy, headless=headless_override)
        logger.info("[Cloak注册] 开始：%s，profile=%s", email, opened.profile_id)
        if bool(getattr(_cfg, "CLOAK_ENABLE_AGENT", False)):
            mode = str(getattr(_cfg, "CLOAK_AGENT_MODE", "hybrid") or "hybrid").strip().lower()
            if mode not in {"hybrid", "takeover"}:
                raise RuntimeError(f"CLOAK_AGENT_MODE 配置无效：{mode}")
            try:
                agent = attach_agent(driver, mode=mode)
            except PageAgentConfigError as exc:
                raise RuntimeError(f"页面 Agent 开关已开启但配置未成功：{exc}") from exc
            if agent is None:
                raise RuntimeError("页面 Agent 开关已开启但 Agent 配置未成功")

        takeover_active = bool(agent and agent.mode == "takeover")
        otp_after_ts = time.time()
        logger.info("[Cloak注册] 打开登录页：%s", _CLOAK_LOGIN_URL)
        _safe_cloak_get(driver, _CLOAK_LOGIN_URL)
        human_delay("navigate")
        _reject_initial_browser_proxy_challenge(driver, proxy)
        _wait_for_cloudflare_challenge(
            driver,
            timeout=int(getattr(_cfg, "CLOAK_CHALLENGE_TIMEOUT", 300) or 300),
            headless=bool(getattr(_cfg, "CLOAK_HEADLESS", False)),
            agent=agent if takeover_active else None,
        )
        _maybe_accept(driver)
        _check_manual_stop()

        current_otp = otp_code
        used_otp_codes: set[str] = set()
        next_state = None
        profile_submitted = False

        if takeover_active:
            # 完全接管模式不再调用固定邮箱提交/长等待器。Agent 每轮只执行
            # 一个动作，随后重新读取当前 HTML，再决定填写、点击或等待。
            password_value = _registration_password_for_agent() if _create_password_enabled() else ""
            next_state, password_used = _run_agent_takeover_until_input(
                agent,
                driver,
                email=email,
                name=name,
                birthday=birthday,
                password=password_value,
                timeout=max(120, int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90) * 2),
            )
            if password_used:
                openai_password = password_value
            logger.info(
                "[Cloak注册][Agent] HTML 单步接管已到达真实阶段=%s，创建密码已执行=%s",
                next_state, bool(openai_password),
            )

            # 只有 HTML 中实际出现 OTP 输入框后才开始收码，避免仍停在邮箱页
            # 时提前进入“等待验证码”状态。
            if next_state == "otp" and current_otp is None:
                logger.info("[Cloak注册][Agent] 已检测到真实 OTP 输入框，开始等待验证码：%s", email)
                current_otp = wait_for_otp(
                    email,
                    after_ts=otp_after_ts,
                    exclude_codes=used_otp_codes,
                )
            if current_otp:
                logger.info("[Cloak注册][Agent] 收到验证码，继续由 Agent 单步填写和提交")

            if next_state != "logged_in":
                final_state, password_used_after, profile_seen = _run_agent_takeover_after_otp(
                    agent,
                    driver,
                    email=email,
                    otp=current_otp,
                    password=password_value,
                    name=name,
                    birthday=birthday,
                    timeout=max(120, int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90) * 2),
                )
                if password_used_after:
                    openai_password = password_value
                if profile_seen:
                    create_acknowledged = True
                logger.info("[Cloak注册][Agent] 全页面接管完成，最终阶段=%s", final_state)
            if _create_password_enabled():
                _require_password_if_enabled(openai_password, email, driver_name="CloakBrowser Agent")
        else:
            # hybrid / 未启用 Agent：继续使用原固定流程，异常时由 Agent 补位。
            next_state = _submit_email_and_wait_next(driver, email, attempts=3)
            _wait_for_cloudflare_challenge(
                driver,
                timeout=int(getattr(_cfg, "CLOAK_CHALLENGE_TIMEOUT", 300) or 300),
                headless=bool(getattr(_cfg, "CLOAK_HEADLESS", False)),
            )
            _check_manual_stop()
            logger.info(
                "[Cloak注册] 邮箱下一步状态=%s，创建密码开关=%s",
                next_state,
                _create_password_enabled(),
            )

            # 新版 Auth 的 OTP 页会提供“使用密码继续”按钮。密码开关开启时，
            # 先进入密码分支并提交密码，再开始邮箱取码。
            if next_state == "otp":
                try:
                    openai_password = _ensure_password_before_otp(
                        driver, email, next_state, openai_password, driver_name="CloakBrowser"
                    )
                except Exception:
                    if not agent:
                        raise
                    _agent_checkpoint(
                        agent,
                        driver,
                        "password",
                        {"email": email, "password": _registration_password_for_agent()},
                        force=True,
                    )
                    openai_password = _fill_password_page_if_present(
                        driver, email, timeout=20, allow_login_password=True
                    )
                    _require_password_if_enabled(openai_password, email, driver_name="CloakBrowser")
            elif next_state != "logged_in":
                openai_password = _fill_password_page_if_present(driver, email, timeout=25)
                _require_password_if_enabled(openai_password, email, driver_name="CloakBrowser")
            _check_manual_stop()
            if next_state == "logged_in":
                openai_password = None
            if next_state != "otp" and next_state != "logged_in":
                _require_password_if_enabled(openai_password, email, driver_name="CloakBrowser")

            max_otp_attempts = 0 if next_state == "logged_in" else 3
            for otp_attempt in range(1, max_otp_attempts + 1):
                if current_otp is None:
                    logger.info("[Cloak注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                    try:
                        current_otp = wait_for_otp(
                            email,
                            after_ts=otp_after_ts,
                            exclude_codes=used_otp_codes,
                        )
                    except Exception as exc:
                        if otp_attempt >= max_otp_attempts:
                            raise
                        logger.warning(
                            "[Cloak注册][OTP] 一直未收到验证码，点击重新发送后继续等待（下一轮 %s/%s）：%s: %s",
                            otp_attempt + 1,
                            max_otp_attempts,
                            type(exc).__name__,
                            str(exc)[:180],
                        )
                        otp_after_ts = time.time()
                        resend_result = _click_resend_email_otp(driver, timeout=25)
                        if resend_result.get("reason") == "already_accepted":
                            logger.info("[Cloak注册][OTP] 等待新验证码期间页面已进入下一阶段，停止继续收码")
                            break
                        human_delay("api")
                        current_otp = None
                        continue
                page_stage = _wait_for_email_otp_page(driver, timeout=15)
                if page_stage == "logged_in":
                    logger.info("[Cloak注册][OTP] 输入验证码前已检测到登录态，跳过提交")
                    break
                if page_stage == "profile":
                    logger.info("[Cloak注册][OTP] 输入验证码前已进入资料页，跳过提交")
                    break
                if page_stage == "login_password":
                    raise RuntimeError(
                        "邮箱验证码等待期间进入登录密码页，邮箱可能已注册："
                        f"url={getattr(driver, 'current_url', '')}"
                    )
                if page_stage != "otp":
                    raise RuntimeError(
                        "邮箱验证码页面未就绪，停止提交并保留页面诊断："
                        f"stage={page_stage} url={getattr(driver, 'current_url', '')}"
                    )
                logger.info("[Cloak注册][OTP] 收到验证码：%s", current_otp)
                _clear_otp_inputs(driver)
                _type_otp(driver, current_otp)
                human_delay("otp_input")
                try:
                    _click_continue(driver)
                except Exception as exc:
                    if agent:
                        snapshot = agent.snapshot(driver)
                        state, snapshot = _agent_page_state(driver, snapshot)
                        if state in {"profile", "logged_in"}:
                            logger.info("[Cloak注册][Agent] 固定提交器异常，但 HTML 已进入阶段=%s", state)
                        else:
                            result = _agent_checkpoint(
                                agent,
                                driver,
                                "otp",
                                {"email": email, "otp": current_otp},
                                force=True,
                                snapshot=snapshot,
                                max_actions=1,
                            )
                            logger.info(
                                "[Cloak注册][OTP] 固定提交器异常，已调用页面 Agent 补位：executed=%s error=%s",
                                getattr(result, "executed", 0),
                                str(exc)[:120],
                            )
                    else:
                        logger.info("[Cloak注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", str(exc)[:120])

                outcome = _wait_after_email_otp_submit(driver, timeout=10)
                if outcome == "stalled" and agent:
                    snapshot = agent.snapshot(driver)
                    state, snapshot = _agent_page_state(driver, snapshot)
                    if state in {"profile", "logged_in"}:
                        logger.info("[Cloak注册][Agent] OTP 后 HTML 已进入阶段=%s，按提交成功继续", state)
                        outcome = "accepted"
                    elif state == "otp" and not (snapshot.get("errors") or []):
                        result = _agent_checkpoint(
                            agent,
                            driver,
                            "otp",
                            {"email": email, "otp": current_otp},
                            force=True,
                            snapshot=snapshot,
                            max_actions=1,
                        )
                        if result and result.executed:
                            logger.info("[Cloak注册][Agent] OTP 页未报错但未推进，Agent 已执行补充动作")
                            outcome = _wait_after_email_otp_submit(driver, timeout=10)
                if outcome == "accepted":
                    break
                if otp_attempt >= max_otp_attempts:
                    raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
                used_otp_codes.add(str(current_otp or "").strip())
                otp_after_ts = time.time()
                resend_result = _click_resend_email_otp(driver, timeout=25)
                if resend_result.get("reason") == "already_accepted":
                    break
                human_delay("api")
                current_otp = None

            openai_password = _ensure_password_after_otp(driver, email, next_state, openai_password)
            profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
            if profile_submitted:
                create_acknowledged = True
                human_delay("post_auth")

        session_info = _fetch_chatgpt_session(driver, timeout=120)
        access_token = session_info["accessToken"]
        logger.info("[Cloak注册] 已拿到 accessToken：%s", email)
        # 后置 2FA/Codex 可能复用并清理当前窗口，先保存注册成功瞬间的完整登录态。
        saved_session = build_saved_session(session_info, capture_browser_cookies(driver))
        from core.traffic import browser_performance_snapshot
        registration_traffic = browser_performance_snapshot(driver)

        totp_secret = None
        twofa_result = {
            "requested": bool(_twofa_cfg.ENABLE_2FA),
            "status": "pending" if _twofa_cfg.ENABLE_2FA else "disabled",
            "error": None,
        }
        if _twofa_cfg.ENABLE_2FA:
            try:
                logger.info("[Cloak注册][2FA] ENABLE_2FA=True，开始设置 TOTP")
                totp_secret = setup_2fa_from_browser(
                    driver,
                    email,
                    proxy=getattr(driver, "upstream_proxy_url", None) or proxy,
                    previous_otp=current_otp,
                    access_token=access_token,
                )
                twofa_result["status"] = "success"
                logger.info("[Cloak注册][2FA] TOTP 设置完成")
            except Exception as exc:
                details = describe_twofa_error(exc)
                twofa_result["status"] = "failed"
                twofa_result["error"] = (
                    f"{details['code']} stage={details['stage']} "
                    f"http={details['status'] or '-'}: {details['detail']}"
                )
                twofa_result.update({
                    "failure_code": details["code"],
                    "failure_stage": details["stage"],
                    "failure_status": details["status"],
                    "attempts": 3,
                })
                logger.error("[Cloak注册][2FA] 设置失败：code=%s stage=%s http=%s detail=%s", details["code"], details["stage"], details["status"] or "-", details["detail"])
                logger.debug("[Cloak注册][2FA] 失败详情", exc_info=True)
                # 2FA 属于注册后处理；失败时保留浏览器现场，便于查看实际页面而不是误以为闪退。
                keep_browser_on_error = bool(getattr(_cfg, "CLOAK_KEEP_BROWSER_OPEN_ON_ERROR", True))
                if keep_browser_on_error:
                    logger.warning("[Cloak注册][2FA] 已保留指纹浏览器窗口供检查，任务仍会保存已注册账号")

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg
            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                from core.roxy_codex_oauth import run_roxy_codex_oauth
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=True，复用当前 CloakBrowser 窗口执行 Codex 授权")
                _check_manual_stop()
                codex_result = run_roxy_codex_oauth(
                    email,
                    reuse_existing_profile=True,
                    existing_driver=driver,
                    existing_opened=opened,
                    force=True,
                    clear_existing_state=True,
                )
            else:
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=(getattr(opened, "proxy_url", None) if opened else None) or proxy or None,
            batch_dir=batch_dir,
            registration_method="cloak",
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "session": saved_session,
                "registration_traffic": registration_traffic,
                "cloakbrowser": {
                    "profile_id": opened.profile_id,
                    "open_result": opened.raw,
                    "proxy_used": getattr(opened, "proxy_url", None) or proxy or "",
                },
                "registration_password": openai_password,
                "twofa": twofa_result,
                "codex": codex_result,
            },
        )
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {"success": bool(codex_ok), "email": email, "account_id": account_id, "access_token": access_token, "totp_secret": totp_secret, "codex": codex_result, "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}"}
    except Exception as exc:
        logger.error("[Cloak注册] 失败：%s: %s", type(exc).__name__, exc)
        try:
            page_state = driver.execute_script("return {url: location.href, title: document.title};") if driver else {}
            logger.error("[Cloak注册] 失败时页面仍在：url=%s title=%s", page_state.get("url", "-"), page_state.get("title", "-"))
        except Exception:
            pass
        logger.debug("[Cloak注册] 失败详情", exc_info=True)
        keep_browser_on_error = bool(getattr(_cfg, "CLOAK_KEEP_BROWSER_OPEN_ON_ERROR", True))
        # 瞬态浏览器/网络错误会由外层有限重跑；保留旧窗口会叠加残留
        # 上下文并放大 TargetClosedError，因此这类错误优先清理现场。
        if _is_transient_cloak_error(exc):
            keep_browser_on_error = False
        if keep_browser_on_error:
            logger.warning("[Cloak注册] 已保留浏览器窗口供检查；关闭窗口后再开始下一条任务")
        if _BROWSER_PROXY_CHALLENGE_MARKER not in str(exc):
            try:
                from core.email_provider import release_email
                release_email(email, status="failed" if create_acknowledged else "available", note=f"Cloak注册失败: {str(exc)[:180]}")
            except Exception:
                pass
        result = {"success": False, "email": email, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        if _BROWSER_PROXY_CHALLENGE_MARKER in str(exc):
            result["_failed_proxy_url"] = str(
                proxy or getattr(driver, "upstream_proxy_url", None) or ""
            ).strip()
        return result
    finally:
        if driver and not bool(_cfg.CLOAK_KEEP_BROWSER_OPEN) and not keep_browser_on_error:
            try:
                driver.quit()
            except Exception:
                pass


def run_cloak_registration(email: str, name: str, birthday: str, proxy: str = None, otp_code: str = None, batch_dir: Path | None = None) -> dict:
    """执行 Cloak 注册，并对挑战/网络/浏览器生命周期故障做有限重跑。"""
    logger.info("[Cloak注册] 使用隔离线程启动浏览器，避免 Sync API 与 asyncio loop 冲突")
    try:
        retry_limit = int(getattr(_cfg, "CLOAK_NAVIGATION_RETRIES", 3) or 3)
    except (TypeError, ValueError):
        retry_limit = 3
    retry_limit = max(1, min(retry_limit, 3))
    configured_headless = bool(getattr(_cfg, "CLOAK_HEADLESS", False))
    fallback_on_challenge = bool(getattr(_cfg, "CLOAK_HEADLESS_FALLBACK_ON_CHALLENGE", True))
    last_result: dict | None = None
    for attempt in range(1, retry_limit + 1):
        # 首次遵循配置；无头遇到交互式挑战后，下一轮强制可见并重新建
        # 浏览器上下文，同时让 proxy=None 重新从池中抽取出口。
        headless_override = None
        if attempt > 1 and fallback_on_challenge and configured_headless and last_result:
            if "cloudflare 人机验证" in str(last_result.get("error") or ""):
                headless_override = False
        result = _run_in_isolated_thread(
            _run_cloak_registration_impl,
            email=email,
            name=name,
            birthday=birthday,
            proxy=proxy,
            otp_code=otp_code,
            batch_dir=batch_dir,
            headless_override=headless_override,
        )
        last_result = result if isinstance(result, dict) else {"success": False, "error": str(result)}
        if _BROWSER_PROXY_CHALLENGE_MARKER in str(last_result.get("error") or ""):
            return last_result
        if last_result.get("success") or not _is_transient_cloak_error(last_result.get("error")) or attempt >= retry_limit:
            return last_result
        logger.warning(
            "[Cloak注册] 瞬态失败，准备重建浏览器并重试：attempt=%s/%s error=%s headless_override=%s",
            attempt, retry_limit, str(last_result.get("error") or "")[:220], headless_override,
        )
        _check_manual_stop()
        time.sleep(min(5.0, 1.5 * attempt))
    return last_result or {"success": False, "email": email, "error": "Cloak 注册未返回结果"}
