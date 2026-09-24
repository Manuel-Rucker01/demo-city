"""Tests for jevcity.world.market."""

from __future__ import annotations

import statistics as st
import time
from collections import defaultdict

import numpy as np
import pytest

from jevcity.population.generator import generate_population
from jevcity.types import (
    Action,
    Agent,
    AgentDecision,
    CommuteMode,
    MarketParams,
    MigrationParams,
    Occupation,
    RentCapPolicy,
    Scenario,
    Tenure,
)
from jevcity.world.market import apply_decisions, apply_policies, daily_update, init_world


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
    days_unemployed=0,
    last_move_tick=None,
    tenure=Tenure.RENTER,
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
        days_unemployed=days_unemployed,
        last_move_tick=last_move_tick,
        tenure=tenure,
    )


def make_scenario(**overrides) -> Scenario:
    return Scenario(name="test", **overrides)


def make_population(profiles, n_per_district=40) -> list[Agent]:
    agents = []
    aid = 0
    for p in profiles:
        for i in range(n_per_district):
            employed = i % 5 != 0  # 80% employed
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


def stay_decision(agent: Agent, tick: int) -> AgentDecision:
    return AgentDecision(
        agent_id=agent.id,
        tick=tick,
        action=Action.STAY,
        destination=None,
        spending=agent.spending_level,
        satisfaction=agent.satisfaction,
        confidence=0.9,
    )


# --- init_world --------------------------------------------------------------------------


def test_init_world_housing_and_jobs(profiles):
    agents = make_population(profiles, 40)
    world = init_world(profiles, agents)
    n_agents = len(agents)
    assert sum(s.occupied_units for s in world.states.values()) == n_agents
    for state in world.states.values():
        assert state.occupied_units <= state.housing_units
        assert state.filled_jobs <= state.jobs
    for p in profiles:
        assert world.rent_history[p.id] == [p.avg_rent_monthly]


# --- apply_policies -----------------------------------------------------------------------


def test_apply_policies_sets_and_clears_cap(profiles):
    agents = make_population(profiles, 10)
    world = init_world(profiles, agents)
    target = profiles[0].id
    scenario = make_scenario(
        policies=[RentCapPolicy(district=target, start_tick=10, cap_monthly=1000.0)]
    )
    apply_policies(world, scenario, tick=5)
    assert world.states[target].rent_cap is None
    apply_policies(world, scenario, tick=10)
    assert world.states[target].rent_cap == 1000.0
    apply_policies(world, scenario, tick=200)
    assert world.states[target].rent_cap == 1000.0


def test_apply_policies_pct_of_initial(profiles):
    agents = make_population(profiles, 10)
    world = init_world(profiles, agents)
    target = profiles[1].id
    initial = world.rent_history[target][0]
    scenario = make_scenario(
        policies=[RentCapPolicy(district=target, start_tick=0, cap_pct_of_initial=1.1)]
    )
    apply_policies(world, scenario, tick=0)
    assert world.states[target].rent_cap == pytest.approx(initial * 1.1)


# --- housing conservation across many ticks --------------------------------------------


def test_housing_conservation_and_bounds(profiles):
    rng = np.random.default_rng(99)
    agents = make_population(profiles, 60)
    n_agents = len(agents)
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    scenario = make_scenario(market=MarketParams(moving_cost=100.0))
    district_ids = [p.id for p in profiles]

    for tick in range(1, 41):
        apply_policies(world, scenario, tick)
        decisions = []
        for a in agents:
            if tick % 7 == a.id % 7:
                dest = district_ids[(a.id + tick) % len(district_ids)]
                decisions.append(
                    AgentDecision(
                        agent_id=a.id,
                        tick=tick,
                        action=Action.MOVE,
                        destination=dest,
                        spending=0.5,
                        satisfaction=0.6,
                        confidence=0.9,
                    )
                )
            else:
                decisions.append(stay_decision(a, tick))
        apply_decisions(world, agents_dict, decisions, scenario, rng, tick)
        daily_update(world, agents_dict, scenario, tick, rng)

        # arrivals join agents_dict, departures become inactive: occupancy tracks active agents
        n_active = sum(a.active for a in agents_dict.values())
        assert n_active >= n_agents - 60  # sanity: population can't vanish
        assert sum(s.occupied_units for s in world.states.values()) == n_active
        for state in world.states.values():
            assert state.occupied_units <= state.housing_units
            assert state.filled_jobs <= state.jobs


# --- rent cap never exceeded ---------------------------------------------------------------


def test_rent_cap_never_exceeded(profiles):
    rng = np.random.default_rng(7)
    agents = make_population(profiles, 30)
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    target = profiles[0].id
    cap = profiles[0].avg_rent_monthly * 1.02
    scenario = make_scenario(
        policies=[RentCapPolicy(district=target, start_tick=0, cap_monthly=cap)],
        market=MarketParams(rent_adjust_interval=30),
    )
    for tick in range(1, 121):
        apply_policies(world, scenario, tick)
        decisions = [stay_decision(a, tick) for a in agents]
        apply_decisions(world, agents_dict, decisions, scenario, rng, tick)
        daily_update(world, agents_dict, scenario, tick, rng)
        assert world.states[target].avg_rent <= cap + 1e-6


# --- renewal freeze --------------------------------------------------------------------


def test_renewal_freeze_zero_increase(profiles):
    from jevcity.events.triggers import detect_events
    from jevcity.types import EventKind, EventParams

    agents = [make_agent(0, profiles[0].id, rent_monthly=500.0, lease_start_tick=-360)]
    world = init_world(profiles, agents)
    world.states[profiles[0].id].max_increase_pct = 0.0
    agents_dict = {a.id: a for a in agents}
    rng = np.random.default_rng(1)
    events = detect_events(world, agents_dict, tick=360, params=EventParams(), rng=rng)
    renewals = [e for e in events if e.kind == EventKind.LEASE_RENEWAL]
    assert len(renewals) == 1
    assert renewals[0].payload["increase_pct"] == pytest.approx(0.0, abs=1e-9)
    assert agents_dict[0].rent_monthly == pytest.approx(500.0)


# --- low/high vacancy rent direction -----------------------------------------------------


def test_low_vacancy_raises_high_vacancy_lowers_rent(profiles):
    rng = np.random.default_rng(3)
    agents = make_population(profiles, 40)
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    scenario = make_scenario(market=MarketParams(rent_adjust_interval=30))

    low_vac_id = profiles[0].id
    high_vac_id = profiles[1].id
    world.states[low_vac_id].housing_units = world.states[low_vac_id].occupied_units + 1
    world.states[high_vac_id].housing_units = round(world.states[high_vac_id].occupied_units / 0.5)

    start_low = world.states[low_vac_id].avg_rent
    start_high = world.states[high_vac_id].avg_rent

    for tick in range(1, 31):
        apply_policies(world, scenario, tick)
        decisions = [stay_decision(a, tick) for a in agents]
        apply_decisions(world, agents_dict, decisions, scenario, rng, tick)
        daily_update(world, agents_dict, scenario, tick, rng)

    assert world.states[low_vac_id].avg_rent > start_low
    assert world.states[high_vac_id].avg_rent < start_high


# --- move infeasibility ------------------------------------------------------------------


def test_move_infeasible_no_vacancy(profiles):
    rng = np.random.default_rng(5)
    src = make_agent(0, profiles[0].id, savings=10000.0, wage_monthly=3000.0)
    world = init_world(profiles, [src])
    dst_id = profiles[1].id
    world.states[dst_id].housing_units = world.states[dst_id].occupied_units  # no vacancy
    agents_dict = {0: src}
    scenario = make_scenario()
    decision = AgentDecision(
        agent_id=0, tick=1, action=Action.MOVE, destination=dst_id,
        spending=0.5, satisfaction=0.6, confidence=0.9,
    )
    delta = apply_decisions(world, agents_dict, [decision], scenario, rng, 1)
    assert delta.failed_moves == 1
    assert delta.moves == []
    assert src.home == profiles[0].id


def test_move_infeasible_unaffordable(profiles):
    rng = np.random.default_rng(5)
    src = make_agent(0, profiles[0].id, savings=200.0, wage_monthly=200.0)
    world = init_world(profiles, [src])
    dst_id = profiles[1].id
    world.states[dst_id].housing_units = world.states[dst_id].occupied_units + 5
    agents_dict = {0: src}
    scenario = make_scenario(market=MarketParams(moving_cost=1500.0))
    decision = AgentDecision(
        agent_id=0, tick=1, action=Action.MOVE, destination=dst_id,
        spending=0.5, satisfaction=0.6, confidence=0.9,
    )
    delta = apply_decisions(world, agents_dict, [decision], scenario, rng, 1)
    assert delta.failed_moves == 1
    assert src.home == profiles[0].id


def test_move_feasible_updates_state(profiles):
    rng = np.random.default_rng(5)
    src = make_agent(0, profiles[0].id, savings=10000.0, wage_monthly=3000.0)
    world = init_world(profiles, [src])
    dst_id = profiles[1].id
    world.states[dst_id].housing_units = world.states[dst_id].occupied_units + 5
    agents_dict = {0: src}
    scenario = make_scenario(market=MarketParams(moving_cost=500.0))
    before_src_occ = world.states[profiles[0].id].occupied_units
    before_dst_occ = world.states[dst_id].occupied_units
    decision = AgentDecision(
        agent_id=0, tick=1, action=Action.MOVE, destination=dst_id,
        spending=0.5, satisfaction=0.6, confidence=0.9,
    )
    delta = apply_decisions(world, agents_dict, [decision], scenario, rng, 1)
    assert delta.failed_moves == 0
    assert len(delta.moves) == 1
    assert src.home == dst_id
    assert world.states[profiles[0].id].occupied_units == before_src_occ - 1
    assert world.states[dst_id].occupied_units == before_dst_occ + 1


# --- owner move (sells, becomes a renter) -------------------------------------------------


def test_owner_move_becomes_renter_at_market_rent(profiles):
    rng = np.random.default_rng(5)
    src = make_agent(
        0, profiles[0].id, tenure=Tenure.OWNER, rent_monthly=200.0,
        savings=1000.0, wage_monthly=3000.0,
    )
    world = init_world(profiles, [src])
    dst_id = profiles[1].id
    world.states[dst_id].housing_units = world.states[dst_id].occupied_units + 5
    agents_dict = {0: src}
    scenario = make_scenario(market=MarketParams(moving_cost=500.0))
    decision = AgentDecision(
        agent_id=0, tick=1, action=Action.MOVE, destination=dst_id,
        spending=0.5, satisfaction=0.6, confidence=0.9,
    )
    savings_before = src.savings
    delta = apply_decisions(world, agents_dict, [decision], scenario, rng, 1)
    assert delta.failed_moves == 0
    assert src.tenure == Tenure.RENTER
    assert src.home == dst_id
    assert src.rent_monthly == pytest.approx(world.states[dst_id].avg_rent)
    assert src.lease_start_tick == 1
    # equity (24 x source avg_rent) was credited, minus the moving cost.
    expected_equity = 24 * profiles[0].avg_rent_monthly
    assert src.savings == pytest.approx(savings_before + expected_equity - 500.0)


def test_owner_move_uses_equity_for_affordability(profiles):
    """An owner with little cash but a valuable home can still afford to move: equity from
    the sale counts toward the affordability and moving-cost checks."""
    rng = np.random.default_rng(5)
    src = make_agent(
        0, profiles[0].id, tenure=Tenure.OWNER, rent_monthly=200.0,
        savings=100.0, wage_monthly=500.0,
    )
    world = init_world(profiles, [src])
    dst_id = profiles[1].id
    world.states[dst_id].housing_units = world.states[dst_id].occupied_units + 5
    agents_dict = {0: src}
    scenario = make_scenario(market=MarketParams(moving_cost=500.0))
    decision = AgentDecision(
        agent_id=0, tick=1, action=Action.MOVE, destination=dst_id,
        spending=0.5, satisfaction=0.6, confidence=0.9,
    )
    delta = apply_decisions(world, agents_dict, [decision], scenario, rng, 1)
    assert delta.failed_moves == 0
    assert src.tenure == Tenure.RENTER


# --- job search ---------------------------------------------------------------------------


def test_job_search_fills_vacancy(profiles):
    rng = np.random.default_rng(1)
    agent = make_agent(0, profiles[0].id, employed=False, wage_monthly=1800.0)
    world = init_world(profiles, [agent])
    state = world.states[profiles[0].id]
    state.jobs = state.filled_jobs + 20  # plenty of vacancies -> near-certain match
    agents_dict = {0: agent}
    scenario = make_scenario(market=MarketParams(job_match_daily_prob=1.0))
    decision = AgentDecision(
        agent_id=0, tick=1, action=Action.JOB_SEARCH, destination=None,
        spending=0.5, satisfaction=0.6, confidence=0.9,
    )
    filled_before = state.filled_jobs
    delta = apply_decisions(world, agents_dict, [decision], scenario, rng, 1)
    assert agent.employed is True
    assert agent.job_district == profiles[0].id
    assert state.filled_jobs == filled_before + 1
    assert delta.job_matches == 1


def test_filled_jobs_never_exceeds_jobs(profiles):
    rng = np.random.default_rng(2)
    agents = make_population(profiles, 30)
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    scenario = make_scenario(market=MarketParams(job_match_daily_prob=1.0))
    decisions = [
        AgentDecision(
            agent_id=a.id, tick=1, action=Action.JOB_SEARCH, destination=None,
            spending=0.5, satisfaction=0.6, confidence=0.9,
        )
        for a in agents
        if not a.employed
    ]
    apply_decisions(world, agents_dict, decisions, scenario, rng, 1)
    for state in world.states.values():
        assert state.filled_jobs <= state.jobs


# --- determinism ------------------------------------------------------------------------


def test_determinism_same_seed(profiles):
    def run(seed):
        rng = np.random.default_rng(seed)
        agents = make_population(profiles, 20)
        world = init_world(profiles, agents)
        agents_dict = {a.id: a for a in agents}
        scenario = make_scenario()
        for tick in range(1, 15):
            apply_policies(world, scenario, tick)
            decisions = [stay_decision(a, tick) for a in agents]
            apply_decisions(world, agents_dict, decisions, scenario, rng, tick)
            daily_update(world, agents_dict, scenario, tick, rng)
        return agents, world

    agents1, world1 = run(123)
    agents2, world2 = run(123)
    for a1, a2 in zip(agents1, agents2, strict=True):
        assert a1.savings == pytest.approx(a2.savings)
        assert a1.rent_monthly == pytest.approx(a2.rent_monthly)
        assert a1.satisfaction == pytest.approx(a2.satisfaction)
    for did in world1.states:
        assert world1.states[did].avg_rent == pytest.approx(world2.states[did].avg_rent)


# --- performance --------------------------------------------------------------------------


def test_performance_daily_update_10000_agents(profiles):
    # The 50ms budget is for daily_update + detect_events combined (see test_events.py);
    # this checks daily_update alone stays well inside it.
    rng = np.random.default_rng(1)
    agents = make_population(profiles, 2000)  # 5 districts * 2000 = 10_000
    n_agents = len(agents)
    assert n_agents == 10_000
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    scenario = make_scenario()

    start = time.perf_counter()
    daily_update(world, agents_dict, scenario, 1, rng)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.05


# --- satisfaction: inertia + flat no-shock drift (realism fix) --------------------------


def test_satisfaction_decision_uses_inertia_not_overwrite(profiles):
    """apply_decisions must nudge satisfaction toward the Jev answer by
    SATISFACTION_INERTIA_WEIGHT, not replace it outright (the old, overwrite-every-decision
    behaviour was the main driver of the runaway satisfaction climb -- see final report)."""
    from jevcity.world.market import SATISFACTION_INERTIA_WEIGHT

    rng = np.random.default_rng(1)
    agent = make_agent(0, profiles[0].id, satisfaction=0.4)
    world = init_world(profiles, [agent])
    agents_dict = {0: agent}
    scenario = make_scenario()
    decision = AgentDecision(
        agent_id=0, tick=1, action=Action.STAY, destination=None,
        spending=0.5, satisfaction=1.0, confidence=0.9,  # Jev says "very happy"
    )
    apply_decisions(world, agents_dict, [decision], scenario, rng, 1)
    expected = (1 - SATISFACTION_INERTIA_WEIGHT) * 0.4 + SATISFACTION_INERTIA_WEIGHT * 1.0
    assert agent.satisfaction == pytest.approx(expected)
    assert agent.satisfaction < 1.0  # not a full overwrite


def test_satisfaction_inertia_is_idempotent_when_answer_matches_current(profiles):
    """A decision that just reaffirms the agent's own current satisfaction is a no-op under
    inertia (used by test_market.py's stay_decision() helper throughout this file)."""
    rng = np.random.default_rng(1)
    agent = make_agent(0, profiles[0].id, satisfaction=0.55)
    world = init_world(profiles, [agent])
    agents_dict = {0: agent}
    scenario = make_scenario()
    apply_decisions(world, agents_dict, [stay_decision(agent, 1)], scenario, rng, 1)
    assert agent.satisfaction == pytest.approx(0.55)


def test_no_shock_satisfaction_stays_roughly_flat_over_a_year(profiles):
    """The headline realism check: a run with NO Jev decisions at all (no events, no shocks --
    only population.generator's calibrated initial satisfaction and market.py's daily drift)
    must not drift district-average satisfaction by more than 0.05 over 365 ticks. Before the
    fix (uncalibrated initial satisfaction + a steeper, uncalibrated daily-drift baseline) the
    real run in runs/base-or/ climbed by ~0.29 (0.585 -> 0.874) over the same span."""
    rng = np.random.default_rng(2024)
    agents = generate_population(profiles, 300, rng)
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    # Isolate the satisfaction-drift mechanism: no migration/shop-driven layoffs muddying the
    # signal (those are separate mechanisms, not part of this fix).
    scenario = make_scenario(migration=MigrationParams(arrivals_per_month_per_1000=0.0))

    start_by_district: dict[str, list[float]] = defaultdict(list)
    for a in agents:
        start_by_district[a.home].append(a.satisfaction)
    start_means = {d: st.mean(v) for d, v in start_by_district.items()}

    for tick in range(1, 366):
        apply_policies(world, scenario, tick)
        daily_update(world, agents_dict, scenario, tick, rng)

    end_by_district: dict[str, list[float]] = defaultdict(list)
    for a in agents_dict.values():
        if a.active:
            end_by_district[a.home].append(a.satisfaction)

    for d, start_mean in start_means.items():
        end_mean = st.mean(end_by_district[d])
        assert abs(end_mean - start_mean) < 0.05, (
            f"{d}: satisfaction drifted {start_mean:.3f} -> {end_mean:.3f} over 365 ticks"
        )


# --- commute habit (Agent.commute_since_tick) --------------------------------------------


def test_commute_decision_change_sets_since_tick(profiles):
    from jevcity.world import transport

    agent = make_agent(0, profiles[0].id, employed=True)
    agent.commute_mode = CommuteMode.CAR
    agent.has_car = True
    agent.commute_since_tick = -900  # long-standing habit

    transport.apply_commute_decision(agent, CommuteMode.METRO, tick=100)
    assert agent.commute_mode == CommuteMode.METRO
    assert agent.commute_since_tick == 100


def test_commute_decision_same_mode_keeps_since_tick(profiles):
    from jevcity.world import transport

    agent = make_agent(0, profiles[0].id, employed=True)
    agent.commute_mode = CommuteMode.METRO
    agent.commute_since_tick = -900

    transport.apply_commute_decision(agent, CommuteMode.METRO, tick=100)
    assert agent.commute_mode == CommuteMode.METRO
    assert agent.commute_since_tick == -900  # unchanged: no actual switch


def test_switch_mode_on_move_sets_since_tick(profiles):
    from jevcity.world import transport

    agent = make_agent(0, profiles[0].id, employed=True, job_district=profiles[1].id)
    agent.commute_mode = CommuteMode.WALK
    agent.commute_since_tick = -500
    agent.home = profiles[0].id  # job_district != home -> WALK is unrealistic, forces METRO

    transport.switch_mode_on_move(agent, tick=42)
    assert agent.commute_mode == CommuteMode.METRO
    assert agent.commute_since_tick == 42


def test_apply_decisions_updates_commute_since_tick_on_switch(profiles):
    agent = make_agent(0, profiles[0].id, employed=True)
    agent.commute_mode = CommuteMode.BUS
    agent.has_car = True
    agent.commute_since_tick = -1000
    world = init_world(profiles, [agent])
    agents_dict = {0: agent}
    scenario = make_scenario()
    rng = np.random.default_rng(1)
    decision = AgentDecision(
        agent_id=0, tick=50, action=Action.STAY, destination=None,
        spending=0.5, satisfaction=0.6, confidence=0.9, commute_mode=CommuteMode.CAR,
    )
    apply_decisions(world, agents_dict, [decision], scenario, rng, 50)
    assert agent.commute_mode == CommuteMode.CAR
    assert agent.commute_since_tick == 50
