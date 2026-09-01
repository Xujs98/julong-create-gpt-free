from pathlib import Path

from webui.app import create_app

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "webui" / "templates" / "index.html"
REACT_BRIDGE = ROOT / "webui" / "static" / "js" / "react-dashboard.js"


def test_react_bridge_is_loaded_without_changing_existing_visual_dom():
    html = TEMPLATE.read_text(encoding="utf-8")
    bridge = REACT_BRIDGE.read_text(encoding="utf-8")

    assert '<script src="/static/vendor/react.production.min.js"></script>' in html
    assert '<script src="/static/vendor/react-dom.production.min.js"></script>' in html
    assert '<script src="/static/js/react-dashboard.js"></script>' in html
    assert 'id="react-dashboard-root" hidden' in html
    assert 'class="app-sidebar"' in html
    assert 'data-tab="register"' in html
    assert 'data-tab="accounts"' in html
    assert "window.ReactDOM.createRoot" in bridge
    assert "nav.addEventListener('click', onClick, true)" in bridge
    assert "window.__dashboardLegacy?.activateTab" in bridge


def test_react_bridge_keeps_legacy_tab_contract_and_avoids_duplicate_requests():
    bridge = REACT_BRIDGE.read_text(encoding="utf-8")
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "event.stopPropagation()" in bridge
    assert "setPanelVisibility(activeTab)" in bridge
    assert "tabIds = ['register', 'accounts', 'codex', 'outlook', 'config']" in bridge
    assert "const API_INFLIGHT = new Map();" in html
    assert "API_INFLIGHT.set(key, run);" in html
    assert "window.__dashboardLegacy.activateTab = activateTab;" in html
    assert html.index("window.__dashboardLegacy.activateTab = activateTab;") < html.index(
        '<script src="/static/js/react-dashboard.js"></script>'
    )


def test_local_react_vendor_bundles_are_present_and_nonempty():
    react = ROOT / "webui" / "static" / "vendor" / "react.production.min.js"
    react_dom = ROOT / "webui" / "static" / "vendor" / "react-dom.production.min.js"
    assert react.stat().st_size > 5000
    assert react_dom.stat().st_size > 50000
    assert b"React" in react.read_bytes()
    assert b"ReactDOM" in react_dom.read_bytes()


def test_vite_source_declares_react_build_and_flask_static_output():
    package = (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    vite = (ROOT / "frontend" / "vite.config.js").read_text(encoding="utf-8")
    source = (ROOT / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")
    assert '"react": "18.3.1"' in package
    assert '"react-dom": "18.3.1"' in package
    assert "outDir: '../webui/static/react'" in vite
    assert "useState" in source
    assert "gpt_console_active_tab" in source


def test_react_assets_are_browser_cacheable():
    client = create_app(auth_code="react-assets-test").test_client()
    response = client.get("/static/js/react-dashboard.js")
    assert response.status_code == 200
    assert "max-age=3600" in response.headers.get("Cache-Control", "")
