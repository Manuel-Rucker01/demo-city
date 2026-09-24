"""Token-budget test over a realistic 10-district population + mixed event mix. Owner: T4.

Verifies the whole point of the district-shortlist / conditional-question design in
state_builder.py: even with 10 districts and 6 possible questions, K=1 requests stay inside
QUESTION_NAMES's documented budget (TOKEN_BUDGET_AVG / TOKEN_BUDGET_MAX real tokens, estimated
the same way jev/meter.py estimates real requests).
"""

from __future__ import annotations

import numpy as np
import pytest

from jevcity.jev.cachekey import request_body
from jevcity.jev.meter import estimate_tokens
from jevcity.prompts.buckets import own_commute_length_text
from jevcity.prompts.state_builder import (
    _select_relevant_districts,
    build_requests,
    wants_commute_question,
    wants_shopping_question,
)
from jevcity.types import (
    AGE_BUCKETS,
    JEV_MODEL_ID,
    LEAVE_CITY,
    TOKEN_BUDGET_AVG,
    TOKEN_BUDGET_MAX,
    Agent,
    CommuteMode,
    DistrictProfile,
    DistrictState,
    Event,
    EventKind,
    Occupation,
    ShoppingPlace,
    Source,
    Tenure,
    World,
    question_key,
)

# 5 more districts beyond tests/conftest.py's 5, so the world has all 10 BCN_DISTRICTS.
_EXTRA_ROWS = [
    # id, name, pop, income/yr, rent, vacancy, unemp, jobs/res, shops, transit, lon, lat
    ("sants_montjuic", "Sants-Montjuïc", 180_000, 19_000, 950, 0.05, 0.10, 0.65, 5_500, 0.75, 2.146, 41.373),
    ("les_corts", "Les Corts", 82_000, 30_000, 1_300, 0.03, 0.05, 1.20, 3_000, 0.85, 2.132, 41.386),
    ("sarria_sant_gervasi", "Sarrià-Sant Gervasi", 150_000, 38_000, 1_600, 0.03, 0.04, 0.90, 4_500, 0.70, 2.121, 41.401),
    ("horta_guinardo", "Horta-Guinardó", 170_000, 15_000, 850, 0.05, 0.11, 0.40, 4_000, 0.55, 2.163, 41.427),
    ("sant_andreu", "Sant Andreu", 150_000, 16_000, 900, 0.05, 0.10, 0.60, 4_500, 0.65, 2.190, 41.435),
]


def make_10_profiles(base_profiles: list[DistrictProfile]) -> list[DistrictProfile]:
    ages = dict(zip(AGE_BUCKETS, (0.14, 0.24, 0.24, 0.19, 0.19)))
    extra = []
    for i, n, pop, inc, rent, vac, un, jpr, shops, tr, lon, lat in _EXTRA_ROWS:
        extra.append(
            DistrictProfile(
                id=i, name=n, population=pop, age_distribution=ages,
                income_per_capita_annual=inc, avg_rent_monthly=rent, vacancy_rate=vac,
                unemployment_rate=un, jobs_per_resident=jpr, shops=shops, transit_score=tr,
                centroid=(lon, lat),
                sources={f: Source.PLAUSIBLE for f in (
                    "population", "age_distribution", "income_per_capita_annual",
                    "avg_rent_monthly", "vacancy_rate", "unemployment_rate",
                    "jobs_per_resident", "shops", "transit_score")},
            )
        )
    return list(base_profiles) + extra


def make_world_10(profiles: list[DistrictProfile], rng: np.random.Generator) -> World:
    states = {}
    for i, p in enumerate(profiles):
        # A realistic mix: some districts get tourism pressure, some LEZ, some a transit boost,
        # some shops closing/opening -- so the token-cost of those conditional fields is exercised.
        tourist_units = int(300 * rng.random()) if i % 3 == 0 else 0
        shops_open = int(p.shops * rng.uniform(0.8, 1.15)) if i % 2 == 0 else 0
        transit_boost = 0.15 if i % 4 == 0 else 0.0
        lez = i % 5 == 0
        states[p.id] = DistrictState(
            id=p.id,
            avg_rent=p.avg_rent_monthly,
            housing_units=1000,
            occupied_units=int(1000 * (1 - p.vacancy_rate)),
            jobs=int(p.population * p.jobs_per_resident / 50),
            filled_jobs=int(p.population * p.jobs_per_resident / 50 * (1 - p.unemployment_rate)),
            tourist_units=tourist_units,
            shops_open=shops_open,
            transit_boost=transit_boost,
            low_emission_zone=lez,
            car_cost_extra_monthly=60.0 if lez else 0.0,
        )
    rent_history = {p.id: [p.avg_rent_monthly * 0.95, p.avg_rent_monthly] for p in profiles}
    return World(profiles={p.id: p for p in profiles}, states=states, tick=200, rent_history=rent_history)


_EVENT_KINDS = list(EventKind)


def make_random_agent(agent_id: int, district_ids: list[str], rng: np.random.Generator) -> Agent:
    occupations = list(Occupation)
    occ = occupations[rng.integers(0, len(occupations))]
    employed = occ not in (Occupation.RETIRED, Occupation.STUDENT) and bool(rng.random() < 0.9)
    home = district_ids[rng.integers(0, len(district_ids))]
    job_district = district_ids[rng.integers(0, len(district_ids))] if employed else None
    tenure = Tenure.OWNER if rng.random() < 0.3 else Tenure.RENTER
    has_car = bool(rng.random() < 0.35)
    commute_mode = None
    if employed and occ not in (Occupation.STUDENT, Occupation.RETIRED):
        modes = list(CommuteMode) if has_car else [m for m in CommuteMode if m is not CommuteMode.CAR]
        commute_mode = modes[rng.integers(0, len(modes))]
    return Agent(
        id=agent_id,
        age=int(rng.integers(18, 90)),
        household_size=int(rng.integers(1, 5)),
        occupation=occ,
        wage_monthly=float(rng.uniform(450, 6000)),
        employed=employed,
        job_district=job_district,
        home=home,
        rent_monthly=float(rng.uniform(400, 2500)),
        lease_start_tick=int(rng.integers(-700, 100)),
        savings=float(rng.uniform(0, 30000)),
        spending_level=float(rng.random()),
        satisfaction=float(rng.random()),
        days_unemployed=int(rng.integers(0, 500)) if not employed else 0,
        tenure=tenure,
        children=int(rng.integers(0, 3)) if rng.random() < 0.4 else 0,
        has_car=has_car,
        commute_mode=commute_mode,
        shopping_place=list(ShoppingPlace)[rng.integers(0, len(list(ShoppingPlace)))],
    )


def make_random_event(agent_id: int, agent: Agent, rng: np.random.Generator) -> Event:
    """A realistic mix of events, favoring the common PAYDAY/lease/rent-burden traffic but
    including every new EventKind so the budget test exercises every conditional path."""
    # Roughly proportional to scenario.events' real daily probabilities (EventParams in
    # types.py): PAYDAY (1/30 per agent) dominates; job/life/tourism/etc. events are all much
    # rarer day to day. Keeps the conditional-question trigger rate realistic for the budget.
    weighted_kinds = (
        [EventKind.PAYDAY] * 670
        + [EventKind.JOB_OFFER] * 200
        + [EventKind.LEASE_RENEWAL] * 56
        + [EventKind.TOURISM_PRESSURE] * 40
        + [EventKind.RENT_BURDEN] * 20
        + [EventKind.SCHOOL_YEAR] * 20
        + [EventKind.SHOP_CLOSED] * 20
        + [EventKind.LIFE_EVENT] * 16
        + [EventKind.TRANSIT_CHANGE] * 10
        + [EventKind.ARRIVED] * 10
        + [EventKind.JOB_LOSS] * 3
    )
    kind = weighted_kinds[rng.integers(0, len(weighted_kinds))]
    payload: dict = {}
    if kind is EventKind.LEASE_RENEWAL:
        payload = {"increase_pct": float(rng.uniform(-0.05, 0.2))}
    elif kind is EventKind.JOB_OFFER:
        payload = {"district": agent.home, "wage": float(rng.uniform(1200, 4000))}
    elif kind is EventKind.RENT_BURDEN:
        payload = {"burden": float(rng.uniform(0.3, 0.9))}
    elif kind is EventKind.LIFE_EVENT:
        payload = {"kind": ["new_child", "partner", "health", "inheritance"][rng.integers(0, 4)]}
    elif kind is EventKind.TRANSIT_CHANGE:
        payload = {"kind": ["lez", "new_line"][rng.integers(0, 2)]}
    elif kind is EventKind.SHOP_CLOSED:
        payload = {"closed_pct": float(rng.uniform(0.05, 0.3))}
    elif kind is EventKind.TOURISM_PRESSURE:
        payload = {"tourist_share": float(rng.uniform(0.05, 0.3))}
    return Event(agent_id=agent_id, kind=kind, payload=payload)


@pytest.fixture
def world10(profiles, rng) -> World:
    return make_world_10(make_10_profiles(profiles), rng)


class TestTokenBudget:
    def test_k1_budget_over_realistic_population(self, world10, rng):
        district_ids = list(world10.profiles)
        n = 600
        agents = {i: make_random_agent(i, district_ids, rng) for i in range(n)}
        events = [make_random_event(i, agents[i], rng) for i in range(n)]

        reqs = build_requests(world10, agents, events, tick=200, agents_per_request=1)
        assert len(reqs) == n  # one event each -> one request each at K=1

        token_counts = []
        state_tokens = []
        question_tokens = []
        for req in reqs:
            body = request_body(req, JEV_MODEL_ID)
            token_counts.append(estimate_tokens(body))
            state_tokens.append(estimate_tokens({"state": req.state}))
            question_tokens.append(estimate_tokens({"questions": req.questions}))

        avg_tokens = sum(token_counts) / len(token_counts)
        max_tokens = max(token_counts)
        sorted_tokens = sorted(token_counts)
        p90_tokens = sorted_tokens[int(0.9 * len(sorted_tokens))]

        print(
            f"\nK=1 token budget over {n} agents: avg={avg_tokens:.0f} p90={p90_tokens} "
            f"max={max_tokens} (state avg={sum(state_tokens) / n:.0f}, "
            f"questions avg={sum(question_tokens) / n:.0f})"
        )

        assert avg_tokens <= TOKEN_BUDGET_AVG, f"avg {avg_tokens:.0f} > budget {TOKEN_BUDGET_AVG}"
        assert max_tokens <= TOKEN_BUDGET_MAX, f"max {max_tokens} > budget {TOKEN_BUDGET_MAX}"

    def test_kn_still_within_a_similar_budget(self, world10, rng):
        """K>1 packs several agents' worth of questions into one request, but the shared
        `districts` state (union of relevant shortlists) and per-agent trimmed affordability
        keep per-agent cost from blowing up."""
        district_ids = list(world10.profiles)
        n = 200
        agents = {i: make_random_agent(i, district_ids, rng) for i in range(n)}
        events = [make_random_event(i, agents[i], rng) for i in range(n)]

        reqs = build_requests(world10, agents, events, tick=200, agents_per_request=8)
        assert reqs

        per_agent_tokens = []
        for req in reqs:
            body = request_body(req, JEV_MODEL_ID)
            tokens = estimate_tokens(body)
            per_agent_tokens.append(tokens / len(req.agent_ids))

        avg_per_agent = sum(per_agent_tokens) / len(per_agent_tokens)
        # K>1 shares one districts block across the batch, so per-agent cost should stay
        # comfortably under the K=1 budget (a looser bound; K>1 isn't the primary target here).
        assert avg_per_agent <= TOKEN_BUDGET_AVG, avg_per_agent


class TestDestinationOptions:
    def test_destination_options_are_relevant_districts_plus_leave_city(self, world10, rng):
        district_ids = list(world10.profiles)
        agent = make_random_agent(1, district_ids, rng)
        world10.states  # noqa: B018 (touch for readability)
        events = [Event(agent_id=1, kind=EventKind.LEASE_RENEWAL, payload={"increase_pct": 0.1})]
        reqs = build_requests(world10, {1: agent}, events, tick=200, agents_per_request=1)
        req = reqs[0]
        state_district_ids = {d["name"] for d in req.state["districts"]}
        dest_criteria = req.questions[question_key(1, "destination")]["criteria"]
        assert LEAVE_CITY in dest_criteria
        assert set(dest_criteria) - {LEAVE_CITY} == {
            did for did in world10.profiles if world10.profiles[did].name in state_district_ids
        }
        assert len(req.state["districts"]) <= 5

    def test_max_districts_in_state_kwarg_is_respected(self, world10, rng):
        district_ids = list(world10.profiles)
        agent = make_random_agent(1, district_ids, rng)
        events = [Event(agent_id=1, kind=EventKind.LEASE_RENEWAL, payload={"increase_pct": 0.1})]
        reqs = build_requests(
            world10, {1: agent}, events, tick=200, agents_per_request=1, max_districts_in_state=3
        )
        assert len(reqs[0].state["districts"]) <= 3
        dest_criteria = reqs[0].questions[question_key(1, "destination")]["criteria"]
        assert len(dest_criteria) <= 4  # <=3 districts + leave_city

    def test_home_and_job_district_always_included(self, world10):
        agent = Agent(
            id=1, age=30, household_size=1, occupation=Occupation.MID_SKILL, wage_monthly=2200,
            employed=True, job_district="les_corts", home="nou_barris", rent_monthly=900,
            lease_start_tick=-50, savings=1000, spending_level=0.5, satisfaction=0.5,
        )
        selected = _select_relevant_districts(agent, world10, max_districts=5)
        assert "nou_barris" in selected
        assert "les_corts" in selected


class TestConditionalQuestions:
    def test_commute_mode_only_on_relevant_events(self):
        agent = Agent(
            id=1, age=30, household_size=1, occupation=Occupation.MID_SKILL, wage_monthly=2200,
            employed=True, job_district="eixample", home="eixample", rent_monthly=900,
            lease_start_tick=-50, savings=1000, spending_level=0.5, satisfaction=0.5,
        )
        assert wants_commute_question(agent, [Event(agent_id=1, kind=EventKind.JOB_OFFER)])
        assert wants_commute_question(agent, [Event(agent_id=1, kind=EventKind.TRANSIT_CHANGE)])
        assert not wants_commute_question(agent, [Event(agent_id=1, kind=EventKind.PAYDAY)])
        assert not wants_commute_question(agent, [Event(agent_id=1, kind=EventKind.SHOP_CLOSED)])

    def test_commute_mode_never_asked_when_unemployed(self):
        agent = Agent(
            id=1, age=30, household_size=1, occupation=Occupation.LOW_SKILL, wage_monthly=1500,
            employed=False, job_district=None, home="eixample", rent_monthly=900,
            lease_start_tick=-50, savings=1000, spending_level=0.5, satisfaction=0.5,
        )
        assert not wants_commute_question(agent, [Event(agent_id=1, kind=EventKind.JOB_OFFER)])

    def test_shopping_place_on_shop_closed_arrived_life_event(self):
        assert wants_shopping_question(
            Agent(id=1, age=30, household_size=1, occupation=Occupation.MID_SKILL,
                  wage_monthly=2000, employed=True, job_district="eixample", home="eixample",
                  rent_monthly=900, lease_start_tick=-50, savings=1000, spending_level=0.5,
                  satisfaction=0.5),
            [Event(agent_id=1, kind=EventKind.SHOP_CLOSED)], tick=10,
        )

    def test_shopping_place_payday_is_periodic_not_every_month(self):
        agent = Agent(
            id=7, age=30, household_size=1, occupation=Occupation.MID_SKILL, wage_monthly=2000,
            employed=True, job_district="eixample", home="eixample", rent_monthly=900,
            lease_start_tick=-50, savings=1000, spending_level=0.5, satisfaction=0.5,
        )
        # Paydays are staggered by agent id (real events module), so this agent's paydays
        # land on ticks == agent.id (mod 30); simulate 30 of them.
        payday_ticks = [agent.id + 30 * k for k in range(30)]
        hits = sum(
            wants_shopping_question(agent, [Event(agent_id=7, kind=EventKind.PAYDAY)], tick=t)
            for t in payday_ticks
        )
        # 30 monthly paydays in this window -> should fire roughly 1/3 of the time, never all.
        assert 0 < hits < 30

    def test_car_option_only_offered_to_car_owners(self, world10):
        no_car = Agent(
            id=1, age=30, household_size=1, occupation=Occupation.MID_SKILL, wage_monthly=2000,
            employed=True, job_district="eixample", home="eixample", rent_monthly=900,
            lease_start_tick=-50, savings=1000, spending_level=0.5, satisfaction=0.5, has_car=False,
        )
        with_car = Agent(
            id=2, age=30, household_size=1, occupation=Occupation.MID_SKILL, wage_monthly=2000,
            employed=True, job_district="eixample", home="eixample", rent_monthly=900,
            lease_start_tick=-50, savings=1000, spending_level=0.5, satisfaction=0.5, has_car=True,
        )
        events = [
            Event(agent_id=1, kind=EventKind.JOB_OFFER),
            Event(agent_id=2, kind=EventKind.JOB_OFFER),
        ]
        reqs = build_requests(
            world10, {1: no_car, 2: with_car}, events, tick=200, agents_per_request=1
        )
        by_agent = {r.agent_ids[0]: r for r in reqs}
        no_car_criteria = by_agent[1].questions[question_key(1, "commute_mode")]["criteria"]
        with_car_criteria = by_agent[2].questions[question_key(2, "commute_mode")]["criteria"]
        assert "car" not in no_car_criteria
        assert "car" in with_car_criteria


def test_own_commute_length_text_smoke(world10):
    agent = Agent(
        id=1, age=30, household_size=1, occupation=Occupation.MID_SKILL, wage_monthly=2000,
        employed=True, job_district="eixample", home="eixample", rent_monthly=900,
        lease_start_tick=-50, savings=1000, spending_level=0.5, satisfaction=0.5,
        commute_mode=CommuteMode.METRO,
    )
    assert own_commute_length_text(agent, world10) == "same"
