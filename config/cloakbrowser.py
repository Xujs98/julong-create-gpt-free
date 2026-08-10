# -*- coding: utf-8 -*-
"""CloakBrowser 自动化注册配置。"""
from config.env_loader import apply_env_overrides

# 是否无头启动：False=显示窗口，True=无头。
CLOAK_HEADLESS: bool = True

# 是否启用 CloakBrowser humanize 行为。
CLOAK_HUMANIZE: bool = True

# 使用当前出口 IP 自动匹配时区/语言/WebRTC IP。
CLOAK_GEOIP: bool = True

# 显式指定 Cloak 语言/时区；留空则在 CLOAK_GEOIP=True 时按出口 IP 自动推断。
# 例如：CLOAK_LOCALE="ja-JP"，CLOAK_TIMEZONE="Asia/Tokyo"。
CLOAK_LOCALE: str = ""
CLOAK_TIMEZONE: str = ""

# 是否把本项目传入/代理池抽取的代理传给 CloakBrowser。
CLOAK_USE_PROXY: bool = True

# Pro license；留空则使用免费 binary。
CLOAK_LICENSE_KEY: str = ""

# 固定指纹 seed；留空则每次 launch 随机生成新指纹。
CLOAK_FINGERPRINT_SEED: str = ""

# 每次启动显式生成全新指纹 seed；开启后忽略固定 seed 和持久化用户目录，
# 使用新的临时上下文，避免 cookies/cache 与新指纹交叉复用。
CLOAK_RANDOMIZE_FINGERPRINT_EACH_LAUNCH: bool = True

# 持久化用户目录；留空则临时上下文。若要固定账号画像/缓存，可填如 "./cloak-profiles/default"。
CLOAK_USER_DATA_DIR: str = ""

# 额外 Chromium 参数，例如 ["--fingerprint=12345"]。CLOAK_FINGERPRINT_SEED 会自动追加。
CLOAK_EXTRA_ARGS: list = []

# 与原 Roxy Selenium 流程共用的超时时间。
CLOAK_SELENIUM_TIMEOUT: int = 90

# 检测到 Cloudflare 交互式人机验证时，保持可见浏览器等待人工完成的时间。
CLOAK_CHALLENGE_TIMEOUT: int = 300

# 调试时保留浏览器不自动关闭。
CLOAK_KEEP_BROWSER_OPEN: bool = False

# 注册异常时保留窗口，便于查看实际页面状态；成功完成后仍按 CLOAK_KEEP_BROWSER_OPEN 清理。
CLOAK_KEEP_BROWSER_OPEN_ON_ERROR: bool = True

# 页面 Agent：必须先在“页面 Agent”配置中选择 provider 并通过配置校验，
# 本开关才允许开启。模式可选 hybrid（异常时介入）或 takeover（每个阶段主动接管）。
CLOAK_ENABLE_AGENT: bool = False
CLOAK_AGENT_MODE: str = "hybrid"

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'CLOAK_HEADLESS': 'bool', 'CLOAK_HUMANIZE': 'bool', 'CLOAK_GEOIP': 'bool', 'CLOAK_LOCALE': 'str', 'CLOAK_TIMEZONE': 'str', 'CLOAK_USE_PROXY': 'bool', 'CLOAK_LICENSE_KEY': 'str', 'CLOAK_FINGERPRINT_SEED': 'str', 'CLOAK_RANDOMIZE_FINGERPRINT_EACH_LAUNCH': 'bool', 'CLOAK_USER_DATA_DIR': 'str', 'CLOAK_SELENIUM_TIMEOUT': 'int', 'CLOAK_CHALLENGE_TIMEOUT': 'int', 'CLOAK_KEEP_BROWSER_OPEN': 'bool', 'CLOAK_KEEP_BROWSER_OPEN_ON_ERROR': 'bool', 'CLOAK_ENABLE_AGENT': 'bool', 'CLOAK_AGENT_MODE': 'str'})
