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
                patch.object(codex_config, "CODEX_SMS_DELETE_USED_CDK", True), \
                patch("core.sms_provider._remove_codex_cdk") as remove:
            session, _ = sms_provider.acquire_number(http=http)
            sms_provider.complete(session, http=http)
        remove.assert_called_once_with("LONG-1")


if __name__ == "__main__":
    unittest.main()
