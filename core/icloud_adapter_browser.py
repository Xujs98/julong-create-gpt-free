# -*- coding: utf-8 -*-
"""Launch a visible browser for selecting an iCloud HTML OTP element."""
from __future__ import annotations

import threading
import time
from typing import Any

from core.icloud_adapter import save_selector


_LOCK = threading.Lock()
_SESSIONS: dict[str, dict[str, Any]] = {}

_SELECTOR_SCRIPT = r"""
(() => {
  const cssPath = (node) => {
    if (node.id) return '#' + CSS.escape(node.id);
    const parts = [];
    while (node && node.nodeType === 1 && node !== document.body && node !== document.documentElement) {
      let part = node.tagName.toLowerCase();
      if (node.classList.length) part += '.' + Array.from(node.classList).slice(0, 3).map(CSS.escape).join('.');
      const siblings = node.parentElement
        ? Array.from(node.parentElement.children).filter(x => x.tagName === node.tagName) : [];
      if (siblings.length) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(' > ') || 'body';
  };
  const install = () => {
    if (document.getElementById('icloud-adapter-panel')) return;
    const panel = document.createElement('div');
    panel.id = 'icloud-adapter-panel';
    panel.style.cssText = 'position:fixed;z-index:2147483647;left:16px;right:16px;top:16px;padding:12px 16px;background:#fff;border:1px solid #d9dfeb;border-radius:10px;box-shadow:0 8px 28px rgba(15,23,42,.18);font:13px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;color:#263146';
    panel.innerHTML = '<b>iCloud 选择器适配</b><span id="icloud-adapter-status" style="margin-left:12px;color:#667085">点击验证码元素，选择器会自动保存</span><code id="icloud-adapter-selector" style="display:block;margin-top:7px;color:#08785f;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"></code>';
    document.documentElement.appendChild(panel);
    document.addEventListener('click', async (event) => {
      const target = event.target.closest ? event.target.closest('body *') : event.target;
      if (!target || panel.contains(target)) return;
      event.preventDefault();
      event.stopPropagation();
      const selector = cssPath(target);
      const sample = (target.textContent || '').trim().slice(0, 120);
      document.getElementById('icloud-adapter-selector').textContent = selector;
      document.getElementById('icloud-adapter-status').textContent = '正在保存选择器…';
      try {
        await window.icloudSaveSelector(selector, sample);
        document.getElementById('icloud-adapter-status').textContent = '已自动保存，后续同域名地址会复用';
      } catch (error) {
        document.getElementById('icloud-adapter-status').textContent = '保存失败：' + (error?.message || error);
      }
    }, true);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install, {once:true});
  else install();
})();
"""


def _set_session(adapter_id: str, **values: Any) -> None:
    with _LOCK:
        _SESSIONS.setdefault(adapter_id, {}).update(values)


def get_session(adapter_id: str) -> dict[str, Any] | None:
    with _LOCK:
        row = _SESSIONS.get(str(adapter_id))
        return dict(row) if row else None


def _run_browser(adapter_id: str, url: str) -> None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()

            def persist_selector(selector: str, sample: str = "") -> dict[str, Any]:
                item = save_selector(adapter_id, selector)
                if not item:
                    raise RuntimeError("适配地址不存在")
                _set_session(adapter_id, status="adapted", selector=selector, sample=sample)
                return {"ok": True}

            context.expose_function("icloudSaveSelector", persist_selector)
            context.add_init_script(_SELECTOR_SCRIPT)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            _set_session(adapter_id, status="running", url=page.url)
            while browser.is_connected() and not page.is_closed():
                time.sleep(0.5)
            browser.close()
    except Exception as exc:
        _set_session(adapter_id, status="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        with _LOCK:
            current = _SESSIONS.get(adapter_id, {})
            if current.get("status") not in {"adapted", "error"}:
                current["status"] = "closed"


def launch_public_adapter(adapter_id: str, url: str) -> dict[str, Any]:
    target = str(adapter_id or "").strip()
    with _LOCK:
        current = _SESSIONS.get(target)
        if current and current.get("status") in {"starting", "running"}:
            return {"ok": True, "status": current.get("status"), "already_open": True}
        _SESSIONS[target] = {"status": "starting", "url": url}
    thread = threading.Thread(target=_run_browser, args=(target, url), name=f"icloud-adapter-{target[:8]}", daemon=True)
    thread.start()
    return {"ok": True, "status": "starting"}
