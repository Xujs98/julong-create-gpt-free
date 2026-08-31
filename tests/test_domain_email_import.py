# -*- coding: utf-8 -*-
"""现代 UI 域名邮箱池导入回归测试。"""
import json
from unittest.mock import patch

from core import db
from webui.app import create_app


def _client():
    client = create_app(auth_code="test-auth").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
    return client


def test_domain_import_route_uses_icloud_style_two_part_material():
    client = _client()
    with patch("webui.app.db.import_domain_emails", return_value=(2, 1)) as importer:
        response = client.post(
            "/api/outlook/import",
            json={
                "source": "cloudflare_domain",
                "text": (
                    "first@example.test----https://mail.example.test/one\n"
                    "second@example.test====data:text/html,code\n"
                    "third@example.test----https://mail.example.test/three"
                ),
            },
        )

    assert response.status_code == 200
    body = response.get_json()
    assert body["inserted"] == 2
    assert body["skipped"] == 1
    assert body["parsed"] == 3
    importer.assert_called_once_with([
        {
            "email": "first@example.test",
            "code_url": "https://mail.example.test/one",
            "access_token": "",
            "totp_secret": "",
        },
        {
            "email": "second@example.test",
            "code_url": "data:text/html,code",
            "access_token": "",
            "totp_secret": "",
        },
        {
            "email": "third@example.test",
            "code_url": "https://mail.example.test/three",
            "access_token": "",
            "totp_secret": "",
        },
    ])


def test_domain_import_rejects_invalid_url_before_database_write():
    client = _client()
    with patch("webui.app.db.import_domain_emails") as importer:
        response = client.post(
            "/api/outlook/import",
            json={
                "source": "cloudflare_domain",
                "text": "broken@example.test----not-a-url",
            },
        )

    assert response.status_code == 400
    body = response.get_json()
    assert body["invalid_count"] == 1
    assert "取码地址" in body["error"]
    importer.assert_not_called()


def test_domain_import_can_mark_material_as_registered():
    client = _client()
    with patch("webui.app.db.import_registered_email_accounts", return_value=(1, 0)) as importer:
        response = client.post(
            "/api/outlook/import",
            json={
                "source": "cloudflare_domain",
                "as_registered": True,
                "text": "registered@example.test----https://mail.example.test/code",
            },
        )

    assert response.status_code == 200
    assert response.get_json()["as_registered"] is True
    importer.assert_called_once_with(
        [
            {
                "email": "registered@example.test",
                "code_url": "https://mail.example.test/code",
                "access_token": "",
                "totp_secret": "",
            }
        ],
        source="cloudflare_domain",
    )


def test_modern_import_dialog_exposes_domain_pool_option():
    html = _client().get("/").get_data(as_text=True)
    assert 'data-value="cloudflare_domain" role="option">域名邮箱池' in html
    assert "通用API / iCloud / 域名邮箱: email----取码地址" in html


def test_domain_pool_import_deduplicates_and_keeps_copy_line():
    rows = []
    with patch.object(db, "_load_domain_pool", return_value=rows), patch.object(
        db, "_save_domain_pool"
    ) as save:
        inserted, skipped = db.import_domain_emails([
            {"email": "first@example.test", "code_url": "https://mail.example.test/one"},
            {"email": "first@example.test", "code_url": "https://mail.example.test/duplicate"},
            {"email": "", "code_url": "https://mail.example.test/invalid"},
        ])

    assert (inserted, skipped) == (1, 2)
    assert rows[0]["copy_line"] == "first@example.test----https://mail.example.test/one"
    save.assert_called_once_with(rows)


def test_domain_pool_claim_matches_icloud_email_url_semantics():
    rows = [
        {"id": 1, "email": "legacy@example.test", "status": "available"},
        {
            "id": 2,
            "email": "first@example.test",
            "code_url": "https://mail.example.test/first",
            "status": "available",
        },
        {
            "id": 3,
            "email": "second@example.test",
            "code_url": "https://mail.example.test/second",
            "status": "available",
        },
    ]
    with patch.object(db, "_load_domain_pool", return_value=rows), patch.object(
        db, "_save_domain_pool"
    ) as save:
        claimed = db.claim_next_domain_email()

    assert claimed["email"] == "first@example.test"
    assert claimed["code_url"] == "https://mail.example.test/first"
    assert claimed["status"] == "used"
    assert rows[0]["status"] == "available"
    assert rows[1]["status"] == "used"
    assert rows[1]["used_at"]
    save.assert_called_once_with(rows)


def test_domain_pool_claim_returns_none_without_available_url_material():
    rows = [{"id": 1, "email": "legacy@example.test", "status": "available"}]
    with patch.object(db, "_load_domain_pool", return_value=rows), patch.object(
        db, "_save_domain_pool"
    ) as save:
        assert db.claim_next_domain_email() is None

    save.assert_not_called()


def test_domain_pool_is_exposed_by_modern_pool_api():
    client = _client()
    rows = [{"email": "one@example.test", "status": "available", "copy_line": "one@example.test----https://mail.example.test/code"}]
    with patch("webui.app.db.list_domain_email_pool", return_value=rows) as listing:
        response = client.get("/api/outlook?source=cloudflare_domain&paged=1&page=1&page_size=50")

    assert response.status_code == 200
    body = response.get_json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == rows[0]["email"]
    assert body["items"][0]["source"] == "cloudflare_domain"
    listing.assert_called_once()


def test_rebind_reservation_returns_imported_domain_code_url():
    rows = [
        {
            "id": 7,
            "email": "target@example.test",
            "code_url": "https://mail.example.test/code/7",
            "status": "available",
        }
    ]
    with patch.object(db, "_load_domain_pool", return_value=rows), patch.object(
        db, "_save_domain_pool"
    ) as save:
        targets = db.reserve_rebind_emails(
            ["cloudflare_domain"], 1, reservation_id="reservation-1"
        )

    assert targets == [
        {
            "id": 7,
            "email": "target@example.test",
            "source": "cloudflare_domain",
            "code_url": "https://mail.example.test/code/7",
            "password": None,
            "client_id": None,
            "refresh_token": None,
            "reservation_id": "reservation-1",
        }
    ]
    assert rows[0]["status"] == "used"
    assert rows[0]["rebind_reservation_id"] == "reservation-1"
    save.assert_called_once_with(rows)


def test_legacy_domain_rows_round_trip_with_new_import_and_statuses(tmp_path, monkeypatch):
    """旧的 QQ IMAP 行没有 code_url，导入新行后仍可筛选/统计/停用。"""
    monkeypatch.setenv("TURB_STORAGE_BACKEND", "json")
    domain_path = tmp_path / "domain.json"
    domain_path.write_text(
        json.dumps([
            {
                "id": 1,
                "email": "legacy@example.test",
                "status": "available",
                "created_at": "2026-08-01T00:00:00",
            }
        ]),
        encoding="utf-8",
    )
    with patch.multiple(
        db,
        _DOMAIN_EMAIL_JSON=domain_path,
        _DEFAULT_DOMAIN_EMAIL_JSON=domain_path,
        _ACCOUNTS_JSON=tmp_path / "accounts.json",
        _DEFAULT_ACCOUNTS_JSON=tmp_path / "accounts.json",
    ):
        inserted, skipped = db.import_domain_emails([
            {"email": "imported@example.test", "code_url": "https://mail.example.test/imported"},
            {"email": "legacy@example.test", "code_url": "https://mail.example.test/duplicate"},
        ])
        assert (inserted, skipped) == (1, 1)
        available = db.list_domain_email_pool(status="available", limit=20)
        assert {row["email"] for row in available} == {"legacy@example.test", "imported@example.test"}
        legacy = next(row for row in available if row["email"] == "legacy@example.test")
        assert legacy["copy_line"] == "legacy@example.test"
        imported = next(row for row in available if row["email"] == "imported@example.test")
        assert imported["copy_line"].endswith("----https://mail.example.test/imported")
        summary = db.domain_email_pool_summary()
        assert summary["available"] == 1
        assert summary["missing_url"] == 1
        db.release_domain_email("legacy@example.test", status="disabled", note="旧 IMAP 地址停用")
        summary = db.domain_email_pool_summary()
        assert summary["available"] == 1
        assert summary["disabled"] == 1
        saved = json.loads(domain_path.read_text(encoding="utf-8"))
        old_saved = next(row for row in saved if row["email"] == "legacy@example.test")
        assert "code_url" not in old_saved
        assert old_saved["copy_line"] == "legacy@example.test"


def test_legacy_domain_pool_status_api_accepts_disabled():
    client = _client()
    with patch("webui.app.db.release_domain_email") as release:
        response = client.post(
            "/api/domain-pool/status",
            json={"email": "legacy@example.test", "status": "disabled", "note": "停用"},
        )

    assert response.status_code == 200
    release.assert_called_once_with("legacy@example.test", status="disabled", note="停用")


def test_domain_pool_delete_filter_and_summary_api_contracts():
    client = _client()
    with patch("webui.app.db.list_domain_email_pool", return_value=[{
        "email": "failed@example.test", "status": "failed", "copy_line": "failed@example.test",
    }]) as listing, patch("webui.app.db.delete_domain_email", return_value=True) as deleting, patch(
        "webui.app.db.domain_email_pool_summary",
        return_value={"total": 4, "available": 1, "used": 1, "failed": 1, "disabled": 1},
    ), patch("webui.app.db.outlook_pool_summary", return_value={"total": 0, "available": 0, "used": 0, "failed": 0}), patch(
        "webui.app.db.generic_api_email_pool_summary",
        return_value={"total": 0, "available": 0, "used": 0, "failed": 0},
    ), patch("webui.app.db.icloud_email_pool_summary", return_value={"total": 0, "available": 0, "used": 0, "failed": 0}), patch(
        "webui.app.db.count_accounts", return_value=0
    ):
        filtered = client.get("/api/outlook?source=cloudflare_domain&status=failed")
        deleted = client.post(
            "/api/outlook/delete",
            json={"source": "cloudflare_domain", "email": "failed@example.test"},
        )
        summary = client.get("/api/summary")

    assert filtered.status_code == 200
    assert filtered.get_json()[0]["source"] == "cloudflare_domain"
    listing.assert_called_once_with(status="failed", limit=500)
    assert deleted.get_json()["deleted"] is True
    deleting.assert_called_once_with("failed@example.test")
    summary_body = summary.get_json()
    assert summary_body["domain_total"] == 4
    assert summary_body["domain_available"] == 1
    assert summary_body["pool_by_source"]["cloudflare_domain"]["disabled"] == 1


def test_rebind_domain_summary_and_reservation_skip_rows_without_url():
    rows = [
        {"id": 1, "email": "legacy@example.test", "status": "available"},
        {
            "id": 2,
            "email": "ready@example.test",
            "code_url": "https://mail.example.test/ready",
            "status": "available",
        },
    ]
    with patch.object(db, "_load_domain_pool", return_value=rows), patch.object(
        db, "_save_domain_pool"
    ):
        summary = db.list_rebind_email_pool_summary()["cloudflare_domain"]
        reserved = db.reserve_rebind_emails(
            ["cloudflare_domain"], 1, reservation_id="domain-url-only"
        )

    assert summary["available"] == 1
    assert summary["missing_url"] == 1
    assert summary["items"] == [{
        "id": 2,
        "email": "ready@example.test",
        "source": "cloudflare_domain",
    }]
    assert reserved[0]["email"] == "ready@example.test"
    assert reserved[0]["code_url"] == "https://mail.example.test/ready"
    assert rows[0]["status"] == "available"
