# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import codex as codex_config
from core import sms_provider
from webui import config_editor


class _Resp:
    status_code = 200

    def __init__(self, text):
        self.text = text


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": params or {}})
        return _Resp(self.responses.pop(0))

    def close(self):
        pass


class GrizzlySmsProviderTests(unittest.TestCase):
    def test_webui_exposes_provider_choices_and_optional_price_preset(self):
        fields = {item["key"]: item for item in config_editor.EDITABLE_FIELDS}
        provider = fields["SMS_PROVIDER"]
        self.assertEqual(provider["choices"], ["grizzly", "h", "l", "codex"])
        self.assertEqual(provider["choice_labels"]["grizzly"], "GrizzlySMS")
        country = fields["SMS_COUNTRY"]
        self.assertTrue(country["searchable"])
        self.assertIn("187", country["choices"])
        self.assertEqual(country["choice_labels"]["187"], "美国（187）")
        self.assertEqual(fields["SMS_MAX_PRICE"]["type"], "str")
        template = config_editor._PROJECT_ROOT.joinpath("webui/templates/index.html").read_text(encoding="utf-8")
        self.assertIn("SMS_MAX_PRICE", template)
        self.assertIn("smsConfigSectionForKey", template)
        self.assertIn("ui-select-search-v3", template)
        self.assertIn("data-searchable=\"true\"", template)

    def test_acquire_number_uses_grizzly_handler_and_max_price(self):
        http = _Http(["ACCESS_NUMBER:activation-1:15551234567"])
        with patch.object(codex_config, "SMS_PROVIDER", "grizzly"), \
                patch.object(codex_config, "SMS_API_KEY", "key"), \
                patch.object(codex_config, "SMS_SERVICE", "openai"), \
                patch.object(codex_config, "SMS_COUNTRY", "10"), \
                patch.object(codex_config, "SMS_MAX_PRICE", "0.25"):
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("activation-1", "15551234567"))
        self.assertEqual(http.calls[0]["params"]["action"], "getNumber")
        self.assertEqual(http.calls[0]["params"]["maxPrice"], "0.25")
        self.assertEqual(http.calls[0]["params"]["api_key"], "key")

    def test_blank_price_is_not_sent_to_grizzly(self):
        http = _Http(["ACCESS_NUMBER:activation-2:15551234568"])
        with patch.object(codex_config, "SMS_PROVIDER", "grizzly"), \
                patch.object(codex_config, "SMS_API_KEY", "key"), \
                patch.object(codex_config, "SMS_MAX_PRICE", ""):
            sms_provider.acquire_number(http=http)

        self.assertNotIn("maxPrice", http.calls[0]["params"])

    def test_wait_for_sms_code_polls_grizzly_status(self):
        http = _Http(["STATUS_WAIT_CODE", "STATUS_OK:482931"])
        with patch.object(codex_config, "SMS_PROVIDER", "grizzly"), \
                patch.object(codex_config, "SMS_CODE_WAIT", 3), \
                patch.object(codex_config, "SMS_POLL_INTERVAL", 1), \
                patch("core.sms_provider.time.sleep"):
            code = sms_provider.wait_for_sms_code("activation-3", http=http, max_wait=3, poll_interval=1)

        self.assertEqual(code, "482931")
        self.assertEqual([call["params"]["action"] for call in http.calls], ["getStatus", "getStatus"])


if __name__ == "__main__":
    unittest.main()
