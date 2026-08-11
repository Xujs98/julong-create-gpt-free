# -*- coding: utf-8 -*-
from pathlib import Path

from config import webui as webui_config
from webui.config_editor import EDITABLE_FIELDS


ROOT = Path(__file__).parents[1]
MODERN = ROOT / "webui" / "templates" / "index.html"
WEBUI_CONFIG = ROOT / "config" / "webui.py"


def test_registration_job_log_uses_structured_colored_rows():
    """注册任务日志应按时间、级别和正文分列，并保留轮询刷新。"""
    source = MODERN.read_text(encoding="utf-8")

    assert 'class="job-log-content" id="logContent"' in source
    assert "function parseJobLogEntries(rawLog)" in source
    assert "function classifyJobLogLevel(rawLevel, message)" in source
    assert "function renderJobLogContent(rawLog)" in source
    assert "job-log-level--success" in source
    assert "job-log-level--error" in source
    assert "job-log-level--warn" in source
    assert "job-log-level--debug" in source
    assert "c.innerHTML = renderJobLogContent(r.log || '')" in source
    assert "logTimer = setInterval(pollLog, settings.interval * 1000)" in source
    assert "每 ${refreshSettings.interval} 秒同步日志" in source
    assert "任务已结束，日志已停止刷新" in source
    assert "['success','failed','stopped','cancelled'].includes(r.job.status)" in source


def test_registration_job_log_dialog_has_accessible_header_and_close_button():
    """日志弹窗应提供明确标题、任务摘要和可访问的关闭按钮。"""
    source = MODERN.read_text(encoding="utf-8")

    assert 'id="logPanel" role="dialog" aria-modal="true"' in source
    assert 'aria-labelledby="jobLogTitle"' in source
    assert 'id="logJobMeta"' in source
    assert 'id="btnCloseLog" title="关闭日志" aria-label="关闭日志"' in source


def test_registration_job_log_refresh_settings_are_editable():
    """配置页应提供实时刷新开关和秒级刷新间隔。"""
    fields = {item["key"]: item for item in EDITABLE_FIELDS}
    config_source = WEBUI_CONFIG.read_text(encoding="utf-8")

    # 本地环境变量可以覆盖运行值，因此默认值直接从配置源文件验证。
    assert "WEBUI_JOB_LOG_AUTO_REFRESH: bool = True" in config_source
    assert "WEBUI_JOB_LOG_REFRESH_INTERVAL: int = 2" in config_source
    assert isinstance(webui_config.WEBUI_JOB_LOG_AUTO_REFRESH, bool)
    assert isinstance(webui_config.WEBUI_JOB_LOG_REFRESH_INTERVAL, int)
    assert fields["WEBUI_JOB_LOG_AUTO_REFRESH"]["type"] == "bool"
    assert fields["WEBUI_JOB_LOG_AUTO_REFRESH"]["group"] == "WebUI"
    assert fields["WEBUI_JOB_LOG_REFRESH_INTERVAL"]["type"] == "int"
    assert fields["WEBUI_JOB_LOG_REFRESH_INTERVAL"]["group"] == "WebUI"
