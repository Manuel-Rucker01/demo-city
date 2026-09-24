"""Synthetic population from district profiles. Owner: T2."""

import numpy as np

from jevcity.types import Agent, DistrictProfile


def generate_population(
    profiles: list[DistrictProfile], n_agents: int, rng: np.random.Generator
) -> list[Agent]:
    """Create n_agents adults (18+) distributed by district population, with age from the
    district age distribution (renormalized without 0-17), wage from income per capita
    (lognormal), occupation, employment from unemployment_rate, job district weighted by
    jobs_per_resident, rent near avg_rent_monthly, staggered lease_start_tick in [-359, 0].
    Must be deterministic for a given rng seed and fast for 10_000 agents (<1s)."""
    raise NotImplementedError
