# Module contracts

Source of truth for types: `src/jevcity/types.py`. Signatures: the stub modules under
`src/jevcity/`. Do not change either without the integrator's approval; if you think a change
is needed, implement around it and say so in your report.

## Tick pipeline (engine/loop.py)

```
apply_policies(world, scenario, tick)
events    = detect_events(world, agents, tick, scenario.events, rng)      # events/triggers.py
reqs      = build_requests(world, agents, events, tick, K)                  # prompts/state_builder.py
responses = await backend.evaluate_many(reqs)                               # jev/
decisions = parse_decisions(reqs, responses, scenario.jev.confidence_threshold)  # prompts/parse.py
delta     = apply_decisions(world, agents, decisions, scenario, rng, tick)  # world/market.py
changes   = daily_update(world, agents, scenario, tick, rng)                # world/market.py
writer.write_tick(TickRecord(...))                                           # runlog/
```

One `numpy.random.Generator(seed)` owned by the engine is passed everywhere: same seed +
same Jev answers => identical run.

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
runs/<run_id>/jev_calls.ndjson   one CallRecord per line (replay cache)
runs/<run_id>/summary.json       RunSummary (written at the end)
```

`jevcity export-web` copies meta/agents/ticks/summary (not jev_calls) to
`web/public/runs/<run_id>/` and writes `web/public/runs/index.json`:

```json
[{"run_id": "...", "scenario": "base", "description": "...", "ticks": 365, "n_agents": 1000}]
```

District polygons: `data/processed/districts.geojson` (FeatureCollection, `properties.id` =
DistrictId, WGS84), copied to `web/public/data/districts.geojson`.

## Modes

- `mock`: no network. Weighted random answers from `DecisionRequest.mock_priors`, same JSON
  shape as the real API, token usage estimated as len(canonical JSON body)/4, `Usage.estimated=True`.
- `real`: httpx POST to `JevConfig.base_url`, key from env `JEV_API_KEY`, rate limited
  (RPM + tokens/s), retries 408/429/5xx/529 with exp backoff + honors retry-after.
- `replay`: answers looked up by `cache_key` in a previous `jev_calls.ndjson`; a miss is an error
  (never falls back to the network).
