"""Tests for the landlord decision path: build_landlord_requests / mock_priors_for_landlord /
parse_landlord_decisions. Owner: T4."""

from __future__ import annotations

from jevcity.jev.cachekey import request_body
from jevcity.jev.meter import estimate_tokens
from jevcity.prompts.parse import parse_landlord_decisions
from jevcity.prompts.questions import mock_priors_for_landlord
from jevcity.prompts.state_builder import build_landlord_requests
from jevcity.types import (
    JEV_MODEL_ID,
    LANDLORD_QUESTION,
    DistrictState,
    JevResponse,
    JevUsage,
    LandlordAction,
    LandlordType,
    RentalSupplyParams,
    Scenario,
    Vacancy,
    World,
    question_key,
)

LANDLORD_TOKEN_BUDGET = 600


def make_world(profiles, *, rent_cap: float | None = None, quality: float = 1.0) -> World:
    states = {
        p.id: DistrictState(
            id=p.id,
            avg_rent=p.avg_rent_monthly,
            housing_units=1000,
            occupied_units=int(1000 * (1 - p.vacancy_rate)),
            jobs=int(p.population * p.jobs_per_resident / 50),
            filled_jobs=int(p.population * p.jobs_per_resident / 50 * (1 - p.unemployment_rate)),
            rent_cap=rent_cap,
            quality=quality,
        )
        for p in profiles
    }
    return World(profiles={p.id: p for p in profiles}, states=states, tick=100)


def make_scenario(*, seasonal_capped: bool = False, seasonal_rent_multiple: float = 1.35) -> Scenario:
    return Scenario(
        name="t",
        rental_supply=RentalSupplyParams(
            enabled=True,
            seasonal_rent_multiple=seasonal_rent_multiple,
            seasonal_capped=seasonal_capped,
            renovation_ticks=90,
        ),
    )


def make_vacancy(
    vacancy_id: int, district: str, *, landlord_type=LandlordType.SMALL, last_rent=900.0, tenant_years=4.0
) -> Vacancy:
    return Vacancy(
        vacancy_id=vacancy_id,
        tick=100,
        district=district,
        landlord_type=landlord_type,
        last_rent=last_rent,
        tenant_years=tenant_years,
    )


class TestBuildLandlordRequests:
    def test_request_shape_and_kind(self, profiles):
        world = make_world(profiles)
        scenario = make_scenario()
        vacancy = make_vacancy(42, "eixample")
        reqs = build_landlord_requests(world, [vacancy], scenario, tick=100)
        assert len(reqs) == 1
        req = reqs[0]
        assert req.kind == "landlord"
        assert req.agent_ids == [42]
        assert req.tick == 100
        key = question_key(42, LANDLORD_QUESTION)
        assert key in req.questions
        assert req.questions[key]["type"] == "choice"
        assert set(req.questions[key]["criteria"]) == {a.value for a in LandlordAction}
        assert key in req.mock_priors
        assert isinstance(req.state, dict)
        assert req.state["district"] == world.profiles["eixample"].name

    def test_one_request_per_vacancy(self, profiles):
        world = make_world(profiles)
        scenario = make_scenario()
        vacancies = [make_vacancy(1, "eixample"), make_vacancy(2, "gracia")]
        reqs = build_landlord_requests(world, vacancies, scenario, tick=100)
        assert len(reqs) == 2
        assert [r.agent_ids for r in reqs] == [[1], [2]]

    def test_cap_mentioned_only_when_binding(self, profiles):
        capped_world = make_world(profiles, rent_cap=900.0)  # eixample market rent is 1250
        uncapped_world = make_world(profiles, rent_cap=None)
        scenario = make_scenario()
        vacancy = make_vacancy(1, "eixample")

        capped_req = build_landlord_requests(capped_world, [vacancy], scenario, tick=100)[0]
        uncapped_req = build_landlord_requests(uncapped_world, [vacancy], scenario, tick=100)[0]

        assert "cap" in capped_req.state
        assert "900" in capped_req.state["cap"]
        assert "cap" not in uncapped_req.state

    def test_cap_not_shown_when_cap_above_market(self, profiles):
        # rent_cap set but above market rent -> not binding, must not appear.
        world = make_world(profiles, rent_cap=5000.0)
        scenario = make_scenario()
        vacancy = make_vacancy(1, "eixample")
        req = build_landlord_requests(world, [vacancy], scenario, tick=100)[0]
        assert "cap" not in req.state

    def test_seasonal_uncapped_when_seasonal_capped_false(self, profiles):
        world = make_world(profiles, rent_cap=900.0)
        scenario = make_scenario(seasonal_capped=False)
        vacancy = make_vacancy(1, "eixample")
        req = build_landlord_requests(world, [vacancy], scenario, tick=100)[0]
        assert "not covered by the cap" in req.state["seasonal"]

    def test_seasonal_capped_when_seasonal_capped_true(self, profiles):
        world = make_world(profiles, rent_cap=900.0)
        scenario = make_scenario(seasonal_capped=True)
        vacancy = make_vacancy(1, "eixample")
        req = build_landlord_requests(world, [vacancy], scenario, tick=100)[0]
        assert "covered by the cap" in req.state["seasonal"]
        assert "not covered" not in req.state["seasonal"]

    def test_seasonal_no_cap_note_when_no_cap(self, profiles):
        world = make_world(profiles, rent_cap=None)
        scenario = make_scenario()
        vacancy = make_vacancy(1, "eixample")
        req = build_landlord_requests(world, [vacancy], scenario, tick=100)[0]
        assert "cap" not in req.state["seasonal"]

    def test_seasonal_rent_uses_multiple(self, profiles):
        world = make_world(profiles, rent_cap=None)
        scenario = make_scenario(seasonal_rent_multiple=1.5)
        vacancy = make_vacancy(1, "eixample")  # eixample market rent = 1250
        req = build_landlord_requests(world, [vacancy], scenario, tick=100)[0]
        assert str(round(1250 * 1.5)) in req.state["seasonal"]

    def test_previous_tenant_rent_and_years(self, profiles):
        world = make_world(profiles)
        scenario = make_scenario()
        vacancy = make_vacancy(1, "eixample", last_rent=950.0, tenant_years=4.2)
        req = build_landlord_requests(world, [vacancy], scenario, tick=100)[0]
        text = req.state["previous_tenant"]
        assert "950" in text
        assert "4 years" in text

    def test_renovation_text_uses_renovation_ticks(self, profiles):
        world = make_world(profiles)
        scenario = Scenario(
            name="t", rental_supply=RentalSupplyParams(enabled=True, renovation_ticks=180)
        )
        vacancy = make_vacancy(1, "eixample")
        req = build_landlord_requests(world, [vacancy], scenario, tick=100)[0]
        assert "6 months" in req.state["renovation"]

    def test_landlord_type_text_differs_small_vs_large(self, profiles):
        world = make_world(profiles)
        scenario = make_scenario()
        small = build_landlord_requests(
            world, [make_vacancy(1, "eixample", landlord_type=LandlordType.SMALL)], scenario, tick=100
        )[0]
        large = build_landlord_requests(
            world, [make_vacancy(2, "eixample", landlord_type=LandlordType.LARGE)], scenario, tick=100
        )[0]
        assert "small landlord" in small.state["landlord"]
        assert "company" in large.state["landlord"]


class TestLandlordTokenBudget:
    def test_landlord_request_within_budget(self, profiles):
        world = make_world(profiles, rent_cap=900.0, quality=0.3)
        scenario = make_scenario(seasonal_capped=True)
        vacancy = make_vacancy(1, "eixample", landlord_type=LandlordType.LARGE)
        req = build_landlord_requests(world, [vacancy], scenario, tick=100)[0]
        body = request_body(req, JEV_MODEL_ID)
        tokens = estimate_tokens(body)
        assert tokens <= LANDLORD_TOKEN_BUDGET, f"{tokens} > {LANDLORD_TOKEN_BUDGET}"


class TestMockPriorsForLandlord:
    def test_uncapped_mostly_relet(self, profiles):
        world = make_world(profiles, rent_cap=None)
        vacancy = make_vacancy(1, "eixample")
        w = mock_priors_for_landlord(vacancy, world)
        assert w[LandlordAction.RELET.value] > w[LandlordAction.SELL.value]
        assert w[LandlordAction.RELET.value] > w[LandlordAction.SEASONAL.value]

    def test_capped_with_large_gap_raises_sell_and_seasonal(self, profiles):
        uncapped_world = make_world(profiles, rent_cap=None)
        capped_world = make_world(profiles, rent_cap=700.0)  # eixample market rent 1250
        vacancy_small = make_vacancy(1, "eixample", landlord_type=LandlordType.SMALL)

        w_uncapped = mock_priors_for_landlord(vacancy_small, uncapped_world)
        w_capped = mock_priors_for_landlord(vacancy_small, capped_world)

        assert w_capped[LandlordAction.SELL.value] > w_uncapped[LandlordAction.SELL.value]
        assert w_capped[LandlordAction.SEASONAL.value] > w_uncapped[LandlordAction.SEASONAL.value]

    def test_capped_small_landlord_favors_sell_over_seasonal(self, profiles):
        world = make_world(profiles, rent_cap=700.0)
        vacancy = make_vacancy(1, "eixample", landlord_type=LandlordType.SMALL)
        w = mock_priors_for_landlord(vacancy, world)
        assert w[LandlordAction.SELL.value] > w[LandlordAction.SEASONAL.value]

    def test_capped_large_landlord_favors_seasonal_over_sell(self, profiles):
        world = make_world(profiles, rent_cap=700.0)
        vacancy = make_vacancy(1, "eixample", landlord_type=LandlordType.LARGE)
        w = mock_priors_for_landlord(vacancy, world)
        assert w[LandlordAction.SEASONAL.value] > w[LandlordAction.SELL.value]

    def test_low_quality_raises_renovate(self, profiles):
        good_world = make_world(profiles, quality=0.95)
        bad_world = make_world(profiles, quality=0.2)
        vacancy = make_vacancy(1, "eixample")
        w_good = mock_priors_for_landlord(vacancy, good_world)
        w_bad = mock_priors_for_landlord(vacancy, bad_world)
        assert w_bad[LandlordAction.RENOVATE.value] > w_good[LandlordAction.RENOVATE.value]

    def test_all_weights_non_negative(self, profiles):
        world = make_world(profiles, rent_cap=700.0, quality=0.1)
        vacancy = make_vacancy(1, "eixample", landlord_type=LandlordType.LARGE)
        w = mock_priors_for_landlord(vacancy, world)
        assert all(v >= 0 for v in w.values())
        assert set(w) == {a.value for a in LandlordAction}


def _choice_answer(choice: str, probabilities: dict[str, float], confidence: float = 0.9) -> dict:
    return {"type": "choice", "choice": choice, "probabilities": probabilities, "confidence": confidence}


class TestParseLandlordDecisions:
    def test_missing_answer_falls_back_to_relet(self, profiles):
        world = make_world(profiles)
        scenario = make_scenario()
        vacancy = make_vacancy(1, "eixample")
        req = build_landlord_requests(world, [vacancy], scenario, tick=100)[0]
        resp = JevResponse(model=JEV_MODEL_ID, answers={}, usage=JevUsage(input_tokens=10))
        [decision] = parse_landlord_decisions([req], [resp], policy="argmax", seed=0)
        assert decision.action == LandlordAction.RELET
        assert decision.vacancy_id == 1
        assert decision.district == "eixample"
        assert decision.confidence == 0.0

    def test_argmax_picks_top_choice(self, profiles):
        world = make_world(profiles)
        scenario = make_scenario()
        vacancy = make_vacancy(7, "gracia")
        req = build_landlord_requests(world, [vacancy], scenario, tick=100)[0]
        probs = {"relet": 0.1, "sell": 0.7, "seasonal": 0.1, "renovate": 0.1}
        resp = JevResponse(
            model=JEV_MODEL_ID,
            answers={question_key(7, LANDLORD_QUESTION): _choice_answer("sell", probs)},
            usage=JevUsage(input_tokens=50),
        )
        [decision] = parse_landlord_decisions([req], [resp], policy="argmax", seed=0)
        assert decision.action == LandlordAction.SELL
        assert decision.district == "gracia"
        assert decision.action_probs == probs

    def test_sample_is_deterministic_for_same_seed(self, profiles):
        world = make_world(profiles)
        scenario = make_scenario()
        vacancy = make_vacancy(3, "eixample")
        req = build_landlord_requests(world, [vacancy], scenario, tick=100)[0]
        probs = {"relet": 0.25, "sell": 0.25, "seasonal": 0.25, "renovate": 0.25}
        resp = JevResponse(
            model=JEV_MODEL_ID,
            answers={question_key(3, LANDLORD_QUESTION): _choice_answer("relet", probs)},
            usage=JevUsage(input_tokens=50),
        )
        [d1] = parse_landlord_decisions([req], [resp], policy="sample", seed=42)
        [d2] = parse_landlord_decisions([req], [resp], policy="sample", seed=42)
        [d3] = parse_landlord_decisions([req], [resp], policy="sample", seed=43)
        assert d1.action == d2.action
        # Not asserting d1 != d3: with only 4 options a different seed may still coincide,
        # but the draw must at least be well-defined (a valid LandlordAction).
        assert d3.action in LandlordAction

    def test_unrecognized_choice_falls_back_to_relet(self, profiles):
        world = make_world(profiles)
        scenario = make_scenario()
        vacancy = make_vacancy(1, "eixample")
        req = build_landlord_requests(world, [vacancy], scenario, tick=100)[0]
        resp = JevResponse(
            model=JEV_MODEL_ID,
            answers={
                question_key(1, LANDLORD_QUESTION): _choice_answer("not_a_real_action", {})
            },
            usage=JevUsage(input_tokens=10),
        )
        [decision] = parse_landlord_decisions([req], [resp], policy="argmax", seed=0)
        assert decision.action == LandlordAction.RELET

    def test_gate_policy_falls_back_to_relet_below_threshold(self, profiles):
        world = make_world(profiles)
        scenario = make_scenario()
        vacancy = make_vacancy(1, "eixample")
        req = build_landlord_requests(world, [vacancy], scenario, tick=100)[0]
        resp = JevResponse(
            model=JEV_MODEL_ID,
            answers={
                question_key(1, LANDLORD_QUESTION): _choice_answer(
                    "sell", {"relet": 0.3, "sell": 0.4, "seasonal": 0.2, "renovate": 0.1}, confidence=0.1
                )
            },
            usage=JevUsage(input_tokens=10),
        )
        [decision] = parse_landlord_decisions([req], [resp], policy="gate", seed=0)
        assert decision.action == LandlordAction.RELET

    def test_multiple_vacancies_in_multiple_requests(self, profiles):
        world = make_world(profiles)
        scenario = make_scenario()
        vacancies = [make_vacancy(1, "eixample"), make_vacancy(2, "gracia")]
        reqs = build_landlord_requests(world, vacancies, scenario, tick=100)
        responses = [
            JevResponse(
                model=JEV_MODEL_ID,
                answers={
                    question_key(1, LANDLORD_QUESTION): _choice_answer(
                        "relet", {"relet": 0.9, "sell": 0.1, "seasonal": 0.0, "renovate": 0.0}
                    )
                },
                usage=JevUsage(input_tokens=10),
            ),
            JevResponse(
                model=JEV_MODEL_ID,
                answers={
                    question_key(2, LANDLORD_QUESTION): _choice_answer(
                        "renovate", {"relet": 0.1, "sell": 0.0, "seasonal": 0.0, "renovate": 0.9}
                    )
                },
                usage=JevUsage(input_tokens=10),
            ),
        ]
        decisions = parse_landlord_decisions(reqs, responses, policy="argmax", seed=0)
        by_id = {d.vacancy_id: d for d in decisions}
        assert by_id[1].action == LandlordAction.RELET
        assert by_id[1].district == "eixample"
        assert by_id[2].action == LandlordAction.RENOVATE
        assert by_id[2].district == "gracia"
