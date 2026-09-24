"""Command line entry point. Owner: T6.

jevcity run --scenario PATH [--provider mock|typesafe|openrouter|vercel]
            [--replay-from CALLS.ndjson] [--batching quality|throughput]
            [--ticks N] [--agents N] [--seed N] [--out runs] [--run-id ID] [--yes]
jevcity batch --scenario PATH --seeds N [--seed-start 1] [--parallel P]
              [--provider P] [--agents N] [--ticks N] [--batch-id ID] [--out runs] [--yes]
jevcity compare RUN_A RUN_B
jevcity compare-batch BATCH_A BATCH_B [--metric NAME]
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
from jevcity.engine import batch as engine_batch
from jevcity.engine import loop as engine_loop
from jevcity.runlog import reader as runlog_reader
from jevcity.scenarios import loader as scenario_loader
from jevcity.types import Scenario

_PROVIDER_CHOICES = ("mock", "typesafe", "openrouter", "vercel")
_BATCHING_CHOICES = ("quality", "throughput")
_ESTIMATE_PROBE_TICKS = 30
_BATCH_MAX_PARALLEL = 4


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

    batch_p = sub.add_parser("batch", help="Run the same scenario at several seeds")
    batch_p.add_argument("--scenario", required=True)
    batch_p.add_argument("--seeds", type=int, required=True, help="number of seeds to run")
    batch_p.add_argument("--seed-start", type=int, default=1)
    batch_p.add_argument("--parallel", type=int, default=1, help=f"max {_BATCH_MAX_PARALLEL}")
    batch_p.add_argument("--provider", choices=_PROVIDER_CHOICES)
    batch_p.add_argument("--agents", type=int)
    batch_p.add_argument("--ticks", type=int)
    batch_p.add_argument("--batch-id")
    batch_p.add_argument("--out", default="runs")
    batch_p.add_argument("--yes", action="store_true")

    cmp_p = sub.add_parser("compare", help="Compare two finished runs")
    cmp_p.add_argument("run_a")
    cmp_p.add_argument("run_b")

    cmpb_p = sub.add_parser("compare-batch", help="Compare two finished batches")
    cmpb_p.add_argument("batch_a")
    cmpb_p.add_argument("batch_b")
    cmpb_p.add_argument("--metric", help="only show rows for this metric (e.g. avg_rent)")

    exp_p = sub.add_parser(
        "export-web", help="Copy run(s) or batch dir(s) into the web app's public/runs"
    )
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


def _cmd_batch(args: argparse.Namespace) -> int:
    if args.seeds < 1:
        print("error: --seeds must be >= 1", file=sys.stderr)
        return 2
    if not (1 <= args.parallel <= _BATCH_MAX_PARALLEL):
        print(f"error: --parallel must be between 1 and {_BATCH_MAX_PARALLEL}", file=sys.stderr)
        return 2

    scenario = scenario_loader.load_scenario(args.scenario)
    if args.provider:
        scenario.jev.provider = args.provider
    if args.ticks is not None:
        scenario.ticks = args.ticks
    if args.agents is not None:
        scenario.n_agents = args.agents

    provider, settings = jev.resolve_provider(scenario.jev)
    print(f"effective provider: {provider}")

    if provider != "mock" and not scenario.jev.replay_from:
        result = _estimate_scenario(scenario, provider, settings)
        total_cost = result["total_cost"] * args.seeds
        print(
            f"estimated cost for '{scenario.name}' x {args.seeds} seeds over {scenario.ticks} "
            f"ticks with provider={provider}: ${total_cost:.2f} "
            f"(time/tick >= {result['time_per_tick']:.3f}s per run, binding={result['binding']}; "
            f"--parallel {args.parallel} multiplies the effective request rate against this "
            f"provider, since each concurrent run has its own rate limiter)"
        )
        if not args.yes:
            print(
                "Refusing to run a batch against a non-mock provider without --yes "
                "(and no --replay-from was given).",
                file=sys.stderr,
            )
            return 2

    batch_id = args.batch_id or (
        f"{scenario.name}-{provider}-batch-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    )
    batch_dir = Path(args.out) / batch_id
    seeds = list(range(args.seed_start, args.seed_start + args.seeds))

    results = asyncio.run(engine_batch.run_batch(scenario, batch_dir, seeds, parallel=args.parallel))

    for r in results:
        if r.error is not None:
            print(f"  seed {r.seed}: FAILED - {r.error}", file=sys.stderr)
        elif r.skipped:
            print(f"  seed {r.seed}: resumed (summary.json already present)")
        else:
            print(f"  seed {r.seed}: ok -> {r.run_dir}")

    runs_ticks = engine_batch.load_run_ticks(batch_dir, [r.seed for r in results if r.summary])
    payload = engine_batch.aggregate_batch(scenario.name, provider, results, runs_ticks)
    summary_path = engine_batch.write_batch_summary(batch_dir, payload)

    n_ok = len(payload["succeeded_seeds"])
    n_failed = len(payload["failed"])
    print(
        f"batch complete: {batch_dir}  {n_ok}/{len(seeds)} seeds ok, {n_failed} failed  "
        f"summary={summary_path}"
    )
    return 1 if n_failed and not n_ok else 0


def _cmd_compare_batch(args: argparse.Namespace) -> int:
    summary_a = engine_batch.read_batch_summary(args.batch_a)
    summary_b = engine_batch.read_batch_summary(args.batch_b)
    if summary_a is None or summary_b is None:
        print("both batches must have a batch_summary.json (i.e. be finished batches)", file=sys.stderr)
        return 2

    rows = engine_batch.compare_batches(summary_a, summary_b)
    if args.metric:
        rows = [r for r in rows if r["metric"] == args.metric]

    print(
        f"A: {args.batch_a} (seeds={summary_a.get('seeds')})\n"
        f"B: {args.batch_b} (seeds={summary_b.get('seeds')})"
    )
    print(
        "NOTE: 'clear' is a rough heuristic (|effect| > 2x pooled std across seeds), not a "
        "formal significance test; with few seeds treat it as a signal, not proof."
    )
    label_w, val_w = 34, 12
    print(f"{'district.metric':<{label_w}}{'mean_A':>{val_w}}{'mean_B':>{val_w}}"
          f"{'effect':>{val_w}}{'noise':>{val_w}}  clear")
    print("-" * (label_w + 4 * val_w + 8))
    for row in rows:
        label = f"{row['district']}.{row['metric']}"
        print(
            f"{label:<{label_w}}{row['mean_a']:>{val_w}.3f}{row['mean_b']:>{val_w}.3f}"
            f"{row['effect']:>{val_w}.3f}{row['noise']:>{val_w}.3f}  "
            f"{'CLEAR' if row['clear'] else '-'}"
        )
    return 0


def _fmt(value, width, prec=None) -> str:
    if isinstance(value, float) and prec is not None:
        return f"{value:>{width}.{prec}f}"
    return f"{value!s:>{width}}"


def _fmt_mode_share(mode_share: dict[str, float]) -> str:
    """Compact 'mode:pct% ...' string, largest share first; '-' when nobody commutes."""
    if not mode_share:
        return "-"
    parts = sorted(mode_share.items(), key=lambda kv: -kv[1])
    return " ".join(f"{mode}:{pct * 100:.0f}%" for mode, pct in parts)


def _migration_totals(reader: runlog_reader.RunReader) -> tuple[int, int]:
    """Sum of TickRecord.arrivals/.departures over every tick of the run."""
    arrivals = 0
    departures = 0
    for t in reader.ticks():
        arrivals += len(t.arrivals)
        departures += len(t.departures)
    return arrivals, departures


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
        print(f"{'  tourist_units':<{label_w}}{_fmt(da.tourist_units if da else 0, val_w)}"
              f"{_fmt(db.tourist_units if db else 0, val_w)}")
        print(f"{'  shops_open':<{label_w}}{_fmt(da.shops_open if da else 0, val_w)}"
              f"{_fmt(db.shops_open if db else 0, val_w)}")
        print(f"{'  online_share':<{label_w}}{_fmt(da.online_share if da else 0.0, val_w, 3)}"
              f"{_fmt(db.online_share if db else 0.0, val_w, 3)}")
        mode_a = _fmt_mode_share(da.mode_share if da else {})
        mode_b = _fmt_mode_share(db.mode_share if db else {})
        mode_w = max(val_w, len(mode_a) + 2, len(mode_b) + 2)
        print(f"{'  mode_share':<{label_w}}{_fmt(mode_a, mode_w)}{_fmt(mode_b, mode_w)}")

    print()
    print(f"{'total_moves':<{label_w}}{_fmt(summary_a.total_moves, val_w)}{_fmt(summary_b.total_moves, val_w)}")
    arrivals_a, departures_a = _migration_totals(reader_a)
    arrivals_b, departures_b = _migration_totals(reader_b)
    print(f"{'total_arrivals':<{label_w}}{_fmt(arrivals_a, val_w)}{_fmt(arrivals_b, val_w)}")
    print(f"{'total_departures':<{label_w}}{_fmt(departures_a, val_w)}{_fmt(departures_b, val_w)}")
    print(f"{'cost_usd':<{label_w}}{_fmt(summary_a.usage.cost_usd, val_w, 4)}{_fmt(summary_b.usage.cost_usd, val_w, 4)}")
    print(f"{'cost_source':<{label_w}}{_fmt(summary_a.usage.cost_source, val_w)}{_fmt(summary_b.usage.cost_source, val_w)}")
    models_a = ",".join(sorted(summary_a.usage.models_seen)) or "-"
    models_b = ",".join(sorted(summary_b.usage.models_seen)) or "-"
    wide = max(val_w, len(models_a) + 2, len(models_b) + 2)
    print(f"{'models_seen':<{label_w}}{_fmt(models_a, wide)}{_fmt(models_b, wide)}")
    return 0


def _expand_run_dirs(run_dir_strs: list[str]) -> list[Path]:
    """Expand any batch dir (one with a batch_summary.json) into its per-seed run dirs, so
    `export-web` given a batch shows every seed's run individually (the web has no notion of a
    batch yet; each seed just becomes one more entry in index.json). Plain run dirs pass
    through unchanged."""
    expanded: list[Path] = []
    for run_dir_str in run_dir_strs:
        run_dir = Path(run_dir_str)
        batch_summary = engine_batch.read_batch_summary(run_dir)
        if batch_summary is not None:
            for seed in batch_summary.get("seeds", []):
                seed_dir = engine_batch.seed_run_dir(run_dir, seed)
                if (seed_dir / "meta.json").exists():
                    expanded.append(seed_dir)
        else:
            expanded.append(run_dir)
    return expanded


def _cmd_export_web(args: argparse.Namespace) -> int:
    dest_root = Path(args.dest)
    dest_root.mkdir(parents=True, exist_ok=True)

    index_path = dest_root / "index.json"
    index_by_id: dict[str, dict] = {}
    if index_path.exists():
        for entry in json.loads(index_path.read_text(encoding="utf-8")):
            index_by_id[entry["run_id"]] = entry

    batch_dirs = [
        Path(s) for s in args.run_dirs if engine_batch.read_batch_summary(Path(s)) is not None
    ]
    run_dirs = _expand_run_dirs(args.run_dirs)

    for run_dir in run_dirs:
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

    for batch_dir in batch_dirs:
        batch_out = dest_root / batch_dir.name
        batch_out.mkdir(parents=True, exist_ok=True)
        shutil.copy2(batch_dir / "batch_summary.json", batch_out / "batch_summary.json")

    geojson_src = Path(args.geojson)
    if geojson_src.exists():
        geo_dest = Path("web/public/data/districts.geojson")
        geo_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(geojson_src, geo_dest)
    else:
        print(f"note: geojson {geojson_src} not found, skipped", file=sys.stderr)

    print(
        f"exported {len(run_dirs)} run(s) and {len(batch_dirs)} batch summary(ies) to {dest_root}"
    )
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
    "batch": _cmd_batch,
    "compare": _cmd_compare,
    "compare-batch": _cmd_compare_batch,
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
