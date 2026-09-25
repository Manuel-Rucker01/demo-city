/** Record-mode-only on-map labels for the "new metro line" story: one label per policy district,
 * pinned (via maplibre's `project()`) to that district's centroid, showing the live metro mode
 * share for the current tick — big — with the day-1 baseline shown small underneath so the
 * before/after reads at a glance without needing the side panel. */

import type { Map as MaplibreMap } from "maplibre-gl";
import type { DistrictId } from "../data/types";
import { districtDisplayName } from "../data/format";

export interface DistrictLabelSpec {
  id: DistrictId;
  lon: number;
  lat: number;
  /** Extra screen-space offset (px) to keep adjacent district labels from overlapping — tuned by
   * eye for the three new_transit_line districts (nou_barris / sant_andreu / sants_montjuic),
   * but harmless (just 0,0) for any other district id. */
  offset?: [number, number];
}

const LABEL_OFFSETS: Partial<Record<DistrictId, [number, number]>> = {
  nou_barris: [-46, -10],
  sant_andreu: [52, -34],
  sants_montjuic: [0, 24],
};

export class DistrictMetroLabels {
  private map: MaplibreMap;
  private specs: DistrictLabelSpec[];
  private root: HTMLDivElement;
  private els: Map<DistrictId, HTMLDivElement> = new Map();
  private rafHandle = 0;

  constructor(container: HTMLElement, map: MaplibreMap, specs: DistrictLabelSpec[]) {
    this.map = map;
    this.specs = specs.map((s) => ({ ...s, offset: s.offset ?? LABEL_OFFSETS[s.id] ?? [0, 0] }));

    this.root = document.createElement("div");
    this.root.className = "metro-label-layer";
    container.appendChild(this.root);

    for (const s of this.specs) {
      const el = document.createElement("div");
      el.className = "metro-label";
      el.innerHTML = `<div class="metro-label-name">${districtDisplayName(s.id)}</div><div class="metro-label-value">—</div>`;
      this.root.appendChild(el);
      this.els.set(s.id, el);
    }

    this.reposition();
    const loop = (): void => {
      this.reposition();
      this.rafHandle = requestAnimationFrame(loop);
    };
    this.rafHandle = requestAnimationFrame(loop);
  }

  private reposition(): void {
    for (const s of this.specs) {
      const el = this.els.get(s.id);
      if (!el) continue;
      const p = this.map.project([s.lon, s.lat]);
      const [ox, oy] = s.offset ?? [0, 0];
      el.style.transform = `translate(${p.x + ox}px, ${p.y + oy}px) translate(-50%, -50%)`;
    }
  }

  /** `current`/`baseline` are fractional metro shares (0..1) keyed by district id. When
   * `showDelta` is false (the base map), only the current value is shown — the base run has no
   * policy, so a "from X%" delta would be a meaningless flat line. */
  update(current: Partial<Record<DistrictId, number>>, baseline: Partial<Record<DistrictId, number>>, showDelta: boolean): void {
    for (const s of this.specs) {
      const el = this.els.get(s.id);
      if (!el) continue;
      const cur = current[s.id];
      const curTxt = cur != null ? `${Math.round(cur * 100)}%` : "—";
      const base = baseline[s.id];
      const fromTxt = showDelta && base != null ? `<div class="metro-label-from">from ${Math.round(base * 100)}%</div>` : "";
      el.innerHTML = `<div class="metro-label-name">${districtDisplayName(s.id)}</div><div class="metro-label-value">${curTxt}</div>${fromTxt}`;
    }
  }

  destroy(): void {
    cancelAnimationFrame(this.rafHandle);
    this.root.remove();
  }
}
