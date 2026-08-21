# -*- coding: utf-8 -*-
"""Plus 试用通用提链配置。"""
from config.env_loader import apply_env_overrides

# 通用提链：protocol=项目内协议提链；api=已保存的提链 API 服务。
EXTRACT_LINK_MODE: str = "protocol"
EXTRACT_LINK_PROVIDER: str = "pp"

# 兼容旧版单 API 配置；新 API 服务通过 WebUI 保存到
# EXTRACT_LINK_API_SERVICES_JSON，不再要求所有服务共用一组字段。
EXTRACT_LINK_API_BASE: str = ""
EXTRACT_LINK_CDK: str = ""
EXTRACT_LINK_TYPE: str = "pix"

# PP 协议提链参数。
EXTRACT_LINK_BILLING_COUNTRY: str = "GB"
EXTRACT_LINK_PAYMENT_METHOD: str = "paypal"
EXTRACT_LINK_AUTO_ENTER_PAYPAL: bool = True
EXTRACT_LINK_CHECKOUT_UPDATE: bool = True

# 批量队列与请求参数。
EXTRACT_LINK_WORKERS: int = 10
EXTRACT_LINK_RETRIES: int = 5
EXTRACT_LINK_QUEUE_LIMIT: int = 500
EXTRACT_LINK_REQUEST_TIMEOUT: int = 30
EXTRACT_LINK_EVENT_TIMEOUT: int = 180

apply_env_overrides(globals(), {
    'EXTRACT_LINK_MODE': 'str',
    'EXTRACT_LINK_PROVIDER': 'str',
    'EXTRACT_LINK_API_BASE': 'str',
    'EXTRACT_LINK_CDK': 'str',
    'EXTRACT_LINK_TYPE': 'str',
    'EXTRACT_LINK_BILLING_COUNTRY': 'str',
    'EXTRACT_LINK_PAYMENT_METHOD': 'str',
    'EXTRACT_LINK_AUTO_ENTER_PAYPAL': 'bool',
    'EXTRACT_LINK_CHECKOUT_UPDATE': 'bool',
    'EXTRACT_LINK_WORKERS': 'int',
    'EXTRACT_LINK_RETRIES': 'int',
    'EXTRACT_LINK_QUEUE_LIMIT': 'int',
    'EXTRACT_LINK_REQUEST_TIMEOUT': 'int',
    'EXTRACT_LINK_EVENT_TIMEOUT': 'int',
})
