from pathlib import Path


def test_accounts_totp_copy_uses_styled_icon_button():
    template = (Path(__file__).parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")

    assert 'class="acc-v2-totp-copy"' in template
    assert 'aria-label="复制当前 2FA 动态验证码"' in template
    assert ".acc-v2-totp-copy:focus-visible" in template
    assert ".acc-v2-totp-copy.is-copied" in template
    assert '>复制验证码</button>' not in template


def test_account_email_is_a_copyable_text_button():
    template = (Path(__file__).parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")

    assert 'class="acc-v2-email-copy"' in template
    assert 'data-account-copy-email="${esc(r.email)}"' in template
    assert "showToast('邮箱已复制')" in template


def test_failed_twofa_state_retries_after_confirmation():
    root = Path(__file__).parents[1]
    for template in (root / "webui" / "templates" / "index.html", root / "webui" / "templates" / "index_legacy.html"):
        source = template.read_text(encoding="utf-8")
        assert 'data-account-setup-twofa="${esc(r.id)}"' in source
        assert "retryAccountTwofa" in source
        assert "await appConfirm(" in source
        assert "/setup-2fa" in source


def test_account_actions_include_twofa_reset_and_log_entries():
    root = Path(__file__).parents[1]
    for template in (root / "webui" / "templates" / "index.html", root / "webui" / "templates" / "index_legacy.html"):
        source = template.read_text(encoding="utf-8")
        assert "重新设置2FA" in source
        assert "重设2FA日志" in source
        assert 'data-account-twofa-log="${esc(r.email)}"' in source
        assert "/api/accounts/twofa-log?email=" in source
        assert "openTwofaLog" in source
