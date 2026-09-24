"""Canonical JSON and the provider/model-independent cache key.

`cache_key(req)` identifies a Jev call by exactly the part of the request that determines
the answer: `{state, questions}`. `request_id`, `tick`, `agent_ids`, `mock_priors`, the chosen
`model`, and the provider are never part of it, so a mock run and a real run of the same
states hash to the same key and can be compared or replayed against each other.

`request_body` builds the actual canonical TypeSafe-shaped body that gets sent on the wire
(adds `model`); this is what `CallRecord.request` stores.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from jevcity.types import DecisionRequest


def canonical_json(obj: Any) -> str:
    """Canonical JSON: sorted keys, no extra whitespace, no ASCII escaping."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def cache_key_body(req: DecisionRequest) -> dict[str, Any]:
    """The provider/model-independent part of a request: exactly `{state, questions}`."""
    return {"state": req.state, "questions": req.questions}


def cache_key(req: DecisionRequest) -> str:
    """sha256 hex digest of the canonical JSON of `{state, questions}`."""
    return hashlib.sha256(canonical_json(cache_key_body(req)).encode("utf-8")).hexdigest()


def request_body(req: DecisionRequest, model: str) -> dict[str, Any]:
    """Canonical TypeSafe-shaped body actually POSTed: `{state, model, questions}`."""
    return {"state": req.state, "model": model, "questions": req.questions}
