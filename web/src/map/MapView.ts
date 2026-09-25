/** Dark deck.gl + maplibre map: district choropleth + 1,000-10,000 breathing/moving agent dots. */

import * as maplibregl from "maplibre-gl";
import type { Map as MaplibreMap } from "maplibre-gl";
import { MapboxOverlay } from "@deck.gl/mapbox";
import { GeoJsonLayer, ScatterplotLayer, ArcLayer } from "@deck.gl/layers";
import type { AgentSnapshot, DistrictId, DistrictSnapshot } from "../data/types";
import { DISTRICT_IDS } from "../data/types";
import type { DistrictsGeo } from "./districts";
import { precomputeAgentPositions, precomputeAllDistrictPositions } from "./districts";
import {
  COMMUTE_MODE_COLORS,
  COMMUTE_MODE_UNKNOWN_COLOR,
  DISTRICT_COLORS_RGB,
  EMPLOYED_COLOR,
  METRO_OTHER_COLOR,
  METRO_RIDER_COLOR,
  UNEMPLOYED_COLOR,
  metroBlueRamp,
  viridis,
} from "../style/theme";

// maplibre-gl derives its worker's URL from `import.meta.url` of whatever bundle references it,
// which only happens to work in `npm run dev` (Vite serves node_modules/ verbatim) and breaks in
// `npm run build` (no matching path in dist/). vite.config.ts copies the worker script *and* the
// maplibre-gl-shared.mjs chunk it internally imports (unhashed, side by side, exactly as they
// ship in node_modules/maplibre-gl/dist/) into public/vendor/maplibre-gl/ on every dev-server
// start and build, so this fixed path is valid in dev, build, and preview alike. Must run before
// any `new maplibregl.Map(...)` — module load order guarantees that here, since this file is
// always imported before a MapView is constructed.
maplibregl.setWorkerUrl("/vendor/maplibre-gl/maplibre-gl-worker.mjs");

export type ColorMode = "district" | "employed" | "satisfaction" | "commute" | "metro";
export type FillMetric =
  | "avg_rent"
  | "unemployment_rate"
  | "avg_satisfaction"
  | "tourist_units"
  | "shops_open"
  | "metro_share";

const BARCELONA_CENTER: [number, number] = [2.17, 41.4];
const DARK_STYLE = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";

/** [minLon, minLat, maxLon, maxLat] over every coordinate in every district polygon, so the
 * camera can be fit to the real district bounds (e.g. Nou Barris in the north) instead of a
 * hand-picked center/zoom that might crop a district off-screen. */
export function districtsBounds(geo: DistrictsGeo): [[number, number], [number, number]] {
  let minLon = Infinity;
  let minLat = Infinity;
  let maxLon = -Infinity;
  let maxLat = -Infinity;
  const visit = (coords: unknown): void => {
    const arr = coords as unknown[];
    if (typeof arr[0] === "number") {
      const [lon, lat] = coords as [number, number];
      if (lon < minLon) minLon = lon;
      if (lat < minLat) minLat = lat;
      if (lon > maxLon) maxLon = lon;
      if (lat > maxLat) maxLat = lat;
    } else {
      for (const c of coords as unknown[]) visit(c);
    }
  };
  for (const f of geo.featureCollection.features) {
    const geom = f.geometry as { coordinates?: unknown } | null;
    if (geom?.coordinates) visit(geom.coordinates);
  }
  if (!Number.isFinite(minLon)) {
    return [
      [BARCELONA_CENTER[0] - 0.02, BARCELONA_CENTER[1] - 0.02],
      [BARCELONA_CENTER[0] + 0.02, BARCELONA_CENTER[1] + 0.02],
    ];
  }
  return [
    [minLon, minLat],
    [maxLon, maxLat],
  ];
}

interface MoveAnim {
  agentId: number;
  srcIdx: number;
  dstIdx: number;
  srcPos: [number, number];
  dstPos: [number, number];
  startedAtMs: number;
  durationMs: number;
}

interface FadeAnim {
  startedAtMs: number;
  durationMs: number;
  /** "in" = arrival (fade in + settle at home), "out" = departure (fly toward city edge + fade). */
  kind: "in" | "out";
  srcPos: [number, number];
  dstPos: [number, number];
}

export interface MapViewOptions {
  container: HTMLElement;
  geo: DistrictsGeo;
  agents: AgentSnapshot[];
  interactive?: boolean;
  pitch?: number;
}

export class MapView {
  readonly map: MaplibreMap;
  private overlay: MapboxOverlay;
  private geo: DistrictsGeo;
  private agents: AgentSnapshot[];
  private agentIdToIndex: Map<number, number>;

  // Stable typed arrays reused every frame (per the perf brief: no new arrays per frame).
  private homePositions: Float64Array; // current "home" position per agent, mutated on move settle
  private renderPositions: Float64Array; // jittered/interpolated positions actually drawn
  private colors: Uint8Array; // nAgents * 3
  /** Base alpha per agent (0..235) from the active/inactive mask set by setAgentState — arrival
   * fade-in / departure fade-out animations below temporarily override this while in flight. */
  private baseAlpha: Uint8Array;
  /** Alpha actually drawn this frame; recomputed every frame from baseAlpha + any fade anim. */
  private renderAlpha: Uint8Array;
  /** Per-agent alpha multiplier (0-255) applied on top of renderAlpha, used by `color=metro` to
   * dim non-metro riders without disturbing the active/inactive/fade alpha logic above. 255
   * (no-op) for every other color mode. */
  private alphaScale: Uint8Array;
  private allDistrictPositions: Map<DistrictId, Float64Array>;
  private cityBounds: [[number, number], [number, number]];
  private cityCenter: [number, number];

  private colorMode: ColorMode = "district";
  private fillMetric: FillMetric = "avg_rent";
  private districtSnapshots: Map<DistrictId, DistrictSnapshot> = new Map();
  private fillDomain: [number, number] = [0, 1];

  private activeMoves: Map<number, MoveAnim> = new Map();
  private fadeAnims: Map<number, FadeAnim> = new Map();
  private rafHandle = 0;
  private frameCounter = 0;
  private jitterEnabled = true;
  /** Stable data handle reused every frame — deck.gl diffs by reference, so we mutate the
   * underlying typed arrays in place and bump updateTriggers instead of replacing this object. */
  private readonly agentDataHandle: { length: number };

  constructor(opts: MapViewOptions) {
    this.geo = opts.geo;
    this.agents = opts.agents;
    this.agentIdToIndex = new Map(opts.agents.map((a, i) => [a.id, i]));

    this.homePositions = precomputeAgentPositions(opts.agents, opts.geo);
    this.renderPositions = new Float64Array(this.homePositions); // one-time copy, then mutated in place
    this.colors = new Uint8Array(opts.agents.length * 3);
    this.baseAlpha = new Uint8Array(opts.agents.length).fill(235);
    this.renderAlpha = new Uint8Array(opts.agents.length).fill(235);
    this.alphaScale = new Uint8Array(opts.agents.length).fill(255);
    this.allDistrictPositions = precomputeAllDistrictPositions(
      opts.agents.map((a) => a.id),
      opts.geo,
      [...DISTRICT_IDS],
    );
    this.cityBounds = districtsBounds(opts.geo);
    this.cityCenter = [
      (this.cityBounds[0][0] + this.cityBounds[1][0]) / 2,
      (this.cityBounds[0][1] + this.cityBounds[1][1]) / 2,
    ];
    this.recolorAll();
    this.agentDataHandle = { length: opts.agents.length };

    this.map = new maplibregl.Map({
      container: opts.container,
      style: DARK_STYLE,
      center: BARCELONA_CENTER,
      zoom: 12.3,
      pitch: opts.pitch ?? 0,
      bearing: 0,
      interactive: opts.interactive ?? true,
      attributionControl: false,
    });

    this.overlay = new MapboxOverlay({ interleaved: false, layers: [] });
    this.map.addControl(this.overlay as unknown as maplibregl.IControl);

    // Fit the camera to the real bounds of all district polygons (padded) instead of a
    // hand-picked center/zoom, so the northern district (Nou Barris) isn't cropped off-screen —
    // both split maps use the same geo, so they end up framed identically ("synced").
    const bounds = this.cityBounds;
    this.map.fitBounds(bounds, { padding: 48, duration: 0 });
    this.map.on("load", () => {
      this.map.fitBounds(bounds, { padding: 48, duration: 0 });
      this.render();
    });
    this.startAnimationLoop();
  }

  setColorMode(mode: ColorMode): void {
    this.colorMode = mode;
    this.recolorAll();
    this.render();
  }

  setFillMetric(metric: FillMetric): void {
    this.fillMetric = metric;
    this.render();
  }

  /** Update district-level snapshot data used for the choropleth fill + tint domain. */
  setDistrictSnapshots(snapshots: DistrictSnapshot[]): void {
    this.districtSnapshots = new Map(snapshots.map((s) => [s.id, s]));
    const values = snapshots.map((s) => this.metricValue(s));
    this.fillDomain = [Math.min(...values, 0), Math.max(...values, 1)];
    this.render();
  }

  private metricValue(s: DistrictSnapshot): number {
    if (this.fillMetric === "avg_rent") return s.avg_rent;
    if (this.fillMetric === "unemployment_rate") return s.unemployment_rate;
    if (this.fillMetric === "avg_satisfaction") return s.avg_satisfaction;
    if (this.fillMetric === "tourist_units") return s.tourist_units ?? 0;
    if (this.fillMetric === "shops_open") return s.shops_open ?? 0;
    return s.mode_share?.metro ?? 0; // metro_share
  }

  /** Apply the current per-agent state (home district, employed, satisfaction, active) for one
   * frame. `active` follows frameAt's convention: 1 = present in the city at this tick. Agents
   * with an in-flight arrival/departure animation keep animating (handled in
   * updateRenderPositions) even though their base alpha already reflects the new state. */
  setAgentState(home: Uint8Array, employed: Uint8Array, satisfaction: Float32Array, active?: Uint8Array): void {
    for (let i = 0; i < this.agents.length; i++) {
      const did = DISTRICT_IDS[home[i]!]!;
      const arr = this.allDistrictPositions.get(did);
      if (arr) {
        this.homePositions[i * 2] = arr[i * 2]!;
        this.homePositions[i * 2 + 1] = arr[i * 2 + 1]!;
      }
      this.setColorFor(i, did, employed[i] === 1, satisfaction[i]!, this.agents[i]!.commute_mode ?? null);
      this.baseAlpha[i] = !active || active[i] === 1 ? 235 : 0;
    }
  }

  /** Trigger the arc-and-glide animation for one agent moving src -> dst district. */
  animateMove(agentId: number, dst: DistrictId, durationMs = 1500): void {
    const idx = this.agentIdToIndex.get(agentId);
    if (idx === undefined) return;
    const srcPos: [number, number] = [this.renderPositions[idx * 2]!, this.renderPositions[idx * 2 + 1]!];
    const dstArr = this.allDistrictPositions.get(dst);
    if (!dstArr) return;
    const dstPos: [number, number] = [dstArr[idx * 2]!, dstArr[idx * 2 + 1]!];
    this.activeMoves.set(agentId, {
      agentId,
      srcIdx: idx,
      dstIdx: idx,
      srcPos,
      dstPos,
      startedAtMs: performance.now(),
      durationMs,
    });
    this.homePositions[idx * 2] = dstPos[0];
    this.homePositions[idx * 2 + 1] = dstPos[1];
  }

  /** New household settling in the city: fades in at their home district position. */
  animateArrival(agentId: number, home: DistrictId, durationMs = 1200): void {
    const idx = this.agentIdToIndex.get(agentId);
    if (idx === undefined) return;
    const arr = this.allDistrictPositions.get(home);
    const pos: [number, number] = arr ? [arr[idx * 2]!, arr[idx * 2 + 1]!] : [this.homePositions[idx * 2]!, this.homePositions[idx * 2 + 1]!];
    this.homePositions[idx * 2] = pos[0];
    this.homePositions[idx * 2 + 1] = pos[1];
    this.renderPositions[idx * 2] = pos[0];
    this.renderPositions[idx * 2 + 1] = pos[1];
    this.fadeAnims.set(idx, { startedAtMs: performance.now(), durationMs, kind: "in", srcPos: pos, dstPos: pos });
  }

  /** Household leaving Barcelona: flies out from their current position toward the nearest city
   * boundary (away from the city center, past the district bounds) while fading out. */
  animateDeparture(agentId: number, durationMs = 1500): void {
    const idx = this.agentIdToIndex.get(agentId);
    if (idx === undefined) return;
    const srcPos: [number, number] = [this.renderPositions[idx * 2]!, this.renderPositions[idx * 2 + 1]!];
    const dx = srcPos[0] - this.cityCenter[0];
    const dy = srcPos[1] - this.cityCenter[1];
    const mag = Math.hypot(dx, dy) || 1e-6;
    const [w, h] = [
      this.cityBounds[1][0] - this.cityBounds[0][0],
      this.cityBounds[1][1] - this.cityBounds[0][1],
    ];
    const reach = Math.max(w, h) * 0.6; // well past the city bounds edge in that direction
    const dstPos: [number, number] = [srcPos[0] + (dx / mag) * reach, srcPos[1] + (dy / mag) * reach];
    this.fadeAnims.set(idx, { startedAtMs: performance.now(), durationMs, kind: "out", srcPos, dstPos });
  }

  setJitterEnabled(enabled: boolean): void {
    this.jitterEnabled = enabled;
  }

  private setColorFor(
    i: number,
    district: DistrictId,
    employed: boolean,
    satisfaction: number,
    commuteMode: string | null,
  ): void {
    let rgb: [number, number, number];
    if (this.colorMode === "district") rgb = DISTRICT_COLORS_RGB[district];
    else if (this.colorMode === "employed") rgb = employed ? EMPLOYED_COLOR : UNEMPLOYED_COLOR;
    else if (this.colorMode === "commute") {
      rgb = commuteMode ? (COMMUTE_MODE_COLORS[commuteMode] ?? COMMUTE_MODE_UNKNOWN_COLOR) : COMMUTE_MODE_UNKNOWN_COLOR;
    } else if (this.colorMode === "metro") {
      rgb = commuteMode === "metro" ? METRO_RIDER_COLOR : METRO_OTHER_COLOR;
    } else rgb = viridis(satisfaction);
    this.colors[i * 3] = rgb[0];
    this.colors[i * 3 + 1] = rgb[1];
    this.colors[i * 3 + 2] = rgb[2];
    // Only "metro" dims non-matching agents via alpha; every other mode draws everyone at full
    // strength (alpha handled separately by active/inactive + fade animations).
    this.alphaScale[i] = this.colorMode === "metro" && commuteMode !== "metro" ? 60 : 255;
  }

  private recolorAll(): void {
    for (let i = 0; i < this.agents.length; i++) {
      const a = this.agents[i]!;
      this.setColorFor(i, a.home, a.employed, a.satisfaction, a.commute_mode ?? null);
    }
  }

  private startAnimationLoop(): void {
    const tick = () => {
      this.updateRenderPositions();
      this.render();
      // Keep maplibre's own repaint loop alive alongside deck.gl's. maplibre only schedules a
      // repaint (via triggerRepaint -> browser.frame) while something is "dirty"; once the
      // style/tiles finish loading and it goes idle it stops asking for frames on its own. With
      // two Map instances sharing the global worker pool/dispatcher (base + compare slots), that
      // idle transition can land in a state where the basemap tiles are fully loaded (confirmed
      // via `sourcedata`/`isSourceLoaded`) but the canvas never gets the one extra paint that
      // would actually draw them — the map silently stays on its pre-tile (black) frame forever,
      // while our own deck.gl overlay (dots + district outlines) keeps rendering on top just
      // fine since it repaints unconditionally every tick. Piggybacking a `triggerRepaint()` on
      // our own already-continuous rAF loop guarantees maplibre gets a fresh paint every frame
      // too, so it can never get stuck "idle" before it has actually drawn the loaded tiles.
      this.map.triggerRepaint();
      this.rafHandle = requestAnimationFrame(tick);
    };
    this.rafHandle = requestAnimationFrame(tick);
  }

  private updateRenderPositions(): void {
    const now = performance.now();
    const n = this.agents.length;
    for (let i = 0; i < n; i++) {
      this.renderPositions[i * 2] = this.homePositions[i * 2]!;
      this.renderPositions[i * 2 + 1] = this.homePositions[i * 2 + 1]!;
    }
    if (this.jitterEnabled) {
      const t = now / 1000;
      for (let i = 0; i < n; i++) {
        const phase = (this.agents[i]!.id * 12.9898) % (Math.PI * 2);
        const amp = 0.00025; // ~25m breathing radius
        this.renderPositions[i * 2] += Math.sin(t * 0.6 + phase) * amp;
        this.renderPositions[i * 2 + 1] += Math.cos(t * 0.5 + phase * 1.3) * amp * 0.7;
      }
    }
    if (this.activeMoves.size > 0) {
      for (const [agentId, move] of this.activeMoves) {
        const progress = Math.min(1, (now - move.startedAtMs) / move.durationMs);
        const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
        const idx = this.agentIdToIndex.get(agentId)!;
        // slight arc lift via a lateral bow on the midpoint (visual "hop" between districts)
        const bow = Math.sin(eased * Math.PI) * 0.0025;
        const dx = move.dstPos[0] - move.srcPos[0];
        const dy = move.dstPos[1] - move.srcPos[1];
        const lon = move.srcPos[0] + dx * eased - dy * bow;
        const lat = move.srcPos[1] + dy * eased + dx * bow;
        this.renderPositions[idx * 2] = lon;
        this.renderPositions[idx * 2 + 1] = lat;
        if (progress >= 1) this.activeMoves.delete(agentId);
      }
    }

    // Alpha defaults to the active/inactive base every frame, then arrival/departure fades
    // (and, for departures, a flight toward the city edge) override it while in flight.
    this.renderAlpha.set(this.baseAlpha);
    if (this.fadeAnims.size > 0) {
      for (const [idx, anim] of this.fadeAnims) {
        const progress = Math.min(1, (now - anim.startedAtMs) / anim.durationMs);
        if (anim.kind === "in") {
          const eased = 1 - Math.pow(1 - progress, 2);
          this.renderAlpha[idx] = Math.round(235 * eased);
        } else {
          const eased = progress * progress; // ease-in: lingers visible briefly, then accelerates out
          const lon = anim.srcPos[0] + (anim.dstPos[0] - anim.srcPos[0]) * eased;
          const lat = anim.srcPos[1] + (anim.dstPos[1] - anim.srcPos[1]) * eased;
          this.renderPositions[idx * 2] = lon;
          this.renderPositions[idx * 2 + 1] = lat;
          this.renderAlpha[idx] = Math.round(235 * (1 - progress));
        }
        if (progress >= 1) this.fadeAnims.delete(idx);
      }
    }
  }

  private buildAgentLayer(): ScatterplotLayer {
    this.frameCounter++;
    return new ScatterplotLayer({
      id: "agents",
      data: this.agentDataHandle,
      getPosition: (_: unknown, { index }: { index: number }) => [
        this.renderPositions[index * 2]!,
        this.renderPositions[index * 2 + 1]!,
      ],
      getFillColor: (_: unknown, { index }: { index: number }) => [
        this.colors[index * 3]!,
        this.colors[index * 3 + 1]!,
        this.colors[index * 3 + 2]!,
        Math.round((this.renderAlpha[index]! * this.alphaScale[index]!) / 255),
      ],
      // Larger, brighter dots so they read clearly over the (now much subtler) choropleth fill.
      getRadius: 12,
      radiusUnits: "meters",
      radiusMinPixels: 2.5,
      radiusMaxPixels: 8,
      stroked: false,
      pickable: false,
      // Additive glow: SRC_ALPHA/ONE blending makes overlapping dots bloom brighter instead of
      // muddying into a flat disc, which keeps dense districts legible against the dark basemap.
      parameters: {
        blend: true,
        blendFunc: [0x0302 /* SRC_ALPHA */, 1 /* ONE */],
        depthTest: false,
      },
      updateTriggers: {
        getPosition: this.frameCounter,
        getFillColor: this.frameCounter,
      },
    });
  }

  private buildArcLayer(): ArcLayer | null {
    if (this.activeMoves.size === 0) return null;
    const moves = [...this.activeMoves.values()];
    return new ArcLayer({
      id: "move-arcs",
      data: moves,
      getSourcePosition: (d: MoveAnim) => d.srcPos,
      getTargetPosition: (d: MoveAnim) => d.dstPos,
      // Bright white-hot origin fading into the destination district's color — a clearly visible
      // "trail" for ~1.5s (see animateMove's durationMs) rather than a faint hairline.
      getSourceColor: [255, 255, 255, 160],
      getTargetColor: (d: MoveAnim) => {
        const idx = this.agentIdToIndex.get(d.agentId)!;
        return [this.colors[idx * 3]!, this.colors[idx * 3 + 1]!, this.colors[idx * 3 + 2]!, 255];
      },
      getWidth: 2.5,
      getHeight: 0.6,
      greatCircle: false,
      parameters: { depthTest: false },
      updateTriggers: { getSourcePosition: this.frameCounter, getTargetPosition: this.frameCounter },
    });
  }

  private buildDistrictLayer(): GeoJsonLayer {
    return new GeoJsonLayer({
      id: "districts",
      data: this.geo.featureCollection,
      filled: true,
      stroked: true,
      // Subtle fill (~10-15% alpha) + a thin tinted outline, so the choropleth reads as
      // ambient color rather than competing with the agent dots for attention.
      getFillColor: (f: GeoJSON.Feature) => {
        const id = (f.properties as Record<string, unknown>)?.id as DistrictId;
        const snap = this.districtSnapshots.get(id);
        if (!snap) return [255, 255, 255, 8];
        const [lo, hi] = this.fillDomain;
        const v = hi > lo ? (this.metricValue(snap) - lo) / (hi - lo) : 0.5;
        // metro_share gets its own higher-alpha single-hue blue ramp so the "new line opens"
        // story reads clearly on video, instead of the generally-subtle viridis fill (~13% alpha)
        // used for every other metric.
        if (this.fillMetric === "metro_share") {
          const [r, g, b] = metroBlueRamp(v);
          return [r, g, b, 130];
        }
        const [r, g, b] = viridis(v);
        return [r, g, b, 34];
      },
      getLineColor: (f: GeoJSON.Feature) => {
        const id = (f.properties as Record<string, unknown>)?.id as DistrictId;
        const snap = this.districtSnapshots.get(id);
        if (!snap) return [255, 255, 255, 70];
        const [lo, hi] = this.fillDomain;
        const v = hi > lo ? (this.metricValue(snap) - lo) / (hi - lo) : 0.5;
        if (this.fillMetric === "metro_share") {
          const [r, g, b] = metroBlueRamp(v);
          return [r, g, b, 220];
        }
        const [r, g, b] = viridis(v);
        return [r, g, b, 190];
      },
      lineWidthMinPixels: 1.25,
      pickable: false,
      updateTriggers: {
        getFillColor: [this.fillMetric, this.districtSnapshots.size, this.frameCounter],
        getLineColor: [this.fillMetric, this.districtSnapshots.size, this.frameCounter],
      },
    });
  }

  private render(): void {
    const layers = [this.buildDistrictLayer(), this.buildArcLayer(), this.buildAgentLayer()].filter(
      (l): l is GeoJsonLayer | ArcLayer | ScatterplotLayer => l !== null,
    );
    this.overlay.setProps({ layers });
  }

  destroy(): void {
    cancelAnimationFrame(this.rafHandle);
    this.map.remove();
  }
}
