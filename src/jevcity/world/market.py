"""World state, housing/job market dynamics and decision application. Owner: T5.

All arithmetic lives here (never ask Jev to do math). Every function is deterministic
given the numpy Generator it receives.

Formulas (documented here so README/docs can quote them verbatim):

- init_world: housing_units = residential_base + tourist_units, where residential_base =
  round(residents / (1 - vacancy_rate)) (at least `residents`) and tourist_units =
  round(profile.tourist_flats (None -> 0) * agent_scale), agent_scale = agents in the
  district / real households in the district (households ~= population / avg_household_size,
  fallback 2.45 -- see world/_helpers.py::compute_agent_scale). Tourist units are dwellings
  NOT available to a resident move (see world/_helpers.py::resident_vacancy: use it, not
  DistrictState.vacant_units, wherever code decides if a district has room for a new
  resident -- the plain `vacant_units` property can't be redefined here and now includes
  tourist-occupied units). shops_open = profile.shops (real count, not agent-scaled: local
  commerce fields on DistrictState are real-count scale, see types.py). Job vacancies are
  sized as 3-5% of filled jobs, more where jobs_per_resident is high (a rough proxy for
  a "hot" job market): vacancy_fraction = 0.03 + 0.02 * min(jobs_per_resident, 1.0).
- apply_policies:
  - RentCapPolicy sets state.rent_cap (absolute cap_monthly, or
    cap_pct_of_initial * rent_history[id][0]) and state.max_increase_pct once
    tick >= start_tick; otherwise both are cleared (None).
  - TouristFlatPolicy (see world/tourism.py): tourist_units shrink linearly between
    start_tick/end_tick from their initial (init_world) level to
    (1 - reduction) * initial; return_to_rental_share of every unit removed so far re-joins
    the residential stock as a vacant unit (added to housing_units, which is otherwise fixed
    at residential_base + tourist_units). Pure function of tick -> idempotent.
  - TransitLinePolicy -> state.transit_boost; LowEmissionZonePolicy -> state.low_emission_zone
    + state.car_cost_extra_monthly (world/transport.py). Both reset to "off" at the top of
    apply_policies, like the rent cap, then set for districts named by an active policy.
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
- apply_decisions / MOVE: feasible when the destination has a resident_vacancy() > 0 (unless
  it's the same district - a relet, always allowed), new_rent = min(dst.avg_rent,
  dst.rent_cap) is affordable (new_rent <= 0.6 * income OR savings >= 6 * new_rent),
  and savings >= moving_cost. **Owners who move**: they sell the current home and become
  a renter at the destination's market rent (owners are never affected by rent caps or
  the destination's affordability check uses savings *plus* the sale proceeds, since the
  sale and the new lease happen as part of the same transaction): equity =
  min(OWNER_EQUITY_RENT_MULTIPLE (24) * source district's avg_rent, OWNER_EQUITY_CAP
  (500_000)), added to savings alongside the usual `-moving_cost`; `tenure` flips to
  RENTER, `rent_monthly` becomes the destination's market rent (a lease, not a housing
  cost) and `lease_start_tick` resets to this tick like any new lease. Housing stock is a
  single pool of units per district that owners and renters share (see init_world); the
  simulation doesn't separately track a "for sale" stock, so rental vacancy is
  approximated by the district's overall resident_vacancy(). A WALK commuter whose job
  district differs from their new home switches to METRO (walking cross-district isn't
  realistic) -- world/transport.py::switch_mode_on_move.
- apply_decisions / destination == LEAVE_CITY: the household leaves Barcelona.
  agent.active = False, its home unit is freed (occupied_units -= 1), its job (if any) is
  freed (filled_jobs -= 1), savings -= scenario.migration.leave_city_moving_cost, and the
  agent id is recorded in TickDelta.departures. Inactive agents are skipped by every later
  function (apply_decisions, daily_update_ex) for the rest of the run.
- apply_decisions / commute_mode, shopping_place: a non-None commute_mode decision from an
  employed agent replaces agent.commute_mode, except CAR is ignored for agents without a car
  (world/transport.py::apply_commute_decision). A non-None shopping_place decision replaces
  agent.shopping_place directly.
- apply_decisions / JOB_SEARCH: best district = home if it has vacancies, else a
  vacancy-weighted random pick among districts with vacancies; success probability =
  job_match_daily_prob * min(1, vacancies_in_best_district / JOB_SEARCH_VACANCY_SCALE).
- daily_update_ex: savings += (income - rent - living_costs - commute_cost -
  discretionary)/30, where living_costs = 400 + 150 * household_size, commute_cost (monthly,
  world/transport.py::commute_cost_monthly) is market.car_cost_monthly (+
  car_cost_extra_monthly if home or job district has an active LEZ) for CAR commuters,
  market.public_transport_monthly for METRO/BUS, 0 for BIKE/WALK/not commuting, and
  discretionary = spending_level * max(income - rent - living_costs - commute_cost, 0) * 0.5.
  Discretionary spend is routed to district shop revenue by ShoppingPlace
  (world/commerce.py::spending_targets): LOCAL -> home; WORK_DISTRICT -> job district (home
  if none); CENTRE -> split 50/50 ciutat_vella/eixample; ONLINE -> leaks out of local commerce
  entirely. shop_revenue_daily accumulates the agent-unit EUR (as before); shop_revenue_monthly
  additionally accumulates the same amount scaled to real EUR by dividing by the spending
  agent's own district agent_scale (world/commerce.py::to_real_eur) -- the documented factor
  that keeps shop_revenue_monthly at a plausible real-EUR order of magnitude. Satisfaction
  drifts 2%/day toward a burden-implied baseline = clip(1 - rent_burden / 1.5, 0, 1), nudged up
  by TRANSIT_SATISFACTION_WEIGHT * clip(transit_score + transit_boost, 0, 1) and down by
  TOURISM_SATISFACTION_WEIGHT * (tourist_units / housing_units); a flat
  SHOP_CLOSURE_SATISFACTION_PENALTY additionally hits residents of a district the month its
  shops close.
- daily_update_ex / rent adjustment (every rent_adjust_interval ticks): excess demand ratio
  = (target_vacancy - vacancy_rate) / target_vacancy + tourism_rent_pressure *
  (tourist_units / housing_units) (tourism adds extra pressure on top of the plain vacancy
  signal); monthly change = clip(rent_elasticity * excess * 0.05, +-max_monthly_rent_change),
  then clamp to rent_cap. With the default rent_elasticity=0.6 and target_vacancy=0.05, a
  district at ~1% vacancy (excess=0.8) changes by +2.4%/month and one at ~10% vacancy
  (excess=-1.0) changes by -3%/month (clipped at max_monthly_rent_change=0.03).
- daily_update_ex / shop open-close (every TICKS_PER_MONTH ticks, world/commerce.py): once a
  shop_revenue_baseline exists (set from the first full month's shop_revenue_monthly), ratio =
  shop_revenue_monthly / shop_revenue_baseline; below shop_close_threshold, shops_open shrinks
  by clip(shop_close_threshold - ratio, 0, shop_monthly_change_max) (proportional to the
  shortfall, capped); above shop_open_threshold it grows symmetrically. Each shop opened or
  closed changes jobs by jobs_per_shop * agent_scale (real jobs -> agent units, rounded);
  jobs never drops below filled_jobs -- if it would, the shortfall's worth of employed agents
  in that district are laid off first (uniformly at random via rng, AgentChange emitted).
  This supersedes the older, simpler spend_to_jobs mechanic (MarketParams.spend_to_jobs is no
  longer read; see the final report).
- daily_update_ex / migration (every TICKS_PER_MONTH ticks): n ~ Poisson(
  migration.arrivals_per_month_per_1000 * active_agents / 1000). New agents come from
  population.generator.spawn_arrivals (looked up via getattr so this module still works if
  that function isn't built yet -- 0 arrivals in that case); an arrival is dropped if its home
  district has no resident_vacancy() left, otherwise it's added to `agents`,
  occupied_units += 1 in its home district and filled_jobs += 1 in its job district if
  employed, and returned via TickDelta.arrivals.
"""

from __future__ import annotations

import numpy as np

from jevcity.population import generator as population_generator
from jevcity.types import (
    LEAVE_CITY,
    TICKS_PER_MONTH,
    Action,
    Agent,
    AgentChange,
    AgentDecision,
    DistrictProfile,
    DistrictState,
    LowEmissionZonePolicy,
    MoveRecord,
    RentCapPolicy,
    Scenario,
    Tenure,
    TickDelta,
    TouristFlatPolicy,
    TransitLinePolicy,
    World,
)
from jevcity.world import commerce, tourism, transport
from jevcity.world._helpers import clip, compute_agent_scale, is_working_age, resident_vacancy

RENEWAL_INCREASE_CAP_DEFAULT = 0.10  # fallback when no policy sets max_increase_pct
JOB_SEARCH_VACANCY_SCALE = 10.0  # vacancies at which job_search success prob saturates
OWNER_EQUITY_RENT_MULTIPLE = 24.0  # equity realized on sale ~= 24 months of source avg_rent
OWNER_EQUITY_CAP = 500_000.0

TRANSIT_SATISFACTION_WEIGHT = 0.05  # nudge on the satisfaction baseline for good transit
TOURISM_SATISFACTION_WEIGHT = 0.15  # nudge down for a high tourist share of the housing stock
SHOP_CLOSURE_SATISFACTION_PENALTY = 0.05  # flat hit to residents the month local shops close


def init_world(profiles: list[DistrictProfile], agents: list[Agent]) -> World:
    """Build DistrictStates in agent units from the initial population.

    housing_units = residential_base + tourist_units; jobs sized from employed agents working
    there plus vacancies implied by unemployment_rate and jobs_per_resident. See module
    docstring for the exact formulas.
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
    agent_scale: dict[str, float] = {}
    tourist_base_housing: dict[str, int] = {}
    initial_tourist_units: dict[str, int] = {}

    for p in profiles:
        r = residents[p.id]
        vacancy_rate = clip(p.vacancy_rate, 0.0, 0.95)
        residential_base = max(round(r / (1 - vacancy_rate)), r)
        scale = compute_agent_scale(p, r)
        agent_scale[p.id] = scale
        t_units = tourism.initial_tourist_units(p, scale)
        tourist_base_housing[p.id] = residential_base
        initial_tourist_units[p.id] = t_units

        fj = filled_jobs[p.id]
        vacancy_fraction = 0.03 + 0.02 * min(p.jobs_per_resident, 1.0)
        vacancies = round(fj * vacancy_fraction)
        jobs = fj + vacancies
        states[p.id] = DistrictState(
            id=p.id,
            avg_rent=p.avg_rent_monthly,
            housing_units=residential_base + t_units,
            occupied_units=r,
            jobs=jobs,
            filled_jobs=fj,
            tourist_units=t_units,
            shops_open=p.shops,
        )
        rent_history[p.id] = [p.avg_rent_monthly]

    world = World(
        profiles={p.id: p for p in profiles},
        states=states,
        tick=0,
        rent_history=rent_history,
    )
    world._agent_scale = agent_scale  # type: ignore[attr-defined]
    world._tourist_base_housing = tourist_base_housing  # type: ignore[attr-defined]
    world._initial_tourist_units = initial_tourist_units  # type: ignore[attr-defined]
    return world


def apply_policies(world: World, scenario: Scenario, tick: int) -> None:
    """Activate/deactivate scenario policies on DistrictState for this tick (idempotent)."""
    for state in world.states.values():
        state.rent_cap = None
        state.max_increase_pct = None
        transport.reset_transport_state(state)

    tourist_base_housing: dict[str, int] = getattr(world, "_tourist_base_housing", {})
    initial_tourist_units: dict[str, int] = getattr(world, "_initial_tourist_units", {})

    for policy in scenario.policies:
        if isinstance(policy, RentCapPolicy):
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

        elif isinstance(policy, TouristFlatPolicy):
            for district_id, state in world.states.items():
                if not tourism.applies_to(policy, district_id):
                    continue
                initial_units = initial_tourist_units.get(district_id, state.tourist_units)
                base_housing = tourist_base_housing.get(
                    district_id, state.housing_units - state.tourist_units
                )
                tourism.apply_phase_out(state, policy, tick, initial_units, base_housing)

        elif isinstance(policy, TransitLinePolicy):
            for district_id in policy.districts:
                state = world.states.get(district_id)
                if state is not None:
                    transport.apply_transit_policy(state, policy, tick)

        elif isinstance(policy, LowEmissionZonePolicy):
            for district_id in policy.districts:
                state = world.states.get(district_id)
                if state is not None:
                    transport.apply_lez_policy(state, policy, tick)


def apply_decisions(
    world: World,
    agents: dict[int, Agent],
    decisions: list[AgentDecision],
    scenario: Scenario,
    rng: np.random.Generator,
    tick: int,
) -> TickDelta:
    """Apply Jev decisions: moves (need vacancy + affordability + moving cost), job search
    (matching against vacancies), spend/save (spending_level), satisfaction update, plus
    commute_mode/shopping_place and LEAVE_CITY (see module docstring)."""
    moves: list[MoveRecord] = []
    changes: list[AgentChange] = []
    departures: list[int] = []
    failed_moves = 0
    job_matches = 0
    market = scenario.market

    for decision in decisions:
        agent = agents.get(decision.agent_id)
        if agent is None or not agent.active:
            continue

        old_employed = agent.employed
        old_satisfaction = agent.satisfaction

        if decision.action == Action.MOVE and decision.destination == LEAVE_CITY:
            src_state = world.states.get(agent.home)
            if src_state is not None:
                src_state.occupied_units = max(src_state.occupied_units - 1, 0)
            if agent.employed and agent.job_district is not None:
                job_state = world.states.get(agent.job_district)
                if job_state is not None:
                    job_state.filled_jobs = max(job_state.filled_jobs - 1, 0)
            agent.active = False
            agent.employed = False
            agent.savings -= scenario.migration.leave_city_moving_cost
            departures.append(agent.id)
            changes.append(AgentChange(agent_id=agent.id, employed=False))
            continue

        if decision.action == Action.MOVE:
            dst_id = decision.destination
            dst_state = world.states.get(dst_id) if dst_id else None
            src_home = agent.home
            src_state = world.states.get(src_home)
            if dst_state is None or src_state is None:
                failed_moves += 1
            else:
                same_district = dst_id == src_home
                if not same_district and resident_vacancy(dst_state) <= 0:
                    failed_moves += 1
                else:
                    new_rent = dst_state.avg_rent
                    if dst_state.rent_cap is not None:
                        new_rent = min(new_rent, dst_state.rent_cap)
                    income = agent.income_monthly
                    was_owner = agent.tenure is Tenure.OWNER
                    # An owner sells as part of the same move; the sale proceeds (equity)
                    # count toward affording the new lease. See module docstring.
                    equity = (
                        min(OWNER_EQUITY_RENT_MULTIPLE * src_state.avg_rent, OWNER_EQUITY_CAP)
                        if was_owner
                        else 0.0
                    )
                    effective_savings = agent.savings + equity
                    affordable = new_rent <= 0.6 * income or effective_savings >= 6 * new_rent
                    can_pay_moving = effective_savings >= market.moving_cost
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
                        agent.savings += equity - market.moving_cost
                        if was_owner:
                            agent.tenure = Tenure.RENTER
                        transport.switch_mode_on_move(agent)
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

        transport.apply_commute_decision(agent, decision.commute_mode)
        if decision.shopping_place is not None:
            agent.shopping_place = decision.shopping_place

        change_fields: dict[str, float | bool] = {}
        if abs(agent.satisfaction - old_satisfaction) > 0.05:
            change_fields["satisfaction"] = agent.satisfaction
        if agent.employed != old_employed:
            change_fields["employed"] = agent.employed
        if change_fields:
            changes.append(AgentChange(agent_id=agent.id, **change_fields))

    return TickDelta(
        moves=moves,
        changes=changes,
        failed_moves=failed_moves,
        job_matches=job_matches,
        departures=departures,
    )


def _adjust_rents(world: World, scenario: Scenario) -> None:
    market = scenario.market
    target_vacancy = market.target_vacancy
    for state in world.states.values():
        if target_vacancy > 0:
            excess = (target_vacancy - state.vacancy_rate) / target_vacancy
            if state.housing_units > 0:
                excess += market.tourism_rent_pressure * (state.tourist_units / state.housing_units)
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


def _layoff(agents: dict[int, Agent], district_id: str, n: int, rng: np.random.Generator) -> list[AgentChange]:
    candidates = [
        a for a in agents.values() if a.active and a.employed and a.job_district == district_id
    ]
    if not candidates or n <= 0:
        return []
    n = min(n, len(candidates))
    idx = rng.choice(len(candidates), size=n, replace=False)
    out: list[AgentChange] = []
    for i in idx:
        a = candidates[int(i)]
        a.employed = False
        a.job_district = None
        a.days_unemployed = 0
        out.append(AgentChange(agent_id=a.id, employed=False))
    return out


def _apply_shop_dynamics(
    world: World,
    agents: dict[int, Agent],
    scenario: Scenario,
    rng: np.random.Generator,
    changes: list[AgentChange],
) -> set[str]:
    """Monthly shop open/close + jobs follow-through. Returns districts whose shops closed
    this month (for the satisfaction penalty). See module docstring."""
    market = scenario.market
    agent_scale: dict[str, float] = getattr(world, "_agent_scale", {})
    baseline_set: set[str] = getattr(world, "_shop_baseline_set", None)
    if baseline_set is None:
        baseline_set = set()
        world._shop_baseline_set = baseline_set  # type: ignore[attr-defined]

    closed_districts: set[str] = set()

    for district_id, state in world.states.items():
        month_total = state.shop_revenue_monthly
        if district_id not in baseline_set:
            state.shop_revenue_baseline = month_total
            baseline_set.add(district_id)
            state.shop_revenue_monthly = 0.0
            continue

        # Compare revenue PER SHOP with the baseline month's revenue per shop (shop count at
        # baseline = the profile's real count). Comparing district totals made opening shops
        # self-reinforcing (+15-25%/yr everywhere); per-shop revenue dilutes as shops open,
        # so openings/closures settle where each shop earns about what it did at baseline.
        baseline = state.shop_revenue_baseline
        base_shops = world.profiles[district_id].shops or 1
        base_per_shop = baseline / base_shops if baseline > 0 else 0.0
        per_shop = month_total / max(state.shops_open, 1)
        ratio = per_shop / base_per_shop if base_per_shop > 0 else 1.0
        frac = commerce.shops_close_open_fraction(
            ratio, market.shop_close_threshold, market.shop_open_threshold, market.shop_monthly_change_max
        )
        if frac != 0.0 and state.shops_open > 0:
            shops_delta = round(state.shops_open * frac)
            if shops_delta != 0:
                state.shops_open = max(state.shops_open + shops_delta, 0)
                scale = agent_scale.get(district_id, 0.0)
                jobs_delta = round(market.jobs_per_shop * scale * shops_delta)
                new_jobs = state.jobs + jobs_delta
                if new_jobs < state.filled_jobs:
                    shortage = state.filled_jobs - new_jobs
                    changes.extend(_layoff(agents, district_id, shortage, rng))
                    state.filled_jobs = max(state.filled_jobs - shortage, 0)
                state.jobs = max(new_jobs, state.filled_jobs)
                if shops_delta < 0:
                    closed_districts.add(district_id)

        state.shop_revenue_monthly = 0.0

    return closed_districts


def _spawn_arrivals(
    world: World, agents: dict[int, Agent], scenario: Scenario, tick: int, rng: np.random.Generator
) -> list[Agent]:
    """Monthly migration arrivals (see module docstring). Silently produces 0 arrivals if
    population.generator.spawn_arrivals hasn't been implemented yet."""
    spawn_fn = getattr(population_generator, "spawn_arrivals", None)
    if spawn_fn is None:
        return []

    migration = scenario.migration
    active_count = sum(1 for a in agents.values() if a.active)
    rate = migration.arrivals_per_month_per_1000 * active_count / 1000.0
    if rate <= 0:
        return []
    n = int(rng.poisson(rate))
    if n <= 0:
        return []

    start_id = (max(agents.keys()) + 1) if agents else 0
    profiles_list = list(world.profiles.values())
    new_agents = spawn_fn(profiles_list, world, n, rng, start_id, tick)

    accepted: list[Agent] = []
    for agent in new_agents:
        state = world.states.get(agent.home)
        if state is None or resident_vacancy(state) <= 0:
            continue  # no room for this arrival: it doesn't materialize
        agent.active = True
        agent.arrived_tick = tick
        state.occupied_units += 1
        if agent.employed and agent.job_district is not None:
            job_state = world.states.get(agent.job_district)
            if job_state is not None:
                job_state.filled_jobs += 1
        agents[agent.id] = agent
        accepted.append(agent)
    return accepted


def daily_update_ex(
    world: World, agents: dict[int, Agent], scenario: Scenario, tick: int, rng: np.random.Generator
) -> TickDelta:
    """Daily accounting, commuting cost, local-commerce spending, satisfaction drift and,
    monthly, rent adjustment, shop open/close (+ jobs), and migration arrivals. See module
    docstring for every formula."""
    changes: list[AgentChange] = []
    market = scenario.market
    agent_scale: dict[str, float] = getattr(world, "_agent_scale", {})

    for state in world.states.values():
        state.shop_revenue_daily = 0.0

    for agent in agents.values():
        if not agent.active:
            continue

        home_state = world.states.get(agent.home)
        job_state = world.states.get(agent.job_district) if agent.job_district else None

        income = agent.income_monthly
        rent = agent.rent_monthly
        living_costs = 400.0 + 150.0 * agent.household_size
        commute_cost = transport.commute_cost_monthly(agent, home_state, job_state, market)
        residual = income - rent - living_costs - commute_cost
        discretionary = agent.spending_level * max(residual, 0.0) * 0.5
        daily_net = (income - rent - living_costs - commute_cost - discretionary) / 30.0
        agent.savings += daily_net

        daily_spend = discretionary / 30.0
        if daily_spend > 0:
            targets = commerce.spending_targets(agent.shopping_place, agent.home, agent.job_district)
            scale = agent_scale.get(agent.home, 0.0)
            for district_id, share in targets.items():
                dstate = world.states.get(district_id)
                if dstate is None:
                    continue
                amount = daily_spend * share
                dstate.shop_revenue_daily += amount
                dstate.shop_revenue_monthly += commerce.to_real_eur(amount, scale)

        if (not agent.employed) and is_working_age(agent.occupation):
            agent.days_unemployed += 1

        burden = agent.rent_burden
        baseline = clip(1.0 - min(burden, 1.5) / 1.5, 0.0, 1.0)
        if home_state is not None:
            profile = world.profiles.get(agent.home)
            transit_score = profile.transit_score if profile is not None else 0.0
            effective_transit = clip(transit_score + home_state.transit_boost, 0.0, 1.0)
            baseline = clip(baseline + TRANSIT_SATISFACTION_WEIGHT * effective_transit, 0.0, 1.0)
            tourist_share = (
                home_state.tourist_units / home_state.housing_units if home_state.housing_units else 0.0
            )
            baseline = clip(baseline - TOURISM_SATISFACTION_WEIGHT * tourist_share, 0.0, 1.0)

        old_satisfaction = agent.satisfaction
        agent.satisfaction = clip(agent.satisfaction + (baseline - agent.satisfaction) * 0.02, 0.0, 1.0)
        if abs(agent.satisfaction - old_satisfaction) > 0.05:
            changes.append(AgentChange(agent_id=agent.id, satisfaction=agent.satisfaction))

    arrivals: list[Agent] = []

    if tick > 0 and tick % TICKS_PER_MONTH == 0:
        closed = _apply_shop_dynamics(world, agents, scenario, rng, changes)
        if closed:
            for agent in agents.values():
                if not agent.active or agent.home not in closed:
                    continue
                old = agent.satisfaction
                agent.satisfaction = clip(
                    agent.satisfaction - SHOP_CLOSURE_SATISFACTION_PENALTY, 0.0, 1.0
                )
                if abs(agent.satisfaction - old) > 0.05:
                    changes.append(AgentChange(agent_id=agent.id, satisfaction=agent.satisfaction))

        arrivals = _spawn_arrivals(world, agents, scenario, tick, rng)

    if tick > 0 and tick % market.rent_adjust_interval == 0:
        _adjust_rents(world, scenario)

    return TickDelta(moves=[], changes=changes, arrivals=arrivals, departures=[])


def daily_update(
    world: World, agents: dict[int, Agent], scenario: Scenario, tick: int, rng: np.random.Generator
) -> list[AgentChange]:
    """Thin backward-compatible wrapper around daily_update_ex (see its docstring)."""
    return daily_update_ex(world, agents, scenario, tick, rng).changes
