"""Command line entry point. Owner: T6.

jevcity run --scenario PATH [--provider mock|typesafe|openrouter|vercel]
            [--replay-from CALLS.ndjson] [--batching quality|throughput]
            [--ticks N] [--agents N] [--seed N] [--out runs] [--run-id ID] [--yes]
jevcity compare RUN_A RUN_B
jevcity export-web RUN_DIR... [--dest web/public/runs] [--geojson data/processed/districts.geojson]
jevcity estimate --scenario PATH [--provider P] [--batching B]
jevcity providers
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

from jevcity import jev
from jevcity.engine import loop as engine_loop
from jevcity.runlog import reader as runlog_reader
from jevcity.scenarios import loader as scenario_loader
from jevcity.types import Scenario

_PROVIDER_CHOICES = ("mock", "typesafe", "openrouter", "vercel")
_BATCHING_CHOICES = ("quality", "throughput")
_ESTIMATE_PROBE_TICKS = 30


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


# --- argument parsing ------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jevcity")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run a scenario")
    run_p.add_argument("--scenario", required=True)
    run_p.add_argument("--provider", choices=_PROVIDER_CHOICES)
    run_p.add_argument("--replay-from")
    run_p.add_argument("--batching", choices=_BATCHING_CHOICES)
    run_p.add_argument("--ticks", type=int)
    run_p.add_argument("--agents", type=int)
    run_p.add_argument("--seed", type=int)
    run_p.add_argument("--out", default="runs")
    run_p.add_argument("--run-id")
    run_p.add_argument("--yes", action="store_true")

    cmp_p = sub.add_parser("compare", help="Compare two finished runs")
    cmp_p.add_argument("run_a")
    cmp_p.add_argument("run_b")

    exp_p = sub.add_parser("export-web", help="Copy run(s) into the web app's public/runs")
    exp_p.add_argument("run_dirs", nargs="+")
    exp_p.add_argument("--dest", default="web/public/runs")
    exp_p.add_argument("--geojson", default="data/processed/districts.geojson")

    est_p = sub.add_parser("estimate", help="Estimate cost/time for a scenario without paying")
    est_p.add_argument("--scenario", required=True)
    est_p.add_argument("--provider", choices=_PROVIDER_CHOICES)
    est_p.add_argument("--batching", choices=_BATCHING_CHOICES)

    sub.add_parser("providers", help="Show provider settings from config/providers.yaml")

    return parser


def _apply_scenario_overrides(scenario: Scenario, args: argparse.Namespace) -> None:
    if getattr(args, "provider", None):
        scenario.jev.provider = args.provider
    if getattr(args, "batching", None):
        scenario.jev.batching = args.batching
    if getattr(args, "replay_from", None):
        scenario.jev.replay_from = args.replay_from
    if getattr(args, "ticks", None) is not None:
        scenario.ticks = args.ticks
    if getattr(args, "agents", None) is not None:
        scenario.n_agents = args.agents
    if getattr(args, "seed", None) is not None:
        scenario.seed = args.seed


# --- estimate (shared by `run`'s pre-flight check and `estimate`) ----------------------------


async def _run_mock_probe(scenario: Scenario, ticks: int):
    """Run `ticks` ticks of `scenario` against the MOCK backend in a throwaway temp dir.
    Never touches the network, regardless of what provider the scenario itself requests."""
    import tempfile

    probe_scenario = scenario.model_copy(deep=True)
    probe_scenario.ticks = max(ticks, 1)
    probe_scenario.jev = probe_scenario.jev.model_copy(
        update={"provider": "mock", "replay_from": None}
    )
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / "estimate-probe"
        return await engine_loop.run_simulation(probe_scenario, run_dir)


def _estimate_scenario(scenario: Scenario, provider: str, settings) -> dict:
    """Probe the scenario with the mock backend, then extrapolate to `provider`'s settings."""
    probe_ticks = min(_ESTIMATE_PROBE_TICKS, max(scenario.ticks, 1))
    summary = asyncio.run(_run_mock_probe(scenario, probe_ticks))

    requests_per_tick = summary.usage.requests / probe_ticks
    tokens_per_tick = summary.usage.input_tokens / probe_ticks

    k = jev.choose_agents_per_request(
        scenario.jev,
        settings,
        est_tokens_per_agent=engine_loop.EST_TOKENS_PER_AGENT,
        est_shared_tokens=engine_loop.EST_SHARED_TOKENS,
    )

    price = settings.price_per_mtok_usd or 0.0
    cost_per_tick = tokens_per_tick * price / 1e6
    total_cost = cost_per_tick * scenario.ticks

    safety = scenario.jev.rate_safety
    rpm_bound = (
        requests_per_tick / (settings.rpm_limit * safety / 60.0) if settings.rpm_limit else 0.0
    )
    tps_bound = tokens_per_tick / (settings.tps_limit * safety) if settings.tps_limit else 0.0
    if rpm_bound >= tps_bound:
        time_per_tick, binding = rpm_bound, "rpm"
    else:
        time_per_tick, binding = tps_bound, "tps"

    return {
        "requests_per_tick": requests_per_tick,
        "tokens_per_tick": tokens_per_tick,
        "k": k,
        "cost_per_tick": cost_per_tick,
        "total_cost": total_cost,
        "time_per_tick": time_per_tick,
        "binding": binding,
    }


# --- commands ----------------------------------------------------------------------------------


def _cmd_run(args: argparse.Namespace) -> int:
    scenario = scenario_loader.load_scenario(args.scenario)
    _apply_scenario_overrides(scenario, args)

    provider, settings = jev.resolve_provider(scenario.jev)
    print(f"effective provider: {provider}")

    if provider != "mock" and not scenario.jev.replay_from:
        result = _estimate_scenario(scenario, provider, settings)
        print(
            f"estimated cost for '{scenario.name}' over {scenario.ticks} ticks with "
            f"provider={provider}: ${result['total_cost']:.2f} "
            f"(time/tick >= {result['time_per_tick']:.3f}s, binding={result['binding']})"
        )
        if not args.yes:
            print(
                "Refusing to run against a non-mock provider without --yes "
                "(and no --replay-from was given).",
                file=sys.stderr,
            )
            return 2

    run_id = args.run_id or (
        f"{scenario.name}-{provider}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    )
    run_dir = Path(args.out) / run_id

    summary = asyncio.run(engine_loop.run_simulation(scenario, run_dir))
    print(
        f"run complete: {run_dir} ticks={summary.ticks} moves={summary.total_moves} "
        f"cost=${summary.usage.cost_usd:.4f} ({summary.usage.cost_source})"
    )
    return 0


def _fmt(value, width, prec=None) -> str:
    if isinstance(value, float) and prec is not None:
        return f"{value:>{width}.{prec}f}"
    return f"{value!s:>{width}}"


def _cmd_compare(args: argparse.Namespace) -> int:
    reader_a = runlog_reader.RunReader(args.run_a)
    reader_b = runlog_reader.RunReader(args.run_b)
    summary_a = reader_a.summary()
    summary_b = reader_b.summary()
    if summary_a is None or summary_b is None:
        print("both runs must have a summary.json (i.e. be finished runs)", file=sys.stderr)
        return 2

    districts_a = {d.id: d for d in summary_a.final_districts}
    districts_b = {d.id: d for d in summary_b.final_districts}

    label_w, val_w = 24, 14
    print(f"{'':<{label_w}}{'A':>{val_w}}{'B':>{val_w}}")
    print("-" * (label_w + 2 * val_w))
    for did in sorted(set(districts_a) | set(districts_b)):
        da, db = districts_a.get(did), districts_b.get(did)
        print(f"[{did}]")
        print(f"{'  avg_rent':<{label_w}}{_fmt(da.avg_rent if da else 0.0, val_w, 1)}"
              f"{_fmt(db.avg_rent if db else 0.0, val_w, 1)}")
        print(f"{'  residents':<{label_w}}{_fmt(da.residents if da else 0, val_w)}"
              f"{_fmt(db.residents if db else 0, val_w)}")
        print(f"{'  unemployment_rate':<{label_w}}{_fmt(da.unemployment_rate if da else 0.0, val_w, 3)}"
              f"{_fmt(db.unemployment_rate if db else 0.0, val_w, 3)}")
        print(f"{'  avg_satisfaction':<{label_w}}{_fmt(da.avg_satisfaction if da else 0.0, val_w, 3)}"
              f"{_fmt(db.avg_satisfaction if db else 0.0, val_w, 3)}")

    print()
    print(f"{'total_moves':<{label_w}}{_fmt(summary_a.total_moves, val_w)}{_fmt(summary_b.total_moves, val_w)}")
    print(f"{'cost_usd':<{label_w}}{_fmt(summary_a.usage.cost_usd, val_w, 4)}{_fmt(summary_b.usage.cost_usd, val_w, 4)}")
    print(f"{'cost_source':<{label_w}}{_fmt(summary_a.usage.cost_source, val_w)}{_fmt(summary_b.usage.cost_source, val_w)}")
    models_a = ",".join(sorted(summary_a.usage.models_seen)) or "-"
    models_b = ",".join(sorted(summary_b.usage.models_seen)) or "-"
    wide = max(val_w, len(models_a) + 2, len(models_b) + 2)
    print(f"{'models_seen':<{label_w}}{_fmt(models_a, wide)}{_fmt(models_b, wide)}")
    return 0


def _cmd_export_web(args: argparse.Namespace) -> int:
    dest_root = Path(args.dest)
    dest_root.mkdir(parents=True, exist_ok=True)

    index_path = dest_root / "index.json"
    index_by_id: dict[str, dict] = {}
    if index_path.exists():
        for entry in json.loads(index_path.read_text(encoding="utf-8")):
            index_by_id[entry["run_id"]] = entry

    for run_dir_str in args.run_dirs:
        run_dir = Path(run_dir_str)
        reader = runlog_reader.RunReader(run_dir)
        meta = reader.meta()
        summary = reader.summary()

        out_dir = dest_root / meta.run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        for fname in ("meta.json", "agents.json", "ticks.ndjson", "summary.json"):
            src = run_dir / fname
            if src.exists():
                shutil.copy2(src, out_dir / fname)

        index_by_id[meta.run_id] = {
            "run_id": meta.run_id,
            "scenario": meta.scenario.name,
            "description": meta.scenario.description,
            "ticks": summary.ticks if summary is not None else meta.scenario.ticks,
            "n_agents": meta.scenario.n_agents,
            "provider": meta.jev_provider,
        }

    index_path.write_text(json.dumps(list(index_by_id.values()), indent=2), encoding="utf-8")

    geojson_src = Path(args.geojson)
    if geojson_src.exists():
        geo_dest = Path("web/public/data/districts.geojson")
        geo_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(geojson_src, geo_dest)
    else:
        print(f"note: geojson {geojson_src} not found, skipped", file=sys.stderr)

    print(f"exported {len(args.run_dirs)} run(s) to {dest_root}")
    return 0


def _cmd_estimate(args: argparse.Namespace) -> int:
    scenario = scenario_loader.load_scenario(args.scenario)
    if args.provider:
        scenario.jev.provider = args.provider
    if args.batching:
        scenario.jev.batching = args.batching

    provider, settings = jev.resolve_provider(scenario.jev)
    result = _estimate_scenario(scenario, provider, settings)

    print(f"scenario: {scenario.name}  provider: {provider}  batching: {scenario.jev.batching}")
    print(f"  K (agents/request):        {result['k']}")
    print(f"  requests/tick:              {result['requests_per_tick']:.2f}")
    print(f"  estimated input tokens/tick: {result['tokens_per_tick']:.0f}")
    print(f"  cost/tick:                  ${result['cost_per_tick']:.4f}")
    print(f"  estimated total cost ({scenario.ticks} ticks): ${result['total_cost']:.2f}")
    print(
        f"  time/tick lower bound:      {result['time_per_tick']:.3f}s "
        f"(binding limit: {result['binding']})"
    )
    return 0


def _cmd_providers(_args: argparse.Namespace) -> int:
    path = _repo_root() / "config" / "providers.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for name, cfg in data.items():
        print(f"== {name} ==")
        print(f"  url:            {cfg.get('base_url', '')}{cfg.get('path', '')}")
        print(f"  wire:           {cfg.get('wire')}")
        print(f"  default_model:  {cfg.get('default_model')}")
        print(f"  rpm_limit:      {cfg.get('rpm_limit')}")
        print(f"  tps_limit:      {cfg.get('tps_limit')}")
        print(f"  max_context:    {cfg.get('max_context_tokens')}")
        print(f"  price/Mtok USD: {cfg.get('price_per_mtok_usd')}")
        for todo in cfg.get("todo") or []:
            print(f"  TODO: {todo}")
        print()
    return 0


_COMMANDS = {
    "run": _cmd_run,
    "compare": _cmd_compare,
    "export-web": _cmd_export_web,
    "estimate": _cmd_estimate,
    "providers": _cmd_providers,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = _COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse already restricts `command`
        parser.error(f"unknown command {args.command!r}")
        return 2
    try:
        return handler(args)
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
