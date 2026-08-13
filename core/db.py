# -*- coding: utf-8 -*-
"""
SQLite 主持久化层。

SQLite 保存账号、邮箱池和任务的权威状态；根目录 JSON/TXT 继续作为兼容镜像：
    - 用于注册的邮箱.txt      仅保留可继续注册的邮箱素材
    - 注册成功的邮箱.txt      仅保存注册成功的邮箱素材，不追加 token
    - 注册成功的token.txt     每行只保存一个 access token
    - 用于注册的邮箱.json     Outlook 账号池完整状态
    - 注册成功的邮箱.json     注册成功账号完整状态
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from core.sqlite_store import SQLiteStore

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT
_LEGACY_DATA_DIR = _PROJECT_ROOT / "data"
_LOG_DIR = _PROJECT_ROOT / "注册日志"
_PLAN_CHECK_STALE_SECONDS = 120
_PLAN_CHECK_QUEUE_STALE_SECONDS = 1800

_OUTLOOK_JSON = _PROJECT_ROOT / "用于注册的邮箱.json"
_OUTLOOK_TXT = _PROJECT_ROOT / "用于注册的邮箱.txt"
_GENERIC_API_EMAIL_JSON = _PROJECT_ROOT / "用于注册的API邮箱.json"
_GENERIC_API_EMAIL_TXT = _PROJECT_ROOT / "用于注册的API邮箱.txt"
_ICLOUD_EMAIL_JSON = _PROJECT_ROOT / "用于注册的iCloud邮箱.json"
_ICLOUD_EMAIL_TXT = _PROJECT_ROOT / "用于注册的iCloud邮箱.txt"
_ACCOUNTS_JSON = _PROJECT_ROOT / "注册成功的邮箱.json"
_ACCOUNTS_TXT = _PROJECT_ROOT / "注册成功的邮箱.txt"
_TOKENS_TXT = _PROJECT_ROOT / "注册成功的token.txt"
_JOBS_JSON = _PROJECT_ROOT / "注册任务.json"
_REGISTRATION_BATCHES_JSON = _PROJECT_ROOT / "注册批次日志.json"
_VIEWER_HTML = _PROJECT_ROOT / "accounts_viewer.html"
_CODEX_DIR = _PROJECT_ROOT / "codex_accounts"
# 导出状态单独存：{ "codex-邮箱-plan.json": {"exported_at": "...", "exported_count": N} }
# 不污染 CPA 兼容的原文件
_CODEX_EXPORT_STATE = _PROJECT_ROOT / "codex_导出状态.json"
_DOMAIN_EMAIL_JSON = _PROJECT_ROOT / "用于注册的域名邮箱.json"
_GROUPS_JSON = _PROJECT_ROOT / "账号分组.json"

_SQLITE_PATH = _LEGACY_DATA_DIR / "registration.sqlite3"
_DEFAULT_SQLITE_PATH = _SQLITE_PATH
_DEFAULT_OUTLOOK_JSON = _OUTLOOK_JSON
_DEFAULT_GENERIC_API_EMAIL_JSON = _GENERIC_API_EMAIL_JSON
_DEFAULT_ICLOUD_EMAIL_JSON = _ICLOUD_EMAIL_JSON
_DEFAULT_ACCOUNTS_JSON = _ACCOUNTS_JSON
_DEFAULT_JOBS_JSON = _JOBS_JSON
_DEFAULT_REGISTRATION_BATCHES_JSON = _REGISTRATION_BATCHES_JSON
_DEFAULT_CODEX_EXPORT_STATE = _CODEX_EXPORT_STATE
_DEFAULT_DOMAIN_EMAIL_JSON = _DOMAIN_EMAIL_JSON
_DEFAULT_GROUPS_JSON = _GROUPS_JSON
_SQLITE_MIGRATION_MARKER = "json_to_sqlite_migration_completed_at"
_SQLITE_READY_PATH: Path | None = None
_SQLITE_STORE_INSTANCE: SQLiteStore | None = None

DEFAULT_ACCOUNT_GROUP = "默认分组"

_LEGACY_SQLITE = _LEGACY_DATA_DIR / "registrations.db"
_LEGACY_OUTLOOK_JSON = _LEGACY_DATA_DIR / "outlook_accounts.json"
_LEGACY_ACCOUNTS_JSON = _LEGACY_DATA_DIR / "registered_accounts.json"
_LEGACY_JOBS_JSON = _LEGACY_DATA_DIR / "registration_jobs.json"
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_storage() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    _ensure_storage()
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    _ensure_storage()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def _storage_backend() -> str:
    """Return the configured backend; JSON remains an emergency rollback mode."""
    value = os.getenv("TURB_STORAGE_BACKEND")
    if value is None:
        env_path = _PROJECT_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                key, separator, raw = line.partition("=")
                if separator and key.strip() == "TURB_STORAGE_BACKEND":
                    value = raw.strip().strip('"\'')
                    break
    return str(value or "sqlite").strip().lower()


def _uses_sqlite(current_path: Path, default_path: Path) -> bool:
    """Tests that redirect a JSON path keep their isolated file backend."""
    return (
        _storage_backend() != "json"
        and Path(_SQLITE_PATH) == Path(_DEFAULT_SQLITE_PATH)
        and Path(current_path) == Path(default_path)
    )


def _read_migration_value(path: Path, expected_type: type, default: Any) -> Any:
    """Strictly read a migration source so malformed files never become empty data."""
    if not path.exists():
        return default
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, expected_type):
        raise ValueError(f"迁移源格式错误: {path.name} 应为 {expected_type.__name__}")
    return data


def _read_migration_list(path: Path, legacy_path: Path | None = None) -> list[dict]:
    if path.exists():
        return _read_migration_value(path, list, [])
    if legacy_path is not None and legacy_path.exists():
        return _read_migration_value(legacy_path, list, [])
    return []


def _account_groups_from_rows(rows: list[dict], existing: list[dict] | None = None) -> list[dict]:
    """Build a stable group table while preserving names found in old account rows."""
    groups = [dict(row) for row in (existing or []) if isinstance(row, dict)]
    by_name = {
        str(row.get("name") or "").strip().casefold(): row
        for row in groups
        if str(row.get("name") or "").strip()
    }
    now = _now()
    if DEFAULT_ACCOUNT_GROUP.casefold() not in by_name:
        group = {
            "id": 1,
            "name": DEFAULT_ACCOUNT_GROUP,
            "created_at": now,
            "updated_at": now,
            "is_default": True,
        }
        groups.insert(0, group)
        by_name[DEFAULT_ACCOUNT_GROUP.casefold()] = group
    for row in rows:
        name = str(row.get("group_name") or DEFAULT_ACCOUNT_GROUP).strip() or DEFAULT_ACCOUNT_GROUP
        key = name.casefold()
        if key in by_name:
            continue
        next_id = max((int(item.get("id") or 0) for item in groups), default=0) + 1
        group = {"id": next_id, "name": name, "created_at": now, "updated_at": now, "is_default": False}
        groups.append(group)
        by_name[key] = group
    for row in groups:
        row["is_default"] = str(row.get("name") or "").strip().casefold() == DEFAULT_ACCOUNT_GROUP.casefold()
    return sorted(groups, key=lambda row: (not bool(row.get("is_default")), int(row.get("id") or 0)))


def _sqlite_source_snapshot() -> tuple[dict[str, list[dict]], dict[str, Any]]:
    collections = {
        "outlook_pool": _read_migration_list(_OUTLOOK_JSON, _LEGACY_OUTLOOK_JSON),
        "generic_api_email_pool": _read_migration_list(_GENERIC_API_EMAIL_JSON),
        "icloud_email_pool": _read_migration_list(_ICLOUD_EMAIL_JSON),
        "registered_accounts": _read_migration_list(_ACCOUNTS_JSON, _LEGACY_ACCOUNTS_JSON),
        "registration_jobs": _read_migration_list(_JOBS_JSON, _LEGACY_JOBS_JSON),
        "registration_batches": _read_migration_list(_REGISTRATION_BATCHES_JSON),
        "domain_email_pool": _read_migration_list(_DOMAIN_EMAIL_JSON),
    }
    collections["account_groups"] = _account_groups_from_rows(
        collections["registered_accounts"],
        _read_migration_list(_GROUPS_JSON),
    )
    documents = {
        "codex_export_state": _read_migration_value(_CODEX_EXPORT_STATE, dict, {}),
    }
    return collections, documents


def initialize_sqlite_storage(*, force: bool = False) -> dict[str, int]:
    """Create SQLite and atomically import the current JSON state once."""
    global _SQLITE_READY_PATH, _SQLITE_STORE_INSTANCE
    store = SQLiteStore(_SQLITE_PATH)
    with _LOCK:
        marker = store.get_metadata(_SQLITE_MIGRATION_MARKER)
        if force or marker is None:
            collections, documents = _sqlite_source_snapshot()
            store.replace_all(
                collections,
                documents=documents,
                metadata={_SQLITE_MIGRATION_MARKER: _now()},
            )
        _SQLITE_READY_PATH = Path(_SQLITE_PATH)
        _SQLITE_STORE_INSTANCE = store
        return store.counts()


def _sqlite_store() -> SQLiteStore:
    global _SQLITE_READY_PATH, _SQLITE_STORE_INSTANCE
    path = Path(_SQLITE_PATH)
    if _SQLITE_READY_PATH != path or _SQLITE_STORE_INSTANCE is None:
        initialize_sqlite_storage()
    assert _SQLITE_STORE_INSTANCE is not None
    return _SQLITE_STORE_INSTANCE


def _next_id(items: list[dict]) -> int:
    ids = [int(item.get("id") or 0) for item in items]
    return (max(ids) if ids else 0) + 1


def _outlook_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        row.get("password") or "",
        row.get("client_id") or "",
        row.get("refresh_token") or "",
    ])


def _generic_api_email_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        row.get("code_url") or "",
    ])


def _icloud_email_line(row: dict) -> str:
    """生成 iCloud 邮箱池文本行：邮箱----HTML 取码地址。"""
    return "----".join([
        row.get("email") or "",
        row.get("code_url") or "",
    ])


def _totp_viewer_url(secret: str | None) -> str:
    secret = str(secret or "").strip()
    return f"https://2fa.fb.tools/{quote(secret, safe='')}" if secret else ""


def _registered_account_parts(row: dict) -> list[str]:
    parts = [str(row.get("email") or "").strip()]
    password = str(row.get("registration_password") or "").strip()
    viewer = _totp_viewer_url(row.get("totp_secret"))
    if password:
        parts.append(password)
    if viewer:
        parts.append(viewer)
    return parts


def _account_line(row: dict) -> str:
    parts = _registered_account_parts(row)
    token = str(row.get("access_token") or "").strip()
    if token:
        parts.append(token)
    return "----".join(parts)


def _registered_email_line(row: dict) -> str:
    """生成 邮箱----账号密码----2FA查看器；token 单独保存。"""
    return "----".join(_registered_account_parts(row))


def _sync_outlook_txt(rows: list[dict]) -> None:
    available_rows = [r for r in rows if r.get("status") == "available"]
    lines = [_outlook_line(r) for r in sorted(available_rows, key=lambda x: int(x.get("id") or 0))]
    _OUTLOOK_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_generic_api_email_txt(rows: list[dict]) -> None:
    available_rows = [r for r in rows if r.get("status") == "available"]
    lines = [_generic_api_email_line(r) for r in sorted(available_rows, key=lambda x: int(x.get("id") or 0))]
    _GENERIC_API_EMAIL_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_icloud_email_txt(rows: list[dict]) -> None:
    """把可用 iCloud 邮箱同步到兼容文本文件。"""
    available_rows = [r for r in rows if r.get("status") == "available"]
    lines = [_icloud_email_line(r) for r in sorted(available_rows, key=lambda x: int(x.get("id") or 0))]
    _ICLOUD_EMAIL_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_accounts_txt(rows: list[dict]) -> None:
    lines = [_registered_email_line(r) for r in sorted(rows, key=lambda x: int(x.get("id") or 0))]
    _ACCOUNTS_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_tokens_txt(rows: list[dict]) -> None:
    tokens = [
        r.get("access_token") or ""
        for r in sorted(rows, key=lambda x: int(x.get("id") or 0))
        if r.get("access_token")
    ]
    _TOKENS_TXT.write_text(("\n".join(tokens) + ("\n" if tokens else "")), encoding="utf-8")


def _viewer_snapshot(outlook_rows: list[dict], account_rows: list[dict]) -> dict:
    account_by_email = {
        (a.get("email") or "").lower(): a
        for a in account_rows
    }
    return {
        "generated_at": _now(),
        "accounts": [
            _decorate_account(r)
            for r in sorted(account_rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        ],
        "outlook": [
            _decorate_outlook(r, account_by_email)
            for r in sorted(outlook_rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        ],
        "summary": {
            "accounts": len(account_rows),
            "outlook_total": len(outlook_rows),
            "outlook_available": sum(1 for r in outlook_rows if r.get("status") == "available"),
            "outlook_used": sum(1 for r in outlook_rows if r.get("status") == "used"),
            "outlook_failed": sum(1 for r in outlook_rows if r.get("status") == "failed"),
        },
    }


def _render_static_viewer(outlook_rows: list[dict] | None = None, account_rows: list[dict] | None = None) -> Path:
    """生成可直接双击打开的静态账号查看页。"""
    outlook_rows = _load_outlook() if outlook_rows is None else outlook_rows
    account_rows = _load_accounts() if account_rows is None else account_rows
    snapshot = _viewer_snapshot(outlook_rows, account_rows)
    data_json = json.dumps(snapshot, ensure_ascii=False).replace("</", "<\\/")
    title = escape(f"账号查看器 - {snapshot['generated_at']}")
    html_text = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    * {{ box-sizing: border-box; }}
    :root {{
      --bg: #eef3f8;
      --surface: #ffffff;
      --soft: #f7f9fc;
      --text: #172033;
      --muted: #667085;
      --line: #d9e2ec;
      --blue: #2563eb;
      --green: #16803c;
      --red: #c2413a;
      --amber: #b7791f;
    }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      padding: 22px 28px;
      background: #101827;
      color: #fff;
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: center;
      flex-wrap: wrap;
    }}
    h1, h2, p {{ margin: 0; }}
    h1 {{ font-size: 28px; }}
    .meta {{ margin-top: 6px; color: #b8c7d9; font-size: 13px; }}
    .stats {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .stat {{
      min-width: 116px;
      padding: 10px 12px;
      border: 1px solid rgba(255,255,255,.16);
      border-radius: 8px;
      background: rgba(255,255,255,.08);
    }}
    .stat span {{ display: block; color: #b8c7d9; font-size: 12px; }}
    .stat strong {{ display: block; margin-top: 4px; font-size: 18px; }}
    main {{ width: min(1500px, calc(100vw - 32px)); margin: 16px auto 30px; display: grid; gap: 16px; }}
    .toolbar, section {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: 0 8px 22px rgba(15,23,42,.06);
    }}
    .toolbar {{ padding: 14px; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
    .search {{ min-width: min(520px, 100%); flex: 1; }}
    input {{
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 12px;
      font: inherit;
    }}
    .buttons {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    button {{
      min-height: 32px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      padding: 0 12px;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{ background: var(--soft); }}
    button.primary {{ border-color: var(--blue); background: var(--blue); color: #fff; }}
    button.good {{ border-color: #2f855a; background: #edf8f1; color: #166534; }}
    button:disabled {{ color: #98a2b3; cursor: not-allowed; background: #f2f4f7; }}
    .head {{ padding: 14px 16px; border-bottom: 1px solid var(--line); background: var(--soft); }}
    .head p {{ margin-top: 4px; color: var(--muted); font-size: 12px; }}
    .table-wrap {{ overflow: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #edf1f5; text-align: left; white-space: nowrap; vertical-align: middle; }}
    th {{ position: sticky; top: 0; background: #fbfcfe; color: #475467; z-index: 1; font-size: 12px; }}
    tr:hover td {{ background: #fbfdff; }}
    .main-cell {{ font-weight: 700; }}
    .sub-cell {{ margin-top: 3px; color: var(--muted); font-size: 12px; }}
    .mono {{ font-family: ui-monospace, "JetBrains Mono", Consolas, monospace; font-size: 12px; }}
    .muted {{ color: var(--muted); }}
    .pill {{ display: inline-flex; min-width: 48px; justify-content: center; padding: 3px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; }}
    .status-available {{ color: var(--blue); background: #eef4ff; }}
    .status-used {{ color: #475467; background: #f2f4f7; }}
    .status-failed {{ color: var(--red); background: #fff0ef; }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    #toast {{
      position: fixed;
      right: 18px;
      bottom: 18px;
      padding: 10px 14px;
      border-radius: 8px;
      background: #101827;
      color: #fff;
      box-shadow: 0 14px 30px rgba(15,23,42,.24);
      opacity: 0;
      transform: translateY(8px);
      pointer-events: none;
      transition: opacity .18s ease, transform .18s ease;
    }}
    #toast.show {{ opacity: 1; transform: translateY(0); }}
    @media (max-width: 820px) {{
      header {{ align-items: flex-start; }}
      .stats {{ width: 100%; }}
      .stat {{ flex: 1; }}
    }}
  </style>
</head>
<body>
<header>
  <div>
    <h1>账号查看器</h1>
    <p class="meta">静态快照，无需启动 Web Server。生成时间：<span id="generated"></span></p>
  </div>
  <div class="stats">
    <div class="stat"><span>已完成</span><strong id="statAccounts">0</strong></div>
    <div class="stat"><span>邮箱总数</span><strong id="statOutlook">0</strong></div>
    <div class="stat"><span>可用邮箱</span><strong id="statAvailable">0</strong></div>
  </div>
</header>
<main>
  <div class="toolbar">
    <div class="search"><input id="q" placeholder="搜索邮箱、token、clientId、状态"></div>
    <div class="buttons">
      <button class="primary" id="copyAllTokens">复制全部 Token</button>
      <button class="good" id="copyAllLines">复制全部整行</button>
      <button id="copyAllEmails">复制全部邮箱素材</button>
    </div>
  </div>
  <section>
    <div class="head">
      <h2>已完成账号</h2>
      <p>整行格式：邮箱----密码----clientId----邮箱刷新令牌----accessToken----totpSecret（如有）</p>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>ID</th><th>邮箱</th><th>来源</th><th>Token</th><th>备注</th><th>2FA</th><th>创建时间</th><th>操作</th></tr></thead>
        <tbody id="accountsBody"></tbody>
      </table>
    </div>
  </section>
  <section>
    <div class="head">
      <h2>邮箱素材库</h2>
      <p>原始格式：邮箱----密码----clientId----邮箱刷新令牌；注册完成后可直接复制对应 Token 或整行。</p>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>邮箱</th><th>状态</th><th>Token</th><th>导入时间</th><th>已用时间</th><th>操作</th></tr></thead>
        <tbody id="outlookBody"></tbody>
      </table>
    </div>
  </section>
</main>
<div id="toast"></div>
<script id="snapshot" type="application/json">{data_json}</script>
<script>
const SNAPSHOT = JSON.parse(document.getElementById('snapshot').textContent);
const $ = (s) => document.querySelector(s);
let copySeq = 0;
const copyStore = new Map();

function fmt(v) {{ return v == null || v === '' ? '-' : String(v); }}
function esc(v) {{
  return fmt(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}
function short(v, n = 34) {{
  const s = v || '';
  return s.length > n ? `${{s.slice(0, n)}}...` : s;
}}
function copyId(v) {{
  if (!v) return '';
  const id = `c${{++copySeq}}`;
  copyStore.set(id, v);
  return id;
}}
function btn(label, value, cls = '') {{
  const id = copyId(value);
  return `<button class="${{cls}}" data-copy-id="${{id}}" ${{id ? '' : 'disabled'}}>${{label}}</button>`;
}}
function pill(status) {{
  const map = {{ available: '可用', used: '已用', failed: '失败' }};
  const label = map[status] || status || '-';
  return `<span class="pill status-${{esc(status)}}">${{esc(label)}}</span>`;
}}
function showToast(text) {{
  const toast = $('#toast');
  toast.textContent = text;
  toast.classList.add('show');
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove('show'), 1400);
}}
async function copyText(text) {{
  if (!text) return;
  if (navigator.clipboard && window.isSecureContext) {{
    await navigator.clipboard.writeText(text);
  }} else {{
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }}
  showToast('已复制');
}}
function haystack(row) {{
  return Object.values(row).join('\\n').toLowerCase();
}}
function render() {{
  copyStore.clear();
  copySeq = 0;
  const q = $('#q').value.trim().toLowerCase();
  const accounts = SNAPSHOT.accounts.filter((r) => !q || haystack(r).includes(q));
  const outlook = SNAPSHOT.outlook.filter((r) => !q || haystack(r).includes(q));
  $('#generated').textContent = SNAPSHOT.generated_at;
  $('#statAccounts').textContent = SNAPSHOT.summary.accounts;
  $('#statOutlook').textContent = SNAPSHOT.summary.outlook_total;
  $('#statAvailable').textContent = SNAPSHOT.summary.outlook_available;
  $('#accountsBody').innerHTML = accounts.map((r) => `
    <tr>
      <td class="muted">#${{esc(r.id)}}</td>
      <td><div class="main-cell">${{esc(r.email)}}</div><div class="sub-cell">${{esc(r.user_name || '-')}}</div></td>
      <td>${{esc(r.email_source || '-')}}</td>
      <td><span class="mono">${{esc(short(r.access_token || '', 42))}}</span></td>
      <td title="${{esc(r.note || '')}}">${{r.note ? esc(short(r.note, 60)) : '<span class="muted">-</span>'}}</td>
      <td>${{r.totp_secret ? '已启用' : '<span class="muted">未启用</span>'}}</td>
      <td class="muted">${{esc(r.created_at || '-')}}</td>
      <td class="actions">${{btn('复制Token', r.access_token, 'primary')}} ${{btn('复制整行', r.copy_line, 'good')}}</td>
    </tr>`).join('');
  $('#outlookBody').innerHTML = outlook.map((r) => `
    <tr>
      <td><div class="main-cell">${{esc(r.email)}}</div><div class="sub-cell mono">${{esc(short(r.copy_line, 76))}}</div></td>
      <td>${{pill(r.status)}}</td>
      <td><span class="mono">${{esc(short(r.access_token || '', 36) || '未生成')}}</span></td>
      <td class="muted">${{esc(r.imported_at || r.created_at || '-')}}</td>
      <td class="muted">${{esc(r.used_at || '-')}}</td>
      <td class="actions">${{btn('复制邮箱', r.copy_line)}} ${{btn('复制Token', r.access_token, 'primary')}} ${{btn('复制整行', r.account_copy_line, 'good')}}</td>
    </tr>`).join('');
}}
document.addEventListener('click', (e) => {{
  const target = e.target.closest('[data-copy-id]');
  if (!target) return;
  copyText(copyStore.get(target.dataset.copyId));
}});
$('#q').addEventListener('input', render);
$('#copyAllTokens').addEventListener('click', () => copyText(SNAPSHOT.accounts.map((r) => r.access_token).filter(Boolean).join('\\n')));
$('#copyAllLines').addEventListener('click', () => copyText(SNAPSHOT.accounts.map((r) => r.copy_line).filter(Boolean).join('\\n')));
$('#copyAllEmails').addEventListener('click', () => copyText(SNAPSHOT.outlook.map((r) => r.copy_line).filter(Boolean).join('\\n')));
render();
</script>
</body>
</html>
"""
    tmp = _VIEWER_HTML.with_suffix(".html.tmp")
    tmp.write_text(html_text, encoding="utf-8")
    try:
        tmp.replace(_VIEWER_HTML)
        return _VIEWER_HTML
    except PermissionError:
        # Windows 下如果目标 HTML 正被浏览器或编辑器短暂占用，原子替换可能失败。
        # 先尝试直接覆盖；仍失败时写一个时间戳快照，避免注册流程被查看页刷新阻断。
        try:
            _VIEWER_HTML.write_text(html_text, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
            return _VIEWER_HTML
        except PermissionError:
            fallback = _DATA_DIR / f"accounts_viewer_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
            fallback.write_text(html_text, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
            return fallback


def _load_outlook() -> list[dict]:
    if _uses_sqlite(_OUTLOOK_JSON, _DEFAULT_OUTLOOK_JSON):
        return _sqlite_store().load_records("outlook_pool")
    rows = _read_json(_OUTLOOK_JSON, None)
    if not isinstance(rows, list):
        rows = _read_json(_LEGACY_OUTLOOK_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_outlook(rows: list[dict]) -> None:
    if _uses_sqlite(_OUTLOOK_JSON, _DEFAULT_OUTLOOK_JSON):
        _sqlite_store().replace_records("outlook_pool", rows)
    _write_json(_OUTLOOK_JSON, rows)
    _sync_outlook_txt(rows)
    _render_static_viewer(outlook_rows=rows)


def _load_generic_api_emails() -> list[dict]:
    if _uses_sqlite(_GENERIC_API_EMAIL_JSON, _DEFAULT_GENERIC_API_EMAIL_JSON):
        return _sqlite_store().load_records("generic_api_email_pool")
    rows = _read_json(_GENERIC_API_EMAIL_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_generic_api_emails(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _generic_api_email_line(row)
    if _uses_sqlite(_GENERIC_API_EMAIL_JSON, _DEFAULT_GENERIC_API_EMAIL_JSON):
        _sqlite_store().replace_records("generic_api_email_pool", rows)
    _write_json(_GENERIC_API_EMAIL_JSON, rows)
    _sync_generic_api_email_txt(rows)


def _load_icloud_emails() -> list[dict]:
    """读取独立 iCloud 邮箱池。"""
    if _uses_sqlite(_ICLOUD_EMAIL_JSON, _DEFAULT_ICLOUD_EMAIL_JSON):
        return _sqlite_store().load_records("icloud_email_pool")
    rows = _read_json(_ICLOUD_EMAIL_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_icloud_emails(rows: list[dict]) -> None:
    """持久化 iCloud 邮箱池并刷新文本镜像。"""
    for row in rows:
        row["copy_line"] = _icloud_email_line(row)
    if _uses_sqlite(_ICLOUD_EMAIL_JSON, _DEFAULT_ICLOUD_EMAIL_JSON):
        _sqlite_store().replace_records("icloud_email_pool", rows)
    _write_json(_ICLOUD_EMAIL_JSON, rows)
    _sync_icloud_email_txt(rows)


def _load_accounts() -> list[dict]:
    if _uses_sqlite(_ACCOUNTS_JSON, _DEFAULT_ACCOUNTS_JSON):
        rows = _sqlite_store().load_records("registered_accounts")
        return _ensure_account_group_storage(rows)
    rows = _read_json(_ACCOUNTS_JSON, None)
    if not isinstance(rows, list):
        rows = _read_json(_LEGACY_ACCOUNTS_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_accounts(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _account_line(row)
    if _uses_sqlite(_ACCOUNTS_JSON, _DEFAULT_ACCOUNTS_JSON):
        _sqlite_store().replace_records("registered_accounts", rows)
    _write_json(_ACCOUNTS_JSON, rows)
    _sync_accounts_txt(rows)
    _sync_tokens_txt(rows)
    _render_static_viewer(account_rows=rows)


def _load_group_rows() -> list[dict]:
    if _uses_sqlite(_GROUPS_JSON, _DEFAULT_GROUPS_JSON):
        return _sqlite_store().load_records("account_groups")
    rows = _read_json(_GROUPS_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_group_rows(rows: list[dict]) -> None:
    if _uses_sqlite(_GROUPS_JSON, _DEFAULT_GROUPS_JSON):
        _sqlite_store().replace_records("account_groups", rows)
    _write_json(_GROUPS_JSON, rows)


def _ensure_account_group_storage(rows: list[dict] | None = None) -> list[dict]:
    """Backfill default group for pre-group accounts and persist discovered names."""
    account_rows = rows if rows is not None else _load_accounts()
    group_rows = _load_group_rows()
    normalized = False
    for row in account_rows:
        name = str(row.get("group_name") or "").strip()
        if not name:
            row["group_name"] = DEFAULT_ACCOUNT_GROUP
            normalized = True
    groups = _account_groups_from_rows(account_rows, group_rows)
    current_names = {str(row.get("name") or "").strip().casefold() for row in group_rows}
    next_names = {str(row.get("name") or "").strip().casefold() for row in groups}
    if normalized:
        _save_accounts(account_rows)
    if current_names != next_names or len(group_rows) != len(groups):
        _save_group_rows(groups)
    return account_rows


def _find_group(rows: list[dict], group_id: int | None = None, name: str | None = None) -> dict | None:
    target_name = str(name or "").strip().casefold()
    target_id = int(group_id) if group_id is not None else None
    for row in rows:
        if target_id is not None and int(row.get("id") or 0) == target_id:
            return row
        if target_name and str(row.get("name") or "").strip().casefold() == target_name:
            return row
    return None


def list_account_groups() -> list[dict]:
    """Return groups with account counts; the default group is always first."""
    with _LOCK:
        rows = _ensure_account_group_storage()
        groups = _load_group_rows()
        counts: dict[str, int] = {}
        for row in rows:
            key = str(row.get("group_name") or DEFAULT_ACCOUNT_GROUP).strip().casefold()
            counts[key] = counts.get(key, 0) + 1
        out = [{
            "id": "all",
            "name": "全部",
            "count": len(rows),
            "is_default": False,
            "is_all": True,
        }]
        for group in _account_groups_from_rows(rows, groups):
            item = dict(group)
            item["count"] = counts.get(str(group.get("name") or "").strip().casefold(), 0)
            out.append(item)
        return out


def create_account_group(name: str) -> dict:
    """Create an empty account group."""
    clean = str(name or "").strip()
    if not clean:
        raise ValueError("分组名称不能为空")
    if len(clean) > 80:
        raise ValueError("分组名称最多 80 个字符")
    with _LOCK:
        _ensure_account_group_storage()
        rows = _load_group_rows()
        if clean.casefold() == DEFAULT_ACCOUNT_GROUP.casefold():
            raise ValueError("默认分组不允许重复创建")
        if _find_group(rows, name=clean):
            raise ValueError("分组名称已存在")
        now = _now()
        group = {
            "id": max((int(row.get("id") or 0) for row in rows), default=0) + 1,
            "name": clean,
            "count": 0,
            "created_at": now,
            "updated_at": now,
            "is_default": False,
        }
        rows.append(group)
        _save_group_rows(rows)
        return dict(group)


def rename_account_group(group_id: int, name: str) -> dict:
    """Rename an empty or populated non-default group and update its accounts."""
    clean = str(name or "").strip()
    if not clean:
        raise ValueError("分组名称不能为空")
    if len(clean) > 80:
        raise ValueError("分组名称最多 80 个字符")
    with _LOCK:
        accounts = _ensure_account_group_storage()
        groups = _load_group_rows()
        group = _find_group(groups, group_id=group_id)
        if group is None:
            raise ValueError("分组不存在")
        if bool(group.get("is_default")) or str(group.get("name") or "").strip().casefold() == DEFAULT_ACCOUNT_GROUP.casefold():
            raise ValueError("默认分组不允许修改")
        duplicate = _find_group(groups, name=clean)
        if duplicate is not None and int(duplicate.get("id") or 0) != int(group.get("id") or 0):
            raise ValueError("分组名称已存在")
        old_name = str(group.get("name") or "")
        now = _now()
        group["name"] = clean
        group["updated_at"] = now
        for row in accounts:
            if str(row.get("group_name") or "").strip().casefold() == old_name.strip().casefold():
                row["group_name"] = clean
                row["updated_at"] = now
        _save_accounts(accounts)
        _save_group_rows(groups)
        updated = dict(group)
        updated["count"] = sum(1 for row in accounts if str(row.get("group_name") or "").strip().casefold() == clean.casefold())
        return updated


def delete_account_group(group_id: int) -> dict:
    """Delete only an empty, non-default group."""
    with _LOCK:
        accounts = _ensure_account_group_storage()
        groups = _load_group_rows()
        group = _find_group(groups, group_id=group_id)
        if group is None:
            raise ValueError("分组不存在")
        if bool(group.get("is_default")) or str(group.get("name") or "").strip().casefold() == DEFAULT_ACCOUNT_GROUP.casefold():
            raise ValueError("默认分组不允许删除")
        name_key = str(group.get("name") or "").strip().casefold()
        count = sum(1 for row in accounts if str(row.get("group_name") or DEFAULT_ACCOUNT_GROUP).strip().casefold() == name_key)
        if count:
            raise ValueError("分组内有账号时不允许删除")
        groups = [row for row in groups if int(row.get("id") or 0) != int(group.get("id") or 0)]
        _save_group_rows(groups)
        return {"id": int(group.get("id") or 0), "name": group.get("name"), "count": 0}


def move_accounts_to_group(account_ids: list[int] | None, group_id: int) -> tuple[list[dict], list[dict]]:
    """Move selected accounts to an existing group."""
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        accounts = _ensure_account_group_storage()
        groups = _load_group_rows()
        group = _find_group(groups, group_id=group_id)
        if group is None:
            raise ValueError("目标分组不存在")
        target = str(group.get("name") or DEFAULT_ACCOUNT_GROUP)
        seen: set[int] = set()
        now = _now()
        for row in accounts:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["group_name"] = target
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "group_name": target})
            seen.add(row_id)
        for missing in ids - seen:
            skipped.append({"id": missing, "reason": "账号不存在"})
        if updated:
            _save_accounts(accounts)
        _save_group_rows(groups)
    return updated, skipped


def _load_jobs() -> list[dict]:
    if _uses_sqlite(_JOBS_JSON, _DEFAULT_JOBS_JSON):
        return _sqlite_store().load_records("registration_jobs")
    rows = _read_json(_JOBS_JSON, None)
    if not isinstance(rows, list):
        rows = _read_json(_LEGACY_JOBS_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_jobs(rows: list[dict]) -> None:
    if _uses_sqlite(_JOBS_JSON, _DEFAULT_JOBS_JSON):
        _sqlite_store().replace_records("registration_jobs", rows)
    _write_json(_JOBS_JSON, rows)


def _load_registration_batches() -> list[dict]:
    """读取注册批次历史；SQLite 为主存储，JSON 保留兼容镜像。"""
    if _uses_sqlite(_REGISTRATION_BATCHES_JSON, _DEFAULT_REGISTRATION_BATCHES_JSON):
        return _sqlite_store().load_records("registration_batches")
    rows = _read_json(_REGISTRATION_BATCHES_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_registration_batches(rows: list[dict]) -> None:
    """保存注册批次历史并同步 JSON 镜像。"""
    if _uses_sqlite(_REGISTRATION_BATCHES_JSON, _DEFAULT_REGISTRATION_BATCHES_JSON):
        _sqlite_store().replace_records("registration_batches", rows)
    _write_json(_REGISTRATION_BATCHES_JSON, rows)


def _find_by_email(rows: list[dict], email: str) -> dict | None:
    target = (email or "").lower()
    return next((r for r in rows if (r.get("email") or "").lower() == target), None)


def _country_code_from_value(value: Any) -> str:
    """从 GeoIP/代理池常见格式中提取两位出口国家码。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    # 仅接受 ISO 3166-1 alpha-2，避免把代理用户名、节点标签里的任意
    # 两个字母误显示成国家（例如 ``ab.proxy.example``）。
    valid_codes = _ISO_ALPHA2_CODES
    if re.fullmatch(r"[A-Za-z]{2}", raw):
        return _normalize_country_code(raw)
    match = re.search(
        r"(?:region|country|location)[-_:=/ ]*([A-Za-z]{2})(?=[-_.:/]|$)",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return _normalize_country_code(match.group(1))
    try:
        parsed = urlsplit(raw if "://" in raw else f"//{raw}")
        host = parsed.hostname or ""
    except ValueError:
        host = ""
    first_label = host.split(".", 1)[0]
    if re.fullmatch(r"[A-Za-z]{2}", first_label):
        return _normalize_country_code(first_label)
    # Browser Use / Skyvern 保存的 provider:country 形式。
    tail = re.split(r"[:/_-]", raw.rsplit("@", 1)[-1])[-1]
    return _normalize_country_code(tail) if re.fullmatch(r"[A-Za-z]{2}", tail) else ""


# ISO 3166-1 alpha-2 codes used by GeoIP providers and proxy pools. Keeping
# this local avoids a runtime dependency just for a small display badge.
_ISO_ALPHA2_CODES = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split())
_COUNTRY_CODE_ALIASES = {"UK": "GB"}  # 常见代理供应商写法，ISO 正式码为 GB。


def _normalize_country_code(value: str) -> str:
    code = str(value or "").strip().upper()
    code = _COUNTRY_CODE_ALIASES.get(code, code)
    return code if code in _ISO_ALPHA2_CODES else ""


def _account_proxy_geo(row: dict) -> dict:
    """返回账号落库的真实出口 GeoIP；兼容旧的嵌套 extra_json 记录。"""
    candidates: list[dict] = []
    direct = row.get("proxy_geo")
    if isinstance(direct, dict):
        candidates.append(direct)
    try:
        extra = json.loads(str(row.get("extra_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        extra = {}

    def walk(value: Any, key_hint: str = "") -> None:
        if isinstance(value, dict):
            hint = key_hint.lower()
            if any(marker in hint for marker in ("geo", "locale", "location", "open_result")):
                candidates.append(value)
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    walk(child, str(key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key_hint)

    walk(extra)
    for geo in candidates:
        code = ""
        for key in ("country_code", "countryCode", "country"):
            code = _country_code_from_value(geo.get(key))
            if code:
                break
        if code:
            return {
                "country_code": code,
                "country": str(geo.get("country_name") or geo.get("country") or "").strip(),
                "region": str(geo.get("region") or geo.get("regionName") or "").strip(),
                "city": str(geo.get("city") or "").strip(),
                "ip": str(geo.get("ip") or geo.get("query") or "").strip(),
            }
    return {}


def _account_proxy_country_code(row: dict) -> str:
    """返回账号注册代理出口国家码，优先使用落库 GeoIP，兼容旧代理字符串。"""
    geo = _account_proxy_geo(row)
    if geo.get("country_code"):
        return geo["country_code"]
    for key in ("proxy_country_code", "proxy_country"):
        code = _country_code_from_value(row.get(key))
        if code:
            return code
    try:
        extra = json.loads(str(row.get("extra_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        extra = {}
    if isinstance(extra, dict):
        profile = extra.get("browser_profile")
        if isinstance(profile, dict):
            profile_geo = profile.get("geo")
            if isinstance(profile_geo, dict):
                for key in ("country_code", "countryCode", "country"):
                    code = _country_code_from_value(profile_geo.get(key))
                    if code:
                        return code
        for value in extra.values():
            if isinstance(value, dict):
                for key in ("proxy_country_code", "country_code", "country"):
                    code = _country_code_from_value(value.get(key))
                    if code:
                        return code
    return _country_code_from_value(row.get("proxy_used"))


def _decorate_account(row: dict) -> dict:
    out = dict(row)
    out["note"] = out.get("note") or ""
    out["note_updated_at"] = out.get("note_updated_at") or ""
    out["link_completed"] = (
        bool(out.get("link_completed"))
        if "link_completed" in out
        else bool(out.get("extract_link_ok")) or out.get("extract_link_status") == "success"
    )
    out["sms_completed"] = (
        bool(out.get("sms_completed"))
        if "sms_completed" in out
        else out.get("codex_status") == "success"
    )
    out["proxy_country_code"] = _account_proxy_country_code(out)
    proxy_geo = _account_proxy_geo(out)
    if proxy_geo:
        out["proxy_country_name"] = proxy_geo.get("country") or ""
        out["proxy_region"] = proxy_geo.get("region") or ""
        out["proxy_city"] = proxy_geo.get("city") or ""
        out["proxy_exit_ip"] = proxy_geo.get("ip") or ""
    if str(out.get("email_source") or "").strip().lower() == "icloud":
        icloud_row = _find_by_email(_load_icloud_emails(), str(out.get("email") or ""))
        out["icloud_code_url_available"] = bool(str((icloud_row or {}).get("code_url") or "").strip())
    plan_status = out.get("plan_check_status")
    if plan_status in {"queued", "running"}:
        try:
            stamp_key = "plan_check_queued_at" if plan_status == "queued" else "plan_check_started_at"
            stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if plan_status == "queued" else _PLAN_CHECK_STALE_SECONDS
            started_at = datetime.fromisoformat(str(out.get(stamp_key) or ""))
            if (datetime.now() - started_at).total_seconds() >= stale_after:
                out["plan_check_status"] = "failed"
                out["plan_check_error"] = "上次套餐查询状态已超时，可重新查询"
                out["plan_check_stale"] = True
        except (TypeError, ValueError):
            out["plan_check_status"] = "failed"
            out["plan_check_error"] = "上次套餐查询状态异常，可重新查询"
            out["plan_check_stale"] = True
    out["copy_line"] = _account_line(out)
    return out


def _account_matches_plan_filter(row: dict, plan_filter: str | None = None) -> bool:
    """账号套餐过滤。plus 表示已开通 Plus（兼容 plus/chatgpt_plus/plus_trial 等标记）。"""
    f = str(plan_filter or "").strip().lower()
    if not f or f in {"all", "any"}:
        return True
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()
    if f == "plus":
        # “free(可Plus试用)”/plus_trial_eligible 只是可试用，不算已开通 Plus。
        # 只有套餐字段本身是 Plus/ChatGPT Plus/plus_* 且不含 free 时才命中。
        return "plus" in plan and "free" not in plan
    if f == "free":
        return plan == "free"
    return plan == f


def _account_matches_status_filter(row: dict, status_filter: str | None = None) -> bool:
    """账号完成状态过滤；空值返回全部，link/sms 只返回对应已完成账号。"""
    value = str(status_filter or "").strip().lower()
    if not value or value in {"all", "any"}:
        return True
    if value in {"link", "linked"}:
        return bool(row.get("link_completed"))
    if value in {"sms", "phone"}:
        return bool(row.get("sms_completed"))
    return True


def _decorate_outlook(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = _outlook_line(out)
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = (
            (account.get("access_token") or "")[:40] + "..."
            if account.get("access_token")
            else ""
        )
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def _decorate_generic_api_email(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = _generic_api_email_line(out)
    out["password"] = out.get("password") or ""
    out["client_id"] = out.get("client_id") or ""
    out["refresh_token"] = out.get("refresh_token") or ""
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = (
            (account.get("access_token") or "")[:40] + "..."
            if account.get("access_token")
            else ""
        )
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def _decorate_icloud_email(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    """补充 iCloud 行的复制内容和关联账号信息。"""
    out = dict(row)
    out["copy_line"] = _icloud_email_line(out)
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = ((account.get("access_token") or "")[:40] + "..." if account.get("access_token") else "")
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def _get_conn() -> None:
    """兼容旧入口：初始化文件存储目录。"""
    _ensure_storage()
    return None


def _row_to_dict(row: dict | None) -> dict | None:
    return dict(row) if row is not None else None


# ============================================================
# registered_accounts
# ============================================================

def insert_account(
    *,
    email: str,
    access_token: str,
    totp_secret: str | None = None,
    user_id: str | None = None,
    user_name: str | None = None,
    plan_type: str | None = None,
    expires_at: str | None = None,
    device_id: str | None = None,
    proxy_used: str | None = None,
    email_source: str | None = None,
    extra: dict | None = None,
    codex_status: str | None = None,   # success / failed / skipped / missing
    codex_error: str | None = None,    # 失败原因（仅 codex_status=failed 时有意义）
    registration_method: str | None = None,
) -> int:
    """插入或更新注册成功账号，返回本地文件中的 id。"""
    with _LOCK:
        accounts = _load_accounts()
        outlook_rows = _load_outlook()
        icloud_rows = _load_icloud_emails()
        existing = _find_by_email(accounts, email)
        outlook_row = _find_by_email(outlook_rows, email)
        icloud_row = _find_by_email(icloud_rows, email)
        twofa = (extra or {}).get("twofa") or {}
        extra_json = json.dumps(extra, ensure_ascii=False) if extra else None

        if existing is None:
            row_id = _next_id(accounts)
            row = {
                "id": row_id,
                "email": email,
                "group_name": DEFAULT_ACCOUNT_GROUP,
                "created_at": _now(),
            }
            accounts.append(row)
        else:
            row = existing
            row_id = int(row["id"])
            row["group_name"] = str(row.get("group_name") or DEFAULT_ACCOUNT_GROUP).strip() or DEFAULT_ACCOUNT_GROUP

        row.update({
            "access_token": access_token,
            "totp_secret": totp_secret if totp_secret is not None else row.get("totp_secret"),
            # 2FA 结果单独落库，避免 ENABLE_2FA=True 但设置异常时只表现为“未启用”。
            # 旧数据没有该字段时保持兼容，由前端按 totp_secret 回退判断。
            "twofa_status": (
                str(twofa.get("status") or "").strip()
                or row.get("twofa_status")
                or ("success" if totp_secret else None)
            ),
            "twofa_error": (
                str(twofa.get("error") or "").strip()
                or row.get("twofa_error")
            ),
            "twofa_requested": (
                bool(twofa.get("requested"))
                if "requested" in twofa
                else row.get("twofa_requested")
            ),
            "user_id": user_id if user_id is not None else row.get("user_id"),
            "user_name": user_name if user_name is not None else row.get("user_name"),
            "plan_type": plan_type if plan_type is not None else row.get("plan_type"),
            "expires_at": expires_at if expires_at is not None else row.get("expires_at"),
            "device_id": device_id if device_id is not None else row.get("device_id"),
            "proxy_used": proxy_used if proxy_used is not None else row.get("proxy_used"),
            "email_source": email_source if email_source is not None else row.get("email_source"),
            "registration_method": (
                str(registration_method or row.get("registration_method") or "protocol").strip().lower()
            ),
            "registration_password": (
                str((extra or {}).get("registration_password") or "").strip()
                or row.get("registration_password")
            ),
            "extra_json": extra_json if extra_json is not None else row.get("extra_json"),
            "codex_status": codex_status if codex_status is not None else row.get("codex_status"),
            "codex_error": codex_error if codex_error is not None else row.get("codex_error"),
            "updated_at": _now(),
        })
        if codex_status == "success":
            row["sms_completed"] = True
            row["sms_completed_at"] = row.get("sms_completed_at") or _now()
            row["sms_status_source"] = "codex"
            row["sms_status_updated_at"] = _now()

        if outlook_row:
            row["password"] = outlook_row.get("password")
            row["client_id"] = outlook_row.get("client_id")
            row["refresh_token"] = outlook_row.get("refresh_token")
            row["original_email_line"] = _outlook_line(outlook_row)
            outlook_row["status"] = "used"
            outlook_row["used_at"] = outlook_row.get("used_at") or _now()
            outlook_row["registered_account_id"] = row_id
            outlook_row["access_token"] = access_token
            outlook_row["completed_at"] = _now()
            if totp_secret:
                outlook_row["totp_secret"] = totp_secret

        if icloud_row:
            # iCloud 素材没有密码字段，只需关联账号状态和 Token。
            icloud_row["status"] = "used"
            icloud_row["used_at"] = icloud_row.get("used_at") or _now()
            icloud_row["registered_account_id"] = row_id
            icloud_row["access_token"] = access_token
            icloud_row["completed_at"] = _now()
            if totp_secret:
                icloud_row["totp_secret"] = totp_secret

        row["copy_line"] = _account_line(row)
        _save_accounts(accounts)
        _save_outlook(outlook_rows)
        _save_icloud_emails(icloud_rows)
        return row_id


def update_account_codex_status(email: str, codex_status: str, codex_error: str | None = None) -> bool:
    """
    单独更新某账号的 codex_status / codex_error（手动补跑 Codex 时用）。
    返回是否找到该账号。
    """
    with _LOCK:
        accounts = _load_accounts()
        row = _find_by_email(accounts, email)
        if row is None:
            return False
        row["codex_status"] = codex_status
        row["codex_error"] = codex_error
        if codex_status == "success":
            row["sms_completed"] = True
            row["sms_completed_at"] = row.get("sms_completed_at") or _now()
            row["sms_status_source"] = "codex"
            row["sms_status_updated_at"] = _now()
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def claim_account_twofa_setup(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号 2FA 重设任务。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current = str(row.get("twofa_status") or "").lower()
        if current in {"queued", "running"}:
            try:
                stamp_key = "twofa_queued_at" if current == "queued" else "twofa_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["twofa_requested"] = True
        row["twofa_status"] = "queued"
        row["twofa_ok"] = False
        row["twofa_trigger"] = str(trigger or "manual")
        row["twofa_queued_at"] = now
        row["twofa_started_at"] = None
        row["twofa_completed_at"] = None
        row["twofa_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def mark_account_twofa_setup_running(acc_id: int) -> bool:
    """把已入队的 2FA 重设任务标为运行中。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or str(row.get("twofa_status") or "").lower() not in {"queued", "running"}:
            return False
        row["twofa_status"] = "running"
        row["twofa_started_at"] = _now()
        row["twofa_error"] = None
        row["updated_at"] = _now()
        _save_accounts(rows)
        return True


def update_account_twofa_setup(acc_id: int, result: dict | None = None) -> bool:
    """写回 2FA 重设结果，并在成功时同步账号和邮箱池的 TOTP secret。"""
    result = result or {}
    with _LOCK:
        rows = _load_accounts()
        outlook_rows = _load_outlook()
        icloud_rows = _load_icloud_emails()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        ok = bool(result.get("ok"))
        row["twofa_requested"] = True
        row["twofa_status"] = "success" if ok else "failed"
        row["twofa_ok"] = ok
        row["twofa_completed_at"] = now
        row["twofa_error"] = None if ok else str(result.get("error") or "2FA 设置失败")[:500]
        row["twofa_trigger"] = result.get("trigger") or row.get("twofa_trigger")
        if ok:
            secret = str(result.get("totp_secret") or "").replace(" ", "").strip()
            if not secret:
                row["twofa_status"] = "failed"
                row["twofa_ok"] = False
                row["twofa_error"] = "2FA 设置未返回 TOTP secret"
            else:
                row["totp_secret"] = secret
                email = str(row.get("email") or "").strip().lower()
                for pool_row in (*outlook_rows, *icloud_rows):
                    if str(pool_row.get("email") or "").strip().lower() == email:
                        pool_row["totp_secret"] = secret
        row["copy_line"] = _account_line(row)
        row["updated_at"] = now
        _save_accounts(rows)
        _save_outlook(outlook_rows)
        _save_icloud_emails(icloud_rows)
        return True


def recover_interrupted_twofa_setups() -> int:
    """启动时恢复遗留的 2FA 重设排队/运行状态。"""
    with _LOCK:
        rows = _load_accounts()
        recovered = 0
        now = _now()
        for row in rows:
            if str(row.get("twofa_status") or "").lower() not in {"queued", "running"}:
                continue
            row["twofa_status"] = "failed"
            row["twofa_ok"] = False
            row["twofa_error"] = "WebUI 重启导致 2FA 重设中断，请重新设置"
            row["twofa_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(rows)
        return recovered


def claim_account_codex_agent(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号 Codex Agent Token 生成任务；已有未超时任务时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("codex_agent_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "codex_agent_queued_at" if current_status == "queued" else "codex_agent_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["codex_agent_status"] = "queued"
        row["codex_agent_ok"] = False
        row["codex_agent_trigger"] = str(trigger or "manual")
        row["codex_agent_queued_at"] = now
        row["codex_agent_started_at"] = None
        row["codex_agent_completed_at"] = None
        row["codex_agent_error"] = None
        row["codex_agent_message"] = "已入队"
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_codex_agent_running(acc_id: int) -> bool:
    """把 Codex Agent Token 生成任务标记为运行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("codex_agent_status") not in {"queued", "running"}:
            return False
        row["codex_agent_status"] = "running"
        row["codex_agent_started_at"] = _now()
        row["codex_agent_error"] = None
        row["codex_agent_message"] = "正在生成 Codex Agent Token"
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_codex_agent(acc_id: int, result: dict | None = None) -> bool:
    """更新账号 Codex Agent Token 生成结果/进度。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["codex_agent_status"] = status
        row["codex_agent_ok"] = ok
        row["codex_agent_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped", "unsupported"}:
            row["codex_agent_completed_at"] = _now()
        row["codex_agent_error"] = None if ok or status == "running" else result.get("error")
        if result.get("message") is not None:
            row["codex_agent_message"] = result.get("message")
        if result.get("agent_runtime_id") is not None:
            row["codex_agent_runtime_id"] = result.get("agent_runtime_id")
        if result.get("auth_path") is not None:
            row["codex_agent_auth_path"] = result.get("auth_path")
        if isinstance(result.get("auth_json"), dict):
            row["codex_agent_token"] = json.dumps(result.get("auth_json"), ensure_ascii=False)
        for _k in (
            "codex_agent_network_route",
            "codex_agent_proxy_mode",
            "codex_agent_proxy_used",
            "codex_agent_proxy_fallback_reason",
            "codex_agent_device_id",
            "codex_agent_oai_session_id",
            "codex_agent_attempt_count",
            "codex_agent_max_attempts",
            "codex_agent_request_timeout",
            "codex_agent_sub2api_path",
            "codex_agent_sub2api_url",
            "codex_agent_sub2api_mode",
            "codex_agent_sub2api_total",
        ):
            src_key = _k.replace("codex_agent_", "", 1)
            if result.get(src_key) is not None:
                row[_k] = result.get(src_key)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_codex_agents() -> int:
    """服务启动时恢复上次进程中断的 Codex Agent 任务状态。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("codex_agent_status") not in {"queued", "running"}:
                continue
            row["codex_agent_status"] = "failed"
            row["codex_agent_ok"] = False
            row["codex_agent_error"] = "WebUI 重启导致 Codex Agent Token 任务中断，请重新生成"
            row["codex_agent_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def claim_account_plan_check(
    acc_id: int | None = None,
    email: str | None = None,
    trigger: str = "manual",
) -> bool:
    """原子占用账号的套餐查询；已有未超时查询时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        target_email = (email or "").lower()
        row = next((
            r for r in accounts
            if (acc_id is not None and int(r.get("id") or 0) == int(acc_id))
            or (target_email and (r.get("email") or "").lower() == target_email)
        ), None)
        if row is None:
            return False

        current_status = row.get("plan_check_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "plan_check_queued_at" if current_status == "queued" else "plan_check_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass

        now = _now()
        row["plan_check_status"] = "queued"
        row["plan_check_trigger"] = str(trigger or "manual")
        row["plan_check_queued_at"] = now
        row["plan_check_started_at"] = None
        row["plan_check_completed_at"] = None
        row["plan_check_error"] = None
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_plan_check_running(acc_id: int) -> bool:
    """把已排队的套餐查询标记为执行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("plan_check_status") not in {"queued", "running"}:
            return False
        row["plan_check_status"] = "running"
        row["plan_check_started_at"] = _now()
        row["plan_check_error"] = None
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_plan_checks() -> int:
    """服务启动时把上次进程遗留的内存队列状态恢复为可重试失败。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("plan_check_status") not in {"queued", "running"}:
                continue
            row["plan_check_status"] = "failed"
            row["plan_check_ok"] = False
            row["plan_check_error"] = "WebUI 重启导致套餐查询中断，请重新查询"
            row["plan_check_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def update_account_plan_check(acc_id: int | None = None, email: str | None = None, result: dict | None = None) -> bool:
    """更新账号套餐/Plus 试用资格查询结果。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        target_email = (email or "").lower()
        row = next((
            r for r in accounts
            if (acc_id is not None and int(r.get("id") or 0) == int(acc_id))
            or (target_email and (r.get("email") or "").lower() == target_email)
        ), None)
        if row is None:
            return False

        ok = bool(result.get("ok"))
        row["plan_check_status"] = "success" if ok else "failed"
        row["plan_check_ok"] = ok
        row["plan_checked_at"] = result.get("checked_at") or _now()
        row["plan_check_completed_at"] = _now()
        row["plan_check_http_status"] = result.get("http_status")
        row["plan_check_error"] = None if ok else result.get("error")

        if result.get("account_id"):
            row["account_id"] = result.get("account_id")
        # 查询失败只更新本次错误和网络信息，不覆盖上一次成功拿到的套餐、
        # 试用资格、优惠及有效期，避免临时网络故障把真实权益清空。
        if ok:
            if result.get("current_plan_type"):
                row["current_plan_type"] = result.get("current_plan_type")
                row["plan_type"] = result.get("current_plan_type")
            if result.get("subscription_plan") is not None:
                row["subscription_plan"] = result.get("subscription_plan")
            if result.get("has_active_subscription") is not None:
                row["has_active_subscription"] = bool(result.get("has_active_subscription"))
            if result.get("expires_at") is not None:
                row["plan_expires_at"] = result.get("expires_at")
            if result.get("renews_at") is not None:
                row["plan_renews_at"] = result.get("renews_at")
            if result.get("cancels_at") is not None:
                row["plan_cancels_at"] = result.get("cancels_at")
            if result.get("billing_period") is not None:
                row["billing_period"] = result.get("billing_period")
            if result.get("billing_currency") is not None:
                row["billing_currency"] = result.get("billing_currency")
            if result.get("is_delinquent") is not None:
                row["is_delinquent"] = bool(result.get("is_delinquent"))
            for _k in (
                "discount_type",
                "discount_amount",
                "discount_duration_num_periods",
                "discount_expires_at",
                "discount_cancellation_policy",
                "discount_promo_campaign_id",
                "last_purchase_origin_platform",
                "last_will_renew",
            ):
                if result.get(_k) is not None:
                    row[_k] = result.get(_k)

            row["plus_trial_eligible"] = bool(result.get("plus_trial_eligible"))
            row["plus_trial_campaign_id"] = result.get("plus_trial_campaign_id")
            row["plus_trial_title"] = result.get("plus_trial_title")
            row["plus_trial_discount_percentage"] = result.get("plus_trial_discount_percentage")
            row["plus_trial_duration_num_periods"] = result.get("plus_trial_duration_num_periods")
            row["plus_trial_duration_period"] = result.get("plus_trial_duration_period")
            row["eligible_offer_ids"] = result.get("eligible_offer_ids") or []
            row["oaics_check_status"] = result.get("oaics_check_status") or "skipped"
            row["oaics_checked_at"] = result.get("oaics_checked_at")
            row["oaics_check_http_status"] = result.get("oaics_check_http_status")
            row["oaics_check_error"] = result.get("oaics_check_error")
            if result.get("oaics_check_status") == "success":
                row["oaics_eligible"] = bool(result.get("oaics_eligible"))
                row["oaics_session_kind"] = result.get("oaics_session_kind")
                row["oaics_processor_entity"] = result.get("oaics_processor_entity")
            elif result.get("oaics_check_status") == "skipped":
                row["oaics_eligible"] = False
                row["oaics_session_kind"] = None
                row["oaics_processor_entity"] = None
            row["plan_last_success_at"] = result.get("checked_at") or _now()
            row["plan_last_success_result_json"] = json.dumps(result, ensure_ascii=False)
        row["plan_check_proxy_mode"] = result.get("proxy_mode")
        row["plan_check_network_route"] = result.get("network_route")
        row["plan_check_proxy_used"] = result.get("proxy_used")
        row["plan_check_proxy_fallback_reason"] = result.get("proxy_fallback_reason")
        row["token_expired"] = result.get("token_expired")
        row["token_expires_at"] = result.get("token_expires_at")
        row["plan_check_result_json"] = json.dumps(result, ensure_ascii=False)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def claim_account_extract(acc_id: int, trigger: str = "manual", link_type: str = "pix") -> bool:
    """原子占用账号提链任务；已有未超时任务时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("extract_link_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "extract_link_queued_at" if current_status == "queued" else "extract_link_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["extract_link_status"] = "queued"
        row["extract_link_ok"] = False
        row["extract_link_trigger"] = str(trigger or "manual")
        row["extract_link_type"] = str(link_type or "pix").lower()
        row["extract_link_queued_at"] = now
        row["extract_link_started_at"] = None
        row["extract_link_completed_at"] = None
        row["extract_link_error"] = None
        row["extract_link_message"] = "已入队"
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_extract_running(acc_id: int) -> bool:
    """把提链任务标记为运行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("extract_link_status") not in {"queued", "running"}:
            return False
        row["extract_link_status"] = "running"
        row["extract_link_started_at"] = _now()
        row["extract_link_error"] = None
        row["extract_link_message"] = "任务运行中"
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_extract(acc_id: int, result: dict | None = None) -> bool:
    """更新账号提链任务结果/进度。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["extract_link_status"] = status
        row["extract_link_ok"] = ok
        row["extract_link_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped"}:
            row["extract_link_completed_at"] = _now()
        row["extract_link_error"] = None if ok or status == "running" else result.get("error")
        if result.get("message") is not None:
            row["extract_link_message"] = result.get("message")
        if result.get("job_id") is not None:
            row["extract_link_job_id"] = result.get("job_id")
        if result.get("link_type") is not None:
            row["extract_link_type"] = result.get("link_type")
        if result.get("cdk_remaining") is not None:
            row["extract_link_cdk_remaining"] = result.get("cdk_remaining")
        payload = result.get("result") if isinstance(result.get("result"), dict) else {}
        if payload:
            row["extract_link_long_url"] = payload.get("long_url")
            row["extract_link_copy_paste"] = payload.get("copy_paste")
            row["extract_link_image_url_png"] = payload.get("image_url_png")
            row["extract_link_image_url_svg"] = payload.get("image_url_svg")
            row["extract_link_payment_method"] = payload.get("payment_method")
            row["extract_link_payment_link_type"] = payload.get("payment_link_type")
            row["extract_link_expires_at"] = payload.get("expires_at")
            if payload.get("cdk_remaining") is not None:
                row["extract_link_cdk_remaining"] = payload.get("cdk_remaining")
            row["extract_link_result_json"] = json.dumps(payload, ensure_ascii=False)
        if ok:
            row["link_completed"] = True
            row["link_completed_at"] = row.get("link_completed_at") or _now()
            row["link_status_source"] = "extract"
            row["link_status_updated_at"] = _now()
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_extract_links() -> int:
    """服务启动时恢复上次进程中断的提链状态。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("extract_link_status") not in {"queued", "running"}:
                continue
            row["extract_link_status"] = "failed"
            row["extract_link_ok"] = False
            row["extract_link_error"] = "WebUI 重启导致提链任务中断，请重新提链"
            row["extract_link_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def _account_query_aliases(row: dict) -> tuple[list[str], list[str]]:
    """构造账号搜索别名，返回（套餐别名，状态别名）。"""
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip()
    plan_lower = plan.lower()
    plan_aliases = [plan, f"套餐:{plan}", f"[套餐:{plan}]"] if plan else []
    if plan_lower == "free":
        if bool(row.get("plus_trial_eligible")):
            plan_aliases.extend([
                "free(可Plus试用)", "free（可Plus试用）", "可Plus试用",
                "[可Plus试用]", "[套餐:free(可Plus试用)]",
            ])
        elif row.get("plan_check_status") in {"queued", "running"} or row.get("plan_check_ok") is None:
            plan_aliases.extend(["free(待查资格)", "待查资格"])

    status_aliases: list[str] = []

    def add_flag(enabled: bool, enabled_label: str, disabled_label: str) -> None:
        label = enabled_label if enabled else disabled_label
        status_aliases.extend([label, f"[{label}]"])

    add_flag(bool(row.get("totp_secret")), "2FA", "无2FA")
    add_flag(bool(row.get("link_completed")), "提链", "未提链")
    add_flag(bool(row.get("sms_completed")), "接码", "未接码")
    add_flag(bool(str(row.get("access_token") or "").strip()), "Token", "无Token")
    add_flag(bool(row.get("archived")), "归档", "未归档")

    twofa_status = str(row.get("twofa_status") or "").strip().lower()
    if twofa_status == "failed":
        status_aliases.extend(["2FA失败", "[2FA失败]"])
    elif twofa_status == "skipped":
        status_aliases.extend(["2FA已跳过", "[2FA已跳过]"])

    codex_status = str(row.get("codex_status") or "").strip().lower()
    if codex_status:
        status_aliases.extend([f"Codex:{codex_status}", f"[Codex:{codex_status}]"])
    if codex_status == "success":
        status_aliases.extend(["Codex", "[Codex]"])

    agent_status = str(row.get("codex_agent_status") or "").strip().lower()
    if agent_status:
        status_aliases.extend([f"Agent:{agent_status}", f"[Agent:{agent_status}]"])
    if agent_status == "success" or bool(str(row.get("codex_agent_token") or "").strip()):
        status_aliases.extend(["Agent", "[Agent]"])

    live_status = str(row.get("live_check_status") or "").strip().lower()
    if live_status:
        status_aliases.extend([f"查活:{live_status}", f"[查活:{live_status}]"])
    if row.get("live_check_ok") is True:
        status_aliases.extend(["查活正常", "[查活正常]"])
    elif row.get("live_check_ok") is False:
        status_aliases.extend(["查活失败", "[查活失败]"])

    if str(row.get("plan_check_status") or "").lower() == "failed":
        status_aliases.extend(["套餐查询失败", "[套餐查询失败]"])
    if row.get("oaics_eligible") is True:
        status_aliases.extend(["oaics", "[oaics]"])
    elif row.get("oaics_eligible") is False:
        status_aliases.extend(["无oaics", "[无oaics]"])
    return plan_aliases, status_aliases


def _account_query_text(row: dict) -> tuple[str, str, str]:
    plan_aliases, status_aliases = _account_query_aliases(row)
    searchable_values = []
    for key, value in row.items():
        # extra_json/session 中可能保留注册瞬间的旧套餐，套餐升级后会干扰 !free。
        if key in {"extra_json", "session", "plan_type"} and row.get("current_plan_type"):
            continue
        if isinstance(value, (dict, list, tuple, set)):
            continue
        searchable_values.append(str(value))
    general = "\n".join(searchable_values + plan_aliases + status_aliases).lower()
    return general, "\n".join(plan_aliases).lower(), "\n".join(status_aliases).lower()


def _account_query_term_matches(row: dict, term: str) -> bool:
    term = str(term or "").strip().strip("*").strip()
    if not term:
        return True
    normalized = term.lower().replace("（", "(").replace("）", ")")
    general, plans, statuses = _account_query_text(row)
    displayed_plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()
    if normalized == "free":
        return displayed_plan == "free"
    if normalized in {"plus", "pro", "team", "go"}:
        return normalized in displayed_plan and displayed_plan != "free"
    plan_terms = {
        "可plus试用", "free(可plus试用)", "free(待查资格)",
    }
    if normalized in plan_terms or normalized.startswith("套餐:") or normalized.startswith("[套餐:"):
        return normalized in plans.replace("（", "(").replace("）", ")")
    if normalized.startswith("[") and normalized.endswith("]"):
        return normalized in statuses.replace("（", "(").replace("）", ")") or normalized in plans.replace("（", "(").replace("）", ")")
    return normalized in general.replace("（", "(").replace("）", ")")


def _account_matches_query(row: dict, q: str | None) -> bool:
    """账号表达式搜索：``&&`` 表示同时满足，``!`` 前缀表示排除。"""
    query = str(q or "").strip()
    if not query:
        return True
    terms = [part.strip() for part in re.split(r"\s*&&\s*", query) if part.strip()]
    for raw_term in terms:
        negated = raw_term.startswith("!")
        term = raw_term[1:].strip() if negated else raw_term
        matched = _account_query_term_matches(row, term)
        if (not negated and not matched) or (negated and matched):
            return False
    return True


def _account_group_filter_names(value: str | list[str] | None) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raw = str(value or "").strip()
        if not raw or raw.casefold() in {"all", "全部", "[]"}:
            return set()
        raw_values = []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                raw_values = parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_values = raw.split(",")
        if not raw_values:
            raw_values = [raw]
    return {
        str(item or "").strip().casefold()
        for item in raw_values
        if str(item or "").strip() and str(item or "").strip().casefold() not in {"all", "全部"}
    }


def _filtered_decorated_accounts(
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    q: str | None = None,
    status_filter: str | None = None,
    group_filter: str | list[str] | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
) -> list[dict]:
    rows = _load_accounts()
    if archived in (True, "1", "true", "yes", "only"):
        rows = [r for r in rows if bool(r.get("archived"))]
    elif archived in ("all", "include"):
        pass
    else:
        rows = [r for r in rows if not bool(r.get("archived"))]
    decorated = [_decorate_account(r) for r in rows]
    group_names = _account_group_filter_names(group_filter)
    if group_names:
        decorated = [
            r for r in decorated
            if str(r.get("group_name") or DEFAULT_ACCOUNT_GROUP).strip().casefold() in group_names
        ]
    date_from = str(created_from or '').strip()[:10]
    date_to = str(created_to or '').strip()[:10]
    if date_from or date_to:
        decorated = [
            r for r in decorated
            if (not date_from or str(r.get("created_at") or "")[:10] >= date_from)
            and (not date_to or str(r.get("created_at") or "")[:10] <= date_to)
        ]
    decorated = [r for r in decorated if _account_matches_plan_filter(r, plan_filter)]
    decorated = [r for r in decorated if _account_matches_status_filter(r, status_filter)]
    decorated = [r for r in decorated if _account_matches_query(r, q)]
    return sorted(decorated, key=lambda x: int(x.get("id") or 0), reverse=True)


def list_account_plan_check_statuses(
    limit: int = 5000,
    offset: int = 0,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    q: str | None = None,
    status_filter: str | None = None,
    group_filter: str | list[str] | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
) -> dict:
    """返回不含 Token/邮箱密码的套餐查询轻量状态快照。"""
    fields = (
        "id", "email", "archived", "link_completed", "sms_completed",
        "plan_type", "current_plan_type", "plus_trial_eligible",
        "oaics_eligible", "oaics_check_status", "oaics_check_error", "oaics_checked_at",
        "oaics_session_kind", "oaics_processor_entity",
        "twofa_status", "twofa_error", "twofa_trigger", "twofa_queued_at", "twofa_started_at", "twofa_completed_at",
        "plan_check_status", "plan_check_ok", "plan_check_error",
        "plan_check_trigger", "plan_check_queued_at", "plan_check_started_at",
        "plan_check_completed_at", "plan_checked_at", "plan_last_success_at",
        "plan_check_network_route", "plan_check_proxy_used", "plan_check_proxy_fallback_reason",
        "live_check_status", "live_check_ok", "live_check_error",
        "live_check_trigger", "live_check_queued_at", "live_check_started_at",
        "live_checked_at", "live_check_proxy_used",
        "subscription_plan", "has_active_subscription", "is_delinquent",
        "expires_at", "plan_expires_at", "plan_renews_at", "renews_at", "plan_cancels_at",
        "billing_period", "billing_currency", "last_purchase_origin_platform", "last_will_renew",
        "discount_amount", "discount_type", "discount_duration_num_periods",
        "discount_expires_at", "discount_cancellation_policy", "discount_promo_campaign_id",
        "extract_link_status", "extract_link_ok", "extract_link_type",
        "extract_link_message", "extract_link_error",
        "extract_link_long_url", "extract_link_copy_paste",
        "extract_link_image_url_png", "extract_link_image_url_svg",
        "extract_link_expires_at",
        "codex_status", "codex_error",
        "codex_agent_status", "codex_agent_message",
        "codex_agent_runtime_id", "codex_agent_sub2api_url",
        "codex_agent_sub2api_mode", "codex_agent_sub2api_total",
    )
    with _LOCK:
        all_rows = _filtered_decorated_accounts(
            archived=archived,
            plan_filter=plan_filter,
            q=q,
            status_filter=status_filter,
            group_filter=group_filter,
            created_from=created_from,
            created_to=created_to,
        )
        total = len(all_rows)
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        rows = all_rows[offset: offset + limit]
        items = []
        for row in rows:
            item = {"id": row.get("id"), "email": row.get("email")}
            for key in fields:
                value = row.get(key)
                if key in ("id", "email"):
                    continue
                # 查活/套餐状态需要把 null 一并传给前端，用于清除上一轮失败原因和时间；
                # 否则轻量轮询 Object.assign 会把旧 plan_check_error 永久留在表格里。
                if key.startswith(("live_check_", "plan_check_", "twofa_")) or (value is not None and value != ""):
                    item[key] = value
            plan = str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
            if not any(x in plan for x in ("plus", "pro", "team", "go")):
                for expire_key in ("expires_at", "plan_expires_at", "plan_renews_at", "renews_at"):
                    item.pop(expire_key, None)
            item["codex_agent_has_token"] = bool(str(row.get("codex_agent_token") or "").strip())
            item["has_access_token"] = bool(str(row.get("access_token") or "").strip())
            items.append(item)
        latest = max((str(row.get("updated_at") or "") for row in all_rows), default="")
        # updated_at 目前只有秒级精度；一次快速查询可能在同一秒内完成
        # queued -> running -> success/failed，导致 revision 不变，前端跳过合并状态，
        # 页面就会一直停在“查询中”。把轻量状态本身纳入签名，保证状态变化可被轮询发现。
        revision_payload = json.dumps(
            [
                {
                    "id": row.get("id"),
                    "updated_at": row.get("updated_at"),
                    "plan_check_status": row.get("plan_check_status"),
                    "plan_check_ok": row.get("plan_check_ok"),
                    "plan_check_error": row.get("plan_check_error"),
                    "plan_checked_at": row.get("plan_checked_at"),
                    "plan_check_network_route": row.get("plan_check_network_route"),
                    "plan_check_proxy_used": row.get("plan_check_proxy_used"),
                    "plan_check_proxy_fallback_reason": row.get("plan_check_proxy_fallback_reason"),
                    "current_plan_type": row.get("current_plan_type"),
                    "plan_type": row.get("plan_type"),
                    "plus_trial_eligible": row.get("plus_trial_eligible"),
                    "oaics_eligible": row.get("oaics_eligible"),
                    "oaics_check_status": row.get("oaics_check_status"),
                    "oaics_check_error": row.get("oaics_check_error"),
                    "oaics_checked_at": row.get("oaics_checked_at"),
                    "twofa_status": row.get("twofa_status"),
                    "twofa_error": row.get("twofa_error"),
                    "twofa_queued_at": row.get("twofa_queued_at"),
                    "twofa_started_at": row.get("twofa_started_at"),
                    "twofa_completed_at": row.get("twofa_completed_at"),
                    "twofa_trigger": row.get("twofa_trigger"),
                    # 悬浮卡详情也纳入轻量轮询签名，避免同秒完成查询时沿用旧详情。
                    "subscription_plan": row.get("subscription_plan"),
                    "has_active_subscription": row.get("has_active_subscription"),
                    "is_delinquent": row.get("is_delinquent"),
                    "plan_expires_at": row.get("plan_expires_at"),
                    "plan_renews_at": row.get("plan_renews_at"),
                    "plan_cancels_at": row.get("plan_cancels_at"),
                    "billing_period": row.get("billing_period"),
                    "billing_currency": row.get("billing_currency"),
                    "discount_amount": row.get("discount_amount"),
                    "discount_type": row.get("discount_type"),
                    "discount_duration_num_periods": row.get("discount_duration_num_periods"),
                    "discount_expires_at": row.get("discount_expires_at"),
                    "discount_cancellation_policy": row.get("discount_cancellation_policy"),
                    "discount_promo_campaign_id": row.get("discount_promo_campaign_id"),
                    "last_purchase_origin_platform": row.get("last_purchase_origin_platform"),
                    "last_will_renew": row.get("last_will_renew"),
                    "live_check_status": row.get("live_check_status"),
                    "live_check_ok": row.get("live_check_ok"),
                    "live_check_error": row.get("live_check_error"),
                    "live_check_queued_at": row.get("live_check_queued_at"),
                    "live_check_started_at": row.get("live_check_started_at"),
                    "live_checked_at": row.get("live_checked_at"),
                    "extract_link_status": row.get("extract_link_status"),
                    "link_completed": row.get("link_completed"),
                    "sms_completed": row.get("sms_completed"),
                    "codex_status": row.get("codex_status"),
                    "codex_agent_status": row.get("codex_agent_status"),
                }
                for row in all_rows
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        revision_sig = hashlib.sha1(revision_payload.encode("utf-8")).hexdigest()[:12]
        return {"items": items, "total": total, "offset": offset, "limit": limit, "revision": f"{total}:{latest}:{revision_sig}"}


def list_accounts(
    limit: int = 500,
    offset: int = 0,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    q: str | None = None,
    status_filter: str | None = None,
    group_filter: str | list[str] | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
) -> list[dict]:
    with _LOCK:
        rows = _filtered_decorated_accounts(
            archived=archived,
            plan_filter=plan_filter,
            q=q,
            status_filter=status_filter,
            group_filter=group_filter,
            created_from=created_from,
            created_to=created_to,
        )
        return rows[max(0, int(offset or 0)): max(0, int(offset or 0)) + max(1, int(limit))]


def list_accounts_page(
    limit: int = 50,
    offset: int = 0,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    q: str | None = None,
    status_filter: str | None = None,
    group_filter: str | list[str] | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
) -> dict:
    with _LOCK:
        rows = _filtered_decorated_accounts(
            archived=archived,
            plan_filter=plan_filter,
            q=q,
            status_filter=status_filter,
            group_filter=group_filter,
            created_from=created_from,
            created_to=created_to,
        )
        total = len(rows)
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        items = rows[offset: offset + limit]
        latest = max((str(row.get("updated_at") or "") for row in rows), default="")
        return {"items": items, "total": total, "offset": offset, "limit": limit, "revision": f"{total}:{latest}"}


def get_account(acc_id: int) -> dict | None:
    with _LOCK:
        row = next((r for r in _load_accounts() if int(r.get("id") or 0) == int(acc_id)), None)
        return _decorate_account(row) if row else None


def get_account_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_accounts(), email)
        return _decorate_account(row) if row else None


def update_account_note(acc_id: int, note: str) -> bool:
    """更新单个已注册账号备注。note 为空字符串时表示清空备注。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["note"] = str(note or "")
        row["note_updated_at"] = now
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_account_completion_status(
    acc_id: int,
    status_name: str,
    enabled: bool,
    source: str = "manual",
) -> dict | None:
    """人工更新账号提链/接码完成状态，返回更新后的轻量状态。"""
    name = str(status_name or "").strip().lower()
    if name not in {"link", "sms"}:
        raise ValueError("status_name 必须是 link 或 sms")
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return None
        now = _now()
        field = f"{name}_completed"
        row[field] = bool(enabled)
        row[f"{name}_completed_at"] = now if enabled else None
        row[f"{name}_status_source"] = str(source or "manual")
        row[f"{name}_status_updated_at"] = now
        row["updated_at"] = now
        _save_accounts(rows)
        return {
            "id": int(row.get("id") or 0),
            "email": row.get("email"),
            "link_completed": _decorate_account(row)["link_completed"],
            "sms_completed": _decorate_account(row)["sms_completed"],
        }


def sync_account_link_status(emails: list[str]) -> tuple[list[dict], list[dict]]:
    """按邮箱批量点亮提链状态，返回 (updated, missing)。"""
    normalized = []
    seen = set()
    for raw in emails or []:
        email = str(raw or "").strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        normalized.append(email)

    with _LOCK:
        rows = _load_accounts()
        by_email = {(str(row.get("email") or "").strip().lower()): row for row in rows}
        updated = []
        missing = []
        now = _now()
        for email in normalized:
            row = by_email.get(email)
            if row is None:
                missing.append({"email": email, "reason": "账号不存在"})
                continue
            row["link_completed"] = True
            row["link_completed_at"] = row.get("link_completed_at") or now
            row["link_status_source"] = "manual_sync"
            row["link_status_updated_at"] = now
            row["updated_at"] = now
            updated.append({"id": int(row.get("id") or 0), "email": row.get("email")})
        if updated:
            _save_accounts(rows)
        return updated, missing


def update_account_liveness(acc_id: int, result: dict | None = None) -> bool:
    """写回账号查活结果；成功时同步刷新最新 access_token 和账号基础信息。"""
    result = result or {}
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False

        now = _now()
        ok = bool(result.get("ok"))
        status = str(result.get("status") or ("live" if ok else "failed"))
        row["live_check_status"] = status
        row["live_check_ok"] = ok
        row["live_checked_at"] = result.get("checked_at") or now
        row["live_check_error"] = None if ok else result.get("error")
        row["updated_at"] = now

        if status == "deactivated":
            row["codex_status"] = "deactivated"
            row["codex_error"] = result.get("error") or "账号已删除/停用/封禁"

        if ok:
            token = str(result.get("access_token") or "").strip()
            if token:
                row["access_token"] = token
            session = result.get("session") or {}
            user = session.get("user") or {}
            account = session.get("account") or {}
            if user.get("id"):
                row["user_id"] = user.get("id")
            if user.get("name") is not None:
                row["user_name"] = user.get("name")
            if account.get("planType"):
                row["plan_type"] = account.get("planType")
            if session.get("expires"):
                row["expires_at"] = session.get("expires")
            if result.get("device_id"):
                row["device_id"] = result.get("device_id")
            if result.get("proxy_used"):
                row["live_check_proxy_used"] = result.get("proxy_used")
            # 仅刷新流程返回真实 cookies 时更新登录态；现有 AT 在线校验生成的
            # 精简 session 不覆盖注册时保存的完整 Session/Cookie。
            if isinstance(session, dict) and isinstance(session.get("cookies"), list) and session.get("cookies"):
                try:
                    extra = json.loads(str(row.get("extra_json") or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    extra = {}
                if not isinstance(extra, dict):
                    extra = {}
                extra["session"] = session
                row["extra_json"] = json.dumps(extra, ensure_ascii=False)
            row["live_check_error"] = None

        row["copy_line"] = _account_line(row)
        _save_accounts(rows)
        return True


def claim_account_live_check(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号查活任务；已有 queued/running 时返回 False。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        if row.get("live_check_status") in {"queued", "running"}:
            try:
                stamp_key = "live_check_queued_at" if row.get("live_check_status") == "queued" else "live_check_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if row.get("live_check_status") == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["live_check_status"] = "queued"
        row["live_check_ok"] = False
        row["live_check_trigger"] = str(trigger or "manual")
        row["live_check_queued_at"] = now
        row["live_check_started_at"] = None
        row["live_checked_at"] = None
        row["live_check_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def recover_interrupted_live_checks() -> int:
    """服务启动时恢复上次进程中断的查活状态，避免 queued/running 卡死。"""
    with _LOCK:
        rows = _load_accounts()
        recovered = 0
        now = _now()
        for row in rows:
            if row.get("live_check_status") not in {"queued", "running"}:
                continue
            row["live_check_status"] = "failed"
            row["live_check_ok"] = False
            row["live_check_error"] = "WebUI 重启或任务异常中断，请重新查活"
            row["live_checked_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(rows)
        return recovered


def mark_account_live_check_running(acc_id: int) -> bool:
    """把账号查活任务标记为运行中。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("live_check_status") not in {"queued", "running"}:
            return False
        now = _now()
        row["live_check_status"] = "running"
        row["live_check_started_at"] = now
        row["live_check_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_accounts_note(account_ids: list[int] | None, note: str) -> tuple[list[dict], list[dict]]:
    """
    批量更新已注册账号备注。
    返回 (updated, skipped)，updated/skipped 元素含 id/email。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        text = str(note or "")
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["note"] = text
            row["note_updated_at"] = now
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "note": text, "note_updated_at": now})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def archive_account(acc_id: int, archived: bool = True) -> bool:
    """归档/取消归档单个已注册账号。归档不会删除 token，只影响默认账号列表查询。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["archived"] = bool(archived)
        row["archived_at"] = now if archived else None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def archive_accounts(account_ids: list[int] | None, archived: bool = True) -> tuple[list[dict], list[dict]]:
    """批量归档/取消归档账号。返回 (updated, skipped)。"""
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["archived"] = bool(archived)
            row["archived_at"] = now if archived else None
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "archived": bool(archived), "archived_at": row.get("archived_at")})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def count_accounts() -> int:
    with _LOCK:
        return len(_load_accounts())


def delete_account(acc_id: int | None = None, email: str | None = None) -> bool:
    """删除一个已注册账号记录，并同步刷新 注册成功的邮箱.txt / token.txt / 静态查看页。"""
    with _LOCK:
        rows = _load_accounts()
        target_email = (email or "").lower()
        new_rows = []
        deleted = False
        for row in rows:
            match_id = acc_id is not None and int(row.get("id") or 0) == int(acc_id)
            match_email = bool(target_email) and (row.get("email") or "").lower() == target_email
            if match_id or match_email:
                deleted = True
                continue
            new_rows.append(row)
        if not deleted:
            return False
        _save_accounts(new_rows)
        return True


def delete_accounts(account_ids: list[int] | None = None, emails: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """
    批量删除已注册账号。
    返回 (deleted, skipped)，deleted 元素含 id/email。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().isdigit()}
    email_set = {(e or "").lower() for e in (emails or []) if e}
    deleted: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        new_rows = []
        seen_ids: set[int] = set()
        seen_emails: set[str] = set()
        for row in rows:
            row_id = int(row.get("id") or 0)
            row_email = (row.get("email") or "").lower()
            if row_id in ids or row_email in email_set:
                deleted.append({"id": row_id, "email": row.get("email")})
                seen_ids.add(row_id)
                seen_emails.add(row_email)
                continue
            new_rows.append(row)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        for item in email_set - seen_emails:
            skipped.append({"email": item, "reason": "账号不存在"})
        if deleted:
            _save_accounts(new_rows)
    return deleted, skipped


# ============================================================
# outlook_pool
# ============================================================

def import_outlook_accounts(records: list[dict]) -> tuple[int, int]:
    """
    批量导入 Outlook 账号。
    records 元素：{email, password, client_id, refresh_token}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_outlook()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "password": (raw.get("password") or "").strip(),
                "client_id": (raw.get("client_id") or raw.get("clientId") or "").strip(),
                "refresh_token": (raw.get("refresh_token") or raw.get("refreshToken") or "").strip(),
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _outlook_line(row)
            rows.append(row)
            inserted += 1
        _save_outlook(rows)
        return inserted, skipped


def import_registered_email_accounts(records: list[dict], source: str | None) -> tuple[int, int]:
    """
    把邮箱素材直接导入为“已注册成功账号”，用于跳过注册、直接在账号页补跑 Codex 授权。

    source:
      - outlook: records 元素 {email,password,client_id,refresh_token[,access_token,totp_secret]}
      - generic_api / icloud: records 元素 {email,code_url[,access_token,totp_secret]}

    返回 (新增账号数, 跳过数)。已存在账号会跳过；邮箱池中已存在的素材会复用并标记 used。
    """
    source = (source or "").strip().lower()
    if source not in ("outlook", "generic_api", "icloud"):
        raise ValueError("source 必须显式传入 outlook / generic_api / icloud")

    with _LOCK:
        accounts = _load_accounts()
        outlook_rows = _load_outlook()
        generic_rows = _load_generic_api_emails()
        icloud_rows = _load_icloud_emails()
        inserted = skipped = 0

        for raw in records:
            email = (raw.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            if _find_by_email(accounts, email):
                skipped += 1
                continue

            now = _now()
            original_line = email
            pool_row = None

            if source in ("generic_api", "icloud"):
                code_url = (raw.get("code_url") or raw.get("url") or "").strip()
                if not code_url:
                    skipped += 1
                    continue
                pool_rows = generic_rows if source == "generic_api" else icloud_rows
                pool_row = _find_by_email(pool_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(pool_rows),
                        "email": email,
                        "code_url": code_url,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    pool_rows.append(pool_row)
                else:
                    pool_row["code_url"] = code_url or pool_row.get("code_url")
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _generic_api_email_line(pool_row) if source == "generic_api" else _icloud_email_line(pool_row)
                original_line = pool_row["copy_line"]
            else:
                password = (raw.get("password") or "").strip()
                client_id = (raw.get("client_id") or raw.get("clientId") or "").strip()
                refresh_token = (raw.get("refresh_token") or raw.get("refreshToken") or "").strip()
                if not (password and client_id and refresh_token):
                    skipped += 1
                    continue
                pool_row = _find_by_email(outlook_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(outlook_rows),
                        "email": email,
                        "password": password,
                        "client_id": client_id,
                        "refresh_token": refresh_token,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    outlook_rows.append(pool_row)
                else:
                    pool_row["password"] = password or pool_row.get("password")
                    pool_row["client_id"] = client_id or pool_row.get("client_id")
                    pool_row["refresh_token"] = refresh_token or pool_row.get("refresh_token")
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _outlook_line(pool_row)
                original_line = _outlook_line(pool_row)

            row_id = _next_id(accounts)
            access_token = (raw.get("access_token") or raw.get("token") or "").strip()
            totp_secret = (raw.get("totp_secret") or raw.get("totp") or "").strip() or None
            account = {
                "id": row_id,
                "email": email,
                "created_at": now,
                "access_token": access_token,
                "totp_secret": totp_secret,
                "user_id": raw.get("user_id"),
                "user_name": raw.get("user_name") or "Imported Account",
                "plan_type": raw.get("plan_type"),
                "expires_at": raw.get("expires_at"),
                "device_id": raw.get("device_id"),
                "proxy_used": raw.get("proxy_used"),
                "email_source": source,
                "extra_json": json.dumps({"imported_registered": True}, ensure_ascii=False),
                "codex_status": raw.get("codex_status") or "",
                "codex_error": raw.get("codex_error"),
                "updated_at": now,
                "original_email_line": original_line,
            }
            if source == "outlook":
                account["password"] = pool_row.get("password")
                account["client_id"] = pool_row.get("client_id")
                account["refresh_token"] = pool_row.get("refresh_token")
            account["copy_line"] = _account_line(account)
            accounts.append(account)

            pool_row["registered_account_id"] = row_id
            pool_row["access_token"] = access_token
            if totp_secret:
                pool_row["totp_secret"] = totp_secret
            inserted += 1

        _save_outlook(outlook_rows)
        _save_generic_api_emails(generic_rows)
        _save_icloud_emails(icloud_rows)
        _save_accounts(accounts)
        return inserted, skipped


def claim_next_outlook() -> dict | None:
    """原子领取一个可用 Outlook 账号并标记为 used。"""
    with _LOCK:
        rows = sorted(_load_outlook(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["note"] = None
        _save_outlook(rows)
        return _decorate_outlook(row)


def release_outlook(email: str, status: str = "available", note: str | None = None) -> None:
    """把账号状态改回 available，或标记为 used/failed/disabled。"""
    with _LOCK:
        rows = _load_outlook()
        row = _find_by_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_outlook(rows)


def release_unconsumed_outlook(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的 Outlook 邮箱。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_outlook()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_outlook(rows)
        return True


def delete_outlook(email: str) -> bool:
    """从邮箱池彻底删除一个邮箱（按 email 匹配）。返回是否删到。"""
    with _LOCK:
        rows = _load_outlook()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_outlook(new_rows)
        return True


def list_outlook_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        account_by_email = {
            (a.get("email") or "").lower(): a
            for a in _load_accounts()
        }
        rows = _load_outlook()
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows = sorted(rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        return [_decorate_outlook(r, account_by_email) for r in rows[:limit]]


def outlook_pool_summary() -> dict:
    with _LOCK:
        out = {"available": 0, "used": 0, "failed": 0}
        for row in _load_outlook():
            status = row.get("status") or "available"
            out[status] = out.get(status, 0) + 1
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def get_outlook_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_outlook(), email)
        return _decorate_outlook(row) if row else None


# ============================================================
# generic_api email pool
# ============================================================

def import_generic_api_emails(records: list[dict]) -> tuple[int, int]:
    """
    批量导入通用 API 取码邮箱。
    records 元素：{email, code_url}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_generic_api_emails()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            code_url = (raw.get("code_url") or raw.get("url") or "").strip()
            if not email or not code_url:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "code_url": code_url,
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _generic_api_email_line(row)
            rows.append(row)
            inserted += 1
        _save_generic_api_emails(rows)
        return inserted, skipped


def claim_next_generic_api_email() -> dict | None:
    """原子领取一个可用通用 API 邮箱并标记为 used。"""
    with _LOCK:
        rows = sorted(_load_generic_api_emails(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["note"] = None
        _save_generic_api_emails(rows)
        return _decorate_generic_api_email(row)


def release_generic_api_email(email: str, status: str = "available", note: str | None = None) -> None:
    """把通用 API 邮箱状态改回 available，或标记为 failed/used。"""
    with _LOCK:
        rows = _load_generic_api_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_generic_api_emails(rows)


def release_unconsumed_generic_api_email(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的通用 API 邮箱。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_generic_api_emails()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_generic_api_emails(rows)
        return True


def delete_generic_api_email(email: str) -> bool:
    """从通用 API 邮箱池彻底删除一个邮箱。"""
    with _LOCK:
        rows = _load_generic_api_emails()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_generic_api_emails(new_rows)
        return True


def list_generic_api_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        account_by_email = {
            (a.get("email") or "").lower(): a
            for a in _load_accounts()
        }
        rows = _load_generic_api_emails()
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows = sorted(rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        return [_decorate_generic_api_email(r, account_by_email) for r in rows[:limit]]


def generic_api_email_pool_summary() -> dict:
    with _LOCK:
        out = {"available": 0, "used": 0, "failed": 0}
        for row in _load_generic_api_emails():
            status = row.get("status") or "available"
            out[status] = out.get(status, 0) + 1
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def get_generic_api_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_generic_api_emails(), email)
        return _decorate_generic_api_email(row) if row else None


# ============================================================
# iCloud email pool
# ============================================================

def import_icloud_emails(records: list[dict]) -> tuple[int, int]:
    """批量导入 iCloud 邮箱，记录格式为 {email, code_url}。"""
    with _LOCK:
        rows = _load_icloud_emails()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            code_url = (raw.get("code_url") or raw.get("url") or "").strip()
            if not email or not code_url:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "code_url": code_url,
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _icloud_email_line(row)
            rows.append(row)
            inserted += 1
        _save_icloud_emails(rows)
        return inserted, skipped


def claim_next_icloud_email() -> dict | None:
    """原子领取一个可用 iCloud 邮箱并标记为 used。"""
    with _LOCK:
        rows = sorted(_load_icloud_emails(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["note"] = None
        _save_icloud_emails(rows)
        return _decorate_icloud_email(row)


def release_icloud_email(email: str, status: str = "available", note: str | None = None) -> None:
    """把 iCloud 邮箱状态改为 available/used/failed/disabled。"""
    with _LOCK:
        rows = _load_icloud_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_icloud_emails(rows)


def release_unconsumed_icloud_email(email: str, note: str | None = None) -> bool:
    """回收尚未生成本地账号且仍为 used 的 iCloud 邮箱。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_icloud_emails()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_icloud_emails(rows)
        return True


def delete_icloud_email(email: str) -> bool:
    """从 iCloud 邮箱池删除一个邮箱。"""
    with _LOCK:
        rows = _load_icloud_emails()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_icloud_emails(new_rows)
        return True


def list_icloud_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    """列出 iCloud 邮箱池，附带关联注册账号的 Token 摘要。"""
    with _LOCK:
        account_by_email = {(a.get("email") or "").lower(): a for a in _load_accounts()}
        rows = _load_icloud_emails()
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows = sorted(rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        return [_decorate_icloud_email(r, account_by_email) for r in rows[:limit]]


def icloud_email_pool_summary() -> dict:
    """统计 iCloud 邮箱池各状态数量。"""
    with _LOCK:
        out = {"available": 0, "used": 0, "failed": 0}
        for row in _load_icloud_emails():
            status = row.get("status") or "available"
            out[status] = out.get(status, 0) + 1
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def get_icloud_email_by_email(email: str) -> dict | None:
    """按邮箱查找 iCloud 池记录。"""
    with _LOCK:
        row = _find_by_email(_load_icloud_emails(), email)
        return _decorate_icloud_email(row) if row else None


# ============================================================
# Codex 授权账号（来自 codex_accounts/codex-邮箱-plan.json）
# ============================================================

def _load_codex_export_state() -> dict:
    """读导出状态映射 {filename: {exported_at, exported_count}}。不存在返回 {}。"""
    if _uses_sqlite(_CODEX_EXPORT_STATE, _DEFAULT_CODEX_EXPORT_STATE):
        data = _sqlite_store().load_document("codex_export_state", {})
        return data if isinstance(data, dict) else {}
    data = _read_json(_CODEX_EXPORT_STATE, {})
    return data if isinstance(data, dict) else {}


def _save_codex_export_state(state: dict) -> None:
    if _uses_sqlite(_CODEX_EXPORT_STATE, _DEFAULT_CODEX_EXPORT_STATE):
        _sqlite_store().replace_document("codex_export_state", state)
    _write_json(_CODEX_EXPORT_STATE, state)


def list_codex_accounts() -> list[dict]:
    """
    扫 codex_accounts/ 目录，每个 codex-*.json 是一条 CPA 兼容凭证。
    返回带元信息的列表（含导出状态、文件大小、token 预览等）。
    """
    with _LOCK:
        out = []
        if not _CODEX_DIR.exists():
            return out
        export_state = _load_codex_export_state()
        for path in sorted(_CODEX_DIR.glob("codex-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                content = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            fname = path.name
            es = export_state.get(fname) or {}
            # 从文件名抽 email 和 plan：codex-{email}.json 或 codex-{email}-{plan}.json
            stem = path.stem  # codex-邮箱-plan
            without_prefix = stem[len("codex-"):] if stem.startswith("codex-") else stem
            # plan 可能为空。简单做法：直接读 JSON 里的 email（更准），文件名只做 fallback
            email = content.get("email") or ""
            if not email:
                # JSON 里 email 为空（旧 bug 产物），从文件名兜底
                # 文件名格式 codex-{email}-{plan}.json，email 里可能有 - 但是常见邮箱不会有
                # 简单做法：去掉末尾 -plan（如 -free / -plus / -team），剩下的当 email
                parts = without_prefix.rsplit("-", 1)
                if len(parts) == 2 and parts[1].lower() in ("free", "plus", "team", "pro", "enterprise"):
                    email = parts[0]
                else:
                    email = without_prefix
            # 推断 plan
            plan = ""
            if "-" in without_prefix:
                tail = without_prefix.rsplit("-", 1)[-1].lower()
                if tail in ("free", "plus", "team", "pro", "enterprise"):
                    plan = tail
            out.append({
                "filename": fname,
                "path": str(path),
                "email": email,
                "plan": plan,
                "account_id": content.get("account_id", ""),
                "type": content.get("type", "codex"),
                "last_refresh": content.get("last_refresh", ""),
                "expired": content.get("expired", ""),
                "access_token_preview": (content.get("access_token", "") or "")[:32],
                "size": path.stat().st_size,
                "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                "exported_at": es.get("exported_at"),
                "exported_count": es.get("exported_count", 0),
            })
        return out


def read_codex_credential(filename: str) -> tuple[str, str]:
    """
    读取一个 codex-*.json 文件原始内容。
    Returns: (content_string, filename)
    抛 ValueError：文件名不合法（防目录穿越）/ 不存在。
    """
    with _LOCK:
        # 防注入：只允许 codex-*.json 模式，不允许路径分隔符
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        path = _CODEX_DIR / filename
        if not path.exists() or not path.is_file():
            raise ValueError(f"文件不存在: {filename}")
        return path.read_text(encoding="utf-8"), filename


def mark_codex_exported(filename: str) -> dict:
    """
    标记某个 codex 凭证已导出（导出计数 +1，记录最近导出时间）。
    Returns: 该 filename 当前的导出状态记录。
    """
    with _LOCK:
        state = _load_codex_export_state()
        rec = state.get(filename) or {"exported_count": 0}
        rec["exported_count"] = int(rec.get("exported_count", 0)) + 1
        rec["exported_at"] = _now()
        state[filename] = rec
        _save_codex_export_state(state)
        return rec


def reset_codex_exported(filename: str) -> None:
    """清掉某个 codex 凭证的导出状态（用户想重置时用）。"""
    with _LOCK:
        state = _load_codex_export_state()
        if filename in state:
            del state[filename]
            _save_codex_export_state(state)


def delete_codex_credential(filename: str) -> bool:
    """删除一个本地 codex-*.json 凭证文件，并清理导出状态。"""
    with _LOCK:
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        path = _CODEX_DIR / filename
        if not path.exists() or not path.is_file():
            return False
        path.unlink()
        state = _load_codex_export_state()
        if filename in state:
            del state[filename]
            _save_codex_export_state(state)
        return True


def codex_accounts_summary() -> dict:
    """codex 账号汇总：总数 / 已导出 / 未导出。"""
    with _LOCK:
        rows = list_codex_accounts()
        total = len(rows)
        exported = sum(1 for r in rows if r.get("exported_count", 0) > 0)
        return {
            "total": total,
            "exported": exported,
            "pending": total - exported,
        }


# ============================================================
# registration_jobs
# ============================================================

_REGISTRATION_TERMINAL_STATUSES = {"success", "failed", "stopped", "cancelled"}


def _parse_local_datetime(value: Any) -> datetime | None:
    """解析项目内不带时区的 ISO 时间，异常值按空处理。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _elapsed_seconds(started_at: Any, completed_at: Any = None, *, now: datetime | None = None) -> int:
    """计算非负墙钟耗时秒数，供前端格式化为时:分:秒。"""
    start = _parse_local_datetime(started_at)
    end = _parse_local_datetime(completed_at) or now or datetime.now()
    if start is None:
        return 0
    try:
        return max(0, int((end - start).total_seconds()))
    except TypeError:
        # 兼容历史数据中带时区和不带时区时间混用的情况。
        return 0


def _registration_batch_snapshot(batch: dict, jobs: list[dict], *, now: datetime | None = None) -> dict:
    """根据批次关联任务生成实时统计，不依赖前端自行猜测任务状态。"""
    current_time = now or datetime.now()
    if str(batch.get("status") or "") == "completed" and batch.get("completed_at"):
        # 批次终态一旦落盘就保持不变；后续删除单任务记录不应改写历史成功/失败统计。
        result = dict(batch)
        result["elapsed_seconds"] = _elapsed_seconds(batch.get("started_at"), batch.get("completed_at"), now=current_time)
        return result
    batch_id = int(batch.get("id") or 0)
    configured_ids = {
        int(item) for item in (batch.get("job_ids") or [])
        if str(item).strip().lstrip("-").isdigit()
    }
    related = [
        row for row in jobs
        if int(row.get("batch_id") or 0) == batch_id
        or (configured_ids and int(row.get("id") or 0) in configured_ids)
    ]
    status_counts: dict[str, int] = {}
    for row in related:
        status = str(row.get("status") or "pending")
        status_counts[status] = status_counts.get(status, 0) + 1

    requested = max(0, int(batch.get("requested_count") or batch.get("submitted_count") or len(related)))
    sealed = bool(batch.get("sealed_at"))
    missing = max(0, requested - len(related))
    success_count = int(status_counts.get("success", 0) or 0)
    failed_count = sum(int(status_counts.get(status, 0) or 0) for status in ("failed", "stopped", "cancelled"))
    if sealed:
        # 已封口后仍缺失的任务代表队列创建未完整落盘，计入失败，保证成功+失败与注册量一致。
        failed_count += missing
        pending_count = int(status_counts.get("pending", 0) or 0)
    else:
        pending_count = int(status_counts.get("pending", 0) or 0) + missing
    running_count = sum(int(status_counts.get(status, 0) or 0) for status in ("running", "stopping"))
    terminal_count = success_count + failed_count
    is_completed = sealed and requested > 0 and terminal_count >= requested and running_count == 0 and pending_count == 0

    completed_at = str(batch.get("completed_at") or "").strip() or None
    if is_completed and not completed_at:
        job_completed = [
            _parse_local_datetime(row.get("completed_at"))
            for row in related
            if str(row.get("status") or "") in _REGISTRATION_TERMINAL_STATUSES
        ]
        valid_completed = [item for item in job_completed if item is not None]
        completed_at = (max(valid_completed) if valid_completed else current_time).isoformat(timespec="seconds")

    result = dict(batch)
    result.update({
        "submitted_count": len(related),
        "success_count": success_count,
        "failed_count": failed_count,
        "running_count": running_count,
        "pending_count": pending_count,
        "completed_count": terminal_count,
        "status": "completed" if is_completed else "running",
        "completed_at": completed_at,
        "elapsed_seconds": _elapsed_seconds(batch.get("started_at"), completed_at, now=current_time),
    })
    return result


def create_registration_batch(*, requested_count: int, workers: int, email_source: str) -> dict:
    """创建一次“开始注册”操作对应的持久化批次日志。"""
    with _LOCK:
        rows = _load_registration_batches()
        now_iso = _now()
        row = {
            "id": _next_id(rows),
            "started_at": now_iso,
            "completed_at": None,
            "sealed_at": None,
            "requested_count": max(0, int(requested_count or 0)),
            "submitted_count": 0,
            "workers": max(1, int(workers or 1)),
            "email_source": str(email_source or ""),
            "job_ids": [],
            "success_count": 0,
            "failed_count": 0,
            "running_count": 0,
            "pending_count": max(0, int(requested_count or 0)),
            "status": "running",
            "created_at": now_iso,
        }
        rows.append(row)
        _save_registration_batches(rows)
        return dict(row)


def seal_registration_batch(batch_id: int, job_ids: list[int]) -> dict | None:
    """提交完本批全部任务后封口，使批次可准确判断完成和失败数量。"""
    with _LOCK:
        batches = _load_registration_batches()
        row = next((item for item in batches if int(item.get("id") or 0) == int(batch_id)), None)
        if row is None:
            return None
        row["job_ids"] = [int(item) for item in job_ids]
        row["submitted_count"] = len(row["job_ids"])
        row["sealed_at"] = _now()
        snapshot = _registration_batch_snapshot(row, _load_jobs())
        row.update(snapshot)
        _save_registration_batches(batches)
        return dict(snapshot)


def list_registration_batches(limit: int = 200) -> list[dict]:
    """返回最新批次历史，并实时刷新执行中批次的耗时和成功/失败统计。"""
    with _LOCK:
        batches = _load_registration_batches()
        jobs = _load_jobs()
        now = datetime.now()
        changed = False
        snapshots: list[dict] = []
        for row in batches:
            snapshot = _registration_batch_snapshot(row, jobs, now=now)
            snapshots.append(snapshot)
            # 终态字段只需在完成时固化；执行中耗时保持实时计算，避免每秒写盘。
            if snapshot.get("status") == "completed" and any(
                row.get(key) != snapshot.get(key)
                for key in ("completed_at", "success_count", "failed_count", "running_count", "pending_count", "completed_count", "status", "elapsed_seconds")
            ):
                row.update(snapshot)
                changed = True
        if changed:
            _save_registration_batches(batches)
        snapshots.sort(key=lambda item: int(item.get("id") or 0), reverse=True)
        return [dict(item) for item in snapshots[:max(1, int(limit or 200))]]


def get_registration_batch(batch_id: int) -> dict | None:
    """按批次 ID 获取实时统计。"""
    return next((item for item in list_registration_batches(limit=1_000_000) if int(item.get("id") or 0) == int(batch_id)), None)


def clear_registration_batches(*, keep_active: bool = True) -> dict:
    """清空批次历史；默认保留仍有任务执行或排队的批次。"""
    with _LOCK:
        batches = _load_registration_batches()
        jobs = _load_jobs()
        snapshots = [_registration_batch_snapshot(row, jobs) for row in batches]
        active_ids = {
            int(item.get("id") or 0)
            for item in snapshots
            if item.get("status") != "completed"
        } if keep_active else set()
        kept = [row for row in batches if int(row.get("id") or 0) in active_ids]
        cleared = len(batches) - len(kept)
        _save_registration_batches(kept)
        return {"cleared": cleared, "kept_active": len(kept)}

def _new_job_row(
    rows: list[dict],
    *,
    email_source: str,
    job_type: str = "registration",
    parent_job_id: int | None = None,
    root_job_id: int | None = None,
    retry_attempt: int = 0,
    retry_action: str | None = None,
    email: str | None = None,
    account_id: int | None = None,
    batch_id: int | None = None,
) -> dict:
    job_uuid = str(uuid.uuid4())
    log_file = str(_LOG_DIR / f"{job_uuid}.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    return {
        "id": _next_id(rows),
        "job_uuid": job_uuid,
        "job_type": job_type,
        "parent_job_id": parent_job_id,
        "root_job_id": root_job_id,
        "retry_attempt": int(retry_attempt or 0),
        "retry_action": retry_action,
        "email_source": email_source,
        "email": email,
        "status": "pending",
        "error_message": None,
        "log_file": log_file,
        "started_at": None,
        "completed_at": None,
        "account_id": account_id,
        "batch_id": batch_id,
        "created_at": _now(),
    }


def create_job(email_source: str, *, batch_id: int | None = None) -> dict:
    """创建一个首次执行的 pending 注册任务。"""
    with _LOCK:
        rows = _load_jobs()
        row = _new_job_row(rows, email_source=email_source, batch_id=batch_id)
        rows.append(row)
        _save_jobs(rows)
        return dict(row)


def create_retry_job(
    source_job_id: int,
    *,
    job_type: str,
    email_source: str,
    email: str | None = None,
    account_id: int | None = None,
) -> tuple[dict, bool]:
    """原子创建重试子任务；同一任务链已有活跃任务时直接复用。"""
    with _LOCK:
        rows = _load_jobs()
        source = next((r for r in rows if int(r.get("id") or 0) == int(source_job_id)), None)
        if source is None:
            raise LookupError("任务不存在")
        if source.get("status") not in ("failed", "stopped", "cancelled"):
            raise ValueError(f"当前状态不支持重试：{source.get('status')}")

        root_id = int(source.get("root_job_id") or source.get("id"))
        active_states = {"pending", "running", "stopping"}
        active = next((
            r for r in rows
            if int(r.get("id") or 0) != int(source_job_id)
            and int(r.get("root_job_id") or 0) == root_id
            and r.get("status") in active_states
        ), None)
        if active is not None:
            if active.get("job_type", "registration") != job_type:
                raise ValueError(f"已有其他类型重试任务 #{active.get('id')} 在排队或运行中")
            return dict(active), False

        attempts = [
            int(r.get("retry_attempt") or 0)
            for r in rows
            if int(r.get("id") or 0) == root_id or int(r.get("root_job_id") or 0) == root_id
        ]
        row = _new_job_row(
            rows,
            email_source=email_source,
            job_type=job_type,
            parent_job_id=int(source_job_id),
            root_job_id=root_id,
            retry_attempt=(max(attempts) if attempts else 0) + 1,
            retry_action=("codex" if job_type == "codex_retry" else "registration"),
            email=email,
            account_id=account_id,
        )
        rows.append(row)
        _save_jobs(rows)
        return dict(row), True


def update_job(
    job_id: int,
    *,
    status: str | None = None,
    email: str | None = None,
    error: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    account_id: int | None = None,
) -> None:
    with _LOCK:
        rows = _load_jobs()
        row = next((r for r in rows if int(r.get("id") or 0) == int(job_id)), None)
        if row is None:
            return
        if status is not None:
            row["status"] = status
        if email is not None:
            row["email"] = email
        if error is not None:
            row["error_message"] = error
        if started_at is not None:
            row["started_at"] = started_at
        if completed_at is not None:
            row["completed_at"] = completed_at
        if account_id is not None:
            row["account_id"] = account_id
        _save_jobs(rows)


def list_jobs(limit: int = 100) -> list[dict]:
    with _LOCK:
        rows = sorted(_load_jobs(), key=lambda x: int(x.get("id") or 0), reverse=True)
        return [dict(r) for r in rows[:limit]]


def get_job(job_id: int) -> dict | None:
    with _LOCK:
        row = next((r for r in _load_jobs() if int(r.get("id") or 0) == int(job_id)), None)
        return dict(row) if row else None


def get_successful_retry_for_job(job_id: int) -> dict | None:
    """返回同一任务链中已成功的其他重试任务，用于保留原任务历史状态并阻止重复重试。"""
    with _LOCK:
        rows = _load_jobs()
        source = next((r for r in rows if int(r.get("id") or 0) == int(job_id)), None)
        if source is None:
            return None
        root_id = int(source.get("root_job_id") or source.get("id") or 0)
        matches = [
            r for r in rows
            if int(r.get("id") or 0) != int(job_id)
            and int(r.get("root_job_id") or 0) == root_id
            and r.get("status") == "success"
        ]
        if not matches:
            return None
        return dict(max(matches, key=lambda r: int(r.get("id") or 0)))


def delete_job(job_id: int, *, delete_log: bool = True, allow_running: bool = False) -> bool:
    """
    删除一个注册任务记录；默认同时删除该任务日志文件。返回是否删除到记录。
    默认不删除 running 任务，避免后台线程仍在执行但前端记录消失。
    """
    with _LOCK:
        rows = _load_jobs()
        idx = next((i for i, r in enumerate(rows) if int(r.get("id") or 0) == int(job_id)), None)
        if idx is None:
            return False
        if not allow_running and rows[idx].get("status") in ("running", "stopping"):
            return False
        row = rows.pop(idx)
        _save_jobs(rows)

    if delete_log:
        log_file = row.get("log_file")
        if log_file:
            try:
                Path(log_file).unlink(missing_ok=True)
            except Exception:
                pass
    return True


# ============================================================
# 迁移与路径
# ============================================================

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _migrate_legacy_sqlite() -> dict:
    summary = {"sqlite_accounts_imported": 0, "sqlite_outlook_imported": 0, "sqlite_outlook_skipped": 0}
    if not _LEGACY_SQLITE.exists():
        return summary
    try:
        conn = sqlite3.connect(str(_LEGACY_SQLITE))
        conn.row_factory = sqlite3.Row
        if _table_exists(conn, "outlook_pool"):
            records = []
            statuses = []
            for row in conn.execute("SELECT * FROM outlook_pool").fetchall():
                records.append({
                    "email": row["email"],
                    "password": row["password"],
                    "client_id": row["client_id"],
                    "refresh_token": row["refresh_token"],
                })
                statuses.append({
                    "email": row["email"],
                    "status": row["status"],
                    "note": row["note"],
                })
            ins, skip = import_outlook_accounts(records)
            for item in statuses:
                if item["status"] != "available":
                    release_outlook(item["email"], status=item["status"], note=item["note"])
            summary["sqlite_outlook_imported"] += ins
            summary["sqlite_outlook_skipped"] += skip
        if _table_exists(conn, "registered_accounts"):
            for row in conn.execute("SELECT * FROM registered_accounts").fetchall():
                insert_account(
                    email=row["email"],
                    access_token=row["access_token"],
                    totp_secret=row["totp_secret"],
                    user_id=row["user_id"],
                    user_name=row["user_name"],
                    plan_type=row["plan_type"],
                    expires_at=row["expires_at"],
                    device_id=row["device_id"],
                    proxy_used=row["proxy_used"],
                    email_source=row["email_source"],
                    extra=json.loads(row["extra_json"]) if row["extra_json"] else None,
                )
                summary["sqlite_accounts_imported"] += 1
        conn.close()
    except Exception as exc:
        summary["sqlite_error"] = f"{type(exc).__name__}: {exc}"
    return summary


def migrate_legacy_files() -> dict:
    """
    把历史 SQLite、accounts/*.json、outlook_accounts.txt、outlook_accounts_used.json
    导入当前主存储。多次调用是幂等的；JSON/TXT 会继续同步为兼容镜像。
    """
    summary = {
        "accounts_imported": 0,
        "outlook_imported": 0,
        "outlook_skipped": 0,
    }
    summary.update(_migrate_legacy_sqlite())

    accounts_dir = _PROJECT_ROOT / "accounts"
    if accounts_dir.exists():
        for jf in accounts_dir.glob("*.json"):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                if not data.get("email") or not data.get("access_token"):
                    continue
                extra = data.get("extra") or {}
                user = extra.get("user") or {}
                account = extra.get("account") or {}
                insert_account(
                    email=data["email"],
                    access_token=data["access_token"],
                    totp_secret=data.get("totp_secret"),
                    user_id=user.get("id"),
                    user_name=user.get("name"),
                    plan_type=account.get("planType"),
                    expires_at=extra.get("expires"),
                    device_id=extra.get("device_id"),
                    extra=extra,
                )
                summary["accounts_imported"] += 1
            except Exception:
                continue

    for txt in (_PROJECT_ROOT / "outlook_accounts.txt", _OUTLOOK_TXT):
        if txt.exists():
            records = []
            for line in txt.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("----")
                # 支持 4 段或 6 段格式
                if len(parts) == 4:
                    email, password, client_id, refresh_token = (p.strip() for p in parts)
                elif len(parts) == 6:
                    email, password, client_id, refresh_token, _, _ = (p.strip() for p in parts)
                else:
                    continue
                records.append({
                    "email": email,
                    "password": password,
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                })
            ins, skip = import_outlook_accounts(records)
            summary["outlook_imported"] += ins
            summary["outlook_skipped"] += skip

    used = _PROJECT_ROOT / "outlook_accounts_used.json"
    if used.exists():
        try:
            emails = json.loads(used.read_text(encoding="utf-8"))
            for email in emails:
                release_outlook(email, status="used")
        except Exception:
            pass

    return summary


def db_path() -> Path:
    """Return the authoritative SQLite path, or the JSON directory in rollback mode."""
    return _SQLITE_PATH if _storage_backend() != "json" else _DATA_DIR


def storage_paths() -> dict:
    return {
        "backend": "sqlite" if _storage_backend() != "json" else "json",
        "sqlite": str(_SQLITE_PATH),
        "outlook_json": str(_OUTLOOK_JSON),
        "outlook_txt": str(_OUTLOOK_TXT),
        "generic_api_json": str(_GENERIC_API_EMAIL_JSON),
        "generic_api_txt": str(_GENERIC_API_EMAIL_TXT),
        "icloud_json": str(_ICLOUD_EMAIL_JSON),
        "icloud_txt": str(_ICLOUD_EMAIL_TXT),
        "accounts_json": str(_ACCOUNTS_JSON),
        "accounts_txt": str(_ACCOUNTS_TXT),
        "tokens_txt": str(_TOKENS_TXT),
        "domain_json": str(_DOMAIN_EMAIL_JSON),
        "groups_json": str(_GROUPS_JSON),
        "codex_export_state": str(_CODEX_EXPORT_STATE),
        "viewer_html": str(_VIEWER_HTML),
        "jobs_json": str(_JOBS_JSON),
        "registration_batches_json": str(_REGISTRATION_BATCHES_JSON),
        "logs_dir": str(_LOG_DIR),
    }


def refresh_static_viewer() -> Path:
    """手动刷新静态查看器，返回 HTML 路径。"""
    with _LOCK:
        outlook_rows = _load_outlook()
        account_rows = _load_accounts()
        _sync_outlook_txt(outlook_rows)
        _sync_accounts_txt(account_rows)
        _sync_tokens_txt(account_rows)
        return _render_static_viewer(outlook_rows=outlook_rows, account_rows=account_rows)


# ============================================================
# Domain email pool（Cloudflare 域名邮箱跟踪）
# ============================================================


def _load_domain_pool() -> list[dict]:
    if _uses_sqlite(_DOMAIN_EMAIL_JSON, _DEFAULT_DOMAIN_EMAIL_JSON):
        return _sqlite_store().load_records("domain_email_pool")
    rows = _read_json(_DOMAIN_EMAIL_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_domain_pool(rows: list[dict]) -> None:
    if _uses_sqlite(_DOMAIN_EMAIL_JSON, _DEFAULT_DOMAIN_EMAIL_JSON):
        _sqlite_store().replace_records("domain_email_pool", rows)
    _write_json(_DOMAIN_EMAIL_JSON, rows)


def _find_domain_email(rows: list[dict], email: str) -> dict | None:
    target = (email or "").lower()
    return next((r for r in rows if (r.get("email") or "").lower() == target), None)


def claim_next_domain_email(email: str) -> dict:
    """记录一个新的域名邮箱地址到池中（标记为 available）。"""
    with _LOCK:
        rows = _load_domain_pool()
        if _find_domain_email(rows, email):
            # 已存在，直接返回
            row = _find_domain_email(rows, email)
            return row
        row = {
            "id": _next_id(rows),
            "email": email,
            "status": "available",
            "used_at": None,
            "note": None,
            "created_at": _now(),
        }
        rows.append(row)
        _save_domain_pool(rows)
        return dict(row)


def release_domain_email(email: str, status: str = "available", note: str | None = None) -> None:
    """更新域名邮箱状态。"""
    with _LOCK:
        rows = _load_domain_pool()
        row = _find_domain_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_domain_pool(rows)


def release_unconsumed_domain_email(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的域名邮箱。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_domain_pool()
        row = _find_domain_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_domain_pool(rows)
        return True


def get_domain_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_domain_email(_load_domain_pool(), email)
        return dict(row) if row else None


def list_domain_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        rows = sorted(_load_domain_pool(), key=lambda x: int(x.get("id") or 0), reverse=True)
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return [dict(r) for r in rows[:limit]]


def domain_email_pool_summary() -> dict:
    with _LOCK:
        out: dict[str, int] = {"available": 0, "used": 0, "failed": 0}
        for row in _load_domain_pool():
            s = row.get("status") or "available"
            out[s] = out.get(s, 0) + 1
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def delete_domain_email(email: str) -> bool:
    """从域名邮箱池删除一个邮箱。"""
    with _LOCK:
        rows = _load_domain_pool()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_domain_pool(new_rows)
        return True
