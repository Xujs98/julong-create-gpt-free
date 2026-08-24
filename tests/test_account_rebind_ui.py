from pathlib import Path

from webui.config_editor import EDITABLE_FIELDS


ROOT = Path(__file__).parents[1]
MODERN = ROOT / "webui" / "templates" / "index.html"


def test_modern_rebind_toolbar_and_dialog_are_present():
    source = MODERN.read_text(encoding="utf-8")
    assert 'id="btnRebindSelectedAccountsV2" disabled' in source
    assert 'id="accountRebindModal"' in source
    for element_id in (
        "accountRebindPoolListV2",
        "accountRebindCountV2",
        "accountRebindWorkersV2",
        "accountRebindGroupV2",
        "btnStartAccountRebind",
    ):
        assert f'id="{element_id}"' in source
    assert 'data-rebind-source' in source


def test_rebind_ui_uses_supported_pool_sources_and_submission_contract():
    source = MODERN.read_text(encoding="utf-8")
    assert "cloudflare_domain" in source
    assert "REBIND_POOL_DEFS" in source
    assert "'/api/rebind/pools'" in source
    assert "'/api/accounts/rebind'" in source
    assert "pool_sources: state.sources" in source
    assert "target_group_id" in source
    assert "workers: state.workers" in source


def test_rebind_ui_enforces_selection_pool_group_and_worker_limits():
    source = MODERN.read_text(encoding="utf-8")
    assert "请先选择要换绑的账号" in source
    assert "请至少选择一个目标邮箱池" in source
    assert "请先选择有可用邮箱的目标邮箱池" in source
    assert "请选择换绑完成后的目标分组" in source
    assert "暂无可用分组，请先创建分组" in source
    assert "workers <= Math.min(16, available, count)" in source
    assert "count <= maxCount" in source
    assert "select.disabled = groups.length === 0" in source


def test_rebind_dialog_requires_fresh_pool_and_group_choices_each_time():
    source = MODERN.read_text(encoding="utf-8")
    assert "groupSelect.value = ''" in source
    assert "syncCommercialSelectV3(groupSelect)" in source
    assert "正在读取邮箱池…" in source
    assert "REBIND_POOL_SUMMARY = {}" in source
    assert "await loadAccountGroups(false)" in source


def test_legacy_ui_and_switch_are_removed():
    modern = MODERN.read_text(encoding="utf-8")
    assert "accountRebindModal" in modern
    assert "切换老 UI" not in modern
    assert not (ROOT / "webui" / "templates" / "index_legacy.html").exists()


def test_live_check_config_is_presented_as_shared_rebind_mode():
    source = MODERN.read_text(encoding="utf-8")
    fields = {item["key"]: item for item in EDITABLE_FIELDS}
    assert fields["REBIND_LOGIN_DRIVER"]["label"] == "换绑登录方式"
    assert fields["REBIND_ACTION_DRIVER"]["label"] == "换绑提交方式"
    assert fields["REBIND_HYBRID_MODE"]["label"] == "换绑混合模式"
    assert "指纹浏览器登录、协议提交" in fields["REBIND_HYBRID_MODE"]["help"]
    assert "换绑方式：跟随“配置 → 账号查活”设置" in source
    assert "混合换绑" in source


def test_rebind_jobs_appear_in_registration_list_and_reuse_log_viewer():
    source = MODERN.read_text(encoding="utf-8")
    assert "const taskType = String(j.job_type || '').toLowerCase() === 'rebind' ? '换绑' : '注册'" in source
    assert 'data-log-job="${esc(j.id)}"' in source
    assert "类型：${taskType} · 状态：${status}" in source
    assert "可在注册页查看日志" in source
