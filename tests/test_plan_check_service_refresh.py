# -*- coding: utf-8 -*-
from unittest.mock import patch

from core import live_check_service, plan_check_service


def test_plan_worker_reads_latest_token_when_execution_starts():
    """套餐任务排队后再执行时，使用数据库最新 AT，而不是入队时的旧 AT。"""
    account = {"id": 1, "email": "user@example.com", "access_token": "NEW_TOKEN"}
    checked = {"ok": True, "current_plan_type": "plus", "plus_trial_eligible": False}
    with patch("core.plan_check_service.db.mark_account_plan_check_running", return_value=True), patch(
        "core.plan_check_service.db.get_account", return_value=account
    ), patch(
        "core.plan_check_service._wait_for_rate_slot"
    ), patch(
        "core.plan_check_service._check_plan_with_account_context", return_value=checked
    ) as query, patch(
        "core.plan_check_service.db.update_account_plan_check"
    ), patch.object(plan_check_service._QUEUE_SLOTS, "release"):
        result = plan_check_service._run_plan_check(
            account_id=1,
            email="user@example.com",
            access_token="OLD_TOKEN",
            trigger="manual",
            proxy="PROXY",
            timezone_offset_min="-",
        )

    assert result["ok"] is True
    assert result["current_plan_type"] == "plus"
    assert query.call_args.args[1] == "NEW_TOKEN"


def test_plan_worker_refreshes_login_and_retries_after_401():
    """套餐接口 401 时，有保存密码的账号协议刷新登录态后用新 AT 重试。"""
    old = {
        "id": 1,
        "email": "user@example.com",
        "access_token": "OLD_TOKEN",
        "registration_password": "PASSWORD",
    }
    new = {**old, "access_token": "NEW_TOKEN", "device_id": "DEVICE"}
    first = {"ok": False, "http_status": 401, "needs_live_check": True, "token_expired": True}
    second = {"ok": True, "current_plan_type": "plus", "plus_trial_eligible": False}
    with patch("core.plan_check_service.db.mark_account_plan_check_running", return_value=True), patch(
        "core.plan_check_service.db.get_account", side_effect=[old, old, new]
    ), patch(
        "core.plan_check_service._wait_for_rate_slot"
    ), patch(
        "core.plan_check_service._check_plan_with_account_context", side_effect=[first, second]
    ) as query, patch(
        "core.plan_check_service._refresh_login_for_plan",
        return_value={"ok": True, "access_token": "NEW_TOKEN", "device_id": "DEVICE"},
    ) as refresh, patch(
        "core.plan_check_service.db.update_account_plan_check"
    ), patch.object(plan_check_service._QUEUE_SLOTS, "release"):
        result = plan_check_service._run_plan_check(
            account_id=1,
            email="user@example.com",
            access_token="OLD_TOKEN",
            trigger="manual",
            proxy="PROXY",
            timezone_offset_min="-",
        )

    assert result["ok"] is True
    assert result["current_plan_type"] == "plus"
    assert result["live_refresh_performed"] is True
    refresh.assert_called_once()
    assert query.call_args_list[-1].args[1] == "NEW_TOKEN"


def test_live_refresh_automatically_queues_plan_recheck():
    """查活写入新 AT 后自动查询套餐，清除表格里上一枚 AT 的失败状态。"""
    account = {"id": 1, "email": "user@example.com", "access_token": "OLD_TOKEN"}
    live_result = {
        "ok": True,
        "status": "live",
        "access_token": "NEW_TOKEN",
        "check_method": "password_totp",
    }
    with patch("core.live_check_service.db.mark_account_live_check_running", return_value=True), patch(
        "core.live_check_service.resolve_plan_check_route",
        return_value={"proxy": "PROXY", "proxy_mode": "auto", "network_route": "proxy", "proxy_used": "***"},
    ), patch(
        "core.live_check_service.db.get_account", return_value=account
    ), patch(
        "core.live_check_service._check_existing_access_token", return_value=None
    ), patch(
        "core.live_check_service.check_account_liveness", return_value=live_result
    ), patch(
        "core.live_check_service.db.update_account_liveness"
    ), patch(
        "core.plan_check_service.enqueue_account_plan_check", return_value={"accepted": True}
    ) as enqueue_plan, patch(
        "core.live_check_service._append_log"
    ), patch.object(live_check_service._QUEUE_SLOTS, "release"):
        result = live_check_service._run_live_check(
            account_id=1,
            email="user@example.com",
            proxy=None,
            trigger="manual",
        )

    assert result == live_result
    enqueue_plan.assert_called_once_with(
        account_id=1,
        email="user@example.com",
        access_token="NEW_TOKEN",
        trigger="live_check_refresh",
        proxy="PROXY",
    )


def test_plan_query_enables_oaics_check_with_account_country():
    account = {
        "id": 1,
        "email": "user@example.com",
        "proxy_country_code": "US",
    }
    with patch("core.plan_check_service.check_account_plan", return_value={"ok": True}) as query:
        plan_check_service._check_plan_with_account_context(
            account,
            "TOKEN",
            proxy="PROXY",
            timezone_offset_min="-",
        )

    assert query.call_args.kwargs["check_oaics"] is True
    assert query.call_args.kwargs["billing_country"] == "US"


def test_failed_plan_protocol_refresh_does_not_overwrite_live_status():
    account = {
        "id": 1,
        "email": "user@example.com",
        "registration_password": "PASSWORD",
    }
    failed = {"ok": False, "status": "failed", "error": "HTTP 403"}
    with patch("core.account_liveness.check_account_liveness", return_value=failed), patch(
        "core.plan_check_service.db.update_account_liveness"
    ) as update_live:
        result = plan_check_service._refresh_login_for_plan(account, proxy="PROXY")

    assert result == failed
    update_live.assert_not_called()


def test_fresh_browser_live_check_does_not_start_duplicate_protocol_refresh():
    account = {
        "id": 1,
        "email": "user@example.com",
        "access_token": "NEW_TOKEN",
        "registration_password": "PASSWORD",
    }
    rejected = {
        "ok": False,
        "http_status": 401,
        "needs_live_check": True,
        "token_expired": True,
        "error": "HTTP 401",
    }
    with patch("core.plan_check_service.db.mark_account_plan_check_running", return_value=True), patch(
        "core.plan_check_service.db.get_account", return_value=account
    ), patch("core.plan_check_service._wait_for_rate_slot"), patch(
        "core.plan_check_service._check_plan_with_account_context", return_value=rejected
    ), patch("core.plan_check_service._refresh_login_for_plan") as refresh, patch(
        "core.plan_check_service.db.update_account_plan_check"
    ), patch.object(plan_check_service._QUEUE_SLOTS, "release"):
        result = plan_check_service._run_plan_check(
            account_id=1,
            email="user@example.com",
            access_token="NEW_TOKEN",
            trigger="live_check_refresh",
            proxy="PROXY",
            timezone_offset_min="-",
        )

    assert result["ok"] is False
    assert result["live_refresh_skipped"] == "fresh_browser_session"
    refresh.assert_not_called()
