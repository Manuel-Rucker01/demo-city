"""MOCK backend: no network, no API key. Weighted-random answers in the same canonical shape
as a real TypeSafe response, deterministic per request (same `cache_key` -> same answer,
regardless of concurrency or call order).

Reports usage/cost as if the `mock_as` provider had answered (its price_per_mtok_usd and
model string), but never throttles -- there's no rate limit to respect against nothing.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import numpy as np

from jevcity.types import (
    CallRecord,
    CallSink,
    DecisionRequest,
    JevConfig,
    JevResponse,
    JevUsage,
    ProviderSettings,
    Usage,
)

from .cachekey import cache_key, request_body
from .codecs import peak_confidence
from .meter import UsageMeter, computed_cost_usd, estimate_tokens

_CONCENTRATION = 8.0  # higher = samples cluster tighter around the prior weights


def _normalized_weights(prior: dict[str, float], keys: Sequence[str]) -> np.ndarray:
    if not prior:
        w = np.ones(len(keys))
    else:
        w = np.array([max(prior.get(k, 0.0), 0.0) for k in keys], dtype=float)
        if w.sum() <= 0:
            w = np.ones(len(keys))
    return w / w.sum()


def _sample(rng: np.random.Generator, weights: np.ndarray) -> np.ndarray:
    alpha = np.clip(weights * _CONCENTRATION, 1e-3, None)
    return rng.dirichlet(alpha)


def _confidence(p: np.ndarray) -> float:
    return peak_confidence([float(x) for x in p])


def _answer_choice(rng: np.random.Generator, q: dict, prior: dict[str, float]) -> dict:
    options = list(q["criteria"].keys())
    p = _sample(rng, _normalized_weights(prior, options))
    choice = options[int(np.argmax(p))]
    return {
        "type": "choice",
        "choice": choice,
        "probabilities": {opt: float(pi) for opt, pi in zip(options, p)},
        "confidence": _confidence(p),
    }


def _answer_score(rng: np.random.Generator, q: dict, prior: dict[str, float]) -> dict:
    levels = list(q["criteria"])
    keys = [str(i) for i in range(len(levels))]
    p = _sample(rng, _normalized_weights(prior, keys))
    score = float(sum(i * pi for i, pi in enumerate(p)))
    return {
        "type": "score",
        "score": score,
        "legend": {str(i): str(levels[i]) for i in range(len(levels))},
        "probabilities": {k: float(pi) for k, pi in zip(keys, p)},
        "confidence": _confidence(p),
    }


def _answer_noul(rng: np.random.Generator, q: dict, prior: dict[str, float]) -> dict:
    keys = ["true", "false"]
    p = _sample(rng, _normalized_weights(prior, keys))
    return {"type": "noul", "noul": float(p[0])}


_ANSWERERS = {"choice": _answer_choice, "score": _answer_score, "noul": _answer_noul}


class MockBackend:
    """Weighted-random answers, deterministic per request via a cache-key-derived seed."""

    provider = "mock"

    def __init__(
        self,
        settings: ProviderSettings,
        cfg: JevConfig,
        sink: CallSink | None = None,
    ) -> None:
        self.settings = settings
        self._cfg = cfg
        self._sink = sink
        self._meter = UsageMeter()
        self._model = f"{settings.default_model}+mock"

    async def evaluate_many(self, reqs: Sequence[DecisionRequest]) -> list[JevResponse]:
        return [self._evaluate_one(req) for req in reqs]

    def _evaluate_one(self, req: DecisionRequest) -> JevResponse:
        t0 = time.monotonic()
        body = request_body(req, self._model)
        key = cache_key(req)
        rng = np.random.default_rng(int(key[:16], 16))

        answers: dict[str, dict] = {}
        for qkey, q in req.questions.items():
            prior = req.mock_priors.get(qkey, {})
            answerer = _ANSWERERS[q["type"]]
            answers[qkey] = answerer(rng, q, prior)

        input_tokens = estimate_tokens(body)
        response = JevResponse(
            model=self._model,
            answers=answers,
            usage=JevUsage(input_tokens=input_tokens, output_tokens=0, cost_usd=None),
            provider="mock",
        )

        usage = Usage(
            requests=1,
            input_tokens=input_tokens,
            output_tokens=0,
            cost_usd=computed_cost_usd(input_tokens, self.settings.price_per_mtok_usd),
            cost_source="estimated",
            estimated=True,
            models_seen={self._model: 1},
        )
        self._meter.record(usage)

        if self._sink is not None:
            self._sink.write_call(
                CallRecord(
                    tick=req.tick,
                    request_id=req.request_id,
                    cache_key=key,
                    provider="mock",
                    requested_model=self._model,
                    resolved_model=response.model,
                    request=body,
                    wire_body=None,
                    response=response,
                    latency_ms=(time.monotonic() - t0) * 1000.0,
                    attempts=1,
                )
            )
        return response

    def usage(self) -> Usage:
        return self._meter.snapshot()

    async def aclose(self) -> None:
        return None
