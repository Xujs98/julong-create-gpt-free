from core.oaics_checker import (
    detect_oaics_checkout,
    extract_checkout_session_id,
)


def test_detects_direct_oaics_session():
    result = detect_oaics_checkout({"id": "oaics_demo"}, billing_country="US")
    assert result == {
        "is_oaics": True,
        "session_kind": "oaics",
        "processor_entity": "openai_llc",
    }


def test_detects_stripe_session_nested_in_checkout_url():
    payload = {"url": "https://checkout.example.test/c/pay/cs_demo#fragment"}
    assert extract_checkout_session_id(payload) == "cs_demo"
    assert detect_oaics_checkout(payload, billing_country="DE")["is_oaics"] is False
