# -*- coding: utf-8 -*-
"""注册流量优化配置。

优化只针对注册阶段的可选请求：核心 document/script/xhr/fetch/websocket
以及 Cloudflare/Sentinel 挑战资源始终保留。所有开关均可通过 ``.env`` 或
WebUI 热加载，关闭后恢复原始浏览器请求行为。
"""
from config.env_loader import apply_env_overrides


# 注册浏览器默认启用低风险流量优化；协议驱动的核心请求不受此开关影响。
REGISTRATION_TRAFFIC_OPTIMIZATION: bool = True

# 阻断 Datadog/Statsig/Segment 等非注册必需的分析上报。
REGISTRATION_BLOCK_ANALYTICS: bool = True

# 阻断登录表单不需要的图片、字体、音视频资源。挑战域名不在资源拦截主机里，
# 因此 Cloudflare/Turnstile 的 document/script/xhr 仍完整保留。
REGISTRATION_BLOCK_MEDIA: bool = True

# 只拦截这些明确的分析主机；其中 ab.chatgpt.com 是独立的 Statsig/A-B
# 初始化与上报端点，不承载 chatgpt.com/auth.openai.com 的认证请求。
REGISTRATION_ANALYTICS_HOSTS: tuple[str, ...] = (
    "ab.chatgpt.com",
    "browser-intake-datadoghq.com",
    "*.browser-intake-datadoghq.com",
    "browser-intake-datadoghq.eu",
    "*.browser-intake-datadoghq.eu",
    "segment.io",
    "*.segment.io",
    "segment.com",
    "*.segment.com",
    "statsig.com",
    "*.statsig.com",
    "google-analytics.com",
    "*.google-analytics.com",
    "googletagmanager.com",
    "*.googletagmanager.com",
    "doubleclick.net",
    "*.doubleclick.net",
    "hotjar.com",
    "*.hotjar.com",
    "fullstory.com",
    "*.fullstory.com",
    "sentry.io",
    "*.sentry.io",
)

# 仅在核心站点上拦截表单无关的静态资源；挑战/验证码主机不在此列表。
REGISTRATION_MEDIA_HOSTS: tuple[str, ...] = (
    "chatgpt.com",
    "auth.openai.com",
    "auth-cdn.oaistatic.com",
    "cdn.oaistatic.com",
)

REGISTRATION_MEDIA_EXTENSIONS: tuple[str, ...] = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".webm", ".mp3", ".m4a",
)

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    "REGISTRATION_TRAFFIC_OPTIMIZATION": "bool",
    "REGISTRATION_BLOCK_ANALYTICS": "bool",
    "REGISTRATION_BLOCK_MEDIA": "bool",
})
