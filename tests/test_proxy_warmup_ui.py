# -*- coding: utf-8 -*-
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
TEMPLATES = (
    ROOT / "webui" / "templates" / "index.html",
)


@pytest.mark.parametrize("template", TEMPLATES)
def test_proxy_warmup_terminal_state_refreshes_cleaned_pool(template):
    source = template.read_text(encoding="utf-8")

    assert "预热完成</span> · 已检查" in source
    assert "脏IP ${task.dirty || 0}" in source
    assert "待复检 ${task.inconclusive || 0}" in source
    assert "自动删除 ${task.removed || 0}" in source
    assert "delete CONFIG_PENDING_UPDATES.PROXY_POOL" in source
    assert "await loadConfig()" in source
    assert "代理池已刷新" in source


def test_modern_proxy_warmup_modal_close_keeps_background_polling():
    source = TEMPLATES[0].read_text(encoding="utf-8")
    close_body = source.split("function closeProxyWarmupLogModal() {", 1)[1].split("\n}", 1)[0]

    assert "classList.add('hidden')" in close_body
    assert "clearInterval(proxyWarmupTimer)" not in close_body
    assert "activeProxyWarmupTask = null" not in close_body


@pytest.mark.parametrize("template", TEMPLATES)
def test_proxy_warmup_running_status_shows_progress(template):
    source = template.read_text(encoding="utf-8")

    assert "正在预热'}：已检查 ${task.completed || 0}/${task.total || 0}" in source
    assert "const isActive = ['running', 'cancelling'].includes(task.status)" in source


def test_manual_proxy_test_separates_connectivity_from_login_and_has_deadline():
    source = TEMPLATES[0].read_text(encoding="utf-8")

    assert 'id="btnTestProxyLoginV2"' in source
    assert "testCurrentProxyV2(check = 'connectivity')" in source
    assert "body: JSON.stringify({proxy, check, timeout: 6})" in source
    assert "const controller = new AbortController()" in source
    assert "API 已提取成功，正在检测出口连通" in source
