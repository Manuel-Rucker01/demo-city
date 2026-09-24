"""Tests for prompts/state_builder.py and prompts/questions.py. Owner: T4."""

from __future__ import annotations

import json
import re

from jevcity.prompts.questions import mock_priors_for_agent
from jevcity.prompts.state_builder import build_requests
from jevcity.types import (
    QUESTION_NAMES,
    Action,
    Agent,
    CommuteMode,
    DistrictState,
    Event,
    EventKind,
    Occupation,
    ShoppingPlace,
    Tenure,
    World,
    question_key,
)

_ALLOWED_NON_ASCII = re.compile(r"^[\x00-\x7F€À-ÿ]*$")  # ascii + euro + latin-1 accents
_LONG_DECIMAL = re.compile(r"\d+\.\d{3,}")


def make_world(profiles) -> World:
    states = {
        p.id: DistrictState(
            id=p.id,
            avg_rent=p.avg_rent_monthly,
            housing_units=1000,
            occupied_units=int(1000 * (1 - p.vacancy_rate)),
            jobs=int(p.population * p.jobs_per_resident / 50),
            filled_jobs=int(p.population * p.jobs_per_resident / 50 * (1 - p.unemployment_rate)),
        )
        for p in profiles
    }
    rent_history = {p.id: [p.avg_rent_monthly * 0.95, p.avg_rent_monthly] for p in profiles}
    return World(
        profiles={p.id: p for p in profiles},
        states=states,
        tick=100,
        rent_history=rent_history,
    )


def make_agent(
    agent_id: int,
    *,
    home: str,
    occupation: Occupation = Occupation.MID_SKILL,
    employed: bool = True,
    job_district: str | None = None,
    wage_monthly: float = 2000.0,
    rent_monthly: float = 1000.0,
    savings: float = 3000.0,
    days_unemployed: int = 0,
    lease_start_tick: int = -100,
    age: int = 34,
    household_size: int = 2,
    satisfaction: float = 0.5,
    spending_level: float = 0.5,
    tenure: Tenure = Tenure.RENTER,
    children: int = 0,
    has_car: bool = False,
    commute_mode: CommuteMode | None = None,
    commute_since_tick: int | None = None,
    shopping_place: ShoppingPlace = ShoppingPlace.LOCAL,
) -> Agent:
    return Agent(
        id=agent_id,
        age=age,
        household_size=household_size,
        occupation=occupation,
        wage_monthly=wage_monthly,
        employed=employed,
        job_district=job_district if job_district is not None else (home if employed else None),
        home=home,
        rent_monthly=rent_monthly,
        lease_start_tick=lease_start_tick,
        savings=savings,
        spending_level=spending_level,
        satisfaction=satisfaction,
        days_unemployed=days_unemployed,
        tenure=tenure,
        children=children,
        has_car=has_car,
        commute_mode=commute_mode,
        commute_since_tick=commute_since_tick,
        shopping_place=shopping_place,
    )


def _assert_ascii_english(obj) -> None:
    """English text only (ascii + euro sign + Catalan/Spanish accents), no long decimals."""
    dumped = json.dumps(obj)
    assert not _LONG_DECIMAL.search(dumped), f"raw float with >2 decimals found in {dumped!r}"
    if isinstance(obj, str):
        assert _ALLOWED_NON_ASCII.match(obj), f"non-English characters in {obj!r}"
    elif isinstance(obj, dict):
        for v in obj.values():
            _assert_ascii_english(v)
    elif isinstance(obj, list):
        for v in obj:
            _assert_ascii_english(v)


def _assert_valid_question(q: dict) -> None:
    assert q["type"] in ("choice", "score", "noul")
    assert "instructions" in q
    if q["type"] == "choice":
        assert isinstance(q["criteria"], dict)
        assert 1 <= len(q["criteria"]) <= 255
    elif q["type"] == "score":
        assert isinstance(q["criteria"], list)
        assert 2 <= len(q["criteria"]) <= 10


class TestBuildRequestsK1:
    def test_agents_without_events_excluded(self, profiles):
        world = make_world(profiles)
        a1 = make_agent(1, home="eixample")
        a2 = make_agent(2, home="gracia")
        events = [Event(agent_id=1, kind=EventKind.PAYDAY)]
        reqs = build_requests(world, {1: a1, 2: a2}, events, tick=100, agents_per_request=1)
        all_agent_ids = {aid for r in reqs for aid in r.agent_ids}
        assert all_agent_ids == {1}

    def test_question_shapes_and_keys(self, profiles):
        world = make_world(profiles)
        agent = make_agent(1, home="eixample")
        events = [Event(agent_id=1, kind=EventKind.PAYDAY)]
        reqs = build_requests(world, {1: agent}, events, tick=100, agents_per_request=1)
        assert len(reqs) == 1
        req = reqs[0]
        assert req.request_id == "t100-r0"
        assert req.agent_ids == [1]
        # PAYDAY alone doesn't trigger the conditional questions (see
        # state_builder.wants_commute_question/wants_shopping_question).
        for name in ("action", "destination", "spending", "satisfaction"):
            key = question_key(1, name)
            assert key in req.questions
            _assert_valid_question(req.questions[key])
        assert question_key(1, "commute_mode") not in req.questions
        assert question_key(1, "shopping_place") not in req.questions

    def test_state_shape_k1(self, profiles):
        world = make_world(profiles)
        agent = make_agent(1, home="eixample")
        events = [Event(agent_id=1, kind=EventKind.LEASE_RENEWAL, payload={"increase_pct": 0.09})]
        reqs = build_requests(world, {1: agent}, events, tick=100, agents_per_request=1)
        state = reqs[0].state
        assert set(state.keys()) == {"person", "today", "districts"}
        assert isinstance(state["today"], list)
        assert len(state["today"]) == 1
        assert "9%" in state["today"][0]
        assert len(state["districts"]) == len(profiles)
        for d in state["districts"]:
            assert set(d.keys()) == {"name", "rent", "jobs", "transit", "you_live_here"}
        homes = [d for d in state["districts"] if d["you_live_here"]]
        assert len(homes) == 1
        assert homes[0]["name"] == world.profiles["eixample"].name

    def test_state_size_budget_k1(self, profiles, rng):
        world = make_world(profiles)
        occupations = list(Occupation)
        district_ids = list(world.profiles)
        sizes = []
        for i in range(200):
            home = district_ids[rng.integers(0, len(district_ids))]
            occ = occupations[rng.integers(0, len(occupations))]
            employed = bool(rng.random() < 0.9)
            agent = make_agent(
                i,
                home=home,
                occupation=occ,
                employed=employed,
                wage_monthly=float(rng.uniform(450, 6000)),
                rent_monthly=float(rng.uniform(400, 2500)),
                savings=float(rng.uniform(0, 30000)),
                days_unemployed=int(rng.integers(0, 500)),
                lease_start_tick=int(rng.integers(-700, 100)),
                age=int(rng.integers(18, 90)),
                household_size=int(rng.integers(1, 5)),
                satisfaction=float(rng.random()),
            )
            events = [Event(agent_id=i, kind=EventKind.PAYDAY)]
            reqs = build_requests(world, {i: agent}, events, tick=100, agents_per_request=1)
            # "compact json" per the state_builder module docstring's token-estimate formula.
            state_json = json.dumps(reqs[0].state, separators=(",", ":"))
            sizes.append(len(state_json))
        avg_size = sum(sizes) / len(sizes)
        assert max(sizes) <= 1200, f"a K=1 state exceeded 1200 chars: {max(sizes)}"
        assert avg_size <= 1200

    def test_state_english_and_no_long_floats(self, profiles):
        world = make_world(profiles)
        agent = make_agent(1, home="gracia", employed=False, occupation=Occupation.LOW_SKILL)
        events = [Event(agent_id=1, kind=EventKind.JOB_LOSS)]
        reqs = build_requests(world, {1: agent}, events, tick=100, agents_per_request=1)
        _assert_ascii_english(reqs[0].state)

    def test_renter_state_says_rents_their_home(self, profiles):
        world = make_world(profiles)
        agent = make_agent(1, home="eixample", tenure=Tenure.RENTER)
        events = [Event(agent_id=1, kind=EventKind.PAYDAY)]
        reqs = build_requests(world, {1: agent}, events, tick=100, agents_per_request=1)
        person_text = reqs[0].state["person"]["rent_burden"]
        assert "rents their home" in person_text
        assert "rent takes" in person_text

    def test_owner_state_says_owns_their_home_and_housing_cost(self, profiles):
        world = make_world(profiles)
        young_owner = make_agent(1, home="eixample", age=30, tenure=Tenure.OWNER, rent_monthly=300.0)
        old_owner = make_agent(2, home="eixample", age=70, tenure=Tenure.OWNER, rent_monthly=200.0)
        events = [Event(agent_id=1, kind=EventKind.PAYDAY), Event(agent_id=2, kind=EventKind.PAYDAY)]
        reqs = build_requests(
            world, {1: young_owner, 2: old_owner}, events, tick=100, agents_per_request=1
        )
        texts = {r.agent_ids[0]: r.state["person"]["rent_burden"] for r in reqs}
        assert "owns their home (mortgage)" in texts[1]
        assert "housing cost takes" in texts[1]
        assert "owns their home outright" in texts[2]
        assert "housing cost takes" in texts[2]

    def test_person_commute_field_shows_mode_and_habit_years(self, profiles):
        world = make_world(profiles)
        long_habit = make_agent(
            1, home="eixample", employed=True, commute_mode=CommuteMode.CAR,
            commute_since_tick=100 - 6 * 360,  # ~6 years before tick 100
        )
        new_habit = make_agent(
            2, home="eixample", employed=True, commute_mode=CommuteMode.METRO,
            commute_since_tick=95,  # 5 ticks ago, well under a year
        )
        events = [Event(agent_id=1, kind=EventKind.PAYDAY), Event(agent_id=2, kind=EventKind.PAYDAY)]
        reqs = build_requests(
            world, {1: long_habit, 2: new_habit}, events, tick=100, agents_per_request=1
        )
        texts = {r.agent_ids[0]: r.state["person"]["commute"] for r in reqs}
        assert texts[1] == "car, 6y"
        assert texts[2] == "metro, <1y"

    def test_person_commute_field_absent_when_no_commute_mode(self, profiles):
        world = make_world(profiles)
        agent = make_agent(1, home="eixample", employed=False, commute_mode=None)
        events = [Event(agent_id=1, kind=EventKind.PAYDAY)]
        reqs = build_requests(world, {1: agent}, events, tick=100, agents_per_request=1)
        assert "commute" not in reqs[0].state["person"]


class TestBuildRequestsKN:
    def test_packing_and_people_keys(self, profiles):
        world = make_world(profiles)
        agents = {i: make_agent(i, home="eixample") for i in range(1, 8)}
        events = [Event(agent_id=i, kind=EventKind.PAYDAY) for i in range(1, 8)]
        reqs = build_requests(world, agents, events, tick=50, agents_per_request=3)
        # 7 agents packed by 3 -> 3 requests (3, 3, 1)
        assert [len(r.agent_ids) for r in reqs] == [3, 3, 1]
        assert reqs[0].agent_ids == [1, 2, 3]
        assert reqs[1].agent_ids == [4, 5, 6]
        assert reqs[2].agent_ids == [7]

        req = reqs[0]
        assert set(req.state.keys()) == {"districts", "people"}
        assert set(req.state["people"].keys()) == {"p1", "p2", "p3"}
        for aid in (1, 2, 3):
            person = req.state["people"][f"p{aid}"]
            assert set(person.keys()) == {"person", "today", "affordability"}
            assert set(person["affordability"].keys()) == set(world.profiles.keys())
            for name in ("action", "destination", "spending", "satisfaction"):
                key = question_key(aid, name)
                assert key in req.questions
                _assert_valid_question(req.questions[key])
                assert f"people.p{aid}" in json.dumps(req.questions[key]["instructions"])

    def test_multiple_events_merge_into_one_today_list(self, profiles):
        world = make_world(profiles)
        agent = make_agent(1, home="eixample")
        events = [
            Event(agent_id=1, kind=EventKind.PAYDAY),
            Event(agent_id=1, kind=EventKind.JOB_OFFER, payload={"district": "gracia", "wage": 2500}),
        ]
        reqs = build_requests(world, {1: agent}, events, tick=10, agents_per_request=2)
        person = reqs[0].state["people"]["p1"]
        assert len(person["today"]) == 2


class TestMockPriors:
    def test_unemployed_agent_has_higher_job_search_prior(self, profiles):
        world = make_world(profiles)
        employed = make_agent(1, home="eixample", employed=True)
        unemployed = make_agent(
            2, home="eixample", employed=False, occupation=Occupation.MID_SKILL, days_unemployed=90
        )
        p_employed = mock_priors_for_agent(employed, [], world)
        p_unemployed = mock_priors_for_agent(unemployed, [], world)
        assert (
            p_unemployed["action"][Action.JOB_SEARCH.value]
            > p_employed["action"][Action.JOB_SEARCH.value]
        )

    def test_severe_burden_raises_move_prior(self, profiles):
        world = make_world(profiles)
        comfortable = make_agent(1, home="eixample", wage_monthly=4000, rent_monthly=800)
        burdened = make_agent(2, home="eixample", wage_monthly=1200, rent_monthly=1100)
        p_comfortable = mock_priors_for_agent(comfortable, [], world)
        p_burdened = mock_priors_for_agent(burdened, [], world)
        assert p_burdened["action"][Action.MOVE.value] > p_comfortable["action"][Action.MOVE.value]

    def test_owner_much_less_likely_to_move(self, profiles):
        world = make_world(profiles)
        renter = make_agent(1, home="eixample", tenure=Tenure.RENTER, wage_monthly=1200, rent_monthly=1100)
        owner = make_agent(2, home="eixample", tenure=Tenure.OWNER, wage_monthly=1200, rent_monthly=1100)
        p_renter = mock_priors_for_agent(renter, [], world)
        p_owner = mock_priors_for_agent(owner, [], world)
        assert p_owner["action"][Action.MOVE.value] < p_renter["action"][Action.MOVE.value]

    def test_commute_habit_makes_switching_less_likely(self, profiles):
        """A long-standing commute habit should make the mock backend more likely to keep the
        agent's current mode (relative to a brand-new commuter with the same current mode) --
        see the new-metro-line realism fix in the final report."""
        world = make_world(profiles)  # world.tick == 100
        long_habit = make_agent(
            1, home="eixample", employed=True, commute_mode=CommuteMode.CAR, has_car=True,
            commute_since_tick=100 - 6 * 360,
        )
        new_habit = make_agent(
            2, home="eixample", employed=True, commute_mode=CommuteMode.CAR, has_car=True,
            commute_since_tick=99,
        )
        p_long = mock_priors_for_agent(long_habit, [], world)
        p_new = mock_priors_for_agent(new_habit, [], world)
        assert (
            p_long["commute_mode"][CommuteMode.CAR.value] > p_new["commute_mode"][CommuteMode.CAR.value]
        )

    def test_priors_cover_all_question_names_and_options(self, profiles):
        world = make_world(profiles)
        agent = make_agent(1, home="eixample")
        priors = mock_priors_for_agent(agent, [], world)
        assert set(priors.keys()) == set(QUESTION_NAMES)
        assert set(priors["action"].keys()) == {a.value for a in Action}
        assert set(priors["destination"].keys()) == set(world.profiles.keys()) | {"leave_city"}
        assert set(priors["spending"].keys()) == {"0", "1", "2", "3", "4"}
        assert set(priors["satisfaction"].keys()) == {"0", "1", "2", "3", "4"}
        # agent has no car (make_agent default) -> "car" excluded from commute_mode options
        assert set(priors["commute_mode"].keys()) == {
            m.value for m in CommuteMode if m is not CommuteMode.CAR
        }
        assert set(priors["shopping_place"].keys()) == {p.value for p in ShoppingPlace}
        for name in QUESTION_NAMES:
            assert all(w >= 0 for w in priors[name].values())
