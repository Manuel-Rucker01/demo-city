# Jev City — web replay

Static, backend-free replay viewer for Jev City simulation runs: a dark, deck.gl-powered map of
Barcelona's 5 districts with 1,000–10,000 animated agent dots, ECharts panels, and a recording
mode for LinkedIn-ready video capture. Built with Vite + TypeScript (strict), no UI framework.

## Run it

```bash
npm i
npm run fake-data   # generates two fake runs (base + rent_cap_gracia) into public/runs/,
                     # and a districts.geojson (real one if data/processed/districts.geojson
                     # exists yet, otherwise a clearly-marked placeholder)
npm run dev          # http://localhost:5173
```

Once the Python side has produced real runs, copy them into `public/runs/<run_id>/` (or run
`jevcity export-web`, see `docs/CONTRACTS.md`) and refresh — `fake-data` never overwrites a real
`data/processed/districts.geojson` if one is already there, it copies it instead.

Other scripts:

```bash
npm run build       # tsc --noEmit + vite build -> dist/
npm test            # vitest run
npx tsc --noEmit    # typecheck only
```

## URL params

| param      | example                  | effect                                                   |
|------------|---------------------------|-----------------------------------------------------------|
| `record`   | `?record=1`                | fixed 1920×1080 recording layout: hides all controls, autoplays after 1s, shows a fading title card, date ticker, and footer |
| `run`      | `&run=fake-base-001`       | primary run id (defaults to the first `scenario: base` run in the index) |
| `compare`  | `&compare=fake-rent-cap-gracia-001` | second run id — turns on split-screen compare mode |
| `speed`    | `&speed=4`                  | playback speed multiplier for record mode (1–10) |
| `title`    | `&title=...`                | (optional) override the title-card headline |

Full record-mode URL used for the demo video:

```
http://localhost:5173/?record=1&run=<base_run_id>&compare=<rent_cap_run_id>&speed=4
```

## Recording tips

- Open the URL above in Chrome, resize the browser window (not just the viewport) to exactly
  1920×1080 — the layout is fixed at that size in record mode so nothing reflows mid-recording.
- Either capture with OBS (Window Capture on the Chrome window, no browser chrome) or Chrome's
  own screen recording extension. `?record=1` already hides the header, run selector, and
  playback bar, so there's nothing to crop out.
- The title card fades out automatically after ~4.4s; playback starts 1s after load, so there's
  a clean ~1s beat of just the title card before the city starts moving — start your recording
  right when you navigate to the URL, no need to time it manually.
- Keyboard shortcuts (space = play/pause, ←/→ = step a day) still work outside record mode, for
  scrubbing to a specific day before switching to the record URL if you want to start mid-run.

## Design decisions

**Compare layout — split-screen maps, not overlaid charts alone.** Two full `MapView` instances
(two maplibre + deck.gl contexts) render side by side, each with its own "Base" / "Rent cap ·
Gràcia" label, synced to one shared `Playback` clock. This reads far better on video than a
single map: seeing both cities breathe and move in lockstep, with Gràcia visibly attracting more
dots (or renting cheaper) in the capped run, is the whole story in one glance. The charts panel
underneath still overlays both runs on the same axes — solid line for base, dashed for the
scenario, with the policy district (Gràcia) drawn thicker — so the numeric divergence is legible
too. Two maps did cost more GPU/CPU than one, but at 1,000–10,000 points per side it stayed
smooth; see Performance notes below.

**Color modes** (district / employment / satisfaction) apply to both maps at once via one
button group, so a comparison stays visually consistent across panes.

**Fill metric** for the district choropleth (avg rent / unemployment / satisfaction) uses the
same perceptually-uniform (viridis-derived) ramp as the satisfaction dot color mode, so the
audience only has to learn one color language.

## Performance notes

- **State reconstruction is O(agents) per frame, not O(ticks).** `src/data/reconstruct.ts`
  precomputes dense typed-array "frames" (`Uint8Array`/`Float32Array`, one row per tick,
  tick-major so a frame is one contiguous slice) from `agents.json` + the cumulative
  `moves`/`changes` in `ticks.ndjson`. Scrubbing to any day is then a single `subarray()` — no
  replay loop — which is what makes smooth scrubbing across 10,000 agents × 365 ticks possible.
- **deck.gl data arrays are stable across frames.** `MapView` keeps one `{length}` data handle
  and mutates the same `Float64Array`/`Uint8Array` position/color buffers in place every
  animation frame (breathing jitter, move interpolation); it never allocates a new data array or
  object literal per frame. Redraws are triggered via `updateTriggers` (a bumped frame counter),
  which is the documented deck.gl pattern for large animated point clouds.
- **Moves animate without resampling.** Every agent's position inside every district is
  precomputed once (seeded by agent id, so it's reproducible), so an inter-district move just
  interpolates between two already-known points over ~1.5s with an eased arc — no per-move
  polygon sampling at runtime.
- **NDJSON is streamed**, not loaded as one JSON blob, so the ~1,000-agent/365-tick fake runs
  (~1.7MB each) and a 10,000-agent run (proportionally larger) don't block the main thread on
  parse; a truncated last line (run still being written, or no trailing newline) is dropped
  instead of throwing.
- Two-map compare mode roughly doubles WebGL context / point-rendering cost; in the fake-data
  smoke test (1,000 agents/side) this stayed comfortably at 60fps. If a 10,000-agent compare run
  turns out to be too heavy on lower-end hardware, the fallback is to drop split-screen to a
  single map + overlaid charts (`compareMode` in `src/main.ts` is the single toggle point).

## Data layer

- `src/data/types.ts` mirrors `src/jevcity/types.py` field-for-field (snake_case) — see
  `docs/CONTRACTS.md`.
- `src/data/ndjson.ts` — NDJSON parsing/streaming, truncated-line tolerant.
- `src/data/reconstruct.ts` — per-tick agent state reconstruction (the O(1)-scrub frames above).
- `src/data/sankey.ts` — cumulative move-flow aggregation for the Sankey chart.
- `src/data/loadRun.ts` — fetches a run's static files and wires the above together.
- `src/map/geo.ts` — deterministic seeded point-in-polygon sampling for agent dot placement.

Run `npm test` for the full suite (NDJSON parsing incl. a truncated last line, state
reconstruction vs. a naive from-scratch replay, point-in-polygon sampler determinism, Sankey
aggregation, and formatting helpers).
