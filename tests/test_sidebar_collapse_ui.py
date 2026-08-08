from pathlib import Path


TEMPLATE = Path(__file__).parents[1] / "webui" / "templates" / "index.html"


def test_sidebar_has_persistent_collapse_control():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert 'id="sidebarToggle"' in source
    assert 'aria-label="折叠侧边栏"' in source
    assert "gpt_console_sidebar_collapsed" in source
    assert "classList.toggle('sidebar-collapsed'" in source
    assert "setSidebarCollapsed(collapsed)" in source
    assert "aria-expanded" in source


def test_collapsed_sidebar_keeps_icon_navigation_and_mobile_width():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "body.sidebar-collapsed { --sidebar-width: 72px; }" in source
    assert "body.sidebar-collapsed .sidebar-item-label" in source
    assert "body.sidebar-collapsed .sidebar-toggle svg" in source
    assert "body.sidebar-collapsed { --sidebar-width: 64px; }" in source
    assert ".sidebar-toggle { display: none; }" in source


def test_sidebar_footer_uses_julong_api_link_only():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert 'href="https://api.julongkj.top"' in source
    assert "矩龙 API" in source
    assert "TG 交流群" not in source
    assert "github.com/myfanhua/turb-gpt-free-register" not in source
    assert "Github 仓库" not in source
