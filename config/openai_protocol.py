# -*- coding: utf-8 -*-
"""
OpenAI / ChatGPT OAuth 协议固定参数

来自抓包，OpenAI 自己的 client_id 是固定值。
SENTINEL_SV 是 sdk.js 的版本号，会随 OpenAI 更新而变化，
更新时去 https://sentinel.openai.com/sentinel/<version>/sdk.js 找当前版本。
"""

from config.env_loader import apply_env_overrides

# OAuth 客户端 ID（固定）
OPENAI_CLIENT_ID = "app_X8zY6vW2pQ9tR3dE7nK1jL5gH"

# OAuth scopes
OPENAI_SCOPE = (
    "openid email profile offline_access "
    "model.request model.read "
    "organization.read organization.write"
)

# OAuth audience
OPENAI_AUDIENCE = "https://api.openai.com/v1"

# OAuth 回调（chatgpt.com 端）
OPENAI_REDIRECT_URI = "https://chatgpt.com/api/auth/callback/openai"

# Sentinel SDK 版本号（影响 sentinel iframe URL 与 referer header）
SENTINEL_SV = "20260219f9f6"

# ChatGPT 页面 build 标识（用于 Sentinel p[6] / documentElement data-build 模拟）
OPENAI_BUILD_ID = "prod-fb4a8a2a751dfec391053cfd7b01c52699ccf78c"

# ChatGPT 前端 CES / API 上报头，来自 2026-07-19 抓包。
OAI_CLIENT_BUILD_NUMBER = "8370486"
OAI_CLIENT_VERSION = OPENAI_BUILD_ID

# Statsig / Analytics SDK 版本，纯协议补齐前端同形态链路时使用。
STATSIG_CLIENT_KEY = "client-nb0qtYlZuy2tCMN5s5ncnuIBCJncjRViT0IzFm7GqST"
STATSIG_SDK_VERSION = "3.32.6"
STATSIG_SDK_TYPE = "javascript-client"
AB_CLIENT_KEY = "client-tN5GMyzpIPKXd3KNv7ANIfiqjRSvNNTTWbZdbdabF58"
AB_SDK_VERSION = "3.32.4"

# HAR 中 email-otp/validate 未携带 Sentinel；默认按 HAR 对齐，保留开关便于回退。
SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE = False

# 是否补齐 HAR 中 ChatGPT Web 首屏 bootstrap 预热链路。
# 注册核心链路不依赖这些登录后首页请求；默认关闭可明显减少每个账号的
# /backend-anon 与 /backend-api 额外响应，遇到需要完整 Web 上下文的出口时再开启。
CHATGPT_ANON_BOOTSTRAP_ENABLED = False
CHATGPT_AUTH_BOOTSTRAP_ENABLED = False
# True 时预热失败会中断主流程；默认 False，仅记录日志并继续。
CHATGPT_BOOTSTRAP_STRICT = False

# 注册前网络预检：full=ChatGPT/Auth/Sentinel 三段，minimal=只检查 ChatGPT 登录页，
# off=跳过预检（真正的 providers/CSRF/authorize 请求仍会做完整错误处理）。
PROTOCOL_PREFLIGHT_MODE = "minimal"

# 纯协议注册是否补齐真实 ChatGPT 登录页、CES/Statsig 前端上下文，并使用
# login_or_signup 入口。默认关闭，保持原有协议流程和请求量不变。
PROTOCOL_BROWSER_LIKE_FLOW = False

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    "SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE": "bool",
    "CHATGPT_ANON_BOOTSTRAP_ENABLED": "bool",
    "CHATGPT_AUTH_BOOTSTRAP_ENABLED": "bool",
    "CHATGPT_BOOTSTRAP_STRICT": "bool",
    "PROTOCOL_PREFLIGHT_MODE": "str",
    "PROTOCOL_BROWSER_LIKE_FLOW": "bool",
})
