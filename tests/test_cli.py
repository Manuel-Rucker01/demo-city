"""Tests for jevcity.cli.

The `run`/`estimate` paths that would otherwise call the (still WIP, per other tasks) real
jev.resolve_provider/make_backend and engine.loop.run_simulation are exercised against
monkeypatched fakes, so these tests check argument parsing, overrides, the --yes guard and
output formatting - not the real simulation or provider machinery.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from jevcity import cli
from jevcity.runlog.writer import RunWriter
from jevcity.types import (
    AgentSnapshot,
    DistrictProfile,
    DistrictSnapshot,
    ProviderSettings,
    RunMeta,
    RunSummary,
    Scenario,
    Source,
    TickRecord,
    Usage,
)

AGE_BUCKETS = ("0-17", "18-34", "35-49", "50-64", "65+")


def _profile(did: str, name: str) -> DistrictProfile:
    ages = dict(zip(AGE_BUCKETS, (0.14, 0.24, 0.24, 0.19, 0.19)))
    return DistrictProfile(
        id=did, name=name, population=1000, age_distribution=ages,
        income_per_capita_annual=20000.0, avg_rent_monthly=1000.0, vacancy_rate=0.05,
        unemployment_rate=0.1, jobs_per_resident=1.0, shops=100, transit_score=0.8,
        centroid=(2.17, 41.38),
        sources={f: Source.PLAUSIBLE for f in (
            "population", "age_distribution", "income_per_capita_annual", "avg_rent_monthly",
            "vacancy_rate", "unemployment_rate", "jobs_per_resident", "shops", "transit_score",
        )},
    )


def _settings(**overrides) -> ProviderSettings:
    base = {
        "base_url": "https://example.test", "path": "/v1", "wire": "systemone",
        "api_key_env": "EXAMPLE_KEY", "default_model": "jev-x", "rpm_limit": 1200,
        "tps_limit": 250_000, "price_per_mtok_usd": 0.042,
    }
    base.update(overrides)
    return ProviderSettings(**base)


def _make_run_dir(
    tmp_path, run_id: str, provider: str = "mock", ticks: int = 2, *,
    tourist_units: int = 3, shops_open: int = 12, online_share: float = 0.2,
    mode_share: dict | None = None, tick_arrivals: int = 1, tick_departures: int = 0,
):
    scenario = Scenario(name="cmp-scenario", ticks=ticks, n_agents=5)
    profile = _profile("d1", "D1")
    writer = RunWriter(tmp_path / run_id)
    writer.write_meta(
        RunMeta(
            run_id=run_id, created_at="2026-01-01T00:00:00+00:00", scenario=scenario,
            profiles=[profile], jev_provider=provider, jev_model_requested="jev-x",
        )
    )
    agent_snapshot = AgentSnapshot(
        id=0, age=30, occupation="mid_skill", home="d1", employed=True,
        job_district="d1", wage_monthly=2000.0, rent_monthly=900.0, satisfaction=0.6,
    )
    writer.write_agents([agent_snapshot])
    mode_share = mode_share if mode_share is not None else {"metro": 0.6, "car": 0.4}
    district = DistrictSnapshot(
        id="d1", avg_rent=1100.0, avg_paid_rent=1000.0, residents=5, vacancy_rate=0.04,
        unemployment_rate=0.08, jobs=20, filled_jobs=18, shop_revenue=50.0,
        avg_satisfaction=0.65, avg_rent_burden=0.32, rent_cap_active=False,
        tourist_units=tourist_units, shops_open=shops_open, shop_revenue_monthly=5000.0,
        mode_share=mode_share, online_share=online_share,
    )
    for tick in range(1, ticks + 1):
        writer.write_tick(
            TickRecord(
                tick=tick, date="2026-01-01", districts=[district], events_by_kind={},
                actions_by_kind={}, gated_decisions=0, moves=[], changes=[],
                usage_tick=Usage(), usage_total=Usage(),
                arrivals=[agent_snapshot] if tick_arrivals else [],
                departures=[0] if tick_departures else [],
            )
        )
    writer.write_summary(
        RunSummary(
            run_id=run_id, ticks=ticks, usage=Usage(requests=2, cost_usd=0.01, cost_source="estimated"),
            total_moves=1, final_districts=[district], wall_time_s=0.01,
        )
    )
    writer.close()
    return tmp_path / run_id


# --- run: parsing & overrides -----------------------------------------------------------------


def test_run_applies_cli_overrides_and_default_run_id(tmp_path, monkeypatch):
    captured = {}

    async def fake_run_simulation(scenario, run_dir, backend=None):
        captured["scenario"] = scenario
        captured["run_dir"] = run_dir
        return RunSummary(
            run_id="x", ticks=scenario.ticks, usage=Usage(), total_moves=0,
            final_districts=[], wall_time_s=0.0,
        )

    monkeypatch.setattr(cli.engine_loop, "run_simulation", fake_run_simulation)
    monkeypatch.setattr(cli.jev, "resolve_provider", lambda cfg: ("mock", _settings()))

    scenario_path = "scenarios/base.yaml"
    code = cli.main([
        "run", "--scenario", scenario_path, "--ticks", "9", "--agents", "42",
        "--seed", "7", "--batching", "throughput", "--out", str(tmp_path),
    ])

    assert code == 0
    scenario = captured["scenario"]
    assert scenario.ticks == 9
    assert scenario.n_agents == 42
    assert scenario.seed == 7
    assert scenario.jev.batching == "throughput"
    assert captured["run_dir"].parent == tmp_path
    assert captured["run_dir"].name.startswith("base-mock-")


def test_run_honors_explicit_run_id(tmp_path, monkeypatch):
    captured = {}

    async def fake_run_simulation(scenario, run_dir, backend=None):
        captured["run_dir"] = run_dir
        return RunSummary(run_id="x", ticks=1, usage=Usage(), total_moves=0, final_districts=[], wall_time_s=0.0)

    monkeypatch.setattr(cli.engine_loop, "run_simulation", fake_run_simulation)
    monkeypatch.setattr(cli.jev, "resolve_provider", lambda cfg: ("mock", _settings()))

    code = cli.main([
        "run", "--scenario", "scenarios/base.yaml", "--out", str(tmp_path), "--run-id", "my-run",
    ])
    assert code == 0
    assert captured["run_dir"] == tmp_path / "my-run"


# --- run: --yes guard for non-mock providers --------------------------------------------------


def test_run_non_mock_without_yes_is_refused(tmp_path, monkeypatch, capsys):
    called = {"run": False}

    async def fake_run_simulation(scenario, run_dir, backend=None):
        called["run"] = True
        return RunSummary(run_id="x", ticks=1, usage=Usage(), total_moves=0, final_districts=[], wall_time_s=0.0)

    async def fake_probe(scenario, ticks):
        return RunSummary(
            run_id="probe", ticks=ticks, usage=Usage(requests=ticks, input_tokens=ticks * 100),
            total_moves=0, final_districts=[], wall_time_s=0.0,
        )

    monkeypatch.setattr(cli.engine_loop, "run_simulation", fake_run_simulation)
    monkeypatch.setattr(cli, "_run_mock_probe", fake_probe)
    monkeypatch.setattr(cli.jev, "resolve_provider", lambda cfg: ("typesafe", _settings()))
    monkeypatch.setattr(
        cli.jev, "choose_agents_per_request", lambda cfg, settings, **kw: 1
    )

    code = cli.main(["run", "--scenario", "scenarios/base.yaml", "--out", str(tmp_path)])

    assert code == 2
    assert called["run"] is False
    err = capsys.readouterr().err
    assert "--yes" in err


def test_run_non_mock_with_yes_proceeds(tmp_path, monkeypatch):
    called = {"run": False}

    async def fake_run_simulation(scenario, run_dir, backend=None):
        called["run"] = True
        return RunSummary(run_id="x", ticks=1, usage=Usage(), total_moves=0, final_districts=[], wall_time_s=0.0)

    async def fake_probe(scenario, ticks):
        return RunSummary(
            run_id="probe", ticks=ticks, usage=Usage(requests=ticks, input_tokens=ticks * 100),
            total_moves=0, final_districts=[], wall_time_s=0.0,
        )

    monkeypatch.setattr(cli.engine_loop, "run_simulation", fake_run_simulation)
    monkeypatch.setattr(cli, "_run_mock_probe", fake_probe)
    monkeypatch.setattr(cli.jev, "resolve_provider", lambda cfg: ("typesafe", _settings()))
    monkeypatch.setattr(cli.jev, "choose_agents_per_request", lambda cfg, settings, **kw: 1)

    code = cli.main(["run", "--scenario", "scenarios/base.yaml", "--out", str(tmp_path), "--yes"])

    assert code == 0
    assert called["run"] is True


def test_run_non_mock_with_replay_from_skips_yes_guard(tmp_path, monkeypatch):
    called = {"run": False}

    async def fake_run_simulation(scenario, run_dir, backend=None):
        called["run"] = True
        return RunSummary(run_id="x", ticks=1, usage=Usage(), total_moves=0, final_districts=[], wall_time_s=0.0)

    monkeypatch.setattr(cli.engine_loop, "run_simulation", fake_run_simulation)
    monkeypatch.setattr(cli.jev, "resolve_provider", lambda cfg: ("typesafe", _settings()))

    calls_path = tmp_path / "jev_calls.ndjson"
    calls_path.write_text("", encoding="utf-8")

    code = cli.main([
        "run", "--scenario", "scenarios/base.yaml", "--out", str(tmp_path),
        "--replay-from", str(calls_path),
    ])
    assert code == 0
    assert called["run"] is True


# --- estimate math ------------------------------------------------------------------------------


def test_estimate_scenario_picks_rpm_as_binding_limit(monkeypatch):
    async def fake_probe(scenario, ticks):
        return RunSummary(
            run_id="probe", ticks=ticks, usage=Usage(requests=ticks * 2, input_tokens=ticks * 100),
            total_moves=0, final_districts=[], wall_time_s=0.0,
        )

    monkeypatch.setattr(cli, "_run_mock_probe", fake_probe)
    monkeypatch.setattr(cli.jev, "choose_agents_per_request", lambda cfg, settings, **kw: 3)

    scenario = Scenario(name="s", ticks=100)
    settings = _settings(rpm_limit=60, tps_limit=1_000_000, price_per_mtok_usd=1.0)

    result = cli._estimate_scenario(scenario, "typesafe", settings)

    assert result["k"] == 3
    assert result["requests_per_tick"] == pytest.approx(2.0)
    assert result["tokens_per_tick"] == pytest.approx(100.0)
    assert result["binding"] == "rpm"
    # rpm_bound = 2 / (60*0.9/60) = 2 / 0.9
    assert result["time_per_tick"] == pytest.approx(2 / 0.9)
    assert result["cost_per_tick"] == pytest.approx(100.0 * 1.0 / 1e6)
    assert result["total_cost"] == pytest.approx(result["cost_per_tick"] * 100)


def test_estimate_scenario_picks_tps_as_binding_limit(monkeypatch):
    async def fake_probe(scenario, ticks):
        return RunSummary(
            run_id="probe", ticks=ticks, usage=Usage(requests=ticks, input_tokens=ticks * 10_000),
            total_moves=0, final_districts=[], wall_time_s=0.0,
        )

    monkeypatch.setattr(cli, "_run_mock_probe", fake_probe)
    monkeypatch.setattr(cli.jev, "choose_agents_per_request", lambda cfg, settings, **kw: 1)

    scenario = Scenario(name="s", ticks=50)
    settings = _settings(rpm_limit=1_000_000, tps_limit=100, price_per_mtok_usd=1.0)

    result = cli._estimate_scenario(scenario, "typesafe", settings)

    assert result["binding"] == "tps"
    # tps_bound = 10000 / (100*0.9) = 111.11
    assert result["time_per_tick"] == pytest.approx(10_000 / (100 * 0.9))


# --- compare -------------------------------------------------------------------------------------


def test_compare_prints_side_by_side_metrics(tmp_path, capsys):
    run_a = _make_run_dir(tmp_path, "run-a")
    run_b = _make_run_dir(tmp_path, "run-b")

    code = cli.main(["compare", str(run_a), str(run_b)])
    assert code == 0
    out = capsys.readouterr().out
    assert "[d1]" in out
    assert "avg_rent" in out
    assert "total_moves" in out
    assert "cost_usd" in out


def test_compare_shows_new_world_expansion_metrics(tmp_path, capsys):
    run_a = _make_run_dir(
        tmp_path, "run-a", ticks=3, tourist_units=5, shops_open=10, online_share=0.3,
        mode_share={"metro": 0.7, "car": 0.3}, tick_arrivals=1, tick_departures=0,
    )
    run_b = _make_run_dir(
        tmp_path, "run-b", ticks=3, tourist_units=0, shops_open=8, online_share=0.1,
        mode_share={"car": 1.0}, tick_arrivals=0, tick_departures=1,
    )

    code = cli.main(["compare", str(run_a), str(run_b)])
    assert code == 0
    out = capsys.readouterr().out

    assert "tourist_units" in out
    assert "shops_open" in out
    assert "online_share" in out
    assert "mode_share" in out
    assert "metro:70%" in out  # run A's mode_share, largest share first
    assert "car:100%" in out  # run B's mode_share
    assert "total_arrivals" in out
    assert "total_departures" in out

    lines = out.splitlines()
    arrivals_line = next(line for line in lines if line.strip().startswith("total_arrivals"))
    departures_line = next(line for line in lines if line.strip().startswith("total_departures"))
    arrivals_vals = arrivals_line.split()[1:]
    departures_vals = departures_line.split()[1:]
    # run A: 1 arrival/tick x 3 ticks = 3, 0 departures. run B: 0 arrivals, 1/tick x 3 = 3.
    assert arrivals_vals == ["3", "0"]
    assert departures_vals == ["0", "3"]


def test_compare_requires_finished_runs(tmp_path, capsys):
    run_a = _make_run_dir(tmp_path, "run-a")
    unfinished = tmp_path / "run-unfinished"
    RunWriter(unfinished).close()

    code = cli.main(["compare", str(run_a), str(unfinished)])
    assert code == 2
    assert "summary.json" in capsys.readouterr().err


# --- export-web ------------------------------------------------------------------------------------


def test_export_web_copies_files_and_merges_index(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_a = _make_run_dir(tmp_path, "run-a")
    run_b = _make_run_dir(tmp_path, "run-b", provider="typesafe")
    dest = tmp_path / "web" / "public" / "runs"

    code = cli.main(["export-web", str(run_a), "--dest", str(dest)])
    assert code == 0
    assert (dest / "run-a" / "meta.json").exists()
    assert (dest / "run-a" / "summary.json").exists()
    assert not (dest / "run-a" / "jev_calls.ndjson").exists()

    index = json.loads((dest / "index.json").read_text(encoding="utf-8"))
    assert len(index) == 1
    assert index[0]["run_id"] == "run-a"
    assert index[0]["provider"] == "mock"

    # exporting a second run merges into the index rather than overwriting it
    code = cli.main(["export-web", str(run_b), "--dest", str(dest)])
    assert code == 0
    index = json.loads((dest / "index.json").read_text(encoding="utf-8"))
    assert {e["run_id"] for e in index} == {"run-a", "run-b"}

    # re-exporting run-a again keeps a single entry (update, not duplicate)
    code = cli.main(["export-web", str(run_a), "--dest", str(dest)])
    index = json.loads((dest / "index.json").read_text(encoding="utf-8"))
    assert len([e for e in index if e["run_id"] == "run-a"]) == 1


# --- providers -------------------------------------------------------------------------------------


def test_providers_command_lists_all_providers(capsys):
    code = cli.main(["providers"])
    assert code == 0
    out = capsys.readouterr().out
    for name in ("mock", "typesafe", "openrouter", "vercel"):
        assert f"== {name} ==" in out


# --- module entry point ----------------------------------------------------------------------------


def test_python_dash_m_jevcity_works():
    result = subprocess.run(
        [sys.executable, "-m", "jevcity", "providers"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0
    assert "== mock ==" in result.stdout


def test_estimate_probe_ignores_env_provider(monkeypatch):
    """Regression: env JEV_PROVIDER must not turn the mock estimate probe into real calls."""
    import asyncio

    from jevcity import cli
    from jevcity.scenarios.loader import load_scenario

    seen = []

    async def fake_run_simulation(scenario, run_dir, backend=None):
        import os

        seen.append(os.environ.get("JEV_PROVIDER"))
        return "summary"

    monkeypatch.setenv("JEV_PROVIDER", "openrouter")
    monkeypatch.setattr(cli.engine_loop, "run_simulation", fake_run_simulation)
    scenario = load_scenario("scenarios/base.yaml")
    assert asyncio.run(cli._run_mock_probe(scenario, 2)) == "summary"
    assert seen == [None]
    import os

    assert os.environ["JEV_PROVIDER"] == "openrouter"  # restored afterwards
