# -*- coding: utf-8 -*-
"""
Flask 本地控制台。

复用现有后端：
    core.db                     —— 账号 / 邮箱池 / 任务的文件持久化与查询
    core.registration_service   —— 线程池批量注册 + 任务日志
    webui.config_editor         —— 安全读写 config/*.py

所有接口返回 JSON；前端是单文件 templates/index.html（原生 JS + fetch）。
默认绑定 127.0.0.1，仅本地访问。
"""
import logging
import json
import re
import threading
import time
import uuid
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, render_template, request

from core import codex_retry_service, db, plan_check_service, extract_link_service, extract_link_registry, codex_agent_service, live_check_service, twofa_setup_service, rebind_service
from webui.auth import init_auth, register_auth_routes
from core import registration_service as svc
from webui import config_editor

logger = logging.getLogger(__name__)

# 中文注释：邮箱池导入格式检查统一放在后端，确保前端绕过时也不会写入脏数据。
_IMPORT_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_IMPORT_URL_RE = re.compile(r"^(?:https?://|data:)[^\s]+$", re.IGNORECASE)


def _parse_email_import_text(text: str, source: str) -> dict:
    """解析并校验邮箱素材，返回统计信息、有效记录及逐行错误。"""
    source = str(source or "").strip().lower()
    expected = 4 if source == "outlook" else 2
    input_count = 0
    invalid = []
    records = []
    for line_no, raw_line in enumerate(str(text or "").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        input_count += 1
        delimiter = "----" if "----" in line else "====" if "====" in line else ""
        parts = [part.strip() for part in line.split(delimiter)] if delimiter else [line]
        errors = []
        if len(parts) < expected:
            errors.append(f"字段不足（需要至少 {expected} 段）")
        email = parts[0] if parts else ""
        if not _IMPORT_EMAIL_RE.fullmatch(email):
            errors.append("邮箱格式有误")
        if delimiter and any(not part for part in parts[:expected]):
            errors.append("必填字段不能为空")
        if source in ("generic_api", "icloud", "cloudflare_domain") and len(parts) >= 2 and not _IMPORT_URL_RE.fullmatch(parts[1]):
            errors.append("取码地址需为 http(s) 或 data 地址")
        if errors:
            invalid.append({"line": line_no, "text": line, "email": email, "errors": errors})
            continue
        if source in ("generic_api", "icloud", "cloudflare_domain"):
            records.append({
                "email": email,
                "code_url": parts[1],
                "access_token": parts[2] if len(parts) > 2 else "",
                "totp_secret": parts[3] if len(parts) > 3 else "",
            })
        else:
            records.append({
                "email": email,
                "password": parts[1],
                "client_id": parts[2],
                "refresh_token": parts[3],
                "access_token": parts[4] if len(parts) > 4 else "",
                "totp_secret": parts[5] if len(parts) > 5 else "",
            })
    return {
        "input_count": input_count,
        "valid_count": len(records),
        "invalid_count": len(invalid),
        "invalid": invalid,
        "records": records,
    }

def _pool_source_arg(default: str = "outlook") -> str:
    src = (request.args.get("source") or "").strip()
    if not src and request.method == "POST":
        data = request.get_json(silent=True) or {}
        src = (data.get("source") or data.get("type") or "").strip()
    return src if src in ("all", "outlook", "generic_api", "icloud", "cloudflare_domain") else default


def _with_pool_source(rows: list[dict], source: str) -> list[dict]:
    out = []
    for r in rows:
        x = dict(r)
        x["source"] = source
        if not x.get("copy_line"):
            x["copy_line"] = x.get("email") or ""
        out.append(x)
    return out


def _account_registration_password(row: dict) -> str:
    """读取注册阶段创建的账号密码；兼容早期仅写入 extra_json 的记录。"""
    value = str(row.get("registration_password") or "").strip()
    if value:
        return value
    try:
        import json
        extra = json.loads(str(row.get("extra_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    return str((extra or {}).get("registration_password") or "").strip()


def _account_saved_session(row: dict) -> dict:
    """读取注册/查活保存的完整 ChatGPT Session。"""
    try:
        extra = json.loads(str(row.get("extra_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    session = extra.get("session") if isinstance(extra, dict) else None
    return session if isinstance(session, dict) else {}


def _account_registration_method(row: dict) -> str:
    """返回账号注册方式；兼容改造前没有 registration_method 的历史记录。"""
    raw = str(row.get("registration_method") or row.get("registration_driver") or "").strip().lower()
    aliases = {
        "api": "protocol", "http": "protocol",
        "roxybrowser": "roxy", "fingerprint": "roxy", "browser": "roxy",
        "cloakbrowser": "cloak",
        "browseruse": "browser_use", "browser-use": "browser_use", "bu": "browser_use",
        "sv": "skyvern",
    }
    if raw:
        return aliases.get(raw, raw)
    try:
        extra = json.loads(str(row.get("extra_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        extra = {}
    if isinstance(extra, dict):
        for key, method in (
            ("roxybrowser", "roxy"),
            ("cloakbrowser", "cloak"),
            ("skyvern", "skyvern"),
            ("browser_use", "browser_use"),
        ):
            if key in extra:
                return method
    return "protocol"




def _matches_query(row: dict, q: str | None) -> bool:
    q = str(q or "").strip().lower()
    if not q:
        return True
    try:
        return q in "\n".join(str(v) for v in row.values()).lower()
    except Exception:
        return False


def _paginate_items(items: list[dict], *, page: int, page_size: int) -> dict:
    page = max(1, int(page or 1))
    page_size = max(1, min(500, int(page_size or 50)))
    total = len(items)
    offset = (page - 1) * page_size
    return {
        "ok": True,
        "items": items[offset:offset + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "offset": offset,
        "limit": page_size,
    }


def _compact_account_for_list(row: dict) -> dict:
    """账号列表轻量对象：只返回当前表格渲染和按钮判断必需字段。

    原则：
    - 不返回完整 Token / Token 预览 / TOTP Secret / Agent Token。
    - 时间戳、错误原因、提链详情等只在前端确实要展示时返回；空值不返回。
    - 复制/下载敏感内容时再通过 /secret 接口按需读取。
    """
    out = {
        "id": row.get("id"),
        "email": row.get("email"),
        "registration_method": _account_registration_method(row),
        "group_name": row.get("group_name") or db.DEFAULT_ACCOUNT_GROUP,
        "has_access_token": bool(str(row.get("access_token") or "").strip()),
        "has_session": bool(_account_saved_session(row)),
        "totp_enabled": bool(row.get("totp_secret")),
        "twofa_status": row.get("twofa_status"),
        "twofa_requested": bool(row.get("twofa_requested")),
        "password_available": bool(_account_registration_password(row)),
        "codex_agent_has_token": bool(str(row.get("codex_agent_token") or "").strip()),
    }

    # 这些是列表固定列直接展示字段。
    for key in (
        "user_name", "email_source", "note", "archived", "created_at",
        "link_completed", "payment_completed", "sms_completed", "proxy_country_code",
        "proxy_country_name", "proxy_region", "proxy_city", "proxy_exit_ip",
        "plan_type", "current_plan_type", "plus_trial_eligible",
        "oaics_eligible", "oaics_check_status",
        "plan_check_status", "codex_status", "codex_agent_status",
        "twofa_status", "twofa_requested",
    ):
        if key in row:
            out[key] = row.get(key)

    if row.get("plan_check_status") in ("queued", "running") or row.get("plan_check_ok") is False:
        out["plan_check_ok"] = row.get("plan_check_ok")

    # 下面字段仅在有值时返回，避免每行堆满 null/空字符串/内部状态。
    optional_keys = (
        # 套餐悬浮详情：完整订阅状态、计费周期、有效期、续费及折扣信息。
        "plan_check_error", "plan_checked_at", "plan_last_success_at",
        "plan_check_network_route", "plan_check_proxy_used", "plan_check_proxy_fallback_reason",
        "oaics_check_error", "oaics_checked_at", "oaics_session_kind", "oaics_processor_entity",
        "oaics_country_results", "oaics_query_count",
        "subscription_plan", "has_active_subscription", "is_delinquent",
        "plan_expires_at", "plan_renews_at", "renews_at", "plan_cancels_at",
        "billing_period", "billing_currency", "last_purchase_origin_platform", "last_will_renew",
        "discount_amount", "discount_type", "discount_duration_num_periods",
        "discount_expires_at", "discount_cancellation_policy", "discount_promo_campaign_id",
        "token_expired", "token_expires_at",
        "twofa_error", "twofa_trigger", "twofa_queued_at", "twofa_started_at", "twofa_completed_at",
        # 查活状态。
        "live_check_status", "live_check_error", "live_checked_at",
        "icloud_code_url_available",
        # 提链成功/失败时才需要。
        "extract_link_status", "extract_link_type", "extract_link_message", "extract_link_error",
        "extract_link_long_url", "extract_link_copy_paste", "extract_link_image_url_png",
        "extract_link_image_url_svg", "extract_link_expires_at", "extract_link_progress",
        "extract_link_service_id", "extract_link_service_name", "extract_link_mode",
        "extract_link_paypal_authorize_url", "extract_link_hosted_checkout_url",
        # Codex / Agent 状态提示。
        "codex_error", "codex_agent_message", "codex_agent_runtime_id",
        "codex_agent_sub2api_url", "codex_agent_sub2api_mode", "codex_agent_sub2api_total",
    )
    for key in optional_keys:
        value = row.get(key)
        if value is not None and value != "":
            out[key] = value
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
    if any(x in plan for x in ("plus", "pro", "team", "go")):
        expire = row.get("expires_at")
        if expire:
            out["expires_at"] = expire
    return out


def _account_secret_value(row: dict, field: str) -> str:
    field = (field or "").strip()
    if field == "access_token":
        return str(row.get("access_token") or "")
    if field == "session":
        session = _account_saved_session(row)
        return json.dumps(session, ensure_ascii=False, separators=(",", ":")) if session else ""
    if field == "copy_line":
        return str(row.get("copy_line") or "")
    if field == "email":
        return str(row.get("email") or "").strip()
    if field in {"password", "registration_password"}:
        return _account_registration_password(row)
    if field == "totp":
        return db._totp_viewer_url(str(row.get("totp_secret") or "").strip())
    if field == "url":
        email = str(row.get("email") or "").strip()
        source = str(row.get("email_source") or "").strip().lower()
        lookups = {
            "icloud": db.get_icloud_email_by_email,
            "generic_api": db.get_generic_api_email_by_email,
            "cloudflare_domain": db.get_domain_email_by_email,
        }
        lookup = lookups.get(source)
        source_row = lookup(email) if lookup else None
        if not source_row:
            # 历史账号的 source 字段可能为空，按邮箱池逐一匹配补全 URL。
            for candidate in (db.get_icloud_email_by_email, db.get_generic_api_email_by_email, db.get_domain_email_by_email):
                source_row = candidate(email)
                if source_row:
                    break
        return str((source_row or {}).get("code_url") or (source_row or {}).get("url") or "").strip()
    if field == "codex_agent_token":
        return str(row.get("codex_agent_token") or "")
    if field == "icloud_code_url":
        if str(row.get("email_source") or "").strip().lower() != "icloud":
            return ""
        source_row = db.get_icloud_email_by_email(str(row.get("email") or ""))
        return str((source_row or {}).get("code_url") or "").strip()
    if field == "totp_code":
        secret = str(row.get("totp_secret") or "").strip()
        if not secret:
            return ""
        import pyotp
        return pyotp.TOTP(secret).now()
    raise ValueError("field 仅支持 email/password/totp/url/access_token/session/copy_line/codex_agent_token/totp_code/icloud_code_url")


def _rebind_response_secrets(row: dict | None) -> list[str]:
    """Load exact worker-only values used to scrub rebind errors and logs."""
    if not isinstance(row, dict) or str(row.get("job_type") or "").strip().lower() != "rebind":
        return []
    records: list[dict] = [row]
    try:
        account_id = row.get("rebind_source_account_id") or row.get("account_id")
        if account_id is not None:
            account = db.get_account(int(account_id))
            if account:
                records.append(account)
    except (TypeError, ValueError):
        pass
    source = str(row.get("rebind_target_source") or "").strip().lower()
    email = str(row.get("rebind_target_email") or row.get("email") or "").strip()
    lookup = {
        "outlook": db.get_outlook_by_email,
        "generic_api": db.get_generic_api_email_by_email,
        "icloud": db.get_icloud_email_by_email,
        "cloudflare_domain": db.get_domain_email_by_email,
    }.get(source)
    if lookup is not None and email:
        try:
            pool_row = lookup(email)
        except Exception:
            pool_row = None
        if pool_row:
            records.append(pool_row)
    keys = {
        "password", "client_id", "clientId", "refresh_token", "refreshToken",
        "access_token", "token", "code_url", "url", "reservation_id",
        "rebind_reservation_id", "rebind_proxy", "log_file", "original_email_line",
    }
    values: list[str] = []
    for record in records:
        for key in keys:
            value = record.get(key)
            if value not in (None, ""):
                values.append(str(value))
    return values


def _redact_rebind_response_text(row: dict | None, value: object) -> str:
    return rebind_service.redact_rebind_text(value, _rebind_response_secrets(row))


def _compact_job_for_list(row: dict) -> dict:
    """注册任务列表轻量对象：只返回表格展示和按钮判断需要的字段。"""
    out = {
        "id": row.get("id"),
        "status": row.get("status"),
    }
    for key in (
        "parent_job_id", "retry_attempt", "batch_id", "email", "started_at", "completed_at",
        "display_status", "retryable", "retry_action", "retry_label",
        "manual_otp_required", "job_type", "rebind_status", "rebind_source_account_id",
        "rebind_source_email", "rebind_target_email", "rebind_target_source",
        "rebind_target_pool_id", "rebind_group_id", "rebind_group_name",
        "rebind_driver", "rebind_login_driver", "rebind_action_driver",
        "rebind_hybrid_mode", "rebind_headless", "rebind_login_headless",
    ):
        value = row.get(key)
        # Rebind execution switches are meaningful when explicitly false;
        # omitting them from the compact/paged task response made a protocol
        # submission look like it had no hybrid/headless setting at all.
        keep_false = key in {"rebind_hybrid_mode", "rebind_headless", "rebind_login_headless"}
        if value is not None and value != "" and (value is not False or keep_false):
            out[key] = value
    err = str(row.get("error_message") or "").strip()
    if err:
        if str(row.get("job_type") or "").strip().lower() == "rebind":
            err = _redact_rebind_response_text(row, err)
        # 列表只需要摘要；完整错误和堆栈看“任务日志”。
        out["error_message"] = err[:240] + ("…" if len(err) > 240 else "")
    return out


def _public_rebind_job(row: dict | None) -> dict:
    """Return the UI-safe subset of a rebind job.

    Mailbox passwords, refresh material, code URLs, reservation IDs and local
    log paths are worker-only fields.  Keep this whitelist in one place so the
    registration task list, task log endpoint and submit response share the
    same redaction policy.
    """
    if not isinstance(row, dict):
        return {}
    allowed = {
        "id", "job_uuid", "job_type", "parent_job_id", "root_job_id", "retry_attempt",
        "retry_action", "email_source", "email", "status", "error_message",
        "started_at", "completed_at", "created_at", "account_id", "batch_id",
        "display_status", "retryable", "retry_label", "retry_reason",
        "successful_retry_job_id", "manual_otp_required",
        "rebind_status", "rebind_source_account_id", "rebind_source_email",
        "rebind_target_email", "rebind_target_source", "rebind_target_pool_id",
        "rebind_group_id", "rebind_group_name", "rebind_driver",
        "rebind_login_driver", "rebind_action_driver", "rebind_hybrid_mode",
        "rebind_headless", "rebind_login_headless",
    }
    public = {key: row[key] for key in allowed if key in row}
    if public.get("error_message") not in (None, ""):
        public["error_message"] = _redact_rebind_response_text(row, public["error_message"])
    return public


def _public_job_for_response(row: dict | None) -> dict:
    """Redact specialized task rows before returning them from WebUI APIs."""
    if not isinstance(row, dict):
        return {}
    if str(row.get("job_type") or "").strip().lower() == "rebind":
        return _public_rebind_job(row)
    return dict(row)


def _job_status_counts(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    counts["active"] = sum(int(counts.get(s, 0) or 0) for s in ("pending", "running", "stopping"))
    return counts


def _registration_batch_for_jobs(jobs: list[dict]) -> dict | None:
    """从本次提交结果定位批次，兼容旧服务或测试桩没有 batch_id 的情况。"""
    if not jobs or jobs[0].get("batch_id") is None:
        return None
    try:
        return db.get_registration_batch(int(jobs[0]["batch_id"]))
    except (TypeError, ValueError):
        return None

def create_app(auth_code: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    _prepared_downloads: dict[str, dict] = {}
    _job_retention_lock = threading.Lock()
    _job_retention_scheduled = False

    def _schedule_job_retention_once() -> None:
        """首次打开任务列表时异步清理历史记录，避免轮询重复触发。"""
        nonlocal _job_retention_scheduled
        with _job_retention_lock:
            if _job_retention_scheduled:
                return
            _job_retention_scheduled = True
        svc.schedule_registration_job_retention()

    def _put_prepared_download(content: bytes, filename: str, mimetype: str = "application/zip") -> str:
        now = time.time()
        # 顺手清理 10 分钟前的临时下载，避免内存堆积。
        for k, v in list(_prepared_downloads.items()):
            if now - float(v.get("created_at") or 0) > 600:
                _prepared_downloads.pop(k, None)
        download_id = uuid.uuid4().hex
        _prepared_downloads[download_id] = {
            "content": bytes(content),
            "filename": filename,
            "mimetype": mimetype,
            "created_at": now,
        }
        return download_id

    @app.get("/api/downloads/<download_id>")
    def api_prepared_download(download_id: str):
        item = _prepared_downloads.pop(str(download_id or ""), None)
        if not item:
            return jsonify({"ok": False, "error": "下载已过期或不存在，请重新生成"}), 404
        content = item.get("content") or b""
        filename = item.get("filename") or "download.zip"
        mimetype = item.get("mimetype") or "application/octet-stream"
        return Response(
            content,
            mimetype=mimetype,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(content)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    init_auth(app, auth_code=auth_code)
    register_auth_routes(app)
    recovered_plan_checks = db.recover_interrupted_plan_checks()
    if recovered_plan_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的套餐查询状态", recovered_plan_checks)
    recovered_extract_links = db.recover_interrupted_extract_links()
    if recovered_extract_links:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的提链状态", recovered_extract_links)
    recovered_live_checks = db.recover_interrupted_live_checks()
    if recovered_live_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的查活状态", recovered_live_checks)
    recovered_twofa_setups = db.recover_interrupted_twofa_setups()
    if recovered_twofa_setups:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的 2FA 重设状态", recovered_twofa_setups)
    recovered_codex_agents = db.recover_interrupted_codex_agents()
    if recovered_codex_agents:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的 Codex Agent Token 状态", recovered_codex_agents)

    # ----------------------------------------------------------
    # 页面
    # ----------------------------------------------------------
    @app.get("/")
    def index():
        return render_template("index.html")

    # ----------------------------------------------------------
    # 统计概览
    # ----------------------------------------------------------
    @app.get("/api/summary")
    def api_summary():
        pool = {"total": 0, "available": 0, "used": 0, "failed": 0}
        pool_by_source = {
            "outlook": db.outlook_pool_summary(),
            "generic_api": db.generic_api_email_pool_summary(),
            "icloud": db.icloud_email_pool_summary(),
            "cloudflare_domain": db.domain_email_pool_summary(),
        }
        # 概览展示全部本地邮箱素材，不受本批注册来源配置影响。
        for one in pool_by_source.values():
            for k in pool:
                pool[k] += int(one.get(k, 0) or 0)
        domain_pool = pool_by_source["cloudflare_domain"]
        return jsonify({
            "accounts": db.count_accounts(),
            "outlook_total": pool.get("total", 0),
            "outlook_available": pool.get("available", 0),
            "outlook_used": pool.get("used", 0),
            "outlook_failed": pool.get("failed", 0),
            "domain_total": domain_pool.get("total", 0),
            "domain_available": domain_pool.get("available", 0),
            "domain_used": domain_pool.get("used", 0),
            "domain_failed": domain_pool.get("failed", 0),
            "pool_by_source": pool_by_source,
        })

    @app.get("/api/registration-drivers/status")
    def api_registration_drivers_status():
        """返回五种注册方式的依赖和必填配置就绪状态。"""
        from core.registration_driver_health import all_registration_driver_preflights

        return jsonify({"ok": True, "items": all_registration_driver_preflights()})

    # ----------------------------------------------------------
    # 已注册账号
    # ----------------------------------------------------------
    @app.get("/api/account-groups")
    def api_account_groups():
        return jsonify({"ok": True, "groups": db.list_account_groups()})

    @app.post("/api/account-groups")
    def api_account_group_create():
        data = request.get_json(silent=True) or {}
        try:
            group = db.create_account_group(data.get("name"))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "group": group}), 201

    @app.patch("/api/account-groups/<int:group_id>")
    def api_account_group_rename(group_id: int):
        data = request.get_json(silent=True) or {}
        try:
            group = db.rename_account_group(group_id, data.get("name"))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "group": group})

    @app.delete("/api/account-groups/<int:group_id>")
    def api_account_group_delete(group_id: int):
        try:
            deleted = db.delete_account_group(group_id)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/accounts/group-move")
    def api_accounts_group_move():
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        try:
            group_id = int(data.get("group_id"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "group_id 必须是有效分组 ID"}), 400
        try:
            updated, skipped = db.move_accounts_to_group(ids, group_id)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "updated": updated, "updated_count": len(updated), "skipped": skipped})

    @app.get("/api/rebind/pools")
    @app.get("/api/email-pools/rebind-summary")
    def api_rebind_pool_summary():
        """Return available target mailbox counts for the rebind dialog."""
        try:
            pools = db.rebind_email_pool_summary()
        except Exception as exc:
            logger.exception("读取换绑邮箱池失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
        try:
            from config import live_check as _live_cfg

            login_driver = str(getattr(_live_cfg, "REBIND_LOGIN_DRIVER", "cloak") or "cloak")
            action_driver = str(getattr(_live_cfg, "REBIND_ACTION_DRIVER", "protocol") or "protocol")
            hybrid = rebind_service.coerce_rebind_bool(
                getattr(_live_cfg, "REBIND_HYBRID_MODE", True), True
            )
            headless = rebind_service.coerce_rebind_bool(
                getattr(_live_cfg, "LIVE_CHECK_HEADLESS", False), False
            )
        except Exception:
            login_driver, action_driver, hybrid, headless = "cloak", "protocol", True, False
        return jsonify({
            "ok": True,
            "pools": pools,
            "pool_by_source": pools,
            "driver": action_driver,
            "login_driver": login_driver,
            "action_driver": action_driver,
            "hybrid": hybrid,
            "headless": headless,
            "queue": rebind_service.queue_settings(),
        })

    @app.post("/api/accounts/rebind")
    def api_accounts_rebind():
        """Start account rebind tasks.

        Body: ``account_ids``, ``pool_sources``, ``group_id`` (or ``group_name``),
        optional ``count``, ``workers``, ``driver``, ``headless`` and ``proxy``.
        """
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        sources = data.get("pool_sources") or data.get("email_pools") or data.get("email_sources") or data.get("sources") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if not isinstance(sources, list) or not sources:
            return jsonify({"ok": False, "error": "至少选择一个换绑邮箱池"}), 400
        try:
            raw_group_id = data.get("group_id") if data.get("group_id") is not None else data.get("target_group_id")
            group_id = int(raw_group_id) if raw_group_id is not None else None
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "group_id 必须是有效分组 ID"}), 400
        try:
            count = int(data["count"]) if data.get("count") is not None else None
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "count 必须是整数"}), 400
        try:
            workers = int(data["workers"]) if data.get("workers") is not None else None
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是整数"}), 400
        try:
            result = rebind_service.submit_rebind(
                ids,
                pool_sources=sources,
                group_id=group_id,
                group_name=data.get("group_name") or data.get("target_group"),
                count=count,
                workers=workers,
                driver=data.get("driver") or data.get("rebind_driver"),
                login_driver=data.get("login_driver") or data.get("rebind_login_driver"),
                action_driver=data.get("action_driver") or data.get("rebind_action_driver"),
                hybrid=(
                    data.get("hybrid")
                    if "hybrid" in data
                    else data.get("rebind_hybrid_mode")
                ),
                headless=data.get("headless") if "headless" in data else data.get("rebind_headless"),
                login_headless=(
                    data.get("login_headless")
                    if "login_headless" in data
                    else data.get("rebind_login_headless")
                ),
                proxy=data.get("proxy"),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            logger.exception("提交换绑任务失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
        # Reservation IDs are internal coordination tokens; clients only need
        # the submitted count and public task metadata.
        result.pop("reservation_id", None)
        result["jobs"] = [_public_rebind_job(job) for job in result.get("jobs") or []]
        return jsonify(result), 202

    @app.get("/api/accounts")
    def api_accounts():
        limit = request.args.get("limit", default=500, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        plan_filter = str(request.args.get("plan", default="") or "").lower()
        status_filter = str(request.args.get("status", default="") or "").lower()
        group_filter = str(request.args.get("group", default="") or "").strip()
        created_from = str(request.args.get("created_from", default="") or "").strip()[:10]
        created_to = str(request.args.get("created_to", default="") or "").strip()[:10]
        if status_filter not in {"", "all", "link", "sms"}:
            return jsonify({"ok": False, "error": "status 仅支持 all / link / sms"}), 400
        q = str(request.args.get("q", default="") or "").strip()
        # 新分页接口：传 page/page_size 或 paged=1 时返回 {items,total,page,page_size,...}
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            result = db.list_accounts_page(
                limit=page_size,
                offset=offset,
                archived=archived,
                plan_filter=plan_filter,
                q=q,
                status_filter=status_filter,
                group_filter=group_filter,
                created_from=created_from,
                created_to=created_to,
            )
            result["items"] = [_compact_account_for_list(r) for r in (result.get("items") or [])]
            result.update({"ok": True, "page": page, "page_size": page_size, "compact": True})
            return jsonify(result)
        return jsonify(db.list_accounts(
            limit=limit,
            archived=archived,
            plan_filter=plan_filter,
            q=q,
            status_filter=status_filter,
            group_filter=group_filter,
        ))

    @app.get("/api/accounts/plan-check-status")
    def api_account_plan_check_status():
        """套餐查询轻量状态，不返回 Token、邮箱密码等敏感字段。"""
        limit = request.args.get("limit", default=5000, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        plan_filter = str(request.args.get("plan", default="") or "").lower()
        status_filter = str(request.args.get("status", default="") or "").lower()
        group_filter = str(request.args.get("group", default="") or "").strip()
        created_from = str(request.args.get("created_from", default="") or "").strip()[:10]
        created_to = str(request.args.get("created_to", default="") or "").strip()[:10]
        if status_filter not in {"", "all", "link", "sms"}:
            return jsonify({"ok": False, "error": "status 仅支持 all / link / sms"}), 400
        q = str(request.args.get("q", default="") or "").strip()
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            snapshot = db.list_account_plan_check_statuses(
                limit=page_size,
                offset=offset,
                archived=archived,
                plan_filter=plan_filter,
                q=q,
                status_filter=status_filter,
                group_filter=group_filter,
                created_from=created_from,
                created_to=created_to,
            )
            snapshot.update({"page": page, "page_size": page_size})
        else:
            snapshot = db.list_account_plan_check_statuses(
                limit=max(1, min(5000, limit)),
                archived=archived,
                plan_filter=plan_filter,
                q=q,
                status_filter=status_filter,
                group_filter=group_filter,
            )
        snapshot["queue"] = plan_check_service.queue_settings()
        snapshot["twofa_queue"] = twofa_setup_service.queue_settings()
        return jsonify(snapshot)


    @app.get("/api/accounts/<int:acc_id>/secret")
    def api_account_secret(acc_id: int):
        """按需读取单账号敏感值，避免账号列表一次性下发完整 Token/整行。"""
        field = str(request.args.get("field") or "").strip()
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            value = _account_secret_value(acc, field)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "id": acc_id, "field": field, "value": value})

    @app.post("/api/accounts/secret-bulk")
    def api_accounts_secret_bulk():
        """按需批量读取账号敏感值。Body {account_ids:[...], field}."""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        field = str(data.get("field") or "").strip()
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多读取 5000 个账号"}), 400
        values = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            try:
                value = _account_secret_value(acc, field)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            if value:
                values.append({"id": acc_id, "email": acc.get("email"), "value": value})
            else:
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "值为空"})
        return jsonify({"ok": True, "field": field, "values": values, "count": len(values), "skipped": skipped})

    @app.post("/api/accounts/inject-session-bulk")
    def api_accounts_inject_session_bulk():
        """批量启动本地随机指纹浏览器并植入已保存的 ChatGPT 登录态。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 100:
            return jsonify({"ok": False, "error": "单次最多植入 100 个账号"}), 400
        try:
            workers = max(1, min(8, int(data.get("workers") or 3)))
        except (TypeError, ValueError):
            workers = 3
        from core.session_injector import inject_sessions
        result = inject_sessions(ids, max_workers=workers)
        return jsonify({
            "ok": True,
            "message": f"成功植入 {len(result.get('success') or [])} 个，失败 {len(result.get('failed') or [])} 个",
            **result,
        })

    @app.post("/api/accounts/inject-session-close")
    def api_accounts_inject_session_close():
        """关闭植入登录态功能保持打开的浏览器窗口。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids")
        from core.session_injector import close_injected_sessions
        closed = close_injected_sessions(ids if isinstance(ids, list) else None)
        return jsonify({"ok": True, "closed": closed})

    @app.post("/api/accounts/export-txt")
    def api_accounts_export_txt():
        """按用户勾选字段生成账号 TXT，每个账号一行。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        fields = data.get("fields") or []
        allowed = ("email", "password", "totp", "url", "access_token")
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if not isinstance(fields, list):
            return jsonify({"ok": False, "error": "fields 必须是数组"}), 400
        # 固定字段顺序，防止不同客户端提交顺序导致同一导出格式漂移。
        selected = [field for field in allowed if field in {str(x).strip() for x in fields}]
        if not selected:
            return jsonify({"ok": False, "error": "至少选择一个导出字段"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多导出 5000 个账号"}), 400

        rows = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            values = []
            for field in selected:
                values.append(_account_secret_value(acc, field))
            rows.append({
                "id": acc_id,
                "email": str(acc.get("email") or ""),
                "values": values,
                "line": "----".join(values),
            })
        return jsonify({
            "ok": True,
            "fields": selected,
            "rows": rows,
            "lines": [row["line"] for row in rows],
            "count": len(rows),
            "skipped": skipped,
        })

    @app.post("/api/accounts/<int:acc_id>/archive")
    def api_account_archive(acc_id: int):
        """归档/取消归档一个账号。Body {archived: true|false}。"""
        data = request.get_json(silent=True) or {}
        archived = bool(data.get("archived", True))
        updated = db.archive_account(acc_id=acc_id, archived=archived)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "archived": archived})

    @app.post("/api/accounts/archive-bulk")
    def api_accounts_archive_bulk():
        """批量归档/取消归档账号。Body {account_ids:[...], archived:true|false}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        archived = bool(data.get("archived", True))
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多归档 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.archive_accounts(account_ids=account_ids, archived=archived)
        skipped.extend(db_skipped)
        return jsonify({"ok": True, "updated": updated, "updated_count": len(updated), "archived": archived, "skipped": skipped})

    @app.post("/api/accounts/<int:acc_id>/delete")
    def api_account_delete(acc_id: int):
        """删除一个已注册账号记录。只删除本地保存的账号/token记录，不改邮箱池状态。"""
        deleted = db.delete_account(acc_id=acc_id)
        if not deleted:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "deleted": True})

    @app.post("/api/accounts/delete-bulk")
    def api_accounts_delete_bulk():
        """批量删除已注册账号记录。Body {account_ids: [...]} 或 {ids: [...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        deleted, db_skipped = db.delete_accounts(account_ids=account_ids)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    @app.post("/api/accounts/delete-deactivated")
    def api_accounts_delete_deactivated():
        """清理全部已标记为废号的本地账号/token 记录。"""
        deleted = db.delete_deactivated_accounts()
        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
        })

    @app.post("/api/accounts/<int:acc_id>/note")
    def api_account_note(acc_id: int):
        """更新单个已注册账号备注。Body {note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        note = str(data.get("note") or "")
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400
        updated = db.update_account_note(acc_id=acc_id, note=note)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "note": note})

    @app.post("/api/accounts/<int:acc_id>/completion-status")
    def api_account_completion_status(acc_id: int):
        """人工切换支付/接码完成状态。Body {status: payment|sms, enabled: bool}。"""
        data = request.get_json(silent=True) or {}
        status_name = str(data.get("status") or "").strip().lower()
        if status_name not in {"payment", "sms"}:
            return jsonify({"ok": False, "error": "status 必须是 payment 或 sms"}), 400
        if not isinstance(data.get("enabled"), bool):
            return jsonify({"ok": False, "error": "enabled 必须是布尔值"}), 400
        updated = db.update_account_completion_status(
            acc_id=acc_id,
            status_name=status_name,
            enabled=data["enabled"],
        )
        if updated is None:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": updated})

    @app.post("/api/accounts/note-bulk")
    def api_accounts_note_bulk():
        """批量更新已注册账号备注。Body {account_ids: [...], note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        note = str(data.get("note") or "")
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多备注 5000 个账号"}), 400
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400

        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.update_accounts_note(account_ids=account_ids, note=note)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.post("/api/accounts/check-live-bulk")
    def api_accounts_check_live_bulk():
        """批量查活：加入后台队列；协议 BrowserSession 指纹环境重新登录并刷新最新 AT。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查活 500 个账号"}), 400

        account_ids: list[int] = []
        skipped: list[dict] = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)

        accounts = []
        for acc_id in account_ids:
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = str(acc.get("email") or "").strip()
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            accounts.append(acc)

        started = []
        busy_count = 0
        failed = []
        for acc in accounts:
            acc_id = int(acc.get("id") or 0)
            email = str(acc.get("email") or "")
            queued = live_check_service.enqueue_account_live_check(
                account_id=acc_id,
                email=email,
                trigger="manual",
                # 查活按“查套餐”同一套网络选路：
                # PLAN_CHECK_PROXY_MODE / PLAN_CHECK_PROXY / PROXY_POOL。
                # 不复用账号注册时的 proxy_used，避免旧注册出口被 CF 403 后一直失败。
                proxy=None,
            )
            if queued.get("accepted"):
                started.append({"id": acc_id, "email": email, "status": "queued"})
            elif queued.get("busy"):
                busy_count += 1
                skipped.append({"id": acc_id, "email": email, "reason": queued.get("error") or "正在查活"})
            else:
                failed.append({"id": acc_id, "email": email, "error": queued.get("error") or "入队失败"})

        return jsonify({
            "ok": True,
            "message": f"已入队 {len(started)} 个查活任务",
            "started": started,
            "started_count": len(started),
            "busy_count": busy_count,
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "queue": live_check_service.queue_settings(),
        }), 202


    @app.post("/api/accounts/check-plan")
    def api_account_check_plan():
        """把单账号套餐查询加入后台队列。Body {account_id|email, proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        email = (data.get("email") or "").strip()
        acc = None
        if acc_id is not None:
            try:
                acc = db.get_account(int(acc_id))
            except Exception:
                acc = None
        if acc is None and email:
            acc = db.get_account_by_email(email)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        account_id = int(acc.get("id"))
        queued = plan_check_service.enqueue_account_plan_check(
            account_id=account_id,
            email=acc.get("email") or "",
            access_token=token,
            trigger="manual",
            proxy=data.get("proxy") if "proxy" in data else None,
            timezone_offset_min=str(data.get("timezone_offset_min") or "-"),
        )
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **queued}), 202

    @app.post("/api/accounts/check-oaics")
    def api_account_check_oaics():
        """把单账号 OAICS 资格查询加入统一后台队列。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        email = str(data.get("email") or "").strip()
        acc = None
        if acc_id is not None:
            try:
                acc = db.get_account(int(acc_id))
            except (TypeError, ValueError):
                acc = None
        if acc is None and email:
            acc = db.get_account_by_email(email)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = str(acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        account_id = int(acc.get("id"))
        queued = plan_check_service.enqueue_account_plan_check(
            account_id=account_id,
            email=acc.get("email") or "",
            access_token=token,
            trigger="oaics_manual",
            proxy=data.get("proxy") if "proxy" in data else None,
            timezone_offset_min=str(data.get("timezone_offset_min") or "-"),
        )
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **queued}), 202

    @app.post("/api/accounts/check-plan-bulk")
    def api_accounts_check_plan_bulk():
        """批量把套餐查询加入统一后台队列。Body {account_ids:[...], proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查询 500 个账号"}), 400
        # 与单账号查询保持一致：未传时使用独立网络策略。
        proxy = data.get("proxy") if "proxy" in data else None
        timezone_offset_min = str(data.get("timezone_offset_min") or "-")

        items = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if not (acc.get("access_token") or "").strip():
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 access_token"})
                continue
            items.append(acc)

        started = []
        busy = []
        failed = []
        for acc in items:
            queued = plan_check_service.enqueue_account_plan_check(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=acc.get("access_token") or "",
                trigger="manual_bulk",
                proxy=proxy,
                timezone_offset_min=timezone_offset_min,
            )
            item = {"id": acc.get("id"), "email": acc.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    @app.post("/api/accounts/check-oaics-bulk")
    def api_accounts_check_oaics_bulk():
        """批量入队 OAICS 检测；复用套餐查询的账号/会话上下文。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查询 500 个账号"}), 400
        proxy = data.get("proxy") if "proxy" in data else None
        timezone_offset_min = str(data.get("timezone_offset_min") or "-")
        started, busy, failed, skipped = [], [], [], []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            token = str(acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 access_token"})
                continue
            queued = plan_check_service.enqueue_account_plan_check(
                account_id=acc_id,
                email=str(acc.get("email") or ""),
                access_token=token,
                trigger="oaics_manual_bulk",
                proxy=proxy,
                timezone_offset_min=timezone_offset_min,
            )
            item = {"id": acc_id, "email": acc.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started, "started_count": len(started),
            "busy": busy, "busy_count": len(busy),
            "failed": failed, "failed_count": len(failed),
            "skipped": skipped, "skipped_count": len(skipped),
        }), 202

    @app.post("/api/accounts/<int:acc_id>/setup-2fa")
    def api_account_setup_twofa(acc_id: int):
        """把单账号 2FA 重设任务加入后台队列。"""
        data = request.get_json(silent=True) or {}
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        if str(acc.get("totp_secret") or "").strip():
            return jsonify({"ok": False, "error": "该账号已设置 2FA，无需重新设置"}), 400
        if not str(acc.get("registration_password") or "").strip():
            return jsonify({"ok": False, "error": "该账号未保存注册密码，无法自动重新设置 2FA"}), 400
        queued = twofa_setup_service.enqueue_account_twofa_setup(
            account_id=acc_id,
            email=str(acc.get("email") or ""),
            trigger="manual_retry",
            proxy=data.get("proxy") if "proxy" in data else None,
        )
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **queued}), 202

    @app.get("/api/extract-link/cdk")
    def api_extract_link_cdk():
        """查询当前配置或传入 CDK 的剩余次数。"""
        code = (request.args.get("code") or "").strip() or None
        service_id = (request.args.get("service_id") or "").strip() or None
        try:
            return jsonify({"ok": True, **extract_link_service.query_cdk(cdk=code, service_id=service_id)})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.get("/api/extract-link/services")
    def api_extract_link_services():
        return jsonify({
            "ok": True,
            "items": extract_link_registry.list_services(mask_secrets=True),
            "queue": extract_link_service.queue_settings(),
        })

    @app.post("/api/extract-link/services")
    def api_extract_link_service_save():
        data = request.get_json(silent=True) or {}
        try:
            service = extract_link_registry.save_api_service(data)
            return jsonify({"ok": True, "service": service})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.delete("/api/extract-link/services/<service_id>")
    def api_extract_link_service_delete(service_id: str):
        if not extract_link_registry.delete_api_service(service_id):
            return jsonify({"ok": False, "error": "提链 API 服务不存在或属于兼容配置"}), 404
        return jsonify({"ok": True, "deleted": service_id})

    def _is_extract_eligible(acc: dict) -> bool:
        plan = str(acc.get("current_plan_type") or acc.get("plan_type") or "").lower()
        return plan == "free" and bool(acc.get("plus_trial_eligible"))

    @app.post("/api/accounts/extract-link")
    def api_account_extract_link():
        """单账号提链。Body {account_id|id, link_type?, cdk?}。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        if not _is_extract_eligible(acc):
            return jsonify({"ok": False, "error": "仅支持 free(可Plus试用) 账号提链；请先查询套餐确认资格"}), 400
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        try:
            queued = extract_link_service.enqueue_account_extract(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=token,
                trigger="manual",
                link_type=data.get("link_type"),
                cdk=data.get("cdk"),
                mode=data.get("mode"),
                provider=data.get("provider") or data.get("service_id"),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/accounts/extract-link-bulk")
    def api_accounts_extract_link_bulk():
        """批量提链。Body {account_ids:[...], link_type?, cdk?}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提链 500 个账号"}), 400

        started = []
        busy = []
        failed = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            if not _is_extract_eligible(acc):
                skipped.append({"id": acc_id, "email": email, "reason": "不是 free(可Plus试用)"})
                continue
            token = (acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            try:
                queued = extract_link_service.enqueue_account_extract(
                    account_id=acc_id,
                    email=email or "",
                    access_token=token,
                    trigger="manual_bulk",
                    link_type=data.get("link_type"),
                    cdk=data.get("cdk"),
                    mode=data.get("mode"),
                    provider=data.get("provider") or data.get("service_id"),
                )
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
                continue
            item = {"id": acc_id, "email": email, **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    @app.post("/api/accounts/codex-agent")
    def api_account_codex_agent():
        """单账号生成 Codex Agent Token。Body {account_id|id, verify_task?}。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        try:
            queued = codex_agent_service.enqueue_account_codex_agent(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=token,
                trigger="manual",
                verify_task=bool(data.get("verify_task", True)),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/accounts/codex-agent-bulk")
    def api_accounts_codex_agent_bulk():
        """批量生成 Codex Agent Token。Body {account_ids:[...], verify_task?}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        started = []
        busy = []
        failed = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            token = (acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            try:
                queued = codex_agent_service.enqueue_account_codex_agent(
                    account_id=acc_id,
                    email=email or "",
                    access_token=token,
                    trigger="manual_bulk",
                    verify_task=bool(data.get("verify_task", True)),
                )
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
                continue
            item = {"id": acc_id, "email": email, **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    def _codex_agent_auth_for_account(acc: dict) -> tuple[str, str]:
        """返回账号已生成的 Codex Agent auth.json 文本与下载文件名。"""
        import json as _json
        from pathlib import Path as _Path

        email = str(acc.get("email") or "").strip()
        safe_email = "".join(ch if ch.isalnum() or ch in ("@", ".", "-", "_") else "_" for ch in (email or f"account-{acc.get('id')}"))
        filename = f"codex-agent-{safe_email}.json"
        token_text = str(acc.get("codex_agent_token") or "").strip()
        if token_text:
            try:
                payload = _json.loads(token_text)
                token_text = _json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            except Exception:
                token_text = token_text + ("\n" if not token_text.endswith("\n") else "")
            return token_text, filename

        auth_path = str(acc.get("codex_agent_auth_path") or "").strip()
        if auth_path:
            p = _Path(auth_path)
            if p.exists() and p.is_file():
                return p.read_text(encoding="utf-8"), p.name or filename

        raise RuntimeError("该账号还没有生成 Codex Agent Token")

    def _join_sub2_url(base: str, path: str) -> str:
        base = str(base or "").strip().rstrip("/")
        path = str(path or "").strip()
        if not base or not path:
            return ""
        parsed = urlparse(path)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return path
        return f"{base}/{path.lstrip('/')}"

    def _sub2_codex_session_import_url() -> str:
        from config import sub2api as sub2api_cfg
        api_base = str(getattr(sub2api_cfg, "SUB2API_API_BASE", "") or "").strip()
        if api_base:
            return _join_sub2_url(api_base, "/api/v1/admin/accounts/import/codex-session")
        # 兼容旧配置：之前 SUB2API_API_URL 是完整上传接口 URL。
        return str(getattr(sub2api_cfg, "SUB2API_API_URL", "") or "").strip()

    def _upload_account_codex_agent_to_sub2(acc: dict) -> dict:
        """把账号已生成的 Codex Agent auth.json 上传到 sub2api。"""
        import json as _json
        from config import sub2api as sub2api_cfg
        from core.codex_agent import upload_sub2api_account

        text, _filename = _codex_agent_auth_for_account(acc)
        try:
            auth_json = _json.loads(text)
        except Exception as exc:
            raise RuntimeError(f"Agent Token JSON 无效: {exc}") from exc

        api_url = _sub2_codex_session_import_url()
        api_token = str(getattr(sub2api_cfg, "SUB2API_API_KEY", "") or getattr(sub2api_cfg, "SUB2API_API_TOKEN", "") or "").strip()
        auth_header = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_HEADER", "x-api-key") or "x-api-key").strip()
        auth_prefix = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_PREFIX", "") or "").strip()
        payload_mode = "codex_session_import"
        proxy_key = str(getattr(sub2api_cfg, "SUB2API_PROXY_KEY", "") or "").strip() or None
        timeout = float(getattr(sub2api_cfg, "SUB2API_API_TIMEOUT", 20) or 20)

        result = upload_sub2api_account(
            auth_json,
            api_url,
            api_token=api_token,
            auth_header=auth_header,
            auth_prefix=auth_prefix,
            payload_mode=payload_mode,
            proxy_key=proxy_key,
            timeout=timeout,
        )
        try:
            db.update_account_codex_agent(int(acc.get("id")), {
                "ok": True,
                "status": "success",
                "message": "Agent Token 已上传 sub2api",
                "sub2api_url": result.get("url"),
                "sub2api_mode": result.get("payload_mode"),
                "sub2api_total": result.get("total"),
            })
        except Exception:
            logger.exception("更新账号 sub2api 上传状态失败: account_id=%s", acc.get("id"))
        return result

    @app.post("/api/accounts/<int:acc_id>/codex-agent/upload-sub2")
    def api_account_codex_agent_upload_sub2(acc_id: int):
        """单账号把已生成的 Codex Agent Token 上传到 sub2api。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            result = _upload_account_codex_agent_to_sub2(acc)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        return jsonify({"ok": True, "account_id": acc_id, "email": acc.get("email"), "result": result})

    @app.post("/api/accounts/codex-agent/upload-sub2-bulk")
    def api_accounts_codex_agent_upload_sub2_bulk():
        """批量把已生成的 Codex Agent Token 上传到 sub2api。Body {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        uploaded, failed, skipped = [], [], []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            if (acc.get("codex_agent_status") or "") != "success" and not (acc.get("codex_agent_token") or acc.get("codex_agent_auth_path")):
                skipped.append({"id": acc_id, "email": email, "reason": "未生成 Agent Token"})
                continue
            try:
                result = _upload_account_codex_agent_to_sub2(acc)
                uploaded.append({"id": acc_id, "email": email, "url": result.get("url"), "status_code": result.get("status_code")})
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
        return jsonify({
            "ok": True,
            "uploaded": uploaded,
            "uploaded_count": len(uploaded),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.get("/api/accounts/<int:acc_id>/codex-agent/download")
    def api_account_codex_agent_download(acc_id: int):
        """下载单个账号的 Codex Agent auth.json。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            content, filename = _codex_agent_auth_for_account(acc)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 404
        data = content.encode("utf-8")
        return Response(
            data,
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(data)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/codex-agent/download-bulk")
    def api_accounts_codex_agent_download_bulk():
        """下载选中账号已生成的 Codex Agent Token，打包 ZIP。"""
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        added = []
        errors = []
        used_names = set()
        seen = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw in ids:
                try:
                    acc_id = int(raw)
                except Exception:
                    errors.append({"id": raw, "error": "ID 非法"})
                    continue
                if acc_id in seen:
                    continue
                seen.add(acc_id)
                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                try:
                    content, filename = _codex_agent_auth_for_account(acc)
                    arcname = filename
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, content)
                    added.append({"id": acc_id, "email": acc.get("email"), "filename": arcname})
                except Exception as exc:
                    errors.append({"id": acc_id, "email": acc.get("email"), "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-codex-agent",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有可下载的 Codex Agent Token", "errors": errors}), 404
        now = _dt.now()
        dl_name = f"accounts-codex-agent-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/download-cpa-bulk")
    def api_accounts_download_cpa_bulk():
        """
        从账号列表选中的账号直接到 CPA auth-files 下载 Codex CPA JSON，并打包为 ZIP。
        Body: {"account_ids": [1,2,...]} 或 {"ids": [...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text, list_cpa_codex_auth_files

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        try:
            cpa_files = list_cpa_codex_auth_files()
        except Exception as exc:
            return jsonify({"ok": False, "error": f"读取 CPA auth-files 失败: {type(exc).__name__}: {exc}"}), 502

        def _match_cpa_file(email: str, local_filename: str = "") -> dict | None:
            """在已缓存的 CPA 文件列表中匹配，避免每个账号都重新请求 auth-files。"""
            email_l = str(email or "").strip().lower()
            local_name_l = str(local_filename or "").strip().lower()
            local_stem_l = local_name_l[:-5] if local_name_l.endswith(".json") else local_name_l

            def score(item: dict) -> int:
                name_l = str(item.get("name") or "").lower()
                item_email_l = str(item.get("email") or "").lower()
                s = 0
                if local_name_l and name_l == local_name_l:
                    s = max(s, 100)
                if local_stem_l and name_l.startswith(local_stem_l):
                    s = max(s, 80)
                if email_l and item_email_l == email_l:
                    s = max(s, 70)
                if email_l and email_l in name_l:
                    s = max(s, 60)
                if local_stem_l.endswith("-cpa-callback"):
                    base = local_stem_l[:-len("-cpa-callback")]
                    if base and name_l.startswith(base + "-"):
                        s = max(s, 75)
                return s

            ranked = sorted(((score(item), item) for item in cpa_files), key=lambda x: x[0], reverse=True)
            return ranked[0][1] if ranked and ranked[0][0] > 0 else None

        # 建立 email -> 本地 codex 文件名索引；有本地文件名时传给 CPA 匹配逻辑可提升命中率。
        local_by_email: dict[str, str] = {}
        try:
            for item in db.list_codex_accounts():
                email_key = str(item.get("email") or "").strip().lower()
                fname = str(item.get("filename") or "").strip()
                if email_key and fname and email_key not in local_by_email:
                    local_by_email[email_key] = fname
        except Exception:
            local_by_email = {}

        errors = []
        added = []
        used_names = set()
        seen_ids = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw_id in ids:
                try:
                    acc_id = int(raw_id)
                except (TypeError, ValueError):
                    errors.append({"id": raw_id, "error": "ID 非法"})
                    continue
                if acc_id in seen_ids:
                    continue
                seen_ids.add(acc_id)

                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                email = str(acc.get("email") or "").strip()
                if not email:
                    errors.append({"id": acc_id, "error": "账号缺少 email"})
                    continue

                local_filename = local_by_email.get(email.lower(), "")
                try:
                    meta = _match_cpa_file(email=email, local_filename=local_filename)
                    cpa_name_hint = str((meta or {}).get("name") or "").strip()
                    if not cpa_name_hint:
                        raise RuntimeError(f"[Codex][CPA] 未在 CPA auth-files 中找到匹配的 Codex 凭证: {email}")
                    cpa_text, cpa_name, meta = download_cpa_codex_auth_text(
                        cpa_name=cpa_name_hint,
                    )
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({
                        "id": acc_id,
                        "email": email,
                        "local_filename": local_filename,
                        "cpa_filename": cpa_name,
                        "cpa_meta": meta,
                    })
                    if local_filename:
                        try:
                            db.mark_codex_exported(local_filename)
                        except Exception:
                            pass
                except Exception as exc:
                    errors.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})

            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"accounts-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        if isinstance(data, dict) and data.get("prepare"):
            download_id = _put_prepared_download(zip_bytes, dl_name, "application/zip")
            return jsonify({
                "ok": True,
                "prepared": True,
                "download_id": download_id,
                "download_url": f"/api/downloads/{download_id}",
                "filename": dl_name,
                "added_count": len(added),
                "error_count": len(errors),
            })
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    @app.post("/api/accounts/export-codex")
    def api_accounts_export_codex():
        """Export selected account Codex OAuth credentials in a target format.

        Body: {"account_ids": [1, 2], "format": "cockpit|sub2api|cap", "prepare": true}
        Cockpit Tools and sub2api produce one JSON document. CAP produces one
        JSON object for a single account and a ZIP of CAP objects for multiple
        accounts, preserving the sample's single-object CAP shape.
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt

        from core.codex_export import (
            build_cap_records,
            build_cockpit_tools_records,
            build_sub2api_payload,
            json_bytes,
        )
        from core.codex_oauth import download_cpa_codex_auth_text, list_cpa_codex_auth_files

        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        export_format = str(data.get("format") or "").strip().lower().replace("-", "_")
        aliases = {"cockpit": "cockpit", "cockpit_tools": "cockpit", "sub2": "sub2api", "sub2api": "sub2api", "cap": "cap"}
        export_format = aliases.get(export_format, export_format)
        if export_format not in {"cockpit", "sub2api", "cap"}:
            return jsonify({"ok": False, "error": "format 必须是 cockpit、sub2api 或 cap"}), 400
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多导出 1000 个账号"}), 400

        # sub2api 是纯格式转换，直接读取本地账号/本地 Codex 凭证。
        # Cockpit/CAP 也优先本地；本地无完整凭证时才按需读取 CPA。
        cpa_files: list[dict] | None = None

        def _load_cpa_files() -> list[dict]:
            nonlocal cpa_files
            if cpa_files is None:
                cpa_files = list_cpa_codex_auth_files()
            return cpa_files

        def _match(email: str, local_filename: str = "") -> dict | None:
            email_l = str(email or "").strip().lower()
            local_l = str(local_filename or "").strip().lower()
            stem_l = local_l[:-5] if local_l.endswith(".json") else local_l

            def score(item: dict) -> int:
                name_l = str(item.get("name") or "").lower()
                item_email_l = str(item.get("email") or "").lower()
                value = 0
                if local_l and name_l == local_l:
                    value = max(value, 100)
                if stem_l and name_l.startswith(stem_l):
                    value = max(value, 80)
                if email_l and item_email_l == email_l:
                    value = max(value, 70)
                if email_l and email_l in name_l:
                    value = max(value, 60)
                if stem_l.endswith("-cpa-callback"):
                    base = stem_l[:-len("-cpa-callback")]
                    if base and name_l.startswith(base + "-"):
                        value = max(value, 75)
                return value

            ranked = sorted(((score(item), item) for item in _load_cpa_files()), key=lambda pair: pair[0], reverse=True)
            return ranked[0][1] if ranked and ranked[0][0] > 0 else None

        local_by_email: dict[str, str] = {}
        try:
            for item in db.list_codex_accounts():
                email_key = str(item.get("email") or "").strip().lower()
                filename = str(item.get("filename") or "").strip()
                if email_key and filename and email_key not in local_by_email:
                    local_by_email[email_key] = filename
        except Exception:
            local_by_email = {}

        credentials: list[dict] = []
        added: list[dict] = []
        errors: list[dict] = []
        seen_ids: set[int] = set()
        for raw_id in ids:
            try:
                account_id = int(raw_id)
            except (TypeError, ValueError):
                errors.append({"id": raw_id, "error": "ID 非法"})
                continue
            if account_id in seen_ids:
                continue
            seen_ids.add(account_id)
            account = db.get_account(account_id)
            if not account:
                errors.append({"id": account_id, "error": "账号不存在"})
                continue
            email = str(account.get("email") or "").strip()
            if not email:
                errors.append({"id": account_id, "error": "账号缺少 email"})
                continue
            local_filename = local_by_email.get(email.lower(), "")
            try:
                parsed = None
                source = "local"
                if local_filename:
                    try:
                        local_content, _local_name = db.read_codex_credential(local_filename)
                        local_parsed = _json.loads(local_content)
                        if isinstance(local_parsed, dict) and any(
                            str(local_parsed.get(key) or "").strip()
                            for key in ("access_token", "id_token", "refresh_token")
                        ):
                            parsed = local_parsed
                    except Exception:
                        parsed = None

                # 注册账号记录本身通常已保存 access_token；作为第二级本地来源，
                # 允许 sub2api 等格式在尚未生成 codex_accounts 文件时直接导出。
                if parsed is None:
                    account_candidate = {
                        "access_token": account.get("access_token") or "",
                        "refresh_token": account.get("refresh_token") or "",
                        "id_token": account.get("id_token") or "",
                        "account_id": account.get("account_id") or account.get("chatgpt_account_id") or "",
                        "email": email,
                        "type": "codex",
                        "expired": account.get("expired") or account.get("token_expires_at") or "",
                        "plan_type": account.get("plan_type") or "",
                    }
                    if any(str(account_candidate.get(key) or "").strip() for key in ("access_token", "id_token", "refresh_token")):
                        parsed = account_candidate

                if parsed is None and export_format == "sub2api":
                    raise RuntimeError("本地账号没有可用的 Codex OAuth 凭证")

                if parsed is None:
                    source = "cpa"
                    meta = _match(email, local_filename)
                    cpa_name = str((meta or {}).get("name") or "").strip()
                    if not cpa_name:
                        raise RuntimeError(f"未找到匹配的 Codex 凭证: {email}")
                    content, real_name, _meta = download_cpa_codex_auth_text(cpa_name=cpa_name)
                    parsed = _json.loads(content)
                    if not isinstance(parsed, dict):
                        raise ValueError("CPA 凭证不是 JSON 对象")
                else:
                    real_name = local_filename
                credentials.append(parsed)
                added.append({"id": account_id, "email": email, "filename": real_name, "source": source})
                if local_filename:
                    try:
                        db.mark_codex_exported(local_filename)
                    except Exception:
                        pass
            except Exception as exc:
                errors.append({"id": account_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})

        if not credentials:
            return jsonify({"ok": False, "error": "没有成功导出的 Codex 凭证", "errors": errors}), 502

        now = _dt.now()
        stamp = now.strftime("%Y%m%d-%H%M%S")
        if export_format == "cockpit":
            output = json_bytes(build_cockpit_tools_records(credentials))
            filename = f"codex-cockpit-tools-{stamp}.json"
            mimetype = "application/json"
        elif export_format == "sub2api":
            output = json_bytes(build_sub2api_payload(credentials))
            filename = f"codex-sub2api-{stamp}.json"
            mimetype = "application/json"
        elif len(credentials) == 1:
            output = json_bytes(build_cap_records(credentials)[0])
            filename = f"codex-cap-{stamp}.json"
            mimetype = "application/json"
        else:
            output_buf = io.BytesIO()
            with zipfile.ZipFile(output_buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for item, record in zip(added, build_cap_records(credentials)):
                    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(item.get("email") or item.get("id") or "account"))
                    archive.writestr(f"cap-{safe_name}.json", json_bytes(record))
                archive.writestr("manifest.json", _json.dumps({"exported_at": now.isoformat(timespec="seconds"), "format": "cap", "files": added, "errors": errors}, ensure_ascii=False, indent=2) + "\n")
            output = output_buf.getvalue()
            filename = f"codex-cap-{stamp}.zip"
            mimetype = "application/zip"

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store, max-age=0",
            "X-Export-Format": export_format,
            "X-Export-Count": str(len(added)),
            "X-Export-Error-Count": str(len(errors)),
        }
        if data.get("prepare"):
            download_id = _put_prepared_download(output, filename, mimetype)
            return jsonify({"ok": True, "prepared": True, "download_id": download_id, "download_url": f"/api/downloads/{download_id}", "filename": filename, "format": export_format, "added_count": len(added), "error_count": len(errors)})
        return Response(output, mimetype=mimetype, headers=headers)

    # ----------------------------------------------------------
    # 邮箱池
    # ----------------------------------------------------------
    @app.get("/api/outlook")
    def api_outlook():
        status = request.args.get("status") or None
        limit = request.args.get("limit", default=500, type=int)
        source = _pool_source_arg()
        q = str(request.args.get("q", default="") or "").strip()
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        fetch_limit = 1_000_000 if (paged or q) else limit
        if source == "all":
            rows = []
            rows += _with_pool_source(db.list_outlook_pool(status=status, limit=fetch_limit), "outlook")
            rows += _with_pool_source(db.list_generic_api_email_pool(status=status, limit=fetch_limit), "generic_api")
            rows += _with_pool_source(db.list_icloud_email_pool(status=status, limit=fetch_limit), "icloud")
            rows += _with_pool_source(db.list_domain_email_pool(status=status, limit=fetch_limit), "cloudflare_domain")
            rows = sorted(rows, key=lambda x: str(x.get("created_at") or x.get("imported_at") or x.get("used_at") or ""), reverse=True)
        elif source == "generic_api":
            rows = _with_pool_source(db.list_generic_api_email_pool(status=status, limit=fetch_limit), "generic_api")
        elif source == "icloud":
            rows = _with_pool_source(db.list_icloud_email_pool(status=status, limit=fetch_limit), "icloud")
        elif source == "cloudflare_domain":
            rows = _with_pool_source(db.list_domain_email_pool(status=status, limit=fetch_limit), "cloudflare_domain")
        else:
            rows = _with_pool_source(db.list_outlook_pool(status=status, limit=fetch_limit), "outlook")
        if q:
            rows = [r for r in rows if _matches_query(r, q)]
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            return jsonify(_paginate_items(rows, page=page, page_size=page_size))
        return jsonify(rows[:limit])

    @app.post("/api/outlook/import")
    def api_outlook_import():
        """
        粘贴文本导入邮箱素材。
        Outlook：email----password----clientId----refreshToken
        通用 API / iCloud / 域名邮箱：email----code_url
        分隔符兼容 ---- 与 ====。
        """
        data = request.get_json(silent=True) or {}
        source = (data.get("source") or data.get("type") or "").strip()
        if source not in ("outlook", "generic_api", "icloud", "cloudflare_domain"):
            return jsonify({"ok": False, "error": "导入时请选择具体类型：Outlook、通用 API、iCloud 或域名邮箱"}), 400
        text = data.get("text") or ""
        as_registered = bool(data.get("as_registered", False))
        check = _parse_email_import_text(text, source)
        if not check["input_count"]:
            need = "2 段：邮箱----取码地址" if source in ("generic_api", "icloud", "cloudflare_domain") else "4 段：email----password----clientId----refreshToken"
            return jsonify({"ok": False, "error": f"未解析到邮箱素材（需 {need}，---- 或 ==== 分隔）", **{k: check[k] for k in ("input_count", "valid_count", "invalid_count", "invalid")}}), 400
        if check["invalid_count"]:
            details = "；".join(
                f"第 {item['line']} 行：{'、'.join(item['errors'])}"
                for item in check["invalid"][:8]
            )
            suffix = "；其余错误已省略" if check["invalid_count"] > 8 else ""
            return jsonify({
                "ok": False,
                "error": f"有 {check['invalid_count']} 条素材待修正，暂不允许导入：{details}{suffix}",
                **{k: check[k] for k in ("input_count", "valid_count", "invalid_count", "invalid")},
            }), 400
        records = check["records"]
        if not records:
            need = "2 段：邮箱----取码地址" if source in ("generic_api", "icloud", "cloudflare_domain") else "4 段：email----password----clientId----refreshToken"
            return jsonify({"ok": False, "error": f"未解析到有效邮箱行（需 {need}，---- 或 ==== 分隔）"}), 400
        if as_registered:
            inserted, skipped = db.import_registered_email_accounts(records, source=source)
        elif source == "generic_api":
            inserted, skipped = db.import_generic_api_emails(records)
        elif source == "icloud":
            inserted, skipped = db.import_icloud_emails(records)
        elif source == "cloudflare_domain":
            inserted, skipped = db.import_domain_emails(records)
        else:
            inserted, skipped = db.import_outlook_accounts(records)
        return jsonify({
            "ok": True,
            "inserted": inserted,
            "skipped": skipped,
            "parsed": len(records),
            "input_count": check["input_count"],
            "valid_count": check["valid_count"],
            "invalid_count": check["invalid_count"],
            "as_registered": as_registered,
        })

    @app.post("/api/outlook/status")
    def api_outlook_status():
        """手动改邮箱状态：body {email, status, note?, source?}。status ∈ available/used/failed/disabled。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        status = (data.get("status") or "").strip()
        if not email or status not in ("available", "used", "failed", "disabled"):
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        source = (data.get("source") or _pool_source_arg()).strip()
        if source == "all":
            source = "outlook"
        if source == "generic_api":
            db.release_generic_api_email(email, status=status, note=data.get("note"))
        elif source == "icloud":
            db.release_icloud_email(email, status=status, note=data.get("note"))
        elif source == "cloudflare_domain":
            db.release_domain_email(email, status=status, note=data.get("note"))
        else:
            db.release_outlook(email, status=status, note=data.get("note"))
        return jsonify({"ok": True})

    @app.post("/api/outlook/status-bulk")
    def api_outlook_status_bulk():
        """批量修改邮箱状态。Body {items:[{email,source}], status, note?}。"""
        data = request.get_json(silent=True) or {}
        items = data.get("items") or data.get("emails") or []
        status = (data.get("status") or "").strip()
        note = data.get("note")
        default_source = (data.get("source") or _pool_source_arg()).strip()
        if status not in ("available", "used", "failed", "disabled"):
            return jsonify({"ok": False, "error": "status 非法"}), 400
        if not isinstance(items, list) or not items:
            return jsonify({"ok": False, "error": "items/emails 必须是非空数组"}), 400
        if len(items) > 5000:
            return jsonify({"ok": False, "error": "单次最多操作 5000 个邮箱"}), 400

        updated = []
        skipped = []
        seen = set()
        for raw_item in items:
            if isinstance(raw_item, dict):
                email = (str(raw_item.get("email") or "")).strip()
                item_source = (raw_item.get("source") or default_source or "outlook").strip()
            else:
                email = (str(raw_item or "")).strip()
                item_source = default_source
            if item_source == "all":
                item_source = "outlook"
            key = f"{item_source}:{email.lower()}"
            if not email:
                skipped.append({"email": raw_item, "reason": "邮箱为空"})
                continue
            if key in seen:
                continue
            seen.add(key)
            try:
                if item_source == "generic_api":
                    db.release_generic_api_email(email, status=status, note=note)
                elif item_source == "icloud":
                    db.release_icloud_email(email, status=status, note=note)
                elif item_source == "cloudflare_domain":
                    db.release_domain_email(email, status=status, note=note)
                else:
                    db.release_outlook(email, status=status, note=note)
                updated.append({"email": email, "source": item_source, "status": status})
            except Exception as exc:
                skipped.append({"email": email, "source": item_source, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
        })

    @app.post("/api/outlook/delete")
    def api_outlook_delete():
        """从邮箱池彻底删除一个邮箱：body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        source = (data.get("source") or _pool_source_arg()).strip()
        if source == "all":
            source = "outlook"
        deleted = (
            db.delete_generic_api_email(email)
            if source == "generic_api"
            else db.delete_icloud_email(email)
            if source == "icloud"
            else db.delete_domain_email(email)
            if source == "cloudflare_domain"
            else db.delete_outlook(email)
        )
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/outlook/delete-bulk")
    def api_outlook_delete_bulk():
        """从邮箱池批量彻底删除邮箱：body {emails: [...]}。"""
        data = request.get_json(silent=True) or {}
        source = _pool_source_arg()
        emails = data.get("items") or data.get("emails") or []
        if not isinstance(emails, list) or not emails:
            return jsonify({"ok": False, "error": "emails/items 必须是非空数组"}), 400
        if len(emails) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个邮箱"}), 400

        deleted: list[str] = []
        skipped: list[dict] = []
        seen: set[str] = set()
        for raw_item in emails:
            if isinstance(raw_item, dict):
                email = (str(raw_item.get("email") or "")).strip()
                item_source = (raw_item.get("source") or source or "outlook").strip()
            else:
                email = (str(raw_item or "")).strip()
                item_source = source
            if item_source == "all":
                item_source = "outlook"
            key = f"{item_source}:{email.lower()}"
            if not email:
                skipped.append({"email": raw_item, "reason": "邮箱为空"})
                continue
            if key in seen:
                continue
            seen.add(key)
            deleted_ok = (
                db.delete_generic_api_email(email)
                if item_source == "generic_api"
                else db.delete_icloud_email(email)
                if item_source == "icloud"
                else db.delete_domain_email(email)
                if item_source == "cloudflare_domain"
                else db.delete_outlook(email)
            )
            if deleted_ok:
                deleted.append({"email": email, "source": item_source})
            else:
                skipped.append({"email": email, "reason": "邮箱不存在"})

        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    # ----------------------------------------------------------
    # 域名邮箱池（Cloudflare 域名邮箱模式）
    # ----------------------------------------------------------
    @app.get("/api/domain-pool")
    def api_domain_pool():
        status = request.args.get("status") or None
        limit = request.args.get("limit", default=500, type=int)
        return jsonify(db.list_domain_email_pool(status=status, limit=limit))

    @app.post("/api/domain-pool/status")
    def api_domain_pool_status():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        status = (data.get("status") or "").strip()
        if not email or status not in ("available", "used", "failed", "disabled"):
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        db.release_domain_email(email, status=status, note=data.get("note"))
        return jsonify({"ok": True})

    @app.post("/api/domain-pool/delete")
    def api_domain_pool_delete():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        deleted = db.delete_domain_email(email)
        return jsonify({"ok": True, "deleted": deleted})

    # ----------------------------------------------------------
    # Codex 授权账号（CPA 兼容凭证）
    # ----------------------------------------------------------
    @app.get("/api/codex")
    def api_codex_list():
        rows = db.list_codex_accounts()
        q = str(request.args.get("q", default="") or "").strip()
        if q:
            rows = [r for r in rows if _matches_query(r, q)]
        limit = request.args.get("limit", default=500, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            result = _paginate_items(rows, page=page, page_size=page_size)
            result["accounts"] = result.pop("items")
            result["summary"] = db.codex_accounts_summary()
            return jsonify(result)
        return jsonify({
            "summary": db.codex_accounts_summary(),
            "accounts": rows[:limit],
        })

    @app.get("/api/codex/download/<path:filename>")
    def api_codex_download(filename: str):
        """
        下载一个 CPA 兼容的 codex-*.json 文件，下载即标记为已导出（计数+1）。
        前端通过浏览器原生下载触发（a 标签 / window.location）。
        """
        try:
            content, fname = db.read_codex_credential(filename)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        db.mark_codex_exported(fname)
        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/api/codex/download-from-cpa/<path:filename>")
    def api_codex_download_from_cpa(filename: str):
        """按本地 codex 文件/回执匹配 CPA auth-files，并从 CPA 下载实际 Codex JSON。"""
        try:
            content, fname = db.read_codex_credential(filename)
            import json as _json
            try:
                local = _json.loads(content)
            except Exception:
                local = {}
            email = str(local.get("email") or "").strip()
            from core.codex_oauth import download_cpa_codex_auth_text
            cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=fname)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
        db.mark_codex_exported(fname)
        return Response(
            cpa_text,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{cpa_name}"'},
        )

    @app.post("/api/codex/download-bulk-from-cpa")
    def api_codex_download_bulk_from_cpa():
        """
        批量从 CPA 下载选中的 Codex 凭证，打包成 zip；zip 内每个文件都是 CPA 原始 JSON。
        Body: {"filenames": ["codex-xxx-cpa-callback.json", ...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        errors = []
        added = []
        used_names = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fname in filenames:
                if not isinstance(fname, str):
                    errors.append({"filename": str(fname), "error": "非字符串"})
                    continue
                try:
                    content, real_fname = db.read_codex_credential(fname)
                    try:
                        local = _json.loads(content)
                    except Exception:
                        local = {}
                    email = str(local.get("email") or "").strip()
                    cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=real_fname)
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({"local_filename": real_fname, "cpa_filename": cpa_name})
                    db.mark_codex_exported(real_fname)
                except Exception as exc:
                    errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"codex-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        return Response(
            buf.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/download-bulk")
    def api_codex_download_bulk():
        """
        批量下载选中的 codex 凭证，打包到一个 JSON 文件里。

        Body: {"filenames": ["codex-xxx.json", ...]}
        响应：聚合 JSON（attachment 触发浏览器下载），结构：
            {
              "exported_at": "...",
              "count": N,
              "credentials": [{"filename": "...", "data": {...原始凭证内容...}}, ...],
              "errors": [...]   // 仅当部分失败时出现
            }
        注意：聚合格式**不能直接被 CPA 读**，CPA 是按单文件加载 auths/ 目录的。
              本接口主要用途是备份 / 跨机迁移 / 二次处理。
        每个成功的凭证会自动标记 mark_exported（计数+1）。
        """
        import json as _json
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        bundle = []
        errors = []
        for fname in filenames:
            if not isinstance(fname, str):
                errors.append({"filename": str(fname), "error": "非字符串"})
                continue
            try:
                content, real_fname = db.read_codex_credential(fname)
                parsed = _json.loads(content)
                bundle.append({"filename": real_fname, "data": parsed})
                db.mark_codex_exported(real_fname)
            except Exception as exc:
                errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})

        now = _dt.now()
        result = {
            "exported_at": now.isoformat(timespec="seconds"),
            "count": len(bundle),
            "credentials": bundle,
        }
        if errors:
            result["errors"] = errors

        dl_name = f"codex-bulk-{now.strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            _json.dumps(result, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/reset-export")
    def api_codex_reset_export():
        """清掉某个 codex 凭证的导出状态（重新标为未导出）。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            db.reset_codex_exported(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/codex/delete")
    def api_codex_delete():
        """删除一个 codex 凭证文件。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            deleted = db.delete_codex_credential(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not deleted:
            return jsonify({"ok": False, "error": "凭证文件不存在"}), 404
        return jsonify({"ok": True, "deleted": fname})

    @app.post("/api/codex/delete-bulk")
    def api_codex_delete_bulk():
        """批量删除 codex 凭证文件。body {filenames:[...]}。"""
        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个"}), 400
        deleted = []
        skipped = []
        seen = set()
        for fname in filenames:
            fname = str(fname or "").strip()
            if not fname or fname in seen:
                continue
            seen.add(fname)
            try:
                ok = db.delete_codex_credential(fname)
                if ok:
                    deleted.append(fname)
                else:
                    skipped.append({"filename": fname, "reason": "文件不存在"})
            except Exception as exc:
                skipped.append({"filename": fname, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    def _reserve_codex_retry(email: str) -> bool:
        """进程内防重复占位；成功返回 True。"""
        return codex_retry_service.reserve(email)

    def _release_codex_retry(email: str) -> None:
        codex_retry_service.release(email)

    def _run_codex_retry_worker(email: str, *, batch_label: str | None = None, clear_log: bool = True) -> None:
        """执行一个账号的 Codex 补跑。调用前必须已经 reserve。"""
        codex_retry_service.run_worker(email, batch_label=batch_label, clear_log=clear_log)


    @app.post("/api/codex/stop")
    def api_codex_stop():
        """停止单个 Codex 补跑。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        result = codex_retry_service.request_stop(email)
        status = int(result.pop("status", 200) or 200)
        return jsonify(result), status

    @app.post("/api/codex/stop-bulk")
    def api_codex_stop_bulk():
        """批量停止 Codex 补跑。Body {emails:[...]} 或 {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        emails = data.get("emails") or []
        ids = data.get("account_ids") or data.get("ids") or []
        targets = []
        if isinstance(emails, list) and emails:
            targets = [str(x or "").strip() for x in emails]
        elif isinstance(ids, list) and ids:
            for raw in ids:
                try:
                    acc = db.get_account(int(raw))
                except Exception:
                    acc = None
                if acc and acc.get("email"):
                    targets.append(str(acc.get("email") or "").strip())
        else:
            return jsonify({"ok": False, "error": "emails 或 account_ids 必须是非空数组"}), 400
        if len(targets) > 500:
            return jsonify({"ok": False, "error": "单次最多停止 500 个"}), 400
        stopped = []
        skipped = []
        seen = set()
        for email in targets:
            key = email.lower()
            if not email or key in seen:
                continue
            seen.add(key)
            acc = db.get_account_by_email(email)
            if acc is None:
                skipped.append({"email": email, "reason": "账号不存在"})
                continue
            if (acc.get("codex_status") or "") != "retrying" and not codex_retry_service.is_retrying(email):
                skipped.append({"email": email, "reason": "未处于补跑中"})
                continue
            r = codex_retry_service.request_stop(email)
            if r.get("ok"):
                stopped.append({"email": email, "injected": r.get("injected"), "running": r.get("running")})
            else:
                skipped.append({"email": email, "reason": r.get("error") or "停止失败"})
        return jsonify({"ok": True, "stopped": stopped, "stopped_count": len(stopped), "skipped": skipped})

    @app.post("/api/codex/reset-retrying")
    def api_codex_reset_retrying():
        """手动重置某账号的 Codex 补跑中状态。Body {email, status?}。"""
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        raw_status = (data.get("status") or "failed").strip().lower()
        if raw_status in ("", "none", "null", "clear"):
            raw_status = "empty"
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        if raw_status not in ("failed", "skipped", "empty"):
            return jsonify({"ok": False, "error": "status 仅支持 failed/skipped/empty"}), 400

        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        new_status = "" if raw_status == "empty" else raw_status
        err = None if raw_status == "empty" else "用户手动重置补跑中状态"
        ok = db.update_account_codex_status(email, new_status, err)
        if not ok:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        _release_codex_retry(email)

        try:
            log_path = codex_retry_service.log_path(email)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                ts = _dt.now().strftime("%H:%M:%S")
                shown = new_status or "空"
                f.write(f"{ts} [WARNING] [Codex 补跑] 用户手动重置补跑中状态，当前状态={shown}\n")
        except Exception:
            logger.exception("写入 Codex 补跑重置日志失败")

        return jsonify({"ok": True, "message": "已重置补跑中状态", "status": new_status})

    @app.post("/api/codex/retry")
    def api_codex_retry():
        """手动补跑某账号的 Codex 授权。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        if (acc.get("codex_status") or "") == "deactivated":
            return jsonify({"ok": False, "error": "账号已废号，不能补跑 Codex"}), 409
        if not _reserve_codex_retry(email):
            return jsonify({"ok": False, "error": "该账号正在补跑中，请稍候"}), 409

        db.update_account_codex_status(email, "retrying", None)
        threading.Thread(
            target=_run_codex_retry_worker,
            kwargs={"email": email, "clear_log": True},
            name=f"codex-retry-{email}",
            daemon=True,
        ).start()
        return jsonify({"ok": True, "message": "已在后台开始补跑，~1-2 分钟后刷新查看"})

    @app.post("/api/codex/retry-bulk")
    def api_codex_retry_bulk():
        """批量补跑 Codex。Body {account_ids:[...], workers: 1-16}。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        workers = data.get("workers", 1)
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        try:
            workers = max(1, min(16, int(workers)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是数字"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多选择 500 个账号"}), 400

        selected = []
        skipped = []
        seen_ids = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen_ids:
                continue
            seen_ids.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = (acc.get("email") or "").strip()
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            if (acc.get("codex_status") or "") == "deactivated":
                skipped.append({"id": acc_id, "email": email, "reason": "账号已废号"})
                continue
            if not _reserve_codex_retry(email):
                skipped.append({"id": acc_id, "email": email, "reason": "正在补跑中"})
                continue
            selected.append({"id": acc_id, "email": email})

        if not selected:
            return jsonify({"ok": False, "error": "没有可补跑的账号", "skipped": skipped}), 409

        batch_id = _dt.now().strftime("%Y%m%d-%H%M%S")
        for item in selected:
            email = item["email"]
            db.update_account_codex_status(email, "retrying", None)
            log_path = codex_retry_service.log_path(email)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                f"{_dt.now().strftime('%H:%M:%S')} [INFO] [Codex 批量补跑] 已加入批量任务 batch={batch_id} workers={workers}，等待线程执行\n",
                encoding="utf-8",
            )

        def _bulk_runner(items: list[dict], max_workers: int, batch: str):
            logger.info(f"[Codex 批量补跑] 启动 batch={batch} count={len(items)} workers={max_workers}")
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"codex-bulk-{batch}") as ex:
                futures = [ex.submit(_run_codex_retry_worker, it["email"], batch_label=f"{batch} #{idx}/{len(items)}", clear_log=False) for idx, it in enumerate(items, 1)]
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception:
                        logger.exception(f"[Codex 批量补跑] 子任务异常 batch={batch}")
            logger.info(f"[Codex 批量补跑] 完成 batch={batch}")

        threading.Thread(
            target=_bulk_runner,
            args=(selected, workers, batch_id),
            name=f"codex-bulk-dispatch-{batch_id}",
            daemon=True,
        ).start()
        return jsonify({
            "ok": True,
            "message": f"已开始批量补跑 {len(selected)} 个账号，并发 {workers}",
            "started": selected,
            "started_count": len(selected),
            "skipped": skipped,
            "batch_id": batch_id,
        })

    @app.get("/api/codex/retry-log")
    def api_codex_retry_log():
        """读取某邮箱最近一次补跑的日志。?email=xxx"""
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = codex_retry_service.log_path(email)
        if not p.exists():
            return jsonify({"ok": True, "log": "", "running": False})
        max_bytes = 50_000
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            content = f.read().decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "log": content,
            "running": codex_retry_service.is_retrying(email),
        })

    @app.get("/api/accounts/live-check-log")
    def api_account_live_check_log():
        """读取某邮箱最近一次查活日志。?email=xxx"""
        from core import account_liveness
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = account_liveness.log_path(email)
        if not p.exists():
            return jsonify({"ok": True, "log": "", "running": live_check_service.is_checking(email)})
        max_bytes = 80_000
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            content = f.read().decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "log": content,
            "running": live_check_service.is_checking(email),
        })

    @app.get("/api/accounts/twofa-log")
    def api_account_twofa_log():
        """读取某邮箱最近一次 2FA 重设日志。?email=xxx"""
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        path = twofa_setup_service.log_path(email)
        if not path.exists():
            return jsonify({"ok": True, "log": "", "running": twofa_setup_service.is_setting(email)})
        max_bytes = 80_000
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            content = handle.read().decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "log": content,
            "running": twofa_setup_service.is_setting(email),
        })

    @app.get("/api/accounts/rebind-log")
    def api_account_rebind_log():
        """Read one rebind task log; task logs are shared with the registration view."""
        raw_job_id = request.args.get("job_id") or request.args.get("id")
        try:
            job_id = int(raw_job_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "job_id 必须是有效任务 ID"}), 400
        job = db.get_job(job_id)
        if not job or str(job.get("job_type") or "") != "rebind":
            return jsonify({"ok": False, "error": "换绑任务不存在"}), 404
        offset = max(0, request.args.get("offset", default=0, type=int) or 0)
        delta = svc.read_job_log_delta(job_id, offset=offset, job=job)
        delta = dict(delta)
        delta["content"] = _redact_rebind_response_text(job, delta.get("content") or "")
        return jsonify({
            "ok": True,
            "job": _public_rebind_job(job),
            "log": delta.get("content") or "",
            "log_delta": delta,
            "offset": int(delta.get("offset") or 0),
            "size": int(delta.get("size") or 0),
            "running": str(job.get("status") or "") in {"pending", "running", "stopping"},
        })

    @app.get("/api/accounts/<int:acc_id>/extract-link-log")
    def api_account_extract_link_log(acc_id: int):
        """读取账号最近一次通用提链日志。"""
        account = db.get_account(acc_id)
        if not account:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        email = str(account.get("email") or "").strip()
        path = extract_link_service.log_path(email)
        content = ""
        if path.exists():
            max_bytes = 80_000
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > max_bytes:
                    handle.seek(size - max_bytes)
                content = handle.read().decode("utf-8", errors="replace")
        status = str(account.get("extract_link_status") or "")
        return jsonify({
            "ok": True,
            "log": content,
            "running": status in {"queued", "running"},
            "status": status,
            "progress": int(account.get("extract_link_progress") or 0),
            "service_name": account.get("extract_link_service_name") or "",
        })

    # ----------------------------------------------------------
    # 注册任务
    # ----------------------------------------------------------
    @app.get("/api/jobs")
    def api_jobs():
        limit = request.args.get("limit", default=100, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        from config import email as _email_cfg
        manual_otp_required = not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True))
        is_paged = paged or page_arg is not None or page_size_arg is not None
        page = max(1, int(page_arg or 1))
        page_size = max(1, min(500, int(page_size_arg or limit or 50)))
        if is_paged:
            page_data = db.list_jobs_page(
                limit=page_size,
                offset=(page - 1) * page_size,
            )
            rows = page_data.get("items") or []
        else:
            page_data = None
            rows = db.list_jobs(limit=max(0, int(limit or 0)))

        retry_info = db.get_job_retry_info_batch(rows)
        for row in rows:
            row["manual_otp_required"] = manual_otp_required
            try:
                row.update(retry_info.get(int(row.get("id") or 0), {}))
            except (TypeError, ValueError):
                pass
        if is_paged:
            assert page_data is not None
            result = {
                "ok": True,
                "items": [_compact_job_for_list(row) for row in rows],
                "total": int(page_data.get("total") or 0),
                "page": page,
                "page_size": page_size,
                "offset": int(page_data.get("offset") or 0),
                "limit": page_size,
                "status_counts": page_data.get("status_counts") or _job_status_counts(rows),
            }
            result["current_batch"] = db.get_latest_registration_batch()
            result["compact"] = True
            _schedule_job_retention_once()
            return jsonify(result)
        _schedule_job_retention_once()
        return jsonify([_public_job_for_response(row) for row in rows])

    @app.get("/api/registration-batches")
    def api_registration_batches():
        """返回注册批次历史，包含实时耗时与成功、失败数量。"""
        limit = max(1, min(1000, request.args.get("limit", default=200, type=int) or 200))
        items = db.list_registration_batches(limit=limit)
        return jsonify({"ok": True, "items": items, "total": len(items)})

    @app.post("/api/registration-batches/clear")
    def api_registration_batches_clear():
        """清空已完成的任务日志，仍在执行的批次继续保留。"""
        result = db.clear_registration_batches(keep_active=True)
        return jsonify({"ok": True, **result})

    @app.post("/api/jobs")
    def api_jobs_create():
        """启动批量注册：body {count, workers, email_source?}。"""
        data = request.get_json(silent=True) or {}
        try:
            count = int(data.get("count", 1))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "count 非法"}), 400
        if count < 1 or count > 200:
            return jsonify({"ok": False, "error": "count 需在 1~200 之间"}), 400

        # workers 控制本次新提交任务使用的线程池；若和上次不同，服务层会为新任务切换到新池。
        try:
            workers = max(1, min(16, int(data.get("workers", 3))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400

        # 开关开启时先检查代理池全部出口：失败项自动从配置中删除，至少
        # 保留一个可用代理才继续创建批次、领取邮箱并提交线程池。
        proxy_check = None
        from config import proxy as _proxy_cfg
        if bool(getattr(_proxy_cfg, "PROXY_CHECK_BEFORE_REGISTRATION", False)):
            from core.proxy_test import ProxyTestError, test_proxy_pool

            try:
                proxy_check = test_proxy_pool(list(getattr(_proxy_cfg, "PROXY_POOL", []) or []))
                valid_proxy_urls = list(proxy_check.pop("valid_proxy_urls", []) or [])
                proxy_check["enabled"] = True
                proxy_check["removed"] = int(proxy_check.get("failed") or 0)
                if proxy_check["removed"]:
                    # 写入 .env 并热加载，确保本批后续随机抽取只会命中已通过检查的代理。
                    config_editor.update_config({"PROXY_POOL": valid_proxy_urls})
                    import config as _config_pkg

                    _config_pkg.reload_all()
                    logger.warning(
                        "注册前代理池已自动清理：removed=%s kept=%s",
                        proxy_check["removed"], proxy_check.get("available"),
                    )
                if not proxy_check.get("ok"):
                    total = int(proxy_check.get("total") or 0)
                    logger.warning("注册任务已终止：%s 个代理全部不可用，代理池已清空", total)
                    return jsonify({
                        "ok": False,
                        "code": "proxy_pool_preflight_failed",
                        "error": f"代理池中的 {total} 个代理全部不可用，失败项已自动删除",
                        "task_ended": True,
                        "jobs_created": 0,
                        "proxy_check": proxy_check,
                    }), 400
                logger.info(
                    "注册任务启动前代理池检查完成：available=%s removed=%s total=%s",
                    proxy_check.get("available"), proxy_check.get("removed"), proxy_check.get("total"),
                )
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ProxyTestError) else f"{type(exc).__name__}: 代理检查异常"
                logger.warning("注册任务已终止：代理池连通性检查失败：%s", reason)
                return jsonify({
                    "ok": False,
                    "code": "proxy_pool_preflight_failed",
                    "error": f"代理池连通性检查失败：{reason}",
                    "task_ended": True,
                    "jobs_created": 0,
                }), 400

        # 提交前先确认池里有足够可用邮箱，给前端一个温和提示（不阻断）
        from config import email as _email_cfg
        from config import register as _register_cfg
        from core.email_provider import parse_email_sources
        source_override = str(data.get("email_source") or "").strip().lower()
        valid_source_overrides = {
            "outlook", "generic_api", "icloud", "cloudflare_domain",
            "cloudflare", "gptmail", "mailnest", "cloudmail",
        }
        if source_override and source_override not in valid_source_overrides:
            return jsonify({"ok": False, "error": f"邮箱类型非法: {source_override}"}), 400
        if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True)):
            reg_email = str(getattr(_register_cfg, "REGISTER_EMAIL", "") or "").strip()
            if not reg_email:
                return jsonify({
                    "ok": False,
                    "error": "手动模式未配置 REGISTER_EMAIL。请到配置页填写「手动注册邮箱」，或开启自动取邮箱+收码。",
                }), 400
            if count > 1:
                return jsonify({
                    "ok": False,
                    "error": "手动模式建议每次只跑 1 个任务（同一 REGISTER_EMAIL）。请把数量设为 1。",
                }), 400
            jobs = svc.submit_registration(count=count, workers=workers)
            return jsonify({
                "ok": True,
                "submitted": len(jobs),
                "jobs": jobs,
                "warning": f"手动 OTP 模式：将使用 {reg_email}；验证码请在任务页提交",
                "workers": workers,
                "batch": _registration_batch_for_jobs(jobs),
                "proxy_check": proxy_check,
            })
        sources = parse_email_sources(source_override or _email_cfg.EMAIL_SOURCE)
        effective_source = ",".join(sources)
        if "gptmail" in sources:
            api_key = str(getattr(_email_cfg, "GPTMAIL_API_KEY", "") or "").strip()
            if not api_key:
                return jsonify({
                    "ok": False,
                    "error": "已选择 gptmail 邮箱来源，请填写 GPTMail API Key（配置 → 邮箱 / OTP）。",
                }), 400
        if "cloudflare" in sources:
            api_base = str(getattr(_email_cfg, "CLOUDFLARE_API_BASE", "") or "").strip()
            if not api_base:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudflare 邮箱来源，请填写 Cloudflare API 地址（配置 → 邮箱 / OTP）。",
                }), 400
            auth_mode = str(getattr(_email_cfg, "CLOUDFLARE_AUTH_MODE", "none") or "none").strip().lower()
            accounts_path = str(getattr(_email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address") or "").strip().lower()
            api_key = str(getattr(_email_cfg, "CLOUDFLARE_API_KEY", "") or "").strip()
            needs_key = auth_mode in ("x-admin-auth", "bearer", "x-api-key", "query-key") or accounts_path.rstrip("/").endswith("/admin/new_address")
            if needs_key and not api_key:
                return jsonify({
                    "ok": False,
                    "error": "Cloudflare admin/鉴权模式需要填写 Cloudflare API Key（配置 → 邮箱 / OTP）。",
                }), 400
        if "mailnest" in sources:
            api_key = str(getattr(_email_cfg, "MAIL_NEST_API_KEY", "") or "").strip()
            project_code = str(getattr(_email_cfg, "MAIL_NEST_PROJECT_CODE", "") or "").strip()
            if not api_key:
                return jsonify({
                    "ok": False,
                    "error": "已选择 mailnest 邮箱来源，请填写 MailNest API Key（配置 → 邮箱 / OTP）。",
                }), 400
            if not project_code:
                return jsonify({
                    "ok": False,
                    "error": "已选择 mailnest 邮箱来源，请填写 MailNest 项目代码（配置 → 邮箱 / OTP）。",
                }), 400
        if "cloudmail" in sources:
            api_base = str(getattr(_email_cfg, "CLOUDMAIL_API_BASE", "") or "").strip()
            token = str(getattr(_email_cfg, "CLOUDMAIL_AUTH_TOKEN", "") or "").strip()
            if not api_base:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudmail 邮箱来源，请填写 CloudMail API 地址（配置 → 邮箱 / OTP）。",
                }), 400
            if not token:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudmail 邮箱来源，请填写 CloudMail Token（配置 → 邮箱 / OTP）。",
                }), 400
        if "gptmail" in sources or "mailnest" in sources or "cloudmail" in sources or "cloudflare" in sources:
            # 临时邮箱在任务开始时动态生成，不需要本地邮箱池容量提示。
            warning = ""
        elif "cloudflare_domain" in sources:
            pool = db.domain_email_pool_summary()
            warning = ""
            if sources == ["cloudflare_domain"] and pool.get("available", 0) < count:
                warning = f"域名邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会自动生成"
        elif sources == ["generic_api"]:
            pool = db.generic_api_email_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"通用 API 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif sources == ["icloud"]:
            pool = db.icloud_email_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"iCloud 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif len(sources) > 1:
            available = 0
            if "outlook" in sources:
                available += db.outlook_pool_summary().get("available", 0)
            if "generic_api" in sources:
                available += db.generic_api_email_pool_summary().get("available", 0)
            if "icloud" in sources:
                available += db.icloud_email_pool_summary().get("available", 0)
            warning = ""
            if available < count:
                warning = f"多个邮箱池合计仅 {available} 个可用，少于任务数 {count}，不足的会失败"
        else:
            pool = db.outlook_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"可用邮箱仅 {pool.get('available', 0)} 个，少于任务数 {count}，不足的会失败"
        submit_kwargs = {"count": count, "workers": workers}
        if source_override:
            submit_kwargs["email_source"] = effective_source
        jobs = svc.submit_registration(**submit_kwargs)
        return jsonify({
            "ok": True,
            "submitted": len(jobs),
            "jobs": jobs,
            "warning": warning,
            "workers": workers,
            "email_source": effective_source,
            "batch": _registration_batch_for_jobs(jobs),
            "proxy_check": proxy_check,
        })

    @app.get("/api/manual-otp/waiting")
    def api_manual_otp_waiting():
        """列出当前正在等待手动验证码的邮箱。"""
        from core.manual_otp import list_waiting
        return jsonify({"ok": True, "waiting": list_waiting()})

    @app.post("/api/manual-otp")
    def api_manual_otp_submit():
        """提交手动邮箱验证码。Body: {email, code} 或 {job_id, code}。"""
        from core.manual_otp import submit_manual_otp
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or data.get("otp") or "").strip()
        email = (data.get("email") or "").strip()
        job_id = data.get("job_id")
        if not email and job_id is not None:
            job = db.get_job(int(job_id))
            email = (job or {}).get("email") or ""
        if not email:
            return jsonify({"ok": False, "error": "email/job_id 缺失"}), 400
        try:
            result = submit_manual_otp(email, code)
            return jsonify(result)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/jobs/cancel-pending")
    def api_jobs_cancel_pending():
        """取消所有还在排队（status=pending）的任务。已在 running 的不动。"""
        cancelled = svc.cancel_pending_jobs()
        rebind_cancelled = rebind_service.cancel_pending_rebind_jobs()
        return jsonify({"ok": True, "cancelled": cancelled + rebind_cancelled, "registration_cancelled": cancelled, "rebind_cancelled": rebind_cancelled})

    @app.post("/api/jobs/<int:job_id>/stop")
    def api_job_stop(job_id: int):
        """手动停止单个注册任务。pending 取消；running 发送停止信号。"""
        job = db.get_job(job_id)
        result = rebind_service.request_stop_rebind_job(job_id) if job and str(job.get("job_type") or "") == "rebind" else svc.request_stop_job(job_id)
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error") or "停止失败"}), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/<int:job_id>/retry")
    def api_job_retry(job_id: int):
        """重试失败/停止/取消任务；服务端自动判断完整注册或 Codex 补跑。"""
        source_job = db.get_job(job_id)
        if source_job and str(source_job.get("job_type") or "").strip().lower() == "rebind":
            return jsonify({"ok": False, "error": "换绑任务不支持注册重试，请重新提交换绑"}), 409
        data = request.get_json(silent=True) or {}
        try:
            workers = max(1, min(16, int(data.get("workers", svc.get_executor_workers()))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400
        result = svc.retry_job(job_id, workers=workers)
        if not result.get("ok"):
            return jsonify(result), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/retry-bulk")
    def api_jobs_retry_bulk():
        """批量重试任务；不支持项逐条跳过并返回原因。"""
        data = request.get_json(silent=True) or {}
        job_ids = data.get("job_ids") or data.get("ids") or []
        if not isinstance(job_ids, list) or not job_ids:
            return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
        if len(job_ids) > 500:
            return jsonify({"ok": False, "error": "单次最多重试 500 个任务"}), 400
        try:
            workers = max(1, min(16, int(data.get("workers", svc.get_executor_workers()))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400

        started: list[dict] = []
        reused: list[dict] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw_id in job_ids:
            try:
                one_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if one_id in seen:
                continue
            seen.add(one_id)
            source_job = db.get_job(one_id)
            if source_job and str(source_job.get("job_type") or "").strip().lower() == "rebind":
                skipped.append({"id": one_id, "reason": "换绑任务不支持注册重试"})
                continue
            result = svc.retry_job(one_id, workers=workers)
            if not result.get("ok"):
                skipped.append({"id": one_id, "reason": result.get("error") or "不能重试"})
            elif result.get("reused"):
                reused.append(result)
            else:
                started.append(result)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "reused": reused,
            "reused_count": len(reused),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "workers": workers,
        })

    @app.post("/api/jobs/<int:job_id>/delete")
    def api_job_delete(job_id: int):
        """删除一个任务记录。运行中的任务不允许删除；排队任务删除后执行前会自动跳过。"""
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        if job.get("status") in ("running", "stopping"):
            return jsonify({"ok": False, "error": "运行中的任务不能删除，请等待完成后再删"}), 409
        if str(job.get("job_type") or "").strip().lower() == "rebind" and job.get("status") == "pending":
            # 释放目标邮箱后再移除任务记录，避免“删除任务但邮箱永远占用”。
            rebind_service.request_stop_rebind_job(job_id)
        deleted = db.delete_job(job_id, delete_log=True, allow_running=False)
        if not deleted:
            return jsonify({"ok": False, "error": "任务不存在或已开始运行"}), 409
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/jobs/delete-bulk")
    def api_jobs_delete_bulk():
        """批量删除任务记录。running 任务跳过，其它任务删除记录和日志。"""
        data = request.get_json(silent=True) or {}
        job_ids = data.get("job_ids") or data.get("ids") or []
        if not isinstance(job_ids, list) or not job_ids:
            return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
        if len(job_ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个任务"}), 400

        deleted: list[int] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw_id in job_ids:
            try:
                job_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if job_id in seen:
                continue
            seen.add(job_id)

            job = db.get_job(job_id)
            if not job:
                skipped.append({"id": job_id, "reason": "任务不存在"})
                continue
            if job.get("status") in ("running", "stopping"):
                skipped.append({"id": job_id, "reason": "运行中，不能删除"})
                continue
            if str(job.get("job_type") or "").strip().lower() == "rebind" and job.get("status") == "pending":
                rebind_service.request_stop_rebind_job(job_id)
            if db.delete_job(job_id, delete_log=True, allow_running=False):
                deleted.append(job_id)
            else:
                skipped.append({"id": job_id, "reason": "任务不存在或已开始运行"})

        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    @app.get("/api/jobs/<int:job_id>/log")
    def api_job_log(job_id: int):
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        offset = max(0, request.args.get("offset", default=0, type=int) or 0)
        log_delta = svc.read_job_log_delta(job_id, offset=offset, job=job)
        if str(job.get("job_type") or "").strip().lower() == "rebind":
            log_delta = dict(log_delta)
            log_delta["content"] = _redact_rebind_response_text(job, log_delta.get("content") or "")
        return jsonify({
            "ok": True,
            "job": _public_job_for_response(job),
            # 保留 log 字段兼容旧页面；新页面使用 offset/log_delta 增量追加。
            "log": log_delta.get("content") or "",
            "log_delta": log_delta,
            "offset": int(log_delta.get("offset") or 0),
            "size": int(log_delta.get("size") or 0),
            "reset": bool(log_delta.get("reset")),
            "log_changed": bool(log_delta.get("changed")),
        })

    # ----------------------------------------------------------
    # RoxyBrowser 辅助接口
    # ----------------------------------------------------------
    @app.get("/api/roxy/workspaces")
    def api_roxy_workspaces():
        try:
            from core.roxybrowser_client import RoxyBrowserClient
            result = RoxyBrowserClient().list_workspaces()
            return jsonify(result)
        except Exception as exc:
            logger.exception("获取 Roxy 团队/工作区失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    # ----------------------------------------------------------
    # 配置读写
    # ----------------------------------------------------------
    @app.get("/api/config")
    def api_config_get():
        return jsonify(config_editor.get_config())

    @app.post("/api/proxy/test")
    def api_proxy_test():
        """测试当前表单中的代理，返回出口 IP 和地理位置。"""
        data = request.get_json(silent=True) or {}
        proxy_url = str(data.get("proxy") or "").strip()
        timeout = data.get("timeout")
        try:
            from core.proxy_test import test_proxy

            result = test_proxy(proxy_url, timeout=timeout)
            return jsonify(result)
        except Exception as exc:
            logger.warning("代理测试失败: %s: %s", type(exc).__name__, exc)
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/proxy/warmup")
    def api_proxy_warmup():
        """启动后台预热任务；每个代理完成后可通过状态接口读取实时结果。"""
        try:
            from config import proxy as _proxy_cfg
            from core.proxy_test import ProxyTestError, persist_proxy_pool, warmup_proxy_pool

            data = request.get_json(silent=True) or {}
            pool = list(getattr(_proxy_cfg, "PROXY_POOL", []) or [])
            target = data.get("target_clean", getattr(_proxy_cfg, "PROXY_WARMUP_TARGET_CLEAN_IPS", 3))
            timeout = data.get("timeout", getattr(_proxy_cfg, "PROXY_WARMUP_TIMEOUT", 12.0))
            workers = data.get("workers", getattr(_proxy_cfg, "PROXY_WARMUP_WORKERS", 4))
            health_url = str(data.get("health_url") or getattr(_proxy_cfg, "PROXY_WARMUP_HEALTH_URL", "")).strip()
            reputation_url = str(data.get("reputation_url") or getattr(_proxy_cfg, "PROXY_WARMUP_REPUTATION_URL", "")).strip()
            anonymity_url = str(data.get("anonymity_url") or getattr(_proxy_cfg, "PROXY_WARMUP_ANONYMITY_URL", "")).strip()
            min_clean_score = data.get("min_clean_score", getattr(_proxy_cfg, "PROXY_WARMUP_MIN_CLEAN_SCORE", 80))
            max_latency = data.get("max_latency", getattr(_proxy_cfg, "PROXY_WARMUP_MAX_LATENCY", 8.0))
            exit_samples = data.get("exit_samples", getattr(_proxy_cfg, "PROXY_WARMUP_EXIT_SAMPLES", 3))
            recheck_clean = data.get("recheck_clean") if "recheck_clean" in data else getattr(
                _proxy_cfg, "PROXY_WARMUP_RECHECK_CLEAN_IPS", False
            )
            if isinstance(recheck_clean, str):
                recheck_clean = recheck_clean.strip().lower() in {"1", "true", "yes", "on"}
            else:
                recheck_clean = bool(recheck_clean)
            task_id = uuid.uuid4().hex
            state = {
                "task_id": task_id, "status": "running", "started_at": time.time(),
                "input_count": sum(1 for item in pool if str(item or "").strip()),
                "total": len({str(item or "").strip() for item in pool if str(item or "").strip()}),
                "completed": 0, "results": [], "recheck_results": [],
                "recheck_enabled": recheck_clean, "recheck_completed": 0, "recheck_total": 0,
                "phase": "initial", "error": "",
            }
            tasks = app.config.setdefault("PROXY_WARMUP_TASKS", {})
            cancel_events = app.config.setdefault("PROXY_WARMUP_CANCEL_EVENTS", {})
            # 仅保留最近任务，避免长期运行的 WebUI 累积完整预热明细。
            for old_id, old_state in list(tasks.items()):
                if old_id != task_id and old_state.get("status") != "running":
                    tasks.pop(old_id, None)
            tasks[task_id] = state
            cancel_events[task_id] = threading.Event()
            app.config["LAST_PROXY_WARMUP_LOG"] = state

            def _run_warmup():
                def _progress(event):
                    result_item = dict(event.get("result") or {})
                    result_item.pop("proxy_url", None)
                    phase = str(event.get("phase") or "initial")
                    state["phase"] = phase
                    state["completed"] = int(event.get("completed") or 0)
                    state["total"] = int(event.get("total") or state.get("total") or 0)
                    index = int(event.get("index") or 0)
                    if phase == "recheck":
                        rows = list(state.get("recheck_results") or [])
                        while len(rows) <= index:
                            rows.append(None)
                        rows[index] = result_item
                        state["recheck_results"] = rows
                        state["recheck_completed"] = int(event.get("phase_completed") or len([item for item in rows if isinstance(item, dict)]))
                        state["recheck_total"] = int(event.get("phase_total") or state.get("recheck_total") or 0)
                        state["recheck_healthy_total"] = sum(1 for item in rows if isinstance(item, dict) and item.get("healthy"))
                    else:
                        rows = list(state.get("results") or [])
                        while len(rows) <= index:
                            rows.append(None)
                        rows[index] = result_item
                        state["results"] = rows
                        completed_rows = [item for item in rows if isinstance(item, dict)]
                        state["healthy_total"] = sum(1 for item in completed_rows if item.get("healthy"))
                        state["available"] = state["healthy_total"]
                        state["failed"] = sum(1 for item in completed_rows if not item.get("healthy"))
                        state["dirty"] = sum(1 for item in completed_rows if not item.get("healthy") and item.get("removable", True))
                        state["inconclusive"] = sum(1 for item in completed_rows if not item.get("healthy") and not item.get("removable", True))
                        state["challenge_count"] = sum(1 for item in completed_rows if item.get("challenge_detected"))
                    app.config["LAST_PROXY_WARMUP_LOG"] = state

                try:
                    result = warmup_proxy_pool(
                        pool,
                        target_clean=max(0, int(target or 0)),
                        timeout=float(timeout or 12.0),
                        health_url=health_url,
                        reputation_url=reputation_url,
                        anonymity_url=anonymity_url,
                        min_clean_score=int(min_clean_score if min_clean_score is not None else 80),
                        max_latency=float(max_latency if max_latency is not None else 8.0),
                        exit_samples=max(1, min(5, int(exit_samples or 3))),
                        max_workers=max(1, int(workers or 4)),
                        recheck_clean=recheck_clean,
                        progress_callback=_progress,
                        cancel_event=cancel_events[task_id],
                    )
                    removed = 0
                    auto_delete = bool(getattr(_proxy_cfg, "PROXY_DELETE_UNHEALTHY_IPS", False))
                    if auto_delete:
                        unhealthy = set(result.get("unhealthy_proxy_urls") or [])
                        retained = [proxy for proxy in pool if proxy not in unhealthy]
                        if unhealthy:
                            persist_proxy_pool(retained)
                            removed = len(unhealthy)
                    challenge_count = sum(1 for item in result.get("failures", []) if item.get("challenge_detected"))
                    state.update({
                        "status": "completed", "ok": bool(result.get("ok")),
                        "checked_all": True, "checked_total": result.get("checked_total", result.get("total", 0)),
                        "healthy_total": result.get("healthy_total", result.get("available", 0)),
                        "selected_clean_count": result.get("selected_clean_count", result.get("clean", 0)),
                        "available": result.get("available", 0), "clean": result.get("clean", 0),
                        "failed": result.get("failed", 0), "target_clean": result.get("target_clean", 0),
                        "dirty": result.get("dirty", 0), "inconclusive": result.get("inconclusive", 0),
                        "duplicate_count": result.get("duplicate_count", 0), "challenge_count": challenge_count,
                        "removed": removed, "retained": len(pool) - removed, "auto_delete": auto_delete,
                        "results": [item for item in result.get("results", []) if isinstance(item, dict)],
                        "recheck_enabled": bool(result.get("recheck_enabled")),
                        "recheck_candidate_count": result.get("recheck_candidate_count", 0),
                        "recheck_checked_total": result.get("recheck_checked_total", 0),
                        "recheck_healthy_total": result.get("recheck_healthy_total", 0),
                        "recheck_results": [item for item in result.get("recheck_results", []) if isinstance(item, dict)],
                        "phase": "completed",
                        "completed_at": time.time(),
                    })
                except Exception as exc:
                    cancelled = cancel_events.get(task_id) and cancel_events[task_id].is_set()
                    state.update({"status": "cancelled" if cancelled else "failed", "ok": False, "error": str(exc), "completed_at": time.time()})
                app.config["LAST_PROXY_WARMUP_LOG"] = state

            threading.Thread(target=_run_warmup, name=f"proxy-warmup-{task_id[:8]}", daemon=True).start()
            return jsonify({
                "ok": True, "task_id": task_id, "status": "running", "total": state["total"],
                "recheck_enabled": recheck_clean,
            }), 202
        except Exception as exc:
            logger.warning("代理池预热失败：%s: %s", type(exc).__name__, exc)
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/proxy/warmup/log")
    def api_proxy_warmup_log():
        """读取本次 WebUI 进程最近一次预热的脱敏明细。"""
        result = app.config.get("LAST_PROXY_WARMUP_LOG")
        if not result:
            return jsonify({"ok": False, "error": "暂无预热日志"}), 404
        return jsonify({"ok": True, "log": result})

    @app.get("/api/proxy/warmup/<task_id>")
    def api_proxy_warmup_status(task_id):
        state = (app.config.get("PROXY_WARMUP_TASKS") or {}).get(str(task_id))
        if not state:
            return jsonify({"ok": False, "error": "预热任务不存在"}), 404
        return jsonify({"ok": True, "task": state})

    @app.post("/api/proxy/warmup/<task_id>/cancel")
    def api_proxy_warmup_cancel(task_id):
        task_id = str(task_id)
        state = (app.config.get("PROXY_WARMUP_TASKS") or {}).get(task_id)
        event = (app.config.get("PROXY_WARMUP_CANCEL_EVENTS") or {}).get(task_id)
        if not state or not event:
            return jsonify({"ok": False, "error": "预热任务不存在"}), 404
        if state.get("status") != "running":
            return jsonify({"ok": True, "task": state, "message": "预热任务已结束"})
        event.set()
        state["status"] = "cancelling"
        state["message"] = "正在终止预热，等待当前请求结束"
        return jsonify({"ok": True, "task": state, "message": "已发送终止预热信号"})

    @app.post("/api/agent/test")
    def api_agent_test():
        """校验页面 Agent 配置；local 模式执行本地配置检查，模型模式发最小探测请求。"""
        try:
            data = request.get_json(silent=True) or {}
            raw_updates = data.get("updates") if isinstance(data, dict) else {}
            # 测试按钮允许先保存页面 Agent 表单，再执行探测请求，避免用户
            # 必须先点击一次通用“保存”才能建立验证状态。只接受 Agent 白名单键。
            agent_updates = {
                str(key): value
                for key, value in (raw_updates.items() if isinstance(raw_updates, dict) else [])
                if str(key).startswith("PAGE_AGENT_") and str(key) != "PAGE_AGENT_VALIDATED"
            }
            saved = None
            if agent_updates:
                saved = config_editor.update_config(agent_updates)
                import config as _config_pkg

                _config_pkg.reload_all()
            from core.page_agent import test_configuration
            result = test_configuration()
            if saved is not None:
                result["saved"] = saved
            return jsonify(result), (200 if result.get("ok", result.get("configured", False)) else 400)
        except Exception as exc:
            logger.warning("页面 Agent 配置测试失败: %s: %s", type(exc).__name__, exc)
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/cloudmail/gen-token")
    def api_cloudmail_gen_token():
        """手动生成 CloudMail Authorization Token，并把本次填写的 CloudMail 配置一并写入 .env。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import gen_token
            from config.env_loader import write_env_values

            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            path = (data.get("path") or "/api/public/genToken").strip() or "/api/public/genToken"
            token = gen_token(
                email=admin_email,
                password=password,
                path=path,
                base_url=api_base,
            )
            updates = {"CLOUDMAIL_AUTH_TOKEN": token}
            # 生成 Token 时用户通常尚未点“保存配置”；这里同步保存本次填写的字段，
            # 避免 loadConfig() 后 API 地址/账号/密码被旧 .env 值覆盖。
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if path:
                updates["CLOUDMAIL_TOKEN_PATH"] = path
            written = write_env_values(updates)
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail Token 写入后热加载失败")
            return jsonify({
                "ok": True,
                "token": token,
                "written": written,
                "message": "CloudMail Token 已生成，且当前 CloudMail 配置已保存",
            })
        except Exception as exc:
            logger.exception("生成 CloudMail Token 失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/cloudmail/domains")
    def api_cloudmail_domains():
        """从 CloudMail 平台获取域名列表，并可写入 .env 作为本地缓存。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import fetch_domains
            from config.env_loader import write_env_values

            updates = {}
            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            token = (data.get("token") or "").strip()
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if token:
                updates["CLOUDMAIL_AUTH_TOKEN"] = token
            if updates:
                write_env_values(updates)
                import config as _config_pkg
                _config_pkg.reload_all()

            domains = fetch_domains(force=True)
            written = write_env_values({"CLOUDMAIL_DOMAINS": "\n".join(domains)})
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail 域名写入后热加载失败")
            return jsonify({
                "ok": True,
                "domains": domains,
                "count": len(domains),
                "written": written,
                "message": f"已获取 {len(domains)} 个 CloudMail 可用域名并保存",
            })
        except Exception as exc:
            logger.exception("获取 CloudMail 域名失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/config")
    def api_config_set():
        data = request.get_json(silent=True) or {}
        updates = data.get("updates") if isinstance(data.get("updates"), dict) else data
        if not isinstance(updates, dict) or not updates:
            return jsonify({"ok": False, "error": "无更新内容"}), 400
        try:
            result = config_editor.update_config(updates)
        except Exception as exc:
            logger.exception("配置写入失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

        # 写盘成功后立即热加载所有 config 子模块，让运行时代码看到新值。
        reload_ok = True
        reload_err = ""
        try:
            import config as _config_pkg
            _config_pkg.reload_all()
        except Exception as exc:
            reload_ok = False
            reload_err = f"{type(exc).__name__}: {exc}"
            logger.exception("配置热加载失败")

        return jsonify({
            "ok": True,
            "updated": result["updated"],
            "ignored": result["ignored"],
            "reloaded": reload_ok,
            "note": (
                "✅ 已保存并热加载，新值立即生效"
                if reload_ok
                else f"⚠️ 已写入文件但热加载失败（{reload_err}），需重启 Web 服务才能生效"
            ),
        })

    return app
