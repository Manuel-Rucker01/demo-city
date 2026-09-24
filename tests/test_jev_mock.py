"""MockBackend: canonical shapes, probability normalization, determinism, prior bias, estimation."""

from __future__ import annotations

import asyncio

import pytest

from jevcity.jev import make_backend
from jevcity.types import DecisionRequest, JevConfig


def make_req(**kw) -> DecisionRequest:
    defaults = {
        "request_id": "r1",
        "tick": 0,
        "agent_ids": [1],
        "state": "Help! My payouts have been failing for 3 days.",
        "questions": {
            "urgent": {"type": "noul", "instructions": "..."},
            "team": {"type": "choice", "instructions": "...", "criteria": {"a": "A", "b": "B", "c": "C"}},
            "sev": {"type": "score", "instructions": "...", "criteria": ["low", "mid", "high"]},
        },
    }
    return DecisionRequest(**{**defaults, **kw})


@pytest.fixture
def backend():
    return make_backend(JevConfig(provider="mock"))


async def test_valid_canonical_shapes_and_probabilities_sum_to_one(backend):
    [resp] = await backend.evaluate_many([make_req()])
    assert resp.provider == "mock"
    assert resp.model.endswith("+mock")

    noul = resp.answers["urgent"]
    assert noul["type"] == "noul"
    assert 0.0 <= noul["noul"] <= 1.0

    choice = resp.answers["team"]
    assert choice["type"] == "choice"
    assert choice["choice"] in {"a", "b", "c"}
    assert sum(choice["probabilities"].values()) == pytest.approx(1.0)
    assert 0.0 <= choice["confidence"] <= 1.0

    score = resp.answers["sev"]
    assert score["type"] == "score"
    assert score["legend"] == {"0": "low", "1": "mid", "2": "high"}
    assert sum(score["probabilities"].values()) == pytest.approx(1.0)
    assert 0.0 <= score["confidence"] <= 1.0

    await backend.aclose()


async def test_deterministic_per_request_regardless_of_concurrency(backend):
    req = make_req()
    [r1] = await backend.evaluate_many([req])
    # a fresh backend (fresh RNG state) sees the same cache_key -> identical answer
    backend2 = make_backend(JevConfig(provider="mock"))
    results = await asyncio.gather(*(backend2.evaluate_many([req]) for _ in range(5)))
    for [r2] in results:
        assert r2.answers == r1.answers
    await backend.aclose()
    await backend2.aclose()


async def test_priors_bias_the_distribution_statistically(backend):
    """With mock_priors strongly favoring option 'a', 'a' should win far more than 1/3 of the
    time across many distinct requests (distinct state -> distinct cache_key -> distinct draw)."""
    wins = {"a": 0, "b": 0, "c": 0}
    n = 200
    for i in range(n):
        req = make_req(
            request_id=f"r{i}",
            state=f"state {i}",
            mock_priors={"team": {"a": 0.9, "b": 0.05, "c": 0.05}},
        )
        [resp] = await backend.evaluate_many([req])
        wins[resp.answers["team"]["choice"]] += 1
    await backend.aclose()
    assert wins["a"] > n * 0.6  # far above the 1/3 uniform baseline


async def test_usage_is_estimated(backend):
    [resp] = await backend.evaluate_many([make_req()])
    usage = backend.usage()
    await backend.aclose()
    assert usage.estimated is True
    assert usage.cost_source == "estimated"
    assert resp.usage.cost_usd is None  # mock never reports a provider cost
    assert usage.input_tokens > 0


async def test_mock_uses_mock_as_settings_for_pricing():
    cfg = JevConfig(provider="mock", mock_as="openrouter")
    backend = make_backend(cfg)
    assert backend.settings.price_per_mtok_usd == pytest.approx(0.042)
    await backend.evaluate_many([make_req()])
    usage = backend.usage()
    await backend.aclose()
    assert usage.cost_usd > 0
