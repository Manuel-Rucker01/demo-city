"""Tests for the world-expansion mechanics in jevcity.world.market: tourist flats,
transport/transit policies, local commerce, migration in/out. See that module's docstring
for the exact formulas these exercise."""

from __future__ import annotations

import time

import numpy as np
import pytest

from jevcity.types import (
    LEAVE_CITY,
    Action,
    Agent,
    AgentDecision,
    CommuteMode,
    LowEmissionZonePolicy,
    MarketParams,
    MigrationParams,
    Occupation,
    ShoppingPlace,
    Tenure,
    TouristFlatPolicy,
    TransitLinePolicy,
)
from jevcity.world import market as market_mod
from jevcity.world.market import apply_decisions, apply_policies, daily_update_ex, init_world


def make_agent(
    id,
    home,
    *,
    age=30,
    household_size=1,
    occupation=Occupation.MID_SKILL,
    wage_monthly=2000.0,
    employed=True,
    job_district=None,
    rent_monthly=1000.0,
    lease_start_tick=-30,
    savings=5000.0,
    spending_level=0.5,
    satisfaction=0.6,
    tenure=Tenure.RENTER,
    has_car=False,
    commute_mode=None,
    shopping_place=ShoppingPlace.LOCAL,
    active=True,
) -> Agent:
    if employed and job_district is None:
        job_district = home
    if not employed:
        job_district = None
    return Agent(
        id=id,
        age=age,
        household_size=household_size,
        occupation=occupation,
        wage_monthly=wage_monthly,
        employed=employed,
        job_district=job_district,
        home=home,
        rent_monthly=rent_monthly,
        lease_start_tick=lease_start_tick,
        savings=savings,
        spending_level=spending_level,
        satisfaction=satisfaction,
        tenure=tenure,
        has_car=has_car,
        commute_mode=commute_mode,
        shopping_place=shopping_place,
        active=active,
    )


def make_population(profiles, n_per_district=40) -> list[Agent]:
    agents = []
    aid = 0
    for p in profiles:
        for i in range(n_per_district):
            employed = i % 5 != 0
            agents.append(
                make_agent(
                    aid,
                    p.id,
                    rent_monthly=p.avg_rent_monthly * (0.9 + 0.2 * (i % 3) / 2),
                    wage_monthly=p.income_per_capita_annual / 12,
                    employed=employed,
                    lease_start_tick=-((aid * 7) % 359),
                )
            )
            aid += 1
    return agents


def make_scenario(**overrides):
    from jevcity.types import Scenario

    return Scenario(name="test", **overrides)


def small_population_profile(profiles, district_id, population=1000, avg_household_size=2.5, **extra):
    """A copy of `profiles` where `district_id` has a small, controlled population so
    agent_scale (agents / real households) is non-trivial for a small test population,
    instead of the ~1:1000 ratio a full Barcelona district gives a 40-100 agent test."""
    out = []
    for p in profiles:
        if p.id == district_id:
            update = {"population": population, "avg_household_size": avg_household_size, **extra}
            out.append(p.model_copy(update=update))
        else:
            out.append(p)
    return out


def tourism_profile(profiles, district_id, tourist_flats=200, population=1000, avg_household_size=2.5):
    return small_population_profile(
        profiles, district_id, population=population, avg_household_size=avg_household_size,
        tourist_flats=tourist_flats,
    )


# --- init_world: tourist units + housing conservation --------------------------------------


def test_init_world_tourist_units_and_conservation(profiles):
    target = profiles[0].id
    profiles2 = tourism_profile(profiles, target)
    agents = make_population(profiles2, 100)
    world = init_world(profiles2, agents)

    cv = world.states[target]
    assert cv.tourist_units > 0
    assert cv.housing_units > cv.occupied_units + cv.tourist_units - 1  # base residential slack

    for state in world.states.values():
        vacant = state.housing_units - state.occupied_units - state.tourist_units
        assert vacant >= 0
        assert state.occupied_units + vacant + state.tourist_units == state.housing_units


# --- TouristFlatPolicy phase-out ------------------------------------------------------------


def test_tourist_flat_phase_out_linear_and_return_to_stock(profiles):
    target = profiles[0].id
    profiles2 = tourism_profile(profiles, target)
    agents = make_population(profiles2, 100)
    world = init_world(profiles2, agents)

    initial_units = world.states[target].tourist_units
    base_housing = world._tourist_base_housing[target]
    assert initial_units > 0

    scenario = make_scenario(
        policies=[
            TouristFlatPolicy(
                districts=[target], start_tick=0, end_tick=100, reduction=1.0, return_to_rental_share=0.5
            )
        ]
    )

    apply_policies(world, scenario, tick=0)
    assert world.states[target].tourist_units == initial_units

    apply_policies(world, scenario, tick=50)
    half = round(initial_units * 0.5)
    assert world.states[target].tourist_units == pytest.approx(half, abs=1)
    removed = initial_units - world.states[target].tourist_units
    returned = round(0.5 * removed)
    assert world.states[target].housing_units == base_housing + world.states[target].tourist_units + returned

    apply_policies(world, scenario, tick=100)
    assert world.states[target].tourist_units == 0
    removed_full = initial_units
    returned_full = round(0.5 * removed_full)
    assert world.states[target].housing_units == base_housing + returned_full

    # idempotent: re-applying at the same tick doesn't change anything further.
    housing_at_100 = world.states[target].housing_units
    apply_policies(world, scenario, tick=100)
    assert world.states[target].housing_units == housing_at_100


def test_tourist_flat_phase_out_scoped_to_listed_districts(profiles):
    target = profiles[0].id
    other = profiles[1].id
    profiles2 = tourism_profile(profiles, target)
    agents = make_population(profiles2, 50)
    world = init_world(profiles2, agents)
    other_tourist_before = world.states[other].tourist_units

    scenario = make_scenario(
        policies=[TouristFlatPolicy(districts=[target], start_tick=0, end_tick=10, reduction=1.0)]
    )
    apply_policies(world, scenario, tick=10)
    assert world.states[other].tourist_units == other_tourist_before


# --- rent dynamics: tourism pressure --------------------------------------------------------


def test_rent_tourism_pressure_pushes_rent_up(profiles):
    did = profiles[0].id
    agents = make_population(profiles, 40)
    scenario = make_scenario(market=MarketParams(rent_adjust_interval=30, tourism_rent_pressure=0.5))

    world_no_tourism = init_world(profiles, agents)
    world_no_tourism.states[did].tourist_units = 0
    market_mod._adjust_rents(world_no_tourism, scenario)
    rent_no_tourism = world_no_tourism.states[did].avg_rent

    world_with_tourism = init_world(profiles, agents)
    world_with_tourism.states[did].tourist_units = round(world_with_tourism.states[did].housing_units * 0.3)
    market_mod._adjust_rents(world_with_tourism, scenario)
    rent_with_tourism = world_with_tourism.states[did].avg_rent

    assert rent_with_tourism > rent_no_tourism


# --- transit / LEZ policies ------------------------------------------------------------------


def test_transit_and_lez_policy_applied_from_start_tick(profiles):
    agents = make_population(profiles, 10)
    world = init_world(profiles, agents)
    d1, d2 = profiles[0].id, profiles[1].id
    scenario = make_scenario(
        policies=[
            TransitLinePolicy(districts=[d1], start_tick=10, transit_boost=0.3),
            LowEmissionZonePolicy(districts=[d2], start_tick=10, car_cost_monthly=80.0),
        ]
    )

    apply_policies(world, scenario, tick=5)
    assert world.states[d1].transit_boost == 0.0
    assert world.states[d2].low_emission_zone is False
    assert world.states[d2].car_cost_extra_monthly == 0.0

    apply_policies(world, scenario, tick=10)
    assert world.states[d1].transit_boost == pytest.approx(0.3)
    assert world.states[d2].low_emission_zone is True
    assert world.states[d2].car_cost_extra_monthly == pytest.approx(80.0)

    apply_policies(world, scenario, tick=200)
    assert world.states[d1].transit_boost == pytest.approx(0.3)
    assert world.states[d2].low_emission_zone is True


# --- LEAVE_CITY -------------------------------------------------------------------------------


def test_leave_city_frees_unit_and_job_and_marks_inactive(profiles):
    rng = np.random.default_rng(1)
    home = profiles[0].id
    agent = make_agent(0, home, employed=True, savings=10_000.0)
    world = init_world(profiles, [agent])
    agents_dict = {0: agent}
    scenario = make_scenario(migration=MigrationParams(leave_city_moving_cost=1000.0))

    occ_before = world.states[home].occupied_units
    filled_before = world.states[home].filled_jobs
    savings_before = agent.savings

    decision = AgentDecision(
        agent_id=0, tick=1, action=Action.MOVE, destination=LEAVE_CITY,
        spending=0.5, satisfaction=0.6, confidence=0.9,
    )
    delta = apply_decisions(world, agents_dict, [decision], scenario, rng, 1)

    assert agent.active is False
    assert world.states[home].occupied_units == occ_before - 1
    assert world.states[home].filled_jobs == filled_before - 1
    assert agent.savings == pytest.approx(savings_before - 1000.0)
    assert delta.departures == [0]


def test_leave_city_unemployed_agent_only_frees_unit(profiles):
    rng = np.random.default_rng(1)
    home = profiles[0].id
    agent = make_agent(0, home, employed=False, savings=5000.0)
    world = init_world(profiles, [agent])
    agents_dict = {0: agent}
    scenario = make_scenario()
    filled_before = world.states[home].filled_jobs

    decision = AgentDecision(
        agent_id=0, tick=1, action=Action.MOVE, destination=LEAVE_CITY,
        spending=0.5, satisfaction=0.6, confidence=0.9,
    )
    apply_decisions(world, agents_dict, [decision], scenario, rng, 1)
    assert world.states[home].filled_jobs == filled_before


# --- commute mode rules ------------------------------------------------------------------------


def test_commute_mode_car_ignored_without_car_but_applied_with_car(profiles):
    rng = np.random.default_rng(1)
    home = profiles[0].id
    agent = make_agent(0, home, employed=True, has_car=False, commute_mode=CommuteMode.METRO)
    world = init_world(profiles, [agent])
    agents_dict = {0: agent}
    scenario = make_scenario()

    decision = AgentDecision(
        agent_id=0, tick=1, action=Action.STAY, destination=None,
        spending=0.5, satisfaction=0.6, confidence=0.9, commute_mode=CommuteMode.CAR,
    )
    apply_decisions(world, agents_dict, [decision], scenario, rng, 1)
    assert agent.commute_mode == CommuteMode.METRO  # CAR ignored: no car

    agent.has_car = True
    apply_decisions(world, agents_dict, [decision], scenario, rng, 1)
    assert agent.commute_mode == CommuteMode.CAR


def test_commute_mode_switches_to_metro_when_walk_commuter_moves_out_of_job_district(profiles):
    rng = np.random.default_rng(5)
    home = profiles[0].id
    dst = profiles[1].id
    agent = make_agent(0, home, employed=True, job_district=home, commute_mode=CommuteMode.WALK, savings=10_000.0)
    world = init_world(profiles, [agent])
    world.states[dst].housing_units = world.states[dst].occupied_units + 5
    agents_dict = {0: agent}
    scenario = make_scenario(market=MarketParams(moving_cost=500.0))

    decision = AgentDecision(
        agent_id=0, tick=1, action=Action.MOVE, destination=dst,
        spending=0.5, satisfaction=0.6, confidence=0.9,
    )
    apply_decisions(world, agents_dict, [decision], scenario, rng, 1)
    assert agent.home == dst
    assert agent.commute_mode == CommuteMode.METRO


# --- shopping place routing --------------------------------------------------------------------


def test_spending_routed_local_vs_online_leak(profiles):
    rng = np.random.default_rng(1)
    home = profiles[0].id

    a_local = make_agent(0, home, shopping_place=ShoppingPlace.LOCAL, spending_level=1.0, savings=20_000.0)
    world_local = init_world(profiles, [a_local])
    daily_update_ex(world_local, {0: a_local}, make_scenario(), 1, rng)
    assert world_local.states[home].shop_revenue_daily > 0

    a_online = make_agent(1, home, shopping_place=ShoppingPlace.ONLINE, spending_level=1.0, savings=20_000.0)
    world_online = init_world(profiles, [a_online])
    daily_update_ex(world_online, {1: a_online}, make_scenario(), 1, rng)
    total = sum(s.shop_revenue_daily for s in world_online.states.values())
    assert total == pytest.approx(0.0)


def test_spending_routed_centre_splits_evenly(profiles):
    rng = np.random.default_rng(1)
    home = "nou_barris"
    agent = make_agent(0, home, shopping_place=ShoppingPlace.CENTRE, spending_level=1.0, savings=20_000.0)
    world = init_world(profiles, [agent])
    daily_update_ex(world, {0: agent}, make_scenario(), 1, rng)
    cv = world.states["ciutat_vella"].shop_revenue_daily
    eix = world.states["eixample"].shop_revenue_daily
    assert cv > 0
    assert cv == pytest.approx(eix)


def test_spending_routed_work_district(profiles):
    rng = np.random.default_rng(1)
    home, job = profiles[0].id, profiles[1].id
    agent = make_agent(
        0, home, employed=True, job_district=job,
        shopping_place=ShoppingPlace.WORK_DISTRICT, spending_level=1.0, savings=20_000.0,
    )
    world = init_world(profiles, [agent])
    daily_update_ex(world, {0: agent}, make_scenario(), 1, rng)
    assert world.states[job].shop_revenue_daily > 0
    assert world.states[home].shop_revenue_daily == pytest.approx(0.0)


# --- shop open/close and jobs follow-through ----------------------------------------------------


def test_shops_close_with_low_revenue_and_jobs_shrink(profiles):
    rng = np.random.default_rng(1)
    did = profiles[0].id
    profiles2 = small_population_profile(profiles, did)  # bump agent_scale so jobs_delta rounds nonzero
    agents = make_population(profiles2, 40)
    world = init_world(profiles2, agents)
    agents_dict = {a.id: a for a in agents}
    state = world.states[did]
    # the ratio is per shop against the profile's real count: keep them consistent
    world.profiles[did] = world.profiles[did].model_copy(update={"shops": 100})
    state.shops_open = 100
    state.jobs += 500  # plenty of slack so no layoffs are forced here
    state.shop_revenue_monthly = 1000.0
    world._shop_baseline_set = {did}
    state.shop_revenue_baseline = 2000.0  # ratio 0.5, well below shop_close_threshold (0.85)
    scenario = make_scenario(market=MarketParams(shop_monthly_change_max=0.02, jobs_per_shop=25.0))

    jobs_before = state.jobs
    changes: list = []
    closed = market_mod._apply_shop_dynamics(world, agents_dict, scenario, rng, changes)

    assert did in closed
    assert state.shops_open < 100
    assert state.jobs < jobs_before
    assert state.shop_revenue_monthly == 0.0  # reset for the next month


def test_shops_open_with_high_revenue_and_jobs_grow(profiles):
    rng = np.random.default_rng(1)
    did = profiles[0].id
    profiles2 = small_population_profile(profiles, did)
    agents = make_population(profiles2, 40)
    world = init_world(profiles2, agents)
    agents_dict = {a.id: a for a in agents}
    state = world.states[did]
    state.shops_open = 100
    state.shop_revenue_monthly = 3000.0
    world._shop_baseline_set = {did}
    state.shop_revenue_baseline = 2000.0  # ratio 1.5, above shop_open_threshold (1.10)
    scenario = make_scenario(market=MarketParams(shop_monthly_change_max=0.02, jobs_per_shop=25.0))

    jobs_before = state.jobs
    changes: list = []
    closed = market_mod._apply_shop_dynamics(world, agents_dict, scenario, rng, changes)

    assert did not in closed
    assert state.shops_open > 100
    assert state.jobs > jobs_before


def test_shop_closures_force_layoffs_when_jobs_would_fall_below_filled(profiles):
    rng = np.random.default_rng(3)
    agents = make_population(profiles, 40)
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    did = profiles[0].id
    state = world.states[did]
    state.shops_open = 1000
    state.jobs = state.filled_jobs  # zero slack
    state.shop_revenue_monthly = 100.0
    world._shop_baseline_set = {did}
    state.shop_revenue_baseline = 100_000.0  # huge shortfall -> capped at shop_monthly_change_max
    scenario = make_scenario(market=MarketParams(shop_monthly_change_max=0.5, jobs_per_shop=50.0))

    filled_before = state.filled_jobs
    changes: list = []
    closed = market_mod._apply_shop_dynamics(world, agents_dict, scenario, rng, changes)

    assert did in closed
    assert state.filled_jobs < filled_before
    assert state.jobs == state.filled_jobs
    assert any(c.employed is False for c in changes)
    for a in agents_dict.values():
        assert a.job_district != did or a.employed  # nobody left "employed here" but unemployed


def test_shop_baseline_set_on_first_month_no_change(profiles):
    rng = np.random.default_rng(1)
    agents = make_population(profiles, 10)
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    did = profiles[0].id
    state = world.states[did]
    state.shops_open = 50
    state.shop_revenue_monthly = 777.0
    scenario = make_scenario()

    shops_before = state.shops_open
    changes: list = []
    closed = market_mod._apply_shop_dynamics(world, agents_dict, scenario, rng, changes)
    assert did not in closed
    assert state.shops_open == shops_before
    assert state.shop_revenue_baseline == pytest.approx(777.0)
    assert state.shop_revenue_monthly == 0.0


# --- migration arrivals ------------------------------------------------------------------------


def test_migration_arrivals_added_and_housing_consistent(profiles, monkeypatch):
    from jevcity.population import generator as population_generator

    home = profiles[0].id

    def fake_spawn_arrivals(profiles_list, world, n, rng, start_id, tick):
        return [make_agent(start_id + i, home, employed=False, savings=1000.0) for i in range(n)]

    monkeypatch.setattr(population_generator, "spawn_arrivals", fake_spawn_arrivals, raising=False)

    rng = np.random.default_rng(1)
    agents = make_population(profiles, 20)
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    world.states[home].housing_units += 50  # make sure there's room

    scenario = make_scenario(migration=MigrationParams(arrivals_per_month_per_1000=1000.0))
    n_before = len(agents_dict)

    delta = daily_update_ex(world, agents_dict, scenario, 30, rng)

    assert len(delta.arrivals) > 0
    assert len(agents_dict) == n_before + len(delta.arrivals)
    for a in delta.arrivals:
        assert a.active is True
        assert a.id in agents_dict
    for state in world.states.values():
        assert state.occupied_units <= state.housing_units


def test_migration_arrivals_dropped_when_no_vacancy(profiles, monkeypatch):
    from jevcity.population import generator as population_generator

    home = profiles[0].id

    def fake_spawn_arrivals(profiles_list, world, n, rng, start_id, tick):
        return [make_agent(start_id + i, home, employed=False, savings=1000.0) for i in range(n)]

    monkeypatch.setattr(population_generator, "spawn_arrivals", fake_spawn_arrivals, raising=False)

    rng = np.random.default_rng(1)
    agents = make_population(profiles, 20)
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    world.states[home].housing_units = world.states[home].occupied_units  # zero vacancy

    scenario = make_scenario(migration=MigrationParams(arrivals_per_month_per_1000=1000.0))
    n_before = len(agents_dict)
    delta = daily_update_ex(world, agents_dict, scenario, 30, rng)

    assert delta.arrivals == []
    assert len(agents_dict) == n_before


def test_migration_arrivals_none_when_spawn_fn_missing(profiles, monkeypatch):
    from jevcity.population import generator as population_generator

    monkeypatch.delattr(population_generator, "spawn_arrivals", raising=False)

    rng = np.random.default_rng(1)
    agents = make_population(profiles, 20)
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    scenario = make_scenario(migration=MigrationParams(arrivals_per_month_per_1000=1000.0))

    delta = daily_update_ex(world, agents_dict, scenario, 30, rng)
    assert delta.arrivals == []


# --- inactive agents are skipped ---------------------------------------------------------------


def test_inactive_agents_untouched_by_daily_update_and_apply_decisions(profiles):
    rng = np.random.default_rng(1)
    home = profiles[0].id
    agent = make_agent(0, home, active=False, savings=123.0, satisfaction=0.4)
    world = init_world(profiles, [agent])
    agents_dict = {0: agent}
    scenario = make_scenario()

    daily_update_ex(world, agents_dict, scenario, 1, rng)
    assert agent.savings == pytest.approx(123.0)
    assert agent.satisfaction == pytest.approx(0.4)

    dst = profiles[1].id
    world.states[dst].housing_units = world.states[dst].occupied_units + 5
    decision = AgentDecision(
        agent_id=0, tick=1, action=Action.MOVE, destination=dst,
        spending=0.9, satisfaction=0.9, confidence=0.9,
    )
    apply_decisions(world, agents_dict, [decision], scenario, rng, 1)
    assert agent.home == home  # unaffected: inactive agents are skipped


# --- determinism -----------------------------------------------------------------------------


def test_determinism_with_new_mechanics(profiles):
    def run(seed):
        rng = np.random.default_rng(seed)
        agents = make_population(profiles, 20)
        for i, a in enumerate(agents):
            a.has_car = i % 3 == 0
            a.commute_mode = CommuteMode.CAR if a.has_car else CommuteMode.METRO
            a.shopping_place = [ShoppingPlace.LOCAL, ShoppingPlace.CENTRE, ShoppingPlace.ONLINE][i % 3]
        world = init_world(profiles, agents)
        agents_dict = {a.id: a for a in agents}
        scenario = make_scenario(
            policies=[
                TransitLinePolicy(districts=[profiles[0].id], start_tick=5, transit_boost=0.2),
                LowEmissionZonePolicy(districts=[profiles[1].id], start_tick=5),
            ]
        )
        for tick in range(1, 35):
            apply_policies(world, scenario, tick)
            decisions = [
                AgentDecision(
                    agent_id=a.id, tick=tick, action=Action.STAY, destination=None,
                    spending=a.spending_level, satisfaction=a.satisfaction, confidence=0.9,
                )
                for a in agents
            ]
            apply_decisions(world, agents_dict, decisions, scenario, rng, tick)
            daily_update_ex(world, agents_dict, scenario, tick, rng)
        return agents, world

    agents1, world1 = run(77)
    agents2, world2 = run(77)
    for a1, a2 in zip(agents1, agents2, strict=True):
        assert a1.savings == pytest.approx(a2.savings)
        assert a1.satisfaction == pytest.approx(a2.satisfaction)
    for did in world1.states:
        assert world1.states[did].avg_rent == pytest.approx(world2.states[did].avg_rent)
        assert world1.states[did].shops_open == world2.states[did].shops_open


# --- performance -------------------------------------------------------------------------------


def test_performance_daily_update_ex_10000_agents(profiles):
    rng = np.random.default_rng(1)
    agents = make_population(profiles, 2000)  # 5 districts * 2000 = 10_000
    assert len(agents) == 10_000
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    scenario = make_scenario()

    start = time.perf_counter()
    daily_update_ex(world, agents_dict, scenario, 1, rng)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.05
