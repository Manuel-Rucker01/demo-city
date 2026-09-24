"""Simulation loop. Owner: T6.

Per tick: apply_policies -> detect_events -> build_requests -> backend.evaluate_many ->
parse_decisions -> apply_decisions -> daily_update -> write TickRecord.

Collaborators are imported as modules (not individual functions) so tests can monkeypatch
`market.apply_decisions`, `jev.resolve_provider`, etc. in place.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np

from jevcity import jev
from jevcity.engine import snapshot
from jevcity.events import triggers
from jevcity.population import generator
from jevcity.prompts import parse as decision_parse
from jevcity.prompts import state_builder
from jevcity.runlog import writer as runlog_writer
from jevcity.types import (
    SIM_START_DATE,
    AgentSnapshot,
    JevBackend,
    RunMeta,
    RunSummary,
    Scenario,
    TickRecord,
    Usage,
)
from jevcity.world import loader as world_loader
from jevcity.world import market

logger = logging.getLogger(__name__)

# K estimation constants (see docs/CONTRACTS.md "Batching"); refine once real state sizes
# are measured against these budgets.
EST_TOKENS_PER_AGENT = 300
EST_SHARED_TOKENS = 200

PROGRESS_EVERY_TICKS = 30


def _usage_diff(after: Usage, before: Usage) -> Usage:
    """Per-tick usage = cumulative `after` minus cumulative `before`, including models_seen."""
    models_seen: dict[str, int] = {}
    for model, count in after.models_seen.items():
        delta = count - before.models_seen.get(model, 0)
        if delta:
            models_seen[model] = delta
    return Usage(
        requests=after.requests - before.requests,
        input_tokens=after.input_tokens - before.input_tokens,
        output_tokens=after.output_tokens - before.output_tokens,
        cost_usd=after.cost_usd - before.cost_usd,
        cost_source=after.cost_source,
        estimated=after.estimated,
        retries=after.retries - before.retries,
        rate_limited=after.rate_limited - before.rate_limited,
        errors=after.errors - before.errors,
        cache_hits=after.cache_hits - before.cache_hits,
        models_seen=models_seen,
    )


def _is_budget_exceeded(exc: BaseException) -> bool:
    """Match jevcity.jev.errors.JevBudgetExceeded, imported lazily so a broken jev/ package
    (still under construction in parallel) doesn't break importing this module. Falls back to
    matching the class name if the import itself fails."""
    try:
        from jevcity.jev.errors import JevBudgetExceeded

        if isinstance(exc, JevBudgetExceeded):
            return True
    except ImportError as import_exc:  # pragma: no cover - defensive, jev/errors.py is stable
        logger.debug("could not import JevBudgetExceeded, matching by name instead: %s", import_exc)
    return type(exc).__name__ == "JevBudgetExceeded"


async def run_simulation(
    scenario: Scenario, run_dir: str | Path, backend: JevBackend | None = None
) -> RunSummary:
    """Run a whole scenario. If backend is None, build one with make_backend(scenario.jev,
    sink=writer). Stops early (and records it) if real-mode cost exceeds jev.max_cost_usd."""
    run_dir = Path(run_dir)
    start_wall = time.monotonic()

    rng = np.random.default_rng(scenario.seed)
    profiles = world_loader.load_profiles(scenario.data_path)
    agents_list = generator.generate_population(profiles, scenario.n_agents, rng)
    agents_by_id = {a.id: a for a in agents_list}
    world = market.init_world(profiles, agents_list)

    writer = runlog_writer.RunWriter(run_dir)

    if backend is not None:
        provider, settings = backend.provider, backend.settings
    else:
        provider, settings = jev.resolve_provider(scenario.jev)

    k = jev.choose_agents_per_request(
        scenario.jev,
        settings,
        est_tokens_per_agent=EST_TOKENS_PER_AGENT,
        est_shared_tokens=EST_SHARED_TOKENS,
    )

    run_id = run_dir.name
    model_requested = scenario.jev.model or settings.default_model
    wire = scenario.jev.wire or settings.wire

    meta = RunMeta(
        run_id=run_id,
        created_at=datetime.now(UTC).isoformat(),
        scenario=scenario,
        profiles=profiles,
        start_date=SIM_START_DATE,
        jev_provider=provider,
        jev_model_requested=model_requested,
        jev_wire=wire,
    )
    writer.write_meta(meta)

    agent_snapshots = [
        AgentSnapshot(
            id=a.id,
            age=a.age,
            occupation=a.occupation,
            home=a.home,
            employed=a.employed,
            job_district=a.job_district,
            wage_monthly=a.wage_monthly,
            rent_monthly=a.rent_monthly,
            satisfaction=a.satisfaction,
        )
        for a in agents_list
    ]
    writer.write_agents(agent_snapshots)

    if backend is None:
        backend = jev.make_backend(scenario.jev, sink=writer)

    sim_start = date.fromisoformat(SIM_START_DATE)
    total_moves = 0
    ticks_completed = 0
    warned_model_change = False
    warnings_list: list[str] = []

    try:
        for tick in range(1, scenario.ticks + 1):
            world.tick = tick
            market.apply_policies(world, scenario, tick)
            events = triggers.detect_events(world, agents_by_id, tick, scenario.events, rng)
            reqs = state_builder.build_requests(world, agents_by_id, events, tick, k)

            usage_before = backend.usage()

            if reqs:
                try:
                    responses = await backend.evaluate_many(reqs)
                except BaseException as exc:
                    if _is_budget_exceeded(exc):
                        msg = f"stopped early at tick {tick}: {exc}"
                        logger.warning(msg)
                        warnings_list.append(msg)
                        break
                    raise
                decisions = decision_parse.parse_decisions(
                    reqs,
                    responses,
                    scenario.jev.confidence_threshold,
                    policy=scenario.jev.decision_policy,
                    seed=scenario.seed,
                )
            else:
                decisions = []

            delta = market.apply_decisions(world, agents_by_id, decisions, scenario, rng, tick)
            daily_changes = market.daily_update(world, agents_by_id, scenario, tick, rng)

            usage_after = backend.usage()
            usage_tick = _usage_diff(usage_after, usage_before)

            if len(usage_after.models_seen) > 1 and not warned_model_change:
                warned_model_change = True
                msg = (
                    f"model version changed mid-run (first noticed at tick {tick}): "
                    f"models_seen={sorted(usage_after.models_seen)}"
                )
                logger.warning(msg)
                warnings_list.append(msg)

            districts = snapshot.build_district_snapshots(world, agents_by_id)

            events_by_kind: dict[str, int] = {}
            for ev in events:
                events_by_kind[ev.kind.value] = events_by_kind.get(ev.kind.value, 0) + 1

            actions_by_kind: dict[str, int] = {}
            gated_decisions = 0
            for d in decisions:
                actions_by_kind[d.action.value] = actions_by_kind.get(d.action.value, 0) + 1
                if d.gated:
                    gated_decisions += 1

            tick_record = TickRecord(
                tick=tick,
                date=(sim_start + timedelta(days=tick)).isoformat(),
                districts=districts,
                events_by_kind=events_by_kind,
                actions_by_kind=actions_by_kind,
                gated_decisions=gated_decisions,
                moves=delta.moves,
                changes=[*delta.changes, *daily_changes],
                usage_tick=usage_tick,
                usage_total=usage_after,
            )
            writer.write_tick(tick_record)

            total_moves += len(delta.moves)
            ticks_completed = tick

            if tick % PROGRESS_EVERY_TICKS == 0 or tick == scenario.ticks:
                logger.info(
                    "tick=%d events=%d requests=%d moves=%d cost=$%.4f provider=%s",
                    tick,
                    len(events),
                    len(reqs),
                    len(delta.moves),
                    usage_after.cost_usd,
                    provider,
                )
    finally:
        final_districts = snapshot.build_district_snapshots(world, agents_by_id)
        summary = RunSummary(
            run_id=run_id,
            ticks=ticks_completed,
            usage=backend.usage(),
            total_moves=total_moves,
            final_districts=final_districts,
            wall_time_s=time.monotonic() - start_wall,
        )
        writer.write_summary(summary)
        if warnings_list:
            (run_dir / "warnings.json").write_text(
                json.dumps(warnings_list, indent=2), encoding="utf-8"
            )
        writer.close()
        await backend.aclose()

    return summary
