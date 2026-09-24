# Jev City

A simulation of Barcelona where every citizen's daily decisions come from **Jev**, TypeSafe AI's
"System One" model: instead of generating text, Jev answers typed questions (choice / score /
yes-no) with calibrated probabilities. The engine applies those decisions to a simple housing and
job market and compares policy scenarios, e.g. *what happens if Gràcia caps rents?*

> **Status:** MVP. Every run so far used the **mock** provider (weighted random answers, no API
> calls). One real test call to Jev via Vercel AI Gateway succeeded; no full run has used the real
> model yet. Nothing here is a forecast. See [Limitations](#limitations).

## What it does

- **World:** 5 Barcelona districts (Ciutat Vella, Eixample, Gràcia, Sant Martí, Nou Barris) with
  population, age structure, income and shops from Open Data BCN, and plausible values where no
  open data exists (see [Data](#data)).
- **Agents:** 1,000 synthetic household heads generated from those distributions (tested up to
  10,000): age, occupation, wage, employment, job district, rent, savings, spending, satisfaction.
- **Daily loop (1 tick = 1 day):** only agents with an event that day decide (payday, lease
  renewal, job loss/offer, rent burden crossing 40 %, life event). About 3.7 % of agents per tick.
  Each deciding agent gets a compact state (~290 tokens) and four typed questions:
  - `action`: stay / move / job_search / spend / save (Choice)
  - `destination`: district if moving (Choice, asked speculatively)
  - `spending`, `satisfaction` (5-level Scores)
- **Market:** monthly rent adjustment from vacancy (excess demand), capped at ±0.8 %/month by
  default; job matching against vacancies; spending feeds shop revenue and job creation.
- **Scenarios** in YAML (`scenarios/`), e.g. `rent_cap_gracia.yaml` extends `base.yaml` and caps
  Gràcia's new-lease rent at its initial level from day 30, with renewals frozen.
- **Replay:** every model call is logged; any run can be replayed without calling Jev again.
- **Web view:** dark, video-ready map + charts comparing two runs side by side.

## Quick start

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 20+.

```bash
uv sync
uv run pytest                      # full test suite
uv run jevcity run --scenario scenarios/base.yaml --run-id base-mock
uv run jevcity run --scenario scenarios/rent_cap_gracia.yaml --run-id rent_cap_gracia-mock
uv run jevcity compare runs/base-mock runs/rent_cap_gracia-mock
uv run jevcity export-web runs/base-mock runs/rent_cap_gracia-mock

cd web && npm i && npm run dev     # open ?run=base-mock&compare=rent_cap_gracia-mock
```

A 1,000-agent, 365-day run takes about 2.5 s in mock mode.

Recording for video: `?record=1&run=base-mock&compare=rent_cap_gracia-mock&speed=4` at 1920×1080
(see `web/README.md`).

## Providers

Jev is available directly and through two gateways. Pick one with `JEV_PROVIDER` (or
`--provider`); the rest of the code never knows which one is in use.

| `JEV_PROVIDER` | Key env var | Endpoint used | Notes |
|---|---|---|---|
| `mock` (default) | none | none | Weighted random answers from hand-written priors. Costs are *estimated*. |
| `typesafe` | `TYPESAFE_API_KEY` | `api.typesafe.ai/v1/systemone` | Reports the exact model version. New signups paused as of 2026-09-22. |
| `openrouter` | `OPENROUTER_API_KEY` | `openrouter.ai/api/v1/systemone` | Reports exact version and `usage.cost`. 32k context. |
| `vercel` | `AI_GATEWAY_API_KEY` | `ai-gateway.vercel.sh/typesafe/v1/systemone` | Reports billed cost and `marketCost`; **does not report the exact Jev version**. Native `/v1/evaluate` (`boolean` questions) also supported via `wire: vercel_evaluate`. |

URLs, rate limits, context sizes and prices live in `config/providers.yaml`, not in code, and
can be overridden per scenario (`jev.overrides`) or with `JEV_RPM_LIMIT`, `JEV_TPS_LIMIT`,
`JEV_MODEL`, `JEV_BASE_URL`. Undocumented provider behaviour is listed as `todo` there
(`uv run jevcity providers`).

The adapter throttles on requests/min and tokens/s, backs off exponentially on 408/429/5xx/529
(honouring `retry-after`), slows down adaptively after a 429, stops at `jev.max_cost_usd`, and
logs the exact model string of every response (`models_seen`), warning if it changes mid-run.

Paid providers require `--yes`:

```bash
uv run jevcity estimate --scenario scenarios/base.yaml --provider openrouter
JEV_PROVIDER=openrouter uv run jevcity run --scenario scenarios/base.yaml --yes
uv run jevcity run --scenario scenarios/base.yaml --replay-from runs/<id>/jev_calls.ndjson.gz
```

### Cost and speed (estimates)

Measured on mock runs (1,000 agents): ~37 requests/tick, ~750 input tokens/request
(~290 state + ~460 questions). At $0.042 per million input tokens (output is free):

| Setup | Requests/tick | Cost/tick | Cost per 365-day run | Time/tick (lower bound) |
|---|---|---|---|---|
| 1,000 agents, `quality` (K=1) | ~37 | ~$0.0012 | **~$0.43** | ~2.1 s (req/min binds) |
| 1,000 agents, `throughput` (K auto: 64 TypeSafe, 39 OpenRouter) | ~1 | ~$0.0009 | ~$0.34 | ~0.1 s |
| 10,000 agents, `quality` | ~370 | ~$0.012 | ~$4.3 | ~21 s |

Time bounds use TypeSafe's published limits (1,200 req/min, 250k tokens/s) at 90 %; OpenRouter
and Vercel publish no Jev-specific limits. Real latency observed once via Vercel: ~190 ms at the
provider, ~1 s round trip.

`throughput` mode packs several agents into one state. Jev's own docs warn that longer states and
indirection reduce accuracy, so `quality` is the default until an A/B comparison on the real
model shows the answers hold up.

### How answers become actions

`jev.decision_policy`:
- `sample` (default): draw the action from Jev's calibrated probabilities, deterministically
  per (seed, tick, agent). This keeps minority behaviour in the population instead of collapsing
  every similar agent onto the most likely option.
- `argmax`: always take the top option.
- `gate`: take the top option, unless `confidence` is below `confidence_threshold`, then stay.

## Data

`scripts/fetch_opendata.py` rebuilds `data/processed/` from Open Data BCN. Full provenance is in
[`data/processed/SOURCES.md`](data/processed/SOURCES.md).

| Field | Source |
|---|---|
| Population, age structure | Open Data BCN `pad_mdbas_edat-q` (padró 2025) |
| Disposable income per capita | Open Data BCN `renda-disponible-llars-bcn` (2023) |
| Shops | Open Data BCN `cens-locals-planta-baixa-act-economica` (2024) |
| District boundaries | Open Data BCN `20170706-districtes-barris` (2017) |
| **Rent, vacancy, unemployment, jobs per resident, transit** | **Plausible hand-set values**: no district-level open dataset was found |

## Limitations

This is a demo and a research sandbox, **not a validated model**. Be careful with any conclusion.

- **Not validated against reality.** Neither the market rules nor the agents' behaviour have been
  calibrated or back-tested against observed Barcelona data. The rent cap result shows what *this*
  model does, not what a real cap would do.
- **Every run so far is mock.** Decisions come from hand-written priors
  (`src/jevcity/prompts/questions.py`), not from Jev. Mock results say nothing about how Jev would
  decide.
- **Half the district inputs are invented.** Rent, vacancy, unemployment, job locations and
  transit are plausible guesses, labelled as such.
- **Simplified economy.** Everyone rents (no owners), there is no migration in or out of the
  city, no construction, no tourism or short-term lets, and firms are just job slots. Rent
  dynamics are a vacancy rule with fixed parameters that were tuned by hand to stay in a
  plausible range.
- **Small numbers.** 1,000 agents means ~130–290 per district, so a handful of moves shifts
  vacancy and rents. Use several seeds before reading anything into differences.
- **Jev caveats.** Jev is strongest in English, weak at arithmetic (all numbers are pre-bucketed
  in code), and its probabilities are calibrated for its training tasks, not for predicting
  Barcelona residents. Synthetic survey or market-research use would need validation against real
  panels first.
- **Provider gaps.** Vercel doesn't report which Jev version answered, OpenRouter and Vercel
  don't publish Jev rate limits, and the confidence formula used to fill gaps comes from an
  example in TypeSafe's docs (see `config/providers.yaml`).

## Layout

```
config/providers.yaml     provider URLs, limits, prices, TODOs
scenarios/*.yaml          scenarios (extends + deep merge)
data/processed/           district data + geojson + SOURCES.md
src/jevcity/types.py      shared contracts (everything that crosses modules or hits disk)
src/jevcity/world/        data loader, market dynamics
src/jevcity/population/   synthetic population
src/jevcity/events/       who decides today
src/jevcity/prompts/      state + typed questions, answer parsing, mock priors
src/jevcity/jev/          provider adapter: codecs, rate limiting, retries, cost, mock, replay
src/jevcity/engine/       tick loop, district snapshots
src/jevcity/runlog/       runs/<id>/{meta,agents,summary}.json, ticks.ndjson, jev_calls.ndjson.gz
web/                      replay viewer (Vite, deck.gl, MapLibre, ECharts)
docs/CONTRACTS.md         module contracts
```
