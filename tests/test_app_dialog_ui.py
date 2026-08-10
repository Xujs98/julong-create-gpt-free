import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
TEMPLATES = ROOT / "webui" / "templates"
PAGE_TEMPLATES = ("index.html", "index_legacy.html")


def test_both_webuis_include_the_shared_app_dialog():
    for name in PAGE_TEMPLATES:
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert '{% include "_app_dialog.html" %}' in source


def test_webuis_do_not_use_native_browser_dialogs():
    native_dialog = re.compile(r"\b(?:window\s*\.\s*)?(?:confirm|prompt|alert)\s*\(")

    for name in PAGE_TEMPLATES:
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert native_dialog.search(source) is None
        assert "await appConfirm(" in source
        assert "await appPrompt(" in source


def test_shared_dialog_has_accessible_keyboard_and_mobile_behavior():
    source = (TEMPLATES / "_app_dialog.html").read_text(encoding="utf-8")

    assert 'role="dialog"' in source
    assert 'aria-modal="true"' in source
    assert 'aria-labelledby="appDialogTitle"' in source
    assert "event.key === 'Escape'" in source
    assert "event.key !== 'Tab'" in source
    assert "previousFocus.focus" in source
    assert "isPrompt ? (options.tone || 'confirm')" in source
    assert "@media (max-width: 560px)" in source
    assert "window.appConfirm" in source
    assert "window.appAlert" in source
    assert "window.appPrompt" in source


def test_registration_proxy_failure_uses_shared_alert_dialog():
    for name in PAGE_TEMPLATES:
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "proxy_pool_preflight_failed" in source
        assert "await appAlert(" in source
        assert "本次注册任务已结束，未创建任何任务" in source
        assert "自动删除" in source
