from pathlib import Path

from webui.app import create_app


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "webui" / "templates" / "index.html"
LEGACY = ROOT / "webui" / "templates" / "index_legacy.html"


def test_legacy_ui_template_route_and_sidebar_switch_are_removed() -> None:
    source = INDEX.read_text(encoding="utf-8")
    app_source = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")

    assert not LEGACY.exists()
    assert "切换老 UI" not in source
    assert "?ui=legacy" not in source
    assert "index_legacy.html" not in app_source
    assert 'return render_template("index.html")' in app_source


def test_legacy_query_parameter_still_serves_the_commercial_ui() -> None:
    client = create_app(auth_code="commercial-ui-test").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "commercial-ui-test"

    response = client.get("/?ui=legacy")

    assert response.status_code == 200
    assert "Commercial UI System v3".encode() in response.data
    assert "切换老 UI".encode() not in response.data


def test_all_native_selects_are_progressively_enhanced() -> None:
    source = INDEX.read_text(encoding="utf-8")

    assert "function enhanceCommercialSelectV3(select)" in source
    assert "function installCommercialSelectsV3()" in source
    assert "select.classList.add('ui-select-native-v3')" in source
    assert "menu.className = 'ui-select-menu-v3'" in source
    assert "document.body.appendChild(menu)" in source
    assert "select.dispatchEvent(new Event('change', { bubbles: true }))" in source
    assert "new MutationObserver" in source


def test_registration_and_codex_drivers_use_the_unified_select_system() -> None:
    source = INDEX.read_text(encoding="utf-8")

    assert 'id="configRegistrationSelectV3" data-key="REGISTRATION_DRIVER"' in source
    assert 'id="configCodexOauthSelectV3" data-key="CODEX_OAUTH_DRIVER"' in source
    assert "configRegistrationSelectV2" not in source
    assert "data-ep-toggle" not in source


def test_commercial_theme_covers_global_navigation_panels_tables_and_dialogs() -> None:
    source = INDEX.read_text(encoding="utf-8")

    assert "Commercial UI System v3" in source
    assert ".app-sidebar" in source
    assert ".jobs-panel-v2" in source
    assert ".accounts-panel-v2" in source
    assert ".codex-panel-v2" in source
    assert ".outlook-panel-v2" in source
    assert ".ui-select-trigger-v3" in source
    assert ".ui-select-menu-v3" in source
