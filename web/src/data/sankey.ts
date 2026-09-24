/** Aggregates move records into cumulative from -> to district flows, for a Sankey chart. */

import type { DistrictId, TickRecord } from "./types";

export interface SankeyLink {
  source: DistrictId;
  target: DistrictId;
  value: number;
}

/** Aggregate all moves across ticks[0..uptoIndex] (inclusive) into source->target flow counts. */
export function aggregateMoves(ticks: TickRecord[], uptoIndex: number): SankeyLink[] {
  const counts = new Map<string, number>();
  const end = Math.min(uptoIndex, ticks.length - 1);
  for (let t = 0; t <= end; t++) {
    const rec = ticks[t];
    if (!rec) continue;
    for (const mv of rec.moves) {
      if (mv.src === mv.dst) continue; // not a district-changing move
      const key = `${mv.src}\u0000${mv.dst}`;
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
  }
  const links: SankeyLink[] = [];
  for (const [key, value] of counts) {
    const [source, target] = key.split("\u0000") as [DistrictId, DistrictId];
    links.push({ source, target, value });
  }
  links.sort((a, b) => b.value - a.value);
  return links;
}
