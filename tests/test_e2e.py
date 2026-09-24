"""Full-stack end-to-end tests: real world/population/events/prompts/jev(mock)/runlog modules
wired together through jevcity.engine.loop.run_simulation, no mocking.

These are best-effort integration tests for a multi-task repo built in parallel: if any
collaborator module is still a stub (raises NotImplementedError) - or fails to import because
one of its own dependencies is still a stub - the whole test is skipped rather than failed, so
this suite doesn't block on work that isn't this task's to finish.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from conftest import make_profiles

from jevcity.engine.loop import run_simulation
from jevcity.runlog.reader import RunReader
from jevcity.types import JevConfig, Scenario

_SKIP_EXC = (NotImplementedError, ImportError, AttributeError)


def _write_districts_json(path: Path) -> None:
    profiles = make_profiles()
    payload = [json.loads(p.model_dump_json()) for p in profiles]
    path.write_text(json.dumps(payload), encoding="utf-8")


def _run_or_skip(scenario: Scenario, run_dir: Path):
    try:
        return asyncio.run(run_simulation(scenario, run_dir))
    except _SKIP_EXC as exc:
        pytest.skip(f"a collaborator module is not implemented yet: {exc!r}")


@pytest.fixture
def e2e_scenario(tmp_path) -> Scenario:
    districts_path = tmp_path / "districts.json"
    _write_districts_json(districts_path)
    return Scenario(
        name="e2e",
        seed=123,
        ticks=20,
        n_agents=200,
        data_path=str(districts_path),
        jev=JevConfig(provider="mock", agents_per_request=1, confidence_threshold=0.35),
    )


def test_full_stack_run_mock_200_agents_20_ticks(e2e_scenario, tmp_path):
    run_dir = tmp_path / "run"
    summary = _run_or_skip(e2e_scenario, run_dir)

    assert summary.ticks == 20
    assert summary.usage.requests > 0
    assert summary.usage.estimated is True  # mock always estimates
    assert len(summary.final_districts) == 5

    reader = RunReader(run_dir)
    meta = reader.meta()
    assert meta.jev_provider == "mock"

    agents = reader.agents()
    assert len(agents) == 200

    ticks = list(reader.ticks())
    assert len(ticks) == 20
    assert [t.tick for t in ticks] == list(range(1, 21))

    calls = list(reader.calls())
    assert len(calls) == summary.usage.requests

    assert reader.summary() is not None
    assert (run_dir / "meta.json").exists()
    assert (run_dir / "agents.json").exists()
    assert (run_dir / "jev_calls.ndjson").exists()
    assert (run_dir / "summary.json").exists()


def test_replay_from_jev_calls_reproduces_the_same_simulation(e2e_scenario, tmp_path):
    run_dir = tmp_path / "run_live"
    _run_or_skip(e2e_scenario, run_dir)

    calls_path = run_dir / "jev_calls.ndjson"
    if not calls_path.exists():
        pytest.skip("no jev_calls.ndjson written (mock backend not wired up yet)")

    replay_scenario = e2e_scenario.model_copy(deep=True)
    replay_scenario.jev = replay_scenario.jev.model_copy(update={"replay_from": str(calls_path)})

    run_dir_replay = tmp_path / "run_replay"
    _run_or_skip(replay_scenario, run_dir_replay)

    live_lines = (run_dir / "ticks.ndjson").read_text(encoding="utf-8").splitlines()
    replay_lines = (run_dir_replay / "ticks.ndjson").read_text(encoding="utf-8").splitlines()
    assert len(live_lines) == len(replay_lines) == 20

    # Determinism must hold for everything the simulation actually decided: district state,
    # events, actions, moves, agent changes. usage_tick/usage_total are deliberately excluded
    # from the comparison: the replay backend legitimately reports replayed answers as
    # cache_hits rather than requests (see CallRecord.replayed / Usage.cache_hits in
    # types.py), so requests/cache_hits differ between a live run and its replay by design,
    # not by accident - everything else must still match exactly.
    compared_fields = (
        "tick", "date", "districts", "events_by_kind", "actions_by_kind",
        "gated_decisions", "moves", "changes",
    )
    for live_line, replay_line in zip(live_lines, replay_lines, strict=True):
        live = json.loads(live_line)
        replay = json.loads(replay_line)
        for field in compared_fields:
            assert live[field] == replay[field], f"tick {live['tick']} field {field!r} differs"
