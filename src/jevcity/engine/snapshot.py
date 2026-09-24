"""Build per-tick DistrictSnapshots from World + Agent state. Owner: T6."""

from __future__ import annotations

import importlib
import statistics
from collections.abc import Callable

from jevcity.types import (
    Agent,
    DistrictId,
    DistrictSnapshot,
    Occupation,
    ShoppingPlace,
    Tenure,
    World,
)

_NON_WORKING_AGE = (Occupation.STUDENT, Occupation.RETIRED)

# below_market_share: below the market rent = "locked in" at a rent below the current market
# rate. `world.below_market(agent, world) -> bool` is a rental-supply collaborator helper whose
# exact home module isn't fixed yet (world/rental.py or world/market.py per docs/CONTRACTS.md);
# it's looked up lazily and cached, with a fallback matching its documented threshold (< 85% of
# current district market rent) if it hasn't landed yet.
_BELOW_MARKET_CANDIDATE_MODULES = ("jevcity.world.rental", "jevcity.world.market")
_BELOW_MARKET_SHARE_THRESHOLD = 0.85


def _find_below_market_fn() -> Callable[[Agent, World], bool] | None:
    for modname in _BELOW_MARKET_CANDIDATE_MODULES:
        try:
            mod = importlib.import_module(modname)
        except ImportError:  # pragma: no cover - defensive, both modules exist today
            continue
        fn = getattr(mod, "below_market", None)
        if fn is not None:
            return fn
    return None


def _below_market_fallback(agent: Agent, world: World) -> bool:
    state = world.states.get(agent.home)
    if state is None or state.avg_rent <= 0:
        return False
    return agent.rent_monthly < _BELOW_MARKET_SHARE_THRESHOLD * state.avg_rent


def build_district_snapshots(
    world: World,
    agents: dict[int, Agent],
    arrivals: list[int] | None = None,
    departures: list[int] | None = None,
) -> list[DistrictSnapshot]:
    """One DistrictSnapshot per district in `world.states`, computed from current residents.

    Inactive agents (`Agent.active is False`, i.e. households that already left Barcelona)
    are excluded from every resident-based figure below (residents, rents, unemployment,
    satisfaction, mode_share, online_share).

    unemployment_rate is computed among working-age (not student, not retired) residents only,
    matching `Agent.income_monthly`'s treatment of those occupations. avg_paid_rent and
    avg_rent_burden are computed over RENTER residents only: owners' `rent_monthly` is a
    housing cost (mortgage/fees), not a market lease payment, and mixing the two would distort
    both the "rent actually paid" and "rent burden" figures the run log reports.

    mode_share is the distribution of `commute_mode` among active, employed residents who have
    one set (i.e. actually commuting); online_share is the share of active residents whose
    `shopping_place` is ShoppingPlace.ONLINE.

    `arrivals`/`departures` are this tick's agent ids (from TickDelta.arrivals/.departures,
    looked up in `agents` for their `.home` district); pass None (the default) when no
    per-tick migration figures apply (e.g. a final/summary snapshot not tied to one tick).
    """
    below_market_fn = _find_below_market_fn() or _below_market_fallback
    completed_today: dict[DistrictId, int] = getattr(world, "completed_today", {}) or {}

    residents_by_district: dict[DistrictId, list[Agent]] = {did: [] for did in world.states}
    for agent in agents.values():
        if not agent.active:
            continue
        residents_by_district.setdefault(agent.home, []).append(agent)

    arrivals_by_district: dict[DistrictId, int] = {}
    for aid in arrivals or []:
        agent = agents.get(aid)
        if agent is not None:
            arrivals_by_district[agent.home] = arrivals_by_district.get(agent.home, 0) + 1

    departures_by_district: dict[DistrictId, int] = {}
    for aid in departures or []:
        agent = agents.get(aid)
        if agent is not None:
            departures_by_district[agent.home] = departures_by_district.get(agent.home, 0) + 1

    snapshots: list[DistrictSnapshot] = []
    for did in sorted(world.states):
        state = world.states[did]
        residents = residents_by_district.get(did, [])
        n = len(residents)
        renters = [a for a in residents if a.tenure is Tenure.RENTER]
        n_renters = len(renters)

        avg_paid_rent = sum(a.rent_monthly for a in renters) / n_renters if n_renters else 0.0
        avg_satisfaction = sum(a.satisfaction for a in residents) / n if n else 0.0
        # Median, not mean: agents with (near) zero income have huge burdens that swamp a mean.
        avg_rent_burden = statistics.median(a.rent_burden for a in renters) if n_renters else 0.0

        working_age = [a for a in residents if a.occupation not in _NON_WORKING_AGE]
        unemployment_rate = (
            sum(1 for a in working_age if not a.employed) / len(working_age) if working_age else 0.0
        )

        commuters = [a for a in residents if a.employed and a.commute_mode is not None]
        mode_share: dict[str, float] = {}
        if commuters:
            counts: dict[str, int] = {}
            for a in commuters:
                mode = a.commute_mode.value  # type: ignore[union-attr]
                counts[mode] = counts.get(mode, 0) + 1
            mode_share = {mode: cnt / len(commuters) for mode, cnt in counts.items()}

        online_share = (
            sum(1 for a in residents if a.shopping_place is ShoppingPlace.ONLINE) / n if n else 0.0
        )

        # Rental-supply figures (see RentalSupplyParams / DistrictSnapshot in types.py). These
        # fields are always present on DistrictState (defaulting to 0 / 1.0) even before the
        # market collaborator populates them, so this is safe to compute unconditionally.
        rental_units = state.rental_units
        renovating_units = getattr(state, "renovating_units", 0)
        # Vacant, lettable rental units: rental stock minus renter-occupied minus off-market
        # (renovating) units -- renovating_units is documented as counted inside rental_units.
        vacant_rental_units = max(rental_units - n_renters - renovating_units, 0)
        rental_vacancy_rate = vacant_rental_units / rental_units if rental_units > 0 else 0.0

        below_market_share = (
            sum(1 for a in renters if below_market_fn(a, world)) / n_renters if n_renters else 0.0
        )

        snapshots.append(
            DistrictSnapshot(
                id=did,
                avg_rent=state.avg_rent,
                avg_paid_rent=avg_paid_rent,
                residents=n,
                vacancy_rate=state.vacancy_rate,
                unemployment_rate=unemployment_rate,
                jobs=state.jobs,
                filled_jobs=state.filled_jobs,
                shop_revenue=state.shop_revenue_daily,
                avg_satisfaction=avg_satisfaction,
                avg_rent_burden=avg_rent_burden,
                rent_cap_active=state.rent_cap is not None,
                tourist_units=state.tourist_units,
                shops_open=state.shops_open,
                shop_revenue_monthly=state.shop_revenue_monthly,
                mode_share=mode_share,
                online_share=online_share,
                arrivals=arrivals_by_district.get(did, 0),
                departures=departures_by_district.get(did, 0),
                owner_units=state.owner_units,
                rental_units=rental_units,
                seasonal_units=state.seasonal_units,
                rental_vacancy_rate=rental_vacancy_rate,
                quality=state.quality,
                new_units_completed=completed_today.get(did, 0),
                below_market_share=below_market_share,
            )
        )
    return snapshots
