from unittest.mock import MagicMock

from core.oaics_checker import (
    CountryQualificationError,
    check_oaics_protocol,
    detect_oaics_checkout,
    extract_checkout_session_id,
    parse_oaics_protocol_response,
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


def test_parses_country_results_from_oaics_protocol():
    result = parse_oaics_protocol_response({
        "query_count": 7,
        "results": [
            {"country": "JP", "country_name": "日本", "status": "eligible", "eligible": True, "message": "有资格"},
            {"country": "PH", "country_name": "菲律宾", "status": "not_eligible", "eligible": False, "message": "无资格"},
        ],
    })
    assert result["oaics_eligible"] is True
    assert result["oaics_query_count"] == 7
    assert result["oaics_country_results"][1]["status"] == "not_eligible"


def test_check_oaics_protocol_posts_token_and_turnstile_header():
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "results": [{"country": "JP", "country_name": "日本", "status": "eligible", "eligible": True, "message": "ok"}]
    }
    session = MagicMock()
    session.post.return_value = response

    result = check_oaics_protocol(session, "TOKEN", turnstile_token="TURNSTILE")

    assert result["oaics_eligible"] is True
    kwargs = session.post.call_args.kwargs
    assert kwargs["json"] == {"access_token": "TOKEN"}
    assert kwargs["headers"]["X-Turnstile-Token"] == "TURNSTILE"


def test_country_qualification_403_exposes_turnstile_requirement():
    response = MagicMock(status_code=403)
    response.json.return_value = {"detail": "安全验证失败，请重试"}
    session = MagicMock()
    session.post.return_value = response

    try:
        from core.oaics_checker import check_country_qualification_protocol
        check_country_qualification_protocol(session, "TOKEN")
    except CountryQualificationError as exc:
        assert exc.status_code == 403
        assert exc.requires_turnstile is True
        assert "Turnstile" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected CountryQualificationError")
