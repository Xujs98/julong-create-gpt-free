# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

import main
from config import roxybrowser


class RegistrationDriverDispatchTests(unittest.TestCase):
    def _run(self):
        return main.run_registration(
            email="sample@example.com",
            name="Sample User",
            birthday="1990-01-01",
            proxy="",
            otp_code="123456",
        )

    @patch("core.roxy_registration.run_roxy_registration", return_value={"driver": "roxy"})
    def test_dispatches_roxy(self, run_driver):
        with patch.object(roxybrowser, "REGISTRATION_DRIVER", "roxy"):
            result = self._run()
        self.assertEqual(result["driver"], "roxy")
        run_driver.assert_called_once()

    @patch("core.cloakbrowser_registration.run_cloak_registration", return_value={"driver": "cloak"})
    def test_dispatches_cloak(self, run_driver):
        with patch.object(roxybrowser, "REGISTRATION_DRIVER", "cloak"):
            result = self._run()
        self.assertEqual(result["driver"], "cloak")
        run_driver.assert_called_once()

    @patch("core.browser_use_registration.run_browser_use_registration", return_value={"driver": "browser_use"})
    def test_dispatches_browser_use(self, run_driver):
        with patch.object(roxybrowser, "REGISTRATION_DRIVER", "browser_use"):
            result = self._run()
        self.assertEqual(result["driver"], "browser_use")
        run_driver.assert_called_once()

    @patch("core.skyvern_registration.run_skyvern_registration", return_value={"driver": "skyvern"})
    def test_dispatches_skyvern(self, run_driver):
        with patch.object(roxybrowser, "REGISTRATION_DRIVER", "skyvern"):
            result = self._run()
        self.assertEqual(result["driver"], "skyvern")
        run_driver.assert_called_once()

    @patch("main.BrowserSession", side_effect=RuntimeError("protocol-selected"))
    def test_dispatches_protocol(self, browser_session):
        with patch.object(roxybrowser, "REGISTRATION_DRIVER", "protocol"):
            with self.assertRaisesRegex(RuntimeError, "protocol-selected"):
                self._run()
        browser_session.assert_called_once_with(proxy="")


if __name__ == "__main__":
    unittest.main()
