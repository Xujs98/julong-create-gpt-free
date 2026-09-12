# -*- coding: utf-8 -*-
"""账号查活与账号换绑专用配置。

查活保留一个独立驱动设置。换绑在需要时可拆成两个阶段：先用指纹
浏览器建立登录态，再用协议提交邮箱变更（混合模式）。旧版只传一个
``driver`` 的调用仍由换绑服务兼容。
"""
from config.env_loader import apply_env_overrides


# 查活驱动：protocol=纯协议；cloak=CloakBrowser；roxy=RoxyBrowser。
LIVE_CHECK_DRIVER: str = "cloak"

# 仅在查活驱动为 cloak/roxy 时生效，不修改对应浏览器的注册无头配置。
LIVE_CHECK_HEADLESS: bool = False

# 查活可优先复用注册代理，其余出口来源统一跟随 config.proxy.PROXY_MODE。
LIVE_CHECK_USE_REGISTRATION_PROXY: bool = True
# 仅地区由查活单独指定：account=跟随账号注册地区，Rand=随机，或 ISO 国家码。
# API 地址、数量、会话、时长、格式、超时与尝试次数统一读取代理池配置。
LIVE_CHECK_PROXY_API_REGION: str = "account"

# 换绑阶段配置。``protocol``、``cloak``、``roxy`` 均可用；混合模式开启
# 时分别使用登录驱动和变更驱动。默认组合为“Cloak 登录 + 协议换绑”。
REBIND_LOGIN_DRIVER: str = "cloak"
REBIND_ACTION_DRIVER: str = "protocol"
REBIND_HYBRID_MODE: bool = True
# 协议登录遇到 403/429/代理连接失败时，最多筛选多少个独立代理出口。
REBIND_PROXY_MAX_ATTEMPTS: int = 8


apply_env_overrides(globals(), {
    "LIVE_CHECK_DRIVER": "str",
    "LIVE_CHECK_HEADLESS": "bool",
    "LIVE_CHECK_USE_REGISTRATION_PROXY": "bool",
    "LIVE_CHECK_PROXY_API_REGION": "str",
    "REBIND_LOGIN_DRIVER": "str",
    "REBIND_ACTION_DRIVER": "str",
    "REBIND_HYBRID_MODE": "bool",
    "REBIND_PROXY_MAX_ATTEMPTS": "int",
})
