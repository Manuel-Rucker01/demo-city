"""Build per-tick DistrictSnapshots from World + Agent state. Owner: T6."""

from __future__ import annotations

import statistics

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
            )
        )
    return snapshots
