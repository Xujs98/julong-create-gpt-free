# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

from config import codex as codex_config
from core import sms_provider
from webui import config_editor


class _Resp:
    def __init__(self, data, status_code=200):
        self.status_code = status_code
        self._data = data
        self.text = json.dumps(data, ensure_ascii=False)

    def json(self):
        return self._data


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, data=None):
        self.calls.append(("POST", url, headers or {}, data))
        return self.responses.pop(0)

    def get(self, url, params=None):
        self.calls.append(("GET", url, params or {}))
        return self.responses.pop(0)

    def close(self):
        pass


class CodexSmsProviderTests(unittest.TestCase):
    def setUp(self):
        sms_provider._CODEX_SESSIONS.clear()
        sms_provider._CODEX_KNOWN_TYPES.clear()
        sms_provider._CODEX_PRECHECKED_SESSIONS.clear()
        sms_provider._CODEX_CDK_IN_USE.clear()
        sms_provider._ACQUIRED_AT.clear()

    def test_config_and_ui_fields(self):
        fields = {item["key"]: item for item in config_editor.EDITABLE_FIELDS}
        self.assertEqual(fields["SMS_PROVIDER"]["choice_labels"]["codex"], "Codex 接码助手")
        self.assertEqual(fields["CODEX_SMS_CDKS"]["type"], "list_str_multiline")
        self.assertEqual(fields["CODEX_SMS_NUMBER_TYPE"]["choices"], ["auto", "short", "long"])
        self.assertTrue(fields["CODEX_SMS_CHECK_BEFORE_USE"]["type"] == "bool")
        template = config_editor._PROJECT_ROOT.joinpath("webui/templates/index.html").read_text(encoding="utf-8")
        self.assertIn("/api/codex-sms/check", template)
        self.assertIn("Codex 接码助手", template)

    def test_acquire_and_poll_codex_session(self):
        http = _Http([
            _Resp({"sessionId": "sess-1", "phone": "+15551234567", "type": "onetime", "state": "polling"}),
            _Resp({"state": "polling", "phone": "+15551234567"}),
            _Resp({"state": "succeeded", "phone": "+15551234567", "code": "482931"}),
        ])
        with patch.object(codex_config, "SMS_PROVIDER", "codex"), \
                patch.object(codex_config, "CODEX_SMS_API_BASE", "https://sms.kkdos.store"), \
                patch.object(codex_config, "CODEX_SMS_CDKS", ["SHORT-1"]), \
                patch.object(codex_config, "SMS_POLL_INTERVAL", 0), \
                patch.object(codex_config, "SMS_CODE_WAIT", 2), \
                patch("core.sms_provider.time.sleep"):
            session, phone = sms_provider.acquire_number(http=http)
            code = sms_provider.wait_for_sms_code(session, http=http, max_wait=2, poll_interval=0)
        self.assertEqual((session, phone, code), ("sess-1", "15551234567", "482931"))
        self.assertTrue(http.calls[0][1].endswith("/api/v1/code/request"))
        self.assertTrue(http.calls[1][1].endswith("/api/v1/code/sess-1"))

    def test_complete_can_delete_used_cdk(self):
        http = _Http([_Resp({"sessionId": "sess-2", "phone": "+15550001111", "type": "bindable"})])
        with patch.object(codex_config, "SMS_PROVIDER", "codex"), \
                patch.object(codex_config, "CODEX_SMS_CDKS", ["LONG-1", "LONG-2"]), \
                patch.object(codex_config, "CODEX_SMS_NUMBER_TYPE", "auto"), \
                patch.object(codex_config, "CODEX_SMS_DELETE_USED_CDK", True), \
                patch("core.sms_provider._remove_codex_cdk") as remove:
            session, _ = sms_provider.acquire_number(http=http)
            sms_provider.complete(session, http=http)
        remove.assert_called_once_with("LONG-1")
        self.assertNotIn("LONG-1", sms_provider._CODEX_CDK_IN_USE)

    def test_codex_set_status_is_local_noop(self):
        http = _Http([])
        with patch.object(codex_config, "SMS_PROVIDER", "codex"):
            result = sms_provider.set_status("sess-noop", 1, http=http)
        self.assertEqual(result, "OK")
        self.assertEqual(http.calls, [])

    def test_replace_number_uses_switch_on_same_session(self):
        http = _Http([
            _Resp({"sessionId": "sess-switch", "phone": "+15550000001", "state": "polling"}),
            _Resp({
                "sessionId": "sess-switch",
                "phone": "+15550000002",
                "attempt": 2,
                "switchCount": 1,
                "remainingSwitches": 4,
            }),
        ])
        with patch.object(codex_config, "SMS_PROVIDER", "codex"), \
                patch.object(codex_config, "CODEX_SMS_CDKS", ["SHORT-SWITCH"]):
            session, _ = sms_provider.acquire_number(http=http)
            new_session, phone = sms_provider.replace_number(session, http=http)
            sms_provider.cancel(new_session, http=http)
        self.assertEqual((new_session, phone), ("sess-switch", "15550000002"))
        self.assertTrue(http.calls[1][1].endswith("/api/v1/code/sess-switch/switch"))
        self.assertEqual(sum(call[1].endswith("/api/v1/code/request") for call in http.calls), 1)

    def test_concurrent_acquires_reserve_different_cdks(self):
        http = _Http([
            _Resp({"sessionId": "sess-a", "phone": "+15550000001", "state": "polling"}),
            _Resp({"sessionId": "sess-b", "phone": "+15550000002", "state": "polling"}),
        ])
        with patch.object(codex_config, "SMS_PROVIDER", "codex"), \
                patch.object(codex_config, "CODEX_SMS_CDKS", ["SHORT-A", "SHORT-B"]):
            first = sms_provider.acquire_number(http=http)
            second = sms_provider.acquire_number(http=http)
            with self.assertRaisesRegex(sms_provider.SmsNoNumbersError, "正被其他补跑任务使用"):
                sms_provider.acquire_number(http=http)
            sms_provider.cancel(first[0], http=http)
            sms_provider.cancel(second[0], http=http)
        payloads = [json.loads(call[3]) for call in http.calls if call[0] == "POST"]
        self.assertEqual(payloads, [{"cdk": "SHORT-A"}, {"cdk": "SHORT-B"}])

    def test_acquire_reuses_onetime_session_from_batch_check(self):
        http = _Http([])
        sms_provider.cache_codex_batch_results(
            ["SHORT-CACHED"],
            [{
                "index": 0,
                "status": "success",
                "type": "onetime",
                "sessionId": "sess-cached",
                "phone": "+15550000003",
            }],
        )
        with patch.object(codex_config, "SMS_PROVIDER", "codex"), \
                patch.object(codex_config, "CODEX_SMS_CDKS", ["SHORT-CACHED"]):
            session, phone = sms_provider.acquire_number(http=http)
            sms_provider.cancel(session, http=http)
        self.assertEqual((session, phone), ("sess-cached", "15550000003"))
        self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
