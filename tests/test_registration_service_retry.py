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


def test_browser_challenge_quarantine_excludes_and_deletes_proxy():
    proxy = "http://proxy-a.test:8080"
    excluded = set()
    log = logging.getLogger("proxy-quarantine-test")
    with patch("config.proxy.PROXY_DELETE_UNHEALTHY_IPS", True), patch(
        "config.proxy.PROXY_POOL", [proxy, "http://proxy-b.test:8080"]
    ), patch("core.proxy_test.persist_proxy_pool") as persist:
        registration_service._quarantine_browser_challenged_proxy(
            proxy, excluded, 57, log
        )

    assert proxy in excluded
    persist.assert_called_once_with(["http://proxy-b.test:8080"])


def test_atomic_proxy_deletion_uses_current_pool_state():
    proxy_a = "http://proxy-a.test:8080"
    proxy_b = "http://proxy-b.test:8080"
    with patch("config.proxy.PROXY_POOL", [proxy_a, proxy_b]), patch(
        "core.proxy_test.persist_proxy_pool"
    ) as persist:
        removed = registration_service._delete_proxies_from_pool({proxy_a})

    assert removed == 1
    persist.assert_called_once_with([proxy_b])


def test_registration_proxy_selection_filters_challenged_proxy_and_passes_samples():
    log = logging.getLogger("proxy-selection-test")
    failed_proxy = "http://proxy-a.test:8080"
    remaining_proxy = "http://proxy-b.test:8080"
    with patch("config.proxy.PROXY_HEALTH_CHECK_BEFORE_REGISTRATION", True), patch(
        "config.proxy.PROXY_DELETE_UNHEALTHY_IPS", False
    ), patch("config.proxy.PROXY_POOL", [failed_proxy, remaining_proxy]), patch(
        "config.proxy.PROXY_WARMUP_EXIT_SAMPLES", 4
    ), patch("core.proxy_test.choose_healthy_proxy") as choose:
        choose.return_value = {
            "ok": True,
            "proxy_url": remaining_proxy,
            "result": {"proxy": "http://proxy-b.test:8080"},
            "checked": [],
        }
        selected = registration_service._select_registration_proxy(
            57, log, excluded_proxies={failed_proxy}
        )

    assert selected == remaining_proxy
    assert choose.call_args.args[0] == [remaining_proxy]
    assert choose.call_args.kwargs["exit_samples"] == 4


def test_registration_proxy_selection_rotates_excluded_proxy_without_health_check():
    log = logging.getLogger("proxy-selection-no-health-test")
    failed_proxy = "http://proxy-a.test:8080"
    remaining_proxy = "http://proxy-b.test:8080"
    with patch("config.proxy.PROXY_HEALTH_CHECK_BEFORE_REGISTRATION", False), patch(
        "config.proxy.PROXY_POOL", [failed_proxy, remaining_proxy]
    ), patch("random.choice", return_value=remaining_proxy) as choose:
        selected = registration_service._select_registration_proxy(
            57, log, excluded_proxies={failed_proxy}
        )

    assert selected == remaining_proxy
    choose.assert_called_once_with([remaining_proxy])


def test_proxy_identity_treats_socks5_and_socks5h_as_same_pool_entry():
    assert registration_service._proxy_identity(
        "socks5://user:pass@proxy.test:1080"
    ) == registration_service._proxy_identity(
        "socks5h://user:pass@proxy.test:1080"
    )


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
