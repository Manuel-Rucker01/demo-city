"""Multi-seed batch runs and aggregation. Owner: T6 (this task).

`run_batch` drives N runs of the same scenario at different seeds into
runs/<batch_id>/s<seed>/ (each one an ordinary run directory, readable by
runlog.reader.RunReader / `jevcity compare` / `jevcity export-web` on its own), then writes
runs/<batch_id>/batch_summary.json with cross-seed aggregates (see `aggregate_batch`).

Concurrency: runs are executed as asyncio tasks *in the same process*, gated by an
`asyncio.Semaphore(parallel)` (not subprocesses - simpler to collect results/exceptions from,
and run_simulation is already async). Each run builds its own backend via
`jev.make_backend`/`engine.loop.run_simulation(backend=None)`, so each concurrent run gets its
own rate limiter instance: the adapter throttles per-backend, not globally, so running with
--parallel P against a real provider multiplies the effective request rate by up to P. Mock and
replay runs are unaffected (no network). Document this to the user before using --parallel > 1
with a paid provider.

Resume: a seed's run directory is skipped (not re-run) if its summary.json already exists, so a
batch that crashed partway through can be re-launched without paying for already-finished runs.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from pathlib import Path

from jevcity.engine import loop as engine_loop
from jevcity.runlog import reader as runlog_reader
from jevcity.types import RunSummary, Scenario, TickRecord, Usage

_BATCH_SUMMARY_FILE = "batch_summary.json"

# Per-district FINAL metrics aggregated (mean/std/min/max) across seeds.
_FINAL_SCALAR_METRICS = (
    "avg_rent", "vacancy_rate", "unemployment_rate", "avg_satisfaction",
    "residents", "tourist_units", "shops_open", "online_share",
)

# Per-tick time series aggregated (mean/min/max at each tick) across seeds.
_SERIES_SCALAR_METRICS = ("avg_rent", "avg_satisfaction")
_SERIES_MODE_METRICS = ("metro", "car")  # mode_share[mode] per district, at least these two


@dataclass
class SeedResult:
    seed: int
    run_dir: Path
    summary: RunSummary | None = None
    error: str | None = None
    skipped: bool = False  # resumed from an already-finished run


# --- stats helpers (pure) -----------------------------------------------------------------


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    """Population standard deviation (ddof=0): with a handful of seeds, sample vs. population
    std barely matters and ddof=0 keeps a single-seed batch well-defined (std=0, not NaN)."""
    if not xs:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _stats(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0}
    return {"mean": _mean(xs), "std": _std(xs), "min": min(xs), "max": max(xs)}


# --- running ---------------------------------------------------------------------------------


def seed_run_dir(batch_dir: Path, seed: int) -> Path:
    return batch_dir / f"s{seed}"


async def _run_one_seed(
    scenario: Scenario, seed: int, run_dir: Path, semaphore: asyncio.Semaphore
) -> SeedResult:
    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        try:
            summary = RunSummary.model_validate_json(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return SeedResult(seed=seed, run_dir=run_dir, error=f"resume failed: {exc}")
        return SeedResult(seed=seed, run_dir=run_dir, summary=summary, skipped=True)

    seed_scenario = scenario.model_copy(deep=True)
    seed_scenario.seed = seed
    async with semaphore:
        try:
            summary = await engine_loop.run_simulation(seed_scenario, run_dir)
        except Exception as exc:  # noqa: BLE001 - isolate one seed's failure from the rest
            return SeedResult(seed=seed, run_dir=run_dir, error=f"{type(exc).__name__}: {exc}")
    return SeedResult(seed=seed, run_dir=run_dir, summary=summary)


async def run_batch(
    scenario: Scenario,
    batch_dir: str | Path,
    seeds: list[int],
    parallel: int = 1,
) -> list[SeedResult]:
    """Run `scenario` once per seed in `seeds`, at most `parallel` concurrently, into
    batch_dir/s<seed>/. Returns one SeedResult per seed (order == `seeds`); failures do not
    stop the other runs. Does not write batch_summary.json (caller aggregates and writes it,
    since it may want to report failures first)."""
    batch_dir = Path(batch_dir)
    batch_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(max(1, parallel))
    tasks = [
        _run_one_seed(scenario, seed, seed_run_dir(batch_dir, seed), semaphore) for seed in seeds
    ]
    return await asyncio.gather(*tasks)


# --- aggregation (pure) ------------------------------------------------------------------------


def _final_district_rows(summary: RunSummary) -> dict[str, dict]:
    return {d.id: d for d in summary.final_districts}


def aggregate_final_metrics(summaries: list[RunSummary]) -> dict[str, dict[str, dict[str, float]]]:
    """{district_id: {metric: {mean, std, min, max}}} over each summary's final_districts, plus
    a `mode_share` entry per district: {mode: {mean, std, min, max}} (modes missing in a run
    count as 0.0 share that seed, not skipped, so the stats reflect all seeds)."""
    district_ids: set[str] = set()
    per_run_districts = [_final_district_rows(s) for s in summaries]
    for rows in per_run_districts:
        district_ids.update(rows)

    result: dict[str, dict[str, dict[str, float]]] = {}
    for did in sorted(district_ids):
        metrics: dict[str, dict[str, float]] = {}
        for metric in _FINAL_SCALAR_METRICS:
            values = [
                float(getattr(rows[did], metric)) for rows in per_run_districts if did in rows
            ]
            metrics[metric] = _stats(values)

        modes: set[str] = set()
        for rows in per_run_districts:
            d = rows.get(did)
            if d is not None:
                modes.update(d.mode_share)
        mode_stats: dict[str, dict[str, float]] = {}
        for mode in sorted(modes):
            values = [
                rows[did].mode_share.get(mode, 0.0) for rows in per_run_districts if did in rows
            ]
            mode_stats[mode] = _stats(values)
        metrics["mode_share"] = mode_stats

        result[did] = metrics
    return result


def _ticks_by_district(ticks: list[TickRecord]) -> dict[str, dict[int, dict]]:
    """{district_id: {tick_number: DistrictSnapshot}} for one run's ticks."""
    out: dict[str, dict[int, dict]] = {}
    for rec in ticks:
        for d in rec.districts:
            out.setdefault(d.id, {})[rec.tick] = d
    return out


def aggregate_series(
    runs_ticks: list[list[TickRecord]],
) -> dict[str, dict[str, list[dict[str, float]]]]:
    """Per-tick time series aggregated across runs.

    Result: {district_id: {series_name: [{"tick": t, "mean": .., "min": .., "max": ..}, ...]}}
    series_name is one of `_SERIES_SCALAR_METRICS` or `f"mode_share.{mode}"` for each mode seen
    (at least metro/car - see `_SERIES_MODE_METRICS` - plus any other mode observed).

    Runs are aligned by tick number, not by list position: a tick present in only some runs
    (e.g. a run stopped early) is aggregated over just the runs that have it. A tick present in
    no run is simply absent from the output (not synthesized as zeros)."""
    per_run_by_district = [_ticks_by_district(ticks) for ticks in runs_ticks]

    district_ids: set[str] = set()
    all_ticks: set[int] = set()
    modes: set[str] = set(_SERIES_MODE_METRICS)
    for by_district in per_run_by_district:
        district_ids.update(by_district)
        for tick_map in by_district.values():
            all_ticks.update(tick_map)
            for d in tick_map.values():
                modes.update(d.mode_share)

    sorted_ticks = sorted(all_ticks)

    result: dict[str, dict[str, list[dict[str, float]]]] = {}
    for did in sorted(district_ids):
        series: dict[str, list[dict[str, float]]] = {}

        def _values_at(tick: int, extract, _did: str = did) -> list[float]:
            values = []
            for by_district in per_run_by_district:
                d = by_district.get(_did, {}).get(tick)
                if d is not None:
                    values.append(extract(d))
            return values

        for metric in _SERIES_SCALAR_METRICS:
            points = []
            for tick in sorted_ticks:
                values = _values_at(tick, lambda d, m=metric: float(getattr(d, m)))
                if values:
                    s = _stats(values)
                    points.append({"tick": tick, "mean": s["mean"], "min": s["min"], "max": s["max"]})
            series[metric] = points

        for mode in sorted(modes):
            points = []
            for tick in sorted_ticks:
                values = _values_at(tick, lambda d, mo=mode: d.mode_share.get(mo, 0.0))
                if values:
                    s = _stats(values)
                    points.append({"tick": tick, "mean": s["mean"], "min": s["min"], "max": s["max"]})
            series[f"mode_share.{mode}"] = points

        result[did] = series
    return result


def aggregate_usage(summaries: list[RunSummary]) -> dict:
    total = Usage()
    for s in summaries:
        total = total.add(s.usage)
    return json.loads(total.model_dump_json())


def aggregate_batch(
    scenario_name: str,
    provider: str,
    results: list[SeedResult],
    runs_ticks_by_seed: dict[int, list[TickRecord]] | None = None,
) -> dict:
    """Build the batch_summary.json payload from SeedResults (+ optionally their tick series;
    when omitted, only final-metric aggregation and usage totals are computed - the caller may
    skip reading ticks.ndjson back for very large batches)."""
    ok = [r for r in results if r.summary is not None]
    failed = [r for r in results if r.error is not None]
    summaries = [r.summary for r in ok if r.summary is not None]

    payload: dict = {
        "scenario": scenario_name,
        "provider": provider,
        "seeds": [r.seed for r in results],
        "succeeded_seeds": [r.seed for r in ok],
        "failed": [{"seed": r.seed, "error": r.error} for r in failed],
        "resumed_seeds": [r.seed for r in results if r.skipped],
        "final_metrics": aggregate_final_metrics(summaries) if summaries else {},
        "usage_total": aggregate_usage(summaries) if summaries else json.loads(Usage().model_dump_json()),
    }
    if runs_ticks_by_seed is not None:
        ordered = [runs_ticks_by_seed[r.seed] for r in ok if r.seed in runs_ticks_by_seed]
        payload["series"] = aggregate_series(ordered) if ordered else {}
    return payload


def write_batch_summary(batch_dir: str | Path, payload: dict) -> Path:
    batch_dir = Path(batch_dir)
    path = batch_dir / _BATCH_SUMMARY_FILE
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def read_batch_summary(batch_dir: str | Path) -> dict | None:
    path = Path(batch_dir) / _BATCH_SUMMARY_FILE
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_run_ticks(batch_dir: str | Path, seeds: list[int]) -> dict[int, list[TickRecord]]:
    """Read back ticks.ndjson for each seed's run dir (used to build the time-series part of
    batch_summary.json after run_batch, and by tests). Skips seeds with no run dir."""
    batch_dir = Path(batch_dir)
    out: dict[int, list[TickRecord]] = {}
    for seed in seeds:
        run_dir = seed_run_dir(batch_dir, seed)
        if not (run_dir / "meta.json").exists():
            continue
        reader = runlog_reader.RunReader(run_dir)
        out[seed] = list(reader.ticks())
    return out


# --- compare-batch -----------------------------------------------------------------------------


def compare_batches(summary_a: dict, summary_b: dict) -> list[dict]:
    """Per-district, per-metric effect of B relative to A: effect = mean_B - mean_A, noise =
    pooled std = sqrt((std_A^2 + std_B^2) / 2). Flags "clear" when |effect| > 2*noise (and noise
    > 0; an exactly-zero-noise batch, e.g. a single seed with no natural variance, is "clear"
    whenever effect != 0 and "n/a" - noise not estimable - when effect == 0 too... in practice
    treated as clear-if-nonzero since there is nothing to compare against).

    This is a rough heuristic, NOT a formal statistical test (no distributional assumptions, no
    seed-count correction): with 3 seeds "2x pooled std" is noisy and should be read as a rough
    signal, not a p-value.
    """
    metrics_a = summary_a.get("final_metrics", {})
    metrics_b = summary_b.get("final_metrics", {})
    district_ids = sorted(set(metrics_a) | set(metrics_b))

    rows: list[dict] = []
    for did in district_ids:
        da = metrics_a.get(did, {})
        db = metrics_b.get(did, {})
        all_metric_names = sorted(set(da) | set(db))
        for metric in all_metric_names:
            if metric == "mode_share":
                for mode in sorted(set(da.get("mode_share", {})) | set(db.get("mode_share", {}))):
                    rows.append(
                        _compare_row(did, f"mode_share.{mode}", da.get("mode_share", {}).get(mode), db.get("mode_share", {}).get(mode))
                    )
            else:
                rows.append(_compare_row(did, metric, da.get(metric), db.get(metric)))
    return rows


def _compare_row(district: str, metric: str, stat_a: dict | None, stat_b: dict | None) -> dict:
    mean_a = (stat_a or {}).get("mean", 0.0)
    mean_b = (stat_b or {}).get("mean", 0.0)
    std_a = (stat_a or {}).get("std", 0.0)
    std_b = (stat_b or {}).get("std", 0.0)
    effect = mean_b - mean_a
    noise = math.sqrt((std_a**2 + std_b**2) / 2)
    if noise > 0:
        clear = abs(effect) > 2 * noise
    else:
        clear = effect != 0
    return {
        "district": district,
        "metric": metric,
        "mean_a": mean_a,
        "mean_b": mean_b,
        "effect": effect,
        "noise": noise,
        "clear": clear,
    }
