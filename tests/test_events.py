"""Tests for jevcity.events.triggers.detect_events."""

from __future__ import annotations

import time
from collections import Counter

import numpy as np
import pytest
from _perf import PERF_SLACK

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
    children=0,
) -> Agent:
    if employed and job_district is None:
        job_district = home
    if not employed and job_district is None:
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
        children=children,
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
    assert elapsed < 0.05 * PERF_SLACK


# --- inactive agents ------------------------------------------------------------------------


def test_inactive_agents_get_no_events(profiles):
    rng = np.random.default_rng(0)
    agent = make_agent(0, profiles[0].id, employed=True, children=0)
    agent.active = False
    world = init_world(profiles, [agent])
    agents_dict = {0: agent}
    params = EventParams(job_loss_daily_prob=1.0, job_offer_daily_prob=1.0, life_event_daily_prob=1.0)
    events = detect_events(world, agents_dict, tick=params.school_year_tick, params=params, rng=rng)
    assert events == []


# --- school year ---------------------------------------------------------------------------


def test_school_year_fires_only_for_parents_on_the_right_tick(profiles):
    rng = np.random.default_rng(0)
    parent = make_agent(0, profiles[0].id)
    parent.children = 2
    childless = make_agent(1, profiles[0].id)
    world = init_world(profiles, [parent, childless])
    agents_dict = {0: parent, 1: childless}
    params = EventParams(job_loss_daily_prob=0.0, job_offer_daily_prob=0.0, life_event_daily_prob=0.0)

    # wrong tick: no SCHOOL_YEAR at all
    events_before = detect_events(world, agents_dict, tick=params.school_year_tick - 1, params=params, rng=rng)
    assert not any(e.kind == EventKind.SCHOOL_YEAR for e in events_before)

    # right tick: only the parent
    events_on = detect_events(world, agents_dict, tick=params.school_year_tick, params=params, rng=rng)
    school_events = [e for e in events_on if e.kind == EventKind.SCHOOL_YEAR]
    assert [e.agent_id for e in school_events] == [0]

    # fires again exactly one year later
    events_next_year = detect_events(
        world, agents_dict, tick=params.school_year_tick + 365, params=params, rng=rng
    )
    school_events_next = [e for e in events_next_year if e.kind == EventKind.SCHOOL_YEAR]
    assert [e.agent_id for e in school_events_next] == [0]


# --- transit change --------------------------------------------------------------------------


def _collect_transit_events(world, agents_dict, params, start_tick, n_ticks):
    """Run detect_events for n_ticks starting at start_tick, collecting every TRANSIT_CHANGE
    event fired -> {agent_id: (tick, kind)}. Errors if an agent fires more than once."""
    by_agent: dict[int, tuple[int, str]] = {}
    rng = np.random.default_rng(0)
    for t in range(start_tick, start_tick + n_ticks):
        events = detect_events(world, agents_dict, tick=t, params=params, rng=rng)
        for e in events:
            if e.kind != EventKind.TRANSIT_CHANGE:
                continue
            assert e.agent_id not in by_agent, "agent fired TRANSIT_CHANGE more than once"
            by_agent[e.agent_id] = (t, e.payload["kind"])
    return by_agent


def test_transit_change_awareness_spreads_over_the_window_not_instant(profiles):
    """A new line shouldn't make every affected commuter reconsider the day it opens (the
    realism bug this fixes): with awareness_days > 1, affected agents' events land on
    different days within [start, start + awareness_days), not all on the opening tick."""
    awareness_days = 30
    agents = [
        make_agent(i, profiles[0].id, employed=False, occupation=Occupation.RETIRED)
        for i in range(200)
    ]
    unrelated = make_agent(1000, profiles[2].id, employed=False, occupation=Occupation.RETIRED)
    agents.append(unrelated)
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    params = EventParams(
        job_loss_daily_prob=0.0, job_offer_daily_prob=0.0, life_event_daily_prob=0.0,
        transit_awareness_days=awareness_days,
    )

    rng0 = np.random.default_rng(0)
    events1 = detect_events(world, agents_dict, tick=1, params=params, rng=rng0)  # baseline
    assert not any(e.kind == EventKind.TRANSIT_CHANGE for e in events1)

    # District 0's transit_boost increases at tick 2 -- affects agents 0..199 (home there),
    # not agent 1000 (a different district).
    world.states[profiles[0].id].transit_boost = 0.2
    fired = _collect_transit_events(world, agents_dict, params, start_tick=2, n_ticks=awareness_days + 5)

    assert set(fired) == set(range(200))  # every affected agent fires, exactly once each
    assert 1000 not in fired  # unrelated district's agent never fires

    ticks_seen = {t for t, _kind in fired.values()}
    assert len(ticks_seen) > 1, "all affected agents fired on the same day"
    assert all(2 <= t < 2 + awareness_days for t, _ in fired.values())
    assert all(kind == "new_line" for _, kind in fired.values())


def test_transit_change_awareness_is_deterministic(profiles):
    """Same seed/inputs -> the same per-agent awareness day (no dependence on the shared rng,
    only on agent id / kind / the tick the change started)."""
    awareness_days = 20

    def run():
        agents = [make_agent(i, profiles[0].id, employed=False) for i in range(50)]
        world = init_world(profiles, agents)
        agents_dict = {a.id: a for a in agents}
        params = EventParams(
            job_loss_daily_prob=0.0, job_offer_daily_prob=0.0, life_event_daily_prob=0.0,
            transit_awareness_days=awareness_days,
        )
        detect_events(world, agents_dict, tick=1, params=params, rng=np.random.default_rng(0))
        world.states[profiles[0].id].transit_boost = 0.2
        return _collect_transit_events(world, agents_dict, params, start_tick=2, n_ticks=awareness_days + 2)

    assert run() == run()


def test_low_emission_zone_change_fires_with_correct_payload(profiles):
    awareness_days = 15
    agent = make_agent(0, profiles[0].id, employed=False)
    world = init_world(profiles, [agent])
    agents_dict = {0: agent}
    params = EventParams(
        job_loss_daily_prob=0.0, job_offer_daily_prob=0.0, life_event_daily_prob=0.0,
        transit_awareness_days=awareness_days,
    )

    rng0 = np.random.default_rng(0)
    detect_events(world, agents_dict, tick=1, params=params, rng=rng0)  # baseline
    world.states[profiles[0].id].low_emission_zone = True
    fired = _collect_transit_events(world, agents_dict, params, start_tick=2, n_ticks=awareness_days + 2)
    assert fired == {0: (fired[0][0], "low_emission_zone")}
    assert 2 <= fired[0][0] < 2 + awareness_days


def test_transit_change_awareness_days_1_fires_immediately(profiles):
    """transit_awareness_days<=1 preserves the old "everyone notices on day one" behaviour."""
    home_agent = make_agent(0, profiles[0].id, employed=False)
    job_agent = make_agent(1, profiles[1].id, employed=True, job_district=profiles[0].id)
    world = init_world(profiles, [home_agent, job_agent])
    agents_dict = {a.id: a for a in [home_agent, job_agent]}
    params = EventParams(
        job_loss_daily_prob=0.0, job_offer_daily_prob=0.0, life_event_daily_prob=0.0,
        transit_awareness_days=1,
    )
    rng0 = np.random.default_rng(0)
    detect_events(world, agents_dict, tick=1, params=params, rng=rng0)
    world.states[profiles[0].id].transit_boost = 0.2
    events2 = detect_events(world, agents_dict, tick=2, params=params, rng=rng0)
    transit_events = {e.agent_id for e in events2 if e.kind == EventKind.TRANSIT_CHANGE}
    assert transit_events == {0, 1}


# --- tourism pressure ------------------------------------------------------------------------


def test_tourism_pressure_scales_with_tourist_share(profiles):
    rng = np.random.default_rng(0)
    agents = [make_agent(i, profiles[0].id, employed=False) for i in range(500)]
    world = init_world(profiles, agents)
    state = world.states[profiles[0].id]
    state.tourist_units = round(0.5 * state.housing_units)  # high tourist share
    agents_dict = {a.id: a for a in agents}
    params = EventParams(
        job_loss_daily_prob=0.0, job_offer_daily_prob=0.0, life_event_daily_prob=0.0,
        tourism_pressure_daily_prob=0.5,
    )
    events = detect_events(world, agents_dict, tick=1, params=params, rng=rng)
    n_tourism = sum(1 for e in events if e.kind == EventKind.TOURISM_PRESSURE)
    tourist_share = state.tourist_units / state.housing_units
    expected_rate = params.tourism_pressure_daily_prob * tourist_share
    assert abs(n_tourism / len(agents) - expected_rate) < 0.15

    for e in events:
        if e.kind == EventKind.TOURISM_PRESSURE:
            assert e.payload["tourist_share"] == pytest.approx(tourist_share)


def test_tourism_pressure_zero_when_no_tourist_units(profiles):
    rng = np.random.default_rng(0)
    agents = [make_agent(i, profiles[0].id, employed=False) for i in range(100)]
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    params = EventParams(
        job_loss_daily_prob=0.0, job_offer_daily_prob=0.0, life_event_daily_prob=0.0,
        tourism_pressure_daily_prob=1.0,
    )
    events = detect_events(world, agents_dict, tick=1, params=params, rng=rng)
    assert not any(e.kind == EventKind.TOURISM_PRESSURE for e in events)


def test_tourism_pressure_owners_excluded(profiles):
    rng = np.random.default_rng(0)
    agent = make_agent(0, profiles[0].id, employed=False, tenure=Tenure.OWNER)
    world = init_world(profiles, [agent])
    world.states[profiles[0].id].tourist_units = round(0.9 * world.states[profiles[0].id].housing_units)
    agents_dict = {0: agent}
    params = EventParams(
        job_loss_daily_prob=0.0, job_offer_daily_prob=0.0, life_event_daily_prob=0.0,
        tourism_pressure_daily_prob=1.0,
    )
    events = detect_events(world, agents_dict, tick=1, params=params, rng=rng)
    assert not any(e.kind == EventKind.TOURISM_PRESSURE for e in events)


# --- shop closed -----------------------------------------------------------------------------


def test_shop_closed_fires_after_a_drop(profiles):
    rng = np.random.default_rng(0)
    agents = [make_agent(i, profiles[0].id, employed=False) for i in range(400)]
    world = init_world(profiles, agents)
    state = world.states[profiles[0].id]
    state.shops_open = 100
    agents_dict = {a.id: a for a in agents}
    params = EventParams(job_loss_daily_prob=0.0, job_offer_daily_prob=0.0, life_event_daily_prob=0.0)

    events1 = detect_events(world, agents_dict, tick=1, params=params, rng=rng)  # baseline
    assert not any(e.kind == EventKind.SHOP_CLOSED for e in events1)

    state.shops_open = 80  # 20% of shops closed
    events2 = detect_events(world, agents_dict, tick=2, params=params, rng=rng)
    shop_events = [e for e in events2 if e.kind == EventKind.SHOP_CLOSED]
    assert shop_events
    for e in shop_events:
        assert e.payload["closed_pct"] == pytest.approx(0.2)
    # fires probabilistically (~0.3), not for every resident
    assert len(shop_events) < len(agents)

    # steady state afterwards: no more events without a further drop
    events3 = detect_events(world, agents_dict, tick=3, params=params, rng=rng)
    assert not any(e.kind == EventKind.SHOP_CLOSED for e in events3)
