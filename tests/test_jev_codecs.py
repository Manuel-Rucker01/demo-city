"""Codec unit tests using the documented example bodies from docs/jev-reference."""

from __future__ import annotations

import pytest

from jevcity.jev import codecs
from jevcity.types import DecisionRequest

REQ_BODY = {
    "state": "Help! My payouts have been failing for 3 days.",
    "questions": {
        "is_bug": {
            "type": "noul",
            "instructions": "Is the customer reporting a software defect?",
            "criteria": {"true": "broken behavior", "false": "a question or feature request"},
        },
        "team": {
            "type": "choice",
            "instructions": "Which team should own this ticket?",
            "criteria": {"account": "...", "frontend": "...", "payments": "..."},
        },
        "urgency": {
            "type": "score",
            "instructions": "How urgent is this ticket?",
            "criteria": ["Can wait", "Should be fixed this week", "Blocking revenue right now"],
        },
    },
}


def make_req(**kw) -> DecisionRequest:
    return DecisionRequest(request_id="r1", tick=0, agent_ids=[1], **{**REQ_BODY, **kw})


# --- systemone (TypeSafe direct) --------------------------------------------------------------


def test_systemone_encode_is_canonical_body():
    out = codecs.encode("systemone", REQ_BODY, "jev-1.13.0")
    assert out == {"state": REQ_BODY["state"], "model": "jev-1.13.0", "questions": REQ_BODY["questions"]}


def test_systemone_decode_typesafe_plain():
    wire = {
        "model": "jev-1.13.0",
        "answers": {"is_bug": {"type": "noul", "noul": 0.96}},
        "usage": {"input_tokens": 296, "output_tokens": 20},
    }
    resp = codecs.decode("systemone", wire, make_req(), "jev-1.13.0")
    assert resp.model == "jev-1.13.0"
    assert resp.answers["is_bug"]["noul"] == 0.96
    assert resp.usage.input_tokens == 296
    assert resp.usage.cost_usd is None
    assert resp.meta == {}


def test_systemone_decode_openrouter_extras():
    wire = {
        "id": "gen-dec-1789738314-X5e5eKGQdvR9rblyX250",
        "model": "typesafe/jev-1.13-20260917",
        "provider": "TypeSafe",
        "usage": {"cost": 0.000019992, "input_tokens": 476, "output_tokens": 70},
        "answers": {
            "is_bug": {"type": "noul", "noul": 0.96},
            "team": {
                "type": "choice",
                "choice": "payments",
                "confidence": 0.75,
                "probabilities": {"account": 0, "frontend": 0.16, "payments": 0.84},
            },
            "urgency": {
                "type": "score",
                "score": 1.99,
                "legend": {"0": "Can wait", "1": "Should be fixed this week", "2": "Blocking revenue right now"},
                "probabilities": {"0": 0, "1": 0.01, "2": 0.99},
                "confidence": 0.99,
            },
        },
    }
    resp = codecs.decode("systemone", wire, make_req(), "typesafe/jev-1.13")
    assert resp.usage.cost_usd == 0.000019992
    assert resp.meta == {"id": "gen-dec-1789738314-X5e5eKGQdvR9rblyX250", "provider": "TypeSafe"}
    assert resp.model == "typesafe/jev-1.13-20260917"


def test_systemone_decode_vercel_typesafe_compat_extras():
    wire = {
        "model": "typesafe-ai/jev",
        "answers": {"refund": {"type": "noul", "noul": 0.98}},
        "usage": {"input_tokens": 275, "output_tokens": 20},
        "provider_metadata": {
            "gateway": {
                "routing": {"originalModelId": "typesafe-ai/jev", "resolvedProvider": "typesafe-ai"},
                "cost": "0.00001155",
                "generationId": "gen_abc",
            }
        },
    }
    req = make_req(questions={"refund": {"type": "noul", "instructions": "..."}})
    resp = codecs.decode("systemone", wire, req, "typesafe-ai/jev")
    assert resp.usage.cost_usd == 0.00001155
    assert resp.meta["generationId"] == "gen_abc"
    assert resp.meta["routing"]["resolvedProvider"] == "typesafe-ai"


def test_openrouter_decisions_codec_is_alias_of_systemone():
    assert codecs.encode_openrouter_decisions is codecs.encode_systemone
    assert codecs.decode_openrouter_decisions is codecs.decode_systemone


# --- vercel_evaluate -----------------------------------------------------------------------


def test_vercel_evaluate_encode_sends_boolean_never_noul():
    out = codecs.encode("vercel_evaluate", REQ_BODY, "typesafe-ai/jev")
    assert out["questions"]["is_bug"]["type"] == "boolean"
    assert out["questions"]["team"]["type"] == "choice"
    assert out["questions"]["urgency"]["type"] == "score"
    assert "noul" not in [q["type"] for q in out["questions"].values()]


def test_vercel_evaluate_decode_boolean_to_noul():
    wire = {
        "model": "typesafe-ai/jev",
        "answers": {"is_bug": {"type": "boolean", "probability": 0.96}},
        "usage": {"inputTokens": 275, "outputTokens": 20},
    }
    req = make_req(questions={"is_bug": REQ_BODY["questions"]["is_bug"]})
    resp = codecs.decode("vercel_evaluate", wire, req, "typesafe-ai/jev")
    assert resp.answers["is_bug"] == {"type": "noul", "noul": 0.96}
    assert resp.usage.input_tokens == 275
    assert resp.usage.output_tokens == 20


def test_vercel_evaluate_decode_missing_confidence_is_computed_and_flagged():
    wire = {
        "model": "typesafe-ai/jev",
        "answers": {
            "team": {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 1, "shipping": 0, "technical": 0},
            }
        },
        "usage": {"inputTokens": 100, "outputTokens": 10},
    }
    req = make_req(questions={"team": REQ_BODY["questions"]["team"]})
    resp = codecs.decode("vercel_evaluate", wire, req, "typesafe-ai/jev")
    ans = resp.answers["team"]
    assert ans["confidence_source"] == "computed"
    # a one-hot distribution is maximally confident
    assert ans["confidence"] == pytest.approx(1.0)


def test_vercel_evaluate_decode_missing_legend_rebuilt_from_request_criteria():
    wire = {
        "model": "typesafe-ai/jev",
        "answers": {
            "urgency": {
                "type": "score",
                "score": 2.97,
                "probabilities": {"0": 0, "1": 0, "2": 0.02, "3": 0.98},
            }
        },
        "usage": {"inputTokens": 100, "outputTokens": 10},
    }
    req = make_req(
        questions={
            "urgency": {
                "type": "score",
                "instructions": "...",
                "criteria": ["poor", "fair", "good", "excellent"],
            }
        }
    )
    resp = codecs.decode("vercel_evaluate", wire, req, "typesafe-ai/jev")
    assert resp.answers["urgency"]["legend"] == {
        "0": "poor",
        "1": "fair",
        "2": "good",
        "3": "excellent",
    }


def test_vercel_evaluate_decode_gateway_cost_and_meta():
    wire = {
        "model": "typesafe-ai/jev",
        "answers": {"refund": {"type": "boolean", "probability": 0.98}},
        "usage": {"inputTokens": 275, "outputTokens": 20},
        "providerMetadata": {
            "gateway": {
                "routing": {"originalModelId": "typesafe-ai/jev"},
                "cost": "0.00001155",
                "generationId": "gen_...",
            }
        },
    }
    req = make_req(questions={"refund": {"type": "noul", "instructions": "..."}})
    resp = codecs.decode("vercel_evaluate", wire, req, "typesafe-ai/jev")
    assert resp.usage.cost_usd == 0.00001155
    assert resp.meta["generationId"] == "gen_..."


# --- error bodies --------------------------------------------------------------------------


def test_parse_error_typesafe_shape():
    body = {"message": "questions.refund.type: expected one of 'noul', 'choice', 'score'", "error_type": "invalid_request"}
    msg = codecs.parse_error_message(422, body)
    assert "expected one of" in msg
    assert "invalid_request" in msg


def test_parse_error_openrouter_shape():
    body = {"error": {"code": 402, "message": "Insufficient credits. Add more using https://openrouter.ai/credits"}}
    msg = codecs.parse_error_message(402, body)
    assert "Insufficient credits" in msg
    assert "402" in msg


def test_parse_error_vercel_gateway_shape():
    body = {"error": {"message": "Rate limit exceeded", "type": "rate_limit_exceeded"}}
    msg = codecs.parse_error_message(429, body)
    assert "Rate limit exceeded" in msg
    assert "rate_limit_exceeded" in msg
