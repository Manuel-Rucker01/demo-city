/** Dark deck.gl + maplibre map: district choropleth + 1,000-10,000 breathing/moving agent dots. */

import * as maplibregl from "maplibre-gl";
import type { Map as MaplibreMap } from "maplibre-gl";
import { MapboxOverlay } from "@deck.gl/mapbox";
import { GeoJsonLayer, ScatterplotLayer, ArcLayer } from "@deck.gl/layers";
import type { AgentSnapshot, DistrictId, DistrictSnapshot } from "../data/types";
import { DISTRICT_IDS } from "../data/types";
import type { DistrictsGeo } from "./districts";
import { precomputeAgentPositions, precomputeAllDistrictPositions } from "./districts";
import { DISTRICT_COLORS_RGB, EMPLOYED_COLOR, UNEMPLOYED_COLOR, viridis } from "../style/theme";

export type ColorMode = "district" | "employed" | "satisfaction";
export type FillMetric = "avg_rent" | "unemployment_rate" | "avg_satisfaction";

const BARCELONA_CENTER: [number, number] = [2.17, 41.4];
const DARK_STYLE = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";

interface MoveAnim {
  agentId: number;
  srcIdx: number;
  dstIdx: number;
  srcPos: [number, number];
  dstPos: [number, number];
  startedAtMs: number;
  durationMs: number;
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
  private allDistrictPositions: Map<DistrictId, Float64Array>;

  private colorMode: ColorMode = "district";
  private fillMetric: FillMetric = "avg_rent";
  private districtSnapshots: Map<DistrictId, DistrictSnapshot> = new Map();
  private fillDomain: [number, number] = [0, 1];

  private activeMoves: Map<number, MoveAnim> = new Map();
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
    this.allDistrictPositions = precomputeAllDistrictPositions(
      opts.agents.map((a) => a.id),
      opts.geo,
      [...DISTRICT_IDS],
    );
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

    this.map.on("load", () => this.render());
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
    return s.avg_satisfaction;
  }

  /** Apply the current per-agent state (home district, employed, satisfaction) for one frame. */
  setAgentState(home: Uint8Array, employed: Uint8Array, satisfaction: Float32Array): void {
    for (let i = 0; i < this.agents.length; i++) {
      const did = DISTRICT_IDS[home[i]!]!;
      const arr = this.allDistrictPositions.get(did);
      if (arr) {
        this.homePositions[i * 2] = arr[i * 2]!;
        this.homePositions[i * 2 + 1] = arr[i * 2 + 1]!;
      }
      this.setColorFor(i, did, employed[i] === 1, satisfaction[i]!);
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

  setJitterEnabled(enabled: boolean): void {
    this.jitterEnabled = enabled;
  }

  private setColorFor(i: number, district: DistrictId, employed: boolean, satisfaction: number): void {
    let rgb: [number, number, number];
    if (this.colorMode === "district") rgb = DISTRICT_COLORS_RGB[district];
    else if (this.colorMode === "employed") rgb = employed ? EMPLOYED_COLOR : UNEMPLOYED_COLOR;
    else rgb = viridis(satisfaction);
    this.colors[i * 3] = rgb[0];
    this.colors[i * 3 + 1] = rgb[1];
    this.colors[i * 3 + 2] = rgb[2];
  }

  private recolorAll(): void {
    for (let i = 0; i < this.agents.length; i++) {
      this.setColorFor(i, this.agents[i]!.home, this.agents[i]!.employed, this.agents[i]!.satisfaction);
    }
  }

  private startAnimationLoop(): void {
    const tick = () => {
      this.updateRenderPositions();
      this.render();
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
        220,
      ],
      getRadius: 12,
      radiusUnits: "meters",
      radiusMinPixels: 1.4,
      radiusMaxPixels: 6,
      stroked: false,
      pickable: false,
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
      getSourceColor: [255, 255, 255, 40],
      getTargetColor: (d: MoveAnim) => {
        const idx = this.agentIdToIndex.get(d.agentId)!;
        return [this.colors[idx * 3]!, this.colors[idx * 3 + 1]!, this.colors[idx * 3 + 2]!, 180];
      },
      getWidth: 1.5,
      greatCircle: false,
      updateTriggers: { getSourcePosition: this.frameCounter, getTargetPosition: this.frameCounter },
    });
  }

  private buildDistrictLayer(): GeoJsonLayer {
    return new GeoJsonLayer({
      id: "districts",
      data: this.geo.featureCollection,
      filled: true,
      stroked: true,
      getFillColor: (f: GeoJSON.Feature) => {
        const id = (f.properties as Record<string, unknown>)?.id as DistrictId;
        const snap = this.districtSnapshots.get(id);
        if (!snap) return [255, 255, 255, 10];
        const [lo, hi] = this.fillDomain;
        const v = hi > lo ? (this.metricValue(snap) - lo) / (hi - lo) : 0.5;
        const [r, g, b] = viridis(v);
        return [r, g, b, 60];
      },
      getLineColor: [255, 255, 255, 90],
      lineWidthMinPixels: 1.5,
      pickable: false,
      updateTriggers: { getFillColor: [this.fillMetric, this.districtSnapshots.size, this.frameCounter] },
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
