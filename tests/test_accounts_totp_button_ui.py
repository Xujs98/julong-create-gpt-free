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
