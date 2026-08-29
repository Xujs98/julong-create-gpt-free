from pathlib import Path


TEMPLATE = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"


def source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_config_workspace_uses_commercial_command_center() -> None:
    html = source()

    assert 'class="config-command-v2"' in html
    assert "OPERATIONS CONTROL" in html
    assert "工作流控制台" in html
    assert 'id="configDiscardV2"' in html
    assert 'id="configSaveAllV2"' in html


def test_config_overview_cards_are_real_runtime_metrics() -> None:
    html = source()

    assert "function renderConfigOverviewCardsV2()" in html
    assert "当前注册策略" in html
    assert "自动化能力" in html
    assert "配置完整度" in html
    assert "变更状态" in html
    assert "占位卡片" not in html


def test_config_fields_switches_and_section_actions_share_design_system() -> None:
    html = source()

    assert 'class="config-field-v2-copy"' in html
    assert 'class="config-field-v2-control"' in html
    assert 'data-config-switch-status-v2' in html
    assert "function renderConfigSectionActionsV2()" in html
    assert 'class="config-save-v2-button"' in html
    assert 'data-config-state-v2' in html


def test_config_unsaved_changes_can_be_tracked_and_discarded() -> None:
    html = source()

    assert "function syncConfigDirtyIndicatorsV2()" in html
    assert "section.classList.toggle('is-dirty', dirty)" in html
    assert "setConfigPendingValueV2(f.key, readConfigElementValue(el, f))" in html
    assert "Object.keys(CONFIG_PENDING_UPDATES).forEach" in html
    assert "已撤销未保存的配置更改" in html


def test_registration_section_contains_oaics_completion_switch() -> None:
    html = source()

    assert "OAICS_CHECK_AFTER_REGISTRATION" in html
    assert "config-switches-v2--registration" in html
    assert "config-switch-v2-icon--oaics" in html
    assert "const oaicsField = fields.find(f => f.key === 'OAICS_CHECK_AFTER_REGISTRATION')" in html
    assert "renderFeatureSwitchField(oaicsField" in html


def test_env_example_documents_oaics_completion_switch() -> None:
    env_example = TEMPLATE.parents[2] / ".env.example"
    text = env_example.read_text(encoding="utf-8")
    assert "OAICS_CHECK_AFTER_REGISTRATION=true" in text
