from pathlib import Path

import pytest

from webui.app import _compact_account_for_list


ROOT = Path(__file__).parents[1]
TEMPLATES = (
    ROOT / "webui" / "templates" / "index.html",
)


@pytest.mark.parametrize("template", TEMPLATES)
def test_paid_plan_summary_keeps_discount_out_of_visible_label(template):
    source = template.read_text(encoding="utf-8")
    plan_cell = source.split("function _planCell(r) {", 1)[1].split("function _planAction", 1)[0]

    assert "if (billing) parts.push(billing);" in plan_cell
    assert "if (r.billing_currency) parts.push(r.billing_currency);" in plan_cell
    assert "if (expire) parts.push(`到期 ${expire}`);" in plan_cell
    assert "if (discount) parts.push(discount);" not in plan_cell
    assert "data-plan-hover" in plan_cell
    assert "plan-summary-pill" in plan_cell


@pytest.mark.parametrize("template", TEMPLATES)
def test_plan_details_use_body_portal_with_motion_and_viewport_positioning(template):
    source = template.read_text(encoding="utf-8")

    assert "card.id = 'planHoverCard';" in source
    assert "document.body.appendChild(card);" in source
    assert "getBoundingClientRect()" in source
    assert "window.innerHeight" in source
    assert "data-placement" in source
    assert "translateY(8px) scale(.96)" in source
    assert "cubic-bezier(.2,.8,.2,1)" in source
    assert "prefers-reduced-motion" in source
    assert "_planHoverItem('折扣'" in source
    assert "_planHoverItem('自动续费'" in source
    assert "_planHoverItem('网络路径'" in source


def test_account_list_keeps_complete_paid_plan_hover_fields():
    row = {
        "id": 4,
        "email": "person@example.com",
        "current_plan_type": "plus",
        "subscription_plan": "chatgptplusplan",
        "has_active_subscription": True,
        "is_delinquent": False,
        "plan_cancels_at": "2026-09-08T00:00:00Z",
        "last_will_renew": False,
        "discount_duration_num_periods": 1,
        "discount_cancellation_policy": "retain_until_period_end",
        "last_purchase_origin_platform": "web",
        "plan_check_network_route": "proxy",
    }

    compact = _compact_account_for_list(row)

    for key, value in row.items():
        if key not in {"id", "email"}:
            assert compact[key] == value


@pytest.mark.parametrize("template", TEMPLATES)
def test_plan_cell_displays_oaics_badge(template):
    source = template.read_text(encoding="utf-8")
    assert "[oaics]" in source
    assert "r.oaics_eligible === true" in source


@pytest.mark.parametrize("template", TEMPLATES)
def test_oaics_result_is_rendered_below_eligible_free_plan(template):
    source = template.read_text(encoding="utf-8")
    assert "function _oaicsResultCell(r)" in source
    assert "OAICS：有资格 [oaics]" in source
    assert "OAICS：无资格" in source
    assert "${_oaicsResultCell(r)}" in source


@pytest.mark.parametrize("template", TEMPLATES)
def test_oaics_bulk_control_uses_dedicated_endpoint(template):
    source = template.read_text(encoding="utf-8")
    assert "checkSelectedOaics" in source
    assert "/api/accounts/check-oaics-bulk" in source
    assert "查询OAICS" in source
