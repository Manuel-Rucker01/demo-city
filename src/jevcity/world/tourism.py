"""Tourist-flat (HUT) sizing and phase-out. Owner: T5.

Formulas (see world/market.py module docstring for the full formula catalogue):

- `initial_tourist_units`: profile.tourist_flats (real licensed dwellings, None -> 0) scaled
  to agent units by `agent_scale` (agents in the district / real households in the district,
  see `_helpers.compute_agent_scale`), rounded. These units are added on top of the
  residential-only stock computed by `init_world`, i.e.
  `housing_units = residential_base + tourist_units`: they are part of the total dwelling
  stock but are never available to a resident move (see `_helpers.resident_vacancy`).
- `apply_phase_out`: a `TouristFlatPolicy` shrinks `tourist_units` linearly between
  `start_tick` and `end_tick` from the initial level down to `(1 - reduction) * initial`.
  `return_to_rental_share` of every unit removed so far re-joins the residential stock as a
  vacant unit (added to `housing_units`, which is otherwise fixed at
  `residential_base + tourist_units`); the rest of the removed units simply leave the total
  dwelling stock (e.g. de-licensed and left off-market, merged into other uses, ...).
  Pure function of `tick` and the stored initial level, so re-applying it for the same tick
  is idempotent.
"""

from __future__ import annotations

from jevcity.types import DistrictProfile, DistrictState, TouristFlatPolicy

from ._helpers import compute_agent_scale


def initial_tourist_units(profile: DistrictProfile, agent_scale: float) -> int:
    tourist_flats = profile.tourist_flats or 0
    return round(tourist_flats * agent_scale)


def phase_out_fraction(policy: TouristFlatPolicy, tick: int) -> float:
    """0.0 before start_tick, 1.0 at/after end_tick, linear in between."""
    if tick < policy.start_tick:
        return 0.0
    if policy.end_tick <= policy.start_tick or tick >= policy.end_tick:
        return 1.0
    return (tick - policy.start_tick) / (policy.end_tick - policy.start_tick)


def applies_to(policy: TouristFlatPolicy, district_id: str) -> bool:
    return policy.districts == "all" or district_id in policy.districts


def apply_phase_out(
    state: DistrictState,
    policy: TouristFlatPolicy,
    tick: int,
    initial_units: int,
    base_housing: int,
) -> None:
    """Recompute state.tourist_units / state.housing_units for this tick (idempotent)."""
    fraction = phase_out_fraction(policy, tick)
    target = initial_units * (1.0 - policy.reduction * fraction)
    new_units = round(target)
    removed = max(initial_units - new_units, 0)
    returned = round(policy.return_to_rental_share * removed)
    state.tourist_units = new_units
    state.housing_units = base_housing + new_units + returned


__all__ = [
    "applies_to",
    "apply_phase_out",
    "compute_agent_scale",
    "initial_tourist_units",
    "phase_out_fraction",
]
