# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core.proxy_utils import masked_proxy_url, normalize_proxy_url, rotate_proxy_session


class ProxyUtilsTests(unittest.TestCase):
    def test_normalizes_four_part_proxy(self):
        self.assertEqual(
            normalize_proxy_url("proxy.example:3000:user-name:pass-word"),
            "http://user-name:pass-word@proxy.example:3000",
        )

    def test_normalizes_host_and_port(self):
        self.assertEqual(
            normalize_proxy_url("127.0.0.1:7897"),
            "http://127.0.0.1:7897",
        )

    def test_preserves_standard_url_and_maps_socks5h(self):
        self.assertEqual(
            normalize_proxy_url("socks5h://user:pass@127.0.0.1:7897"),
            "socks5h://user:pass@127.0.0.1:7897",
        )

    @patch("core.proxy_utils._endpoint_supports_socks5", return_value=True)
    def test_auto_detects_socks5h_for_four_part_proxy(self, supports_socks5):
        self.assertEqual(
            normalize_proxy_url("proxy.example:3000:user:pass", default_scheme="auto"),
            "socks5h://user:pass@proxy.example:3000",
        )
        supports_socks5.assert_called_once_with("proxy.example", 3000, 2.0)

    @patch("core.proxy_utils._endpoint_supports_socks5", return_value=False)
    def test_auto_falls_back_to_http(self, supports_socks5):
        self.assertEqual(
            normalize_proxy_url("proxy.example:3000:user:pass", default_scheme="auto"),
            "http://user:pass@proxy.example:3000",
        )
        supports_socks5.assert_called_once_with("proxy.example", 3000, 2.0)

    def test_encodes_special_characters_in_four_part_credentials(self):
        self.assertEqual(
            normalize_proxy_url("proxy.example:3000:user@example:p:a/s"),
            "http://user%40example:p%3Aa%2Fs@proxy.example:3000",
        )

    def test_masked_proxy_hides_four_part_credentials(self):
        masked = masked_proxy_url("proxy.example:3000:private-user:private-password")
        self.assertEqual(masked, "http://***:***@proxy.example:3000")
        self.assertNotIn("private-user", masked)
        self.assertNotIn("private-password", masked)

    def test_rejects_ambiguous_proxy(self):
        with self.assertRaisesRegex(ValueError, "代理格式"):
            normalize_proxy_url("proxy.example")

    def test_rotates_provider_session_id(self):
        proxy = "socks5://user-region-US-sid-OLD123-t-5:pass@proxy.example:3000"
        rotated = rotate_proxy_session(proxy, session_id="NEW456")
        self.assertIn("-sid-NEW456-t-5", rotated)
        self.assertNotIn("-sid-OLD123-t-5", rotated)

    def test_rotation_leaves_regular_proxy_unchanged(self):
        proxy = "socks5://user:pass@proxy.example:3000"
        self.assertEqual(rotate_proxy_session(proxy, session_id="NEW456"), proxy)


if __name__ == "__main__":
    unittest.main()
