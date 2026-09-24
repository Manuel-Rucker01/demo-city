"""Turn Jev answers back into AgentDecisions. Owner: T4."""

from __future__ import annotations

import logging

from jevcity.types import (
    Action,
    AgentDecision,
    DecisionRequest,
    JevResponse,
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


def parse_decisions(
    reqs: list[DecisionRequest],
    responses: list[JevResponse],
    confidence_threshold: float,
) -> list[AgentDecision]:
    """Map answers to decisions. action below threshold -> STAY with gated=True.
    destination only kept when action == MOVE. Score answers normalized to 0..1."""
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
            if confidence < confidence_threshold:
                action = Action.STAY
                gated = True
            else:
                try:
                    action = Action(action_ans.get("choice"))
                except ValueError:
                    logger.warning(
                        "t%d agent %d: unrecognized action choice %r, falling back to STAY (gated)",
                        req.tick,
                        agent_id,
                        action_ans.get("choice"),
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
                    destination = dest_ans.get("choice")

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
                )
            )

    return decisions
