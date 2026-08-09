# -*- coding: utf-8 -*-
"""
注册成功后自动触发 Flow 的配置项。
设置 ENABLE_FLOW_TRIGGER = False 可完全跳过此步骤。
"""
import json

from config.env_loader import apply_env_overrides, env_str

# 是否启用自动触发 Flow（False = 跳过，不影响注册结果）
ENABLE_FLOW_TRIGGER: bool = False

# Flow 触发接口地址
FLOW_TRIGGER_URL: str = ""

# Bearer Token（Authorization 头）
FLOW_TRIGGER_BEARER: str = ""

# Cookie 字符串
FLOW_TRIGGER_COOKIE: str = ""

# 发送的 JSON payload（会把 access_token 注入进去）
FLOW_TRIGGER_PAYLOAD: dict = {}

# 请求超时（秒）
FLOW_TRIGGER_TIMEOUT: int = 15

# ---- .env overrides for WebUI editable fields ----
# Flow 配置由 WebUI 写入项目根 .env；此前只有开关被热加载，导致 URL 一直为空，
# 最终在 requests.post() 内抛出 MissingSchema。这里把所有运行时字段一起加载。
apply_env_overrides(globals(), {
    'ENABLE_FLOW_TRIGGER': 'bool',
    'FLOW_TRIGGER_URL': 'str',
    'FLOW_TRIGGER_BEARER': 'str',
    'FLOW_TRIGGER_COOKIE': 'str',
    'FLOW_TRIGGER_TIMEOUT': 'int',
})


def _load_payload() -> dict:
    """从 .env 读取 JSON payload；空值或非法 JSON 均回退为空对象。"""
    raw = env_str("FLOW_TRIGGER_PAYLOAD", "")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


FLOW_TRIGGER_PAYLOAD = _load_payload()
