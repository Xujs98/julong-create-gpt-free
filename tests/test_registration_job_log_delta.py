from pathlib import Path
from unittest.mock import patch

from core import registration_service


def test_read_job_log_delta_returns_only_new_bytes(tmp_path: Path):
    log_path = tmp_path / "job.log"
    log_path.write_text("first\n", encoding="utf-8")
    job = {"id": 1, "log_file": str(log_path)}

    with patch.object(registration_service.db, "get_job", return_value=job):
        first = registration_service.read_job_log_delta(1)
        log_path.write_text("first\nsecond\n", encoding="utf-8")
        second = registration_service.read_job_log_delta(1, offset=first["offset"])

    assert first["reset"] is True
    assert first["changed"] is True
    assert first["content"] == "first\n"
    assert second["reset"] is False
    assert second["changed"] is True
    assert second["content"] == "second\n"
    assert second["offset"] == log_path.stat().st_size

    unchanged = registration_service.read_job_log_delta(1, offset=second["offset"], job=job)
    assert unchanged["content"] == ""
    assert unchanged["changed"] is False


def test_read_job_log_delta_resets_after_truncation(tmp_path: Path):
    log_path = tmp_path / "job.log"
    log_path.write_text("old\n", encoding="utf-8")
    job = {"id": 1, "log_file": str(log_path)}

    with patch.object(registration_service.db, "get_job", return_value=job):
        first = registration_service.read_job_log_delta(1)
        log_path.write_text("new\n", encoding="utf-8")
        second = registration_service.read_job_log_delta(1, offset=first["offset"] + 100)

    assert second["reset"] is True
    assert second["content"] == "new\n"


def test_read_job_log_delta_defers_growth_after_stat_to_next_poll(tmp_path: Path):
    log_path = tmp_path / "job.log"
    log_path.write_bytes(b"first\n")
    job = {"id": 1, "log_file": str(log_path)}
    original_open = Path.open
    appended = False

    def open_after_growth(self, *args, **kwargs):
        nonlocal appended
        if self == log_path and args and args[0] == "rb" and not appended:
            appended = True
            with original_open(log_path, "ab") as handle:
                handle.write(b"second\n")
        return original_open(self, *args, **kwargs)

    with patch.object(Path, "open", open_after_growth):
        first = registration_service.read_job_log_delta(1, job=job)
    second = registration_service.read_job_log_delta(1, offset=first["offset"], job=job)

    assert first["content"] == "first\n"
    assert first["offset"] == len(b"first\n")
    assert second["content"] == "second\n"
    assert second["offset"] == len(b"first\nsecond\n")


def test_read_job_log_delta_handles_cleanup_between_stat_and_open(tmp_path: Path):
    log_path = tmp_path / "job.log"
    log_path.write_text("terminal\n", encoding="utf-8")
    job = {"id": 1, "log_file": str(log_path)}
    original_open = Path.open

    def open_after_delete(self, *args, **kwargs):
        if self == log_path and args and args[0] == "rb":
            log_path.unlink(missing_ok=True)
        return original_open(self, *args, **kwargs)

    with patch.object(Path, "open", open_after_delete):
        result = registration_service.read_job_log_delta(1, offset=3, job=job)

    assert result["content"] == ""
    assert result["exists"] is False
