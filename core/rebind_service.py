# -*- coding: utf-8 -*-
"""后台账号换绑任务服务。

换绑和注册共用 ``registration_jobs`` 存储实体，因此任务会自然出现在
注册页的任务列表和日志接口中。实际站点交互通过可注入 executor 完成；
生产环境可提供 ``core.rebind_driver.rebind_account``，测试和集成层可用
``set_rebind_executor`` 注入确定性的实现。
"""
from __future__ import annotations

import inspect
import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from core import db

logger = logging.getLogger(__name__)

REBIND_SOURCES = ("icloud", "cloudflare_domain", "generic_api", "outlook")
REBIND_DRIVERS = ("protocol", "cloak", "roxy")
REBIND_BROWSER_DRIVERS = ("cloak", "roxy")
_DEFAULT_WORKERS = 3
_MAX_WORKERS = 16
_QUEUE_LIMIT = 500

_executor: ThreadPoolExecutor | None = None
_dispatcher: ThreadPoolExecutor | None = None
_executor_workers = _DEFAULT_WORKERS
_executor_lock = threading.RLock()
_pool_generation = 0
_queue_slots = threading.BoundedSemaphore(_QUEUE_LIMIT)
_stop_events: dict[int, threading.Event] = {}
_active_jobs: set[int] = set()
_state_lock = threading.RLock()
_rebind_executor: Callable[..., Any] | None = None


class RebindExecutionError(RuntimeError):
    """换绑驱动未配置或实际操作失败。"""


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Normalize WebUI/config boolean values without Python's string trap.

    JSON callers and ``.env`` values can arrive as strings.  Using ``bool``
    directly would treat every non-empty string (including ``"false"``) as
    true, which is especially dangerous for the hybrid/headless switches.
    Unknown strings fall back to the supplied default rather than silently
    enabling a mode.
    """
    if value is None:
        return bool(default)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on", "y", "是", "开启", "启用"}:
            return True
        if normalized in {"0", "false", "no", "off", "n", "否", "关闭", "禁用", ""}:
            return False
        return bool(default)
    return bool(value)


def coerce_rebind_bool(value: Any, default: bool = False) -> bool:
    """Public wrapper used by WebUI/config adapters for consistent parsing."""
    return _coerce_bool(value, default)


_SECRET_LABEL_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|access[_-]?token|refresh[_-]?token|"
    r"reservation(?:_id)?|code[_-]?url|log[_-]?file)\b(\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_URL_RE = re.compile(r"(?i)(?:https?://|data:)[^\s\"'<>]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_LOG_PATH_RE = re.compile(r"(?:[A-Za-z]:)?[/\\][^\r\n\"']+?\.log\b", re.IGNORECASE)


def _redact_text(value: Any, secrets: list[str] | tuple[str, ...] | set[str] = ()) -> str:
    """Remove worker-only mailbox/session material from persisted task text."""
    text = str(value or "")
    candidates = sorted(
        {
            str(item)
            for item in secrets
            if item is not None and len(str(item)) >= 4
        },
        key=len,
        reverse=True,
    )
    for secret in candidates:
        text = text.replace(secret, "[REDACTED]")
    text = _SECRET_LABEL_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    text = _URL_RE.sub("[REDACTED_URL]", text)
    text = _JWT_RE.sub("[REDACTED_TOKEN]", text)
    text = _LOG_PATH_RE.sub("[REDACTED_LOG_PATH]", text)
    return text


def redact_rebind_text(
    value: Any,
    secrets: list[str] | tuple[str, ...] | set[str] = (),
) -> str:
    """Public redaction helper for WebUI task/log responses."""
    return _redact_text(value, secrets)


def _secret_values(*records: dict | None) -> list[str]:
    """Collect exact secret values used to scrub callback output and errors."""
    keys = {
        "password", "client_id", "clientId", "refresh_token", "refreshToken",
        "access_token", "token", "code_url", "url", "reservation_id",
        "rebind_reservation_id", "rebind_proxy", "log_file", "original_email_line",
    }
    values: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in keys:
            raw = record.get(key)
            if raw not in (None, ""):
                values.append(str(raw))
    return values


def _session_email(session: dict) -> str:
    user = session.get("user") if isinstance(session.get("user"), dict) else {}
    return str(
        session.get("email")
        or session.get("user_email")
        or user.get("email")
        or ""
    ).strip()


def _session_access_token(session: dict) -> str:
    direct = str(
        session.get("access_token")
        or session.get("accessToken")
        or ""
    ).strip()
    if direct:
        return direct
    for key in ("user", "session", "session_info", "data", "result", "payload"):
        nested = session.get(key)
        if isinstance(nested, dict):
            token = _session_access_token(nested)
            if token:
                return token
    return ""


def _validated_success_result(raw: Any, target_email: str) -> dict:
    """Require proof that the external account now uses the reserved mailbox.

    An ``ok`` boolean alone is insufficient to delete the old local record.
    The driver must return the observed account email and a usable refreshed
    access token (directly or inside ``session``).
    """
    if not isinstance(raw, dict) or not _coerce_bool(raw.get("ok"), False):
        # Callback payloads may contain mailbox credentials or session material
        # in ``error``/``message``; keep the persisted task message generic.
        raise RebindExecutionError("换绑驱动未确认成功")
    result = dict(raw)
    session = result.get("session") if isinstance(result.get("session"), dict) else {}
    observed_email = str(
        result.get("verified_email")
        or result.get("current_email")
        or result.get("account_email")
        or result.get("email")
        or _session_email(session)
        or ""
    ).strip()
    if not observed_email or observed_email.casefold() != str(target_email or "").strip().casefold():
        raise RebindExecutionError("换绑驱动未验证目标邮箱已生效")
    token = str(result.get("access_token") or _session_access_token(session) or "").strip()
    if not token:
        raise RebindExecutionError("换绑驱动未返回已验证会话的 Access Token")
    result["verified_email"] = observed_email
    result["access_token"] = token
    return result


def set_rebind_executor(executor: Callable[..., Any] | None) -> None:
    """设置进程级换绑执行器；传 ``None`` 恢复默认驱动发现逻辑。"""
    global _rebind_executor
    with _state_lock:
        _rebind_executor = executor


def get_rebind_executor() -> Callable[..., Any] | None:
    with _state_lock:
        return _rebind_executor


def _default_executor(
    account: dict,
    target: dict,
    *,
    driver: str,
    login_driver: str | None = None,
    action_driver: str | None = None,
    hybrid: bool = False,
    headless: bool,
    login_headless: bool | None = None,
    proxy: str | None = None,
    log: Callable[[str], None] | None = None,
) -> dict:
    """发现可选的站点驱动。

    驱动模块是可选依赖，避免在没有浏览器依赖的环境中导入 WebUI 失败。
    约定函数签名为 ``rebind_account(account, target_email, ...)``，返回
    ``{"ok": True, ...}``；目标邮箱池素材通过 ``target`` 一并传入。
    """
    try:
        from core import rebind_driver  # type: ignore
    except (ImportError, AttributeError) as exc:
        raise RebindExecutionError(
            "未配置换绑驱动，请提供 core.rebind_driver.rebind_account 或注入 executor"
        ) from exc
    fn = getattr(rebind_driver, "rebind_account", None)
    if not callable(fn):
        raise RebindExecutionError("core.rebind_driver 缺少可调用的 rebind_account")
    return _invoke_executor(
        fn,
        account,
        target,
        driver=driver,
        login_driver=login_driver,
        action_driver=action_driver,
        hybrid=hybrid,
        headless=headless,
        login_headless=login_headless,
        proxy=proxy,
        log=log,
    )


def _invoke_executor(fn: Callable[..., Any], account: dict, target: dict, **kwargs: Any) -> Any:
    """兼容旧的短签名 executor，同时优先传递完整上下文。"""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        signature = None
    if signature is None:
        return fn(account, target, **kwargs)
    params = signature.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(account, target, **kwargs)
    accepted = {key: value for key, value in kwargs.items() if key in params}
    # 支持常见的 (account, target_email) / (account, target) 两种契约。
    positional = list(params.values())
    if len(positional) >= 2:
        second = positional[1].name.lower()
        if "email" in second or second in {"new", "new_email", "target_email"}:
            return fn(account, str(target.get("email") or ""), **accepted)
    return fn(account, target, **accepted)


def _normalize_driver(value: str | None, *, default: str = "protocol") -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        raw = default
    aliases = {"browser": "cloak", "cloakbrowser": "cloak", "roxybrowser": "roxy"}
    raw = aliases.get(raw, raw)
    if raw not in REBIND_DRIVERS:
        raise ValueError(f"换绑方式无效：{raw}，可选 {', '.join(REBIND_DRIVERS)}")
    return raw


def _effective_driver(value: str | None) -> str:
    """Resolve the legacy single-driver argument.

    This remains the action driver for callers that submit ``driver=...``.
    New callers should use :func:`_rebind_plan` so login and submission can be
    configured independently.
    """
    try:
        from config import live_check

        default = str(getattr(live_check, "REBIND_ACTION_DRIVER", "") or "").strip().lower()
        if not default:
            default = str(getattr(live_check, "LIVE_CHECK_DRIVER", "protocol") or "protocol").strip().lower()
    except Exception:
        default = "protocol"
    return _normalize_driver(value, default=default)


def _rebind_plan(
    *,
    driver: str | None = None,
    login_driver: str | None = None,
    action_driver: str | None = None,
    hybrid: Any = None,
    headless: Any = None,
    login_headless: Any = None,
) -> dict[str, Any]:
    """Resolve the two-stage rebind execution plan.

    ``driver`` is the pre-existing API field and, when supplied, keeps the
    historical single-driver behavior unless explicit stage values are also
    supplied. Configuration defaults to hybrid Cloak login + protocol submit.
    """
    try:
        from config import live_check

        configured_login = str(getattr(live_check, "REBIND_LOGIN_DRIVER", "cloak") or "cloak")
        configured_action = str(getattr(live_check, "REBIND_ACTION_DRIVER", "protocol") or "protocol")
        configured_hybrid = _coerce_bool(getattr(live_check, "REBIND_HYBRID_MODE", True), True)
        configured_headless = _coerce_bool(getattr(live_check, "LIVE_CHECK_HEADLESS", False), False)
    except Exception:
        configured_login, configured_action, configured_hybrid, configured_headless = "cloak", "protocol", True, False

    explicit_stage = login_driver is not None or action_driver is not None or hybrid is not None
    if driver is not None and not explicit_stage:
        # Compatibility for existing API clients and tests: one driver means
        # both login and submission use that driver.
        action = _normalize_driver(driver, default=configured_action)
        login = action
        mixed = False
    else:
        action = _normalize_driver(action_driver, default=configured_action)
        login = _normalize_driver(login_driver, default=configured_login)
        mixed = configured_hybrid if hybrid is None else _coerce_bool(hybrid, configured_hybrid)

    # 关闭混合模式时只保留一个驱动。提交驱动是任务的兼容主字段，因而
    # 同时承担登录和提交阶段；否则 UI 显示“单一驱动”但 worker 仍会启动
    # 两套不同的登录/提交实现，配置语义会出现漂移。
    if not mixed:
        login = action

    if mixed and login == "protocol":
        # A mixed plan can still be explicitly configured as protocol login,
        # but the mode remains visible and deterministic to the executor.
        pass
    selected_headless = configured_headless if headless is None else _coerce_bool(headless, configured_headless)
    selected_login_headless = selected_headless if login_headless is None else _coerce_bool(login_headless, selected_headless)
    return {
        "driver": action,
        "action_driver": action,
        "login_driver": login,
        "hybrid": mixed,
        "headless": selected_headless,
        "login_headless": selected_login_headless,
    }


def _effective_headless(value: Any) -> bool:
    if value is not None:
        return _coerce_bool(value, False)
    try:
        from config import live_check

        return _coerce_bool(getattr(live_check, "LIVE_CHECK_HEADLESS", False), False)
    except Exception:
        return False


def _workers(value: Any, count: int) -> int:
    try:
        # With one selected mailbox the omitted setting should still produce a
        # valid single-worker task; an explicitly oversized value remains an
        # input error so the UI/API contract is enforced server-side.
        worker_count = int(value) if value is not None else min(_DEFAULT_WORKERS, max(1, int(count)))
    except (TypeError, ValueError):
        raise ValueError("workers 必须是整数")
    if worker_count < 1 or worker_count > _MAX_WORKERS:
        raise ValueError(f"workers 必须在 1~{_MAX_WORKERS} 之间")
    if worker_count > count:
        raise ValueError("并发线程数量必须小于或等于待换绑邮箱数量")
    return worker_count


def _dispatcher_pool() -> ThreadPoolExecutor:
    """Return the serial batch dispatcher.

    A new batch may request a different worker count while an earlier batch is
    still running.  Serializing batches prevents the retired and replacement
    worker pools from overlapping (which previously exceeded the configured
    concurrency) while still allowing every batch to use its own exact limit.
    """
    global _dispatcher
    with _executor_lock:
        if _dispatcher is None:
            _dispatcher = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rebind-dispatch")
        return _dispatcher


def _run_batch(job_ids: list[int], workers: int) -> None:
    """Run one batch with its requested concurrency, then fully retire its pool."""
    global _executor, _executor_workers, _pool_generation
    pool: ThreadPoolExecutor | None = None
    try:
        with _executor_lock:
            _pool_generation += 1
            pool = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix=f"rebind-worker-{_pool_generation}",
            )
            _executor = pool
            _executor_workers = workers
        for job_id in job_ids:
            try:
                pool.submit(_run_one, int(job_id))
            except Exception as exc:
                job = db.get_job(int(job_id)) or {}
                db.release_rebind_email(
                    job.get("rebind_target_source"),
                    job.get("rebind_target_email"),
                    reservation_id=job.get("rebind_reservation_id"),
                    success=False,
                    note="任务提交异常，邮箱已释放",
                )
                _mark_terminal(
                    int(job_id),
                    status="failed",
                    error=f"任务提交失败：{type(exc).__name__}: {exc}"[:500],
                    account_id=job.get("rebind_source_account_id") or job.get("account_id"),
                    email=job.get("rebind_target_email"),
                    rebind_status="failed",
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                _queue_slots.release()
        # ``shutdown(wait=True)`` is the batch barrier: the serial dispatcher
        # will not start the next worker-count configuration until all current
        # jobs have finished and released their queue slots.
        pool.shutdown(wait=True, cancel_futures=False)
    except Exception as exc:
        # Pool construction itself can fail.  No worker owns these jobs in that
        # case, so every reservation and queue slot must be rolled back here.
        for job_id in job_ids:
            job = db.get_job(int(job_id)) or {}
            if str(job.get("status") or "") != "pending":
                continue
            db.release_rebind_email(
                job.get("rebind_target_source"),
                job.get("rebind_target_email"),
                reservation_id=job.get("rebind_reservation_id"),
                success=False,
                note="任务调度异常，邮箱已释放",
            )
            _mark_terminal(
                int(job_id),
                status="failed",
                error=f"任务调度失败：{type(exc).__name__}: {exc}"[:500],
                account_id=job.get("rebind_source_account_id") or job.get("account_id"),
                email=job.get("rebind_target_email"),
                rebind_status="failed",
                completed_at=datetime.now().isoformat(timespec="seconds"),
            )
            _queue_slots.release()
        logger.exception("换绑批次调度失败 job_ids=%s", job_ids)
    finally:
        with _executor_lock:
            if pool is not None and _executor is pool:
                _executor = None


def _append_log(
    job: dict,
    message: str,
    *,
    clear: bool = False,
    secrets: list[str] | tuple[str, ...] | set[str] = (),
) -> None:
    path = Path(str(job.get("log_file") or ""))
    if not str(path):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H:%M:%S")
        mode = "w" if clear else "a"
        with path.open(mode, encoding="utf-8") as handle:
            handle.write(f"{stamp} [INFO] [换绑] {_redact_text(message, secrets)}\n")
    except OSError:
        logger.exception("换绑日志写入失败 job_id=%s", job.get("id"))


def _mark_terminal(job_id: int, *, status: str, error: str | None = None, account_id: int | None = None, email: str | None = None, **extra: Any) -> None:
    fields: dict[str, Any] = {
        "status": status,
        "error": error,
        "account_id": account_id,
        "email": email,
    }
    fields.update(extra)
    db.update_job_fields(job_id, **fields)


def _run_one(job_id: int) -> None:
    job = db.get_job(job_id)
    if not job:
        _queue_slots.release()
        return
    secrets = _secret_values(job)
    external_verified = False
    with _state_lock:
        _active_jobs.add(int(job_id))
        _stop_events.setdefault(int(job_id), threading.Event())
    try:
        # Cancellation and this claim are both atomic storage transitions.
        # A task cancelled while waiting in the executor therefore exits here
        # without ever invoking the external driver.
        claimed = db.claim_rebind_job(job_id)
        if claimed is None:
            return
        job = claimed
        _append_log(job, f"开始换绑 source_account_id={job.get('rebind_source_account_id')} target={job.get('rebind_target_email')}")
        if _stop_events[int(job_id)].is_set():
            raise RebindExecutionError("用户手动停止")
        account = db.get_account(int(job.get("rebind_source_account_id") or job.get("account_id") or 0))
        if not account:
            raise RebindExecutionError("原账号不存在")
        secrets.extend(_secret_values(account))
        target = db.get_rebind_target(
            job.get("rebind_target_source"),
            job.get("rebind_target_pool_id"),
            job.get("rebind_target_email"),
            reservation_id=job.get("rebind_reservation_id"),
        )
        if not target:
            raise RebindExecutionError("换绑目标邮箱预留已失效或邮箱池记录不存在")
        secrets.extend(_secret_values(target))
        plan = _rebind_plan(
            driver=job.get("rebind_driver"),
            login_driver=job.get("rebind_login_driver"),
            action_driver=job.get("rebind_action_driver"),
            hybrid=job.get("rebind_hybrid_mode") if "rebind_hybrid_mode" in job else None,
            headless=job.get("rebind_headless"),
            login_headless=job.get("rebind_login_headless"),
        )
        selected_driver = str(plan["driver"])
        _append_log(
            job,
            "执行方式 "
            f"login_driver={plan['login_driver']} action_driver={plan['action_driver']} "
            f"hybrid={plan['hybrid']} headless={plan['headless']} "
            f"login_headless={plan['login_headless']}",
        )
        callback = get_rebind_executor()
        fn = callback or _default_executor
        result = _invoke_executor(
            fn,
            account,
            target,
            driver=selected_driver,
            login_driver=plan["login_driver"],
            action_driver=plan["action_driver"],
            hybrid=plan["hybrid"],
            headless=plan["headless"],
            login_headless=plan["login_headless"],
            proxy=job.get("rebind_proxy"),
            log=lambda line: _append_log(
                db.get_job(job_id) or job,
                str(line),
                secrets=secrets,
            ),
        )
        result = _validated_success_result(result, str(target.get("email") or ""))
        external_verified = True
        secrets.extend(_secret_values(result))
        replacement = db.finalize_rebind_account(
            int(account.get("id") or 0),
            target_email=str(target.get("email") or ""),
            target_source=str(target.get("source") or ""),
            target_pool_id=target.get("id"),
            reservation_id=job.get("rebind_reservation_id"),
            group_id=job.get("rebind_group_id"),
            group_name=job.get("rebind_group_name"),
            job_id=job_id,
            target_material=target,
            result=result,
        )
        new_id = int(replacement.get("id") or 0)
        db.update_job_fields(
            job_id,
            status="success",
            rebind_status="success",
            account_id=new_id,
            email=target.get("email"),
            completed_at=datetime.now().isoformat(timespec="seconds"),
            error=None,
        )
        _append_log({**job, "id": job_id}, f"换绑成功，新账号 id={new_id} email={target.get('email')}；原账号已清理")
    except Exception as exc:
        message = _redact_text(exc, secrets)[:500]
        current = db.get_job(job_id) or job
        stopped = _stop_events.get(int(job_id)) and _stop_events[int(job_id)].is_set()
        # Once the driver has verified the new mailbox remotely, the target is
        # consumed even if the local finalize step fails.  Releasing it would
        # let another task reuse an address already bound to this account.
        db.release_rebind_email(
            current.get("rebind_target_source"),
            current.get("rebind_target_email"),
            reservation_id=current.get("rebind_reservation_id"),
            success=external_verified,
            note=(
                f"远端换绑已确认，本地落库失败，需人工核对：{message}"
                if external_verified
                else f"换绑失败：{message}"
            ),
        )
        final_status = "stopped" if stopped else "failed"
        _mark_terminal(
            job_id,
            status=final_status,
            error="用户手动停止" if stopped else message,
            account_id=current.get("rebind_source_account_id") or current.get("account_id"),
            email=current.get("rebind_target_email"),
            rebind_status=final_status,
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
        _append_log(
            {**current, "id": job_id},
            f"换绑{('停止' if stopped else '失败')}：{message}",
            secrets=secrets,
        )
        logger.exception("换绑任务失败 job_id=%s", job_id)
    finally:
        with _state_lock:
            _active_jobs.discard(int(job_id))
            _stop_events.pop(int(job_id), None)
        _queue_slots.release()


def submit_rebind(
    account_ids: list[int] | tuple[int, ...],
    *,
    pool_sources: list[str] | tuple[str, ...],
    group_id: int | None = None,
    group_name: str | None = None,
    count: int | None = None,
    workers: int | None = None,
    driver: str | None = None,
    login_driver: str | None = None,
    action_driver: str | None = None,
    hybrid: bool | None = None,
    headless: bool | None = None,
    login_headless: bool | None = None,
    proxy: str | None = None,
) -> dict:
    """校验、预留邮箱并启动一批换绑任务。"""
    ids: list[int] = []
    seen: set[int] = set()
    for raw in account_ids or []:
        try:
            one = int(raw)
        except (TypeError, ValueError):
            continue
        if one > 0 and one not in seen:
            seen.add(one)
            ids.append(one)
    if not ids:
        raise ValueError("至少选择一个待换绑账号")
    sources: list[str] = []
    for raw in pool_sources or []:
        value = str(raw or "").strip().lower()
        if value not in REBIND_SOURCES:
            raise ValueError(f"换绑邮箱池无效：{value}")
        if value not in sources:
            sources.append(value)
    if not sources:
        raise ValueError("至少选择一个换绑邮箱池")
    requested = len(ids) if count is None else int(count)
    if requested < 1 or requested > len(ids):
        raise ValueError("换绑数量必须在 1 到已选择账号数之间")
    selected_ids = ids[:requested]
    worker_count = _workers(workers, requested)
    plan = _rebind_plan(
        driver=driver,
        login_driver=login_driver,
        action_driver=action_driver,
        hybrid=hybrid,
        headless=headless,
        login_headless=login_headless,
    )
    group = db.get_account_group(group_id=group_id, name=group_name)
    if not group:
        raise ValueError("请选择有效的换绑分组")
    accounts: list[dict] = []
    skipped: list[dict] = []
    for account_id in selected_ids:
        account = db.get_account(account_id)
        if not account:
            skipped.append({"id": account_id, "reason": "账号不存在"})
            continue
        if db.has_active_rebind_for_account(account_id):
            skipped.append({"id": account_id, "email": account.get("email"), "reason": "账号已有换绑任务"})
            continue
        accounts.append(account)
    if not accounts:
        raise ValueError("没有可提交的待换绑账号")
    if len(accounts) < requested:
        requested = len(accounts)
        if worker_count > requested:
            raise ValueError("可换绑账号数量少于并发线程数量")
        selected_ids = [int(item.get("id") or 0) for item in accounts]
    reservation_id = uuid.uuid4().hex
    targets = db.reserve_rebind_emails(sources, requested, reservation_id=reservation_id)
    if len(targets) != requested:
        db.release_rebind_reservation(reservation_id, note="可用邮箱不足，预留已回滚")
        available = sum(int(item.get("available") or 0) for item in db.rebind_email_pool_summary().values())
        raise ValueError(f"所选邮箱池可用邮箱不足：需要 {requested} 个，当前 {available} 个")

    jobs: list[dict] = []
    acquired_slots = 0
    try:
        for account, target in zip(accounts[:requested], targets):
            job = db.create_rebind_job(
                source_account_id=int(account.get("id") or 0),
                source_email=str(account.get("email") or ""),
                target=target,
                group=group,
                reservation_id=reservation_id,
                driver=str(plan["driver"]),
                login_driver=str(plan["login_driver"]),
                action_driver=str(plan["action_driver"]),
                hybrid=bool(plan["hybrid"]),
                headless=bool(plan["headless"]),
                login_headless=bool(plan["login_headless"]),
                proxy=proxy,
            )
            jobs.append(job)
        # Reserve every queue slot before handing the batch to the dispatcher.
        # Therefore an enqueue failure has no concurrently-running worker and
        # can roll back the complete reservation without racing a task.
        for _job in jobs:
            if not _queue_slots.acquire(blocking=False):
                raise ValueError("换绑任务队列已满，请稍后重试")
            acquired_slots += 1
        _dispatcher_pool().submit(
            _run_batch,
            [int(job["id"]) for job in jobs],
            worker_count,
        )
    except Exception:
        for _ in range(acquired_slots):
            _queue_slots.release()
        for job in jobs:
            job_id = int(job.get("id") or 0)
            db.release_rebind_email(
                job.get("rebind_target_source"), job.get("rebind_target_email"),
                reservation_id=reservation_id, success=False, note="任务提交异常，邮箱已释放",
            )
            db.update_job_fields(job_id, status="failed", rebind_status="failed", error="任务提交失败", completed_at=datetime.now().isoformat(timespec="seconds"))
        # A failure while creating jobs can leave reserved targets that do not
        # have a persisted task row yet.
        db.release_rebind_reservation(reservation_id, note="任务提交异常，邮箱已释放")
        raise
    return {
        "ok": True,
        "reservation_id": reservation_id,
        "submitted": len(jobs),
        "workers": worker_count,
        "driver": plan["driver"],
        "login_driver": plan["login_driver"],
        "action_driver": plan["action_driver"],
        "hybrid": plan["hybrid"],
        "headless": plan["headless"],
        "login_headless": plan["login_headless"],
        "jobs": jobs,
        "skipped": skipped,
        "group": group,
    }


def request_stop_rebind_job(job_id: int) -> dict:
    job = db.get_job(job_id)
    if not job or str(job.get("job_type") or "") != "rebind":
        return {"ok": False, "error": "换绑任务不存在", "status": 404}
    current = str(job.get("status") or "")
    if current == "pending":
        cancelled = db.cancel_pending_rebind_job(job_id)
        if cancelled is not None:
            return {"ok": True, "job_id": job_id, "state": "cancelled", "message": "换绑任务已取消"}
        # A worker claimed it between the first read and the atomic cancel.
        job = db.get_job(job_id)
        if job is None:
            return {"ok": False, "error": "换绑任务不存在", "status": 404}
        current = str(job.get("status") or "")
    if current in {"success", "failed", "stopped", "cancelled"}:
        return {"ok": True, "job_id": job_id, "state": current, "message": f"任务已结束：{current}"}
    if current not in {"running", "stopping"}:
        return {"ok": False, "error": f"当前状态不支持停止：{current}", "status": 409}
    with _state_lock:
        event = _stop_events.get(int(job_id))
        if event is not None:
            event.set()
    if event is None:
        # A running/stopping row without an in-process worker is a restart or
        # crashed-worker residue.  It is safe to release because no executor
        # can reach the external operation from this process.
        db.release_rebind_email(
            job.get("rebind_target_source"),
            job.get("rebind_target_email"),
            reservation_id=job.get("rebind_reservation_id"),
            success=False,
            note="任务实例不存在，邮箱已释放",
        )
        db.update_job_fields(
            job_id,
            status="stopped",
            rebind_status="stopped",
            error="用户手动停止（任务实例不存在）",
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
        return {"ok": True, "job_id": job_id, "state": "stopped", "message": "任务实例不存在，已直接停止"}
    db.update_job_fields(job_id, status="stopping", rebind_status="stopping", error="用户手动停止中")
    return {"ok": True, "job_id": job_id, "state": "stopping", "message": "已发送停止信号"}


def cancel_pending_rebind_jobs() -> int:
    """Cancel all pending rebind jobs and release their reserved mailboxes."""
    cancelled = 0
    for job in db.list_jobs(limit=5000):
        if str(job.get("job_type") or "") != "rebind" or str(job.get("status") or "") != "pending":
            continue
        result = request_stop_rebind_job(int(job.get("id") or 0))
        if result.get("state") == "cancelled":
            cancelled += 1
    return cancelled


def is_rebind_job(job_id: int) -> bool:
    job = db.get_job(job_id)
    return bool(job and str(job.get("job_type") or "") == "rebind")


def queue_settings() -> dict:
    with _executor_lock:
        workers = _executor_workers
    return {"workers": workers, "queue_limit": _QUEUE_LIMIT}


def shutdown_executor(wait: bool = True) -> None:
    global _executor, _dispatcher
    with _executor_lock:
        dispatcher = _dispatcher
        _dispatcher = None
        current = _executor
        _executor = None
    # The dispatcher owns the active batch and waits for its worker pool, so it
    # is sufficient to stop it first.  ``current`` is only a fallback for a
    # concurrent non-waiting shutdown.
    if dispatcher is not None:
        dispatcher.shutdown(wait=wait, cancel_futures=False)
    if current is not None and not wait:
        current.shutdown(wait=False, cancel_futures=False)
