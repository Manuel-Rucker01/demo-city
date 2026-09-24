# Module contracts

Source of truth for types: `src/jevcity/types.py`. Signatures: the stub modules under
`src/jevcity/`. Do not change either without the integrator's approval; if you think a change
is needed, implement around it and say so in your report.

## Tick pipeline (engine/loop.py)

```
apply_policies(world, scenario, tick)
events    = detect_events(world, agents, tick, scenario.events, rng)      # events/triggers.py
events   += pending_arrived_events.pop(tick, [])                            # see "Migration" below
reqs      = build_requests(world, agents, events, tick, K,
                            max_districts_in_state=scenario.prompt.max_districts_in_state)
                                                                              # prompts/state_builder.py
                                                                              # (kwarg passed only if
                                                                              #  the callable accepts it)
responses = await backend.evaluate_many(reqs)                               # jev/
decisions = parse_decisions(reqs, responses, scenario.jev.confidence_threshold)  # prompts/parse.py
delta     = apply_decisions(world, agents, decisions, scenario, rng, tick)  # world/market.py
daily     = daily_update_ex(world, agents, scenario, tick, rng)             # world/market.py
                                                                              # (TickDelta; falls back
                                                                              #  to daily_update(...) ->
                                                                              #  list[AgentChange], no
                                                                              #  migration, if absent)
# --- rental supply (only when scenario.rental_supply.enabled) --- see "Rental supply" below
writer.write_tick(TickRecord(...))                                           # runlog/
```

One `numpy.random.Generator(seed)` owned by the engine is passed everywhere: same seed +
same Jev answers => identical run.

`scenario.districts` (`None` = every district in the data file) filters the `DistrictProfile`
list loaded from `data_path` *before* population generation, so a scenario can simulate a
subset of the 10 districts.

### Migration (arrivals/departures)

- `world/market.py`'s `daily_update_ex(world, agents, scenario, tick, rng) -> TickDelta` is the
  single owner of both directions of migration: it calls `population.generator.spawn_arrivals`
  for new households (sized from `scenario.migration.arrivals_per_month_per_1000`) and folds
  them into `world`/`agents` itself (occupied units, filled jobs), returning them in
  `TickDelta.arrivals`; agents whose `LEAVE_CITY` `MOVE` decision (from `apply_decisions`,
  handled by `world/market.py`) or another migration rule sends them out of Barcelona have
  `Agent.active` set to `False` and their id appended to `TickDelta.departures`.
  `apply_decisions`'s own `TickDelta.departures` (from `LEAVE_CITY` decisions) and
  `daily_update_ex`'s `TickDelta.departures` are concatenated by the engine into one
  per-tick departures list.
- The engine adds every `TickDelta.arrivals` agent to `agents_by_id` (a no-op if
  `daily_update_ex` already inserted them - the operation is idempotent), then queues an
  `EventKind.ARRIVED` event for each, for **tick + 1** (`pending_arrived_events`), so a new
  household gets its first decision the day after it settles rather than the same day. That
  queued event is merged into `detect_events`'s output at the top of the next tick.
- `TickRecord.arrivals` is written as `list[AgentSnapshot]` (same shape as `agents.json`, so the
  web view can add new dots) and `TickRecord.departures` as `list[int]` (ids only, so the web
  view can remove dots). Departed agents are **not** deleted from `agents_by_id` - they stay
  (with `active=False`) so later ticks can still reference their id if needed; `engine/snapshot.py`
  excludes inactive agents from every resident-based figure.
- If `market.daily_update_ex` doesn't exist yet (module still mid-change), the engine falls back
  to the old `daily_update(world, agents, scenario, tick, rng) -> list[AgentChange]`: no
  arrivals/departures happen, `TickRecord.arrivals`/`.departures` are empty for that run.

### Rental supply (landlord decisions)

Off entirely (no extra calls, no behaviour change) when `scenario.rental_supply.enabled` is
`False` (the default) - see `RentalSupplyParams` in types.py.

When enabled, after `apply_decisions` and `daily_update_ex` (or its `daily_update` fallback)
have run for the tick, the engine collects `TickDelta.vacancies` from *both* deltas (a rental
unit can free up either because its tenant moved away via `apply_decisions`, or through
`daily_update_ex`'s own mechanics). If that combined list is non-empty:

```
landlord_reqs      = build_landlord_requests(world, vacancies, scenario, tick)   # prompts/state_builder.py
landlord_responses = await backend.evaluate_many(landlord_reqs)                  # SECOND round of Jev
                                                                                    # calls this tick;
                                                                                    # counted in usage_tick
                                                                                    # like the resident round
landlord_decisions = parse_landlord_decisions(
    landlord_reqs, landlord_responses, scenario.jev.decision_policy, scenario.seed
)                                                                                 # prompts/parse.py
landlord_actions_by_kind = apply_landlord_decisions(
    world, landlord_decisions, scenario, rng, tick
)                                                                                 # world/market.py
                                                                                    # -> dict[str, int],
                                                                                    #    written to
                                                                                    #    TickRecord
```

`build_landlord_requests`/`parse_landlord_decisions` (prompts) and `apply_landlord_decisions`
(world/market) are looked up with `getattr` (like `daily_update_ex`): if a collaborator hasn't
landed yet, that step is skipped (no landlord requests built / no decisions applied /
`landlord_actions_by_kind` stays `{}`) rather than raising, so this engine works against
whichever of those functions currently exist. A budget-exceeded error during the landlord round
stops the run early exactly like one during the resident round (same warning message and
`warnings.json` entry); a tick that stops there has already applied its resident decisions and
`daily_update_ex` but its `TickRecord` is not written.

`DecisionRequest.kind == "landlord"` requests are ordinary `DecisionRequest`s otherwise (same
`state`/`questions`/`mock_priors` shape), so the mock and replay backends need no special-casing:
`cache_key`/`request_body` only look at `{state, questions}`, not `kind` or `agent_ids`.

`engine/snapshot.py`'s new `DistrictSnapshot` fields are filled from `DistrictState`
(`owner_units`, `rental_units`, `seasonal_units`, `quality` - copied straight through, `0`/`1.0`
defaults even before the market collaborator populates them) plus two derived figures:
- `rental_vacancy_rate` = `max(rental_units - <renters in this district> - renovating_units, 0)
  / rental_units` (0 if `rental_units == 0`) - `renovating_units` is documented in types.py as
  off-market but counted inside `rental_units`, so it's excluded from what counts as "vacant and
  available".
- `below_market_share` = share of RENTER residents in the district for whom
  `world.below_market(agent, world)` is `True` (a rental-supply collaborator helper whose home
  module isn't fixed yet - `world/rental.py` or `world/market.py` - looked up by `importlib` at
  each snapshot build and cached; falls back to `agent.rent_monthly < 0.85 * district.avg_rent`
  if neither module defines it yet).
- `new_units_completed` = `world.completed_today.get(district_id, 0)` (`getattr` fallback to
  `{}` if the market collaborator hasn't added `World.completed_today` yet).

### Batch aggregation (engine/batch.py)

`_FINAL_SCALAR_METRICS` (per-district final-tick stats) and `_SERIES_SCALAR_METRICS`
(per-district per-tick series) both gained the rental-supply `DistrictSnapshot` fields
(`owner_units`, `rental_units`, `seasonal_units`, `rental_vacancy_rate`, `quality`,
`below_market_share` in the former; `rental_units`, `seasonal_units` added to the latter
alongside the existing `avg_rent`/`avg_satisfaction`). Cumulative landlord actions and
completed-construction units are **citywide totals per run**, not per-district, so they live in
a separate top-level `batch_summary.json` key instead of `final_metrics`/`series` (only present
when `run_batch`'s ticks were read back, same condition as `series`):

```json
"cumulative": {
  "landlord_actions": {"relet": {"mean": ..., "std": ..., "min": ..., "max": ...}, "sell": {...}, "seasonal": {...}, "renovate": {...}},
  "completed_units": {"mean": ..., "std": ..., "min": ..., "max": ...}
}
```

Each run's totals are `sum(TickRecord.landlord_actions_by_kind)` over every tick (by kind, `0`
for a kind that never occurred) and `sum(DistrictSnapshot.new_units_completed)` over every
district and tick; `engine.batch.aggregate_cumulative` then takes mean/std/min/max of those
per-run totals across seeds, same statistics helper as everything else in this module. `jevcity
compare`'s single-run equivalent (`total_units_completed`/`landlord_actions` rows) and `jevcity
compare-batch`'s "cumulative" section read the same underlying sums.

## Jev request shape (prompts -> jev)

- One `DecisionRequest` = one HTTP call. Body sent = `{"state", "model", "questions"}`.
- Questions per agent (keys `question_key(agent_id, name)`):
  - `action`: Choice, options = `Action` values (stay, move, job_search, spend, save), each with a criteria description.
  - `destination`: Choice, options = district ids, criteria = display name + short description. Asked speculatively; used only when action == move.
  - `spending`: Score, 5 levels (lowest to highest discretionary spending).
  - `satisfaction`: Score, 5 levels (very unhappy .. very happy with their situation).
- `K = agents_per_request`. K == 1: state = {"person", "today", "districts"}. K > 1: state =
  {"districts", "people": {"p<id>": {...}}}, instructions reference `` `people.p<id>` ``.
- Answer shapes: see docs/jev-reference/api.md (choice: choice/probabilities/confidence;
  score: score/legend/probabilities/confidence; noul: noul).

## Run directory (runlog -> web)

```
runs/<run_id>/meta.json          RunMeta
runs/<run_id>/agents.json        list[AgentSnapshot]   (initial population)
runs/<run_id>/ticks.ndjson       one TickRecord per line
runs/<run_id>/jev_calls.ndjson.gz   one CallRecord per line (replay cache)
runs/<run_id>/summary.json       RunSummary (written at the end)
```

`TickRecord` (per line of `ticks.ndjson`), fields relevant to district/migration reporting:
- `districts: list[DistrictSnapshot]` - one row per district in `world.states`, built by
  `engine/snapshot.build_district_snapshots`. Beyond the original rent/jobs/satisfaction
  figures it now also carries `tourist_units`/`shops_open`/`shop_revenue_monthly` (copied
  straight from `DistrictState`), `mode_share` (commute-mode distribution among active,
  employed residents who have a `commute_mode` set - not all residents), `online_share`
  (share of active residents whose `shopping_place` is `ONLINE`), and this tick's
  `arrivals`/`departures` counts for that district. Inactive agents (`Agent.active is False`,
  i.e. already left the city) are excluded from every resident-based figure.
- `arrivals: list[AgentSnapshot]` - new households that moved into Barcelona this tick (see
  "Migration" above); empty most ticks.
- `departures: list[int]` - agent ids that left Barcelona this tick (via a `LEAVE_CITY` MOVE
  decision or `daily_update_ex`'s own migration-out rule); empty most ticks.

`jevcity export-web` copies meta/agents/ticks/summary (not jev_calls) to
`web/public/runs/<run_id>/` and writes `web/public/runs/index.json`:

```json
[{"run_id": "...", "scenario": "base", "description": "...", "ticks": 365, "n_agents": 1000}]
```

District polygons: `data/processed/districts.geojson` (FeatureCollection, `properties.id` =
DistrictId, WGS84), copied to `web/public/data/districts.geojson`.

## Batch directory layout (engine/batch.py -> runlog)

`jevcity batch` writes one ordinary run directory per seed plus a batch-level summary:

```
runs/<batch_id>/s<seed>/          ordinary run dir (meta/agents/ticks/summary/jev_calls, as above)
runs/<batch_id>/batch_summary.json
```

`batch_summary.json` shape (built by `engine/batch.aggregate_batch`):

```json
{
  "scenario": "base", "provider": "mock",
  "seeds": [1, 2, 3], "succeeded_seeds": [1, 2, 3], "resumed_seeds": [],
  "failed": [{"seed": 4, "error": "RuntimeError: ..."}],
  "final_metrics": {
    "<district_id>": {
      "avg_rent": {"mean": ..., "std": ..., "min": ..., "max": ...},
      "vacancy_rate": {...}, "unemployment_rate": {...}, "avg_satisfaction": {...},
      "residents": {...}, "tourist_units": {...}, "shops_open": {...}, "online_share": {...},
      "mode_share": {"metro": {"mean": ..., "std": ..., "min": ..., "max": ...}, "car": {...}, ...}
    }
  },
  "series": {
    "<district_id>": {
      "avg_rent": [{"tick": 1, "mean": ..., "min": ..., "max": ...}, ...],
      "avg_satisfaction": [...],
      "mode_share.metro": [...], "mode_share.car": [...]
    }
  },
  "usage_total": { "...": "Usage, summed via Usage.add over every succeeded seed" }
}
```

`final_metrics`/`series` are computed only from seeds whose run finished (`succeeded_seeds`); a
district or mode missing from one seed's run is treated as absent for that seed's data point
(final metrics) or 0.0 share (mode_share only) rather than dropped from the aggregate. std is
the population standard deviation (ddof=0) so a single-seed batch is well-defined (std=0).
`compare_batches` reads two `batch_summary.json` payloads and, per district/metric, reports
`effect = mean_B - mean_A` against `noise = sqrt((std_A^2 + std_B^2) / 2)`, flagging `clear` when
`abs(effect) > 2 * noise` - a heuristic, not a significance test.

Resume: `run_batch` skips (and marks `skipped=True` / lists under `resumed_seeds`) any seed
whose `s<seed>/summary.json` already exists, so re-launching a crashed batch with the same
`--batch-id` does not re-pay for finished seeds. Concurrency: seeds run as asyncio tasks in one
process gated by `asyncio.Semaphore(parallel)`, not subprocesses; each run builds its own Jev
backend, so its rate limiter is per-run, not shared - `--parallel P` against a real provider
multiplies the effective request rate by up to P.

## Providers (jev/)

`JEV_PROVIDER=mock|typesafe|openrouter|vercel` (env wins over `scenario.jev.provider`).
Provider settings (URL, path, wire format, key env var, default model, rpm/tps limits,
context size, fallback price, TODOs) come from `config/providers.yaml`; scenario
`jev.overrides` and env `JEV_RPM_LIMIT`, `JEV_TPS_LIMIT`, `JEV_MODEL`, `JEV_BASE_URL` override them.

- Inside jevcity everything is TypeSafe-shaped (`noul`/`choice`/`score`, `confidence`,
  `legend`, `usage.input_tokens`). Each wire format has a codec: `encode(canonical body) ->
  wire body` and `decode(wire response) -> JevResponse`. Nothing outside `jev/` knows the provider.
- Wire formats: `systemone` (TypeSafe; also OpenRouter `/api/v1/systemone` and Vercel
  `/typesafe/v1/systemone`), `openrouter_decisions` (`/api/alpha/decisions`, same schema),
  `vercel_evaluate` (`/v1/evaluate`: `boolean`/`probability`, camelCase usage, cost in
  `providerMetadata.gateway.cost`).
- `mock`: no network. Weighted random answers from `DecisionRequest.mock_priors`, tokens
  estimated as len(canonical JSON body)/4, cost estimated at the `mock_as` provider's price,
  `Usage.estimated=True`.
- Replay (`jev.replay_from`): answers looked up by `cache_key` in a previous
  `jev_calls.ndjson.gz`; a miss is an error (never falls back to the network).
- Every response's `model` (exact version reported) is kept in `CallRecord.resolved_model` and
  counted in `Usage.models_seen`; the engine warns if it changes mid-run.
- Rate limiting: token buckets for rpm and input tokens/s at `rate_safety` x configured limits,
  adaptive (x0.7 on 429, slow recovery), exponential backoff with jitter on 408/429/5xx/524/529,
  honoring `retry-after` / `retry-after-ms`.
- Batching: `quality` uses K = `agents_per_request` (default 1). `throughput` picks the
  largest K <= `max_agents_per_request` whose estimated request fits the provider context.
