from pathlib import Path
import json

from core import db
from core.db import _account_matches_query


def _row(*, plan="free", trial=False, twofa=False, **extra):
    row = {
        "id": 1,
        "email": "user@example.test",
        "email_source": "icloud",
        "current_plan_type": plan,
        "plan_type": "free",
        "plus_trial_eligible": trial,
        "plan_check_status": "success",
        "plan_check_ok": True,
        "totp_secret": "TOTPSECRET" if twofa else None,
        "access_token": "ACCESS_TOKEN",
        "link_completed": False,
        "sms_completed": False,
        "extra_json": '{"account":{"planType":"free"}}',
    }
    row.update(extra)
    return row


def test_formula_requires_free_trial_and_twofa():
    query = "free(可Plus试用)&&[2FA]"
    assert _account_matches_query(_row(trial=True, twofa=True), query)
    assert not _account_matches_query(_row(trial=True, twofa=False), query)
    assert not _account_matches_query(_row(plan="plus", trial=True, twofa=True), query)


def test_bang_excludes_displayed_free_plan_and_ignores_stale_extra_json():
    assert not _account_matches_query(_row(plan="free"), "!free")
    assert _account_matches_query(_row(plan="plus"), "!free")
    assert not _account_matches_query(_row(plan="free"), "!**free")


def test_plus_search_does_not_include_free_trial_accounts():
    assert not _account_matches_query(_row(plan="free", trial=True), "plus")
    assert _account_matches_query(_row(plan="chatgpt_plus", trial=False), "plus")


def test_formula_supports_negative_status_and_other_status_aliases():
    assert _account_matches_query(_row(plan="free", twofa=False), "free&&![2FA]")
    assert not _account_matches_query(_row(plan="free", twofa=True), "free&&![2FA]")
    assert _account_matches_query(_row(plan="plus", twofa=True, link_completed=True), "[提链]&&[2FA]")


def test_plain_search_remains_case_insensitive():
    assert _account_matches_query(_row(), "USER@EXAMPLE.TEST")
    assert _account_matches_query(_row(), "ICLOUD")


def test_oaics_status_alias_is_searchable():
    assert _account_matches_query(_row(oaics_eligible=True), "[oaics]")
    assert not _account_matches_query(_row(oaics_eligible=False), "[oaics]")
    assert _account_matches_query(_row(oaics_eligible=False), "[无oaics]")


def test_proxy_geo_alias_supports_brackets_and_and():
    row = _row(
        twofa=True,
        proxy_geo={
            "ip": "203.0.113.9",
            "country_code": "JP",
            "country": "Japan",
            "region": "Tokyo",
            "city": "Tokyo",
            "timezone": "Asia/Tokyo",
        }
    )
    assert _account_matches_query(row, "[jp]")
    assert _account_matches_query(row, "[jp]&&[2FA]")
    assert _account_matches_query(row, "[tokyo]&&free")
    assert _account_matches_query(row, "[203.0.113.9]")
    assert not _account_matches_query(row, "[us]")


def test_proxy_country_search_backfills_legacy_proxy_label():
    row = _row(proxy_used="socks5h://***:***@jp.proxy.example:3000")
    assert _account_matches_query(row, "[jp]")
    assert _account_matches_query(row, "[jp]&&free")


def test_account_search_inputs_explain_formula_syntax():
    root = Path(__file__).parents[1]
    for relative in ("webui/templates/index.html", "webui/templates/index_legacy.html"):
        source = (root / relative).read_text(encoding="utf-8")
        assert "free(可Plus试用)&&[2FA]" in source
        assert "搜索邮箱、Token、来源；&& 联合，! 排除" in source
        assert "free&&![2FA]" in source
        assert "[提链]&&[2FA]" in source
        for label in (
            "[2FA]", "[无2FA]", "[提链]", "[未提链]", "[接码]", "[Token]",
            "[Codex]", "[Agent]", "[归档]", "[查活正常]", "[查活失败]", "[套餐查询失败]",
            "[oaics]",
        ):
            assert label in source
        assert "!**free" in source


def test_account_search_supports_all_documented_status_aliases():
    cases = [
        ("[2FA]", {"twofa": True}),
        ("[无2FA]", {"twofa": False}),
        ("[提链]", {"link_completed": True}),
        ("[未提链]", {"link_completed": False}),
        ("[接码]", {"sms_completed": True}),
        ("[Token]", {"access_token": "ACCESS_TOKEN"}),
        ("[Codex]", {"codex_status": "success"}),
        ("[Agent]", {"codex_agent_status": "success"}),
        ("[归档]", {"archived": True}),
        ("[查活正常]", {"live_check_ok": True}),
        ("[查活失败]", {"live_check_ok": False}),
        ("[套餐查询失败]", {"plan_check_status": "failed"}),
        ("[oaics]", {"oaics_eligible": True}),
    ]
    for query, options in cases:
        assert _account_matches_query(_row(**options), query), query


def test_plan_update_persists_oaics_qualification(monkeypatch):
    accounts = [_row(oaics_eligible=False)]
    monkeypatch.setattr(db, "_load_accounts", lambda: accounts)
    monkeypatch.setattr(db, "_save_accounts", lambda rows: None)

    assert db.update_account_plan_check(acc_id=1, result={
        "ok": True,
        "checked_at": "2026-08-13T12:00:00",
        "current_plan_type": "free",
        "oaics_check_status": "success",
        "oaics_checked_at": "2026-08-13T12:00:01",
        "oaics_eligible": True,
        "oaics_session_kind": "oaics",
        "oaics_processor_entity": "openai_llc",
    })

    assert accounts[0]["oaics_eligible"] is True
    assert accounts[0]["oaics_session_kind"] == "oaics"
    assert json.loads(accounts[0]["plan_check_result_json"])["oaics_eligible"] is True
