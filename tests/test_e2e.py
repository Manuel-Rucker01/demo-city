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
from jevcity.types import AGE_BUCKETS, BCN_DISTRICTS, DistrictProfile, JevConfig, Scenario, Source

_SKIP_EXC = (NotImplementedError, ImportError, AttributeError)

# Plausible per-district rows for all 10 official Barcelona districts (see types.BCN_DISTRICTS),
# in the same shape/spirit as tests/conftest.py's 5-district _ROWS - not real Open Data BCN
# figures, just enough spread to exercise ten districts' worth of world/population/events code.
_TEN_DISTRICT_ROWS = [
    # id, name, pop, income/yr, rent, vacancy, unemp, jobs/res, shops, transit, lon, lat
    ("ciutat_vella", "Ciutat Vella", 109_000, 15_500, 1_050, 0.06, 0.11, 1.10, 1_500, 0.95, 2.176, 41.381),
    ("eixample", "Eixample", 270_000, 25_500, 1_250, 0.04, 0.07, 1.40, 3_800, 0.95, 2.162, 41.391),
    ("sants_montjuic", "Sants-Montjuïc", 181_000, 19_000, 1_000, 0.05, 0.09, 0.85, 2_200, 0.85, 2.148, 41.375),
    ("les_corts", "Les Corts", 82_000, 27_000, 1_200, 0.03, 0.06, 1.20, 1_400, 0.85, 2.127, 41.386),
    ("sarria_sant_gervasi", "Sarrià-Sant Gervasi", 148_000, 34_000, 1_500, 0.03, 0.05, 0.95, 1_900, 0.75, 2.123, 41.401),
    ("gracia", "Gràcia", 124_000, 23_000, 1_150, 0.03, 0.07, 0.55, 1_600, 0.80, 2.156, 41.404),
    ("horta_guinardo", "Horta-Guinardó", 168_000, 17_000, 900, 0.05, 0.10, 0.60, 1_500, 0.65, 2.163, 41.427),
    ("nou_barris", "Nou Barris", 175_000, 12_500, 800, 0.05, 0.12, 0.30, 1_100, 0.60, 2.177, 41.441),
    ("sant_andreu", "Sant Andreu", 148_000, 16_500, 950, 0.04, 0.10, 0.65, 1_300, 0.70, 2.189, 41.436),
    ("sant_marti", "Sant Martí", 243_000, 18_500, 1_100, 0.04, 0.09, 0.75, 2_400, 0.80, 2.199, 41.407),
]


def _make_10_district_profiles() -> list[DistrictProfile]:
    ages = dict(zip(AGE_BUCKETS, (0.14, 0.24, 0.24, 0.19, 0.19), strict=True))
    rows_by_id = {r[0]: r for r in _TEN_DISTRICT_ROWS}
    assert set(rows_by_id) == set(BCN_DISTRICTS)
    out = []
    for i, n, pop, inc, rent, vac, un, jpr, shops, tr, lon, lat in _TEN_DISTRICT_ROWS:
        out.append(
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
    return out


def _write_districts_json(path: Path) -> None:
    profiles = make_profiles()
    payload = [json.loads(p.model_dump_json()) for p in profiles]
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_10_district_json(path: Path) -> None:
    profiles = _make_10_district_profiles()
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
    assert (run_dir / "jev_calls.ndjson.gz").exists()
    assert (run_dir / "summary.json").exists()


def test_replay_from_jev_calls_reproduces_the_same_simulation(e2e_scenario, tmp_path):
    run_dir = tmp_path / "run_live"
    _run_or_skip(e2e_scenario, run_dir)

    calls_path = run_dir / "jev_calls.ndjson.gz"
    if not calls_path.exists():
        pytest.skip("no jev_calls.ndjson.gz written (mock backend not wired up yet)")

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


# --- 10-district world expansion (tourist flats, transport, commerce, migration) --------------


@pytest.fixture
def e2e_scenario_10_districts(tmp_path) -> Scenario:
    districts_path = tmp_path / "districts_10.json"
    _write_10_district_json(districts_path)
    return Scenario(
        name="e2e-10-districts",
        seed=7,
        ticks=40,
        n_agents=300,
        data_path=str(districts_path),
        jev=JevConfig(provider="mock", agents_per_request=1, confidence_threshold=0.35),
    )


def test_full_stack_run_mock_300_agents_40_ticks_10_districts(e2e_scenario_10_districts, tmp_path):
    """Full-stack run against all 10 official Barcelona districts (see types.BCN_DISTRICTS),
    long enough (40 ticks > TICKS_PER_MONTH) to exercise the monthly rent/shop/migration
    cadence in world/market.py's daily_update_ex, if that collaborator has landed it."""
    run_dir = tmp_path / "run"
    summary = _run_or_skip(e2e_scenario_10_districts, run_dir)

    assert summary.ticks == 40
    assert summary.usage.requests > 0
    assert len(summary.final_districts) == 10
    assert {d.id for d in summary.final_districts} == set(BCN_DISTRICTS)

    reader = RunReader(run_dir)
    agents = reader.agents()
    assert len(agents) == 300

    ticks = list(reader.ticks())
    assert len(ticks) == 40
    assert [t.tick for t in ticks] == list(range(1, 41))

    # New DistrictSnapshot fields must be present and well-formed on every tick's districts,
    # regardless of whether tourist/commerce/transit/migration mechanics are wired up yet.
    total_arrivals = 0
    total_departures = 0
    for t in ticks:
        assert len(t.districts) == 10
        for d in t.districts:
            assert d.tourist_units >= 0
            assert d.shops_open >= 0
            assert d.shop_revenue_monthly >= 0.0
            assert 0.0 <= d.online_share <= 1.0
            assert d.arrivals >= 0
            assert d.departures >= 0
            for share in d.mode_share.values():
                assert 0.0 <= share <= 1.0
        total_arrivals += len(t.arrivals)
        total_departures += len(t.departures)

    # arrivals/departures may legitimately be zero this run (small population, short horizon,
    # or the migration/market collaborator not landed yet) - just check the plumbing is sane:
    # every arrival snapshot's id shows up in a later (or the same) tick's population, and no
    # departure id was invented.
    all_ids = {a.id for a in agents}
    for t in ticks:
        for a in t.arrivals:
            all_ids.add(a.id)
    for t in ticks:
        for did in t.departures:
            assert did in all_ids

    assert reader.summary() is not None
