from types import SimpleNamespace
from unittest.mock import patch

from core import chatgpt_plan
from core import qualification_test


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


class _TextResponse(_Response):
    def __init__(self, payload, text, status_code=200):
        super().__init__(payload, status_code=status_code)
        self.text = text


def _env(post):
    return SimpleNamespace(
        device_id="device-1",
        proxy="http://proxy.example:8080",
        get_chatgpt_headers=lambda **_kwargs: {"x-test": "1"},
        navigator_language=lambda: "en-US",
        post=post,
        get=lambda *_args, **_kwargs: _Response({
            "custom_payment_methods": [{"id": "cpmt_1TOgstC6h1nxGoI3WUVEY2cJ", "type": "custom"}],
        }),
    )


def test_custom_checkout_detects_known_gcash_method():
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return _Response({"id": "oaics_demo", "processor_entity": "openai_ie"})

    env = _env(post)
    with patch("core.qualification_test.request_sentinel_token", return_value={"token": "challenge"}), patch(
        "core.qualification_test.build_sentinel_header", return_value=("sentinel", "so")
    ):
        result = qualification_test.check_payment_channel(
            env, "TOKEN", {"channel": "gcash", "country": "PH", "currency": "PHP"}, timeout=5
        )

    assert result["status"] == "eligible"
    assert result["eligible"] is True
    assert result["checkout_session_id"] == "oaics_demo"
    assert result["checkout_provider"] == "open_ai"
    assert calls[0][1]["json"]["billing_details"] == {"country": "PH", "currency": "PHP"}
    assert calls[0][1]["headers"]["openai-sentinel-token"] == "sentinel"
    assert calls[0][1]["headers"]["openai-sentinel-so-token"] == "so"


def test_stripe_checkout_detects_paypal_and_returns_amount():
    def post(url, **kwargs):
        if url.endswith("/payments/checkout"):
            return _Response({"id": "cs_live_demo", "publishable_key": "pk_live_demo", "processor_entity": "openai_ie"})
        return _Response({
            "currency": "GBP",
            "total_summary": {"due": 1999},
            "payment_method_types": [{"type": "paypal"}, {"type": "card"}],
        })

    env = _env(post)
    with patch("core.qualification_test.request_sentinel_token", return_value={"token": "challenge"}), patch(
        "core.qualification_test.build_sentinel_header", return_value=("sentinel", None)
    ):
        result = qualification_test.check_payment_channel(
            env, "TOKEN", {"channel": "paypal", "country": "GB", "currency": "GBP"}, timeout=5
        )

    assert result["status"] == "eligible"
    assert result["processor_entity"] == "openai_ie"
    assert result["checkout_amount"] == 1999
    assert "paypal" in result["available_channels"]


def test_checkout_session_id_accepts_opaque_stripe_prefix_from_body():
    """Stripe can return a cs_* session in a redirect/body field without an id key."""
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return _TextResponse({}, 'redirect=https://checkout.stripe.com/cs_live_opaque-session-1')

    env = _env(post)
    with patch("core.qualification_test.request_sentinel_token", return_value={"token": "challenge"}), patch(
        "core.qualification_test.build_sentinel_header", return_value=("sentinel", None)
    ):
        sid, meta = qualification_test._create_checkout(
            env, "TOKEN", {"channel": "paypal", "country": "GB", "currency": "GBP"}, 5
        )

    assert sid == "cs_live_opaque-session-1"
    assert meta["processor_entity"] == "openai_ie"
    assert calls == [qualification_test.CHECKOUT_URL]


def test_headers_accept_navigator_language_string():
    env = SimpleNamespace(
        device_id="device-1",
        navigator_language="zh-CN",
        get_chatgpt_headers=lambda **_kwargs: {},
    )
    assert qualification_test._headers(env, "TOKEN")["OAI-Language"] == "zh-CN"


def test_query_country_qualification_keeps_partial_results():
    presets = [
        {"name": "英国·PayPal", "channel": "paypal", "country": "GB", "currency": "GBP"},
        {"name": "荷兰·iDEAL", "channel": "ideal", "country": "NL", "currency": "EUR"},
    ]
    with patch(
        "core.qualification_test.check_payment_channel",
        side_effect=[
            {"country": "GB", "country_name": "英国", "channel": "paypal", "status": "eligible", "eligible": True},
            RuntimeError("temporary checkout failure"),
        ],
    ):
        result = qualification_test.query_country_qualification(SimpleNamespace(), "TOKEN", presets=presets)

    assert result["country_qualification_status"] == "success"
    assert result["country_qualification_eligible"] is True
    assert result["country_qualification_query_count"] == 2
    assert result["country_qualification_source"] == "qualification-test"
    assert result["country_qualification_results"][1]["status"] == "failed"


def test_plan_country_check_uses_checkout_engine_and_ignores_legacy_turnstile():
    expected = {
        "country_qualification_results": [],
        "country_qualification_eligible": False,
        "country_qualification_query_count": 10,
        "country_qualification_status": "success",
        "country_qualification_source": "qualification-test",
    }
    env = SimpleNamespace()
    with patch("core.qualification_test.query_country_qualification", return_value=expected) as query:
        result = chatgpt_plan._check_country_qualification(
            env, "TOKEN", timeout=8, turnstile_token="legacy-token"
        )

    query.assert_called_once_with(env, "TOKEN", timeout=15.0)
    assert result["country_qualification_source"] == "qualification-test"
    assert result["country_qualification_requires_turnstile"] is False
    assert result["country_qualification_turnstile_ignored"] is True
