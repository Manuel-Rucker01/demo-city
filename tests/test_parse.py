"""Tests for prompts/parse.py. Owner: T4."""

from __future__ import annotations

from jevcity.prompts.parse import parse_decisions
from jevcity.types import Action, DecisionRequest, JevResponse, JevUsage, question_key


def make_request(agent_ids: list[int], tick: int = 10) -> DecisionRequest:
    return DecisionRequest(
        request_id=f"t{tick}-r0",
        tick=tick,
        agent_ids=agent_ids,
        state={"noop": True},
        questions={},
    )


def choice_answer(choice: str, probabilities: dict[str, float], confidence: float) -> dict:
    return {"type": "choice", "choice": choice, "probabilities": probabilities, "confidence": confidence}


def score_answer(score: float, n_levels: int, confidence: float = 0.9) -> dict:
    legend = {str(i): f"level {i}" for i in range(n_levels)}
    probs = {str(i): (1.0 if i == round(score) else 0.0) for i in range(n_levels)}
    return {
        "type": "score",
        "score": score,
        "legend": legend,
        "probabilities": probs,
        "confidence": confidence,
    }


def make_response(agent_id: int, *, action="stay", action_conf=0.9, destination=None,
                   spending_score=2.0, satisfaction_score=2.0, n_levels=5) -> JevResponse:
    answers = {
        question_key(agent_id, "action"): choice_answer(
            action, {"stay": 0.9, "move": 0.1, "job_search": 0.0, "spend": 0.0, "save": 0.0}, action_conf
        ),
        question_key(agent_id, "spending"): score_answer(spending_score, n_levels),
        question_key(agent_id, "satisfaction"): score_answer(satisfaction_score, n_levels),
    }
    if destination is not None:
        answers[question_key(agent_id, "destination")] = choice_answer(
            destination, {destination: 0.9, "eixample": 0.1}, 0.85
        )
    return JevResponse(model="jev-1.13.0", answers=answers, usage=JevUsage(input_tokens=100))


class TestParseDecisions:
    def test_basic_stay_mapping(self):
        req = make_request([1])
        resp = make_response(1, action="stay")
        [decision] = parse_decisions([req], [resp], confidence_threshold=0.35)
        assert decision.agent_id == 1
        assert decision.action == Action.STAY
        assert decision.destination is None
        assert decision.gated is False
        assert decision.confidence == 0.9

    def test_move_keeps_destination(self):
        req = make_request([1])
        resp = make_response(1, action="move", destination="gracia")
        [decision] = parse_decisions([req], [resp], confidence_threshold=0.35)
        assert decision.action == Action.MOVE
        assert decision.destination == "gracia"

    def test_destination_dropped_when_not_moving(self):
        req = make_request([1])
        resp = make_response(1, action="stay", destination="gracia")
        [decision] = parse_decisions([req], [resp], confidence_threshold=0.35)
        assert decision.action == Action.STAY
        assert decision.destination is None

    def test_low_confidence_gates_to_stay(self):
        req = make_request([1])
        resp = make_response(1, action="move", action_conf=0.2, destination="gracia")
        [decision] = parse_decisions([req], [resp], confidence_threshold=0.35)
        assert decision.action == Action.STAY
        assert decision.gated is True
        assert decision.destination is None

    def test_move_missing_destination_answer_gates_to_stay(self):
        req = make_request([1])
        resp = make_response(1, action="move", destination=None)
        [decision] = parse_decisions([req], [resp], confidence_threshold=0.35)
        assert decision.action == Action.STAY
        assert decision.gated is True
        assert decision.destination is None

    def test_score_normalization(self):
        req = make_request([1])
        # 5 levels (0..4): score 2.0 -> 2/4 = 0.5; score 4.0 -> 1.0; score 0.0 -> 0.0
        resp = make_response(1, spending_score=4.0, satisfaction_score=0.0, n_levels=5)
        [decision] = parse_decisions([req], [resp], confidence_threshold=0.35)
        assert decision.spending == 1.0
        assert decision.satisfaction == 0.0

    def test_missing_action_answer_gates_to_stay(self):
        req = make_request([1])
        resp = JevResponse(model="jev-1.13.0", answers={}, usage=JevUsage(input_tokens=10))
        [decision] = parse_decisions([req], [resp], confidence_threshold=0.35)
        assert decision.action == Action.STAY
        assert decision.gated is True
        assert decision.confidence == 0.0
        assert decision.action_probs == {}

    def test_multiple_agents_in_one_request(self):
        req = make_request([1, 2, 3])
        answers = {}
        answers.update(make_response(1, action="stay").answers)
        answers.update(make_response(2, action="job_search").answers)
        answers.update(make_response(3, action="move", destination="nou_barris").answers)
        resp = JevResponse(model="jev-1.13.0", answers=answers, usage=JevUsage(input_tokens=300))
        decisions = parse_decisions([req], [resp], confidence_threshold=0.35)
        by_id = {d.agent_id: d for d in decisions}
        assert by_id[1].action == Action.STAY
        assert by_id[2].action == Action.JOB_SEARCH
        assert by_id[3].action == Action.MOVE
        assert by_id[3].destination == "nou_barris"

    def test_action_probs_passthrough(self):
        req = make_request([1])
        resp = make_response(1, action="stay")
        [decision] = parse_decisions([req], [resp], confidence_threshold=0.35)
        assert decision.action_probs == {
            "stay": 0.9, "move": 0.1, "job_search": 0.0, "spend": 0.0, "save": 0.0,
        }
