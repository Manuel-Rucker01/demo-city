"""Turn Jev answers back into AgentDecisions. Owner: T4."""

from jevcity.types import AgentDecision, DecisionRequest, JevResponse


def parse_decisions(
    reqs: list[DecisionRequest],
    responses: list[JevResponse],
    confidence_threshold: float,
) -> list[AgentDecision]:
    """Map answers to decisions. action below threshold -> STAY with gated=True.
    destination only kept when action == MOVE. Score answers normalized to 0..1."""
    raise NotImplementedError
