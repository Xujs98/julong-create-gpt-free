# -*- coding: utf-8 -*-
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from config import browser_use
from core.browser_use_client import BrowserUseClient


class BrowserUseConfigTests(unittest.TestCase):
    def test_build_connect_url_contains_required_cloud_options(self):
        with patch.object(browser_use, "BROWSER_USE_PROXY_COUNTRY_CODE", "us"), patch.object(
            browser_use, "BROWSER_USE_USE_PROXY", True
        ), patch.object(browser_use, "BROWSER_USE_PROFILE_ID", "profile-123"), patch.object(
            browser_use, "BROWSER_USE_SESSION_TIMEOUT", 999
        ):
            session = BrowserUseClient(api_key="key-123").open_session()

        parsed = urlsplit(session.connect_url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "wss")
        self.assertEqual(query["apiKey"], ["key-123"])
        self.assertEqual(query["proxyCountryCode"], ["us"])
        self.assertEqual(query["profileId"], ["profile-123"])
        self.assertEqual(query["timeout"], ["240"])
        self.assertNotIn("key-123", str(session.raw))


if __name__ == "__main__":
    unittest.main()
