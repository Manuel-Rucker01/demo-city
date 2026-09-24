"""Build per-tick DistrictSnapshots from World + Agent state. Owner: T6."""

from __future__ import annotations

import statistics

from jevcity.types import Agent, DistrictId, DistrictSnapshot, Occupation, Tenure, World

_NON_WORKING_AGE = (Occupation.STUDENT, Occupation.RETIRED)


def build_district_snapshots(world: World, agents: dict[int, Agent]) -> list[DistrictSnapshot]:
    """One DistrictSnapshot per district in `world.states`, computed from current residents.

    unemployment_rate is computed among working-age (not student, not retired) residents only,
    matching `Agent.income_monthly`'s treatment of those occupations. avg_paid_rent and
    avg_rent_burden are computed over RENTER residents only: owners' `rent_monthly` is a
    housing cost (mortgage/fees), not a market lease payment, and mixing the two would distort
    both the "rent actually paid" and "rent burden" figures the run log reports.
    """
    residents_by_district: dict[DistrictId, list[Agent]] = {did: [] for did in world.states}
    for agent in agents.values():
        residents_by_district.setdefault(agent.home, []).append(agent)

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
            )
        )
    return snapshots
