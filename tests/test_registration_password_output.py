from core import db
from core.account_export import _account_copy_line, _account_material_line
from core import browser_use_registration, roxy_registration


def test_registered_output_uses_account_password_and_totp_viewer():
    row = {
        "email": "user@example.com",
        "registration_password": "StrongPassword",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "access_token": "TOKEN",
    }

    expected = "user@example.com----StrongPassword----https://2fa.fb.tools/JBSWY3DPEHPK3PXP"
    assert db._registered_email_line(row) == expected
    assert db._account_line(row) == expected + "----TOKEN"
    assert _account_material_line(row["email"], row) == expected
    assert _account_copy_line(expected, "TOKEN", row["totp_secret"]) == expected + "----TOKEN"


def test_password_flow_skips_mailbox_otp_after_profile_transition():
    assert roxy_registration._needs_email_otp_after_password(None, "logged_in", password_set=True) is False
    assert browser_use_registration._needs_email_otp_after_password(None, "profile", password_set=True) is False


def test_password_flow_keeps_mailbox_otp_when_verification_is_required():
    assert roxy_registration._needs_email_otp_after_password(None, "otp", password_set=True) is True
    assert browser_use_registration._needs_email_otp_after_password(None, "email_verification", password_set=True) is True
