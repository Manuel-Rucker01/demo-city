"""Per-`WireFormat` request/response codecs.

Inside jevcity everything is TypeSafe-shaped: `noul`/`choice`/`score` questions and answers,
`confidence`, `legend`, `usage.input_tokens`/`usage.output_tokens`. Each wire format gets a pair
of pure functions:

    encode(req_body, model) -> wire body    # req_body is {"state":..., "questions":...}
    decode(wire_json, req, model_requested) -> JevResponse   # always canonical TypeSafe shape

Nothing outside this module (and http.py, which only calls these) knows the wire format.

TODOs carried over from config/providers.yaml (undocumented behaviour, verify before real use):
  - typesafe: latency is undocumented; the real tokenizer is undocumented (mock estimates
    len(canonical_json)/4 as a rule of thumb, never used for a real call).
  - openrouter: no Jev-specific rate limit is documented, so config assumes TypeSafe's upstream
    limits; a 429 may originate at OpenRouter or be passed through from TypeSafe. The alpha
    `/api/alpha/decisions` endpoint (wire `openrouter_decisions`) shares the `systemone` schema
    per its OpenAPI spec, so its codec just aliases the `systemone` codec.
  - vercel: the exact Jev version behind `typesafe-ai/jev` is not documented (routing/
    generationId are logged in `meta` instead of trusted as a version); no gateway-side rate
    limit is documented for the paid tier (upstream TypeSafe limits assumed); context length is
    not documented for the gateway (config uses a conservative 32k); the native `/v1/evaluate`
    examples in the docs show no `confidence` or `legend` on choice/score answers, so this codec
    computes confidence locally (peak formula from docs.typesafe.ai/confidence, flagged
    `confidence_source: "computed"`) and rebuilds `legend` from the request's `score` criteria
    when the response omits it.
"""

from __future__ import annotations

from typing import Any

from jevcity.types import DecisionRequest, JevResponse, JevUsage

# --- shared helpers --------------------------------------------------------------------------


def peak_confidence(probs: list[float]) -> float:
    """(n * max(p) - 1) / (n - 1), clipped to [0, 1]: 0 for a uniform distribution, 1 for a
    certain one. This is the Choice formula used by the ConfidenceExplorer widget on
    docs.typesafe.ai/confidence; the prose there only says confidence is "derived from the
    probabilities", so treat it as TypeSafe's documented example, not a guaranteed spec
    (for Score it is unconfirmed). Used to fill confidence when a wire format omits it
    (vercel_evaluate) and by the mock backend."""
    n = len(probs)
    if n <= 1:
        return 1.0
    return max(0.0, min(1.0, (n * max(probs) - 1) / (n - 1)))


def _absorb_gateway(gateway: dict, meta: dict[str, Any]) -> float | None:
    """Vercel gateway metadata -> meta; returns the billed cost.

    Observed in a real call (2026-09-24): on free credits `cost` is "0" while `marketCost`
    carries the list price (input_tokens x $0.042/M). We keep `cost` as what was billed and
    store `marketCost` as meta["market_cost_usd"] for reporting.
    """
    for key in ("routing", "generationId"):
        if key in gateway:
            meta[key] = gateway[key]
    market = _parse_gateway_cost(gateway.get("marketCost"))
    if market is not None:
        meta["market_cost_usd"] = market
    return _parse_gateway_cost(gateway.get("cost"))


def _parse_gateway_cost(value: Any) -> float | None:
    """Vercel reports gateway cost as a decimal string, e.g. "0.00001155"."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- systemone (TypeSafe direct; also OpenRouter's /v1/systemone and Vercel's TypeSafe-compat
# /typesafe/v1/systemone -- same request/response schema, only extra fields differ) -----------


def encode_systemone(req_body: dict[str, Any], model: str) -> dict[str, Any]:
    return {"state": req_body["state"], "model": model, "questions": req_body["questions"]}


def decode_systemone(
    wire: dict[str, Any], req: DecisionRequest, model_requested: str
) -> JevResponse:
    usage_raw = wire.get("usage", {})
    input_tokens = int(usage_raw.get("input_tokens", 0))
    output_tokens = int(usage_raw.get("output_tokens", 0))

    cost_usd: float | None = None
    meta: dict[str, Any] = {}

    # OpenRouter extras: top-level id/provider, usage.cost.
    if "cost" in usage_raw:
        cost_usd = _parse_gateway_cost(usage_raw.get("cost"))
    if "id" in wire:
        meta["id"] = wire["id"]
    if "provider" in wire:
        meta["provider"] = wire["provider"]

    # Vercel TypeSafe-compat extras: provider_metadata.gateway {routing, cost, generationId}.
    gateway = wire.get("provider_metadata", {}).get("gateway") if "provider_metadata" in wire else None
    if gateway:
        billed = _absorb_gateway(gateway, meta)
        if cost_usd is None:
            cost_usd = billed
    # Undocumented extras seen in real responses (e.g. provider_metadata.typesafe) are kept
    # verbatim without interpretation.
    extra_pm = {k: v for k, v in (wire.get("provider_metadata") or {}).items() if k != "gateway"}
    if extra_pm:
        meta["provider_metadata"] = extra_pm

    return JevResponse(
        model=wire["model"],
        answers=wire["answers"],
        usage=JevUsage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd),
        meta=meta,
    )


# openrouter_decisions (/api/alpha/decisions): same schema per the OpenAPI spec, own name so
# JevConfig.wire can select it explicitly.
encode_openrouter_decisions = encode_systemone
decode_openrouter_decisions = decode_systemone


# --- vercel_evaluate (native /v1/evaluate) -----------------------------------------------------


def encode_vercel_evaluate(req_body: dict[str, Any], model: str) -> dict[str, Any]:
    questions: dict[str, Any] = {}
    for key, q in req_body["questions"].items():
        q = dict(q)
        if q.get("type") == "noul":
            q = {**q, "type": "boolean"}  # criteria true/false kept as-is
        questions[key] = q
    return {"state": req_body["state"], "model": model, "questions": questions}


def _rebuild_score_legend(req: DecisionRequest, qkey: str, n_levels: int) -> dict[str, str]:
    q = req.questions.get(qkey, {})
    criteria = q.get("criteria") or []
    legend = {}
    for i in range(n_levels):
        legend[str(i)] = str(criteria[i]) if i < len(criteria) else str(i)
    return legend


def decode_vercel_evaluate(
    wire: dict[str, Any], req: DecisionRequest, model_requested: str
) -> JevResponse:
    answers: dict[str, dict[str, Any]] = {}
    for qkey, ans in wire.get("answers", {}).items():
        ans = dict(ans)
        atype = ans.get("type")
        if atype == "boolean":
            ans = {"type": "noul", "noul": ans.get("probability")}
        elif atype in ("choice", "score"):
            if "confidence" not in ans:
                probs = list(ans.get("probabilities", {}).values())
                ans["confidence"] = peak_confidence([float(p) for p in probs])
                ans["confidence_source"] = "computed"
            if atype == "score" and "legend" not in ans:
                ans["legend"] = _rebuild_score_legend(
                    req, qkey, len(ans.get("probabilities", {}))
                )
        answers[qkey] = ans

    usage_raw = wire.get("usage", {})
    input_tokens = int(usage_raw.get("inputTokens", usage_raw.get("input_tokens", 0)))
    output_tokens = int(usage_raw.get("outputTokens", usage_raw.get("output_tokens", 0)))

    cost_usd: float | None = None
    meta: dict[str, Any] = {}
    gateway = wire.get("providerMetadata", {}).get("gateway") if "providerMetadata" in wire else None
    if gateway:
        cost_usd = _absorb_gateway(gateway, meta)

    return JevResponse(
        model=wire["model"],
        answers=answers,
        usage=JevUsage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd),
        meta=meta,
    )


# --- registry ----------------------------------------------------------------------------------

ENCODERS = {
    "systemone": encode_systemone,
    "openrouter_decisions": encode_openrouter_decisions,
    "vercel_evaluate": encode_vercel_evaluate,
}

DECODERS = {
    "systemone": decode_systemone,
    "openrouter_decisions": decode_openrouter_decisions,
    "vercel_evaluate": decode_vercel_evaluate,
}


def encode(wire_format: str, req_body: dict[str, Any], model: str) -> dict[str, Any]:
    return ENCODERS[wire_format](req_body, model)


def decode(
    wire_format: str, wire_json: dict[str, Any], req: DecisionRequest, model_requested: str
) -> JevResponse:
    return DECODERS[wire_format](wire_json, req, model_requested)


# --- error bodies --------------------------------------------------------------------------------


def parse_error_message(status_code: int, body: Any) -> str:
    """Parse any of the three documented error shapes into a human-readable message.

    - TypeSafe / Vercel TypeSafe-compat / Vercel native evaluate: {"message": ..., "error_type": ...}
    - OpenRouter: {"error": {"code": ..., "message": ...}}
    - Vercel AI Gateway's own (e.g. 429 from the gateway itself): {"error": {"message": ..., "type": ...}}
    """
    if isinstance(body, dict):
        if isinstance(body.get("message"), str):
            error_type = body.get("error_type")
            return f"{body['message']} (error_type={error_type})" if error_type else body["message"]
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message", "unknown error")
            code = err.get("code", err.get("type"))
            return f"{msg} (code={code})" if code is not None else str(msg)
        if isinstance(err, str):
            return err
    return f"HTTP {status_code}: {body!r}"
