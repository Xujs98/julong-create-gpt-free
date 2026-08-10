# -*- coding: utf-8 -*-
"""页面 Agent 配置。

provider:
  disabled            关闭 Agent
  local               仅使用本地 DOM 识别与动作执行，不需要外部模型
  openai_compatible   使用 OpenAI-compatible Chat Completions 接口
"""
from config.env_loader import apply_env_overrides, env_str

PAGE_AGENT_PROVIDER: str = "disabled"
PAGE_AGENT_API_BASE: str = ""
PAGE_AGENT_API_KEY: str = env_str("PAGE_AGENT_API_KEY", "")
PAGE_AGENT_MODEL: str = ""
PAGE_AGENT_TIMEOUT: int = 20
PAGE_AGENT_MAX_STEPS: int = 4
PAGE_AGENT_TEMPERATURE: float = 0.0
# 由 WebUI “测试 Agent 配置”成功后写入；任一连接配置变化会重置为 False。
PAGE_AGENT_VALIDATED: bool = False


def effective_config(overrides: dict | None = None) -> dict:
    values = {
        "provider": str(PAGE_AGENT_PROVIDER or "disabled").strip().lower(),
        "api_base": str(PAGE_AGENT_API_BASE or "").strip().rstrip("/"),
        "api_key": str(PAGE_AGENT_API_KEY or "").strip(),
        "model": str(PAGE_AGENT_MODEL or "").strip(),
        "timeout": max(3, int(PAGE_AGENT_TIMEOUT or 20)),
        "max_steps": max(1, min(12, int(PAGE_AGENT_MAX_STEPS or 4))),
        "temperature": max(0.0, min(1.0, float(PAGE_AGENT_TEMPERATURE or 0.0))),
        "validated": bool(PAGE_AGENT_VALIDATED),
    }
    for key, value in (overrides or {}).items():
        name = str(key).upper()
        mapping = {
            "PAGE_AGENT_PROVIDER": "provider",
            "PAGE_AGENT_API_BASE": "api_base",
            "PAGE_AGENT_API_KEY": "api_key",
            "PAGE_AGENT_MODEL": "model",
            "PAGE_AGENT_TIMEOUT": "timeout",
            "PAGE_AGENT_MAX_STEPS": "max_steps",
            "PAGE_AGENT_TEMPERATURE": "temperature",
            "PAGE_AGENT_VALIDATED": "validated",
        }
        target = mapping.get(name)
        if target:
            values[target] = value
    values["provider"] = str(values["provider"] or "disabled").strip().lower()
    values["api_base"] = str(values["api_base"] or "").strip().rstrip("/")
    values["api_key"] = str(values["api_key"] or "").strip()
    values["model"] = str(values["model"] or "").strip()
    if isinstance(values["validated"], str):
        values["validated"] = values["validated"].strip().lower() in {"true", "1", "yes", "on", "y"}
    else:
        values["validated"] = bool(values["validated"])
    return values


def configuration_status(overrides: dict | None = None) -> dict:
    cfg = effective_config(overrides)
    provider = cfg["provider"]
    if provider == "local":
        return {
            "configured": bool(cfg["validated"]), "provider": provider,
            "reason": "local_dom_agent" if cfg["validated"] else "请先点击“测试 Agent 配置”",
        }
    if provider in {"openai", "openai_compatible", "compatible"}:
        missing = [name for name, value in (("API 地址", cfg["api_base"]), ("API Key", cfg["api_key"]), ("模型", cfg["model"])) if not value]
        if missing:
            return {"configured": False, "provider": provider, "reason": "缺少" + "、".join(missing)}
        return {
            "configured": bool(cfg["validated"]), "provider": "openai_compatible",
            "reason": "api_configured" if cfg["validated"] else "请先点击“测试 Agent 配置”",
        }
    return {"configured": False, "provider": provider or "disabled", "reason": "provider_disabled"}


apply_env_overrides(globals(), {
    "PAGE_AGENT_PROVIDER": "str",
    "PAGE_AGENT_API_BASE": "str",
    "PAGE_AGENT_API_KEY": "str",
    "PAGE_AGENT_MODEL": "str",
    "PAGE_AGENT_TIMEOUT": "int",
    "PAGE_AGENT_MAX_STEPS": "int",
    "PAGE_AGENT_TEMPERATURE": "float",
    "PAGE_AGENT_VALIDATED": "bool",
})
