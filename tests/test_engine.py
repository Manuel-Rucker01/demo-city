"""Tests for jevcity.engine.loop.run_simulation.

All collaborators (world/loader, population/generator, world/market, events/triggers,
prompts/state_builder, prompts/parse, jev.choose_agents_per_request, engine/snapshot) are
monkeypatched with small deterministic fakes so this test exercises only the loop's own
wiring: call order, tick counts, usage diffing, the model-change warning, the budget-stop
path and always-closing the writer/backend - not the (owned-by-other-tasks) correctness of
those collaborators themselves.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from jevcity.engine import loop as engine_loop
from jevcity.jev.errors import JevBudgetExceeded
from jevcity.runlog.reader import RunReader
from jevcity.types import (
    Action,
    AgentDecision,
    DecisionRequest,
    DistrictSnapshot,
    Event,
    EventKind,
    JevConfig,
    JevResponse,
    JevUsage,
    MoveRecord,
    ProviderSettings,
    Scenario,
    TickDelta,
    Usage,
)


def _settings() -> ProviderSettings:
    return ProviderSettings(
        base_url="mock://local",
        path="",
        wire="systemone",
        api_key_env=None,
        default_model="jev-test-model",
    )


class FakeBackend:
    """Deterministic fake JevBackend keyed by the tick of the (single) request per tick."""

    def __init__(self, responses_by_tick, usage_by_tick, fail_at_tick=None, fail_exc=None):
        self.provider = "mock"
        self.settings = _settings()
        self._responses_by_tick = responses_by_tick
        self._usage_by_tick = usage_by_tick
        self._current_usage = Usage()
        self._fail_at_tick = fail_at_tick
        self._fail_exc = fail_exc
        self.eval_calls: list[int] = []
        self.closed = False

    async def evaluate_many(self, reqs):
        tick = reqs[0].tick
        self.eval_calls.append(tick)
        if self._fail_at_tick == tick:
            raise self._fail_exc
        self._current_usage = self._usage_by_tick[tick]
        return self._responses_by_tick[tick]

    def usage(self) -> Usage:
        return self._current_usage

    async def aclose(self) -> None:
        self.closed = True


def _resp(model: str) -> JevResponse:
    return JevResponse(model=model, answers={}, usage=JevUsage(input_tokens=10))


class _Harness:
    """Patches every engine_loop collaborator with call-order-tracking fakes."""

    def __init__(self, monkeypatch, *, events_by_tick, requests_by_tick, decisions_by_tick,
                 delta_by_tick, k=7):
        self.calls: list[tuple[str, int]] = []
        self.k = k
        self.k_seen: list[int] = []
        self.agents = [
            SimpleNamespace(
                id=1, age=30, occupation="mid_skill", home="d1", employed=True,
                job_district="d1", wage_monthly=2000.0, rent_monthly=900.0, satisfaction=0.6,
            ),
            SimpleNamespace(
                id=2, age=40, occupation="mid_skill", home="d1", employed=True,
                job_district="d1", wage_monthly=2200.0, rent_monthly=950.0, satisfaction=0.5,
            ),
        ]
        world = SimpleNamespace(tick=0, states={"d1": None, "d2": None}, profiles={}, rent_history={})

        def fake_load_profiles(path):
            self.calls.append(("load_profiles", 0))
            return []

        def fake_generate_population(profiles, n_agents, rng):
            self.calls.append(("generate_population", 0))
            return self.agents

        def fake_init_world(profiles, agents):
            self.calls.append(("init_world", 0))
            return world

        def fake_apply_policies(w, scenario, tick):
            self.calls.append(("apply_policies", tick))

        def fake_detect_events(w, agents, tick, params, rng):
            self.calls.append(("detect_events", tick))
            return events_by_tick.get(tick, [])

        def fake_build_requests(w, agents, events, tick, agents_per_request):
            self.calls.append(("build_requests", tick))
            self.k_seen.append(agents_per_request)
            return requests_by_tick.get(tick, [])

        def fake_parse_decisions(reqs, responses, threshold, **kwargs):
            tick = reqs[0].tick
            self.calls.append(("parse_decisions", tick))
            return decisions_by_tick.get(tick, [])

        def fake_apply_decisions(w, agents, decisions, scenario, rng, tick):
            self.calls.append(("apply_decisions", tick))
            return delta_by_tick.get(tick, TickDelta(moves=[], changes=[]))

        def fake_daily_update(w, agents, scenario, tick, rng):
            self.calls.append(("daily_update", tick))
            return []

        def fake_build_district_snapshots(w, agents):
            self.calls.append(("build_district_snapshots", w.tick))
            return [
                DistrictSnapshot(
                    id="d1", avg_rent=1000.0, avg_paid_rent=950.0, residents=2,
                    vacancy_rate=0.05, unemployment_rate=0.1, jobs=10, filled_jobs=9,
                    shop_revenue=100.0, avg_satisfaction=0.6, avg_rent_burden=0.3,
                    rent_cap_active=False,
                )
            ]

        def fake_choose_k(cfg, settings, est_tokens_per_agent, est_shared_tokens):
            return self.k

        monkeypatch.setattr(engine_loop.world_loader, "load_profiles", fake_load_profiles)
        monkeypatch.setattr(engine_loop.generator, "generate_population", fake_generate_population)
        monkeypatch.setattr(engine_loop.market, "init_world", fake_init_world)
        monkeypatch.setattr(engine_loop.market, "apply_policies", fake_apply_policies)
        monkeypatch.setattr(engine_loop.triggers, "detect_events", fake_detect_events)
        monkeypatch.setattr(engine_loop.state_builder, "build_requests", fake_build_requests)
        monkeypatch.setattr(engine_loop.decision_parse, "parse_decisions", fake_parse_decisions)
        monkeypatch.setattr(engine_loop.market, "apply_decisions", fake_apply_decisions)
        monkeypatch.setattr(engine_loop.market, "daily_update", fake_daily_update)
        monkeypatch.setattr(
            engine_loop.snapshot, "build_district_snapshots", fake_build_district_snapshots
        )
        monkeypatch.setattr(engine_loop.jev, "choose_agents_per_request", fake_choose_k)


def _scenario(ticks: int) -> Scenario:
    return Scenario(name="engine-test", seed=1, ticks=ticks, n_agents=2, jev=JevConfig(provider="mock"))


@pytest.mark.asyncio
async def test_pipeline_order_ticks_and_usage_diff(tmp_path, monkeypatch):
    events_by_tick = {
        1: [Event(agent_id=1, kind=EventKind.PAYDAY)],
        2: [],  # no events -> build_requests returns [] -> evaluate_many must be skipped
        3: [Event(agent_id=2, kind=EventKind.PAYDAY)],
    }
    req1 = DecisionRequest(request_id="t1-r0", tick=1, agent_ids=[1], state={}, questions={})
    req3 = DecisionRequest(request_id="t3-r0", tick=3, agent_ids=[2], state={}, questions={})
    requests_by_tick = {1: [req1], 3: [req3]}
    decisions_by_tick = {
        1: [AgentDecision(agent_id=1, tick=1, action=Action.STAY, destination=None,
                           spending=0.5, satisfaction=0.5, confidence=0.9)],
        3: [AgentDecision(agent_id=2, tick=3, action=Action.MOVE, destination="d2",
                           spending=0.5, satisfaction=0.6, confidence=0.9)],
    }
    delta_by_tick = {
        3: TickDelta(moves=[MoveRecord(agent_id=2, src="d1", dst="d2")], changes=[]),
    }

    harness = _Harness(
        monkeypatch,
        events_by_tick=events_by_tick,
        requests_by_tick=requests_by_tick,
        decisions_by_tick=decisions_by_tick,
        delta_by_tick=delta_by_tick,
    )

    usage_after_1 = Usage(
        requests=1, input_tokens=50, cost_usd=0.01, cost_source="estimated",
        estimated=True, models_seen={"jev-test-model": 1},
    )
    usage_after_3 = Usage(
        requests=2, input_tokens=90, cost_usd=0.02, cost_source="estimated",
        estimated=True, models_seen={"jev-test-model": 1, "jev-other-model": 1},
    )
    backend = FakeBackend(
        responses_by_tick={1: [_resp("jev-test-model")], 3: [_resp("jev-other-model")]},
        usage_by_tick={1: usage_after_1, 3: usage_after_3},
    )

    scenario = _scenario(ticks=3)
    run_dir = tmp_path / "run1"

    summary = await engine_loop.run_simulation(scenario, run_dir, backend=backend)

    # evaluate_many only called for ticks with requests
    assert backend.eval_calls == [1, 3]
    assert backend.closed is True

    # K from jev.choose_agents_per_request is threaded through to build_requests every tick
    assert harness.k_seen == [7, 7, 7]

    # pipeline order within tick 1
    tick1_calls = [name for name, t in harness.calls if t == 1]
    assert tick1_calls == [
        "apply_policies", "detect_events", "build_requests", "parse_decisions",
        "apply_decisions", "daily_update", "build_district_snapshots",
    ]
    # tick 2 has no requests: parse_decisions must not run
    tick2_calls = [name for name, t in harness.calls if t == 2]
    assert "parse_decisions" not in tick2_calls
    assert tick2_calls == [
        "apply_policies", "detect_events", "build_requests",
        "apply_decisions", "daily_update", "build_district_snapshots",
    ]

    reader = RunReader(run_dir)
    meta = reader.meta()
    assert meta.jev_provider == "mock"
    assert meta.jev_model_requested == "jev-test-model"  # scenario.jev.model None -> settings default

    ticks = list(reader.ticks())
    assert len(ticks) == 3
    assert [t.tick for t in ticks] == [1, 2, 3]

    t1, t2, t3 = ticks
    assert t1.usage_tick.requests == 1
    assert t1.usage_tick.models_seen == {"jev-test-model": 1}
    assert t1.usage_total.requests == 1

    # tick 2: usage before == usage after (no call made) -> zero diff
    assert t2.usage_tick.requests == 0
    assert t2.usage_tick.models_seen == {}
    assert t2.usage_total.requests == 1  # cumulative carried over from tick 1

    # tick 3: usage_tick is the delta since tick 1 (tick 2 made no calls)
    assert t3.usage_tick.requests == 1
    assert t3.usage_tick.input_tokens == 40
    # jev-test-model count didn't change (delta 0, dropped); jev-other-model is new (delta 1)
    assert t3.usage_tick.models_seen == {"jev-other-model": 1}
    assert t3.usage_total.models_seen == {"jev-test-model": 1, "jev-other-model": 1}

    assert len(t3.moves) == 1
    assert t3.moves[0].agent_id == 2
    assert summary.total_moves == 1
    assert summary.ticks == 3
    assert summary.usage.requests == 2

    # summary + agents were written
    assert reader.summary() is not None
    assert (run_dir / "agents.json").exists()


@pytest.mark.asyncio
async def test_model_change_warning_logged_once_and_recorded(tmp_path, monkeypatch, caplog):
    events_by_tick = {
        1: [Event(agent_id=1, kind=EventKind.PAYDAY)],
        2: [Event(agent_id=2, kind=EventKind.PAYDAY)],
    }
    req1 = DecisionRequest(request_id="t1-r0", tick=1, agent_ids=[1], state={}, questions={})
    req2 = DecisionRequest(request_id="t2-r0", tick=2, agent_ids=[2], state={}, questions={})
    requests_by_tick = {1: [req1], 2: [req2]}
    decisions_by_tick = {1: [], 2: []}

    _Harness(
        monkeypatch,
        events_by_tick=events_by_tick,
        requests_by_tick=requests_by_tick,
        decisions_by_tick=decisions_by_tick,
        delta_by_tick={},
    )

    usage1 = Usage(requests=1, models_seen={"model-a": 1})
    usage2 = Usage(requests=2, models_seen={"model-a": 1, "model-b": 1})
    backend = FakeBackend(
        responses_by_tick={1: [_resp("model-a")], 2: [_resp("model-b")]},
        usage_by_tick={1: usage1, 2: usage2},
    )

    scenario = _scenario(ticks=2)
    run_dir = tmp_path / "run_warn"

    with caplog.at_level("WARNING"):
        await engine_loop.run_simulation(scenario, run_dir, backend=backend)

    assert any("model version changed" in rec.message for rec in caplog.records)
    warnings_path = run_dir / "warnings.json"
    assert warnings_path.exists()
    warnings = json.loads(warnings_path.read_text(encoding="utf-8"))
    assert len(warnings) == 1
    assert "model version changed" in warnings[0]


@pytest.mark.asyncio
async def test_budget_exceeded_stops_early_and_records(tmp_path, monkeypatch):
    events_by_tick = {t: [Event(agent_id=1, kind=EventKind.PAYDAY)] for t in range(1, 6)}
    requests_by_tick = {
        t: [DecisionRequest(request_id=f"t{t}-r0", tick=t, agent_ids=[1], state={}, questions={})]
        for t in range(1, 6)
    }
    decisions_by_tick = {1: []}

    _Harness(
        monkeypatch,
        events_by_tick=events_by_tick,
        requests_by_tick=requests_by_tick,
        decisions_by_tick=decisions_by_tick,
        delta_by_tick={},
    )

    usage1 = Usage(requests=1, models_seen={"m": 1})
    backend = FakeBackend(
        responses_by_tick={1: [_resp("m")]},
        usage_by_tick={1: usage1},
        fail_at_tick=2,
        fail_exc=JevBudgetExceeded(cost_so_far=1.0, max_cost_usd=0.5),
    )

    scenario = _scenario(ticks=5)
    run_dir = tmp_path / "run_budget"

    summary = await engine_loop.run_simulation(scenario, run_dir, backend=backend)

    assert backend.eval_calls == [1, 2]
    assert backend.closed is True
    assert summary.ticks == 1

    reader = RunReader(run_dir)
    assert len(list(reader.ticks())) == 1
    warnings = json.loads((run_dir / "warnings.json").read_text(encoding="utf-8"))
    assert any("stopped early" in w and "tick 2" in w for w in warnings)


@pytest.mark.asyncio
async def test_writer_and_backend_closed_on_unexpected_exception(tmp_path, monkeypatch):
    events_by_tick = {t: [Event(agent_id=1, kind=EventKind.PAYDAY)] for t in range(1, 4)}
    requests_by_tick = {
        t: [DecisionRequest(request_id=f"t{t}-r0", tick=t, agent_ids=[1], state={}, questions={})]
        for t in range(1, 4)
    }
    _Harness(
        monkeypatch,
        events_by_tick=events_by_tick,
        requests_by_tick=requests_by_tick,
        decisions_by_tick={1: []},
        delta_by_tick={},
    )

    usage1 = Usage(requests=1, models_seen={"m": 1})
    backend = FakeBackend(
        responses_by_tick={1: [_resp("m")]},
        usage_by_tick={1: usage1},
        fail_at_tick=2,
        fail_exc=RuntimeError("boom"),
    )

    scenario = _scenario(ticks=3)
    run_dir = tmp_path / "run_crash"

    with pytest.raises(RuntimeError, match="boom"):
        await engine_loop.run_simulation(scenario, run_dir, backend=backend)

    assert backend.closed is True  # closed even though the loop raised
    assert (run_dir / "summary.json").exists()  # summary is best-effort written on exception
    reader = RunReader(run_dir)
    summary = reader.summary()
    assert summary is not None
    assert summary.ticks == 1
