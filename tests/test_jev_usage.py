"""Usage math (cost computation, models_seen) and the model-change warning."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from jevcity.jev import make_backend
from jevcity.jev.meter import computed_cost_usd
from jevcity.types import DecisionRequest, JevConfig


def make_req(**kw) -> DecisionRequest:
    defaults = {
        "request_id": "r1",
        "tick": 0,
        "agent_ids": [1],
        "state": "s",
        "questions": {"q": {"type": "noul", "instructions": "..."}},
    }
    return DecisionRequest(**{**defaults, **kw})


def test_computed_cost_1m_tokens_at_042_per_mtok():
    assert computed_cost_usd(1_000_000, 0.042) == pytest.approx(0.042)


def test_computed_cost_none_price_is_zero():
    assert computed_cost_usd(1_000_000, None) == 0.0


async def test_models_seen_counts(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-test")
    cfg = JevConfig(provider="typesafe")
    with respx.mock(base_url="https://api.typesafe.ai") as m:
        m.post("/v1/systemone").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "model": "jev-1.13.0",
                        "answers": {"q": {"type": "noul", "noul": 0.5}},
                        "usage": {"input_tokens": 10, "output_tokens": 1},
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "model": "jev-1.13.0",
                        "answers": {"q": {"type": "noul", "noul": 0.5}},
                        "usage": {"input_tokens": 10, "output_tokens": 1},
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "model": "jev-1.13.1",
                        "answers": {"q": {"type": "noul", "noul": 0.5}},
                        "usage": {"input_tokens": 10, "output_tokens": 1},
                    },
                ),
            ]
        )
        backend = make_backend(cfg)
        await backend.evaluate_many([make_req(request_id=f"r{i}", state=str(i)) for i in range(3)])
        usage = backend.usage()
        await backend.aclose()

    assert usage.models_seen == {"jev-1.13.0": 2, "jev-1.13.1": 1}
    assert usage.requests == 3


async def test_model_change_warning_logged(monkeypatch, caplog):
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-test")
    cfg = JevConfig(provider="typesafe", max_concurrency=1)  # serialize so order is deterministic
    with respx.mock(base_url="https://api.typesafe.ai") as m:
        m.post("/v1/systemone").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "model": "jev-1.13.0",
                        "answers": {"q": {"type": "noul", "noul": 0.5}},
                        "usage": {"input_tokens": 10, "output_tokens": 1},
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "model": "jev-1.13.1",
                        "answers": {"q": {"type": "noul", "noul": 0.5}},
                        "usage": {"input_tokens": 10, "output_tokens": 1},
                    },
                ),
            ]
        )
        backend = make_backend(cfg)
        with caplog.at_level(logging.WARNING, logger="jevcity.jev.http"):
            await backend.evaluate_many([make_req(request_id="a", state="a")])
            await backend.evaluate_many([make_req(request_id="b", state="b")])
        await backend.aclose()

    assert any("resolved model changed" in r.message for r in caplog.records)
