from pathlib import Path

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


def test_account_search_inputs_explain_formula_syntax():
    root = Path(__file__).parents[1]
    for relative in ("webui/templates/index.html", "webui/templates/index_legacy.html"):
        source = (root / relative).read_text(encoding="utf-8")
        assert "free(可Plus试用)&&[2FA]" in source
        assert "&& 组合，! 排除" in source
