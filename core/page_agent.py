# -*- coding: utf-8 -*-
"""页面识别 Agent：本地 DOM Agent + OpenAI-compatible 模型适配。

Agent 只接收脱敏后的页面结构；邮箱、密码、OTP 等敏感值通过 value_ref
由调用方注入，不发送给模型。动作集合限制为 click/fill/wait/done。
"""
from __future__ import annotations

import ast
import json
import logging
import re
import time
from dataclasses import dataclass, field

import requests

from config import page_agent as _cfg
from core.proxy_utils import masked_proxy_url, normalize_proxy_url

logger = logging.getLogger(__name__)


class PageAgentConfigError(RuntimeError):
    pass


@dataclass
class AgentResult:
    ok: bool
    stage: str
    actions: list[dict] = field(default_factory=list)
    executed: int = 0
    executed_actions: list[dict] = field(default_factory=list)
    reason: str = ""
    snapshot: dict = field(default_factory=dict)


def _visible_script() -> str:
    return "!!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)) && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'"


def _safe_text(value: object, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _post_model_request(
    cfg: dict,
    *,
    url: str,
    headers: dict,
    payload: dict,
):
    """按页面 Agent 配置选择直连或代理池出口发送模型请求。"""
    route = str(cfg.get("network_route") or "direct").strip().lower()
    session = requests.Session()
    # 两种模式都忽略系统 HTTP(S)_PROXY，确保出口只由页面 Agent 配置决定。
    session.trust_env = False
    request_kwargs = {
        "headers": headers,
        "json": payload,
        "timeout": cfg["timeout"],
    }
    if route == "proxy_pool":
        from config.proxy import pick_proxy

        proxy_url = normalize_proxy_url(pick_proxy(), default_scheme="auto")
        if not proxy_url:
            session.close()
            raise PageAgentConfigError("页面 Agent 已选择代理池出口，但代理池为空")
        request_kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
        logger.info("[Agent] 模型请求使用代理池出口：%s", masked_proxy_url(proxy_url))
    else:
        logger.info("[Agent] 模型请求使用本机直连出口")

    try:
        return session.post(url, **request_kwargs)
    finally:
        session.close()


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
          const marker = 'page-agent-' + fallback;
          el.setAttribute('data-page-agent-id', marker);
          return '[data-page-agent-id=\"' + CSS.escape(marker) + '\"]';
        }};
        const nodes = (selectorText, limit) => [...document.querySelectorAll(selectorText)]
          .filter(visible).slice(0, limit);
        const inputs = nodes('input,textarea,select', 24).map((el, i) => ({{
          selector: selector(el, 'input-' + i),
          tag: el.tagName.toLowerCase(), type: el.type || '', name: el.name || '',
          id: el.id || '', autocomplete: el.autocomplete || '', inputmode: el.inputMode || '',
          aria: el.getAttribute('aria-label') || '', placeholder: el.placeholder || '',
          disabled: !!el.disabled, checked: !!el.checked,
          valuePresent: /checkbox|radio/.test(el.type || '') ? !!el.checked : !!String(el.value || '')
        }}));
        const buttons = nodes('button,a,[role=button],input[type=submit],input[type=button]', 36).map((el, i) => ({{
          selector: selector(el, 'button-' + i),
          tag: el.tagName.toLowerCase(), text: (el.innerText || el.textContent || el.value || '').replace(/\\s+/g, ' ').trim().slice(0, 120),
          aria: el.getAttribute('aria-label') || '', disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
        }}));
        const challengeFrames = nodes('iframe[src*="challenges.cloudflare.com"],iframe[title*="Cloudflare"],iframe[src*="turnstile"],.cf-turnstile,[data-cf-challenge]', 12).map((el, i) => ({{
          selector: selector(el, 'challenge-' + i),
          tag: el.tagName.toLowerCase(),
          title: el.getAttribute('title') || '',
          src: el.getAttribute('src') || '',
          aria: el.getAttribute('aria-label') || ''
        }}));
        return {{url: location.href, title: document.title, text: (document.body?.innerText || '').slice(0, 2400), inputs, buttons, challenge_frames: challengeFrames}};
        """
        try:
            return driver.execute_script(script) or {}
        except Exception as exc:
            return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}

    def _local_actions(self, stage: str, snapshot: dict, context: dict) -> list[dict]:
        inputs = list(snapshot.get("inputs") or [])
        buttons = list(snapshot.get("buttons") or [])
        def match_input(words, exclude=()):
            for item in inputs:
                attrs = " ".join(str(item.get(k) or "") for k in ("type", "name", "id", "autocomplete", "inputmode", "aria", "placeholder")).lower()
                if not item.get("disabled") and any(word in attrs for word in words) and not any(word in attrs for word in exclude):
                    return item
            return None
        def match_button(words, exclude=()):
            for item in buttons:
                attrs = " ".join(str(item.get(k) or "") for k in ("text", "aria")).lower()
                if not item.get("disabled") and any(word in attrs for word in words) and not any(word in attrs for word in exclude):
                    return item
            return None

        actions: list[dict] = []
        if stage == "challenge":
            # Turnstile 控件位于跨域 iframe，交由浏览器适配层按 frame 选择器点击。
            target = next((item for item in (snapshot.get("challenge_frames") or []) if item.get("selector")), None)
            if target:
                actions.append({"type": "click", "selector": target.get("selector"), "target": "challenge"})
        elif stage == "email" and context.get("email"):
            target = match_input(("email", "username")) or next((x for x in inputs if x.get("tag") == "input" and not x.get("disabled")), None)
            if target and not target.get("valuePresent"):
                actions.append({"type": "fill", "selector": target.get("selector"), "value_ref": "email"})
            # 输入值与提交按钮分开决策：完全接管模式每轮重读 HTML，
            # 第二轮看到邮箱已有值时仍应点击 Continue，而不是返回空动作。
            button = match_button(
                ("continue", "next", "sign up", "signup", "注册", "继续", "次へ"),
                exclude=("google", "apple", "microsoft", "facebook", "github"),
            )
            if button:
                actions.append({"type": "click", "selector": button.get("selector")})
        elif stage in {"otp", "email_otp"} and context.get("otp"):
            target = match_input(("one-time", "otp", "code", "numeric", "tel"))
            if not target:
                plain = [x for x in inputs if x.get("tag") == "input" and not x.get("disabled")]
                target = plain[0] if len(plain) == 1 else None
            if target and not target.get("valuePresent"):
                actions.append({"type": "fill", "selector": target.get("selector"), "value_ref": "otp"})
            # OTP 已由上一轮填入时，当前轮继续提交验证码。
            button = match_button(("continue", "verify", "submit", "确认", "继续", "認証", "次へ"))
            if button:
                actions.append({"type": "click", "selector": button.get("selector")})
        elif stage == "password_entry":
            button = match_button((
                "use password", "continue with password", "password to continue",
                "使用密码", "密码继续", "パスワードで続行", "パスワードを使用",
            ))
            if button:
                actions.append({"type": "click", "selector": button.get("selector")})
        elif stage in {"password", "create_password"} and context.get("password"):
            target = match_input(("password", "passwort", "パスワード", "密码", "密碼"))
            if target and not target.get("valuePresent"):
                actions.append({"type": "fill", "selector": target.get("selector"), "value_ref": "password"})
            # 密码字段已有值时仍需推进注册流程。
            button = match_button(("continue", "create", "register", "signup", "use password", "続行", "登録", "继续"))
            if button:
                actions.append({"type": "click", "selector": button.get("selector")})
        elif stage in {"profile", "about_you"}:
            name_target = match_input(
                ("fullname", "full-name", "given-name", "displayname", "display-name", "your name", "name", "名前", "姓名"),
                exclude=("username", "email"),
            )
            if name_target and context.get("name") and not name_target.get("valuePresent"):
                actions.append({"type": "fill", "selector": name_target.get("selector"), "value_ref": "name"})
            birthday_target = match_input(("birthday", "birthdate", "birth-date", "date-of-birth", "dob"))
            if birthday_target and context.get("birthday") and not birthday_target.get("valuePresent"):
                actions.append({"type": "fill", "selector": birthday_target.get("selector"), "value_ref": "birthday"})
            for ref, words in (
                ("birth_year", ("birth-year", "birthyear", "year")),
                ("birth_month", ("birth-month", "birthmonth", "month")),
                ("birth_day", ("birth-day", "birthday-day", "day")),
            ):
                target = match_input(words)
                if target and context.get(ref) and not target.get("valuePresent"):
                    actions.append({"type": "fill", "selector": target.get("selector"), "value_ref": ref})
            for item in inputs:
                if str(item.get("type") or "").lower() == "checkbox" and not item.get("checked"):
                    actions.append({"type": "click", "selector": item.get("selector")})
            button = match_button(("continue", "submit", "finish", "done", "继续", "完成", "続行", "次へ"))
            if button:
                actions.append({"type": "click", "selector": button.get("selector")})
        else:
            button = match_button(("continue", "next", "submit", "继续", "続行", "次へ"))
            if button:
                actions.append({"type": "click", "selector": button.get("selector")})
        return actions

    @staticmethod
    def _parse_model_json(content: object) -> dict:
        """兼容纯 JSON、Markdown 代码块、前后解释文本与 Python 风格字典。"""
        if isinstance(content, dict):
            return content
        if isinstance(content, list):
            content = "".join(
                str(item.get("text") or item.get("content") or "")
                for item in content
                if isinstance(item, dict)
            )
        text = str(content or "").strip()
        candidates = [text]
        candidates.extend(re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE))
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            candidates.append(text[start:end + 1])
        for candidate in candidates:
            value = str(candidate or "").strip().strip("`").strip()
            if not value:
                continue
            try:
                parsed = json.loads(value)
            except Exception:
                try:
                    parsed = ast.literal_eval(value)
                except Exception:
                    continue
            if isinstance(parsed, dict):
                return parsed
        raise ValueError("模型响应中未找到有效 JSON 动作对象")

    def _model_actions(self, stage: str, snapshot: dict, context: dict) -> list[dict]:
        available_refs = [
            key for key in ("email", "otp", "password", "name", "birthday", "birth_year", "birth_month", "birth_day")
            if context.get(key) is not None
        ]
        system = (
            "Return JSON only: {actions:[{type:'click'|'fill'|'wait'|'done',selector?,value_ref?}]} . "
            "Use only selectors listed in the page snapshot. Never return literal values; use only an available value_ref. "
            "Do not choose Google, Apple, Microsoft, Facebook, GitHub, or other social-login controls."
        )
        prompt = json.dumps({"stage": stage, "available_value_refs": available_refs, "page": snapshot}, ensure_ascii=False)
        url = self.config["api_base"] + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.config['api_key']}", "Content-Type": "application/json"}
        payload = {
            "model": self.config["model"],
            "temperature": self.config["temperature"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        resp = _post_model_request(
            self.config,
            url=url,
            headers=headers,
            payload=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        choice = (data.get("choices") or [{}])[0] if isinstance(data, dict) else {}
        message = choice.get("message") or {}
        content = (
            message.get("content")
            or message.get("reasoning_content")
            or choice.get("text")
            or (data.get("output_text") if isinstance(data, dict) else "")
            or ""
        )
        tool_calls = message.get("tool_calls") or []
        if not content and tool_calls:
            content = ((tool_calls[0].get("function") or {}).get("arguments") or "")
        parsed = self._parse_model_json(content)
        raw_actions = parsed.get("actions") if isinstance(parsed.get("actions"), list) else []
        allowed_selectors = {
            str(item.get("selector") or "")
            for item in list(snapshot.get("inputs") or []) + list(snapshot.get("buttons") or [])
            if item.get("selector")
        }
        allowed_selectors.update(
            str(item.get("selector") or "")
            for item in (snapshot.get("challenge_frames") or [])
            if item.get("selector")
        )
        challenge_selectors = {
            str(item.get("selector") or "")
            for item in (snapshot.get("challenge_frames") or [])
            if item.get("selector")
        }
        input_by_selector = {
            str(item.get("selector") or ""): item
            for item in (snapshot.get("inputs") or [])
            if item.get("selector")
        }
        button_by_selector = {
            str(item.get("selector") or ""): item
            for item in (snapshot.get("buttons") or [])
            if item.get("selector")
        }
        actions = []
        for raw in raw_actions:
            if not isinstance(raw, dict):
                continue
            action = dict(raw)
            kind = str(action.get("type") or "").strip().lower()
            selector = str(action.get("selector") or "").strip()
            value_ref = str(action.get("value_ref") or "").strip()
            if kind not in {"click", "fill", "wait", "done"}:
                continue
            if kind in {"click", "fill"} and selector not in allowed_selectors:
                continue
            if kind == "fill" and value_ref not in available_refs:
                continue
            if kind == "fill" and input_by_selector.get(selector, {}).get("valuePresent"):
                # 单步接管中，已填写的控件下一轮应由 Agent 决定点击/提交，
                # 避免模型重复填值导致 React 页面一直停留原步骤。
                continue
            if kind == "click":
                button_text = " ".join(
                    str(button_by_selector.get(selector, {}).get(k) or "")
                    for k in ("text", "aria")
                ).lower()
                if any(word in button_text for word in ("google", "apple", "microsoft", "facebook", "github")):
                    continue
                if selector in challenge_selectors:
                    # 标记为 challenge 后由 CloakBrowser 适配层进入对应 iframe。
                    action["target"] = "challenge"
            action["type"] = kind
            actions.append(action)
        return actions

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
        if kind == "click" and str(action.get("target") or "").lower() == "challenge":
            clicker = getattr(driver, "click_challenge_frame", None)
            if callable(clicker):
                return bool(clicker(selector))
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
            const proto=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:
              el.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype;
            const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set; if(setter) setter.call(el,value); else el.value=value;
            el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); return true;
            """
            return bool(driver.execute_script(script, selector, str(value)))
        return False

    def assist(
        self,
        driver,
        stage: str,
        context: dict | None = None,
        *,
        force: bool = False,
        snapshot: dict | None = None,
        max_actions: int | None = None,
    ) -> AgentResult:
        context = dict(context or {})
        if self.mode == "hybrid" and not force:
            return AgentResult(ok=True, stage=stage, reason="hybrid_waiting_for_fallback")
        snapshot = snapshot if snapshot is not None else self.snapshot(driver)
        try:
            reason = "actions_executed"
            if stage == "challenge":
                # 验证盾属于结构明确的 iframe 控件，直接走本地快速处理，
                # 避免先等待一次模型网络请求再点击。
                actions = self._local_actions(stage, snapshot, context)
                reason = "challenge_fast_path"
            elif self.provider == "local":
                actions = self._local_actions(stage, snapshot, context)
            else:
                try:
                    actions = self._model_actions(stage, snapshot, context)
                except Exception as exc:
                    logger.warning(
                        "[Agent] stage=%s 模型动作解析失败，转入本地 DOM 动作：%s: %s",
                        stage, type(exc).__name__, str(exc)[:180],
                    )
                    actions = self._local_actions(stage, snapshot, context)
                    reason = f"model_fallback_local:{type(exc).__name__}"
                if not actions:
                    actions = self._local_actions(stage, snapshot, context)
                    reason = "model_empty_fallback_local"
            action_limit = max_actions if max_actions is not None else int(self.config["max_steps"])
            actions = actions[: max(1, int(action_limit))]
            executed_actions = [action for action in actions if self._execute(driver, action, context)]
            return AgentResult(
                ok=bool(executed_actions) or not actions,
                stage=stage,
                actions=actions,
                executed=len(executed_actions),
                executed_actions=executed_actions,
                reason=reason,
                snapshot=snapshot,
            )
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
        resp = _post_model_request(
            cfg,
            url=cfg["api_base"] + "/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
            payload={"model": cfg["model"], "messages":[{"role":"user","content":"Reply with OK"}], "max_tokens": 4, "temperature": 0},
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
