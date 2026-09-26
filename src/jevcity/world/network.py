"""Zone-level transit network: barris, door-to-door travel times, network variants. Owner: T3.

Contract (do not change signatures without the integrator): docs/TRANSIT_ACCESS.md.

Everything here is a no-op / returns None when `world.access` is None, so district-only
scenarios (no `Scenario.access_path`) behave exactly as before.
"""

from __future__ import annotations

import json
import weakref
from pathlib import Path

import numpy as np
from pydantic import ValidationError

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

_SUM_TOLERANCE = 1e-3

# Per-TransitAccess-instance cache (zone-id -> matrix-index dict, and (variant, mode) -> numpy
# matrix), keyed by id(access) rather than the instance itself: TransitAccess is a plain
# (mutable) pydantic BaseModel, which pydantic leaves unhashable, so it can't be a dict/
# WeakKeyDictionary key directly. Keying by id() with a weakref + finalizer callback gets the
# same effect (entry auto-evicted once the access object is garbage-collected, no leak across
# many short-lived TransitAccess instances in tests) without needing it to be hashable.
# Lookups must be O(1): building these is O(zones**2 * modes * variants), done at most once
# per TransitAccess instance, never per call.
_ACCESS_CACHE: dict[int, tuple[weakref.ref, dict]] = {}


def _access_cache(access: TransitAccess) -> dict:
    key = id(access)
    entry = _ACCESS_CACHE.get(key)
    if entry is not None and entry[0]() is access:
        return entry[1]
    cache: dict = {}

    def _evict(_ref: weakref.ref, key: int = key) -> None:
        _ACCESS_CACHE.pop(key, None)

    _ACCESS_CACHE[key] = (weakref.ref(access, _evict), cache)
    return cache


def _zone_index(access: TransitAccess) -> dict[ZoneId, int]:
    cache = _access_cache(access)
    idx = cache.get("zone_index")
    if idx is None:
        idx = {z.id: i for i, z in enumerate(access.zones)}
        cache["zone_index"] = idx
    return idx


def _matrix(access: TransitAccess, variant: str, mode: str) -> np.ndarray:
    cache = _access_cache(access)
    matrices = cache.get("matrices")
    if matrices is None:
        matrices = {}
        cache["matrices"] = matrices
    key = (variant, mode)
    arr = matrices.get(key)
    if arr is None:
        n = len(access.zones)
        arr = np.asarray(access.times[variant][mode], dtype=float).reshape(n, n)
        matrices[key] = arr
    return arr


def load_access(path: str | Path) -> TransitAccess:
    """Read and validate data/processed/transit_access.json (checks matrix sizes and that every
    zone's district exists is the caller's job: see validate_access)."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read transit access file {path}: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        # ValueError (not TypeError) is this module's documented contract for malformed input.
        raise ValueError(  # noqa: TRY004
            f"{path} must contain a JSON object (one TransitAccess document), got {type(payload).__name__}"
        )

    try:
        return TransitAccess(**payload)
    except ValidationError as exc:
        raise ValueError(f"{path} failed validation: {exc}") from exc


def validate_access(access: TransitAccess, districts: set[DistrictId]) -> None:
    """Raise ValueError if: a variant/mode matrix is not len(zones)**2 long, "base" is not the first
    variant, a zone's district is not in `districts`, a district has no zone, or job_weight does not
    sum to ~1 within a district."""
    if not access.variants or access.variants[0] != "base":
        raise ValueError(f"TransitAccess.variants must start with 'base', got {access.variants!r}")

    n = len(access.zones)
    expected_len = n * n
    for variant in access.variants:
        modes_for_variant = access.times.get(variant)
        if modes_for_variant is None:
            raise ValueError(f"TransitAccess.times is missing variant {variant!r}")
        for mode in access.modes:
            matrix = modes_for_variant.get(mode)
            if matrix is None:
                raise ValueError(f"TransitAccess.times[{variant!r}] is missing mode {mode!r}")
            if len(matrix) != expected_len:
                raise ValueError(
                    f"TransitAccess.times[{variant!r}][{mode!r}] has length {len(matrix)}, "
                    f"expected {expected_len} (= {n} zones squared)"
                )

    zones_by_district: dict[DistrictId, list[Zone]] = {}
    for zone in access.zones:
        if zone.district not in districts:
            raise ValueError(
                f"zone {zone.id!r} ({zone.name!r}) has district {zone.district!r}, "
                f"which is not one of the known districts {sorted(districts)}"
            )
        zones_by_district.setdefault(zone.district, []).append(zone)

    missing = sorted(districts - set(zones_by_district))
    if missing:
        raise ValueError(f"district(s) with no zone in TransitAccess.zones: {missing}")

    for district, zones in zones_by_district.items():
        total_job_weight = sum(z.job_weight for z in zones)
        if abs(total_job_weight - 1.0) > _SUM_TOLERANCE:
            raise ValueError(
                f"district {district!r}: zone job_weight sums to {total_job_weight!r}, "
                f"expected ~1.0 (tolerance {_SUM_TOLERANCE})"
            )


def zones_in(access: TransitAccess, district: DistrictId) -> list[Zone]:
    return [z for z in access.zones if z.district == district]


def sample_home_zone(access: TransitAccess, district: DistrictId, rng: np.random.Generator) -> ZoneId:
    """A barri inside `district`, weighted by population."""
    zones = zones_in(access, district)
    if not zones:
        raise ValueError(f"no zone for district {district!r}")
    weights = np.array([max(z.population, 0) for z in zones], dtype=float)
    if weights.sum() <= 0:
        weights = np.ones(len(zones))
    probs = weights / weights.sum()
    idx = rng.choice(len(zones), p=probs)
    return zones[int(idx)].id


def sample_job_zone(access: TransitAccess, district: DistrictId, rng: np.random.Generator) -> ZoneId:
    """A barri inside `district`, weighted by job_weight."""
    zones = zones_in(access, district)
    if not zones:
        raise ValueError(f"no zone for district {district!r}")
    weights = np.array([max(z.job_weight, 0.0) for z in zones], dtype=float)
    if weights.sum() <= 0:
        weights = np.ones(len(zones))
    probs = weights / weights.sum()
    idx = rng.choice(len(zones), p=probs)
    return zones[int(idx)].id


def active_network_policy(scenario: Scenario, tick: int) -> TransitNetworkPolicy | None:
    """The TransitNetworkPolicy with the latest start_tick <= tick, else None."""
    best: TransitNetworkPolicy | None = None
    for policy in scenario.policies:
        if not isinstance(policy, TransitNetworkPolicy):
            continue
        if policy.start_tick > tick:
            continue
        if best is None or policy.start_tick >= best.start_tick:
            best = policy
    return best


def active_variant(scenario: Scenario, tick: int) -> str:
    """The variant of the TransitNetworkPolicy with the latest start_tick <= tick, else "base"."""
    policy = active_network_policy(scenario, tick)
    return policy.variant if policy is not None else "base"


def trip_minutes(
    world: World, home_zone: ZoneId, job_zone: ZoneId, variant: str | None = None
) -> dict[str, float]:
    """Door-to-door minutes by mode (TransitAccess.modes) for home_zone -> job_zone under `variant`
    (default: world.network_variant). Matrix lookups must be O(1) (cache an index per access)."""
    access = world.access
    if access is None:
        raise ValueError("world.access is None")
    v = variant if variant is not None else world.network_variant
    idx = _zone_index(access)
    i = idx.get(home_zone)
    j = idx.get(job_zone)
    if i is None:
        raise KeyError(f"unknown home zone {home_zone!r}")
    if j is None:
        raise KeyError(f"unknown job zone {job_zone!r}")
    return {mode: float(_matrix(access, v, mode)[i, j]) for mode in access.modes}


def agent_trip_minutes(world: World, agent: Agent, variant: str | None = None) -> dict[str, float] | None:
    """trip_minutes for the agent's own commute; None when world.access is None, the agent is not
    employed, or either zone is unset."""
    if world.access is None or not agent.employed or agent.home_zone is None or agent.job_zone is None:
        return None
    return trip_minutes(world, agent.home_zone, agent.job_zone, variant)


def new_access_share(world: World, zone: ZoneId, variant: str) -> float:
    """Zone.new_coverage[variant] (0.0 if absent)."""
    access = world.access
    if access is None:
        return 0.0
    idx = _zone_index(access)
    i = idx.get(zone)
    if i is None:
        return 0.0
    return access.zones[i].new_coverage.get(variant, 0.0)


# A usual commute on foot or by bike longer than this is implausible (EMEF 2024: the average walking
# trip in Barcelona is well under 20 min). District-level quotas assign modes without distances,
# so with zones some agents would "walk" 99 minutes; repair_commute_mode moves them to transit.
WALK_COMMUTE_MAX_MIN = 35.0
BIKE_COMMUTE_MAX_MIN = 45.0


def repair_commute_mode(world: World, agent: Agent) -> bool:
    """With zones: turn an implausibly long usual walk/bike commute into the faster of metro and bus
    (deterministic, no rng draw, commute_since_tick kept). Returns True if the mode changed. No-op
    without world.access, without zones, or for any other mode."""
    from jevcity.types import CommuteMode

    if agent.commute_mode not in (CommuteMode.WALK, CommuteMode.BIKE):
        return False
    times = agent_trip_minutes(world, agent)
    if times is None:
        return False
    limit = WALK_COMMUTE_MAX_MIN if agent.commute_mode is CommuteMode.WALK else BIKE_COMMUTE_MAX_MIN
    if times.get(agent.commute_mode.value, 0.0) <= limit:
        return False
    agent.commute_mode = CommuteMode.METRO if times.get("metro", 1e9) <= times.get("bus", 1e9) else CommuteMode.BUS
    return True
