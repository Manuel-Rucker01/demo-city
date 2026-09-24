"""World state, housing/job market dynamics and decision application. Owner: T5.

All arithmetic lives here (never ask Jev to do math). Every function is deterministic
given the numpy Generator it receives.

Formulas (documented here so README/docs can quote them verbatim):

- init_world: housing_units = round(residents / (1 - vacancy_rate)); job vacancies are
  sized as 3-5% of filled jobs, more where jobs_per_resident is high (a rough proxy for
  a "hot" job market), so a job_search has a realistic chance of success:
  vacancy_fraction = 0.03 + 0.02 * min(jobs_per_resident, 1.0).
- apply_policies: RentCapPolicy sets state.rent_cap (absolute cap_monthly, or
  cap_pct_of_initial * rent_history[id][0]) and state.max_increase_pct once
  tick >= start_tick; otherwise both are cleared (None).
- detect_events / LEASE_RENEWAL: new_rent = min(max(old_rent, avg_rent * relative_position),
  old_rent * (1 + cap)), then clamped to state.rent_cap if active, where
  relative_position = clip(agent.rent / district.avg_rent, 0.7, 1.4) approximates the
  agent's standing relative to the market at signing time. The outer max() makes renewals
  monotonically non-decreasing (a renewal only ever catches an underpriced lease up toward
  market, up to the cap; it never lowers rent), so cap=0 (a freeze policy) always yields
  exactly a 0% increase. cap = state.max_increase_pct if set else RENEWAL_INCREASE_CAP_DEFAULT
  (0.10). See "Contract concern" in the final report: EventParams has no renewal-cap field,
  so the cap is plumbed through DistrictState.max_increase_pct (set by apply_policies) with
  this module-level default as fallback.
- apply_decisions / MOVE: feasible when the destination has a vacant unit (unless it's
  the same district - a relet, always allowed), new_rent = min(dst.avg_rent,
  dst.rent_cap) is affordable (new_rent <= 0.6 * income OR savings >= 6 * new_rent),
  and savings >= moving_cost.
- apply_decisions / JOB_SEARCH: best district = home if it has vacancies, else a
  vacancy-weighted random pick among districts with vacancies; success probability =
  job_match_daily_prob * min(1, vacancies_in_best_district / JOB_SEARCH_VACANCY_SCALE).
- daily_update: savings += (income - rent - living_costs - discretionary) / 30, where
  living_costs = 400 + 150 * household_size and discretionary = spending_level *
  max(income - rent - living_costs, 0) * 0.5; discretionary/30 (i.e. the daily slice) is
  added to the home district's shop_revenue_daily. Satisfaction drifts 2%/day toward a
  burden-implied baseline = clip(1 - rent_burden / 1.5, 0, 1).
- daily_update / rent adjustment (every rent_adjust_interval ticks): excess demand ratio
  = (target_vacancy - vacancy_rate) / target_vacancy; monthly change = clip(rent_elasticity
  * excess * 0.05, +-max_monthly_rent_change), then clamp to rent_cap. With the default
  rent_elasticity=0.6 and target_vacancy=0.05, a district at ~1% vacancy (excess=0.8)
  changes by +2.4%/month and one at ~10% vacancy (excess=-1.0) changes by -3%/month
  (clipped at max_monthly_rent_change=0.03).
- daily_update / job creation (same cadence): jobs += round(spend_to_jobs *
  (this_month_shop_revenue - first_month_shop_revenue_baseline)), floored at filled_jobs.
"""

from __future__ import annotations

import numpy as np

from jevcity.types import (
    Action,
    Agent,
    AgentChange,
    AgentDecision,
    DistrictProfile,
    DistrictState,
    MoveRecord,
    RentCapPolicy,
    Scenario,
    TickDelta,
    World,
)
from jevcity.world._helpers import clip, is_working_age

RENEWAL_INCREASE_CAP_DEFAULT = 0.10  # fallback when no policy sets max_increase_pct
JOB_SEARCH_VACANCY_SCALE = 10.0  # vacancies at which job_search success prob saturates


def init_world(profiles: list[DistrictProfile], agents: list[Agent]) -> World:
    """Build DistrictStates in agent units from the initial population.

    housing_units = residents / (1 - vacancy_rate); jobs sized from employed agents working
    there plus vacancies implied by unemployment_rate and jobs_per_resident.
    """
    residents: dict[str, int] = {p.id: 0 for p in profiles}
    filled_jobs: dict[str, int] = {p.id: 0 for p in profiles}
    for agent in agents:
        if agent.home in residents:
            residents[agent.home] += 1
        if agent.employed and agent.job_district is not None and agent.job_district in filled_jobs:
            filled_jobs[agent.job_district] += 1

    states: dict[str, DistrictState] = {}
    rent_history: dict[str, list[float]] = {}
    for p in profiles:
        r = residents[p.id]
        vacancy_rate = clip(p.vacancy_rate, 0.0, 0.95)
        housing_units = max(round(r / (1 - vacancy_rate)), r)
        fj = filled_jobs[p.id]
        vacancy_fraction = 0.03 + 0.02 * min(p.jobs_per_resident, 1.0)
        vacancies = round(fj * vacancy_fraction)
        jobs = fj + vacancies
        states[p.id] = DistrictState(
            id=p.id,
            avg_rent=p.avg_rent_monthly,
            housing_units=housing_units,
            occupied_units=r,
            jobs=jobs,
            filled_jobs=fj,
        )
        rent_history[p.id] = [p.avg_rent_monthly]

    return World(
        profiles={p.id: p for p in profiles},
        states=states,
        tick=0,
        rent_history=rent_history,
    )


def apply_policies(world: World, scenario: Scenario, tick: int) -> None:
    """Activate/deactivate scenario policies (rent caps) on DistrictState for this tick."""
    for state in world.states.values():
        state.rent_cap = None
        state.max_increase_pct = None

    for policy in scenario.policies:
        if not isinstance(policy, RentCapPolicy):
            continue
        if tick < policy.start_tick:
            continue
        state = world.states.get(policy.district)
        if state is None:
            continue

        cap = policy.cap_monthly
        if cap is None and policy.cap_pct_of_initial is not None:
            history = world.rent_history.get(policy.district) if world.rent_history else None
            initial = history[0] if history else state.avg_rent
            cap = policy.cap_pct_of_initial * initial
        if cap is not None:
            state.rent_cap = cap
        if policy.max_increase_pct is not None:
            state.max_increase_pct = policy.max_increase_pct


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
    moves: list[MoveRecord] = []
    changes: list[AgentChange] = []
    failed_moves = 0
    job_matches = 0
    market = scenario.market

    for decision in decisions:
        agent = agents.get(decision.agent_id)
        if agent is None:
            continue

        old_employed = agent.employed
        old_satisfaction = agent.satisfaction

        if decision.action == Action.MOVE:
            dst_id = decision.destination
            dst_state = world.states.get(dst_id) if dst_id else None
            src_home = agent.home
            src_state = world.states.get(src_home)
            if dst_state is None or src_state is None:
                failed_moves += 1
            else:
                same_district = dst_id == src_home
                if not same_district and dst_state.vacant_units <= 0:
                    failed_moves += 1
                else:
                    new_rent = dst_state.avg_rent
                    if dst_state.rent_cap is not None:
                        new_rent = min(new_rent, dst_state.rent_cap)
                    income = agent.income_monthly
                    affordable = new_rent <= 0.6 * income or agent.savings >= 6 * new_rent
                    can_pay_moving = agent.savings >= market.moving_cost
                    if not (affordable and can_pay_moving):
                        failed_moves += 1
                    else:
                        if not same_district:
                            src_state.occupied_units -= 1
                            dst_state.occupied_units += 1
                            moves.append(MoveRecord(agent_id=agent.id, src=src_home, dst=dst_id))
                        agent.home = dst_id
                        agent.rent_monthly = new_rent
                        agent.lease_start_tick = tick
                        agent.last_move_tick = tick
                        agent.savings -= market.moving_cost
        elif decision.action == Action.JOB_SEARCH and not agent.employed:
            home_state = world.states.get(agent.home)
            best_state = None
            if home_state is not None and home_state.job_vacancies > 0:
                best_state = home_state
            else:
                candidates = [s for s in world.states.values() if s.job_vacancies > 0]
                if candidates:
                    weights = np.array([s.job_vacancies for s in candidates], dtype=float)
                    weights /= weights.sum()
                    idx = rng.choice(len(candidates), p=weights)
                    best_state = candidates[idx]
            if best_state is not None:
                prob = market.job_match_daily_prob * min(
                    1.0, best_state.job_vacancies / JOB_SEARCH_VACANCY_SCALE
                )
                if rng.random() < prob:
                    agent.employed = True
                    agent.job_district = best_state.id
                    agent.days_unemployed = 0
                    best_state.filled_jobs += 1
                    job_matches += 1

        # Always: spending_level nudged toward the decision's answer, satisfaction updated.
        agent.spending_level = clip(0.5 * agent.spending_level + 0.5 * decision.spending, 0.0, 1.0)
        if decision.action == Action.SPEND:
            agent.spending_level = clip(agent.spending_level + 0.1, 0.0, 1.0)
        elif decision.action == Action.SAVE:
            agent.spending_level = clip(agent.spending_level - 0.1, 0.0, 1.0)
        agent.satisfaction = clip(decision.satisfaction, 0.0, 1.0)

        change_fields: dict[str, float | bool] = {}
        if abs(agent.satisfaction - old_satisfaction) > 0.05:
            change_fields["satisfaction"] = agent.satisfaction
        if agent.employed != old_employed:
            change_fields["employed"] = agent.employed
        if change_fields:
            changes.append(AgentChange(agent_id=agent.id, **change_fields))

    return TickDelta(moves=moves, changes=changes, failed_moves=failed_moves, job_matches=job_matches)


def daily_update(
    world: World, agents: dict[int, Agent], scenario: Scenario, tick: int, rng: np.random.Generator
) -> list[AgentChange]:
    """Daily accounting (savings += (income - rent - spending)/30, shop revenue per district,
    unemployment counters, satisfaction drift) and, every rent_adjust_interval ticks, the
    market rent update (excess demand -> rent, respecting caps) and job creation from spending.
    """
    changes: list[AgentChange] = []
    market = scenario.market

    revenue_accum: dict[str, float] = getattr(world, "_monthly_revenue_accum", None)
    if revenue_accum is None:
        revenue_accum = {d: 0.0 for d in world.states}
        world._monthly_revenue_accum = revenue_accum  # type: ignore[attr-defined]
    revenue_baseline: dict[str, float] | None = getattr(world, "_revenue_baseline", None)
    if revenue_baseline is None:
        revenue_baseline = {}
        world._revenue_baseline = revenue_baseline  # type: ignore[attr-defined]

    for state in world.states.values():
        state.shop_revenue_daily = 0.0

    for agent in agents.values():
        income = agent.income_monthly
        rent = agent.rent_monthly
        living_costs = 400.0 + 150.0 * agent.household_size
        residual = income - rent - living_costs
        discretionary = agent.spending_level * max(residual, 0.0) * 0.5
        daily_net = (income - rent - living_costs - discretionary) / 30.0
        agent.savings += daily_net

        daily_spend = discretionary / 30.0
        home_state = world.states.get(agent.home)
        if home_state is not None:
            home_state.shop_revenue_daily += daily_spend
            revenue_accum[agent.home] = revenue_accum.get(agent.home, 0.0) + daily_spend

        if (not agent.employed) and is_working_age(agent.occupation):
            agent.days_unemployed += 1

        burden = agent.rent_burden
        baseline = clip(1.0 - min(burden, 1.5) / 1.5, 0.0, 1.0)
        old_satisfaction = agent.satisfaction
        agent.satisfaction = clip(agent.satisfaction + (baseline - agent.satisfaction) * 0.02, 0.0, 1.0)
        if abs(agent.satisfaction - old_satisfaction) > 0.05:
            changes.append(AgentChange(agent_id=agent.id, satisfaction=agent.satisfaction))

    if tick > 0 and tick % market.rent_adjust_interval == 0:
        target_vacancy = market.target_vacancy
        for state in world.states.values():
            if target_vacancy > 0:
                excess = (target_vacancy - state.vacancy_rate) / target_vacancy
            else:
                excess = 0.0
            change_pct = clip(
                market.rent_elasticity * excess * 0.05,
                -market.max_monthly_rent_change,
                market.max_monthly_rent_change,
            )
            new_rent = max(state.avg_rent * (1 + change_pct), 0.0)
            if state.rent_cap is not None:
                new_rent = min(new_rent, state.rent_cap)
            state.avg_rent = new_rent
            if world.rent_history is not None:
                world.rent_history.setdefault(state.id, []).append(new_rent)

        for district_id, state in world.states.items():
            month_total = revenue_accum.get(district_id, 0.0)
            if district_id not in revenue_baseline:
                revenue_baseline[district_id] = month_total
            else:
                deviation = month_total - revenue_baseline[district_id]
                job_delta = round(market.spend_to_jobs * deviation)
                if job_delta != 0:
                    state.jobs = max(state.jobs + job_delta, state.filled_jobs)
            revenue_accum[district_id] = 0.0

    return changes
