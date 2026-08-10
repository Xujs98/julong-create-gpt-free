# -*- coding: utf-8 -*-
"""通过 CloakBrowser + Playwright 适配层执行 ChatGPT 注册。"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from config import cloakbrowser as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data, setup_2fa_from_browser
from core.cloakbrowser_driver import build_cloak_driver
from core.email_provider import wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay
from core.session_state import build_saved_session, capture_browser_cookies
from core.page_agent import PageAgentConfigError, attach_agent

# 复用 Roxy 注册流程里已维护好的页面操作函数。
from core.roxy_registration import (  # noqa: F401
    _maybe_accept, _submit_email_and_wait_next, _fill_password_page_if_present,
    _ensure_password_before_otp,
    _wait_email_submit_next_state,
    _clear_otp_inputs, _type_otp, _click_continue, _wait_after_email_otp_submit,
    _click_resend_email_otp, _complete_profile_page, _fetch_chatgpt_session, _check_manual_stop,
    _wait_for_cloudflare_challenge,
    _require_password_if_enabled, _create_password_enabled,
    _registration_password,
)

logger = logging.getLogger(__name__)


def _agent_checkpoint(agent, driver, stage: str, context: dict, *, force: bool = False):
    if agent is None:
        return None
    result = agent.assist(driver, stage, context, force=force)
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


def _run_cloak_registration_impl(email: str, name: str, birthday: str, proxy: str = None, otp_code: str = None, batch_dir: Path | None = None) -> dict:
    """CloakBrowser 自动化注册入口。"""
    driver = None
    opened = None
    create_acknowledged = False
    keep_browser_on_error = False
    openai_password: str | None = None
    agent = None
    try:
        driver, opened = build_cloak_driver(proxy=proxy)
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

        otp_after_ts = time.time()
        logger.info("[Cloak注册] 打开登录页：https://chatgpt.com/auth/login")
        driver.get("https://chatgpt.com/auth/login")
        human_delay("navigate")
        _wait_for_cloudflare_challenge(
            driver,
            timeout=int(getattr(_cfg, "CLOAK_CHALLENGE_TIMEOUT", 300) or 300),
            headless=bool(getattr(_cfg, "CLOAK_HEADLESS", False)),
        )
        _maybe_accept(driver)
        _check_manual_stop()

        next_state = None
        if agent and agent.mode == "takeover":
            email_result = _agent_checkpoint(agent, driver, "email", {"email": email}, force=True)
            if email_result and email_result.executed:
                observed_state = _wait_email_submit_next_state(driver, email, timeout=20)
                if observed_state in {"password", "otp", "logged_in"}:
                    next_state = observed_state
                    logger.info("[Cloak注册][Agent] 邮箱阶段已完全接管，下一状态=%s", next_state)
                else:
                    logger.info("[Cloak注册][Agent] 邮箱阶段未形成有效下一状态=%s，使用固定流程补位", observed_state)
        if next_state is None:
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
        # 必须先点击该分支并提交 create-account/password，再开始邮箱取码。
        if next_state == "otp":
            if agent and agent.mode == "takeover" and _create_password_enabled():
                password_value = _registration_password_for_agent()
                _agent_checkpoint(agent, driver, "password_entry", {"email": email}, force=True)
                time.sleep(0.7)
                password_result = _agent_checkpoint(
                    agent, driver, "password", {"email": email, "password": password_value}, force=True
                )
                if _agent_used_value_ref(password_result, "password"):
                    openai_password = password_value
            try:
                openai_password = _ensure_password_before_otp(
                    driver, email, next_state, openai_password, driver_name="CloakBrowser"
                )
            except Exception:
                if not agent:
                    raise
                _agent_checkpoint(agent, driver, "password", {"email": email, "password": _registration_password_for_agent()}, force=True)
                openai_password = _fill_password_page_if_present(driver, email, timeout=20, allow_login_password=True)
                _require_password_if_enabled(openai_password, email, driver_name="CloakBrowser")
        elif next_state != "logged_in":
            if agent and agent.mode == "takeover":
                password_value = _registration_password_for_agent()
                password_result = _agent_checkpoint(
                    agent, driver, "password", {"email": email, "password": password_value}, force=True
                )
                if _agent_used_value_ref(password_result, "password"):
                    openai_password = password_value
            fallback_password = _fill_password_page_if_present(driver, email, timeout=25)
            if fallback_password:
                openai_password = fallback_password
            _require_password_if_enabled(openai_password, email, driver_name="CloakBrowser")
        _check_manual_stop()
        if next_state == "logged_in":
            # 持久化上下文已有登录态时，不重复要求创建密码。
            openai_password = None
        if next_state != "otp" and next_state != "logged_in":
            _require_password_if_enabled(openai_password, email, driver_name="CloakBrowser")

        current_otp = otp_code
        max_otp_attempts = 0 if next_state == "logged_in" else 3
        for otp_attempt in range(1, max_otp_attempts + 1):
            if current_otp is None:
                logger.info("[Cloak注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                try:
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                except Exception as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    logger.warning(
                        "[Cloak注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                        otp_attempt + 1,
                        max_otp_attempts,
                        type(exc).__name__,
                        str(exc)[:180],
                    )
                    otp_after_ts = time.time()
                    _click_resend_email_otp(driver, timeout=25)
                    human_delay("api")
                    current_otp = None
                    continue
            logger.info("[Cloak注册][OTP] 收到验证码：%s", current_otp)
            if agent and agent.mode == "takeover":
                agent_result = _agent_checkpoint(agent, driver, "otp", {"email": email, "otp": current_otp}, force=True)
                if agent_result and agent_result.executed:
                    outcome = _wait_after_email_otp_submit(driver, timeout=10)
                    if outcome == "accepted":
                        break
            _clear_otp_inputs(driver)
            try:
                _type_otp(driver, current_otp)
            except RuntimeError:
                if not agent:
                    raise
                _agent_checkpoint(agent, driver, "otp", {"email": email, "otp": current_otp}, force=True)
                _type_otp(driver, current_otp)
            human_delay("otp_input")
            try:
                _click_continue(driver)
            except Exception as exc:
                logger.info("[Cloak注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", str(exc)[:120])

            outcome = _wait_after_email_otp_submit(driver, timeout=10)
            if outcome == "accepted":
                break
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            otp_after_ts = time.time()
            resend_result = _click_resend_email_otp(driver, timeout=25)
            if resend_result.get("reason") == "already_accepted":
                break
            human_delay("api")
            current_otp = None

        openai_password = _ensure_password_after_otp(driver, email, next_state, openai_password)

        if agent and agent.mode == "takeover":
            birthday_parts = str(birthday or "").split("-")
            profile_result = _agent_checkpoint(
                agent,
                driver,
                "profile",
                {
                    "name": name,
                    "birthday": birthday,
                    "birth_year": birthday_parts[0] if len(birthday_parts) > 0 else "",
                    "birth_month": birthday_parts[1] if len(birthday_parts) > 1 else "",
                    "birth_day": birthday_parts[2] if len(birthday_parts) > 2 else "",
                },
                force=True,
            )
            if profile_result and profile_result.executed:
                human_delay("post_auth")
        profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
        if profile_submitted:
            create_acknowledged = True
            human_delay("post_auth")

        session_info = _fetch_chatgpt_session(driver, timeout=120)
        access_token = session_info["accessToken"]
        logger.info("[Cloak注册] 已拿到 accessToken：%s", email)
        # 后置 2FA/Codex 可能复用并清理当前窗口，先保存注册成功瞬间的完整登录态。
        saved_session = build_saved_session(session_info, capture_browser_cookies(driver))

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
                )
                twofa_result["status"] = "success"
                logger.info("[Cloak注册][2FA] TOTP 设置完成")
            except Exception as exc:
                twofa_result["status"] = "failed"
                twofa_result["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
                logger.error("[Cloak注册][2FA] 设置失败：%s: %s", type(exc).__name__, exc)
                logger.debug("[Cloak注册][2FA] 失败详情", exc_info=True)

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
            proxy_used=((opened.raw or {}).get("proxy") if opened else None) or proxy or None,
            batch_dir=batch_dir,
            registration_method="cloak",
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "session": saved_session,
                "cloakbrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
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
        if keep_browser_on_error:
            logger.warning("[Cloak注册] 已保留浏览器窗口供检查；关闭窗口后再开始下一条任务")
        try:
            from core.email_provider import release_email
            release_email(email, status="failed" if create_acknowledged else "available", note=f"Cloak注册失败: {str(exc)[:180]}")
        except Exception:
            pass
        return {"success": False, "email": email, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        if driver and not bool(_cfg.CLOAK_KEEP_BROWSER_OPEN) and not keep_browser_on_error:
            try:
                driver.quit()
            except Exception:
                pass


def run_cloak_registration(email: str, name: str, birthday: str, proxy: str = None, otp_code: str = None, batch_dir: Path | None = None) -> dict:
    """执行 Cloak 注册，并隔离 Playwright Sync API 的事件循环。"""
    logger.info("[Cloak注册] 使用隔离线程启动浏览器，避免 Sync API 与 asyncio loop 冲突")
    return _run_in_isolated_thread(
        _run_cloak_registration_impl,
        email=email,
        name=name,
        birthday=birthday,
        proxy=proxy,
        otp_code=otp_code,
        batch_dir=batch_dir,
    )
