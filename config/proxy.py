# -*- coding: utf-8 -*-
"""
代理池配置

每次注册随机抽取一个代理，保证不同 sid 之间彼此独立，避免风控关联。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）
"""
import random
from config.proxy_api import build_request_url, load_api_entries

from config.env_loader import apply_env_overrides


# 本地代理入口；实际出口地区以代理/分流规则为准。
# 推荐使用 socks5h://（DNS 在代理端解析），避免本地 DNS 与出口 IP 地区错配。
PROXY_POOL = [
    "socks5://127.0.0.1:7897",
]

# 代理来源：pool=使用下方固定代理池；api=每次注册任务从代理 API 动态获取一个出口。
# API 模式不会清空或改写 PROXY_POOL，切回 pool 后原列表仍然可用。
PROXY_MODE = "pool"

# 兼容旧版单 API 配置；API 管理保存后由 JSON 列表中的选中项决定来源。
# 地址可包含密钥，仅写入本地 .env。空字符串迁移旧 API，显式 [] 表示无 API。
PROXY_API_SOURCES_JSON = ""
PROXY_API_URL = (
    "https://api.cliproxy.io/white/api?region=Rand&num=1&time=10&format=n&type=json"
)
PROXY_API_REGION = "Rand"
PROXY_API_FORMAT = "n"
PROXY_API_SESSION_TYPE = "sticky"
PROXY_API_TIME = 10
PROXY_API_TYPE = "json"
PROXY_API_NUM = 1
PROXY_API_TIMEOUT = 8.0
# 所有 API 代理任务共用的获取/健康检查总尝试次数（含首次，范围 1-20）。
# 每次重新请求 API 并检查一个出口，通过即停止；与 API 返回数量无关。
PROXY_API_MAX_ATTEMPTS = 3

# ISO 3166-1 alpha-2 regions accepted by the provider. Keeping this list in
# the runtime config makes the WebUI searchable by either code or Chinese name.
_PROXY_API_REGION_DATA = """
AD|安道尔
AE|阿拉伯联合酋长国
AF|阿富汗
AG|安提瓜和巴布达
AI|安圭拉
AL|阿尔巴尼亚
AM|亚美尼亚
AO|安哥拉
AQ|南极洲
AR|阿根廷
AS|美属萨摩亚
AT|奥地利
AU|澳大利亚
AW|阿鲁巴
AX|奥兰群岛
AZ|阿塞拜疆
BA|波斯尼亚和黑塞哥维那
BB|巴巴多斯
BD|孟加拉国
BE|比利时
BF|布基纳法索
BG|保加利亚
BH|巴林
BI|布隆迪
BJ|贝宁
BL|圣巴泰勒米
BM|百慕大
BN|文莱
BO|玻利维亚
BQ|荷属加勒比区
BR|巴西
BS|巴哈马
BT|不丹
BV|布韦岛
BW|博茨瓦纳
BY|白俄罗斯
BZ|伯利兹
CA|加拿大
CC|科科斯（基林）群岛
CD|刚果（金）
CF|中非共和国
CG|刚果（布）
CH|瑞士
CI|科特迪瓦
CK|库克群岛
CL|智利
CM|喀麦隆
CN|中国
CO|哥伦比亚
CR|哥斯达黎加
CU|古巴
CV|佛得角
CW|库拉索
CX|圣诞岛
CY|塞浦路斯
CZ|捷克
DE|德国
DJ|吉布提
DK|丹麦
DM|多米尼克
DO|多米尼加共和国
DZ|阿尔及利亚
EC|厄瓜多尔
EE|爱沙尼亚
EG|埃及
EH|西撒哈拉
ER|厄立特里亚
ES|西班牙
ET|埃塞俄比亚
FI|芬兰
FJ|斐济
FK|福克兰群岛
FM|密克罗尼西亚
FO|法罗群岛
FR|法国
GA|加蓬
GB|英国
GD|格林纳达
GE|格鲁吉亚
GF|法属圭亚那
GG|根西岛
GH|加纳
GI|直布罗陀
GL|格陵兰
GM|冈比亚
GN|几内亚
GP|瓜德罗普
GQ|赤道几内亚
GR|希腊
GS|南乔治亚和南桑威奇群岛
GT|危地马拉
GU|关岛
GW|几内亚比绍
GY|圭亚那
HK|中国香港特别行政区
HM|赫德岛和麦克唐纳群岛
HN|洪都拉斯
HR|克罗地亚
HT|海地
HU|匈牙利
ID|印度尼西亚
IE|爱尔兰
IL|以色列
IM|马恩岛
IN|印度
IO|英属印度洋领地
IQ|伊拉克
IR|伊朗
IS|冰岛
IT|意大利
JE|泽西岛
JM|牙买加
JO|约旦
JP|日本
KE|肯尼亚
KG|吉尔吉斯斯坦
KH|柬埔寨
KI|基里巴斯
KM|科摩罗
KN|圣基茨和尼维斯
KP|朝鲜
KR|韩国
KW|科威特
KY|开曼群岛
KZ|哈萨克斯坦
LA|老挝
LB|黎巴嫩
LC|圣卢西亚
LI|列支敦士登
LK|斯里兰卡
LR|利比里亚
LS|莱索托
LT|立陶宛
LU|卢森堡
LV|拉脱维亚
LY|利比亚
MA|摩洛哥
MC|摩纳哥
MD|摩尔多瓦
ME|黑山
MF|法属圣马丁
MG|马达加斯加
MH|马绍尔群岛
MK|北马其顿
ML|马里
MM|缅甸
MN|蒙古
MO|中国澳门特别行政区
MP|北马里亚纳群岛
MQ|马提尼克
MR|毛里塔尼亚
MS|蒙特塞拉特
MT|马耳他
MU|毛里求斯
MV|马尔代夫
MW|马拉维
MX|墨西哥
MY|马来西亚
MZ|莫桑比克
NA|纳米比亚
NC|新喀里多尼亚
NE|尼日尔
NF|诺福克岛
NG|尼日利亚
NI|尼加拉瓜
NL|荷兰
NO|挪威
NP|尼泊尔
NR|瑙鲁
NU|纽埃
NZ|新西兰
OM|阿曼
PA|巴拿马
PE|秘鲁
PF|法属波利尼西亚
PG|巴布亚新几内亚
PH|菲律宾
PK|巴基斯坦
PL|波兰
PM|圣皮埃尔和密克隆群岛
PN|皮特凯恩群岛
PR|波多黎各
PS|巴勒斯坦领土
PT|葡萄牙
PW|帕劳
PY|巴拉圭
QA|卡塔尔
RE|留尼汪
RO|罗马尼亚
RS|塞尔维亚
RU|俄罗斯
RW|卢旺达
SA|沙特阿拉伯
SB|所罗门群岛
SC|塞舌尔
SD|苏丹
SE|瑞典
SG|新加坡
SH|圣赫勒拿
SI|斯洛文尼亚
SJ|斯瓦尔巴和扬马延
SK|斯洛伐克
SL|塞拉利昂
SM|圣马力诺
SN|塞内加尔
SO|索马里
SR|苏里南
SS|南苏丹
ST|圣多美和普林西比
SV|萨尔瓦多
SX|荷属圣马丁
SY|叙利亚
SZ|斯威士兰
TC|特克斯和凯科斯群岛
TD|乍得
TF|法属南部领地
TG|多哥
TH|泰国
TJ|塔吉克斯坦
TK|托克劳
TL|东帝汶
TM|土库曼斯坦
TN|突尼斯
TO|汤加
TR|土耳其
TT|特立尼达和多巴哥
TV|图瓦卢
TW|台湾
TZ|坦桑尼亚
UA|乌克兰
UG|乌干达
UM|美国本土外小岛屿
US|美国
UY|乌拉圭
UZ|乌兹别克斯坦
VA|梵蒂冈
VC|圣文森特和格林纳丁斯
VE|委内瑞拉
VG|英属维尔京群岛
VI|美属维尔京群岛
VN|越南
VU|瓦努阿图
WF|瓦利斯和富图纳
WS|萨摩亚
YE|也门
YT|马约特
ZA|南非
ZM|赞比亚
ZW|津巴布韦
"""
PROXY_API_REGION_OPTIONS = [("Rand", "随机地区")] + [
    (code, name)
    for line in _PROXY_API_REGION_DATA.splitlines()
    if "|" in line
    for code, name in [line.strip().split("|", 1)]
]
PROXY_API_REGION_LABELS = {code: f"{name}（{code}）" for code, name in PROXY_API_REGION_OPTIONS}

# 开启后，每次创建注册批次前检查代理池全部出口；失败项自动删除，至少一项可用即继续。
PROXY_CHECK_BEFORE_REGISTRATION = False

# 代理池预热/注册健康检查：综合出口稳定性、IP 信誉、匿名性、注册入口可达性和挑战页判定。
# 预热按钮会按此数量保留通过全部检查的干净出口；目标大于实际干净数量时保留全部通过项。
PROXY_WARMUP_TARGET_CLEAN_IPS = 3
PROXY_WARMUP_HEALTH_URL = "https://chatgpt.com/auth/login"
PROXY_WARMUP_REPUTATION_URL = "https://us.ipapi.is/?q={ip}"
PROXY_WARMUP_ANONYMITY_URL = "https://echo.free.beeceptor.com,https://httpbin.io/get"
PROXY_WARMUP_MIN_CLEAN_SCORE = 80
PROXY_WARMUP_MAX_LATENCY = 8.0
PROXY_WARMUP_EXIT_SAMPLES = 3
PROXY_WARMUP_TIMEOUT = 12.0
PROXY_WARMUP_WORKERS = 4
# 开启后，第一轮预热筛出的健康出口会再完整检测一次；只有两轮都通过的 IP 才会保留。
PROXY_WARMUP_RECHECK_CLEAN_IPS = False

# 开启后每个注册任务开始前选择一个通过多维干净度检查的健康出口。
PROXY_HEALTH_CHECK_BEFORE_REGISTRATION = False
# 真实指纹浏览器打开注册入口仍出现人机验证时，立即淘汰本次代理并换下一个，
# 不在已判定不可靠的出口上继续等待人工验证。
PROXY_BROWSER_CHALLENGE_AUTO_ROTATE = True
# 开启后健康检查/预热判定不健康的代理会从代理池配置中自动删除。
PROXY_DELETE_UNHEALTHY_IPS = False

# 套餐/Plus 试用资格查询与 Codex Agent Token 生成共用这组独立网络策略，
# 避免批量请求被注册代理池中的临时本地代理拖垮，也避免无条件直连造成出口策略失控。
#   auto   = 优先使用 PLAN_CHECK_PROXY 或代理池；本地代理端口未监听时回退直连
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或代理池，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Codex Agent Token 生成专用代理。留空时 auto/proxy 模式从 PROXY_POOL 选择。
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY = ""

# 查套餐 / 生成 Codex Agent Token 使用独立的短超时和有限重试，避免后台任务长时间卡住。
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 2
PLAN_CHECK_RETRY_DELAY = 1.5

# 新注册账号的权益可能存在短暂同步延迟。首次查询失败，或返回 free 且暂未发现
# Plus 试用资格时，等待该秒数后再复查一次；设为 0 可关闭复查。
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0

# 自动、手动和批量套餐查询共用同一个后台队列；Codex Agent Token 使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 3
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 0.4
PLAN_CHECK_JITTER = 0.3


def proxy_api_entries() -> list[dict]:
    return load_api_entries(PROXY_API_SOURCES_JSON, PROXY_API_URL)


def build_proxy_api_request_url(region: str | None = None, *, entry: dict | None = None) -> str:
    """Choose one selected API and apply shared settings with its provider adapter."""
    if entry is None:
        selected = [item for item in proxy_api_entries() if item["enabled"]]
        if not selected:
            raise ValueError("请在配置 → 代理池 → API管理中至少选中一个 API")
        entry = random.choice(selected)
    return build_request_url(
        entry, region=str((PROXY_API_REGION if region is None else region) or "Rand").strip() or "Rand",
        count=max(1, min(20, int(PROXY_API_NUM or 1))),
        duration=max(1, min(360, int(PROXY_API_TIME or 10))),
        delimiter=str(PROXY_API_FORMAT or "n"), data_type=str(PROXY_API_TYPE or "json"),
        session_type=str(PROXY_API_SESSION_TYPE or "sticky").strip().lower(),
    )


def pick_proxy(*, excluded_proxies=None, log=None) -> str:
    """按代理来源选择一个代理 URL；API 模式每次调用都会请求新出口。

    pool 模式从固定列表随机选择；空列表返回空串（即不使用代理）。
    """
    if str(PROXY_MODE or "pool").strip().lower() == "api":
        from core.live_check_proxy import fetch_available_proxy_api

        proxies = fetch_available_proxy_api(
            str(PROXY_API_REGION or "Rand").strip() or "Rand",
            timeout=max(0.5, float(PROXY_API_TIMEOUT or 8.0)),
            excluded_proxies=excluded_proxies,
            log=log,
        )
        if not proxies:
            raise RuntimeError("代理 API 未返回可用代理")
        return str(proxies[0])
    return random.choice(PROXY_POOL) if PROXY_POOL else ""


# 兼容入口：固定池模式保留启动时的随机值；API 模式避免导入阶段请求网络。
PROXY = "" if str(PROXY_MODE or "pool").strip().lower() == "api" else pick_proxy()

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'PROXY_MODE': 'str',
    'PROXY_API_URL': 'str',
    'PROXY_API_SOURCES_JSON': 'str',
    'PROXY_API_REGION': 'str',
    'PROXY_API_FORMAT': 'str',
    'PROXY_API_SESSION_TYPE': 'str',
    'PROXY_API_TIME': 'int',
    'PROXY_API_TYPE': 'str',
    'PROXY_API_NUM': 'int',
    'PROXY_API_TIMEOUT': 'float',
    'PROXY_API_MAX_ATTEMPTS': 'int',
    'PROXY_CHECK_BEFORE_REGISTRATION': 'bool',
    'PROXY_WARMUP_TARGET_CLEAN_IPS': 'int',
    'PROXY_WARMUP_HEALTH_URL': 'str',
    'PROXY_WARMUP_REPUTATION_URL': 'str',
    'PROXY_WARMUP_ANONYMITY_URL': 'str',
    'PROXY_WARMUP_MIN_CLEAN_SCORE': 'int',
    'PROXY_WARMUP_MAX_LATENCY': 'float',
    'PROXY_WARMUP_EXIT_SAMPLES': 'int',
    'PROXY_WARMUP_TIMEOUT': 'float',
    'PROXY_WARMUP_WORKERS': 'int',
    'PROXY_WARMUP_RECHECK_CLEAN_IPS': 'bool',
    'PROXY_HEALTH_CHECK_BEFORE_REGISTRATION': 'bool',
    'PROXY_BROWSER_CHALLENGE_AUTO_ROTATE': 'bool',
    'PROXY_DELETE_UNHEALTHY_IPS': 'bool',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'str',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
})
PROXY = "" if str(PROXY_MODE or "pool").strip().lower() == "api" else pick_proxy()
