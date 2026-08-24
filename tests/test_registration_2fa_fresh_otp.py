from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_all_registration_drivers_forward_the_accepted_registration_otp():
    expected = {
        "main.py": "setup_2fa(session, email, previous_otp=current_otp)",
        "core/cloakbrowser_registration.py": "previous_otp=current_otp",
        "core/roxy_registration.py": "previous_otp=current_otp",
    }

    for relative_path, marker in expected.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert marker in source


def test_account_table_explains_that_unenabled_2fa_can_be_a_setup_failure():
    source = (ROOT / "webui/templates/index.html").read_text(encoding="utf-8")
    assert "可能是注册时未开启 2FA，或设置阶段失败，请查看注册日志" in source
