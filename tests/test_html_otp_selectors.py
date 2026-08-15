# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from config import email as email_config
from core import generic_api_mail_client, icloud_client
from webui.config_editor import EDITABLE_FIELDS


class HtmlOtpSelectorTests(unittest.TestCase):
    def test_selector_list_is_exposed_as_email_otp_config(self):
        field = next(item for item in EDITABLE_FIELDS if item["key"] == "HTML_OTP_SELECTORS")
        self.assertEqual(field["group"], "邮箱 / OTP")
        self.assertEqual(field["type"], "list_str_multiline")

    def test_multiple_id_and_class_selectors_are_tried_in_order(self):
        html = """
        <div id="noise">111111</div>
        <div class="message"><span class="wrong">222222</span></div>
        <span id="otp-value"><strong>010949</strong></span>
        """
        self.assertEqual(
            generic_api_mail_client._extract_html_selector_values(
                html, ["#missing", "class=wrong", "id=otp-value"]
            ),
            ["222222", "010949"],
        )
        self.assertEqual(
            generic_api_mail_client._extract_html_selector_code(
                html, ["#missing", "id=otp-value"]
            ),
            "010949",
        )

    def test_selector_can_target_tag_and_multiple_classes(self):
        html = '<div class="box otp highlighted"><span>482931</span></div>'
        self.assertEqual(
            generic_api_mail_client._extract_html_selector_code(html, ["div.otp.highlighted"]),
            "482931",
        )

    @patch("core.icloud_client.requests.get")
    @patch("core.icloud_client.get_account_context")
    def test_icloud_prefers_configured_element_over_page_noise(self, get_context, request_get):
        get_context.return_value = icloud_client.ICloudEmailAccount(
            email="sample@icloud.com", code_url="https://mail.example/code"
        )
        request_get.return_value = Mock(
            status_code=200,
            text='<div id="timestamp">20260815</div><div class="otp">010949</div>',
        )
        with patch.object(email_config, "HTML_OTP_SELECTORS", [".otp"]):
            code = icloud_client.fetch_latest_otp(
                "sample@icloud.com", max_wait=2, poll_interval=1, settle_seconds=0
            )
        self.assertEqual(code, "010949")


if __name__ == "__main__":
    unittest.main()
