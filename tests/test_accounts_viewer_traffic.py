from pathlib import Path


DB_SOURCE = Path(__file__).parents[1] / "core" / "db.py"


def test_offline_accounts_viewer_shows_bidirectional_registration_traffic():
    source = DB_SOURCE.read_text(encoding="utf-8")

    assert "function fmtTrafficBreakdown(total, upload, download)" in source
    assert (
        "fmtTrafficBreakdown(r.registration_traffic_bytes, "
        "r.registration_upload_bytes, r.registration_download_bytes)"
    ) in source
