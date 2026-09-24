/** Builds short "policy starts/ends here" markLine annotations from a scenario's policy list,
 * shared by every time-series chart panel (line / stacked-area / bar). */

import type { EChartsOption, SeriesOption } from "echarts";
import type { Policy } from "../data/types";
import { ACCENT } from "../style/theme";

export interface PolicyMark {
  /** 0-based x-axis category index (tick index into the ticks/dates array). */
  tickIndex: number;
  label: string;
}

/** One markLine point + label per policy start (and end, for the tourist-flat ban). */
export function policyMarks(policies: Policy[] | undefined | null): PolicyMark[] {
  if (!policies || policies.length === 0) return [];
  const marks: PolicyMark[] = [];
  for (const p of policies) {
    const at = (tick: number) => Math.max(0, tick - 1);
    switch (p.type) {
      case "rent_cap":
        marks.push({ tickIndex: at(p.start_tick), label: `Rent cap · ${p.district}` });
        break;
      case "tourist_flat_ban":
        marks.push({ tickIndex: at(p.start_tick), label: "HUT ban starts" });
        marks.push({ tickIndex: at(p.end_tick), label: "HUT ban ends" });
        break;
      case "new_transit_line":
        marks.push({ tickIndex: at(p.start_tick), label: "New line opens" });
        break;
      case "low_emission_zone":
        marks.push({ tickIndex: at(p.start_tick), label: "LEZ starts" });
        break;
    }
  }
  return marks;
}

/** A silent, non-interactive echarts line series carrying only a markLine — the standard way
 * this app draws static vertical annotations (see the original cap-starts marker this replaces). */
export function policyMarkLineSeries(marks: PolicyMark[], color: string = ACCENT): SeriesOption {
  return {
    id: "policy-annotations",
    type: "line",
    data: [],
    markLine: {
      symbol: "none",
      silent: true,
      animation: false,
      lineStyle: { color, width: 1.25, type: "dashed" },
      label: { show: true, formatter: "{b}", color, fontSize: 10, position: "insideEndTop" },
      data: marks.map((m) => ({ xAxis: m.tickIndex, name: m.label })),
    },
  };
}

/** Convenience: apply the markLine series to a chart via setOption. */
export function applyPolicyMarks(
  setOption: (opt: EChartsOption) => void,
  marks: PolicyMark[],
  color?: string,
): void {
  setOption({ series: [policyMarkLineSeries(marks, color)] });
}
