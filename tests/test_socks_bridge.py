# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core.socks_bridge import AuthenticatedSocksBridge, needs_authenticated_socks_bridge


class SocksBridgeTests(unittest.TestCase):
    def test_bridge_requirement_only_for_authenticated_socks(self):
        self.assertTrue(needs_authenticated_socks_bridge("socks5://user:pass@proxy.example:1080"))
        self.assertTrue(needs_authenticated_socks_bridge("socks5h://user:pass@proxy.example:1080"))
        self.assertFalse(needs_authenticated_socks_bridge("socks5://proxy.example:1080"))
        self.assertFalse(needs_authenticated_socks_bridge("http://user:pass@proxy.example:8080"))

    @patch("core.socks_bridge.socks.socksocket")
    def test_bridge_connect_uses_remote_dns_and_credentials(self, socket_factory):
        conn = socket_factory.return_value
        bridge = AuthenticatedSocksBridge("socks5://user:pass@proxy.example:1080")

        result = bridge.connect("target.example", 443)

        self.assertIs(result, conn)
        conn.set_proxy.assert_called_once()
        kwargs = conn.set_proxy.call_args.kwargs
        self.assertTrue(kwargs["rdns"])
        self.assertEqual(kwargs["username"], "user")
        self.assertEqual(kwargs["password"], "pass")
        conn.connect.assert_called_once_with(("target.example", 443))


if __name__ == "__main__":
    unittest.main()
