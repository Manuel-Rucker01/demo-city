"""Tests for jevcity.engine.batch (multi-seed batch runs + aggregation) and the `batch` /
`compare-batch` CLI commands.

Aggregation math (aggregate_final_metrics/aggregate_series/aggregate_usage/compare_batches) is
tested against small hand-built TickRecord/RunSummary objects - no simulation involved. Resume
and failure isolation are tested against `run_batch` with a monkeypatched `run_simulation`.
There is also one small end-to-end batch through the real mock provider.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from jevcity import cli
from jevcity.engine import batch as engine_batch
from jevcity.runlog.reader import RunReader
from jevcity.types import DistrictSnapshot, RunSummary, Scenario, TickRecord, Usage

# --- helpers -----------------------------------------------------------------------------------


def _district(
    did: str = "d1",
    avg_rent: float = 1000.0,
    vacancy_rate: float = 0.05,
    unemployment_rate: float = 0.1,
    avg_satisfaction: float = 0.6,
    residents: int = 100,
    tourist_units: int = 2,
    shops_open: int = 10,
    online_share: float = 0.2,
    mode_share: dict | None = None,
) -> DistrictSnapshot:
    return DistrictSnapshot(
        id=did, avg_rent=avg_rent, avg_paid_rent=avg_rent, residents=residents,
        vacancy_rate=vacancy_rate, unemployment_rate=unemployment_rate, jobs=50, filled_jobs=45,
        shop_revenue=100.0, avg_satisfaction=avg_satisfaction, avg_rent_burden=0.3,
        rent_cap_active=False, tourist_units=tourist_units, shops_open=shops_open,
        mode_share=mode_share if mode_share is not None else {"metro": 0.5, "car": 0.5},
        online_share=online_share,
    )


def _summary(run_id: str, districts: list[DistrictSnapshot], usage: Usage | None = None) -> RunSummary:
    return RunSummary(
        run_id=run_id, ticks=3, usage=usage or Usage(), total_moves=0,
        final_districts=districts, wall_time_s=0.1,
    )


def _tick(tick: int, districts: list[DistrictSnapshot]) -> TickRecord:
    return TickRecord(
        tick=tick, date="2026-01-01", districts=districts, events_by_kind={}, actions_by_kind={},
        gated_decisions=0, moves=[], changes=[], usage_tick=Usage(), usage_total=Usage(),
    )


# --- aggregate_final_metrics --------------------------------------------------------------------


def test_aggregate_final_metrics_mean_std_min_max():
    summaries = [
        _summary("s1", [_district(avg_rent=900.0, mode_share={"metro": 0.4, "car": 0.6})]),
        _summary("s2", [_district(avg_rent=1000.0, mode_share={"metro": 0.6, "car": 0.4})]),
        _summary("s3", [_district(avg_rent=1100.0, mode_share={"metro": 0.5, "car": 0.5})]),
    ]

    result = engine_batch.aggregate_final_metrics(summaries)

    rent = result["d1"]["avg_rent"]
    assert rent["mean"] == pytest.approx(1000.0)
    assert rent["min"] == pytest.approx(900.0)
    assert rent["max"] == pytest.approx(1100.0)
    # population std of [900, 1000, 1100] = sqrt(((-100)^2+0^2+100^2)/3) = sqrt(20000/3)
    assert rent["std"] == pytest.approx((20000 / 3) ** 0.5)

    metro = result["d1"]["mode_share"]["metro"]
    assert metro["mean"] == pytest.approx(0.5)


def test_aggregate_final_metrics_missing_district_counts_only_runs_that_have_it():
    summaries = [
        _summary("s1", [_district("d1", avg_rent=1000.0), _district("d2", avg_rent=500.0)]),
        _summary("s2", [_district("d1", avg_rent=1200.0)]),  # d2 missing this seed
    ]

    result = engine_batch.aggregate_final_metrics(summaries)

    assert result["d1"]["avg_rent"]["mean"] == pytest.approx(1100.0)
    # d2 only appears in one run -> stats computed over that single value
    assert result["d2"]["avg_rent"]["mean"] == pytest.approx(500.0)
    assert result["d2"]["avg_rent"]["std"] == pytest.approx(0.0)


def test_aggregate_final_metrics_missing_mode_counts_as_zero_share():
    summaries = [
        _summary("s1", [_district(mode_share={"metro": 1.0})]),
        _summary("s2", [_district(mode_share={"car": 1.0})]),  # no metro at all this seed
    ]

    result = engine_batch.aggregate_final_metrics(summaries)

    # metro share: 1.0 in s1, 0.0 (absent) in s2 -> mean 0.5
    assert result["d1"]["mode_share"]["metro"]["mean"] == pytest.approx(0.5)
    assert result["d1"]["mode_share"]["car"]["mean"] == pytest.approx(0.5)


# --- aggregate_series ----------------------------------------------------------------------------


def test_aggregate_series_aligns_by_tick_and_computes_mean_min_max():
    run1 = [_tick(1, [_district(avg_rent=900.0, avg_satisfaction=0.5)]),
            _tick(2, [_district(avg_rent=910.0, avg_satisfaction=0.55)])]
    run2 = [_tick(1, [_district(avg_rent=1100.0, avg_satisfaction=0.7)]),
            _tick(2, [_district(avg_rent=1110.0, avg_satisfaction=0.75)])]

    series = engine_batch.aggregate_series([run1, run2])

    rent_series = series["d1"]["avg_rent"]
    assert [p["tick"] for p in rent_series] == [1, 2]
    assert rent_series[0]["mean"] == pytest.approx(1000.0)
    assert rent_series[0]["min"] == pytest.approx(900.0)
    assert rent_series[0]["max"] == pytest.approx(1100.0)
    assert rent_series[1]["mean"] == pytest.approx(1010.0)

    sat_series = series["d1"]["avg_satisfaction"]
    assert sat_series[0]["mean"] == pytest.approx(0.6)

    # mode_share.metro / mode_share.car always present, at least metro/car
    assert "mode_share.metro" in series["d1"]
    assert "mode_share.car" in series["d1"]


def test_aggregate_series_handles_runs_with_unequal_tick_counts():
    """A run stopped early (fewer ticks) still contributes to the ticks it has; a tick present
    in only one run is aggregated over just that run, not synthesized for the missing one."""
    run1 = [_tick(1, [_district(avg_rent=1000.0)])]
    run2 = [_tick(1, [_district(avg_rent=1000.0)]), _tick(2, [_district(avg_rent=1200.0)])]

    series = engine_batch.aggregate_series([run1, run2])

    rent_series = series["d1"]["avg_rent"]
    assert [p["tick"] for p in rent_series] == [1, 2]
    assert rent_series[0]["mean"] == pytest.approx(1000.0)  # both runs have tick 1
    assert rent_series[1]["mean"] == pytest.approx(1200.0)  # only run2 has tick 2
    assert rent_series[1]["min"] == rent_series[1]["max"] == pytest.approx(1200.0)


# --- aggregate_usage -------------------------------------------------------------------------


def test_aggregate_usage_sums_across_runs():
    summaries = [
        _summary("s1", [_district()], usage=Usage(requests=5, input_tokens=100, cost_usd=0.1, cost_source="reported")),
        _summary("s2", [_district()], usage=Usage(requests=7, input_tokens=200, cost_usd=0.2, cost_source="reported")),
    ]

    total = engine_batch.aggregate_usage(summaries)

    assert total["requests"] == 12
    assert total["input_tokens"] == 300
    assert total["cost_usd"] == pytest.approx(0.3)
    assert total["cost_source"] == "reported"


# --- run_batch: resume + failure isolation -----------------------------------------------------


def test_run_batch_resumes_when_summary_already_exists(tmp_path):
    batch_dir = tmp_path / "b1"
    scenario = Scenario(name="s", ticks=2, n_agents=3)

    # Pre-populate seed 1's run dir with a finished run (summary.json present).
    from jevcity.runlog.writer import RunWriter
    from jevcity.types import RunMeta

    seed1_dir = engine_batch.seed_run_dir(batch_dir, 1)
    writer = RunWriter(seed1_dir)
    writer.write_meta(RunMeta(run_id="s1", created_at="2026-01-01T00:00:00+00:00", scenario=scenario, profiles=[]))
    writer.write_summary(_summary("s1", [_district(avg_rent=777.0)]))
    writer.close()

    calls = {"count": 0}

    async def fake_run_simulation(scenario, run_dir, backend=None):
        calls["count"] += 1
        return _summary(run_dir.name, [_district(avg_rent=123.0)])

    import jevcity.engine.loop as engine_loop

    orig = engine_loop.run_simulation
    engine_loop.run_simulation = fake_run_simulation
    try:
        results = asyncio.run(engine_batch.run_batch(scenario, batch_dir, [1, 2], parallel=1))
    finally:
        engine_loop.run_simulation = orig

    by_seed = {r.seed: r for r in results}
    assert by_seed[1].skipped is True
    assert by_seed[1].summary.final_districts[0].avg_rent == pytest.approx(777.0)
    assert by_seed[2].skipped is False
    # only seed 2 actually ran the (fake) simulation
    assert calls["count"] == 1


def test_run_batch_isolates_failures(tmp_path):
    batch_dir = tmp_path / "b2"
    scenario = Scenario(name="s", ticks=2, n_agents=3)

    async def fake_run_simulation(scenario, run_dir, backend=None):
        if run_dir.name == "s2":
            raise RuntimeError("boom")
        return _summary(run_dir.name, [_district()])

    import jevcity.engine.loop as engine_loop

    orig = engine_loop.run_simulation
    engine_loop.run_simulation = fake_run_simulation
    try:
        results = asyncio.run(engine_batch.run_batch(scenario, batch_dir, [1, 2, 3], parallel=2))
    finally:
        engine_loop.run_simulation = orig

    by_seed = {r.seed: r for r in results}
    assert by_seed[1].error is None
    assert by_seed[1].summary is not None
    assert by_seed[2].error is not None
    assert "boom" in by_seed[2].error
    assert by_seed[2].summary is None
    assert by_seed[3].error is None  # seed 3 continues despite seed 2's failure

    payload = engine_batch.aggregate_batch("s", "mock", results)
    assert payload["succeeded_seeds"] == [1, 3]
    assert payload["failed"] == [{"seed": 2, "error": by_seed[2].error}]
    # final_metrics still computed from the 2 successful seeds
    assert "d1" in payload["final_metrics"]


# --- compare_batches -------------------------------------------------------------------------


def test_compare_batches_flags_clear_and_noisy_effects():
    # avg_rent: A ~ 1000 +/- 5 (tight), B ~ 1200 +/- 5 (tight, big separation) -> clear
    summary_a = {
        "final_metrics": {
            "d1": {
                "avg_rent": {"mean": 1000.0, "std": 5.0, "min": 995.0, "max": 1005.0},
                "avg_satisfaction": {"mean": 0.60, "std": 0.10, "min": 0.5, "max": 0.7},
            }
        }
    }
    summary_b = {
        "final_metrics": {
            "d1": {
                "avg_rent": {"mean": 1200.0, "std": 5.0, "min": 1195.0, "max": 1205.0},
                # satisfaction barely moved but noise (std) is wide -> not clear
                "avg_satisfaction": {"mean": 0.62, "std": 0.10, "min": 0.5, "max": 0.75},
            }
        }
    }

    rows = engine_batch.compare_batches(summary_a, summary_b)
    by_metric = {r["metric"]: r for r in rows}

    assert by_metric["avg_rent"]["clear"] is True
    assert by_metric["avg_rent"]["effect"] == pytest.approx(200.0)
    assert by_metric["avg_satisfaction"]["clear"] is False


def test_compare_batches_zero_noise_nonzero_effect_is_clear():
    summary_a = {"final_metrics": {"d1": {"avg_rent": {"mean": 1000.0, "std": 0.0, "min": 1000.0, "max": 1000.0}}}}
    summary_b = {"final_metrics": {"d1": {"avg_rent": {"mean": 1000.0, "std": 0.0, "min": 1000.0, "max": 1000.0}}}}
    rows = engine_batch.compare_batches(summary_a, summary_b)
    assert rows[0]["clear"] is False  # effect == 0 too

    summary_b2 = {"final_metrics": {"d1": {"avg_rent": {"mean": 1050.0, "std": 0.0, "min": 1050.0, "max": 1050.0}}}}
    rows2 = engine_batch.compare_batches(summary_a, summary_b2)
    assert rows2[0]["clear"] is True


# --- write/read_batch_summary + load_run_ticks -------------------------------------------------


def test_write_and_read_batch_summary_roundtrip(tmp_path):
    payload = {"scenario": "s", "seeds": [1, 2], "final_metrics": {}}
    path = engine_batch.write_batch_summary(tmp_path, payload)
    assert path.exists()
    loaded = engine_batch.read_batch_summary(tmp_path)
    assert loaded == payload


def test_read_batch_summary_missing_returns_none(tmp_path):
    assert engine_batch.read_batch_summary(tmp_path) is None


# --- CLI: batch parsing / guards ---------------------------------------------------------------


def test_cli_batch_rejects_invalid_parallel(tmp_path):
    code = cli.main([
        "batch", "--scenario", "scenarios/base.yaml", "--seeds", "2",
        "--parallel", "5", "--out", str(tmp_path),
    ])
    assert code == 2


def test_cli_batch_rejects_zero_seeds(tmp_path):
    code = cli.main([
        "batch", "--scenario", "scenarios/base.yaml", "--seeds", "0", "--out", str(tmp_path),
    ])
    assert code == 2


def test_cli_batch_non_mock_without_yes_is_refused(tmp_path, monkeypatch):
    from jevcity.types import ProviderSettings

    async def fake_run_simulation(scenario, run_dir, backend=None):
        return _summary(run_dir.name, [_district()])

    async def fake_probe(scenario, ticks):
        return RunSummary(
            run_id="probe", ticks=ticks, usage=Usage(requests=ticks, input_tokens=ticks * 100),
            total_moves=0, final_districts=[], wall_time_s=0.0,
        )

    settings = ProviderSettings(
        base_url="https://example.test", path="/v1", wire="systemone",
        api_key_env="EXAMPLE_KEY", default_model="jev-x", rpm_limit=1200,
        tps_limit=250_000, price_per_mtok_usd=0.042,
    )

    monkeypatch.setattr(cli.engine_loop, "run_simulation", fake_run_simulation)
    monkeypatch.setattr(cli, "_run_mock_probe", fake_probe)
    monkeypatch.setattr(cli.jev, "resolve_provider", lambda cfg: ("typesafe", settings))
    monkeypatch.setattr(cli.jev, "choose_agents_per_request", lambda cfg, s, **kw: 1)

    code = cli.main([
        "batch", "--scenario", "scenarios/base.yaml", "--seeds", "2", "--out", str(tmp_path),
    ])
    assert code == 2
    assert not (tmp_path).exists() or not any((tmp_path).iterdir())


# --- CLI: compare-batch ---------------------------------------------------------------------


def test_cli_compare_batch_prints_clear_and_noisy_rows(tmp_path, capsys):
    batch_a = tmp_path / "batch-a"
    batch_b = tmp_path / "batch-b"
    batch_a.mkdir()
    batch_b.mkdir()
    engine_batch.write_batch_summary(batch_a, {
        "scenario": "s", "seeds": [1, 2, 3],
        "final_metrics": {"d1": {"avg_rent": {"mean": 1000.0, "std": 5.0, "min": 995, "max": 1005}}},
    })
    engine_batch.write_batch_summary(batch_b, {
        "scenario": "s", "seeds": [1, 2, 3],
        "final_metrics": {"d1": {"avg_rent": {"mean": 1300.0, "std": 5.0, "min": 1295, "max": 1305}}},
    })

    code = cli.main(["compare-batch", str(batch_a), str(batch_b)])
    assert code == 0
    out = capsys.readouterr().out
    assert "d1.avg_rent" in out
    assert "CLEAR" in out
    assert "heuristic" in out.lower()


def test_cli_compare_batch_requires_finished_batches(tmp_path, capsys):
    batch_a = tmp_path / "batch-a"
    batch_a.mkdir()
    engine_batch.write_batch_summary(batch_a, {"scenario": "s", "seeds": [1], "final_metrics": {}})
    missing = tmp_path / "no-such-batch"

    code = cli.main(["compare-batch", str(batch_a), str(missing)])
    assert code == 2
    assert "batch_summary.json" in capsys.readouterr().err


# --- end-to-end: a small real mock batch --------------------------------------------------------


def test_end_to_end_mock_batch_produces_batch_summary(tmp_path):
    code = cli.main([
        "batch", "--scenario", "scenarios/base.yaml", "--seeds", "3",
        "--agents", "60", "--ticks", "15", "--parallel", "2",
        "--batch-id", "e2e-mock-batch", "--out", str(tmp_path),
    ])
    assert code == 0

    batch_dir = tmp_path / "e2e-mock-batch"
    summary_path = batch_dir / "batch_summary.json"
    assert summary_path.exists()

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["seeds"] == [1, 2, 3]
    assert payload["succeeded_seeds"] == [1, 2, 3]
    assert payload["failed"] == []
    assert payload["usage_total"]["requests"] > 0
    assert payload["final_metrics"]  # at least one district
    some_district = next(iter(payload["final_metrics"].values()))
    assert "avg_rent" in some_district
    assert set(some_district["avg_rent"]) == {"mean", "std", "min", "max"}
    assert "series" in payload
    some_series = next(iter(payload["series"].values()))
    assert "avg_rent" in some_series
    assert "mode_share.metro" in some_series
    assert "mode_share.car" in some_series

    # each seed's run dir is independently readable as a normal run
    for seed in (1, 2, 3):
        run_dir = batch_dir / f"s{seed}"
        reader = RunReader(run_dir)
        assert reader.meta().scenario.seed == seed
        assert reader.summary() is not None

    # re-launching the same batch resumes every seed instead of re-running (no crash, no dup cost)
    code2 = cli.main([
        "batch", "--scenario", "scenarios/base.yaml", "--seeds", "3",
        "--agents", "60", "--ticks", "15", "--parallel", "2",
        "--batch-id", "e2e-mock-batch", "--out", str(tmp_path),
    ])
    assert code2 == 0


def test_export_web_prefixes_seed_runs_with_batch_id(tmp_path):
    """Seeds from two batches (both s1..sN) must not overwrite each other on the web."""
    import json as _json

    from jevcity.cli import main

    runs = tmp_path / "runs"
    for b in ("batch-a", "batch-b"):
        assert main(["batch", "--scenario", "scenarios/base.yaml", "--seeds", "1", "--agents", "20",
                     "--ticks", "3", "--provider", "mock", "--batch-id", b, "--out", str(runs)]) == 0
    dest = tmp_path / "web" / "public" / "runs"
    assert main(["export-web", str(runs / "batch-a"), str(runs / "batch-b"), "--dest", str(dest),
                 "--geojson", str(tmp_path / "none.geojson")]) == 0
    ids = {e["run_id"] for e in _json.loads((dest / "index.json").read_text())}
    assert {"batch-a-s1", "batch-b-s1"} <= ids
    assert _json.loads((dest / "batch-a-s1" / "meta.json").read_text())["run_id"] == "batch-a-s1"
