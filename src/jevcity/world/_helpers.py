"""Small private helpers shared by world/market.py and events/triggers.py.

Not part of the public contract (see docs/CONTRACTS.md); safe to change freely
within T5's scope.
"""

from __future__ import annotations

from jevcity.types import DistrictProfile, DistrictState, Occupation

DEFAULT_AVG_HOUSEHOLD_SIZE = 2.45  # kept in sync with population/generator.py's constant


def clip(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def is_working_age(occupation: Occupation) -> bool:
    """True for agents who can hold/lose/search for a job (excludes students & retirees)."""
    return occupation not in (Occupation.STUDENT, Occupation.RETIRED)


def real_households(profile: DistrictProfile) -> float:
    """Estimated real households in a district: population / avg_household_size (fallback
    DEFAULT_AVG_HOUSEHOLD_SIZE when the profile doesn't set it)."""
    size = profile.avg_household_size or DEFAULT_AVG_HOUSEHOLD_SIZE
    return profile.population / size if size > 0 else 0.0


def compute_agent_scale(profile: DistrictProfile, residents: int) -> float:
    """agents-in-district / real-households-in-district. Used to convert real-count
    quantities (tourist flats, shop jobs) into agent units, and to convert agent-unit
    spending back into real EUR for shop revenue (dividing by this factor)."""
    households = real_households(profile)
    return residents / households if households > 0 else 0.0


def resident_vacancy(state: DistrictState) -> int:
    """Housing units available to a resident household: total stock minus what's already
    resident-occupied minus units used as tourist flats (not available to residents).

    `DistrictState.vacant_units` (housing_units - occupied_units) can't be redefined here
    (it's a property on the pydantic model in types.py, out of this module's scope), so it
    now conflates "empty" with "tourist-occupied": this helper is the corrected figure and
    must be used wherever code decides whether a resident can move into a district."""
    return state.housing_units - state.occupied_units - state.tourist_units
