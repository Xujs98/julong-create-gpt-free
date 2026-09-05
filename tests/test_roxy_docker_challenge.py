from unittest.mock import patch

from core import roxy_registration


def test_docker_roxy_keeps_cloudflare_resources_unblocked():
    driver = object()
    with patch("core.roxy_selenium._running_in_container", return_value=True), patch(
        "core.traffic_optimizer.install_selenium_network_optimization"
    ) as install:
        roxy_registration._install_registration_traffic_optimization(driver)

    install.assert_not_called()
