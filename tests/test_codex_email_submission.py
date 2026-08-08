from core import roxy_codex_oauth as mod


class _Driver:
    def get(self, _url):
        return None


def test_codex_submit_reuses_expected_email_after_typing_drift(monkeypatch):
    expected = "user.name_42@icloud.com"
    submitted = []
    attempts = {"count": 0}

    monkeypatch.setattr(mod, "human_delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mod, "_maybe_accept", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mod, "_type_email_address", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mod, "_submit_email_step", lambda _driver, email=None: submitted.append(email))
    monkeypatch.setattr(mod, "_maybe_click_passwordless_after_email", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mod, "_is_email_verification_page", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(mod, "_wait_for_otp_input", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mod, "_clear_otp_inputs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mod, "_type_otp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mod, "_install_email_otp_validate_hook", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mod, "_click_if_present", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(mod, "_wait_after_email_otp_submit", lambda *_args, **_kwargs: "accepted")

    def wait_for_otp(*_args, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("retry")
        return "123456"

    monkeypatch.setattr(mod, "_wait_for_fresh_email_otp", wait_for_otp)

    mod._fill_email_and_otp(_Driver(), expected, lambda *_args, **_kwargs: "", "https://auth.example/")

    assert submitted == [expected, expected]


def test_codex_totp_challenge_uses_saved_secret(monkeypatch):
    class Driver:
        current_url = "https://auth.example/mfa-challenge/id"

    driver = Driver()
    typed = []

    class FakeTotp:
        interval = 30

        def now(self):
            return "123456"

    from core import db
    monkeypatch.setattr(db, "get_account_by_email", lambda _email: {"totp_secret": "TESTSECRET"})
    monkeypatch.setattr(mod.pyotp, "TOTP", lambda _secret: FakeTotp())
    monkeypatch.setattr(mod, "_clear_otp_inputs", lambda _driver: None)
    monkeypatch.setattr(mod, "_type_otp", lambda _driver, code: typed.append(code))
    monkeypatch.setattr(mod, "human_delay", lambda *_args, **_kwargs: None)

    def click(_driver, _selectors, timeout=0):
        driver.current_url = "https://auth.example/consent"
        return True

    monkeypatch.setattr(mod, "_click_if_present", click)

    assert mod._handle_totp_challenge(driver, "user@example.com") is True
    assert typed == ["123456"]


def test_codex_totp_handler_skips_non_challenge_page():
    driver = type("Driver", (), {"current_url": "https://auth.example/consent"})()
    assert mod._handle_totp_challenge(driver, "user@example.com") is False


def test_codex_prefers_saved_registration_password(monkeypatch):
    class Driver:
        current_url = "https://auth.openai.com/log-in/password"

        def execute_script(self, _script, _password):
            return True

    driver = Driver()
    from core import db
    monkeypatch.setattr(db, "get_account_by_email", lambda _email: {"registration_password": "StrongPassword"})

    def click(_driver, _selectors, timeout=0):
        driver.current_url = "https://auth.openai.com/mfa-challenge/id"
        return True

    monkeypatch.setattr(mod, "_click_if_present", click)

    assert mod._submit_stored_password_if_present(driver, "user@example.com") is True
