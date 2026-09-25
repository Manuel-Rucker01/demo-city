"""Simulation loop. Owner: T6.

Per tick: apply_policies -> detect_events -> build_requests -> backend.evaluate_many ->
parse_decisions -> apply_decisions -> daily_update -> write TickRecord.

Collaborators are imported as modules (not individual functions) so tests can monkeypatch
`market.apply_decisions`, `jev.resolve_provider`, etc. in place.
"""

from __future__ import annotations

import inspect
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
    Agent,
    AgentSnapshot,
    Event,
    EventKind,
    JevBackend,
    RunMeta,
    RunSummary,
    Scenario,
    TickRecord,
    Usage,
)
from jevcity.world import loader as world_loader
from jevcity.world import market, network

logger = logging.getLogger(__name__)

# K estimation constants (see docs/CONTRACTS.md "Batching"), in real tokens: prompts measured
# ~640 len/4-units per agent at K=3, and real calls count ~1.7x more (2.3 chars/token).
EST_TOKENS_PER_AGENT = 1100
EST_SHARED_TOKENS = 350

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


def _agent_snapshot(a: Agent) -> AgentSnapshot:
    """One AgentSnapshot row (initial population write, and per-tick arrivals)."""
    return AgentSnapshot(
        id=a.id,
        age=a.age,
        occupation=a.occupation,
        home=a.home,
        employed=a.employed,
        job_district=a.job_district,
        wage_monthly=a.wage_monthly,
        rent_monthly=a.rent_monthly,
        satisfaction=a.satisfaction,
        tenure=a.tenure,
        children=a.children,
        has_car=a.has_car,
        commute_mode=a.commute_mode,
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
    if scenario.districts is not None:
        profiles = [p for p in profiles if p.id in scenario.districts]
    agents_list = generator.generate_population(profiles, scenario.n_agents, rng)
    agents_by_id = {a.id: a for a in agents_list}
    world = market.init_world(profiles, agents_list)

    # Zone-level transit network (docs/TRANSIT_ACCESS.md section 2): entirely opt-in via
    # scenario.access_path, so a scenario that doesn't set it consumes no extra rng draws and
    # produces no extra fields anywhere -- behaviour stays byte-identical to before this existed.
    if scenario.access_path is not None:
        access = network.load_access(scenario.access_path)
        network.validate_access(access, {p.id for p in profiles})
        world.access = access
        world.network_variant = "base"
        for agent in agents_list:
            agent.home_zone = network.sample_home_zone(access, agent.home, rng)
            if agent.employed and agent.job_district is not None:
                agent.job_zone = network.sample_job_zone(access, agent.job_district, rng)

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

    agent_snapshots = [_agent_snapshot(a) for a in agents_list]
    writer.write_agents(agent_snapshots)

    if backend is None:
        backend = jev.make_backend(scenario.jev, sink=writer)

    # Robustness: build_requests gains an optional max_districts_in_state kwarg (prompts task,
    # possibly not yet landed) - only pass it through if the callable in place accepts it, so
    # this engine works against either signature.
    build_requests_kwargs: dict[str, int] = {}
    try:
        sig = inspect.signature(state_builder.build_requests)
    except (TypeError, ValueError):  # pragma: no cover - defensive, signature() rarely fails
        sig = None
    if sig is not None and "max_districts_in_state" in sig.parameters:
        build_requests_kwargs["max_districts_in_state"] = scenario.prompt.max_districts_in_state

    # ARRIVED events for households that spawned this tick are queued for the *next* tick (so
    # they get a chance to settle in before their first decision) and merged into that tick's
    # detected events.
    pending_events: dict[int, list[Event]] = {}

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
            queued_events = pending_events.pop(tick, [])
            if queued_events:
                events = [*events, *queued_events]
            reqs = state_builder.build_requests(
                world, agents_by_id, events, tick, k, **build_requests_kwargs
            )

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

            # market.daily_update_ex (arrivals/departures/tourism/commerce) supersedes plain
            # daily_update once the market task lands it; fall back to the old signature
            # (list[AgentChange], no migration) until then, per docs/CONTRACTS.md.
            daily_update_ex = getattr(market, "daily_update_ex", None)
            if daily_update_ex is not None:
                daily_delta = daily_update_ex(world, agents_by_id, scenario, tick, rng)
                daily_changes = daily_delta.changes
                new_arrivals = daily_delta.arrivals
                new_departures = daily_delta.departures
            else:
                daily_changes = market.daily_update(world, agents_by_id, scenario, tick, rng)
                new_arrivals = []
                new_departures = []

            # daily_update_ex owns adding new agents to world/market state; agents_by_id is
            # only mutated here if it hasn't been already (idempotent either way).
            for new_agent in new_arrivals:
                agents_by_id.setdefault(new_agent.id, new_agent)
                pending_events.setdefault(tick + 1, []).append(
                    Event(agent_id=new_agent.id, kind=EventKind.ARRIVED)
                )

            arrival_ids = [a.id for a in new_arrivals]
            departure_ids = [*delta.departures, *new_departures]

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

            districts = snapshot.build_district_snapshots(
                world, agents_by_id, arrivals=arrival_ids, departures=departure_ids
            )

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
                arrivals=[_agent_snapshot(a) for a in new_arrivals],
                departures=departure_ids,
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
