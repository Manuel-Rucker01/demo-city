"""Tests for jevcity.events.triggers.detect_events."""

from __future__ import annotations

import time
from collections import Counter

import numpy as np
import pytest

from jevcity.events.triggers import detect_events
from jevcity.types import Agent, EventKind, EventParams, Occupation, Scenario, Tenure
from jevcity.world.market import daily_update, init_world


def make_scenario(**overrides) -> Scenario:
    return Scenario(name="test", **overrides)


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


def make_population(profiles, n_per_district=200) -> list[Agent]:
    agents = []
    aid = 0
    for p in profiles:
        for i in range(n_per_district):
            employed = i % 5 != 0
            agents.append(
                make_agent(
                    aid,
                    p.id,
                    occupation=Occupation.MID_SKILL,
                    rent_monthly=p.avg_rent_monthly,
                    wage_monthly=p.income_per_capita_annual / 12,
                    employed=employed,
                    lease_start_tick=-((aid * 11) % 359),
                )
            )
            aid += 1
    return agents


# --- payday staggering --------------------------------------------------------------------


def test_payday_staggering(profiles):
    rng = np.random.default_rng(11)
    agents = [make_agent(i, profiles[i % len(profiles)].id) for i in range(3000)]
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    params = EventParams(
        job_loss_daily_prob=0.0, job_offer_daily_prob=0.0, life_event_daily_prob=0.0
    )
    events = detect_events(world, agents_dict, tick=5, params=params, rng=rng)
    paydays = [e for e in events if e.kind == EventKind.PAYDAY]
    # ~1/30 of agents should have payday this tick
    assert abs(len(paydays) - len(agents) / 30) < len(agents) * 0.02
    # confirm each is exactly the staggering rule
    for e in paydays:
        assert (5 + e.agent_id) % 30 == 0


# --- lease renewal --------------------------------------------------------------------


def test_lease_renewal_fires_on_schedule_and_updates_rent(profiles):
    rng = np.random.default_rng(3)
    agent = make_agent(0, profiles[0].id, rent_monthly=800.0, lease_start_tick=0)
    world = init_world(profiles, [agent])
    world.states[profiles[0].id].avg_rent = 1200.0  # agent underpaying -> should catch up
    agents_dict = {0: agent}
    params = EventParams()

    events = detect_events(world, agents_dict, tick=params.lease_length_ticks, params=params, rng=rng)
    renewals = [e for e in events if e.kind == EventKind.LEASE_RENEWAL]
    assert len(renewals) == 1
    assert agent.rent_monthly > 800.0
    assert agent.rent_monthly <= 800.0 * 1.10 + 1e-6  # default renewal cap
    assert agent.lease_start_tick == params.lease_length_ticks


def test_lease_renewal_never_decreases_rent(profiles):
    rng = np.random.default_rng(4)
    # Agent overpaying vs market -> renewal must not lower rent below what they pay.
    agent = make_agent(0, profiles[0].id, rent_monthly=2000.0, lease_start_tick=0)
    world = init_world(profiles, [agent])
    world.states[profiles[0].id].avg_rent = 900.0
    agents_dict = {0: agent}
    params = EventParams()
    detect_events(world, agents_dict, tick=params.lease_length_ticks, params=params, rng=rng)
    assert agent.rent_monthly >= 2000.0 - 1e-6


def test_owners_never_get_lease_renewal(profiles):
    rng = np.random.default_rng(3)
    agent = make_agent(
        0, profiles[0].id, tenure=Tenure.OWNER, rent_monthly=200.0, lease_start_tick=0,
    )
    world = init_world(profiles, [agent])
    agents_dict = {0: agent}
    params = EventParams()
    events = detect_events(world, agents_dict, tick=params.lease_length_ticks, params=params, rng=rng)
    assert not any(e.kind == EventKind.LEASE_RENEWAL for e in events)
    assert agent.rent_monthly == 200.0  # housing cost untouched


# --- job loss / job offer ----------------------------------------------------------------


def test_job_loss_updates_state(profiles):
    rng = np.random.default_rng(0)
    agent = make_agent(0, profiles[0].id, employed=True)
    world = init_world(profiles, [agent])
    agents_dict = {0: agent}
    params = EventParams(job_loss_daily_prob=1.0, job_offer_daily_prob=0.0, life_event_daily_prob=0.0)
    filled_before = world.states[profiles[0].id].filled_jobs
    events = detect_events(world, agents_dict, tick=1, params=params, rng=rng)
    kinds = [e.kind for e in events]
    assert EventKind.JOB_LOSS in kinds
    assert agent.employed is False
    assert agent.job_district is None
    assert world.states[profiles[0].id].filled_jobs == filled_before - 1


def test_job_offer_requires_vacancy_and_unemployment(profiles):
    rng = np.random.default_rng(0)
    agent = make_agent(0, profiles[0].id, employed=False)
    world = init_world(profiles, [agent])
    state = world.states[profiles[0].id]
    state.jobs = state.filled_jobs + 50  # plenty of vacancies
    agents_dict = {0: agent}
    params = EventParams(job_loss_daily_prob=0.0, job_offer_daily_prob=1.0, life_event_daily_prob=0.0)
    events = detect_events(world, agents_dict, tick=1, params=params, rng=rng)
    offers = [e for e in events if e.kind == EventKind.JOB_OFFER]
    assert len(offers) == 1
    assert offers[0].payload["district"] == profiles[0].id
    # job_offer never employs the agent directly
    assert agent.employed is False


def test_students_and_retired_never_get_job_events(profiles):
    rng = np.random.default_rng(0)
    agents = [
        make_agent(0, profiles[0].id, occupation=Occupation.STUDENT, employed=False, age=20),
        make_agent(1, profiles[0].id, occupation=Occupation.RETIRED, employed=False, age=70),
    ]
    world = init_world(profiles, agents)
    for state in world.states.values():
        state.jobs = state.filled_jobs + 50
    agents_dict = {a.id: a for a in agents}
    params = EventParams(job_loss_daily_prob=1.0, job_offer_daily_prob=1.0, life_event_daily_prob=0.0)
    events = detect_events(world, agents_dict, tick=1, params=params, rng=rng)
    assert not any(e.kind in (EventKind.JOB_LOSS, EventKind.JOB_OFFER) for e in events)


# --- rent burden ----------------------------------------------------------------------


def test_rent_burden_fires_on_upward_crossing_only(profiles):
    rng = np.random.default_rng(0)
    agent = make_agent(0, profiles[0].id, wage_monthly=1000.0, rent_monthly=350.0)
    world = init_world(profiles, [agent])
    agents_dict = {0: agent}
    params = EventParams(
        job_loss_daily_prob=0.0, job_offer_daily_prob=0.0, life_event_daily_prob=0.0,
        rent_burden_threshold=0.40,
    )
    # tick 1: burden = 350/1000 = 0.35, below threshold -> no event, establishes baseline
    events1 = detect_events(world, agents_dict, tick=1, params=params, rng=rng)
    assert not any(e.kind == EventKind.RENT_BURDEN for e in events1)

    # tick 2: burden crosses above threshold
    agent.rent_monthly = 450.0  # burden = 0.45
    events2 = detect_events(world, agents_dict, tick=2, params=params, rng=rng)
    assert any(e.kind == EventKind.RENT_BURDEN for e in events2)

    # tick 3: still above threshold, must not refire
    events3 = detect_events(world, agents_dict, tick=3, params=params, rng=rng)
    assert not any(e.kind == EventKind.RENT_BURDEN for e in events3)


# --- determinism ------------------------------------------------------------------------


def test_determinism_same_seed(profiles):
    def run(seed):
        agents = make_population(profiles, 50)
        world = init_world(profiles, agents)
        agents_dict = {a.id: a for a in agents}
        rng = np.random.default_rng(seed)
        params = EventParams()
        all_events = []
        for tick in range(1, 20):
            all_events.append(detect_events(world, agents_dict, tick, params, rng))
        return all_events, agents

    events1, agents1 = run(555)
    events2, agents2 = run(555)
    for tick_events1, tick_events2 in zip(events1, events2, strict=True):
        kinds1 = [(e.agent_id, e.kind) for e in tick_events1]
        kinds2 = [(e.agent_id, e.kind) for e in tick_events2]
        assert kinds1 == kinds2
    for a1, a2 in zip(agents1, agents2, strict=True):
        assert a1.employed == a2.employed
        assert a1.rent_monthly == pytest.approx(a2.rent_monthly)


# --- event rates close to params over many ticks -----------------------------------------


def test_event_rates_close_to_params(profiles):
    rng = np.random.default_rng(2024)
    agents = make_population(profiles, 400)  # 2000 agents, all employed/unemployed mix
    world = init_world(profiles, agents)
    for state in world.states.values():
        state.jobs = state.filled_jobs + max(20, state.filled_jobs // 5)
    agents_dict = {a.id: a for a in agents}
    params = EventParams(
        job_loss_daily_prob=0.002,
        job_offer_daily_prob=0.01,
        life_event_daily_prob=0.001,
    )

    n_ticks = 200
    counts = Counter()
    employed_agent_days = 0
    unemployed_agent_days = 0
    total_agent_days = 0
    for tick in range(1, n_ticks + 1):
        events = detect_events(world, agents_dict, tick, params, rng)
        for e in events:
            counts[e.kind] += 1
        for a in agents_dict.values():
            total_agent_days += 1
            if a.employed:
                employed_agent_days += 1
            else:
                unemployed_agent_days += 1

    life_event_rate = counts[EventKind.LIFE_EVENT] / total_agent_days
    assert abs(life_event_rate - params.life_event_daily_prob) < params.life_event_daily_prob * 0.5

    job_loss_rate = counts[EventKind.JOB_LOSS] / max(employed_agent_days, 1)
    assert abs(job_loss_rate - params.job_loss_daily_prob) < params.job_loss_daily_prob * 0.6


# --- performance: daily_update + detect_events combined, 10_000 agents -------------------


def test_performance_daily_update_and_detect_events_10000_agents(profiles):
    rng = np.random.default_rng(1)
    agents = make_population(profiles, 2000)  # 10_000 agents
    assert len(agents) == 10_000
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    scenario = make_scenario()
    params = EventParams()

    start = time.perf_counter()
    detect_events(world, agents_dict, 1, params, rng)
    daily_update(world, agents_dict, scenario, 1, rng)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.05
