# -*- coding: utf-8 -*-
"""
配置读写层（供 WebUI /api/config 使用）。

设计原则：
    1. 白名单：只暴露"运行时安全"的开关/数值/默认值，协议级常量
       （client_id / scope / sentinel 版本等）一律不开放，避免一改就废号。
    2. 所有 WebUI 可编辑项统一写入项目根 `.env`，不再修改 `config/*.py`。
    3. `config/*.py` 只保留默认值；运行时通过 config.env_loader 用 `.env` 覆盖。
    4. 读取时优先 `.env`，缺失时回退解析 `config/*.py` 默认值。
"""
import ast
import os
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"
EXPLICIT_EMPTY_LIST_KEYS = {"PROXY_POOL"}

# GrizzlySMS country IDs.  Keep the provider's numeric IDs as the stored values
# while exposing localized names in the WebUI; the searchable select also
# matches the raw ID so operators can paste a country code directly.
SMS_COUNTRY_OPTIONS = [
    ("175", "澳大利亚"), ("50", "奥地利"), ("35", "阿塞拜疆"), ("155", "阿尔巴尼亚"),
    ("58", "阿尔及利亚"), ("181", "安圭拉"), ("16", "英国"), ("76", "安哥拉"),
    ("39", "阿根廷"), ("148", "亚美尼亚"), ("179", "阿鲁巴岛"), ("74", "阿富汗"),
    ("122", "巴哈马"), ("60", "孟加拉国"), ("118", "巴巴多斯"), ("145", "巴林"),
    ("51", "白俄罗斯"), ("124", "伯利兹"), ("82", "比利时"), ("120", "贝宁"),
    ("83", "保加利亚"), ("92", "玻利维亚"), ("108", "波斯尼亚和黑塞哥维那"), ("123", "博茨瓦纳"),
    ("73", "巴西"), ("121", "文莱"), ("152", "布基纳法索"), ("119", "布隆迪"),
    ("158", "不丹"), ("84", "匈牙利"), ("70", "委内瑞拉"), ("91", "东帝汶"),
    ("10", "越南"), ("154", "加蓬"), ("26", "海地"), ("131", "圭亚那"),
    ("28", "冈比亚"), ("38", "加纳"), ("160", "瓜德罗普岛"), ("94", "危地马拉"),
    ("68", "几内亚"), ("130", "几内亚比绍"), ("43", "德国"), ("88", "洪都拉斯"),
    ("14", "香港"), ("127", "格林纳达"), ("129", "希腊"), ("128", "格鲁吉亚"),
    ("172", "丹麦"), ("168", "吉布提"), ("126", "多米尼克"), ("109", "多明尼加共和国"),
    ("21", "埃及"), ("147", "赞比亚"), ("96", "津巴布韦"), ("13", "以色列"),
    ("22", "印度"), ("6", "印度尼西亚"), ("116", "约旦"), ("47", "伊拉克"),
    ("10016", "伊朗"), ("23", "爱尔兰"), ("132", "冰岛"), ("56", "西班牙"),
    ("86", "意大利"), ("30", "也门"), ("186", "佛得角"), ("2", "哈萨克斯坦"),
    ("170", "开曼群岛"), ("24", "柬埔寨"), ("41", "喀麦隆"), ("36", "加拿大"),
    ("111", "卡塔尔"), ("8", "肯尼亚"), ("77", "塞浦路斯"), ("11", "吉尔吉斯斯坦"),
    ("3", "中国"), ("33", "哥伦比亚"), ("133", "科摩罗"), ("150", "刚果共和国"),
    ("18", "刚果民主共和国"), ("93", "哥斯达黎加"), ("27", "科特迪瓦"), ("100", "科威特"),
    ("25", "老挝"), ("49", "拉脱维亚"), ("136", "莱索托"), ("135", "利比里亚"),
    ("153", "黎巴嫩"), ("102", "利比亚"), ("44", "立陶宛"), ("10348", "列支敦士登"),
    ("165", "卢森堡"), ("157", "毛里求斯"), ("114", "毛里塔尼亚"), ("17", "马达加斯加"),
    ("20", "澳门"), ("137", "马拉维"), ("7", "马来西亚"), ("69", "马里"),
    ("159", "马尔代夫"), ("37", "摩洛哥"), ("54", "墨西哥"), ("80", "莫桑比克"),
    ("85", "摩尔多瓦"), ("144", "摩纳哥"), ("72", "蒙古"), ("180", "蒙特塞拉特"),
    ("5", "缅甸"), ("138", "纳米比亚"), ("81", "尼泊尔"), ("139", "尼日尔"),
    ("19", "尼日利亚"), ("48", "荷兰"), ("90", "尼加拉瓜"), ("67", "新西兰"),
    ("185", "新喀里多尼亚"), ("174", "挪威"), ("95", "阿联酋"), ("107", "阿曼"),
    ("66", "巴基斯坦"), ("112", "巴拿马"), ("79", "巴布亚新几内亚"), ("87", "巴拉圭"),
    ("65", "秘鲁"), ("15", "波兰"), ("117", "葡萄牙"), ("97", "波多黎各"),
    ("146", "留尼汪"), ("0", "俄罗斯联邦"), ("140", "卢旺达"), ("32", "罗马尼亚"),
    ("101", "萨尔瓦多"), ("10231", "萨摩亚"), ("178", "圣多美和普林西比"), ("53", "沙特阿拉伯"),
    ("106", "史瓦帝尼"), ("183", "北马其顿"), ("184", "塞舌尔"), ("61", "塞内加尔"),
    ("166", "圣文森特和格林纳丁斯"), ("134", "圣基茨和尼维斯"), ("164", "圣卢西亚"), ("29", "塞尔维亚"),
    ("10351", "新加坡"), ("10349", "圣马丁岛"), ("141", "斯洛伐克"), ("59", "斯洛文尼亚"),
    ("149", "索马里"), ("142", "苏里南"), ("12", "美国（虚拟）"), ("187", "美国"),
    ("115", "塞拉利昂"), ("143", "塔吉克斯坦"), ("52", "泰国"), ("55", "台湾"),
    ("9", "坦桑尼亚"), ("99", "多哥"), ("10227", "汤加"), ("104", "特立尼达和多巴哥"),
    ("89", "突尼斯"), ("161", "土库曼斯坦"), ("62", "土耳其"), ("75", "乌干达"),
    ("40", "乌兹别克斯坦"), ("1", "乌克兰"), ("156", "乌拉圭"), ("4", "菲律宾"),
    ("163", "芬兰"), ("78", "法国"), ("162", "法属圭亚那"), ("45", "克罗地亚"),
    ("125", "中非共和国"), ("42", "乍得"), ("171", "黑山"), ("63", "捷克共和国"),
    ("151", "智利"), ("173", "瑞士"), ("46", "瑞典"), ("64", "斯里兰卡"),
    ("105", "厄瓜多尔"), ("167", "赤道几内亚"), ("176", "厄立特里亚"), ("34", "爱沙尼亚"),
    ("71", "埃塞俄比亚"), ("31", "南非"), ("10350", "韩国"), ("177", "南苏丹"),
    ("103", "牙买加"), ("182", "日本"),
]
SMS_COUNTRY_CHOICES = [code for code, _ in SMS_COUNTRY_OPTIONS]
SMS_COUNTRY_LABELS = {code: f"{name}（{code}）" for code, name in SMS_COUNTRY_OPTIONS}


# ============================================================
# 白名单：每个可编辑项声明它在哪个文件、键名、类型、分组、说明
# type 决定前端控件 + 写回时的字面量格式：
#   bool   -> True/False
#   int    -> 整数
#   str    -> 带引号字符串
#   list_str_multiline -> 多行字符串列表（PROXY_POOL 专用，整块替换）
# ============================================================

EDITABLE_FIELDS = [
    # ---- WebUI 授权 ----
    {
        "key": "WEBUI_AUTH_CODE", "file": "codex.py", "type": "str", "group": "WebUI 授权",
        "label": "WebUI 授权码", "help": "仅保存在 .env（WEBUI_AUTH_CODE），避免出现在进程命令行中；保存后重启 WebUI 生效",
        "storage": "env", "secret": True,
    },
    {
        "key": "WEBUI_SESSION_SECRET", "file": "codex.py", "type": "str", "group": "WebUI 授权",
        "label": "Session 签名密钥", "help": "可选，保存在 .env（WEBUI_SESSION_SECRET）；不填则从固定授权码派生，修改授权码会使已有登录失效",
        "storage": "env", "secret": True,
    },
    {
        "key": "WEBUI_JOB_LOG_AUTO_REFRESH", "file": "webui.py", "type": "bool", "group": "日志管理",
        "label": "任务日志实时刷新", "help": "开启后，运行中的任务日志按刷新间隔自动同步；任务成功、失败、停止或取消后自动终止刷新",
    },
    {
        "key": "WEBUI_JOB_LOG_REFRESH_INTERVAL", "file": "webui.py", "type": "int", "group": "日志管理",
        "label": "日志刷新间隔(秒)", "help": "任务日志实时刷新间隔，建议 1-10 秒；关闭实时刷新时此项不生效",
    },
    {
        "key": "WEBUI_REGISTRATION_JOB_RETENTION_COUNT", "file": "webui.py", "type": "int", "group": "日志管理",
        "label": "注册任务保留条数", "help": "保留最近 N 条已结束注册任务；排队中、运行中及其他非终态任务始终保留，超出部分及对应日志由后台清理",
    },
    {
        "key": "ACCOUNT_LOG_AUTO_CLEANUP", "file": "webui.py", "type": "bool", "group": "日志管理",
        "label": "账号日志自动清理", "help": "开启后按保留天数自动清理查活、提链、重设2FA和Codex补跑日志；注册任务日志不受影响",
    },
    {
        "key": "ACCOUNT_LOG_RETENTION_DAYS", "file": "webui.py", "type": "int", "group": "日志管理",
        "label": "账号日志保留天数", "help": "按日志文件最后修改时间计算，超过该天数的账号日志会被后台自动清理",
        "min": 1, "max": 3650,
    },
    # ---- 功能开关 ----
    {
        "key": "ENABLE_CODEX_AUTO", "file": "codex.py", "type": "bool", "group": "功能开关",
        "label": "启用 Codex OAuth", "help": "注册成功后自动跑 Codex 授权（全新session+接码），落盘 codex-邮箱.json",
    },
    {
        "key": "PROTOCOL_BROWSER_LIKE_FLOW", "file": "openai_protocol.py", "type": "bool", "group": "功能开关",
        "label": "协议注册网页化流程", "help": "仅 protocol 纯协议驱动生效；开启后先访问 ChatGPT 登录页、补齐 CES/Statsig 前端上下文，并使用 login_or_signup 入口；关闭保持原协议流程",
    },
    {
        "key": "REGISTRATION_DRIVER", "file": "roxybrowser.py", "type": "str", "group": "注册方式",
        "label": "注册驱动", "help": "默认推荐 roxy；protocol=纯协议，容易封号不建议；roxy=RoxyBrowser；cloak=CloakBrowser；browser_use=Browser Use Cloud+Playwright；skyvern=Skyvern Browser Sessions+Playwright",
    },
    {
        "key": "REGISTRATION_TRANSIENT_RETRIES", "file": "register.py", "type": "int", "group": "注册方式",
        "label": "瞬态错误重试", "help": "仅对网络超时、Cloudflare 挑战、浏览器关闭等可恢复错误进行整流程重试；业务/验证码错误不自动重跑",
    },
    {
        "key": "OAICS_CHECK_AFTER_REGISTRATION", "file": "register.py", "type": "bool", "group": "注册方式",
        "label": "注册完成查询 OAICS", "help": "开启后注册成功并保存账号后自动调用 ChatGPT checkout 检测 OAICS；各国资格查询在账号页单独执行",
    },
    {
        "key": "COUNTRY_QUALIFICATION_CHECK_AFTER_REGISTRATION", "file": "register.py", "type": "bool", "group": "注册方式",
        "label": "注册完成查询各国资格", "help": "开启后注册成功并保存账号后自动调用 qualification-test Checkout 检测各国支付渠道；与 OAICS 检测相互独立",
    },
    # ---- 账号查活 ----
    {
        "key": "LIVE_CHECK_DRIVER", "file": "live_check.py", "type": "str", "group": "账号查活",
        "label": "查活方式", "help": "仅用于账号查活；协议=纯协议请求，Cloak/Roxy=在对应指纹浏览器中恢复 Session 或重新登录，不改变注册方式",
        "choices": ["cloak", "roxy", "protocol"],
        "choice_labels": {"cloak": "本地指纹浏览器（CloakBrowser）", "roxy": "RoxyBrowser", "protocol": "纯协议"},
    },
    {
        "key": "LIVE_CHECK_HEADLESS", "file": "live_check.py", "type": "bool", "group": "账号查活",
        "label": "查活浏览器无头", "help": "仅影响查活启动的 Cloak/Roxy 浏览器；不会修改注册流程或两个浏览器各自的无头设置",
    },
    {
        "key": "LIVE_CHECK_USE_REGISTRATION_PROXY", "file": "live_check.py", "type": "bool", "group": "账号查活",
        "label": "查活使用注册代理", "help": "开启后优先使用账号注册时保存的代理；该代理失败后再尝试查活代理 API 和代理池",
    },
    {
        "key": "LIVE_CHECK_PROXY_API_ENABLED", "file": "live_check.py", "type": "bool", "group": "账号查活",
        "label": "启用查活代理 API", "help": "开启后按账号保存的国家/地区请求代理 API；注册代理关闭时，查活优先使用 API，再回退代理池",
    },
    {
        "key": "LIVE_CHECK_PROXY_API_URL", "file": "live_check.py", "type": "str", "group": "账号查活",
        "label": "查活代理 API 地址", "help": "支持 {region}/{country}/{country_code} 占位符；region 参数会自动替换为账号国家码",
        "placeholder": "https://api.example/white/api?region={region}&num=1&time=10&format=n&type=json",
    },
    {
        "key": "LIVE_CHECK_PROXY_API_TIMEOUT", "file": "live_check.py", "type": "float", "group": "账号查活",
        "label": "查活代理 API 超时(秒)", "help": "获取代理 API 的最大等待时间，建议 3-15 秒",
        "min": 0.5, "max": 60,
    },
    {
        "key": "REBIND_LOGIN_DRIVER", "file": "live_check.py", "type": "str", "group": "账号查活",
        "label": "换绑登录方式", "help": "换绑第一阶段建立登录态的方式；推荐使用 CloakBrowser/RoxyBrowser 指纹浏览器",
        "choices": ["cloak", "roxy", "protocol"],
        "choice_labels": {"cloak": "CloakBrowser 指纹浏览器", "roxy": "RoxyBrowser 指纹浏览器", "protocol": "纯协议"},
    },
    {
        "key": "REBIND_ACTION_DRIVER", "file": "live_check.py", "type": "str", "group": "账号查活",
        "label": "换绑提交方式", "help": "换绑第二阶段提交新邮箱的方式；协议模式使用已建立的会话提交并验证邮箱变更",
        "choices": ["protocol", "cloak", "roxy"],
        "choice_labels": {"protocol": "纯协议", "cloak": "CloakBrowser 指纹浏览器", "roxy": "RoxyBrowser 指纹浏览器"},
    },
    {
        "key": "REBIND_HYBRID_MODE", "file": "live_check.py", "type": "bool", "group": "账号查活",
        "label": "换绑混合模式", "help": "开启后按“换绑登录方式 → 换绑提交方式”执行；关闭后换绑任务沿用单一驱动。默认是指纹浏览器登录、协议提交。",
    },
    {
        "key": "CODEX_RETRY_FOLLOW_LIVE_CHECK", "file": "codex.py", "type": "bool", "group": "账号查活",
        "label": "Codex补跑跟随查活", "help": "开启后，Codex补跑自动使用账号查活的驱动；关闭后使用下方独立补跑方式",
    },
    {
        "key": "CODEX_RETRY_DRIVER", "file": "codex.py", "type": "str", "group": "账号查活",
        "label": "Codex补跑方式", "help": "补跑驱动：same_as_live_check=跟随查活，protocol=纯协议，roxy/cloak=指纹浏览器",
        "choices": ["same_as_live_check", "protocol", "roxy", "cloak", "browser_use", "skyvern"],
        "choice_labels": {"same_as_live_check": "跟随查活", "protocol": "纯协议", "roxy": "RoxyBrowser", "cloak": "CloakBrowser", "browser_use": "Browser Use", "skyvern": "Skyvern"},
    },
    {
        "key": "CODEX_RETRY_FALLBACK_DRIVER", "file": "codex.py", "type": "str", "group": "账号查活",
        "label": "Codex补跑降级方式", "help": "RoxyBrowser 本地服务不可用时自动切换的方式；留空则保留 Roxy 并返回连接错误",
        "choices": ["", "cloak", "protocol"],
        "choice_labels": {"": "不降级", "cloak": "CloakBrowser", "protocol": "纯协议"},
    },
    {
        "key": "CODEX_RETRY_HEADLESS", "file": "codex.py", "type": "bool", "group": "账号查活",
        "label": "Codex补跑浏览器无头", "help": "仅影响 Codex 补跑使用的 Cloak/Roxy 浏览器；纯协议和云端浏览器忽略此项",
    },

    # ---- CloakBrowser ----
    {
        "key": "CLOAK_HEADLESS", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak无头", "help": "True=无头运行；False=显示浏览器窗口",
    },
    {
        "key": "CLOAK_HUMANIZE", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak人工行为", "help": "启用 CloakBrowser humanize 鼠标/键盘/滚动行为",
    },
    {
        "key": "CLOAK_GEOIP", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak按出口定位", "help": "按当前出口 IP 自动匹配时区/语言/WebRTC IP；支持显式代理、系统代理/VPN",
    },
    {
        "key": "CLOAK_LOCALE", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak语言", "help": "留空自动；日本可填 ja-JP，美国 en-US",
    },
    {
        "key": "CLOAK_TIMEZONE", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak时区", "help": "留空自动；日本可填 Asia/Tokyo，美国 America/Los_Angeles",
    },
    {
        "key": "CLOAK_USE_PROXY", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak使用代理", "help": "把本项目传入或代理池抽取的代理传给 CloakBrowser",
    },
    {
        "key": "CLOAK_LICENSE_KEY", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak License", "help": "Pro license；留空使用免费 binary",
    },
    {
        "key": "CLOAK_FINGERPRINT_SEED", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak指纹Seed", "help": "留空每次随机；固定值可保持同一指纹",
    },
    {
        "key": "CLOAK_RANDOMIZE_FINGERPRINT_EACH_LAUNCH", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "每次随机Cloak指纹", "help": "每次启动显式生成新指纹并使用临时上下文，不复用固定 cookies/cache",
    },
    {
        "key": "CLOAK_USER_DATA_DIR", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak用户目录", "help": "仅关闭“每次随机Cloak指纹”时生效；填写路径可持久化 cookies/cache",
    },
    {
        "key": "CLOAK_SELENIUM_TIMEOUT", "file": "cloakbrowser.py", "type": "int", "group": "CloakBrowser",
        "label": "Cloak超时", "help": "页面和元素等待超时时间，秒",
    },
    {
        "key": "CLOAK_NAVIGATION_RETRIES", "file": "cloakbrowser.py", "type": "int", "group": "CloakBrowser",
        "label": "Cloak导航重试", "help": "初始登录页遇到临时网络/页面超时时的最大导航尝试次数",
    },
    {
        "key": "CLOAK_CHALLENGE_TIMEOUT", "file": "cloakbrowser.py", "type": "int", "group": "CloakBrowser",
        "label": "验证盾等待(秒)", "help": "出现 Cloudflare 人机验证时保持可见窗口，完成验证后自动续跑",
    },
    {
        "key": "CLOAK_HEADLESS_FALLBACK_ON_CHALLENGE", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "挑战自动转可见", "help": "无头模式遇到交互式验证时，下一轮自动改为可见窗口并重建上下文",
    },
    {
        "key": "CLOAK_KEEP_BROWSER_OPEN", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "保留Cloak浏览器", "help": "调试时开启，任务结束后不自动关闭",
    },
    {
        "key": "CLOAK_KEEP_BROWSER_OPEN_ON_ERROR", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "失败时保留Cloak窗口", "help": "注册异常时保留当前页面，便于查看失败原因",
    },
    {
        "key": "CLOAK_ENABLE_AGENT", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "启用页面 Agent", "help": "只有“页面 Agent”配置成功后才可开启；Agent 会根据下方模式介入页面操作",
        "requires_agent": True,
    },
    {
        "key": "CLOAK_AGENT_MODE", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Agent 运行模式", "help": "混合模式=固定流程优先，仅异常页面介入；完全接管=每个注册阶段主动识别并操作",
        "choices": ["takeover", "hybrid"],
        "choice_labels": {"takeover": "完全接管", "hybrid": "混合模式"},
    },

    # ---- 页面 Agent ----
    {
        "key": "PAGE_AGENT_PROVIDER", "file": "page_agent.py", "type": "str", "group": "页面 Agent",
        "label": "Agent 服务提供方", "help": "关闭 Agent、本地 DOM 识别，或调用兼容 Chat Completions 的模型服务",
        "choices": ["disabled", "local", "openai_compatible"],
        "choice_labels": {"disabled": "关闭", "local": "本地 DOM Agent", "openai_compatible": "兼容模型 API"},
    },
    {
        "key": "PAGE_AGENT_API_BASE", "file": "page_agent.py", "type": "str", "group": "页面 Agent",
        "label": "Agent API 地址", "help": "兼容 Chat Completions 的服务地址，例如 http://HOST/v1；local 模式留空",
    },
    {
        "key": "PAGE_AGENT_API_KEY", "file": "page_agent.py", "type": "str", "group": "页面 Agent",
        "label": "Agent API Key", "help": "模型服务密钥，写入 .env；local 模式留空",
        "storage": "env", "secret": True,
    },
    {
        "key": "PAGE_AGENT_MODEL", "file": "page_agent.py", "type": "str", "group": "页面 Agent",
        "label": "Agent 模型", "help": "兼容模型名称；local 模式留空",
    },
    {
        "key": "PAGE_AGENT_NETWORK_ROUTE", "file": "page_agent.py", "type": "str", "group": "页面 Agent",
        "label": "模型网络出口", "help": "直连=模型请求从本机直接访问；代理池出口=从代理池抽取代理访问模型服务；默认直连",
        "choices": ["direct", "proxy_pool"],
        "choice_labels": {"direct": "本机直连", "proxy_pool": "代理池出口"},
    },
    {
        "key": "PAGE_AGENT_TIMEOUT", "file": "page_agent.py", "type": "int", "group": "页面 Agent",
        "label": "Agent 请求超时(秒)", "help": "单次模型请求最大等待时间",
    },
    {
        "key": "PAGE_AGENT_MAX_STEPS", "file": "page_agent.py", "type": "int", "group": "页面 Agent",
        "label": "Agent 最大动作数", "help": "单个页面阶段最多执行的动作数量，建议 3-6",
    },
    {
        "key": "PAGE_AGENT_TEMPERATURE", "file": "page_agent.py", "type": "float", "group": "页面 Agent",
        "label": "Agent 温度", "help": "模型动作随机性；页面操作建议 0",
    },

    # ---- Browser Use Cloud ----
    {
        "key": "BROWSER_USE_API_KEY", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "Browser Use API Key", "help": "保存在 .env（BROWSER_USE_API_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "BROWSER_USE_PROXY_COUNTRY_CODE", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "代理国家代码", "help": "两位国家码，如 jp/us/sg；配合 Browser Use 内置 residential proxy",
    },
    {
        "key": "BROWSER_USE_USE_PROXY", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "使用内置代理", "help": "True=连接参数带 proxyCountryCode；False=不强制传国家代理参数",
    },
    {
        "key": "BROWSER_USE_PROFILE_ID", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "Profile ID", "help": "可选。填写则复用 Browser Use profile 的 cookies/localStorage；批量建议留空",
    },
    {
        "key": "BROWSER_USE_CDP_BASE", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "CDP 地址", "help": "默认 wss://connect.browser-use.com",
    },
    {
        "key": "BROWSER_USE_TIMEOUT", "file": "browser_use.py", "type": "int", "group": "Browser Use",
        "label": "操作超时(秒)", "help": "Playwright 默认操作超时",
    },
    {
        "key": "BROWSER_USE_SESSION_TIMEOUT", "file": "browser_use.py", "type": "int", "group": "Browser Use",
        "label": "云端keepAlive(分钟)", "help": "传给 Browser Use connect URL 的 timeout/keepAlive；程序会自动限制到 1-240，建议 240",
    },
    {
        "key": "BROWSER_USE_FAST_MODE", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "快速模式", "help": "减少 Browser Use 额外等待和 humanize 延迟；建议开启，异常排查时可关闭",
    },
    {
        "key": "BROWSER_USE_LOG_TIMING", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "耗时日志", "help": "打印 Browser Use 各阶段耗时：连接、打开页面、邮箱、OTP、手机、callback",
    },
    {
        "key": "BROWSER_USE_KEEP_BROWSER_OPEN", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "保留远端会话", "help": "调试时可不主动 browser.close()；默认 False",
    },
    {
        "key": "BROWSER_USE_START_URL", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "起始 URL", "help": "默认 https://chatgpt.com/auth/login",
    },

    # ---- Skyvern Cloud Browser ----
    {
        "key": "SKYVERN_API_KEY", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "Skyvern API Key", "help": "保存在 .env（SKYVERN_API_KEY），用于创建 Skyvern Browser Session",
        "storage": "env", "secret": True,
    },
    {
        "key": "SKYVERN_API_BASE", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "API 地址", "help": "默认 https://api.skyvern.com",
    },
    {
        "key": "SKYVERN_BROWSER_SESSION_TIMEOUT", "file": "skyvern.py", "type": "int", "group": "Skyvern",
        "label": "Session 超时(分钟)", "help": "创建 Skyvern Browser Session 时传入的 timeout",
    },
    {
        "key": "SKYVERN_BROWSER_PROFILE_ID", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "Browser Profile ID", "help": "可选，复用 Skyvern browser profile",
    },
    {
        "key": "SKYVERN_PROXY_LOCATION", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "代理地区", "help": "可填 jp/us/gb 等简写；会自动转为 Skyvern 枚举，如 jp→RESIDENTIAL_JP；留空不传",
    },
    {
        "key": "SKYVERN_BROWSER_TYPE", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "浏览器类型", "help": "Skyvern 支持 msedge / chrome / stealth-chromium；旧值 chromium-headful 会自动转为 stealth-chromium",
    },
    {
        "key": "SKYVERN_AD_BLOCKER", "file": "skyvern.py", "type": "bool", "group": "Skyvern",
        "label": "广告拦截", "help": "创建 Skyvern Browser Session 时启用 ad_blocker",
    },
    {
        "key": "SKYVERN_GENERATE_BROWSER_PROFILE", "file": "skyvern.py", "type": "bool", "group": "Skyvern",
        "label": "保存浏览器Profile", "help": "Session 结束时是否让 Skyvern 生成/保存 browser profile",
    },
    {
        "key": "SKYVERN_KEEP_BROWSER_OPEN", "file": "skyvern.py", "type": "bool", "group": "Skyvern",
        "label": "保留浏览器", "help": "调试时可开启，任务结束后不主动关闭 Skyvern Browser Session",
    },
    {
        "key": "SKYVERN_START_URL", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "起始 URL", "help": "默认 https://chatgpt.com/auth/login",
    },
    {
        "key": "ROXY_API_BASE", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy API 地址", "help": "默认 http://127.0.0.1:50000；需在 Roxy 应用 API 配置中开启",
    },
    {
        "key": "ROXY_API_TOKEN", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy API Key", "help": "保存在 .env（ROXY_API_TOKEN），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "ROXY_PROFILE_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 环境ID", "help": "指定要打开的 Roxy 浏览器环境/Profile ID；留空则尝试创建临时环境",
    },
    {
        "key": "ROXY_WORKSPACE_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 工作区ID", "help": "创建一号一环境时必填，会作为 workspaceId 提交给 Roxy 创建 Profile 接口",
    },
    {
        "key": "ROXY_PROJECT_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 项目ID", "help": "从 /browser/workspace 的 project_details.projectId 获取；创建 Profile 时会作为 projectId 提交",
    },
    {
        "key": "ROXY_WORKSPACE_LIST_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "获取团队接口", "help": "默认 /browser/workspace；点击获取团队/项目时会先试此路径，再自动尝试常见兼容路径",
    },
    {
        "key": "ROXY_OPEN_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "打开接口路径", "help": "默认 /browser/open；如 Roxy 版本不同可在此调整",
    },
    {
        "key": "ROXY_OPEN_HEADLESS", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "无头启动窗口", "help": "打开 Roxy 环境时向 /browser/open 传 headless；False=显示窗口，True=无头启动",
    },
    {
        "key": "ROXY_CLOSE_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "关闭接口路径", "help": "默认 /browser/close",
    },
    {
        "key": "ROXY_KEEP_BROWSER_OPEN", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "保留浏览器", "help": "调试时可开启，任务结束后不自动关闭 Roxy 环境",
    },
    {
        "key": "ROXY_ONE_PROFILE_PER_ACCOUNT", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "一号一环境", "help": "每个账号强制创建新 Roxy Profile，用完关闭并删除，禁止复用固定环境",
    },
    {
        "key": "ROXY_DELETE_PROFILE_AFTER_RUN", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "结束后删除环境", "help": "一号一环境模式下，任务结束后删除本轮创建的 Roxy Profile",
    },
    {
        "key": "ROXY_RANDOM_OS_ON_CREATE", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境随机OS", "help": "创建 Roxy 环境时每次在 Windows / macOS 中随机，不固定 macOS",
    },
    {
        "key": "ROXY_RANDOM_OS_CHOICES", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "随机OS范围", "help": "逗号分隔，默认 Windows,macOS；Roxy 支持 Windows / macOS / Linux / IOS / Android",
    },
    {
        "key": "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境随机名称", "help": "创建 Roxy 环境时自动生成不同名称，避免固定 gpt-free-register",
    },
    {
        "key": "ROXY_PROFILE_NAME_PREFIX", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "随机名称前缀", "help": "默认 rb；实际名称格式类似 rb-时间戳-随机码",
    },
    {
        "key": "ROXY_CREATE_USE_PROXY_POOL", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境使用代理池", "help": "创建 Roxy 环境时从配置页「代理池」随机取一个代理，写入 Roxy proxyInfo",
    },
    {
        "key": "ROXY_PROXY_CHECK_CHANNEL", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "代理检测通道", "help": "写入 Roxy proxyInfo.checkChannel；留空则不传，默认 IPRust.io",
    },
    {
        "key": "ROXY_DELETE_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "删除接口路径", "help": "默认 /browser/delete；如 Roxy 版本不同可调整",
    },
    {
        "key": "CODEX_OAUTH_DRIVER", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "Codex授权驱动", "help": "默认跟随注册驱动；protocol=原协议授权；roxy=用 RoxyBrowser；cloak=用 CloakBrowser；browser_use=用 Browser Use Cloud；skyvern=用 Skyvern；same_as_registration=跟随注册驱动",
    },
    {
        "key": "ROXY_CODEX_CALLBACK_TIMEOUT", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "Codex回调超时", "help": "Roxy Codex OAuth 等待 localhost:1455 callback 的最长秒数",
    },
    {
        "key": "ENABLE_2FA", "file": "twofa.py", "type": "bool", "group": "功能开关",
        "label": "启用 2FA(TOTP)", "help": "注册完成后自动设置动态口令（会多收一封 OTP 邮件）",
    },
    {
        "key": "REGISTER_PASSWORD", "file": "register.py", "type": "str", "group": "注册方式",
        "label": "注册密码", "help": "开启创建账号密码后优先使用；留空则自动生成强密码。保存于账号 extra_json.registration_password",
        "secret": True,
    },
    {
        "key": "ENABLE_CREATE_PASSWORD", "file": "register.py", "type": "bool", "group": "功能开关",
        "label": "创建账号密码", "help": "开启后，邮箱验证码页先点击“使用密码继续”并提交密码，再验证邮箱验证码；关闭时直接走验证码分支",
    },
    {
        "key": "ENABLE_FLOW_TRIGGER", "file": "flow_trigger.py", "type": "bool", "group": "功能开关",
        "label": "启用 Flow 触发", "help": "注册成功后自动调用内部 Flow 接口（不影响注册结果）",
    },
    {
        "key": "FLOW_TRIGGER_URL", "file": "flow_trigger.py", "type": "str", "group": "Flow",
        "label": "Flow 地址", "help": "注册成功后 POST 的 http:// 或 https:// 地址；留空时跳过 Flow",
    },
    {
        "key": "FLOW_TRIGGER_BEARER", "file": "flow_trigger.py", "type": "str", "group": "Flow",
        "label": "Flow Bearer", "help": "可选 Authorization Bearer 值，保存在 .env",
        "secret": True,
    },
    {
        "key": "FLOW_TRIGGER_COOKIE", "file": "flow_trigger.py", "type": "str", "group": "Flow",
        "label": "Flow Cookie", "help": "可选 Cookie 请求头，保存在 .env",
        "secret": True,
    },
    {
        "key": "FLOW_TRIGGER_PAYLOAD", "file": "flow_trigger.py", "type": "str", "group": "Flow",
        "label": "Flow JSON 参数", "help": "JSON 对象字符串；access_token 会由注册结果自动注入",
    },
    {
        "key": "FLOW_TRIGGER_TIMEOUT", "file": "flow_trigger.py", "type": "int", "group": "Flow",
        "label": "Flow 超时(秒)", "help": "Flow 请求超时秒数，默认 15",
    },
    {
        "key": "ENABLE_HUMANIZE_DELAY", "file": "humanize.py", "type": "bool", "group": "人工节奏",
        "label": "启用随机停顿", "help": "在注册、OTP、授权等步骤之间加入随机等待，更接近人工操作节奏",
    },
    {
        "key": "HUMANIZE_DELAY_FACTOR", "file": "humanize.py", "type": "float", "group": "人工节奏",
        "label": "停顿倍率", "help": "随机停顿整体倍率；1.0=默认，0.5=减半，2.0=加倍",
    },
    {
        "key": "ENABLE_HUMANIZE_BROWSER_ACTIONS", "file": "humanize.py", "type": "bool", "group": "人工节奏",
        "label": "浏览器动作随机化", "help": "Roxy/Cloak 点击、输入、页面观察使用随机鼠标落点和逐字输入，降低机械操作痕迹",
    },
    # ---- 邮箱 / OTP ----
    {
        "key": "USE_EMAIL_SERVICE", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "自动取邮箱+收码", "help": "True=从邮箱池自动领邮箱并自动收 OTP；False=手动模式：用 REGISTER_EMAIL，OTP 在任务页手填",
    },
    {
        "key": "REGISTER_EMAIL", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "手动注册邮箱", "help": "USE_EMAIL_SERVICE=False 时必填。例如你的 outlook.com 地址；OTP 去网页邮箱看，再回任务页提交",
    },
    {
        "key": "REGISTER_NAME", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "显示名称", "help": "留空则自动生成英文名",
    },
    {
        "key": "OTP_MAX_WAIT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "OTP 最长等待(秒)", "help": "等待验证码邮件的最长秒数，超时判失败",
    },
    {
        "key": "OTP_POLL_INTERVAL", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "OTP 轮询间隔(秒)", "help": "每隔多少秒查一次新邮件",
    },
    {
        "key": "EMAIL_SOURCE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "邮箱来源", "help": "可填单个或多个，逗号分隔并按顺序兜底：outlook,generic_api,icloud,cloudflare_domain,cloudflare,gptmail,mailnest,cloudmail",
    },
    {
        "key": "ICLOUD_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "iCloud HTML请求超时(秒)", "help": "读取每个邮箱导入 URL 的单次超时；总等待时间由 OTP 最长等待控制",
    },
    {
        "key": "ICLOUD_VERIFY_TLS", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "iCloud校验TLS证书", "help": "HTTPS 取码地址默认开启证书校验；自签名的内网地址可关闭",
    },
    {
        "key": "HTML_OTP_SELECTORS", "file": "email.py", "type": "list_str_multiline", "group": "邮箱 / OTP",
        "label": "HTML验证码选择器", "help": "每行一个选择器，支持 #otp、.verification-code、id=otp、class=verification-code、标签名 code 以及网页检查器格式 <code>；命中元素后优先提取其文本。SPA 页面首屏为占位符时会自动读取页面的 /data 接口",
    },
    {
        "key": "GPTMAIL_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "GPTMail API Key", "help": "选择 gptmail 邮箱来源时必填；保存在 .env，不会写入 config 源码",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare API 地址", "help": "Worker 临时邮箱 API 根地址，如 https://mail.example.com；选择 cloudflare 时必填",
        "storage": "env",
    },
    {
        "key": "CLOUDFLARE_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare API Key", "help": "匿名可空；admin 模式填 ADMIN_PASSWORD；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_AUTH_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 鉴权模式", "help": "none / bearer / x-api-key / x-admin-auth / query-key",
    },
    {
        "key": "CLOUDFLARE_CUSTOM_AUTH", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 全局密码", "help": "Worker PASSWORDS，注入 x-custom-auth；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_PATH_ACCOUNTS", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 创建路径", "help": "默认 /api/new_address；admin 常用 /admin/new_address",
    },
    {
        "key": "CLOUDFLARE_PATH_MESSAGES", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 邮件路径", "help": "默认 /api/mails",
    },
    {
        "key": "CLOUDFLARE_PATH_DOMAINS", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 域名路径", "help": "默认 /api/domains（预留）",
    },
    {
        "key": "CLOUDFLARE_PATH_TOKEN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare Token路径", "help": "默认 /api/token（fallback 预留）",
    },
    {
        "key": "CLOUDFLARE_DEFAULT_DOMAINS", "file": "email.py", "type": "list_str_multiline", "group": "邮箱 / OTP",
        "label": "Cloudflare 默认域名", "help": "收信域名，每行一个或逗号分隔；创建时轮询使用，可留空",
    },
    {
        "key": "CLOUDFLARE_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Cloudflare 请求超时(秒)", "help": "HTTP 请求超时，默认 20",
    },
    {
        "key": "CLOUDFLARE_NAME_LENGTH", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Cloudflare 随机名前缀长度", "help": "admin 创建时 local-part 长度，默认 10",
    },
    {
        "key": "OUTLOOK_FETCH_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Outlook取件模式", "help": "auto=远端优先，远端 402/DEPLOYMENT_DISABLED 自动切 Graph 直连；direct=只用 Microsoft Graph 直连；remote=只用远端服务",
    },
    {
        "key": "MAIL_NEST_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "MailNest API Key", "help": "选择 mailnest 邮箱来源时必填；保存在 .env，不会写入 config 源码",
        "storage": "env", "secret": True,
    },
    {
        "key": "MAIL_NEST_PROJECT_CODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "MailNest 项目代码", "help": "项目代码 默认 chatgpt001 获取页面 mailnest.top/buy-email",
    },
    {
        "key": "CLOUDMAIL_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail API 地址", "help": "Cloud Mail Worker/API 地址，例如 https://mail.example.com",
    },
    {
        "key": "CLOUDMAIL_ADMIN_EMAIL", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail管理员邮箱", "help": "用于生成 Token；域名被平台隐藏时也会用它登录读取域名",
        "storage": "env",
    },
    {
        "key": "CLOUDMAIL_PASSWORD", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail 密码", "help": "用于自动获取 Token；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDMAIL_TOKEN_PATH", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail Token路径", "help": "固定使用 /api/public/genToken；如部署版本不同可修改",
    },
    {
        "key": "CLOUDMAIL_AUTH_TOKEN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail Token", "help": "CloudMail/Cloud Mail API Authorization Token；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDMAIL_DOMAINS", "file": "email.py", "type": "list_str_multiline", "group": "邮箱 / OTP",
        "label": "CloudMail 域名列表", "help": "可留空；运行时会自动从平台获取。也可点“获取 CloudMail 域名”缓存到这里",
    },
    {
        "key": "CLOUDMAIL_AUTO_ADD_USER", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "CloudMail自动创建用户", "help": "生成随机邮箱后调用 /api/public/addUser 创建用户",
    },
    {
        "key": "CLOUDMAIL_RANDOM_LOCAL_LENGTH", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "CloudMail随机名前缀长度", "help": "生成邮箱 local-part 的长度，建议 10-16",
    },
    # ---- 浏览器地区画像 ----
    {
        "key": "ENABLE_HIGH_FIDELITY_FINGERPRINT", "file": "browser.py", "type": "bool", "group": "浏览器画像",
        "label": "最高模拟画像", "help": "开启后纯协议注册、登录和查活按账号持久化独立画像，并统一 HTTP/TLS/Sentinel 的浏览器环境字段",
    },
    {
        "key": "BROWSER_LOCALE_PROFILE", "file": "browser.py", "type": "str", "group": "浏览器画像",
        "label": "地区画像（自定义）", "help": "关闭“跟随代理池 IP”后使用；可选 jp/cn/us/sg，当前值作为语言、时区与 Accept-Language 画像",
    },

    {
        "key": "AUTO_BROWSER_LOCALE_FROM_IP", "file": "browser.py", "type": "bool", "group": "浏览器画像",
        "label": "跟随代理池 IP", "help": "开启：按当前代理池出口 IP 自动选择地区；关闭：使用下方“地区画像（自定义）”",
    },
    {
        "key": "IP_GEO_TIMEOUT", "file": "browser.py", "type": "float", "group": "浏览器画像",
        "label": "IP定位超时(秒)", "help": "出口 IP 地理信息接口的单次请求超时；接口失败会自动回退，不影响注册",
    },

    # ---- 代理池 ----
    {
        "key": "PROXY_POOL", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "代理池(每行一个)", "help": "每行一个代理 URL，留空行会被忽略；为空则不使用代理",
    },
    {
        "key": "PROXY_CHECK_BEFORE_REGISTRATION", "file": "proxy.py", "type": "bool", "group": "代理池",
        "label": "注册前检查并清理代理", "help": "开启后，每次开始注册任务都会检查代理池全部出口；自动删除失败项并保留可用项，没有可用代理时才终止任务",
    },
    {
        "key": "PROXY_WARMUP_TARGET_CLEAN_IPS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "预热保留干净IP数量", "help": "点击“代理池预热”时，从代理池筛选并保留的健康出口数量；超过实际健康数量时保留全部健康项",
    },
    {
        "key": "PROXY_WARMUP_HEALTH_URL", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "预热业务检查地址", "help": "通过代理访问该注册入口，检查可达性、HTTP 状态和 Cloudflare 挑战；这只是干净度判定中的一项",
    },
    {
        "key": "PROXY_WARMUP_REPUTATION_URL", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "IP信誉检查接口", "help": "查询出口 IP 的代理/VPN/Tor/机房/滥用/Bogon 等信誉信号；地址中的 {ip} 会替换为出口 IP",
    },
    {
        "key": "PROXY_WARMUP_ANONYMITY_URL", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "代理匿名性检查地址", "help": "通过代理访问 JSON 回显接口执行匿名性检查，核对出口 IP，并识别泄漏头；可用逗号或换行填写多个地址，失败时自动回退",
    },
    {
        "key": "PROXY_WARMUP_MIN_CLEAN_SCORE", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "干净度最低分", "help": "0-100；信誉风险、匿名性泄漏、业务入口失败、挑战页和高延迟都会扣分，达到该分数且关键项通过才算干净",
    },
    {
        "key": "PROXY_WARMUP_MAX_LATENCY", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "最大可接受延迟(秒)", "help": "业务入口响应超过该时间判定健康度不通过；用于过滤虽然能连接但质量过差的出口",
    },
    {
        "key": "PROXY_WARMUP_EXIT_SAMPLES", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "出口稳定性采样次数", "help": "同一代理连续建立多次新连接并核对出口 IP；出现不同出口说明是按连接轮换代理，无法保证注册时仍使用预热通过的 IP",
    },
    {
        "key": "PROXY_WARMUP_TIMEOUT", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "预热单代理超时(秒)", "help": "出口 IP 与注册入口健康探测的单代理超时",
    },
    {
        "key": "PROXY_WARMUP_WORKERS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "预热并发数", "help": "预热同时检查的代理数量，建议 2-6",
    },
    {
        "key": "PROXY_WARMUP_RECHECK_CLEAN_IPS", "file": "proxy.py", "type": "bool", "group": "代理池",
        "label": "预热后复查干净IP", "help": "开启后，第一轮预热通过的健康出口会再检测一轮；只有两轮都通过的 IP 才会进入最终保留结果。",
    },
    {
        "key": "PROXY_HEALTH_CHECK_BEFORE_REGISTRATION", "file": "proxy.py", "type": "bool", "group": "代理池",
        "label": "注册任务检查健康IP", "help": "开启后每个注册任务开始前执行多维干净度检查并选择通过项；不会改变注册方式配置",
    },
    {
        "key": "PROXY_BROWSER_CHALLENGE_AUTO_ROTATE", "file": "proxy.py", "type": "bool", "group": "代理池",
        "label": "浏览器遇验证自动换IP", "help": "真实指纹浏览器打开注册页仍出现 Cloudflare 人机验证时，立即关闭当前窗口、淘汰该代理并换下一个，不再原地等待人工验证",
    },
    {
        "key": "PROXY_DELETE_UNHEALTHY_IPS", "file": "proxy.py", "type": "bool", "group": "代理池",
        "label": "自动删除不健康IP", "help": "开启后只删除明确命中信誉、匿名性、延迟、入口或挑战风险的脏 IP；外部检测接口故障产生的待复检项会保留",
    },
    {
        "key": "PLAN_CHECK_PROXY_MODE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "套餐/Agent网络模式", "help": "用于查套餐和生成 Agent Token；auto=本地代理可用则走代理、未监听则直连；proxy=强制代理；direct=强制直连",
    },
    {
        "key": "PLAN_CHECK_PROXY", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "套餐/Agent专用代理", "help": "用于查套餐和生成 Agent Token；留空时 auto/proxy 从代理池选择。可能包含认证信息，仅保存到 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "PLAN_CHECK_TIMEOUT", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent超时(秒)", "help": "查套餐和生成 Agent Token 的单次请求超时，建议 10-20 秒；独立于注册请求超时",
    },
    {
        "key": "PLAN_CHECK_MAX_ATTEMPTS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐/Agent最大尝试次数", "help": "查套餐和生成 Agent Token 遇到网络错误、429、5xx 等临时错误时的重试次数，建议 2 次",
    },
    {
        "key": "PLAN_CHECK_RETRY_DELAY", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent重试间隔(秒)", "help": "查套餐和生成 Agent Token 的重试间隔，按尝试次数递增；服务端 Retry-After 优先",
    },
    {
        "key": "PLAN_CHECK_REGISTRATION_RECHECK_DELAY", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "新账号资格复查延迟(秒)", "help": "新注册 free 账号未发现试用资格或首次查询失败时复查一次；0 表示关闭",
    },
    {
        "key": "PLAN_CHECK_WORKERS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐查询并发数", "help": "自动、手动和批量查套餐共用；Agent Token 生成使用独立队列；建议 2-4 个线程",
    },
    {
        "key": "PLAN_CHECK_QUEUE_LIMIT", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐查询队列上限", "help": "防止异常批量操作无限堆积，建议 100-1000",
    },
    {
        "key": "PLAN_CHECK_MIN_INTERVAL", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent请求最小间隔(秒)", "help": "限制查套餐和生成 Agent Token 的请求启动频率，降低 429 风险",
    },
    {
        "key": "PLAN_CHECK_JITTER", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent请求随机抖动(秒)", "help": "在查套餐和生成 Agent Token 的最小间隔上增加随机延迟，避免请求过于规律",
    },
    # ---- 提链 ----
    {
        "key": "EXTRACT_LINK_MODE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链方式", "help": "API提链=使用已保存的第三方提链服务；协议提链=使用项目内置协议实现",
        "choices": ["api", "protocol"],
        "choice_labels": {"api": "API提链", "protocol": "协议提链"},
    },
    {
        "key": "EXTRACT_LINK_PROVIDER", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链服务", "help": "通用提链列表中当前选中的 API 或协议服务标识",
    },
    {
        "key": "EXTRACT_LINK_BILLING_COUNTRY", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "账单国家", "help": "PP 协议创建 Checkout 和账单地址所用国家",
        "choices": ["GB", "US", "DE", "FR", "JP", "IE", "NL", "IT", "ES", "PT", "AT", "BE", "DK", "SE", "NO", "FI", "LU", "CH", "CA", "AU"],
        "choice_labels": {"GB": "英国 (GB)", "US": "美国 (US)", "DE": "德国 (DE)", "FR": "法国 (FR)", "JP": "日本 (JP)", "IE": "爱尔兰 (IE)", "NL": "荷兰 (NL)", "IT": "意大利 (IT)", "ES": "西班牙 (ES)", "PT": "葡萄牙 (PT)", "AT": "奥地利 (AT)", "BE": "比利时 (BE)", "DK": "丹麦 (DK)", "SE": "瑞典 (SE)", "NO": "挪威 (NO)", "FI": "芬兰 (FI)", "LU": "卢森堡 (LU)", "CH": "瑞士 (CH)", "CA": "加拿大 (CA)", "AU": "澳大利亚 (AU)"},
    },
    {
        "key": "EXTRACT_LINK_PAYMENT_METHOD", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "支付方式", "help": "PP 协议当前支持 PayPal",
        "choices": ["paypal"], "choice_labels": {"paypal": "PayPal"},
    },
    {
        "key": "EXTRACT_LINK_AUTO_ENTER_PAYPAL", "file": "extract_link.py", "type": "bool", "group": "提链",
        "label": "提链成功后自动进入 PAYPAL", "help": "开启后复制/返回 PayPal authorize 直达地址；关闭时优先返回 Hosted Checkout 地址",
    },
    {
        "key": "EXTRACT_LINK_CHECKOUT_UPDATE", "file": "extract_link.py", "type": "bool", "group": "提链",
        "label": "执行 Checkout Update", "help": "Stripe confirm 要求人工批准时调用 Checkout Update/approve 后继续等待 PayPal 地址",
    },
    {
        "key": "EXTRACT_LINK_WORKERS", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "当前用户并发", "help": "批量提链同时运行的账号任务数，范围 1-20",
    },
    {
        "key": "EXTRACT_LINK_RETRIES", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "提链重试次数", "help": "单账号失败后切换链路重新尝试的次数，范围 0-30",
    },
    # ---- Codex 配置 ----
    {
        "key": "SUB2API_AUTO_EXPORT", "file": "sub2api.py", "type": "bool", "group": "Codex",
        "label": "Agent sub2 自动同步", "help": "生成 Codex Agent Token 成功后自动同步到 sub2api",
    },
    {
        "key": "SUB2API_SYNC_MODE", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 同步模式", "help": "api=直接上传接口；file=写本地json；both=接口+本地json",
    },
    {
        "key": "SUB2API_API_BASE", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 API基址", "help": "sub2api 服务地址；Agent Token 上传和 Codex OAuth 共用，例如 http://127.0.0.1:8080",
    },
    {
        "key": "SUB2API_API_KEY", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 API Key", "help": "sub2api 管理接口 API Key；请求头使用 x-api-key；为空则不带鉴权头", "storage": "env", "secret": True,
    },
    {
        "key": "SUB2API_API_TIMEOUT", "file": "sub2api.py", "type": "int", "group": "Codex",
        "label": "sub2 超时", "help": "sub2api 请求超时秒数",
    },
    {
        "key": "SUB2API_OUTPUT_PATH", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 本地路径", "help": "仅 SUB2API_SYNC_MODE=file/both 时使用；相对路径按项目根目录解析",
    },
    {
        "key": "SUB2API_PROXY_KEY", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 代理键", "help": "可选；写入 account.proxy_key，并在 proxies 为空时初始化 proxies[0].proxy_key",
    },
    # ---- 接码平台 ----
    # ---- Codex：基础 / CPA / sub2api 配置 ----
    {
        "key": "CODEX_AUTH_URL_SOURCE", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "授权地址来源", "help": "cpa=CPA生成并上传CPA；sub2=sub2生成并上传sub2；local=本地PKCE",
    },
    {
        "key": "CPA_MANAGEMENT_URL", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "CPA 管理地址", "help": "例如 http://localhost:8317/admin/oauth；程序会取 origin 调用 /v0/management/*",
    },
    {
        "key": "CPA_MANAGEMENT_KEY", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "管理密钥", "help": "保存在 .env（CPA_MANAGEMENT_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "CPA_REQUEST_TIMEOUT", "file": "codex.py", "type": "int", "group": "Codex",
        "label": "CPA 超时(秒)", "help": "请求 CPA 管理接口的超时时间",
    },
    {
        "key": "CPA_SAVE_CALLBACK_RECEIPT", "file": "codex.py", "type": "bool", "group": "Codex",
        "label": "保存CPA回执", "help": "CPA 未返回完整授权文件时，本地仍保存一份回调提交记录",
    },

    {
        "key": "SMS_PROVIDER", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "接码平台", "help": "选择已经适配的接码平台；Codex 接码助手使用 CDK 管理长期/短效号码",
        "choices": ["grizzly", "h", "l", "codex"],
        "choice_labels": {"grizzly": "GrizzlySMS", "h": "H 接码", "l": "L 接码", "codex": "Codex 接码助手"},
    },
    {
        "key": "SMS_COUNTRY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "国家代码", "help": "可搜索国家名称或国家代码；选中后传给接码平台。GrizzlySMS 常用：美国=187；H/L 通道复用此值",
        "choices": SMS_COUNTRY_CHOICES,
        "choice_labels": SMS_COUNTRY_LABELS,
        "searchable": True,
    },
    {
        "key": "SMS_SERVICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "服务/项目代码", "help": "GrizzlySMS/L 作为 service；H 通道作为 H_API.md 的 projectId",
    },
    {
        "key": "SMS_MAX_PRICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "价格预设（最高价）", "help": "可选，填写单个号码愿意支付的最高价格；留空时不传价格限制，支持该参数的平台按此值取号",
    },
    {
        "key": "SMS_MAX_RETRIES", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "换号重试次数", "help": "一个号收不到短信/被OpenAI拒时换下一个号，最多重试几次",
    },
    {
        "key": "SMS_CODE_WAIT", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "单号等短信(秒)", "help": "单个号等待短信到达的最长秒数，超时则换号",
    },
    {
        "key": "SMS_API_KEY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "GrizzlySMS API密钥", "help": "GrizzlySMS 平台 API Key，保存在 .env（SMS_API_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "H_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H API 地址", "help": "H 取号服务基础地址，例如 http://localhost:8788",
    },
    {
        "key": "H_ADMIN_AUTH_CODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 授权码", "help": "保存在 .env（H_ADMIN_AUTH_CODE），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "H_PHONE_PREFIX", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 号码前缀", "help": "H 返回号码不含国家码时填写，例如美国 10 位本地号填 1；留空则不补",
    },
    {
        "key": "H_PHONE_ACQUIRE_MODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 取号方式", "help": "reusable=优先复用历史可用号码；new=每次都取一个新号码",
    },
    {
        "key": "L_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L API 地址", "help": "L 取号服务基础地址，例如 http://localhost:8788",
    },
    {
        "key": "L_ADMIN_AUTH_CODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L 授权码", "help": "保存在 .env（L_ADMIN_AUTH_CODE），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "L_PHONE_PREFIX", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L 号码前缀", "help": "L 返回号码不含国家码时填写，例如美国 10 位本地号填 1；留空则不补",
    },
    {
        "key": "CODEX_SMS_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "Codex API 地址", "help": "Codex 接码助手 API 基址，默认 https://sms.kkdos.store",
    },
    {
        "key": "CODEX_SMS_CDKS", "file": "codex.py", "type": "list_str_multiline", "group": "接码平台",
        "label": "Codex CDK 池", "help": "每行输入一个 CDK；支持批量检查，CDK 只保存在 .env，不写入源码或日志",
    },
    {
        "key": "CODEX_SMS_NUMBER_TYPE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "号码类型", "help": "自动按可用 CDK 选择，或优先使用短效/长效号码",
        "choices": ["auto", "short", "long"],
        "choice_labels": {"auto": "自动选择", "short": "短效号码", "long": "长效号码"},
    },
    {
        "key": "CODEX_SMS_CHECK_BEFORE_USE", "file": "codex.py", "type": "bool", "group": "接码平台",
        "label": "使用前检查 CDK", "help": "取号前跳过已知不可用 CDK，并优先使用最近检查通过的 CDK",
    },
    {
        "key": "CODEX_SMS_DELETE_USED_CDK", "file": "codex.py", "type": "bool", "group": "接码平台",
        "label": "使用后自动删除 CDK", "help": "成功收到验证码后，从本地 CDK 池移除本次使用的 CDK",
    },
]

_FIELD_BY_KEY = {f["key"]: f for f in EDITABLE_FIELDS}


# ============================================================
# 读：解析源码取当前值（不 import，避免缓存/副作用）
# ============================================================

def _config_path(filename: str) -> Path:
    path = (_CONFIG_DIR / filename).resolve()
    # 防目录穿越：必须落在 config/ 下
    if _CONFIG_DIR not in path.parents:
        raise ValueError(f"非法配置路径: {filename}")
    return path


def _literal_default_from_expr(node):
    """尽量从赋值表达式中取“源码默认值”，不执行模块代码。

    兼容：
      KEY = "literal"
      KEY: str = env_str("KEY", "default")
      KEY = env_bool("KEY", True)
      KEY = env_value("KEY", 123, "int")
    """
    try:
        return ast.literal_eval(node)
    except Exception:
        pass

    if isinstance(node, ast.Call):
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        # env_str/env_bool/env_int/env_float/env_list 的第二个位置参数是默认值。
        if func_name in {"env_str", "env_bool", "env_int", "env_float", "env_list"}:
            if len(node.args) >= 2:
                try:
                    return ast.literal_eval(node.args[1])
                except Exception:
                    return None
            return None

        # env_value(key, default, vtype)
        if func_name == "env_value" and len(node.args) >= 2:
            try:
                return ast.literal_eval(node.args[1])
            except Exception:
                return None

    return None


def _find_assignment_value_node(source: str, key: str):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id == key:
                return node.value
    return None


def _parse_value_from_source(source: str, key: str, vtype: str):
    """从源码里解析 KEY 的当前值。失败返回 None。"""
    if vtype == "list_str_multiline":
        # 用 AST 解析整个模块，取这个赋值的 list 字面量
        value_node = _find_assignment_value_node(source, key)
        if value_node is None:
            return None
        try:
            val = ast.literal_eval(value_node)
            if isinstance(val, (list, tuple)):
                return [str(x) for x in val]
        except (ValueError, SyntaxError):
            return None
        return None

    # 标量：优先 AST 取默认值，避免 env_str("KEY", "") 被当成普通字符串。
    value_node = _find_assignment_value_node(source, key)
    if value_node is not None:
        value = _literal_default_from_expr(value_node)
        if value is not None:
            return value

    # AST 失败时再回退到旧的正则解析。
    m = re.search(
        rf"^{re.escape(key)}\s*(?::[^=\n]+)?=\s*(.+?)\s*(?:#.*)?$",
        source, re.MULTILINE,
    )
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw


def _parse_env_typed_value(raw: str, fallback, vtype: str):
    """把 .env 字符串按字段类型转换；失败时回退 fallback。"""
    from config.env_loader import env_value
    return env_value("__NO_SUCH_ENV_KEY__", fallback, vtype) if raw is None else _coerce_raw_value(raw, fallback, vtype)


def _coerce_raw_value(raw: str, fallback, vtype: str):
    try:
        if raw is None or str(raw).strip() == "":
            return fallback
        if vtype == "bool":
            return str(raw).strip().lower() in ("true", "1", "yes", "on", "y")
        if vtype == "int":
            return int(str(raw).strip())
        if vtype == "float":
            return float(str(raw).strip())
        if vtype == "list_str_multiline":
            text = str(raw)
            try:
                val = ast.literal_eval(text)
                if isinstance(val, (list, tuple)):
                    return [str(x).strip() for x in val if str(x).strip()]
            except Exception:
                pass
            return [line.strip() for line in text.splitlines() if line.strip()]
        return str(raw)
    except Exception:
        return fallback


def get_config() -> list[dict]:
    """返回所有可编辑项的当前值 + 元信息，供前端渲染表单。

    优先读取 `.env` / 环境变量；没有配置时回退到 `config/*.py` 默认值。
    """
    from config.env_loader import load_env, read_env_file
    load_env(override=True)
    env_file_values = read_env_file()

    out = []
    for field in EDITABLE_FIELDS:
        key = field["key"]
        path = _config_path(field["file"])
        source = path.read_text(encoding="utf-8") if path.exists() else ""
        fallback = _parse_value_from_source(source, key, field["type"])

        if key in env_file_values:
            raw_env_value = env_file_values[key]
            if field["type"] == "list_str_multiline" and key in EXPLICIT_EMPTY_LIST_KEYS and str(raw_env_value).strip() == "":
                value = []
            else:
                value = _coerce_raw_value(raw_env_value, fallback, field["type"])
        elif os.getenv(key) is not None:
            value = _coerce_raw_value(os.getenv(key, ""), fallback, field["type"])
        else:
            value = fallback

        if field["type"] in ("str", "list_str_multiline"):
            value = _normalize_config_value(value, field["type"])
        item = dict(field)
        item["storage"] = "env"
        item["value"] = value
        out.append(item)
    try:
        from config.page_agent import configuration_status
        status = configuration_status()
        out.append({
            "key": "PAGE_AGENT_STATUS",
            "file": "page_agent.py",
            "type": "status",
            "group": "页面 Agent",
            "label": "Agent 配置状态",
            "help": "只有状态为“配置成功”时，本地指纹浏览器的 Agent 开关才允许开启",
            "readonly": True,
            "value": "配置成功" if status.get("configured") else f"未配置：{status.get('reason') or '未知原因'}",
            "status_ok": bool(status.get("configured")),
        })
    except Exception as exc:
        out.append({
            "key": "PAGE_AGENT_STATUS", "file": "page_agent.py", "type": "status",
            "group": "页面 Agent", "label": "Agent 配置状态", "help": "读取 Agent 配置失败",
            "readonly": True, "value": f"读取失败：{type(exc).__name__}", "status_ok": False,
        })
    return out


# ============================================================
# 写：统一写 .env，不修改 config/*.py
# ============================================================


_PLACEHOLDER_EMPTY = {
    "", "-", "—", "无", "空", "none", "null", "n/a", "na", "未设置", "未配置",
}


def _normalize_config_value(value, vtype: str):
    """把前端/历史占位空值规范化，避免 '-' 被当成真实配置。"""
    if vtype == "str":
        s = "" if value is None else str(value).strip()
        if s.lower() in {x.lower() for x in _PLACEHOLDER_EMPTY}:
            return ""
        return s
    if vtype == "list_str_multiline":
        if value is None:
            return []
        if isinstance(value, str):
            lines = value.splitlines()
        elif isinstance(value, (list, tuple)):
            lines = list(value)
        else:
            lines = [str(value)]
        out = []
        for item in lines:
            s = str(item or "").strip()
            if not s or s.lower() in {x.lower() for x in _PLACEHOLDER_EMPTY}:
                continue
            out.append(s)
        return out
    return value


def _format_literal(value, vtype: str) -> str:
    """把前端传来的值格式化成 Python 字面量字符串。"""
    if vtype == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on")
        return "True" if value else "False"
    if vtype == "int":
        return str(int(value))
    if vtype == "float":
        return repr(float(value))
    if vtype == "str":
        s = str(value)
        # 用 repr 保证转义安全，但统一成双引号风格
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise ValueError(f"_format_literal 不支持的类型: {vtype}")


def _replace_scalar(source: str, key: str, literal: str) -> str:
    """替换 `KEY[: 类型] = 旧值` 行的右值，保留行内注释和类型标注。"""
    pattern = re.compile(
        rf"^(?P<head>{re.escape(key)}\s*(?::[^=\n]+)?=\s*)"
        rf"(?P<val>.+?)"
        rf"(?P<tail>\s*(?:#.*)?)$",
        re.MULTILINE,
    )
    if not pattern.search(source):
        raise ValueError(f"未在源码中找到可替换的赋值: {key}")
    return pattern.sub(lambda m: f"{m.group('head')}{literal}{m.group('tail')}", source, count=1)


def _replace_proxy_pool(source: str, lines: list[str]) -> str:
    """整块替换 PROXY_POOL = [ ... ] 列表字面量（保留前面的赋值头）。"""
    items = [ln.strip() for ln in lines if ln.strip()]
    if items:
        body = "\n".join(
            '    "' + it.replace("\\", "\\\\").replace('"', '\\"') + '",'
            for it in items
        )
        literal = "[\n" + body + "\n]"
    else:
        literal = "[]"

    # 匹配 PROXY_POOL = [ ... ]（含跨行），用 AST 定位起止偏移最稳
    tree = ast.parse(source)
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "PROXY_POOL":
                src_lines = source.splitlines(keepends=True)
                start = node.value.lineno          # 值（[）所在行，1-based
                end = node.value.end_lineno        # 值（]）所在行，1-based
                col = node.value.col_offset         # [ 在起始行的列偏移
                # 保留起始行 [ 之前的内容（即 "PROXY_POOL = " 或 "PROXY_POOL: list = "）
                prefix = src_lines[start - 1][:col]
                # 保留结束行 ] 之后的内容（行内注释 / 换行）
                end_line = src_lines[end - 1]
                suffix = end_line[node.value.end_col_offset:]
                new_lines = (
                    src_lines[: start - 1]
                    + [prefix + literal + suffix]
                    + src_lines[end:]
                )
                return "".join(new_lines)
    raise ValueError("未找到 PROXY_POOL 赋值")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _format_env_value(value, vtype: str) -> str:
    """把前端值格式化成适合写入 .env 的字符串。"""
    if vtype == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on", "y")
        return "True" if value else "False"
    if vtype == "int":
        return str(int(value))
    if vtype == "float":
        return repr(float(value))
    if vtype == "list_str_multiline":
        lines = _normalize_config_value(value, vtype)
        return "\n".join(lines) if lines else "[]"
    if vtype == "str":
        return _normalize_config_value(value, vtype)
    return "" if value is None else str(value)


def _validate_config_value(key: str, value, field: dict) -> object:
    """校验带范围约束的配置值，避免 WebUI 写入不可用的运行时配置。"""
    vtype = field.get("type")
    if vtype != "int":
        return value
    if isinstance(value, bool):
        raise ValueError(f"{key} 必须是整数")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{key} 必须是整数") from None
    # 拒绝 1.5 这类会被 int() 截断的输入；字符串整数和真正整数均可。
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{key} 必须是整数")
    minimum = field.get("min")
    maximum = field.get("max")
    if minimum is not None and parsed < int(minimum):
        raise ValueError(f"{key} 必须不小于 {minimum}")
    if maximum is not None and parsed > int(maximum):
        raise ValueError(f"{key} 必须不大于 {maximum}")
    return parsed


def update_config(updates: dict) -> dict:
    """批量更新配置。所有 WebUI 可编辑项只写项目根 `.env`。"""
    from config.env_loader import write_env_values, load_env

    updated, ignored = [], []
    env_updates: dict[str, str] = {}

    # 配置页面一次提交整个表单，因此不能仅按 key 是否出现来判断 Agent
    # 配置发生变化。以当前有效值做比较，避免每次保存其他分组时意外使
    # Agent 验证状态失效。
    current_values = {
        item["key"]: item.get("value")
        for item in get_config()
        if isinstance(item, dict) and item.get("key")
    }
    agent_keys = {
        "PAGE_AGENT_PROVIDER",
        "PAGE_AGENT_API_BASE",
        "PAGE_AGENT_API_KEY",
        "PAGE_AGENT_MODEL",
        "PAGE_AGENT_NETWORK_ROUTE",
        "PAGE_AGENT_TIMEOUT",
        "PAGE_AGENT_MAX_STEPS",
        "PAGE_AGENT_TEMPERATURE",
    }

    def _same_value(key: str, value) -> bool:
        field = _FIELD_BY_KEY.get(key)
        if not field:
            return True
        vtype = field["type"]
        if vtype in {"str", "list_str_multiline"}:
            left = _normalize_config_value(current_values.get(key), vtype)
            right = _normalize_config_value(value, vtype)
            return left == right
        if vtype == "bool":
            def _as_bool(raw) -> bool:
                if isinstance(raw, str):
                    return raw.strip().lower() in {"true", "1", "yes", "on", "y"}
                return bool(raw)

            return _as_bool(current_values.get(key)) == _as_bool(value)
        try:
            return float(current_values.get(key)) == float(value)
        except (TypeError, ValueError):
            return str(current_values.get(key)) == str(value)

    changed_agent_keys = {
        key for key in agent_keys if key in updates and not _same_value(key, updates.get(key))
    }

    raw_enable_agent = updates.get("CLOAK_ENABLE_AGENT")
    enable_agent = (
        raw_enable_agent.strip().lower() in {"true", "1", "yes", "on", "y"}
        if isinstance(raw_enable_agent, str)
        else bool(raw_enable_agent)
    )
    if "CLOAK_ENABLE_AGENT" in updates and enable_agent:
        from config.page_agent import configuration_status
        if changed_agent_keys:
            raise ValueError("页面 Agent 配置已变化，请先单独保存并测试成功后再开启")
        status = configuration_status()
        if not status.get("configured"):
            raise ValueError(f"页面 Agent 尚未配置成功：{status.get('reason') or '请先完成 Agent 配置'}")
    if "CLOAK_AGENT_MODE" in updates:
        mode = str(updates.get("CLOAK_AGENT_MODE") or "hybrid").strip().lower()
        if mode not in {"hybrid", "takeover"}:
            raise ValueError("CLOAK_AGENT_MODE 仅支持 hybrid 或 takeover")

    for key, value in updates.items():
        field = _FIELD_BY_KEY.get(key)
        if field is None:
            ignored.append(key)
            continue
        if field.get("readonly"):
            ignored.append(key)
            continue
        value = _validate_config_value(key, value, field)
        env_updates[key] = _format_env_value(value, field["type"])
        updated.append(key)

    if changed_agent_keys:
        # 任何关键 Agent 配置变化都使之前的连接验证失效；测试成功后
        # core.page_agent.test_configuration() 会再次写入 True。
        env_updates["PAGE_AGENT_VALIDATED"] = "False"


    env_updated = write_env_values(env_updates) if env_updates else []
    if env_updated:
        load_env(override=True)

    return {"updated": updated, "ignored": ignored, "env_updated": env_updated}
