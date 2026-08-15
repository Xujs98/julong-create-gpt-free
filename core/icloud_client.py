# -*- coding: utf-8 -*-
"""iCloud 邮箱池客户端：读取导入的 HTML 取码地址并轮询验证码。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from config import email as _email_cfg
from core.generic_api_mail_client import (
    _extract_code,
    _extract_html_selector_code,
    _extract_yangyang_openai_code,
)

logger = logging.getLogger(__name__)


class ICloudMailError(RuntimeError):
    """iCloud 邮箱池请求或取码失败。"""


@dataclass
class ICloudEmailAccount:
    """一个 iCloud 邮箱及其 HTML 取码地址。"""

    email: str
    code_url: str


_CONTEXT_CACHE: dict[str, ICloudEmailAccount] = {}


def _key(email: str) -> str:
    """统一邮箱大小写，避免上下文缓存出现重复键。"""
    return str(email or "").strip().lower()


def pick_account() -> ICloudEmailAccount:
    """从 iCloud 邮箱池原子领取一个可用邮箱。"""
    from core.db import claim_next_icloud_email, icloud_email_pool_summary

    row = claim_next_icloud_email()
    if row is None:
        summary = icloud_email_pool_summary()
        raise ICloudMailError(f"iCloud 邮箱池没有可用账号: {summary}，请导入“邮箱----URL”素材")
    account = ICloudEmailAccount(email=row["email"], code_url=row["code_url"])
    _CONTEXT_CACHE[_key(account.email)] = account
    logger.info("[iCloud] 选中邮箱: %s（DB id=%s）", account.email, row.get("id"))
    return account


def get_account_context(email: str) -> ICloudEmailAccount | None:
    """按邮箱查找内存或持久化的 iCloud 取码上下文。"""
    cache_key = _key(email)
    if cache_key in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[cache_key]
    from core.db import get_icloud_email_by_email

    row = get_icloud_email_by_email(email)
    if row is None:
        return None
    account = ICloudEmailAccount(email=row["email"], code_url=row["code_url"])
    _CONTEXT_CACHE[cache_key] = account
    return account


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    """更新 iCloud 邮箱池状态并清理当前任务上下文。"""
    from core.db import release_icloud_email

    release_icloud_email(email, status=status, note=note)
    _CONTEXT_CACHE.pop(_key(email), None)


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
    exclude_codes: set[str] | None = None,
) -> str:
    """轮询 HTML 地址并返回稳定且未被排除的 6 位验证码。"""
    account = get_account_context(email)
    if account is None:
        raise ICloudMailError(f"iCloud 邮箱不存在或未导入: {email}")

    deadline = time.time() + int(max_wait if max_wait is not None else getattr(_email_cfg, "OTP_MAX_WAIT", 90) or 90)
    interval = max(1, int(poll_interval if poll_interval is not None else getattr(_email_cfg, "OTP_POLL_INTERVAL", 3) or 3))
    settle = max(0, int(settle_seconds if settle_seconds is not None else getattr(_email_cfg, "OTP_SETTLE_SECONDS", 5) or 0))
    timeout = max(1, int(getattr(_email_cfg, "ICLOUD_REQUEST_TIMEOUT", 20) or 20))
    verify = bool(getattr(_email_cfg, "ICLOUD_VERIFY_TLS", True))
    headers = {"Accept": "text/html,application/xhtml+xml,text/plain,*/*", "User-Agent": "Mozilla/5.0 (compatible; iCloudMail/1.0)"}
    excluded = {str(code or "").strip() for code in (exclude_codes or set()) if str(code or "").strip()}
    best: str | None = None
    settle_until: float | None = None
    last_excluded_logged: str | None = None
    last_error = ""
    logger.info("[iCloud] 开始轮询 HTML 取码地址: %s", email)

    while time.time() < deadline:
        try:
            response = requests.get(account.code_url, headers=headers, timeout=timeout, verify=verify)
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}: {(response.text or '')[:160]}"
            else:
                body = response.text or ""
                # iCloud API 返回 HTML；先用带 OpenAI 语义的抽取器，减少模板数字误判。
                code = (
                    _extract_html_selector_code(body)
                    or _extract_yangyang_openai_code("", body)
                    or _extract_code(body)
                )
                if code:
                    # HTML 取码页只暴露“当前验证码”，没有邮件时间戳；2FA 重认证时
                    # 必须显式排除注册阶段已经使用过的验证码，等待页面更新为新码。
                    if code in excluded:
                        best = None
                        settle_until = None
                        if last_excluded_logged != code:
                            logger.info("[iCloud] 忽略已使用验证码=%s，继续等待新验证码", code)
                            last_excluded_logged = code
                        time.sleep(min(interval, max(0.1, deadline - time.time())))
                        continue
                    now = time.time()
                    if best != code:
                        best = code
                        settle_until = now + settle
                        logger.info("[iCloud] 锁定验证码=%s，等待 %ss 确认更新", code, settle)
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        now = time.time()
        if best and settle_until is not None and now >= settle_until:
            return best
        time.sleep(min(interval, max(0.1, deadline - now)))

    if best:
        logger.warning("[iCloud] 轮询超时但已有验证码，返回候选=%s", best)
        return best
    suffix = f"；最近错误: {last_error}" if last_error else ""
    raise ICloudMailError(f"等待 iCloud HTML 验证码超时: {email}{suffix}")
