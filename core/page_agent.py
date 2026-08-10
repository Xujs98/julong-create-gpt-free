# -*- coding: utf-8 -*-
"""页面识别 Agent：本地 DOM Agent + OpenAI-compatible 模型适配。

Agent 只接收脱敏后的页面结构；邮箱、密码、OTP 等敏感值通过 value_ref
由调用方注入，不发送给模型。动作集合限制为 click/fill/wait/done。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

import requests

from config import page_agent as _cfg

logger = logging.getLogger(__name__)


class PageAgentConfigError(RuntimeError):
    pass


@dataclass
class AgentResult:
    ok: bool
    stage: str
    actions: list[dict] = field(default_factory=list)
    executed: int = 0
    reason: str = ""
    snapshot: dict = field(default_factory=dict)


def _visible_script() -> str:
    return "!!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)) && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'"


def _safe_text(value: object, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


class PageAgent:
    def __init__(self, *, mode: str = "hybrid"):
        status = _cfg.configuration_status()
        if not status["configured"]:
            raise PageAgentConfigError(f"页面 Agent 未配置：{status['reason']}")
        self.config = _cfg.effective_config()
        self.mode = str(mode or "hybrid").strip().lower()
        if self.mode not in {"hybrid", "takeover"}:
            self.mode = "hybrid"

    @property
    def provider(self) -> str:
        return str(self.config.get("provider") or "local")

    def snapshot(self, driver) -> dict:
        script = f"""
        const visible = el => {_visible_script()};
        const selector = (el, fallback) => {{
          if (el.id) return '#' + CSS.escape(el.id);
          for (const key of ['name','autocomplete','data-testid','data-index']) {{
            const value = el.getAttribute(key);
            if (value) return el.tagName.toLowerCase() + '[' + key + '=\"' + CSS.escape(value) + '\"]';
          }}
          return fallback;
        }};
        const nodes = (selectorText, limit) => [...document.querySelectorAll(selectorText)]
          .filter(visible).slice(0, limit);
        const inputs = nodes('input,textarea,select', 24).map((el, i) => ({{
          selector: selector(el, 'input:nth-of-type(' + (i + 1) + ')'),
          tag: el.tagName.toLowerCase(), type: el.type || '', name: el.name || '',
          id: el.id || '', autocomplete: el.autocomplete || '', inputmode: el.inputMode || '',
          aria: el.getAttribute('aria-label') || '', placeholder: el.placeholder || '',
          disabled: !!el.disabled, valuePresent: !!String(el.value || '')
        }}));
        const buttons = nodes('button,a,[role=button],input[type=submit],input[type=button]', 36).map((el, i) => ({{
          selector: selector(el, 'button:nth-of-type(' + (i + 1) + ')'),
          tag: el.tagName.toLowerCase(), text: (el.innerText || el.textContent || el.value || '').replace(/\\s+/g, ' ').trim().slice(0, 120),
          aria: el.getAttribute('aria-label') || '', disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
        }}));
        return {{url: location.href, title: document.title, text: (document.body?.innerText || '').slice(0, 2400), inputs, buttons}};
        """
        try:
            return driver.execute_script(script) or {}
        except Exception as exc:
            return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}

    def _local_actions(self, stage: str, snapshot: dict, context: dict) -> list[dict]:
        inputs = list(snapshot.get("inputs") or [])
        buttons = list(snapshot.get("buttons") or [])
        def match_input(words):
            for item in inputs:
                attrs = " ".join(str(item.get(k) or "") for k in ("type", "name", "id", "autocomplete", "inputmode", "aria", "placeholder")).lower()
                if not item.get("disabled") and any(word in attrs for word in words):
                    return item
            return None
        def match_button(words):
            for item in buttons:
                attrs = " ".join(str(item.get(k) or "") for k in ("text", "aria")).lower()
                if not item.get("disabled") and any(word in attrs for word in words):
                    return item
            return None

        actions: list[dict] = []
        if stage == "email" and context.get("email"):
            target = match_input(("email", "username")) or next((x for x in inputs if x.get("tag") == "input" and not x.get("disabled")), None)
            if target:
                actions.append({"type": "fill", "selector": target.get("selector"), "value_ref": "email"})
                button = match_button(("continue", "next", "sign up", "signup", "注册", "继续", "次へ"))
                if button:
                    actions.append({"type": "click", "selector": button.get("selector")})
        elif stage in {"otp", "email_otp"} and context.get("otp"):
            target = match_input(("one-time", "otp", "code", "numeric", "tel"))
            if not target:
                plain = [x for x in inputs if x.get("tag") == "input" and not x.get("disabled")]
                target = plain[0] if len(plain) == 1 else None
            if target:
                actions.append({"type": "fill", "selector": target.get("selector"), "value_ref": "otp"})
                button = match_button(("continue", "verify", "submit", "确认", "继续", "認証", "次へ"))
                if button:
                    actions.append({"type": "click", "selector": button.get("selector")})
        elif stage in {"password", "create_password"} and context.get("password"):
            target = match_input(("password", "passwort", "パスワード", "密码", "密碼"))
            if target:
                actions.append({"type": "fill", "selector": target.get("selector"), "value_ref": "password"})
                button = match_button(("continue", "create", "register", "signup", "use password", "続行", "登録", "继续"))
                if button:
                    actions.append({"type": "click", "selector": button.get("selector")})
        else:
            button = match_button(("continue", "next", "submit", "继续", "続行", "次へ"))
            if button:
                actions.append({"type": "click", "selector": button.get("selector")})
        return actions

    def _model_actions(self, stage: str, snapshot: dict) -> list[dict]:
        system = (
            "Return JSON only: {actions:[{type:'click'|'fill'|'wait'|'done',selector?,value_ref?}]} . "
            "Use only selectors listed in the page snapshot. Never return literal secrets; use value_ref email, otp, password."
        )
        prompt = json.dumps({"stage": stage, "page": snapshot}, ensure_ascii=False)
        url = self.config["api_base"] + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.config['api_key']}", "Content-Type": "application/json"}
        payload = {
            "model": self.config["model"],
            "temperature": self.config["temperature"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=self.config["timeout"])
        resp.raise_for_status()
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            content = "".join(str(x.get("text") or "") for x in content if isinstance(x, dict))
        parsed = json.loads(str(content).strip().strip("`"))
        return parsed.get("actions") if isinstance(parsed, dict) and isinstance(parsed.get("actions"), list) else []

    def _execute(self, driver, action: dict, context: dict) -> bool:
        kind = str(action.get("type") or "").lower()
        selector = str(action.get("selector") or "").strip()
        if kind == "wait":
            time.sleep(min(5.0, max(0.1, float(action.get("seconds") or 0.5))))
            return True
        if kind == "done":
            return True
        if not selector or any(token in selector for token in ("javascript:", "<", ">")):
            return False
        if kind == "click":
            result = driver.execute_script("const el=document.querySelector(arguments[0]); if(!el) return false; el.scrollIntoView({block:'center'}); el.click(); return true;", selector)
            return bool(result)
        if kind == "fill":
            ref = str(action.get("value_ref") or "").strip()
            value = context.get(ref)
            if value is None:
                return False
            script = """
            const el=document.querySelector(arguments[0]); if(!el || el.disabled) return false;
            const value=String(arguments[1]); el.focus();
            const proto=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
            const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set; if(setter) setter.call(el,value); else el.value=value;
            el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); return true;
            """
            return bool(driver.execute_script(script, selector, str(value)))
        return False

    def assist(self, driver, stage: str, context: dict | None = None, *, force: bool = False) -> AgentResult:
        context = dict(context or {})
        if self.mode == "hybrid" and not force:
            return AgentResult(ok=True, stage=stage, reason="hybrid_waiting_for_fallback")
        snapshot = self.snapshot(driver)
        try:
            actions = self._local_actions(stage, snapshot, context) if self.provider == "local" else self._model_actions(stage, snapshot)
            actions = actions[: int(self.config["max_steps"])]
            executed = sum(1 for action in actions if self._execute(driver, action, context))
            return AgentResult(ok=executed > 0 or not actions, stage=stage, actions=actions, executed=executed, reason="actions_executed", snapshot=snapshot)
        except Exception as exc:
            logger.warning("[Agent] stage=%s 执行失败：%s: %s", stage, type(exc).__name__, str(exc)[:180])
            return AgentResult(ok=False, stage=stage, reason=f"{type(exc).__name__}: {exc}", snapshot=snapshot)


def agent_status() -> dict:
    return _cfg.configuration_status()


def test_configuration() -> dict:
    # 测试动作本身就是建立 validated 门禁的入口，因此不能先要求
    # configuration_status() 已经 configured=True；先检查原始配置，
    # 成功后再持久化 PAGE_AGENT_VALIDATED=True。
    cfg = _cfg.effective_config()
    provider = str(cfg.get("provider") or "disabled").strip().lower()

    def _persist_validation(value: bool) -> None:
        from config.env_loader import write_env_values

        write_env_values({"PAGE_AGENT_VALIDATED": "True" if value else "False"})
        try:
            import config as config_package

            config_package.reload_all()
        except Exception:
            logger.exception("[Agent] 持久化验证状态后热加载失败")

    if provider == "disabled":
        _persist_validation(False)
        return {
            "ok": False,
            "configured": False,
            "provider": provider,
            "reason": "provider_disabled",
            "message": "请先选择 local 或 openai_compatible",
        }

    if provider == "local":
        _persist_validation(True)
        status = _cfg.configuration_status()
        return {**status, "ok": True, "message": "本地 DOM Agent 配置成功"}

    if provider not in {"openai", "openai_compatible", "compatible"}:
        _persist_validation(False)
        return {
            "ok": False,
            "configured": False,
            "provider": provider,
            "reason": f"不支持的 provider：{provider}",
        }

    missing = [
        label
        for label, value in (
            ("API 地址", cfg.get("api_base")),
            ("API Key", cfg.get("api_key")),
            ("模型", cfg.get("model")),
        )
        if not str(value or "").strip()
    ]
    if missing:
        _persist_validation(False)
        return {
            "ok": False,
            "configured": False,
            "provider": "openai_compatible",
            "reason": "缺少" + "、".join(missing),
        }

    try:
        resp = requests.post(
            cfg["api_base"] + "/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
            json={"model": cfg["model"], "messages":[{"role":"user","content":"Reply with OK"}], "max_tokens": 4, "temperature": 0},
            timeout=cfg["timeout"],
        )
        resp.raise_for_status()
        _persist_validation(True)
        status = _cfg.configuration_status()
        return {**status, "ok": True, "message": "模型 Agent 配置连接成功"}
    except Exception as exc:
        _persist_validation(False)
        return {
            "ok": False,
            "configured": False,
            "provider": "openai_compatible",
            "reason": f"连接失败：{type(exc).__name__}: {exc}",
        }


def attach_agent(driver, *, mode: str) -> PageAgent | None:
    status = _cfg.configuration_status()
    if not status["configured"]:
        return None
    agent = PageAgent(mode=mode)
    setattr(driver, "_page_agent", agent)
    logger.info("[Agent] 已启用 provider=%s mode=%s", agent.provider, agent.mode)
    return agent
