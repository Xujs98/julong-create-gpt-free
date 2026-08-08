from webui.app import _account_secret_value


def test_account_secret_value_generates_current_totp_code():
    code = _account_secret_value({"totp_secret": "JBSWY3DPEHPK3PXP"}, "totp_code")

    assert len(code) == 6
    assert code.isdigit()


def test_account_secret_value_returns_empty_without_totp_secret():
    assert _account_secret_value({}, "totp_code") == ""
