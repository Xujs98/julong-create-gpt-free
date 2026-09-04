# -*- coding: utf-8 -*-
"""注册模式与流量优化配置。

优化只针对注册阶段的可选请求：核心 document/script/xhr/fetch/websocket
以及 Cloudflare/Sentinel 挑战资源始终保留。所有开关均可通过 ``.env`` 或
WebUI 热加载，关闭后恢复原始浏览器请求行为。
"""
from config.env_loader import apply_env_overrides


# 注册模式：
#   default  = 保持原始浏览器和协议配置，不安装流量拦截。
#   stable   = 阻断媒体和低风险遥测，放行 A/B 初始化。
#   throttle = 在 stable 基础上追加阻断 A/B 初始化。
REGISTRATION_TRAFFIC_MODE: str = "default"

# 兼容此前的高级开关；default 模式始终保持原始请求，stable/throttle 才读取。
REGISTRATION_TRAFFIC_OPTIMIZATION: bool = True

# 阻断 Datadog/Statsig/Segment 等非注册必需的分析上报。
REGISTRATION_BLOCK_ANALYTICS: bool = True

# 阻断登录表单不需要的图片、字体、音视频资源。挑战域名不在资源拦截主机里，
# 因此 Cloudflare/Turnstile 的 document/script/xhr 仍完整保留。
REGISTRATION_BLOCK_MEDIA: bool = True

# stable 与 throttle 都阻断的低风险遥测主机。
REGISTRATION_ANALYTICS_HOSTS: tuple[str, ...] = (
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

# 仅 throttle 阻断。A/B 初始化可能参与前端功能开关，因此 stable 保持放行。
REGISTRATION_THROTTLE_ONLY_HOSTS: tuple[str, ...] = (
    "ab.chatgpt.com",
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


def normalize_registration_traffic_mode(value: str | None = None) -> str:
    """把 WebUI/.env/旧别名归一化为三种稳定机器值。"""
    raw = REGISTRATION_TRAFFIC_MODE if value is None else value
    mode = str(raw or "default").strip().lower().replace("-", "_")
    aliases = {
        "original": "default", "off": "default", "none": "default",
        "safe": "stable", "balanced": "stable",
        "saving": "throttle", "aggressive": "throttle", "low_traffic": "throttle",
    }
    mode = aliases.get(mode, mode)
    return mode if mode in {"default", "stable", "throttle"} else "default"


def effective_protocol_preflight_mode(configured: str) -> str:
    """注册模式对协议预检的有效映射；default 完整保留显式配置。"""
    if normalize_registration_traffic_mode() == "default":
        return str(configured or "full").strip().lower() or "full"
    return "minimal"


def effective_protocol_bootstrap_enabled(configured: bool) -> bool:
    """stable/throttle 关闭额外首页数据预热，default 沿用显式配置。"""
    if normalize_registration_traffic_mode() == "default":
        return bool(configured)
    return False


def effective_protocol_browser_like_enabled(configured: bool) -> bool:
    """低流量模式不追加网页化预热；default 沿用现有开关。"""
    if normalize_registration_traffic_mode() == "default":
        return bool(configured)
    return False

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    "REGISTRATION_TRAFFIC_MODE": "str",
    "REGISTRATION_TRAFFIC_OPTIMIZATION": "bool",
    "REGISTRATION_BLOCK_ANALYTICS": "bool",
    "REGISTRATION_BLOCK_MEDIA": "bool",
})
