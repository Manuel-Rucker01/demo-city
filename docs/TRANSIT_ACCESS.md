# Transit access contract (zones, travel times, network variants)

Goal: simulate a **real** new metro line (first case: the L9 central section, Zona Universitària to
La Sagrera, 12 stations) at the level where people actually decide. That means knowing who gets
walking-distance access they did not have before (level 1) and how many door-to-door minutes each
person's commute saves by each mode (level 2).

The types are in `src/jevcity/types.py`: `ZoneId`, `Zone`, `Station`, `TransitAccess`,
`TransitNetworkPolicy`, `Agent.home_zone/job_zone`, `World.access/network_variant` and
`Scenario.access_path`. The function signatures are in `src/jevcity/world/network.py`. Do not change
either without the integrator; if you think something is wrong, work around it and say so in your
report.

## 1. Data file: `data/processed/transit_access.json` (built by `scripts/build_transit_access.py`)

It is one `TransitAccess` JSON document.

- **`zones`**: the 73 Barcelona barris, ordered by id. Each has:
  - `id`: the two-digit Open Data BCN code.
  - `name` and `district`, where `district` is a jevcity `DistrictId` slug from
    `data/processed/districts.json`.
  - `population` (padró, most recent year).
  - `centroid`, weighted by population and computed from census sections (seccions censals).
  - `job_weight`: the share of the district's jobs located in the barri. It sums to 1 within each
    district. Use a real jobs-by-barri source if one exists. Otherwise use a documented proxy (e.g.
    ground-floor commercial premises + offices) and write `"job_weight": "proxy: ..."` in `sources`.
  - `rail_coverage` and `new_coverage[variant]`.
- **Coverage** is computed on census-section population points. A resident is "covered" if within
  `radius_m` (600 m crow-fly) of a station.
  - `rail_coverage` counts every station that exists today: metro, FGC, tram and Rodalies inside or
    near Barcelona.
  - `new_coverage[v]` counts residents covered by a station added in variant `v` **and not** covered
    by any existing station.
- **`variants`**: `["base", "l9_central"]`.
  - `l9_central` = today's network plus the full L9/L10 central section: Zona Universitària, Campus
    Nord, Manuel Girona, Prat de la Riba, Sarrià, Mandri, El Putxet, Lesseps, Travessera de Dalt,
    Sanllehy, Guinardó-Hospital de Sant Pau, Plaça de Maragall and La Sagrera.
  - It is joined to the existing L9 Sud (at Zona Universitària) and L9/L10 Nord (at La Sagrera), so
    L9 runs as one line.
  - Stations that also exist today (Zona Universitària, Lesseps, Maragall, Sagrera) become
    interchanges with the new line.
- **`modes`**: `["metro", "bus", "car", "bike", "walk"]`, the CommuteMode values. `metro` means any
  rail.
- **`times[variant][mode]`**: row-major door-to-door minutes over `len(zones)**2` pairs, rounded to
  0.1. Origin: the zone's census-section population points (population-weighted mean over origins).
  Destination: the zone's centroid. Diagonal = trips within the zone. Every variant lists every mode;
  only `metro` should differ between variants.
- **`stations`**: every station used. New ones carry their variant. Set `approx: true` when the
  coordinates are not from an official or OSM source.
- **`sources`**: provenance of every input (dataset id or URL plus access date).
- **`assumptions`**: every constant below, with the value used.

### Travel-time model (defaults; change only with a reason written in `assumptions`)

| Mode | Model |
|---|---|
| walk | crow-fly × 1.3 detour / 4.8 km/h + net ascent / 10 m per minute (Naismith-style; descents get no bonus) |
| bike | crow-fly × 1.3 / 13 km/h + net ascent / 6 m per minute (casual rider, ~360 vertical m/hour; descents get no bonus) + 5 min (unlock/lock, dock or parking, both ends) |
| car | crow-fly × 1.3 / 20 km/h + 8 min (parking + walk) |
| bus | 3 min access + 5 min wait + crow-fly × 1.3 / 11 km/h + 3 min egress (no route data; stated as such) |
| metro (rail) | walk to any of the 5 nearest stations (≤1.5 km) + wait (half headway) + in-vehicle + transfers + walk from the station to the destination. Best over station choices; shortest path on the line graph. The access/egress walking legs carry the same net-ascent penalty as a standalone walk trip (origin → boarding station, alighting station → destination). |

Barcelona rises steeply from the sea toward Collserola (Sarrià-Sant Gervasi, Horta-Guinardó, Nou
Barris, upper Gràcia); a flat crow-fly model made bike the fastest mode for the large majority of
commutes, which does not match its real ~3% mode share (EMEF 2024). Elevation (ground level, m) is
looked up per point -- every census-section population point, every zone centroid, and every
station -- from a public DEM (default: OpenTopoData `eudem25m`), cached in
`data/raw/transit/elevations_eudem25m.json` and recorded in `sources`/`SOURCES.md`. Net ascent =
max(0, elevation[destination] − elevation[origin]) in the direction of travel; descents never get a
time bonus.

Rail parameters:

- **In-vehicle time** comes from consecutive-stop crow-fly distance × 1.15, divided by the commercial
  speed: metro 27 km/h, FGC 30, tram 18, Rodalies 40.
- **Headways:** metro 4 min, L9/L10 6, FGC 6, tram 8, Rodalies 15. The wait is half the headway.
- **Transfers:** 5 min walking penalty plus the new line's wait.
- **Line topology:** the ordered stop sequence of each line from OpenStreetMap route relations. Merge
  a station's platforms by name and position so transfers work.

**Sanity output (a report, not a gate).** Apply a plain multinomial logit to the base times:
- utility = −0.1 × minutes.
- Car only for car-owning households (`car_ownership` per district).
- Walk not considered beyond 40 min.

The resulting city-wide shares are printed next to EMEF 2024 (walk 51.3 %, bike 3.2 %, rail 17.3 %,
bus 11.6 %, car 16.7 %; all trip purposes).

Also print the 20 zone pairs with the largest L9 time gain, and each district's population-weighted
new coverage. Produce `media/l9_access_map.png`, which shows the zones coloured by `new_coverage`,
existing stations grey and new stations red.

## 2. Simulation behaviour (T3)

Everything below applies only when `Scenario.access_path` is set. Without it, behaviour and outputs
must stay byte-identical to today, and the tests prove it.

- **Loading:** `load_access` + `validate_access` run where the world is built. `World.access` is set
  and `World.network_variant = "base"`.
- **Population:**
  - Each agent gets `home_zone = sample_home_zone(home)`.
  - Employed agents get `job_zone = sample_job_zone(job_district)`.
  - Arrivals get the same treatment.
  - On a move, `home_zone` is redrawn in the new district. On a new job, `job_zone` is redrawn.
  - Use the engine rng, so runs stay deterministic.
- **Policy:** `apply_policies` sets `world.network_variant = active_variant(scenario, tick)` every
  tick (idempotent, like the other policies). `TransitLinePolicy` stays as it is.
- **Events:** when the variant changes, the existing `TRANSIT_CHANGE` awareness mechanism (spread
  over `transit_awareness_days`, deterministic offsets) informs only affected agents:
  - An **employed agent** whose rail trip `home_zone -> job_zone` gets at least `min_gain_minutes`
    shorter receives payload `{"kind": "network", "line": label, "before_min": b, "after_min": a}`.
  - Any **other agent** (including employed agents who did not gain) receives payload
    `{"kind": "network_access", "line": label}` with probability `new_coverage[variant]` of their
    home zone. Draw deterministically per agent: hash of (agent_id, variant), like
    `_awareness_offset`.
- **Event texts** (state_builder, compact):
  - `"The new {line} opened: your trip to work by metro now takes ~{a} min instead of ~{b}."`
  - `"A new {line} station opened within walking distance of your home."`
- **Person block:** when the agent has trip times and a usual commute mode, add
  `"trip_to_work": "usual: metro ~38min"` (car adds `+parking`) — the usual mode only
  (`prompts/buckets.py` `usual_trip_text`). The full per-mode list (`trip_times_text`, e.g.
  `"metro 38min, bus 44min, car 25min+parking, bike 24min"`) was tested and rejected: on the same
  200 commute decisions Jev kept its current mode 99% of the time without times or with the usual
  trip only, but 61% with the full list (metro share doubled, bike ×7). A new line's time gain
  reaches the people it helps through their own TRANSIT_CHANGE event, which carries before/after
  minutes.
- **District blocks (destination choice):** a district with `new_coverage > 0` in the active variant
  gets `", new {line} stations"` appended to its `transit` field, where `{line}` is the policy label.
- **Mock priors (commute_mode):** when times exist, use a prior proportional to
  exp(−minutes / 12) over the allowed modes, blended 50/50 with the current mode's habit weight
  (the existing prior).
- **Tokens:** the person-block addition should average ≤ 45 tokens. Check it with the existing
  token-budget test (`TOKEN_BUDGET_AVG` / `TOKEN_BUDGET_MAX`) and raise the budget only if it truly
  needs to, reporting the numbers.
- **Snapshots/log:** each `DistrictSnapshot` gets no new fields. Add `TickRecord.network_variant`
  only if it is trivial; otherwise skip it.

## 3. Scenarios (T4)

- `scenarios/base_zones.yaml`: `extends: base.yaml`, plus
  `access_path: data/processed/transit_access.json`. This is the new baseline.
- `scenarios/l9_central.yaml`: `extends: base_zones.yaml`, plus
  `TransitNetworkPolicy(variant="l9_central", label="L9", start_tick=60)`.
- A `*_local.yaml` twin of each for `JEV_PROVIDER=local`, with `max_concurrency: 2` and
  `timeout_s: 300`.

## 4. Acceptance

- `uv run ruff check .` and `uv run pytest -q` are green, run with `JEVCITY_PERF_SLACK=5`.
- Mock runs of `l9_central` and `base_zones` (1,000 agents, 365 ticks) complete.
- In the L9 run, agents informed with kind `network` exist only in zones where the line helps.
- A replay of a mock run is identical.
