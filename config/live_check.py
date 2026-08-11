# -*- coding: utf-8 -*-
"""账号查活专用配置；与注册驱动及浏览器注册无头设置相互独立。"""
from config.env_loader import apply_env_overrides


# 查活驱动：protocol=纯协议；cloak=CloakBrowser；roxy=RoxyBrowser。
LIVE_CHECK_DRIVER: str = "cloak"

# 仅在查活驱动为 cloak/roxy 时生效，不修改对应浏览器的注册无头配置。
LIVE_CHECK_HEADLESS: bool = False


apply_env_overrides(globals(), {
    "LIVE_CHECK_DRIVER": "str",
    "LIVE_CHECK_HEADLESS": "bool",
})
