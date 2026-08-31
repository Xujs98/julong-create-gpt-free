# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import cf_temp_mail_client, email_provider


class EmailProviderCloudflareTests(unittest.TestCase):
    def setUp(self):
        cf_temp_mail_client._CONTEXT_CACHE.clear()

    def test_parse_email_sources_includes_cloudflare(self):
        self.assertEqual(
            email_provider.parse_email_sources("cloudflare,gptmail"),
            ["cloudflare", "gptmail"],
        )

    def test_resolve_prefers_cf_context_over_domain_suffix(self):
        cf_temp_mail_client._CONTEXT_CACHE["x@custom.com"] = cf_temp_mail_client.CFTempMailAccount(
            email="x@custom.com",
            jwt="jwt",
            domain="custom.com",
        )
        with patch.object(email_provider, "parse_email_sources", return_value=["cloudflare_domain"]):
            # even if EMAIL_DOMAIN matches, cache wins
            from config import email as email_cfg
            with patch.object(email_cfg, "EMAIL_DOMAIN", "custom.com"):
                self.assertEqual(email_provider.resolve_email_source("x@custom.com"), "cloudflare")

    @patch("core.cf_temp_mail_client.pick_account")
    def test_pick_from_source_cloudflare(self, pick_account):
        pick_account.return_value = cf_temp_mail_client.CFTempMailAccount(
            email="n@mail.test", jwt="j"
        )
        self.assertEqual(email_provider._pick_from_source("cloudflare"), "n@mail.test")
        pick_account.assert_called_once()

    @patch("core.cf_temp_mail_client.release_account")
    def test_release_routes_to_cloudflare(self, release_account):
        cf_temp_mail_client._CONTEXT_CACHE["n@mail.test"] = cf_temp_mail_client.CFTempMailAccount(
            email="n@mail.test", jwt="j"
        )
        source = email_provider.release_email("n@mail.test", status="used", note="ok")
        self.assertEqual(source, "cloudflare")
        release_account.assert_called_once()

    @patch("core.icloud_client.fetch_latest_otp", return_value="482931")
    @patch("core.db.get_domain_email_by_email", return_value={
        "email": "target@example.test",
        "code_url": "https://mail.example.test/code/target",
    })
    @patch("core.email_provider.resolve_email_source", return_value="cloudflare_domain")
    def test_domain_imported_html_url_uses_icloud_parser(self, _resolve, get_domain, fetch_latest_otp):
        code = email_provider.wait_for_otp(
            "target@example.test",
            after_ts=123.0,
            max_wait=7,
            exclude_codes={"123456"},
        )

        self.assertEqual(code, "482931")
        get_domain.assert_called_once_with("target@example.test")
        fetch_latest_otp.assert_called_once_with(
            "target@example.test",
            code_url="https://mail.example.test/code/target",
            after_ts=123.0,
            exclude_codes={"123456"},
            max_wait=7,
        )

    @patch("core.db.get_domain_email_by_email", return_value={
        "email": "legacy@example.test",
        "status": "available",
    })
    @patch("core.email_provider.resolve_email_source", return_value="cloudflare_domain")
    def test_domain_row_without_url_reports_material_error(self, _resolve, get_domain):
        with self.assertRaisesRegex(RuntimeError, "缺少接码 URL"):
            email_provider.wait_for_otp("legacy@example.test", after_ts=123.0)

        get_domain.assert_called_once_with("legacy@example.test")

    @patch("core.db.domain_email_pool_summary", return_value={"available": 0, "total": 2})
    @patch("core.db.claim_next_domain_email", return_value=None)
    def test_domain_source_requires_imported_email_url_material(self, claim, summary):
        with self.assertRaisesRegex(RuntimeError, "email----URL"):
            email_provider._pick_from_source("cloudflare_domain")

        claim.assert_called_once_with()
        summary.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
