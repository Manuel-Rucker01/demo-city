"""Decode a real Vercel TypeSafe-compat response captured on 2026-09-24 (trimmed routing)."""

from jevcity.jev.codecs import decode
from jevcity.types import DecisionRequest

OBSERVED = {
    "model": "typesafe-ai/jev",
    "answers": {"is_urgent": {"type": "noul", "noul": 0.95}},
    "usage": {"input_tokens": 283, "output_tokens": 23},
    "provider_metadata": {
        "typesafe": {"confidence": {}},
        "gateway": {
            "routing": {"canonicalSlug": "typesafe-ai/jev", "finalProvider": "typesafe-ai"},
            "cost": "0",
            "marketCost": "0.000011886",
            "surchargeCost": "0",
            "gatewayCost": "0",
            "generationId": "gen_01M39WQHBNJTW0P49SXNYEPBTC",
        },
    },
}
REQUEST = {
    "model": "typesafe-ai/jev",
    "state": "Help! My payouts have been failing for 3 days.",
    "questions": {"is_urgent": {"type": "noul", "instructions": "Does this convey urgency?"}},
}


def test_observed_free_credit_response():
    req = DecisionRequest(
        request_id="smoke", tick=0, agent_ids=[], state=REQUEST["state"],
        questions=REQUEST["questions"],
    )
    resp = decode("systemone", OBSERVED, req, "typesafe-ai/jev")
    assert resp.answers["is_urgent"] == {"type": "noul", "noul": 0.95}
    assert resp.model == "typesafe-ai/jev"
    assert resp.usage.input_tokens == 283
    assert resp.usage.cost_usd == 0.0  # billed: free credits
    assert abs(resp.meta["market_cost_usd"] - 283 * 0.042e-6) < 1e-12
    assert resp.meta["generationId"] == "gen_01M39WQHBNJTW0P49SXNYEPBTC"
    assert resp.meta["provider_metadata"] == {"typesafe": {"confidence": {}}}
