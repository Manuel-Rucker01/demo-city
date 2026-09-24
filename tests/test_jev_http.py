"""HTTP backend: per-provider URL/auth/body shape, decoding, retries, error mapping, budget.

Retry backoff and retry-after waits go through `backend._sleep` (injectable; defaults to real
asyncio.sleep), so these tests replace it with a fast recorder and never actually wait.
"""

from __future__ import annotations

import datetime
import email.utils
import json

import httpx
import pytest
import respx

from jevcity.jev import make_backend
from jevcity.jev.errors import (
    JevAuthError,
    JevBudgetExceeded,
    JevRequestError,
    JevRetryExhausted,
)
from jevcity.types import DecisionRequest, JevConfig


def make_req(**kw) -> DecisionRequest:
    defaults = {
        "request_id": "r1",
        "tick": 0,
        "agent_ids": [1],
        "state": "Help! My payouts have been failing for 3 days.",
        "questions": {"is_urgent": {"type": "noul", "instructions": "Does this convey urgency?"}},
    }
    return DecisionRequest(**{**defaults, **kw})


def _fast_sleep():
    """Records requested delays and returns instantly instead of actually waiting."""
    calls: list[float] = []

    async def sleep(seconds: float) -> None:
        calls.append(seconds)

    sleep.calls = calls  # type: ignore[attr-defined]
    return sleep


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-typesafe-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter-test")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "sk-vercel-test")


ANSWER_200 = {
    "model": "jev-1.13.0",
    "answers": {"is_urgent": {"type": "noul", "noul": 0.9}},
    "usage": {"input_tokens": 10, "output_tokens": 1},
}


# --- per-provider URL / auth / body shape ---------------------------------------------------


async def test_typesafe_request_shape():
    cfg = JevConfig(provider="typesafe")
    with respx.mock(base_url="https://api.typesafe.ai") as m:
        route = m.post("/v1/systemone").mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": "jev-1.13.0",
                    "answers": {"is_urgent": {"type": "noul", "noul": 0.95}},
                    "usage": {"input_tokens": 296, "output_tokens": 20},
                },
            )
        )
        backend = make_backend(cfg)
        [resp] = await backend.evaluate_many([make_req()])
        await backend.aclose()

    assert route.called
    sent = route.calls[0].request
    assert sent.headers["authorization"] == "Bearer sk-typesafe-test"
    assert resp.answers["is_urgent"]["noul"] == 0.95


async def test_openrouter_request_shape_and_cost_reported():
    cfg = JevConfig(provider="openrouter")
    with respx.mock(base_url="https://openrouter.ai/api") as m:
        route = m.post("/v1/systemone").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "gen-dec-1",
                    "model": "typesafe/jev-1.13-20260917",
                    "provider": "TypeSafe",
                    "usage": {"cost": 0.000019992, "input_tokens": 476, "output_tokens": 70},
                    "answers": {"is_urgent": {"type": "noul", "noul": 0.96}},
                },
            )
        )
        backend = make_backend(cfg)
        [resp] = await backend.evaluate_many([make_req()])
        usage = backend.usage()
        await backend.aclose()

    assert route.called
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-openrouter-test"
    assert resp.model == "typesafe/jev-1.13-20260917"
    assert usage.cost_source == "reported"
    assert usage.cost_usd == pytest.approx(0.000019992)


async def test_vercel_typesafe_compat_request_shape():
    cfg = JevConfig(provider="vercel")
    with respx.mock(base_url="https://ai-gateway.vercel.sh") as m:
        route = m.post("/typesafe/v1/systemone").mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": "typesafe-ai/jev",
                    "answers": {"is_urgent": {"type": "noul", "noul": 0.98}},
                    "usage": {"input_tokens": 275, "output_tokens": 20},
                    "provider_metadata": {"gateway": {"cost": "0.00001155", "generationId": "gen_x"}},
                },
            )
        )
        backend = make_backend(cfg)
        [resp] = await backend.evaluate_many([make_req()])
        await backend.aclose()

    assert route.called
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-vercel-test"
    assert resp.meta["generationId"] == "gen_x"


async def test_vercel_evaluate_wire_sends_boolean_never_noul():
    cfg = JevConfig(provider="vercel", wire="vercel_evaluate")
    with respx.mock(base_url="https://ai-gateway.vercel.sh") as m:
        route = m.post("/v1/evaluate").mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": "typesafe-ai/jev",
                    "answers": {"is_urgent": {"type": "boolean", "probability": 0.98}},
                    "usage": {"inputTokens": 275, "outputTokens": 20},
                },
            )
        )
        backend = make_backend(cfg)
        [resp] = await backend.evaluate_many([make_req()])
        await backend.aclose()

    parsed = json.loads(route.calls[0].request.content)
    assert parsed["questions"]["is_urgent"]["type"] == "boolean"
    assert resp.answers["is_urgent"] == {"type": "noul", "noul": 0.98}


# --- errors: non-retryable ------------------------------------------------------------------


async def test_400_raises_jev_request_error_no_retry():
    cfg = JevConfig(provider="typesafe", max_retries=3)
    with respx.mock(base_url="https://api.typesafe.ai") as m:
        route = m.post("/v1/systemone").mock(
            return_value=httpx.Response(400, json={"message": "bad", "error_type": "invalid_request"})
        )
        backend = make_backend(cfg)
        with pytest.raises(JevRequestError):
            await backend.evaluate_many([make_req()])
        await backend.aclose()
    assert route.call_count == 1


async def test_422_raises_jev_request_error_no_retry():
    cfg = JevConfig(provider="typesafe", max_retries=3)
    with respx.mock(base_url="https://api.typesafe.ai") as m:
        route = m.post("/v1/systemone").mock(
            return_value=httpx.Response(422, json={"message": "bad field", "error_type": "invalid_request"})
        )
        backend = make_backend(cfg)
        with pytest.raises(JevRequestError):
            await backend.evaluate_many([make_req()])
        await backend.aclose()
    assert route.call_count == 1


async def test_401_raises_jev_auth_error():
    cfg = JevConfig(provider="typesafe", max_retries=3)
    with respx.mock(base_url="https://api.typesafe.ai") as m:
        m.post("/v1/systemone").mock(
            return_value=httpx.Response(401, json={"message": "Missing Authentication header"})
        )
        backend = make_backend(cfg)
        with pytest.raises(JevAuthError):
            await backend.evaluate_many([make_req()])
        await backend.aclose()


async def test_402_raises_jev_budget_exceeded():
    cfg = JevConfig(provider="openrouter", max_retries=3)
    with respx.mock(base_url="https://openrouter.ai/api") as m:
        m.post("/v1/systemone").mock(
            return_value=httpx.Response(
                402, json={"error": {"code": 402, "message": "Insufficient credits"}}
            )
        )
        backend = make_backend(cfg)
        with pytest.raises(JevBudgetExceeded):
            await backend.evaluate_many([make_req()])
        await backend.aclose()


async def test_413_raises_jev_request_error():
    cfg = JevConfig(provider="typesafe", max_retries=3)
    with respx.mock(base_url="https://api.typesafe.ai") as m:
        m.post("/v1/systemone").mock(
            return_value=httpx.Response(413, json={"message": "payload too large"})
        )
        backend = make_backend(cfg)
        with pytest.raises(JevRequestError):
            await backend.evaluate_many([make_req()])
        await backend.aclose()


async def test_budget_guard_before_sending():
    cfg = JevConfig(provider="typesafe", max_cost_usd=0.0)
    with respx.mock(base_url="https://api.typesafe.ai", assert_all_called=False) as m:
        route = m.post("/v1/systemone").mock(return_value=httpx.Response(500))
        backend = make_backend(cfg)
        with pytest.raises(JevBudgetExceeded):
            await backend.evaluate_many([make_req()])
        await backend.aclose()
    assert route.call_count == 0


# --- retries: 429 / 5xx / 524 / 529, retry-after variants ------------------------------------


async def test_429_then_200_retries_and_honors_retry_after_seconds():
    cfg = JevConfig(provider="typesafe", max_retries=3)
    with respx.mock(base_url="https://api.typesafe.ai") as m:
        route = m.post("/v1/systemone").mock(
            side_effect=[
                httpx.Response(429, headers={"retry-after": "2"}, json={"message": "slow down"}),
                httpx.Response(200, json=ANSWER_200),
            ]
        )
        backend = make_backend(cfg)
        fake_sleep = _fast_sleep()
        backend._sleep = fake_sleep
        configured_rpm = backend._limiter._configured_rpm
        [resp] = await backend.evaluate_many([make_req()])
        usage = backend.usage()
        await backend.aclose()

    assert route.call_count == 2
    assert resp.answers["is_urgent"]["noul"] == 0.9
    assert usage.rate_limited == 1
    assert 2.0 in fake_sleep.calls  # the retry-after wait was honored, not exponential backoff
    assert backend._limiter.current_rpm < configured_rpm  # adaptive reduction on 429


async def test_retry_after_as_http_date():
    cfg = JevConfig(provider="typesafe", max_retries=3)
    future = email.utils.format_datetime(
        datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=3)
    )
    with respx.mock(base_url="https://api.typesafe.ai") as m:
        m.post("/v1/systemone").mock(
            side_effect=[
                httpx.Response(429, headers={"retry-after": future}, json={"message": "slow down"}),
                httpx.Response(200, json=ANSWER_200),
            ]
        )
        backend = make_backend(cfg)
        fake_sleep = _fast_sleep()
        backend._sleep = fake_sleep
        [resp] = await backend.evaluate_many([make_req()])
        await backend.aclose()

    assert resp.answers["is_urgent"]["noul"] == 0.9
    assert any(s > 0 for s in fake_sleep.calls)


async def test_retry_after_ms_header():
    cfg = JevConfig(provider="typesafe", max_retries=3)
    with respx.mock(base_url="https://api.typesafe.ai") as m:
        m.post("/v1/systemone").mock(
            side_effect=[
                httpx.Response(429, headers={"retry-after-ms": "250"}, json={"message": "slow"}),
                httpx.Response(200, json=ANSWER_200),
            ]
        )
        backend = make_backend(cfg)
        fake_sleep = _fast_sleep()
        backend._sleep = fake_sleep
        [resp] = await backend.evaluate_many([make_req()])
        await backend.aclose()

    assert resp.answers["is_urgent"]["noul"] == 0.9
    assert 0.25 in fake_sleep.calls


@pytest.mark.parametrize("status", [524, 529, 500, 502, 503])
async def test_retryable_statuses_are_retried(status):
    cfg = JevConfig(provider="typesafe", max_retries=3)
    with respx.mock(base_url="https://api.typesafe.ai") as m:
        route = m.post("/v1/systemone").mock(
            side_effect=[
                httpx.Response(status, json={"message": "try again"}),
                httpx.Response(200, json=ANSWER_200),
            ]
        )
        backend = make_backend(cfg)
        backend._sleep = _fast_sleep()
        [resp] = await backend.evaluate_many([make_req()])
        await backend.aclose()
    assert route.call_count == 2
    assert resp.answers["is_urgent"]["noul"] == 0.9


async def test_retries_exhausted_raises():
    cfg = JevConfig(provider="typesafe", max_retries=2)
    with respx.mock(base_url="https://api.typesafe.ai") as m:
        route = m.post("/v1/systemone").mock(return_value=httpx.Response(500, json={"message": "down"}))
        backend = make_backend(cfg)
        backend._sleep = _fast_sleep()
        with pytest.raises(JevRetryExhausted) as exc_info:
            await backend.evaluate_many([make_req()])
        await backend.aclose()
    assert route.call_count == 3  # 1 initial + 2 retries
    assert exc_info.value.attempts == 3


async def test_server_rate_limit_headers_are_adopted_and_retries_reacquire(monkeypatch):
    """Vercel sends x-ratelimit-* headers (observed 2026-09-24): limit 30 req/min, reset 32s."""
    import httpx
    import respx

    from jevcity.jev import make_backend
    from jevcity.types import DecisionRequest, JevConfig

    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-key")
    monkeypatch.delenv("JEV_PROVIDER", raising=False)
    cfg = JevConfig(provider="vercel", max_retries=2)
    backend = make_backend(cfg)
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    backend._sleep = fake_sleep  # type: ignore[attr-defined]
    limited = httpx.Response(
        429,
        headers={
            "retry-after": "0",
            "x-ratelimit-limit-requests": "30",
            "x-ratelimit-remaining-requests": "5",
            "x-ratelimit-reset-requests": "32s",
        },
        json={"error": {"message": "busy", "type": "rate_limit_exceeded"}},
    )
    ok = httpx.Response(
        200,
        json={"model": "typesafe-ai/jev", "answers": {"q": {"type": "noul", "noul": 0.9}},
              "usage": {"input_tokens": 10, "output_tokens": 1}},
    )
    req = DecisionRequest(
        request_id="r", tick=1, agent_ids=[1], state="s",
        questions={"q": {"type": "noul", "instructions": "?"}},
    )
    with respx.mock(base_url="https://ai-gateway.vercel.sh") as mock:
        route = mock.post("/typesafe/v1/systemone").mock(side_effect=[limited, ok])
        [resp] = await backend.evaluate_many([req])
    assert route.call_count == 2
    assert resp.answers["q"]["noul"] == 0.9
    assert backend._limiter.current_rpm <= 30 * cfg.rate_safety  # type: ignore[attr-defined]
    await backend.aclose()
