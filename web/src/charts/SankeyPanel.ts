/** Cumulative district-to-district move flows, rendered as an ECharts Sankey. */

import * as echarts from "echarts";
import { CHART_THEME_NAME } from "./theme";
import type { SankeyLink } from "../data/sankey";
import { DISTRICT_IDS, type DistrictId } from "../data/types";
import { DISTRICT_COLORS } from "../style/theme";
import { districtDisplayName } from "../data/format";

/** Greedily builds the highest-weight subset of `links` that forms a strict DAG. `aggregateMoves`
 * legitimately keeps every observed direction (it's a general-purpose data aggregation, covered
 * by its own tests) — but ECharts' Sankey renderer requires a DAG and throws (asynchronously,
 * inside its own render scheduling, so it can't reliably be try/caught at the call site) on any
 * cycle. Over a full year with 5 districts, both simple A<->B reversals *and* longer A->B->C->A
 * cycles show up, so this can't just dedupe pairs — it processes links heaviest-first and skips
 * any link that would close a cycle, via a reachability check on the graph built so far. This
 * view-only simplification belongs here, not in the shared aggregation. */
function collapseToDag(links: SankeyLink[]): SankeyLink[] {
  const sorted = [...links].sort((a, b) => b.value - a.value);
  const adjacency = new Map<DistrictId, Set<DistrictId>>();
  const reachable = (from: DistrictId, to: DistrictId): boolean => {
    const seen = new Set<DistrictId>([from]);
    const stack = [from];
    while (stack.length > 0) {
      const cur = stack.pop()!;
      if (cur === to) return true;
      for (const next of adjacency.get(cur) ?? []) {
        if (!seen.has(next)) {
          seen.add(next);
          stack.push(next);
        }
      }
    }
    return false;
  };
  const kept: SankeyLink[] = [];
  for (const l of sorted) {
    if (l.source === l.target) continue;
    if (reachable(l.target, l.source)) continue; // would close a cycle back to source
    kept.push(l);
    if (!adjacency.has(l.source)) adjacency.set(l.source, new Set());
    adjacency.get(l.source)!.add(l.target);
  }
  return kept;
}

export class SankeyPanel {
  private chart: echarts.ECharts;

  constructor(container: HTMLElement) {
    this.chart = echarts.init(container, CHART_THEME_NAME, { renderer: "canvas" });
    this.chart.setOption({
      title: { text: "Cumulative moves", left: 8, top: 4, textStyle: { fontSize: 13, fontWeight: 600 } },
      tooltip: { trigger: "item" },
      animation: false,
      series: [
        {
          type: "sankey",
          top: 40,
          bottom: 12,
          left: 12,
          right: 100,
          data: DISTRICT_IDS.map((id) => ({ name: districtDisplayName(id), itemStyle: { color: DISTRICT_COLORS[id] } })),
          links: [] as { source: string; target: string; value: number }[],
          emphasis: { focus: "adjacency" },
          lineStyle: { color: "gradient", curveness: 0.5, opacity: 0.35 },
          label: { color: "#eef1f7" },
        },
      ],
    });
  }

  setLinks(links: SankeyLink[]): void {
    const data = collapseToDag(links).map((l) => ({
      source: districtDisplayName(l.source),
      target: districtDisplayName(l.target),
      value: l.value,
    }));
    try {
      this.chart.setOption({ series: [{ links: data }] });
    } catch (err) {
      // ECharts' Sankey renderer throws (rather than rejecting) on a cyclic flow graph — with 5
      // districts a 3+-cycle can still slip through the pairwise dedupe in aggregateMoves. This
      // must never propagate: an uncaught throw here previously froze the whole playback loop
      // (Playback.loop calls listeners synchronously before scheduling its next animation frame).
      console.warn("SankeyPanel: skipping an unrenderable (cyclic) flow graph", err);
    }
  }

  resize(): void {
    this.chart.resize();
  }

  dispose(): void {
    this.chart.dispose();
  }
}
