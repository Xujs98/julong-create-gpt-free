from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import twofa_setup_service


def test_enqueue_twofa_setup_claims_then_submits_worker():
    slots = MagicMock()
    slots.acquire.return_value = True
    with patch.object(twofa_setup_service, "_QUEUE_SLOTS", slots), patch(
        "core.twofa_setup_service.db.claim_account_twofa_setup", return_value=True
    ) as claim, patch.object(twofa_setup_service._EXECUTOR, "submit") as submit:
        result = twofa_setup_service.enqueue_account_twofa_setup(
            account_id=9,
            email="user@example.test",
            trigger="manual_retry",
            proxy="PROXY",
        )

    assert result["accepted"] is True
    assert result["status"] == "queued"
    claim.assert_called_once_with(9, trigger="manual_retry")
    submit.assert_called_once_with(
        twofa_setup_service._run_twofa_setup,
        account_id=9,
        email="user@example.test",
        proxy="PROXY",
        trigger="manual_retry",
    )
    slots.release.assert_not_called()


def test_enqueue_twofa_setup_releases_slot_when_account_is_busy():
    slots = MagicMock()
    slots.acquire.return_value = True
    with patch.object(twofa_setup_service, "_QUEUE_SLOTS", slots), patch(
        "core.twofa_setup_service.db.claim_account_twofa_setup", return_value=False
    ):
        result = twofa_setup_service.enqueue_account_twofa_setup(
            account_id=9,
            email="user@example.test",
        )

    assert result["accepted"] is False
    assert result["busy"] is True
    slots.release.assert_called_once()


def test_twofa_worker_refreshes_login_and_persists_new_secret():
    account = {
        "id": 9,
        "email": "user@example.test",
        "registration_password": "PASSWORD",
        "device_id": "ACCOUNT_DEVICE",
        "extra_json": '{"session":{"cookies":[{"name":"sid","value":"COOKIE"}]}}',
    }
    fake_env = SimpleNamespace(
        session=SimpleNamespace(cookies=MagicMock(), close=MagicMock()),
        device_id="DEVICE",
    )
    slots = MagicMock()
    with patch.object(twofa_setup_service, "_QUEUE_SLOTS", slots), patch(
        "core.twofa_setup_service.db.mark_account_twofa_setup_running", return_value=True
    ), patch(
        "core.twofa_setup_service.db.get_account", side_effect=[account, account]
    ), patch(
        "core.twofa_setup_service.resolve_plan_check_route", return_value={"proxy": "PROXY"}
    ), patch(
        "core.twofa_setup_service.check_account_liveness",
        return_value={"ok": True, "session": {"cookies": [{"name": "sid", "value": "NEW"}]}},
    ) as liveness, patch(
        "core.twofa_setup_service.db.update_account_liveness"
    ) as update_live, patch(
        "core.twofa_setup_service.BrowserSession", return_value=fake_env
    ), patch(
        "core.twofa_setup_service.setup_2fa", return_value="TOTPSECRET"
    ) as setup, patch(
        "core.twofa_setup_service.db.update_account_twofa_setup"
    ) as save:
        result = twofa_setup_service._run_twofa_setup(
            account_id=9,
            email="user@example.test",
            proxy=None,
            trigger="manual_retry",
        )

    assert result["ok"] is True
    assert result["totp_secret"] == "TOTPSECRET"
    liveness.assert_called_once_with(
        "user@example.test",
        proxy="PROXY",
        clear_log=False,
        account=account,
        preflight_attempts=1,
        rotate_proxy_on_retry=False,
    )
    update_live.assert_called_once()
    setup.assert_called_once_with(fake_env, "user@example.test")
    assert fake_env.device_id == "ACCOUNT_DEVICE"
    save.assert_called_once()
    assert save.call_args.args[1]["totp_secret"] == "TOTPSECRET"
    fake_env.session.close.assert_called_once()
    slots.release.assert_called_once()


def test_twofa_prefers_account_saved_proxy_over_plan_check_route():
    account = {
        "proxy_used": "socks5://user:pass@proxy.example:3000",
        "live_check_proxy_used": "socks5://user:pass@latest.example:3000",
    }

    selected, source = twofa_setup_service._resolve_twofa_proxy(account, None)

    assert selected == "socks5://user:pass@latest.example:3000"
    assert source == "account:live_check_proxy_used"


def test_twofa_explicit_proxy_overrides_account_saved_proxy():
    account = {"proxy_used": "socks5://user:pass@proxy.example:3000"}
    with patch("core.twofa_setup_service.resolve_plan_check_route", return_value={"proxy": "REQUEST_PROXY"}) as route:
        selected, source = twofa_setup_service._resolve_twofa_proxy(account, "REQUEST_PROXY")

    assert selected == "REQUEST_PROXY"
    assert source == "request"
    route.assert_called_once_with("REQUEST_PROXY")
