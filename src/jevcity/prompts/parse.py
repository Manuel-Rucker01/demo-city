"""Turn Jev answers back into AgentDecisions. Owner: T4."""

from __future__ import annotations

import hashlib
import logging
import random

from jevcity.types import (
    Action,
    AgentDecision,
    CommuteMode,
    DecisionRequest,
    JevResponse,
    ShoppingPlace,
    question_key,
)

logger = logging.getLogger(__name__)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _score_fraction(answer: dict) -> float:
    levels = len(answer.get("legend") or {}) or 5
    score = float(answer.get("score", 0.0))
    if levels <= 1:
        return 0.0
    return _clamp01(score / (levels - 1))


def _pick(answer: dict, policy: str, seed: int, tick: int, agent_id: int, name: str) -> str | None:
    """Choose an option from a Choice answer.

    "sample": draw from the calibrated probabilities (keeps minority behaviour in the population;
    argmax over many similar agents would collapse everyone onto the mode). Deterministic per
    (seed, tick, agent, question) so replays reproduce runs exactly.
    "argmax"/"gate": the model's top choice.
    """
    probs = answer.get("probabilities") or {}
    if policy != "sample" or not probs:
        return answer.get("choice")
    digest = hashlib.sha256(f"{seed}:{tick}:{agent_id}:{name}".encode()).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    options = list(probs)
    weights = [max(float(probs[o]), 0.0) for o in options]
    if sum(weights) <= 0:
        return answer.get("choice")
    return rng.choices(options, weights=weights, k=1)[0]


def parse_decisions(
    reqs: list[DecisionRequest],
    responses: list[JevResponse],
    confidence_threshold: float,
    policy: str = "gate",
    seed: int = 0,
) -> list[AgentDecision]:
    """Map answers to decisions. Policy "gate": action below threshold -> STAY with gated=True;
    "argmax": top choice; "sample": draw from the probabilities (see _pick). Missing answers
    always fall back to STAY (gated). destination only kept when action == MOVE.
    Score answers normalized to 0..1."""
    decisions: list[AgentDecision] = []

    for req, resp in zip(reqs, responses, strict=True):
        for agent_id in req.agent_ids:
            action_ans = resp.answers.get(question_key(agent_id, "action"))
            if action_ans is None:
                logger.warning(
                    "t%d agent %d: missing 'action' answer, falling back to STAY (gated)",
                    req.tick,
                    agent_id,
                )
                decisions.append(
                    AgentDecision(
                        agent_id=agent_id,
                        tick=req.tick,
                        action=Action.STAY,
                        destination=None,
                        spending=0.0,
                        satisfaction=0.0,
                        confidence=0.0,
                        gated=True,
                        action_probs={},
                    )
                )
                continue

            confidence = float(action_ans.get("confidence", 0.0))
            probs = dict(action_ans.get("probabilities", {}))
            gated = False
            if policy == "gate" and confidence < confidence_threshold:
                action = Action.STAY
                gated = True
            else:
                chosen = _pick(action_ans, policy, seed, req.tick, agent_id, "action")
                try:
                    action = Action(chosen)
                except ValueError:
                    logger.warning(
                        "t%d agent %d: unrecognized action choice %r, falling back to STAY (gated)",
                        req.tick,
                        agent_id,
                        chosen,
                    )
                    action = Action.STAY
                    gated = True

            destination = None
            if action is Action.MOVE:
                dest_ans = resp.answers.get(question_key(agent_id, "destination"))
                if dest_ans is None:
                    logger.warning(
                        "t%d agent %d: missing 'destination' answer for a MOVE, "
                        "falling back to STAY (gated)",
                        req.tick,
                        agent_id,
                    )
                    action = Action.STAY
                    gated = True
                else:
                    destination = _pick(dest_ans, policy, seed, req.tick, agent_id, "destination")

            spending_ans = resp.answers.get(question_key(agent_id, "spending"))
            if spending_ans is None:
                logger.warning(
                    "t%d agent %d: missing 'spending' answer, defaulting to 0.5",
                    req.tick,
                    agent_id,
                )
                spending = 0.5
            else:
                spending = _score_fraction(spending_ans)

            satisfaction_ans = resp.answers.get(question_key(agent_id, "satisfaction"))
            if satisfaction_ans is None:
                logger.warning(
                    "t%d agent %d: missing 'satisfaction' answer, defaulting to 0.5",
                    req.tick,
                    agent_id,
                )
                satisfaction = 0.5
            else:
                satisfaction = _score_fraction(satisfaction_ans)

            # commute_mode / shopping_place are conditional questions (see state_builder's
            # wants_commute_question/wants_shopping_question): tolerate their absence, and
            # tolerate an unrecognized choice (None = "not asked / keep current").
            commute_mode = None
            commute_ans = resp.answers.get(question_key(agent_id, "commute_mode"))
            if commute_ans is not None:
                chosen = _pick(commute_ans, policy, seed, req.tick, agent_id, "commute_mode")
                try:
                    commute_mode = CommuteMode(chosen)
                except ValueError:
                    commute_mode = None

            shopping_place = None
            shopping_ans = resp.answers.get(question_key(agent_id, "shopping_place"))
            if shopping_ans is not None:
                chosen = _pick(shopping_ans, policy, seed, req.tick, agent_id, "shopping_place")
                try:
                    shopping_place = ShoppingPlace(chosen)
                except ValueError:
                    shopping_place = None

            decisions.append(
                AgentDecision(
                    agent_id=agent_id,
                    tick=req.tick,
                    action=action,
                    destination=destination,
                    spending=spending,
                    satisfaction=satisfaction,
                    confidence=confidence,
                    gated=gated,
                    action_probs=probs,
                    commute_mode=commute_mode,
                    shopping_place=shopping_place,
                )
            )

    return decisions
