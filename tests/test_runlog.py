"""Round-trip tests for jevcity.runlog (RunWriter / RunReader)."""

import json
import logging

import pytest

from jevcity.runlog import RunReader, RunWriter
from jevcity.types import (
    AgentChange,
    AgentSnapshot,
    CallRecord,
    DistrictSnapshot,
    JevResponse,
    JevUsage,
    MoveRecord,
    Occupation,
    RunMeta,
    RunSummary,
    Scenario,
    TickRecord,
    Usage,
)


def make_district_snapshot(district_id: str, name_suffix: str = "") -> DistrictSnapshot:
    return DistrictSnapshot(
        id=district_id,
        avg_rent=1150.0,
        avg_paid_rent=1080.5,
        residents=24_600,
        vacancy_rate=0.03,
        unemployment_rate=0.07,
        jobs=5000,
        filled_jobs=4650,
        shop_revenue=125_430.75,
        avg_satisfaction=0.62,
        avg_rent_burden=0.31,
        rent_cap_active=False,
    )


def make_agent_snapshot(agent_id: int, home: str) -> AgentSnapshot:
    return AgentSnapshot(
        id=agent_id,
        age=34,
        occupation=Occupation.MID_SKILL,
        home=home,
        employed=True,
        job_district="eixample",
        wage_monthly=2100.0,
        rent_monthly=1150.0,
        satisfaction=0.58,
    )


def make_usage(n: int = 1) -> Usage:
    return Usage(
        requests=n,
        input_tokens=120 * n,
        output_tokens=0,
        cost_usd=0.0004 * n,
        estimated=True,
        retries=0,
        errors=0,
        cache_hits=0,
    )


def make_tick_record(tick: int, district_ids: list[str], n_moves: int = 3, n_changes: int = 10) -> TickRecord:
    moves = [
        MoveRecord(
            agent_id=i,
            src=district_ids[i % len(district_ids)],
            dst=district_ids[(i + 1) % len(district_ids)],
        )
        for i in range(n_moves)
    ]
    changes = [
        AgentChange(agent_id=i, employed=(i % 2 == 0), satisfaction=0.5 + (i % 10) / 100)
        for i in range(n_changes)
    ]
    return TickRecord(
        tick=tick,
        date="2026-01-01",
        districts=[make_district_snapshot(d) for d in district_ids],
        events_by_kind={"payday": 12, "job_loss": 1},
        actions_by_kind={"stay": 800, "move": n_moves, "job_search": 40},
        gated_decisions=2,
        moves=moves,
        changes=changes,
        usage_tick=make_usage(1),
        usage_total=make_usage(tick + 1),
    )


def make_call_record(tick: int, request_id: str, cache_key: str) -> CallRecord:
    response = JevResponse(
        model="jev-1.13.0",
        answers={
            "1:action": {"type": "choice", "choice": "stay", "probabilities": {"stay": 0.7}, "confidence": 0.8},
            "1:satisfaction": {"type": "score", "score": 3, "legend": "content", "probabilities": {}, "confidence": 0.6},
        },
        usage=JevUsage(input_tokens=150, output_tokens=0),
    )
    return CallRecord(
        tick=tick,
        request_id=request_id,
        cache_key=cache_key,
        provider="mock",
        requested_model="jev-1.13.0",
        resolved_model="jev-1.13.0",
        request={"state": {"today": "2026-01-01"}, "model": "jev-1.13.0", "questions": {}},
        response=response,
        latency_ms=12.5,
        attempts=1,
    )


def make_run_meta(profiles, run_id: str = "run-test-1") -> RunMeta:
    return RunMeta(
        run_id=run_id,
        created_at="2026-09-24T00:00:00Z",
        scenario=Scenario(name="base", n_agents=1000, ticks=365),
        profiles=profiles,
    )


# --- fixtures ---------------------------------------------------------------------------


@pytest.fixture
def district_ids(profiles) -> list[str]:
    return [p.id for p in profiles]


# --- tests -------------------------------------------------------------------------------


def test_round_trip_full_run(tmp_path, profiles, district_ids):
    run_dir = tmp_path / "run1"
    meta = make_run_meta(profiles)
    agents = [make_agent_snapshot(i, district_ids[i % len(district_ids)]) for i in range(25)]
    ticks = [make_tick_record(t, district_ids) for t in range(5)]
    calls = [make_call_record(t, f"req-{t}", f"cachekey-{t}") for t in range(5)]
    summary = RunSummary(
        run_id=meta.run_id,
        ticks=len(ticks),
        usage=make_usage(5),
        total_moves=15,
        final_districts=[make_district_snapshot(d) for d in district_ids],
        wall_time_s=12.34,
    )

    writer = RunWriter(run_dir)
    writer.write_meta(meta)
    writer.write_agents(agents)
    for t in ticks:
        writer.write_tick(t)
    for c in calls:
        writer.write_call(c)
    writer.write_summary(summary)
    writer.close()

    reader = RunReader(run_dir)
    assert reader.meta() == meta
    assert reader.agents() == agents
    read_ticks = list(reader.ticks())
    assert read_ticks == ticks
    read_calls = list(reader.calls())
    assert read_calls == calls
    assert reader.summary() == summary
    assert reader.tick_count() == len(ticks)


def test_truncated_last_line_is_skipped(tmp_path, profiles, district_ids, caplog):
    run_dir = tmp_path / "run_trunc"
    writer = RunWriter(run_dir)
    good_ticks = [make_tick_record(0, district_ids), make_tick_record(1, district_ids)]
    for t in good_ticks:
        writer.write_tick(t)
    writer.close()

    # Simulate a crash mid-write: append a truncated JSON line.
    with open(run_dir / "ticks.ndjson", "a", encoding="utf-8") as fh:
        fh.write('{"tick": 2, "date": "2026-01-03", "districts": [')

    reader = RunReader(run_dir)
    with caplog.at_level(logging.WARNING):
        read_ticks = list(reader.ticks())
    assert read_ticks == good_ticks
    assert any("truncated" in rec.message.lower() for rec in caplog.records)


def test_overwrite_guard(tmp_path):
    run_dir = tmp_path / "run_guard"
    writer = RunWriter(run_dir)
    writer.write_tick(make_tick_record(0, ["gracia"]))
    writer.close()

    with pytest.raises(FileExistsError):
        RunWriter(run_dir)

    # overwrite=True should succeed and allow appending further.
    writer2 = RunWriter(run_dir, overwrite=True)
    writer2.write_tick(make_tick_record(1, ["gracia"]))
    writer2.close()

    reader = RunReader(run_dir)
    assert reader.tick_count() == 2


def test_close_idempotent(tmp_path):
    run_dir = tmp_path / "run_close"
    writer = RunWriter(run_dir)
    writer.write_tick(make_tick_record(0, ["gracia"]))
    writer.close()
    writer.close()  # must not raise


def test_context_manager(tmp_path, district_ids):
    run_dir = tmp_path / "run_ctx"
    with RunWriter(run_dir) as writer:
        writer.write_tick(make_tick_record(0, district_ids))
        writer.write_call(make_call_record(0, "req-0", "key-0"))

    reader = RunReader(run_dir)
    assert reader.tick_count() == 1
    assert len(list(reader.calls())) == 1


def test_missing_summary_returns_none(tmp_path, district_ids):
    run_dir = tmp_path / "run_no_summary"
    writer = RunWriter(run_dir)
    writer.write_tick(make_tick_record(0, district_ids))
    writer.close()

    reader = RunReader(run_dir)
    assert reader.summary() is None


def test_unicode_district_names_preserved(tmp_path, profiles):
    run_dir = tmp_path / "run_unicode"
    meta = make_run_meta(profiles)
    writer = RunWriter(run_dir)
    writer.write_meta(meta)
    writer.close()

    reader = RunReader(run_dir)
    read_meta = reader.meta()
    names = {p.id: p.name for p in read_meta.profiles}
    assert names["gracia"] == "Gràcia"
    assert names["sant_marti"] == "Sant Martí"

    # Also verify the raw bytes on disk are proper UTF-8, not escaped ASCII.
    raw = (run_dir / "meta.json").read_text(encoding="utf-8")
    assert "Gràcia" in raw


def test_calls_index_by_cache_key(tmp_path):
    run_dir = tmp_path / "run_index"
    writer = RunWriter(run_dir)
    calls = [make_call_record(t, f"req-{t}", f"cachekey-{t}") for t in range(4)]
    for c in calls:
        writer.write_call(c)
    writer.close()

    reader = RunReader(run_dir)
    index = reader.load_calls_index()
    assert set(index.keys()) == {f"cachekey-{t}" for t in range(4)}
    for t in range(4):
        assert index[f"cachekey-{t}"] == calls[t].response


def test_agents_json_is_valid_json_list(tmp_path):
    run_dir = tmp_path / "run_agents_shape"
    writer = RunWriter(run_dir)
    agents = [make_agent_snapshot(i, "gracia") for i in range(3)]
    writer.write_agents(agents)
    writer.close()

    raw = (run_dir / "agents.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert isinstance(data, list)
    assert len(data) == 3
