"""World state, housing/job market dynamics and decision application. Owner: T5.

All arithmetic lives here (never ask Jev to do math). Every function is deterministic
given the numpy Generator it receives.
"""

import numpy as np

from jevcity.types import (
    Agent,
    AgentChange,
    AgentDecision,
    DistrictProfile,
    Scenario,
    TickDelta,
    World,
)


def init_world(profiles: list[DistrictProfile], agents: list[Agent]) -> World:
    """Build DistrictStates in agent units from the initial population.

    housing_units = residents / (1 - vacancy_rate); jobs sized from employed agents working
    there plus vacancies implied by unemployment_rate and jobs_per_resident.
    """
    raise NotImplementedError


def apply_policies(world: World, scenario: Scenario, tick: int) -> None:
    """Activate/deactivate scenario policies (rent caps) on DistrictState for this tick."""
    raise NotImplementedError


def apply_decisions(
    world: World,
    agents: dict[int, Agent],
    decisions: list[AgentDecision],
    scenario: Scenario,
    rng: np.random.Generator,
    tick: int,
) -> TickDelta:
    """Apply Jev decisions: moves (need vacancy + affordability + moving cost), job search
    (matching against vacancies), spend/save (spending_level), satisfaction update."""
    raise NotImplementedError


def daily_update(
    world: World, agents: dict[int, Agent], scenario: Scenario, tick: int, rng: np.random.Generator
) -> list[AgentChange]:
    """Daily accounting (savings += (income - rent - spending)/30, shop revenue per district,
    unemployment counters, satisfaction drift) and, every rent_adjust_interval ticks, the
    market rent update (excess demand -> rent, respecting caps) and job creation from spending."""
    raise NotImplementedError
