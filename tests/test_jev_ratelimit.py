"""RateLimiter: buckets never exceed configured rate under a simulated minute, unlimited (None)
limits never throttle, adaptive reduce/recover, and evaluate_many preserves input order under
concurrency."""

from __future__ import annotations

import httpx
import pytest
import respx

from jevcity.jev import make_backend
from jevcity.jev.ratelimit import RateLimiter
from jevcity.types import DecisionRequest, JevConfig


class SimClock:
    """A clock that only advances when told to (via `advance`); `sleep` fast-forwards it."""

    def __init__(self) -> None:
        self.t = 0.0

    def time(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


async def test_rpm_bucket_never_exceeds_configured_rate_in_a_simulated_minute():
    """A token bucket allows an initial burst up to its capacity (=configured rpm), then
    settles to the configured steady-state rate. Check the steady state, past the burst."""
    clock = SimClock()
    limiter = RateLimiter(rpm_limit=60, tps_limit=None, max_concurrency=100, clock=clock)

    granted_at: list[float] = []
    for _ in range(150):
        async with limiter.acquire(0):
            granted_at.append(clock.time())

    burst, steady = granted_at[:60], granted_at[60:]
    assert burst == [0.0] * 60  # the whole capacity is available immediately

    # Past the burst, no rolling 60s window should grant more than the configured rate
    # (+ a small rounding/re-fill slack).
    for start in steady:
        count = sum(1 for t in steady if start <= t < start + 60.0)
        assert count <= 61


async def test_none_limits_never_throttle():
    clock = SimClock()
    limiter = RateLimiter(rpm_limit=None, tps_limit=None, max_concurrency=1000, clock=clock)
    for _ in range(500):
        async with limiter.acquire(10_000):
            pass
    assert clock.time() == 0.0  # never had to wait


async def test_note_rate_limited_reduces_and_floors():
    clock = SimClock()
    limiter = RateLimiter(rpm_limit=100, tps_limit=1000, max_concurrency=10, clock=clock)
    for _ in range(30):
        limiter.note_rate_limited()
    assert limiter.current_rpm == pytest.approx(10.0)  # floor = 10% of 100
    assert limiter.current_tps == pytest.approx(100.0)  # floor = 10% of 1000


async def test_note_success_recovers_after_a_simulated_minute():
    clock = SimClock()
    limiter = RateLimiter(rpm_limit=100, tps_limit=None, max_concurrency=10, clock=clock)
    limiter.note_rate_limited()
    reduced = limiter.current_rpm
    assert reduced < 100

    clock.t += 60.0
    limiter.note_success()
    assert limiter.current_rpm == pytest.approx(min(100.0, reduced + 5.0))

    # keeps recovering, capped at configured
    for _ in range(50):
        clock.t += 60.0
        limiter.note_success()
    assert limiter.current_rpm == pytest.approx(100.0)


async def test_evaluate_many_preserves_order_under_concurrency(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-test")
    cfg = JevConfig(provider="typesafe", max_concurrency=8)

    def make_response(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        qkey = next(iter(body["questions"]))
        idx = body["state"]
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {qkey: {"type": "noul", "noul": float(idx) / 100.0}},
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        )

    with respx.mock(base_url="https://api.typesafe.ai") as m:
        m.post("/v1/systemone").mock(side_effect=make_response)
        backend = make_backend(cfg)
        reqs = [
            DecisionRequest(
                request_id=f"r{i}",
                tick=0,
                agent_ids=[i],
                state=str(i),
                questions={"q": {"type": "noul", "instructions": "..."}},
            )
            for i in range(20)
        ]
        results = await backend.evaluate_many(reqs)
        await backend.aclose()

    for i, resp in enumerate(results):
        assert resp.answers["q"]["noul"] == pytest.approx(i / 100.0)
