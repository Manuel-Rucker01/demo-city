"""Tests for the rental-market supply dynamics in jevcity.world.rental, wired through
jevcity.world.market (apply_decisions / daily_update_ex / apply_landlord_decisions). See
world/rental.py's module docstring for the formulas these exercise."""

from __future__ import annotations

import time

import numpy as np
import pytest

from jevcity.types import (
    Action,
    Agent,
    AgentDecision,
    LandlordAction,
    LandlordDecision,
    MarketParams,
    Occupation,
    RentalSupplyParams,
    Scenario,
    Tenure,
)
from jevcity.world import market as market_mod
from jevcity.world import rental
from jevcity.world.market import (
    apply_decisions,
    apply_landlord_decisions,
    daily_update_ex,
    init_world,
)

TICKS_PER_MONTH = 30


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
    savings=20_000.0,
    spending_level=0.5,
    satisfaction=0.6,
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
        tenure=tenure,
    )


def make_population(profiles, n_per_district=40):
    agents = []
    aid = 0
    for p in profiles:
        for i in range(n_per_district):
            employed = i % 5 != 0
            tenure = Tenure.OWNER if i % 4 == 0 else Tenure.RENTER
            agents.append(
                make_agent(
                    aid,
                    p.id,
                    rent_monthly=p.avg_rent_monthly * (0.9 + 0.2 * (i % 3) / 2),
                    wage_monthly=p.income_per_capita_annual / 12,
                    employed=employed,
                    lease_start_tick=-((aid * 7) % 359),
                    tenure=tenure,
                )
            )
            aid += 1
    return agents


def make_scenario(**overrides) -> Scenario:
    overrides.setdefault("rental_supply", RentalSupplyParams(enabled=True))
    return Scenario(name="test", **overrides)


def stay_decision(agent: Agent, tick: int) -> AgentDecision:
    return AgentDecision(
        agent_id=agent.id, tick=tick, action=Action.STAY, destination=None,
        spending=agent.spending_level, satisfaction=agent.satisfaction, confidence=0.9,
    )


def assert_stock_invariant(world) -> None:
    for state in world.states.values():
        assert state.owner_units + state.rental_units + state.seasonal_units + state.tourist_units == (
            state.housing_units
        )
        assert state.owner_units >= 0
        assert state.rental_units >= 0
        assert state.seasonal_units >= 0
        assert state.renovating_units <= state.rental_units


# --- stock invariant ------------------------------------------------------------------------


def test_stock_invariant_at_init(profiles):
    agents = make_population(profiles, 40)
    world = init_world(profiles, agents)
    assert_stock_invariant(world)
    for state in world.states.values():
        assert state.owner_units > 0
        assert state.rental_units > 0


def test_stock_invariant_holds_across_many_ticks(profiles):
    rng = np.random.default_rng(11)
    agents = make_population(profiles, 30)
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    scenario = make_scenario(market=MarketParams(moving_cost=100.0))
    district_ids = [p.id for p in profiles]

    for tick in range(1, 61):
        market_mod.apply_policies(world, scenario, tick)
        decisions = []
        for a in agents:
            if tick % 7 == a.id % 7:
                dest = district_ids[(a.id + tick) % len(district_ids)]
                decisions.append(
                    AgentDecision(
                        agent_id=a.id, tick=tick, action=Action.MOVE, destination=dest,
                        spending=0.5, satisfaction=0.6, confidence=0.9,
                    )
                )
            else:
                decisions.append(stay_decision(a, tick))
        delta = apply_decisions(world, agents_dict, decisions, scenario, rng, tick)
        # Landlords always decide the same tick the engine gets the vacancy.
        landlord_decisions = [
            LandlordDecision(
                vacancy_id=v.vacancy_id, tick=tick, district=v.district,
                action=LandlordAction.RELET, confidence=0.9,
            )
            for v in delta.vacancies
        ]
        apply_landlord_decisions(world, landlord_decisions, scenario, rng, tick)
        daily_update_ex(world, agents_dict, scenario, tick, rng)
        assert_stock_invariant(world)


# --- disabled reproduces legacy behaviour exactly --------------------------------------------


def test_disabled_never_touches_rental_pools_or_emits_vacancies(profiles):
    rng = np.random.default_rng(3)
    agents = make_population(profiles, 20)
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    scenario = Scenario(name="test", rental_supply=RentalSupplyParams(enabled=False))
    district_ids = [p.id for p in profiles]

    pools_before = {
        did: (world._vacant_owner[did], world._vacant_rental[did]) for did in district_ids
    }
    stock_before = {
        did: (world.states[did].owner_units, world.states[did].rental_units, world.states[did].seasonal_units)
        for did in district_ids
    }

    for tick in range(1, 31):
        decisions = [
            AgentDecision(
                agent_id=a.id, tick=tick, action=Action.MOVE,
                destination=district_ids[(a.id + tick) % len(district_ids)],
                spending=0.5, satisfaction=0.6, confidence=0.9,
            )
            if tick % 5 == a.id % 5
            else stay_decision(a, tick)
            for a in agents
        ]
        delta = apply_decisions(world, agents_dict, decisions, scenario, rng, tick)
        assert delta.vacancies == []
        daily_update_ex(world, agents_dict, scenario, tick, rng)

    for did in district_ids:
        assert (world._vacant_owner[did], world._vacant_rental[did]) == pools_before[did]
        state = world.states[did]
        assert (state.owner_units, state.rental_units, state.seasonal_units) == stock_before[did]
    assert world.completed_today == {}


def test_determinism_same_seed_enabled(profiles):
    def run(seed):
        rng = np.random.default_rng(seed)
        agents = make_population(profiles, 20)
        world = init_world(profiles, agents)
        agents_dict = {a.id: a for a in agents}
        scenario = make_scenario()
        for tick in range(1, 20):
            decisions = [stay_decision(a, tick) for a in agents]
            delta = apply_decisions(world, agents_dict, decisions, scenario, rng, tick)
            landlord_decisions = [
                LandlordDecision(
                    vacancy_id=v.vacancy_id, tick=tick, district=v.district,
                    action=LandlordAction.RELET, confidence=0.9,
                )
                for v in delta.vacancies
            ]
            apply_landlord_decisions(world, landlord_decisions, scenario, rng, tick)
            daily_update_ex(world, agents_dict, scenario, tick, rng)
        return world

    w1 = run(55)
    w2 = run(55)
    for did in w1.states:
        assert w1.states[did].rental_units == w2.states[did].rental_units
        assert w1.states[did].owner_units == w2.states[did].owner_units
        assert w1.states[did].quality == pytest.approx(w2.states[did].quality)


# --- vacancy emission: renter departures only -------------------------------------------------


def test_vacancy_emitted_on_renter_leave_city_not_owner(profiles):
    rng = np.random.default_rng(1)
    home = profiles[0].id
    renter = make_agent(0, home, tenure=Tenure.RENTER, employed=False)
    owner = make_agent(1, home, tenure=Tenure.OWNER, employed=False)
    world = init_world(profiles, [renter, owner])
    agents_dict = {0: renter, 1: owner}
    scenario = make_scenario()

    from jevcity.types import LEAVE_CITY

    decisions = [
        AgentDecision(
            agent_id=a.id, tick=1, action=Action.MOVE, destination=LEAVE_CITY,
            spending=0.5, satisfaction=0.6, confidence=0.9,
        )
        for a in (renter, owner)
    ]
    owner_vacant_before = world._vacant_owner[home]
    delta = apply_decisions(world, agents_dict, decisions, scenario, rng, 1)

    assert len(delta.vacancies) == 1
    assert delta.vacancies[0].district == home
    assert delta.vacancies[0].last_rent == pytest.approx(renter.rent_monthly)
    assert world._vacant_owner[home] == owner_vacant_before + 1  # owner's unit freed outright


def test_vacancy_emitted_on_renter_move_not_owner_move(profiles):
    rng = np.random.default_rng(1)
    home = profiles[0].id
    dst = profiles[1].id
    renter = make_agent(0, home, tenure=Tenure.RENTER)
    owner = make_agent(1, home, tenure=Tenure.OWNER, rent_monthly=200.0)
    world = init_world(profiles, [renter, owner])
    world._vacant_rental[dst] += 2
    agents_dict = {0: renter, 1: owner}
    scenario = make_scenario(market=MarketParams(moving_cost=500.0))

    decisions = [
        AgentDecision(
            agent_id=a.id, tick=1, action=Action.MOVE, destination=dst,
            spending=0.5, satisfaction=0.6, confidence=0.9,
        )
        for a in (renter, owner)
    ]
    delta = apply_decisions(world, agents_dict, decisions, scenario, rng, 1)
    # Both end up renting at dst (owner sells+rents, per existing move rule) but only the
    # renter's OLD unit produces a landlord decision; the owner's old unit sold outright.
    assert len(delta.vacancies) == 1
    assert delta.vacancies[0].district == home
    assert renter.home == dst
    assert owner.home == dst
    assert owner.tenure == Tenure.RENTER


# --- landlord actions move units correctly ---------------------------------------------------


@pytest.mark.parametrize(
    ("action", "check"),
    [
        (LandlordAction.RELET, lambda before, after, did: after.vacant_rental == before.vacant_rental + 1
            and after.rental_units == before.rental_units),
        (LandlordAction.SELL, lambda before, after, did: after.rental_units == before.rental_units - 1
            and after.owner_units == before.owner_units + 1 and after.vacant_owner == before.vacant_owner + 1),
        (LandlordAction.SEASONAL, lambda before, after, did: after.rental_units == before.rental_units - 1
            and after.seasonal_units == before.seasonal_units + 1),
        (LandlordAction.RENOVATE, lambda before, after, did: after.renovating_units == before.renovating_units + 1
            and after.rental_units == before.rental_units),
    ],
)
def test_landlord_action_moves_units_correctly(profiles, action, check):
    class Snap:
        def __init__(self, world, did):
            s = world.states[did]
            self.rental_units = s.rental_units
            self.owner_units = s.owner_units
            self.seasonal_units = s.seasonal_units
            self.renovating_units = s.renovating_units
            self.vacant_rental = world._vacant_rental[did]
            self.vacant_owner = world._vacant_owner[did]

    rng = np.random.default_rng(1)
    agents = make_population(profiles, 10)
    world = init_world(profiles, agents)
    did = profiles[0].id
    scenario = make_scenario()

    world._pending_vacancies[999] = did
    before = Snap(world, did)
    decision = LandlordDecision(vacancy_id=999, tick=1, district=did, action=action, confidence=0.9)
    counts = apply_landlord_decisions(world, [decision], scenario, rng, 1)
    after = Snap(world, did)

    assert check(before, after, did)
    assert counts[action.value] == 1
    assert 999 not in world._pending_vacancies


# --- pending units not rentable until decided --------------------------------------------------


def test_pending_unit_not_immediately_rentable(profiles):
    rng = np.random.default_rng(1)
    a_id, b_id = profiles[0].id, profiles[1].id
    mover_out = make_agent(0, a_id, tenure=Tenure.RENTER, savings=50_000.0, wage_monthly=5000.0)
    mover_in = make_agent(1, b_id, tenure=Tenure.RENTER, savings=50_000.0, wage_monthly=5000.0)
    world = init_world(profiles, [mover_out, mover_in])
    agents_dict = {0: mover_out, 1: mover_in}
    scenario = make_scenario(market=MarketParams(moving_cost=500.0))

    world._vacant_rental[a_id] = 0  # no vacancy in A until mover_out leaves
    world._vacant_rental[b_id] = 5  # plenty of room in B

    decisions = [
        AgentDecision(
            agent_id=0, tick=1, action=Action.MOVE, destination=b_id,
            spending=0.5, satisfaction=0.6, confidence=0.9,
        ),
        AgentDecision(
            agent_id=1, tick=1, action=Action.MOVE, destination=a_id,
            spending=0.5, satisfaction=0.6, confidence=0.9,
        ),
    ]
    delta = apply_decisions(world, agents_dict, decisions, scenario, rng, 1)

    assert mover_out.home == b_id  # left A successfully (B had room)
    assert mover_in.home == b_id  # stayed put: A's freed unit was still pending, not rentable
    assert delta.failed_moves == 1
    assert len(delta.vacancies) == 1
    assert delta.vacancies[0].district == a_id
    assert world._vacant_rental[a_id] == 0  # still pending after apply_decisions


# --- missing decision defaults to RELET --------------------------------------------------------


def test_missing_landlord_decision_defaults_to_relet(profiles):
    rng = np.random.default_rng(1)
    did = profiles[0].id
    world = init_world(profiles, make_population(profiles, 5))
    scenario = make_scenario()
    world._pending_vacancies[42] = did
    vacant_before = world._vacant_rental[did]

    counts = apply_landlord_decisions(world, [], scenario, rng, 1)

    assert counts[LandlordAction.RELET.value] == 1
    assert world._vacant_rental[did] == vacant_before + 1
    assert 42 not in world._pending_vacancies


# --- renovation returns after N ticks -----------------------------------------------------------


def test_renovation_returns_after_renovation_ticks_with_quality_gain(profiles):
    rng = np.random.default_rng(1)
    did = profiles[0].id
    world = init_world(profiles, make_population(profiles, 10))
    scenario = make_scenario(rental_supply=RentalSupplyParams(enabled=True, renovation_ticks=10))
    world.states[did].quality = 0.5

    world._pending_vacancies[7] = did
    decision = LandlordDecision(vacancy_id=7, tick=1, district=did, action=LandlordAction.RENOVATE, confidence=0.9)
    apply_landlord_decisions(world, [decision], scenario, rng, 1)
    assert world.states[did].renovating_units == 1

    rental.process_renovations(world, scenario, tick=5)  # too early
    assert world.states[did].renovating_units == 1

    vacant_before = world._vacant_rental[did]
    rental.process_renovations(world, scenario, tick=11)  # 1 + 10 = 11
    assert world.states[did].renovating_units == 0
    assert world._vacant_rental[did] == vacant_before + 1
    assert world.states[did].quality > 0.5


# --- construction: completes after lag, responds to caps ----------------------------------------


def test_construction_completes_after_lag(profiles):
    did = profiles[0].id
    world = init_world(profiles, make_population(profiles, 100))
    scenario = make_scenario(
        rental_supply=RentalSupplyParams(
            enabled=True, construction_monthly_share=0.01, construction_lag_ticks=30,
            construction_rental_share=0.5,
        )
    )
    housing_before = world.states[did].housing_units
    rental.process_construction(world, scenario, tick=TICKS_PER_MONTH)  # starts
    assert world.states[did].construction_pipeline  # something queued
    assert world.states[did].housing_units == housing_before  # not completed yet

    first_entry = world.states[did].construction_pipeline[0]
    ready_tick = first_entry[0]
    rental.process_construction(world, scenario, tick=ready_tick)
    assert world.states[did].housing_units > housing_before
    assert world.completed_today.get(did, 0) > 0
    # ready_tick is itself a month boundary, so a fresh batch may start the same call the
    # first one completes; the ORIGINAL entry is gone either way.
    assert first_entry not in world.states[did].construction_pipeline


def test_construction_fewer_starts_under_binding_cap(profiles):
    did = profiles[0].id
    rs = RentalSupplyParams(enabled=True, construction_monthly_share=0.02, construction_rent_elasticity=3.0)

    world_uncapped = init_world(profiles, make_population(profiles, 20))
    scenario_uncapped = make_scenario(rental_supply=rs)
    world_uncapped.states[did].avg_rent = world_uncapped.rent_history[did][0] * 1.5  # rents rose 50%
    rental.process_construction(world_uncapped, scenario_uncapped, tick=TICKS_PER_MONTH)
    starts_uncapped = sum(u for _, u in world_uncapped.states[did].construction_pipeline)

    world_capped = init_world(profiles, make_population(profiles, 20))
    scenario_capped = make_scenario(rental_supply=rs)
    world_capped.states[did].avg_rent = world_capped.rent_history[did][0] * 1.5
    world_capped.states[did].rent_cap = world_capped.rent_history[did][0]  # cap at the ORIGINAL rent
    rental.process_construction(world_capped, scenario_capped, tick=TICKS_PER_MONTH)
    starts_capped = sum(u for _, u in world_capped.states[did].construction_pipeline)

    assert starts_capped < starts_uncapped


# --- quality decays under cap, recovers otherwise -------------------------------------------------


def test_quality_decays_under_cap_and_recovers_otherwise(profiles):
    did, other = profiles[0].id, profiles[1].id
    world = init_world(profiles, make_population(profiles, 10))
    scenario = make_scenario(
        rental_supply=RentalSupplyParams(
            enabled=True, quality_decay_capped_monthly=0.02, quality_recovery_monthly=0.01
        )
    )
    world.states[did].rent_cap = 900.0
    world.states[other].rent_cap = None

    rental.process_quality(world, scenario, tick=TICKS_PER_MONTH)

    assert world.states[did].quality == pytest.approx(1.0 - 0.02)
    assert world.states[other].quality == pytest.approx(1.0)  # already at the 1.0 cap


def test_quality_recovers_once_below_one(profiles):
    did = profiles[0].id
    world = init_world(profiles, make_population(profiles, 10))
    world.states[did].quality = 0.5
    scenario = make_scenario(rental_supply=RentalSupplyParams(enabled=True, quality_recovery_monthly=0.01))
    rental.process_quality(world, scenario, tick=TICKS_PER_MONTH)
    assert world.states[did].quality == pytest.approx(0.51)


# --- seasonal units can return -------------------------------------------------------------------


def test_seasonal_units_return_when_rent_closes_gap(profiles):
    did = profiles[0].id
    world = init_world(profiles, make_population(profiles, 10))
    world.states[did].seasonal_units = 100
    scenario = make_scenario(
        rental_supply=RentalSupplyParams(enabled=True, seasonal_rent_multiple=1.0, seasonal_capped=False)
    )
    rental_before = world.states[did].rental_units
    vacant_before = world._vacant_rental[did]

    rental.process_seasonal_return(world, scenario, tick=TICKS_PER_MONTH)

    assert world.states[did].seasonal_units < 100
    assert world.states[did].rental_units > rental_before
    assert world._vacant_rental[did] > vacant_before


def test_seasonal_units_mostly_stay_when_gap_is_wide(profiles):
    did = profiles[0].id
    world = init_world(profiles, make_population(profiles, 10))
    world.states[did].seasonal_units = 100
    scenario = make_scenario(
        rental_supply=RentalSupplyParams(enabled=True, seasonal_rent_multiple=3.0, seasonal_capped=False)
    )
    rental.process_seasonal_return(world, scenario, tick=TICKS_PER_MONTH)
    assert world.states[did].seasonal_units > 90  # small return only (share capped by the gap)


# --- below_market lock-in helper -----------------------------------------------------------------


def test_below_market(profiles):
    home = profiles[0].id
    world = init_world(profiles, make_population(profiles, 5))
    world.states[home].avg_rent = 1000.0
    cheap_renter = make_agent(0, home, tenure=Tenure.RENTER, rent_monthly=700.0)
    market_renter = make_agent(1, home, tenure=Tenure.RENTER, rent_monthly=950.0)
    owner = make_agent(2, home, tenure=Tenure.OWNER, rent_monthly=100.0)

    assert market_mod.below_market(cheap_renter, world) is True
    assert market_mod.below_market(market_renter, world) is False
    assert market_mod.below_market(owner, world) is False


# --- performance -----------------------------------------------------------------------------------


def test_performance_10000_agents_rental_supply_enabled(profiles):
    # Two separate 50ms budgets, matching test_market.py / test_world_expansion.py's own
    # per-function perf tests: apply_decisions (+ apply_landlord_decisions, same tick) and
    # daily_update_ex are each on their own budget, not summed.
    rng = np.random.default_rng(1)
    agents = make_population(profiles, 2000)  # 5 districts * 2000 = 10_000
    assert len(agents) == 10_000
    world = init_world(profiles, agents)
    agents_dict = {a.id: a for a in agents}
    scenario = make_scenario()
    decisions = [stay_decision(a, 1) for a in agents]

    start = time.perf_counter()
    delta = apply_decisions(world, agents_dict, decisions, scenario, rng, 1)
    landlord_decisions = [
        LandlordDecision(
            vacancy_id=v.vacancy_id, tick=1, district=v.district, action=LandlordAction.RELET, confidence=0.9,
        )
        for v in delta.vacancies
    ]
    apply_landlord_decisions(world, landlord_decisions, scenario, rng, 1)
    elapsed_decisions = time.perf_counter() - start
    assert elapsed_decisions < 0.05

    # tick=1 (not a month boundary), matching test_market.py /
    # test_world_expansion.py's own daily_update(_ex) perf tests: migration/shop-dynamics
    # (heavier, and not rental_supply's concern) only run monthly, so this isolates the
    # per-tick cost this module adds (process_renovations + process_construction, both
    # essentially free when nothing is mid-pipeline, plus the one extra quality term in the
    # per-agent satisfaction loop).
    start = time.perf_counter()
    daily_update_ex(world, agents_dict, scenario, 1, rng)
    elapsed_daily = time.perf_counter() - start
    assert elapsed_daily < 0.05


def test_construction_accumulates_fractional_starts_in_small_districts(profiles):
    """At agent scale a district starts < 1 unit per month; fractions must carry over."""
    agents = make_population(profiles, 20)
    world = init_world(profiles, agents)
    scenario = make_scenario()
    scenario.rental_supply.construction_lag_ticks = 30
    before = sum(s.housing_units for s in world.states.values())
    monthly = sum(
        scenario.rental_supply.construction_monthly_share * s.housing_units
        for s in world.states.values()
    )
    assert monthly < len(world.states)  # well under one unit per district per month
    for tick in range(1, 30 * 36 + 1):  # three years
        rental.process_construction(world, scenario, tick)
        assert_stock_invariant(world)
    built = sum(s.housing_units for s in world.states.values()) - before
    assert built >= int(monthly * 34) - len(world.states)  # ~35 months of completions
    assert built > 0
