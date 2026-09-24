"""Turn agents + events into Jev DecisionRequests. Owner: T4.

Rules from docs.typesafe.ai/model-jaggedness/jev-1.13: English text, semantic buckets
instead of raw numbers (code computes 'rent burden: severe'), only relevant fields,
direct literal questions, criteria aligned with instructions. Target <= ~300 state tokens
per agent (estimate tokens as len(json)/4).
"""

from jevcity.types import Agent, DecisionRequest, Event, World


def build_requests(
    world: World,
    agents: dict[int, Agent],
    events: list[Event],
    tick: int,
    agents_per_request: int = 1,
) -> list[DecisionRequest]:
    """One DecisionRequest per group of K agents with events. Each agent gets the four
    QUESTION_NAMES questions keyed with question_key(agent_id, name). Fills mock_priors."""
    raise NotImplementedError
