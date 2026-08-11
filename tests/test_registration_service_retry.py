import logging
import tempfile
from pathlib import Path
from unittest.mock import patch

from core import registration_service


def test_transient_registration_failure_classification():
    assert registration_service._is_transient_registration_failure(
        "TargetClosedError: target page, context or browser has been closed"
    )
    assert registration_service._is_transient_registration_failure(
        "TimeoutError: Page.goto: Timeout 90000ms exceeded"
    )
    assert not registration_service._is_transient_registration_failure(
        "RuntimeError: 邮箱验证码连续错误/过期"
    )
    assert not registration_service._is_transient_registration_failure(
        "RuntimeError: 邮箱提交后进入登录密码页"
    )


def test_transient_retry_limit_is_bounded_and_configurable():
    with patch("config.register.REGISTRATION_TRANSIENT_RETRIES", 99):
        assert registration_service._registration_transient_retry_limit() == 3
    with patch("config.register.REGISTRATION_TRANSIENT_RETRIES", -1):
        assert registration_service._registration_transient_retry_limit() == 0


def test_job_log_context_allows_info_when_root_logger_is_warning():
    root = logging.getLogger()
    previous_level = root.level
    try:
        root.setLevel(logging.WARNING)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "job.log"
            with registration_service._JobLogContext(str(path)):
                logging.getLogger("job-test").info("terminal success marker")
            assert "terminal success marker" in path.read_text(encoding="utf-8")
        assert root.level == logging.WARNING
    finally:
        root.setLevel(previous_level)
