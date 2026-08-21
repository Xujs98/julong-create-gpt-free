# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


class AccountCompletionStatusTests(unittest.TestCase):
    def _storage(self, root: Path):
        return patch.multiple(
            db,
            _DATA_DIR=root,
            _ACCOUNTS_JSON=root / "accounts.json",
            _LEGACY_ACCOUNTS_JSON=root / "legacy_accounts.json",
            _ACCOUNTS_TXT=root / "accounts.txt",
            _TOKENS_TXT=root / "tokens.txt",
            _VIEWER_HTML=root / "viewer.html",
        )

    def test_legacy_statuses_are_derived_and_server_side_filters_work(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text(
                """[
                  {"id":1,"email":"linked@example.com","extract_link_status":"success"},
                  {"id":2,"email":"sms@example.com","codex_status":"success"},
                  {"id":3,"email":"plain@example.com"}
                ]""",
                encoding="utf-8",
            )
            with self._storage(root):
                linked = db.list_accounts(status_filter="link")
                sms = db.list_accounts(status_filter="sms")

            self.assertEqual([row["id"] for row in linked], [1])
            self.assertEqual([row["id"] for row in sms], [2])
            self.assertTrue(linked[0]["link_completed"])
            self.assertTrue(sms[0]["sms_completed"])

    def test_payment_and_sms_statuses_remain_manual(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text(
                '[{"id":1,"email":"linked@example.com","extract_link_status":"success"}]',
                encoding="utf-8",
            )
            with self._storage(root):
                updated = db.update_account_completion_status(1, "payment", True)
                account = db.get_account(1)

            self.assertTrue(updated["payment_completed"])
            self.assertTrue(account["payment_completed"])
            self.assertTrue(account["link_completed"])
            self.assertEqual(account["payment_status_source"], "manual")

    def test_link_status_rejects_manual_toggle(self):
        with self.assertRaises(ValueError):
            db.update_account_completion_status(1, "link", False)

    def test_successful_extract_and_codex_updates_light_the_statuses(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text(
                '[{"id":1,"email":"auto@example.com"}]',
                encoding="utf-8",
            )
            with self._storage(root):
                self.assertTrue(db.update_account_extract(1, {"ok": True, "status": "success"}))
                self.assertTrue(db.update_account_codex_status("auto@example.com", "success"))
                account = db.get_account(1)

            self.assertTrue(account["link_completed"])
            self.assertTrue(account["sms_completed"])
            self.assertEqual(account["link_status_source"], "extract")
            self.assertEqual(account["sms_status_source"], "codex")

    def test_new_extract_clears_stale_link_and_progress_changes_poll_revision(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text(
                """[{"id":1,"email":"retry@example.com","extract_link_status":"success",
                "extract_link_long_url":"https://old.example.test/link","extract_link_progress":100,
                "link_completed":true}]""",
                encoding="utf-8",
            )
            with self._storage(root):
                self.assertTrue(db.claim_account_extract(1, service_id="pp", service_name="PP提链", mode="protocol"))
                queued = db.list_account_plan_check_statuses()
                self.assertTrue(db.update_account_extract(1, {
                    "ok": False, "status": "running", "progress": 48, "message": "创建 PayPal 支付方式",
                }))
                running = db.list_account_plan_check_statuses()

            queued_item = queued["items"][0]
            running_item = running["items"][0]
            self.assertIsNone(queued_item["extract_link_long_url"])
            self.assertFalse(queued_item["link_completed"])
            self.assertEqual(queued_item["extract_link_progress"], 0)
            self.assertEqual(running_item["extract_link_progress"], 48)
            self.assertNotEqual(queued["revision"], running["revision"])


class AccountCompletionStatusApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.db.update_account_completion_status")
    def test_single_status_endpoint(self, update_status):
        update_status.return_value = {
            "id": 7,
            "email": "sample@example.com",
            "link_completed": True,
            "payment_completed": True,
            "sms_completed": False,
        }
        response = self.client.post(
            "/api/accounts/7/completion-status",
            json={"status": "payment", "enabled": True},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["updated"]["payment_completed"])
        update_status.assert_called_once_with(acc_id=7, status_name="payment", enabled=True)

    def test_manual_link_sync_endpoint_is_removed(self):
        response = self.client.post(
            "/api/accounts/link-status/sync",
            json={"text": "sample@example.com"},
        )

        self.assertEqual(response.status_code, 404)

    @patch("webui.app.db.list_accounts_page")
    def test_list_status_filter_is_forwarded(self, list_page):
        list_page.return_value = {"items": [], "total": 0, "offset": 0, "limit": 20, "revision": "0:"}
        response = self.client.get("/api/accounts?paged=1&page=1&page_size=20&status=sms")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list_page.call_args.kwargs["status_filter"], "sms")


if __name__ == "__main__":
    unittest.main()
