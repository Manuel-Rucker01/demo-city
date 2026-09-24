"""Event detection: which agents must decide this tick. Owner: T5."""

import numpy as np

from jevcity.types import Agent, Event, EventParams, World


def detect_events(
    world: World,
    agents: dict[int, Agent],
    tick: int,
    params: EventParams,
    rng: np.random.Generator,
) -> list[Event]:
    """Return this tick's events (an agent may have several). Only agents with events decide.

    Also performs the event's own state change when it is not a decision (e.g. JOB_LOSS sets
    employed=False; LEASE_RENEWAL computes new rent from market rent and caps, applied here).
    PAYDAY is staggered: agent fires when (tick + agent.id) % 30 == 0.
    """
    raise NotImplementedError
