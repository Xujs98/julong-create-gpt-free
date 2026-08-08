# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import db, email_provider, icloud_client


class ICloudClientTests(unittest.TestCase):
    def test_email_source_parser_accepts_icloud(self):
        self.assertEqual(
            email_provider.parse_email_sources("outlook,icloud,generic_api,icloud"),
            ["outlook", "icloud", "generic_api"],
        )

    @patch("core.icloud_client.requests.get")
    @patch("core.icloud_client.get_account_context")
    def test_fetch_latest_otp_reads_html(self, get_context, request_get):
        get_context.return_value = icloud_client.ICloudEmailAccount(
            email="sample@icloud.com",
            code_url="https://mail.example/s/token/sample@icloud.com",
        )
        response = Mock(status_code=200)
        response.text = """
        <html><style>.x { color: #123456; }</style>
        <body><h1>Your verification code is 482931</h1></body></html>
        """
        request_get.return_value = response

        code = icloud_client.fetch_latest_otp(
            "sample@icloud.com",
            max_wait=2,
            poll_interval=1,
            settle_seconds=0,
        )

        self.assertEqual(code, "482931")
        request_get.assert_called_once()

    @patch("core.icloud_client.time.sleep", return_value=None)
    @patch("core.icloud_client.requests.get")
    @patch("core.icloud_client.get_account_context")
    def test_fetch_latest_otp_ignores_used_code_until_html_updates(self, get_context, request_get, _sleep):
        get_context.return_value = icloud_client.ICloudEmailAccount(
            email="sample@icloud.com",
            code_url="https://mail.example/s/token/sample@icloud.com",
        )
        old_response = Mock(status_code=200, text="Your verification code is 683938")
        new_response = Mock(status_code=200, text="Your verification code is 294617")
        request_get.side_effect = [old_response, new_response]

        code = icloud_client.fetch_latest_otp(
            "sample@icloud.com",
            max_wait=2,
            poll_interval=1,
            settle_seconds=0,
            exclude_codes={"683938"},
        )

        self.assertEqual(code, "294617")
        self.assertEqual(request_get.call_count, 2)

    @patch("core.icloud_client.fetch_latest_otp", return_value="294617")
    @patch("core.email_provider.resolve_email_source", return_value="icloud")
    def test_email_provider_forwards_excluded_codes_to_icloud(self, _resolve, fetch_latest_otp):
        code = email_provider.wait_for_otp(
            "sample@icloud.com",
            after_ts=123.0,
            exclude_codes={"683938"},
        )

        self.assertEqual(code, "294617")
        fetch_latest_otp.assert_called_once_with(
            "sample@icloud.com",
            after_ts=123.0,
            exclude_codes={"683938"},
        )


class ICloudPoolTests(unittest.TestCase):
    def test_import_claim_release_and_delete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(db, "_ICLOUD_EMAIL_JSON", root / "icloud.json"), patch.object(
                db, "_ICLOUD_EMAIL_TXT", root / "icloud.txt"
            ), patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"):
                inserted, skipped = db.import_icloud_emails([
                    {"email": "sample@icloud.com", "code_url": "https://mail.example/code"}
                ])
                self.assertEqual((inserted, skipped), (1, 0))
                self.assertEqual(db.icloud_email_pool_summary()["available"], 1)

                claimed = db.claim_next_icloud_email()
                self.assertEqual(claimed["email"], "sample@icloud.com")
                self.assertEqual(db.icloud_email_pool_summary()["used"], 1)

                db.release_icloud_email("sample@icloud.com", status="available")
                self.assertEqual(db.icloud_email_pool_summary()["available"], 1)
                self.assertTrue(db.delete_icloud_email("sample@icloud.com"))
                self.assertEqual(db.icloud_email_pool_summary()["total"], 0)


if __name__ == "__main__":
    unittest.main()
