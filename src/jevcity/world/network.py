"""Zone-level transit network: barris, door-to-door travel times, network variants. Owner: T3.

Contract (do not change signatures without the integrator): docs/TRANSIT_ACCESS.md.

Everything here is a no-op / returns None when `world.access` is None, so district-only
scenarios (no `Scenario.access_path`) behave exactly as before.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from jevcity.types import (
    Agent,
    DistrictId,
    Scenario,
    TransitAccess,
    TransitNetworkPolicy,
    World,
    Zone,
    ZoneId,
)


def load_access(path: str | Path) -> TransitAccess:
    """Read and validate data/processed/transit_access.json (checks matrix sizes and that every
    zone's district exists is the caller's job: see validate_access)."""
    raise NotImplementedError


def validate_access(access: TransitAccess, districts: set[DistrictId]) -> None:
    """Raise ValueError if: a variant/mode matrix is not len(zones)**2 long, "base" is not the first
    variant, a zone's district is not in `districts`, a district has no zone, or job_weight does not
    sum to ~1 within a district."""
    raise NotImplementedError


def zones_in(access: TransitAccess, district: DistrictId) -> list[Zone]:
    raise NotImplementedError


def sample_home_zone(access: TransitAccess, district: DistrictId, rng: np.random.Generator) -> ZoneId:
    """A barri inside `district`, weighted by population."""
    raise NotImplementedError


def sample_job_zone(access: TransitAccess, district: DistrictId, rng: np.random.Generator) -> ZoneId:
    """A barri inside `district`, weighted by job_weight."""
    raise NotImplementedError


def active_variant(scenario: Scenario, tick: int) -> str:
    """The variant of the TransitNetworkPolicy with the latest start_tick <= tick, else "base"."""
    raise NotImplementedError


def active_network_policy(scenario: Scenario, tick: int) -> TransitNetworkPolicy | None:
    raise NotImplementedError


def trip_minutes(
    world: World, home_zone: ZoneId, job_zone: ZoneId, variant: str | None = None
) -> dict[str, float]:
    """Door-to-door minutes by mode (TransitAccess.modes) for home_zone -> job_zone under `variant`
    (default: world.network_variant). Matrix lookups must be O(1) (cache an index per access)."""
    raise NotImplementedError


def agent_trip_minutes(world: World, agent: Agent, variant: str | None = None) -> dict[str, float] | None:
    """trip_minutes for the agent's own commute; None when world.access is None, the agent is not
    employed, or either zone is unset."""
    raise NotImplementedError


def new_access_share(world: World, zone: ZoneId, variant: str) -> float:
    """Zone.new_coverage[variant] (0.0 if absent)."""
    raise NotImplementedError
