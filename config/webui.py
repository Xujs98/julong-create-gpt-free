# -*- coding: utf-8 -*-
"""WebUI 展示与日志轮询配置。"""
from config.env_loader import apply_env_overrides


# 任务运行期间是否自动读取新增日志；关闭后打开日志弹窗时仍会读取一次。
WEBUI_JOB_LOG_AUTO_REFRESH: bool = True

# 任务日志自动刷新间隔，单位秒；前端会限制在 1-60 秒范围内。
WEBUI_JOB_LOG_REFRESH_INTERVAL: int = 2


apply_env_overrides(globals(), {
    'WEBUI_JOB_LOG_AUTO_REFRESH': 'bool',
    'WEBUI_JOB_LOG_REFRESH_INTERVAL': 'int',
})
