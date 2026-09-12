from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import roxy_codex_oauth as mod


FORM = {"inputs": [{"autocomplete": "one-time-code", "ariaInvalid": "false"}]}
ERROR_PAGE = {"inputs": [], "buttons": [{"action": "Try again", "text": "Thử lại", "tag": "A"}]}


@pytest.fixture
def flow(monkeypatch):
    clock = SimpleNamespace(now=300.0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(mod.time, "time", lambda: clock.now)
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(mod.time, "sleep", sleep)
    driver = SimpleNamespace(current_url="https://auth.example/mfa-challenge/session", state=FORM)
    monkeypatch.setattr(mod, "_email_otp_page_state", lambda d: d.state)
    monkeypatch.setattr(mod, "_extract_callback_url_from_page", lambda d: "")
    monkeypatch.setattr("core.db.get_account_by_email", lambda _email: {"totp_secret": "SYNTHETIC-SECRET"})
    monkeypatch.setattr(mod.pyotp, "TOTP", lambda _: SimpleNamespace(
        interval=30, now=lambda: f"{int(clock.now // 30):06d}"
    ))
    clear = Mock()
    typed = Mock()
    monkeypatch.setattr(mod, "_clear_otp_inputs", clear)
    monkeypatch.setattr(mod, "_type_otp", typed)
    return driver, clock, typed, clear


def test_error_document_after_first_totp_does_not_type_a_second_code(flow, monkeypatch):
    driver, clock, typed, clear = flow

    def submit(*_args, **_kwargs):
        driver.state = ERROR_PAGE  # Same URL, but the code form is gone.
        return True

    monkeypatch.setattr(mod, "_click_if_present", submit)
    with pytest.raises(RuntimeError, match="重新建立授权会话"):
        mod._handle_totp_challenge(driver, "account@example.test")
    assert typed.call_count == clear.call_count == 1
    assert clock.now < 305  # Detect the error instead of waiting 30 + 15 seconds.


def test_error_document_before_totp_does_not_touch_inputs(flow, monkeypatch):
    driver, _, typed, clear = flow
    driver.state = ERROR_PAGE
    submit = Mock()
    monkeypatch.setattr(mod, "_click_if_present", submit)
    with pytest.raises(mod._TotpAuthorizationRestartRequired):
        mod._handle_totp_challenge(driver, "account@example.test")
    typed.assert_not_called()
    clear.assert_not_called()
    submit.assert_not_called()


@pytest.mark.parametrize("url,state,expected", [
    ("https://auth.example/mfa-challenge/id", FORM, "challenge"),
    ("https://auth.example/mfa-challenge/id", ERROR_PAGE, "auth_error"),
    ("https://auth.example/mfa-challenge/id", {"inputs": []}, "pending"),
    ("https://auth.example/mfa-challenge/id", {"inputs": [{"name": "code", "ariaInvalid": "true"}]}, "rejected"),
    ("https://auth.example/mfa-challenge/id", {"inputs": [{"type": "text"}] * 6}, "challenge"),
    ("https://auth.example/consent", {}, "advanced"),
    ("https://auth.example/workspace/select", {}, "advanced"),
    ("https://auth.example/add-phone", {}, "advanced"),
    ("http://localhost:1455/auth/callback?code=TEST&state=TEST", {}, "advanced"),
    ("https://auth.example/error", {}, "auth_error"),
    ("https://auth.example/log-in", {}, "auth_error"),
    ("about:blank", {}, "pending"),
    ("chrome-error://chromewebdata/", {}, "pending"),
    ("https://auth.example/unexpected", {}, "pending"),
])
def test_totp_classifies_dom_and_navigation(flow, url, state, expected):
    driver, *_ = flow
    driver.current_url = url
    driver.state = state
    assert mod._totp_challenge_stage(driver) == expected


@pytest.mark.parametrize("text", ["Try again", "Thử lại", "重试", "再试一次", "もう一度試す"])
def test_localized_error_page_is_recognized(flow, text):
    driver, *_ = flow
    driver.state = {"inputs": [], "buttons": [{"text": text}]}
    assert mod._totp_challenge_stage(driver) == "auth_error"


def test_rejected_totp_waits_for_new_time_step(flow, monkeypatch):
    driver, clock, typed, _ = flow

    def submit(*_args, **_kwargs):
        if typed.call_count == 1:
            driver.state = {"inputs": [{"name": "code", "ariaInvalid": "true"}]}
        else:
            driver.current_url = "https://auth.example/consent"
            driver.state = {}
        return True

    monkeypatch.setattr(mod, "_click_if_present", submit)
    assert mod._handle_totp_challenge(driver, "account@example.test") is True
    assert [call.args[1] for call in typed.call_args_list] == ["000010", "000011"]
    assert any(seconds > 20 for seconds in clock.sleeps)


def test_challenge_that_stays_open_stops_after_two_distinct_codes(flow, monkeypatch):
    driver, _, typed, _ = flow
    monkeypatch.setattr(mod, "_click_if_present", lambda *_args, **_kwargs: True)
    with pytest.raises(RuntimeError, match="连续两次"):
        mod._handle_totp_challenge(driver, "account@example.test", timeout=1)
    assert len({call.args[1] for call in typed.call_args_list}) == 2
    assert typed.call_count == 2


def test_navigation_during_next_code_wait_prevents_more_input(flow, monkeypatch):
    driver, clock, typed, _ = flow
    normal_sleep = mod.time.sleep

    def sleep(seconds):
        normal_sleep(seconds)
        if seconds > 20:
            driver.state = ERROR_PAGE

    monkeypatch.setattr(mod.time, "sleep", sleep)
    monkeypatch.setattr(mod, "_click_if_present", lambda *_args, **_kwargs: True)
    with pytest.raises(mod._TotpAuthorizationRestartRequired):
        mod._handle_totp_challenge(driver, "account@example.test", timeout=1)
    assert typed.call_count == 1


def test_pending_document_does_not_get_reported_as_success(flow, monkeypatch):
    driver, _, typed, _ = flow
    driver.state = {"inputs": []}
    monkeypatch.setattr(mod, "_click_if_present", Mock())
    with pytest.raises(RuntimeError, match="页面等待超时"):
        mod._handle_totp_challenge(driver, "account@example.test", timeout=1)
    typed.assert_not_called()


@pytest.mark.parametrize("succeeds", [True, False])
def test_totp_authorization_restart_is_bounded_and_clears_reused_state(monkeypatch, succeeds):
    failure = {"ok": False, "status": "failed", "message": "TOTP 授权页面错误", "retry_reason": "totp_auth_error"}
    final = {"ok": True, "status": "success"} if succeeds else failure
    once = Mock(side_effect=[failure, final])
    monkeypatch.setattr(mod, "_run_roxy_codex_oauth_once", once)
    driver = object()
    opened = object()
    result = mod.run_roxy_codex_oauth(
        "account@example.test", force=True, existing_driver=driver, existing_opened=opened,
        reuse_existing_profile=True, clear_existing_state=False,
    )
    assert result["ok"] is succeeds
    assert once.call_count == 2
    assert once.call_args_list[0].kwargs["clear_existing_state"] is False
    assert once.call_args_list[1].kwargs["clear_existing_state"] is True
    assert once.call_args.kwargs["existing_driver"] is driver
    if not succeeds:
        assert "2 轮仍失败" in result["message"]


def test_wrong_totp_is_not_an_authorization_restart(monkeypatch):
    failure = {"ok": False, "status": "failed", "message": "Codex TOTP 连续两次验证未通过"}
    once = Mock(return_value=failure)
    monkeypatch.setattr(mod, "_run_roxy_codex_oauth_once", once)
    assert mod.run_roxy_codex_oauth("account@example.test", force=True) == failure
    once.assert_called_once()


def test_existing_cpa_callback_retry_still_works(monkeypatch):
    failure = {"ok": False, "status": "failed", "message": "cpa-timeout-fixture"}
    once = Mock(side_effect=[failure, {"ok": True, "status": "success"}])
    monkeypatch.setattr(mod, "_run_roxy_codex_oauth_once", once)
    monkeypatch.setattr("core.codex_oauth._is_cpa_callback_reauth_error", lambda text: text == "cpa-timeout-fixture")
    assert mod.run_roxy_codex_oauth("account@example.test", force=True)["ok"] is True
    assert once.call_count == 2


@pytest.mark.parametrize("system,machine,container", [
    ("Darwin", "x86_64", False), ("Darwin", "arm64", False),
    ("Linux", "x86_64", True), ("Linux", "aarch64", True),
    ("Windows", "AMD64", False), ("Windows", "ARM64", False),
])
def test_totp_retains_host_callback_extraction_across_platforms(flow, monkeypatch, system, machine, container):
    import platform

    driver, _, typed, _ = flow
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setenv("CONTAINER", "docker" if container else "")
    monkeypatch.setattr(mod, "_extract_callback_url_from_page", lambda d: "http://localhost:1455/auth/callback?code=TEST&state=TEST")

    def submit(*_args, **_kwargs):
        driver.current_url = "chrome-error://chromewebdata/"
        driver.state = {}
        return True

    monkeypatch.setattr(mod, "_click_if_present", submit)
    assert mod._handle_totp_challenge(driver, "account@example.test") is True
    typed.assert_called_once()


def test_each_authorization_restart_creates_new_pkce_state_and_cleans_driver(monkeypatch):
    from core import codex_oauth as proto
    from core.roxybrowser_client import RoxyOpenResult

    opened = RoxyOpenResult("TEST-PROFILE", {}, created_by_run=True)
    client = Mock()
    client.open_profile.return_value = opened
    driver = Mock()
    monkeypatch.setattr(mod, "RoxyBrowserClient", lambda: client)
    monkeypatch.setattr(mod, "_build_driver", lambda _: driver)
    monkeypatch.setattr(mod, "_center_browser_window", lambda _: None)
    monkeypatch.setattr(mod, "human_delay", lambda *_args: None)
    fill = Mock()
    monkeypatch.setattr(mod, "_fill_email_and_otp", fill)
    monkeypatch.setattr(mod, "_handle_totp_challenge", Mock(side_effect=mod._TotpAuthorizationRestartRequired("TOTP 授权错误")))
    monkeypatch.setattr(proto, "_codex_auth_url_source", lambda: "local")
    states = Mock(side_effect=["STATE-A", "STATE-B"])
    pkce = Mock(side_effect=[("VERIFIER-A", "CHALLENGE-A"), ("VERIFIER-B", "CHALLENGE-B")])
    monkeypatch.setattr(proto, "_generate_state", states)
    monkeypatch.setattr(proto, "_generate_pkce", pkce)
    monkeypatch.setattr(proto, "_build_authorize_url", lambda state, challenge, **_: f"https://auth.example/authorize?state={state}&challenge={challenge}")
    result = mod.run_roxy_codex_oauth("account@example.test", force=True)
    assert result["status"] == "failed" and result["retry_reason"] == "totp_auth_error"
    assert states.call_count == pkce.call_count == 2
    assert fill.call_args_list[0].args[-1] != fill.call_args_list[1].args[-1]
    assert driver.quit.call_count == client.cleanup_profile.call_count == 2
    assert all(call.kwargs["force"] is True for call in client.cleanup_profile.call_args_list)
