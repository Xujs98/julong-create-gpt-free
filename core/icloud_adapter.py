# -*- coding: utf-8 -*-
"""Persisted HTML pickup URL adapters for the iCloud mailbox provider."""
from __future__ import annotations

import html
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ADAPTER_FILE = _PROJECT_ROOT / "data" / "icloud_html_adapters.json"
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read() -> list[dict]:
    if not _ADAPTER_FILE.exists():
        return []
    try:
        value = json.loads(_ADAPTER_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, ValueError, TypeError):
        return []


def _write(rows: list[dict]) -> None:
    _ADAPTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = _ADAPTER_FILE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(_ADAPTER_FILE)


def normalize_url(value: str) -> str:
    text = str(value or "").strip()
    markdown = re.fullmatch(r"\[[^\]]*\]\((https?://[^)\s]+)\)", text, re.IGNORECASE)
    if markdown:
        text = markdown.group(1)
    text = re.sub(r"\\([@_&?=:/%.#-])", r"\1", text)
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("接码地址必须是有效的 http(s) URL")
    return text


def url_key(value: str) -> str:
    parsed = urlsplit(normalize_url(value))
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path or '/'}"


def host_key(value: str) -> str:
    parsed = urlsplit(normalize_url(value))
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    return f"{hostname}:{port}" if port else hostname


def list_adapters() -> list[dict]:
    with _LOCK:
        return [dict(row) for row in reversed(_read())]


def get_adapter(adapter_id: str) -> dict | None:
    target = str(adapter_id or "").strip()
    with _LOCK:
        return next((dict(row) for row in _read() if str(row.get("id")) == target), None)


def add_adapter(value: str) -> dict:
    url = normalize_url(value)
    key = url_key(url)
    with _LOCK:
        rows = _read()
        for row in rows:
            if row.get("url_key") == key:
                return dict(row)
        row = {
            "id": uuid.uuid4().hex,
            "url": url,
            "url_key": key,
            "host_key": host_key(url),
            "selectors": [],
            "adapted": False,
            "created_at": _now(),
            "updated_at": _now(),
        }
        rows.append(row)
        _write(rows)
        return dict(row)


def delete_adapter(adapter_id: str) -> bool:
    target = str(adapter_id or "").strip()
    with _LOCK:
        rows = _read()
        kept = [row for row in rows if str(row.get("id")) != target]
        if len(kept) == len(rows):
            return False
        _write(kept)
        return True


def save_selector(adapter_id: str, selector: str) -> dict | None:
    value = str(selector or "").strip()
    if not value or len(value) > 500 or any(ch in value for ch in "\r\n"):
        raise ValueError("CSS 选择器不能为空或过长")
    with _LOCK:
        rows = _read()
        for row in rows:
            if str(row.get("id")) == str(adapter_id or "").strip():
                row["selectors"] = [value]
                row["adapted"] = True
                row["updated_at"] = _now()
                _write(rows)
                return dict(row)
    return None


def test_selector(adapter_id: str, selector: str) -> dict:
    """Fetch one pickup page and test a CSS selector without persisting it."""
    value = str(selector or "").strip()
    if not value or len(value) > 500 or any(ch in value for ch in "\r\n"):
        raise ValueError("CSS 选择器不能为空或过长")
    row = get_adapter(adapter_id)
    if not row:
        raise LookupError("适配地址不存在")
    response = requests.get(
        row["url"],
        timeout=20,
        headers={"Accept": "text/html,application/xhtml+xml,text/plain,*/*", "User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "text/html")
    if "html" not in content_type.lower() and "text" not in content_type.lower():
        raise ValueError("接码地址没有返回 HTML/文本页面")
    from core.generic_api_mail_client import _extract_html_selector_code, _extract_html_selector_values

    values = _extract_html_selector_values(response.text or "", [value])
    return {
        "ok": True,
        "matched": bool(values),
        "values": values[:5],
        "code": _extract_html_selector_code(response.text or "", [value]),
        "status_code": response.status_code,
    }


def selectors_for_url(value: str) -> list[str]:
    try:
        key = url_key(value)
        host = host_key(value)
    except ValueError:
        return []
    with _LOCK:
        rows = list(reversed(_read()))
        matches = [row for row in rows if row.get("adapted") and row.get("url_key") == key]
        matches.extend(
            row
            for row in rows
            if row.get("adapted")
            and (row.get("host_key") or _legacy_host_key(row.get("url"))) == host
            and row not in matches
        )
        for row in matches:
            selectors = [str(item) for item in row.get("selectors") or [] if str(item).strip()]
            if selectors:
                return selectors
    return []


def _legacy_host_key(value: str | None) -> str:
    """Return a host key for adapter records written before host_key existed."""
    try:
        return host_key(str(value or ""))
    except ValueError:
        return ""


def fetch_proxy_html(adapter_id: str) -> tuple[str, str]:
    row = get_adapter(adapter_id)
    if not row:
        raise LookupError("适配地址不存在")
    response = requests.get(row["url"], timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "text/html")
    if "html" not in content_type.lower() and "text" not in content_type.lower():
        raise ValueError("接码地址没有返回 HTML/文本页面")
    body = response.text or ""
    # 商家页面经常自带 frame-src/frame-ancestors CSP，嵌入适配小窗时会直接空白。
    # 代理只移除文档内的 CSP 声明，外部资源仍按原页面 base URL 加载。
    body = re.sub(
        r"<meta[^>]+http-equiv=[\"']?content-security-policy[\"']?[^>]*>",
        "",
        body,
        flags=re.IGNORECASE,
    )
    parsed_url = urlsplit(row["url"])
    public_route = parsed_url.path or "/"
    if parsed_url.query:
        public_route += "?" + parsed_url.query
    base = f'<base href="{html.escape(row["url"], quote=True)}">'
    route_script = f"<script>history.replaceState(null, '', {json.dumps(public_route)});</script>"
    script = f"""
<script>
(() => {{
  const adapterId = {json.dumps(str(adapter_id))};
  const cssPath = (node) => {{
    if (node.id) return '#' + CSS.escape(node.id);
    const parts = [];
    while (node && node.nodeType === 1 && node !== document.body && node !== document.documentElement) {{
      let part = node.tagName.toLowerCase();
      if (node.classList.length) part += '.' + Array.from(node.classList).slice(0, 3).map(CSS.escape).join('.');
      const siblings = node.parentElement ? Array.from(node.parentElement.children).filter(x => x.tagName === node.tagName) : [];
      if (siblings.length) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
      parts.unshift(part); node = node.parentElement;
    }}
    return parts.join(' > ') || 'body';
  }};
  document.addEventListener('click', (event) => {{
    const target = event.target.closest ? event.target.closest('body *') : event.target;
    if (!target) return;
    event.preventDefault(); event.stopPropagation();
    const receiver = window.parent !== window ? window.parent : window.opener;
    if (receiver) receiver.postMessage({{type:'icloud-adapter-selector', adapterId, selector:cssPath(target), sample:(target.textContent || '').trim().slice(0, 120)}}, '*');
  }}, true);
}})();
</script>
"""
    if re.search(r"<head[^>]*>", body, re.IGNORECASE):
        body = re.sub(r"(<head[^>]*>)", r"\1" + base + route_script, body, count=1, flags=re.IGNORECASE)
    else:
        body = base + route_script + body
    if re.search(r"</body>", body, re.IGNORECASE):
        body = re.sub(r"</body>", script + "</body>", body, count=1, flags=re.IGNORECASE)
    else:
        body += script
    return body, "text/html; charset=utf-8"
