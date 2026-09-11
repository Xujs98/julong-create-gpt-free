from contextlib import ExitStack
from unittest.mock import ANY, patch

from core import browser_use_codex_oauth, roxy_codex_oauth


def test_phone_state_log_is_single_line_and_bounded():
    state = {
        "url": "https://auth.openai.com/add-phone",
        "radios": [],
        "inputs": [{"name": "phone", "ariaInvalid": "false"}],
        "bodyText": "Phone required\n" + "Country\n" * 200,
    }
    summary = roxy_codex_oauth._phone_state_for_log(state)
    assert "\n" not in summary
    assert len(summary) < 360
    assert "inputs=phone(invalid=false)" in summary


def test_country_list_whatsapp_text_is_not_treated_as_selected_channel():
    state = {
        "url": "https://auth.openai.com/add-phone",
        "radios": [],
        "inputs": [{"type": "tel", "ariaInvalid": "false"}],
        "bodyText": "Add a phone number WhatsApp United States",
    }
    assert roxy_codex_oauth._classify_phone_page_failure(state) == ""


def test_checked_whatsapp_radio_is_still_rejected():
    state = {
        "url": "https://auth.openai.com/add-phone",
        "radios": [{"value": "whatsapp", "checked": True}],
        "inputs": [{"type": "tel"}],
        "bodyText": "Add a phone number",
    }
    assert roxy_codex_oauth._classify_phone_page_failure(state) == "whatsapp_channel"


def test_country_dropdown_is_dismissed_before_submit():
    events = []

    class _Element:
        def send_keys(self, value):
            events.append(("key", value))

    class _SwitchTo:
        active_element = _Element()

    class _Driver:
        switch_to = _SwitchTo()

        def execute_script(self, _script):
            events.append(("script", "dismiss"))

    with patch("core.roxy_codex_oauth.time.sleep"):
        roxy_codex_oauth._dismiss_phone_country_dropdown(_Driver())
    assert [event[0] for event in events] == ["key", "script"]


def test_roxy_retry_switches_codex_session_before_next_attempt():
    class _Http:
        def close(self):
            pass

    phone_fill = {
        "e164": "+15550000001",
        "actualVisible": "5550000001",
        "hiddenValue": "+15550000001",
        "dialCode": "1",
        "selectedText": "United States (+1)",
    }
    with ExitStack() as stack:
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider._cfg, "SMS_PROVIDER", "codex"))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider._cfg, "SMS_MAX_RETRIES", 2))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "_http", return_value=_Http()))
        stack.enter_context(patch.object(roxy_codex_oauth, "_has_strict_add_phone_form", return_value=True))
        acquire = stack.enter_context(patch.object(
            roxy_codex_oauth.sms_provider, "acquire_number", return_value=("sess-1", "15550000001")
        ))
        replace = stack.enter_context(patch.object(
            roxy_codex_oauth.sms_provider, "replace_number", return_value=("sess-1", "15550000002")
        ))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "set_status"))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "wait_for_sms_code", return_value="123456"))
        complete = stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "complete"))
        stack.enter_context(patch.object(roxy_codex_oauth, "_ensure_add_phone_input"))
        stack.enter_context(patch.object(roxy_codex_oauth, "_set_phone_value", return_value=phone_fill))
        stack.enter_context(patch.object(roxy_codex_oauth, "_blur_active_input_and_wait"))
        stack.enter_context(patch.object(roxy_codex_oauth, "_verify_add_phone_value_before_submit", return_value=phone_fill))
        stack.enter_context(patch.object(roxy_codex_oauth, "_select_sms_channel_or_raise"))
        stack.enter_context(patch.object(roxy_codex_oauth, "_click_add_phone_continue_button", return_value={"ok": True}))
        stack.enter_context(patch.object(roxy_codex_oauth, "_wait_page_settle_after_submit"))
        stack.enter_context(patch.object(
            roxy_codex_oauth, "_wait_after_phone_send", side_effect=[RuntimeError("send_not_accepted"), "code_page"]
        ))
        stack.enter_context(patch.object(roxy_codex_oauth, "_type_otp"))
        stack.enter_context(patch.object(roxy_codex_oauth, "human_delay"))
        stack.enter_context(patch.object(roxy_codex_oauth, "_click_if_present", return_value=True))
        stack.enter_context(patch.object(roxy_codex_oauth, "_wait_after_phone_otp_submit", return_value="left_phone_flow"))
        stack.enter_context(patch.object(roxy_codex_oauth, "_is_phone_code_page", return_value=False))
        stack.enter_context(patch.object(roxy_codex_oauth, "_find_any", return_value=object()))
        stack.enter_context(patch.object(roxy_codex_oauth, "_refresh_add_phone_for_retry"))
        stack.enter_context(patch.object(roxy_codex_oauth, "_sleep_before_phone_retry"))
        roxy_codex_oauth._do_phone_verification_if_present(object())
    acquire.assert_called_once()
    replace.assert_called_once()
    complete.assert_called_once_with("sess-1", ANY)


def test_browser_use_callback_completes_session_and_closes_http():
    class _Http:
        closed = False

        def close(self):
            self.closed = True

    http = _Http()
    with ExitStack() as stack:
        stack.enter_context(patch.object(browser_use_codex_oauth.sms_provider._cfg, "SMS_MAX_RETRIES", 1))
        stack.enter_context(patch.object(browser_use_codex_oauth.sms_provider, "_http", return_value=http))
        stack.enter_context(patch.object(browser_use_codex_oauth, "_is_callback_url", return_value=False))
        stack.enter_context(patch.object(browser_use_codex_oauth, "_has_phone_prompt", return_value=True))
        stack.enter_context(patch.object(browser_use_codex_oauth, "_ensure_add_phone_form", return_value=True))
        stack.enter_context(patch.object(
            browser_use_codex_oauth.sms_provider, "acquire_number", return_value=("sess-callback", "15550000001")
        ))
        stack.enter_context(patch.object(browser_use_codex_oauth, "_fill_phone", return_value="+15550000001"))
        stack.enter_context(patch.object(browser_use_codex_oauth, "_bu_delay"))
        stack.enter_context(patch.object(browser_use_codex_oauth, "_wait_after_phone_send", return_value="callback"))
        complete = stack.enter_context(patch.object(browser_use_codex_oauth.sms_provider, "complete"))
        browser_use_codex_oauth._do_phone_verification_if_present(object())

    complete.assert_called_once_with("sess-callback", http)
    assert http.closed is True
