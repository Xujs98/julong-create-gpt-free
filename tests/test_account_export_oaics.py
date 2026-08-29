from unittest.mock import patch

from core.account_export import save_account_data


def _save_patches():
    return (
        patch("core.account_export._capture_proxy_geo", return_value={}),
        patch("core.account_export._append_batch_archive"),
        patch("core.db.insert_account", return_value=42),
    )


def test_registration_auto_oaics_query_respects_disabled_switch():
    with _save_patches()[0] as geo, _save_patches()[1] as archive, _save_patches()[2] as insert, patch(
        "config.register.OAICS_CHECK_AFTER_REGISTRATION", False
    ), patch("core.plan_check_service.enqueue_account_plan_check") as enqueue:
        row_id = save_account_data("user@example.test", "TOKEN", extra={})

    assert row_id == 42
    insert.assert_called_once()
    archive.assert_called_once()
    geo.assert_called_once()
    enqueue.assert_called_once_with(
        account_id=42,
        email="user@example.test",
        access_token="TOKEN",
        trigger="registration_auto",
        check_oaics=False,
    )


def test_registration_auto_oaics_query_is_enqueued_when_enabled():
    with _save_patches()[0], _save_patches()[1], _save_patches()[2], patch(
        "config.register.OAICS_CHECK_AFTER_REGISTRATION", True
    ), patch(
        "core.plan_check_service.enqueue_account_plan_check",
        return_value={"accepted": True},
    ) as enqueue:
        save_account_data("user@example.test", "TOKEN", extra={})

    assert enqueue.call_args.kwargs == {
        "account_id": 42,
        "email": "user@example.test",
        "access_token": "TOKEN",
        "trigger": "registration_auto",
        "check_oaics": True,
    }
