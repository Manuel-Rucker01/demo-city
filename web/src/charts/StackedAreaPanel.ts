/** City-wide commute mode share, stacked area over time (metro/bus/car/bike/walk). Aggregates
 * each tick's per-district `mode_share` (a share among that district's commuters) weighted by
 * residents into one city-wide share per mode, so districts with more commuters count more. */

import * as echarts from "echarts";
import { CHART_THEME_NAME } from "./theme";
import type { CommuteMode, Policy, TickRecord } from "../data/types";
import { COMMUTE_MODE_COLORS } from "../style/theme";
import { commuteModeLabel } from "../data/format";
import { policyMarkLineSeries, policyMarks } from "./policyAnnotations";

const MODES: CommuteMode[] = ["metro", "bus", "car", "bike", "walk"];

/** Weighted (by residents) city-wide share per commute mode for one tick. Returns null entries
 * (rather than 0) when a tick has no mode_share data at all, so older/partial runs render a gap
 * instead of a misleading flat 0% line. */
export function cityModeShareSeries(ticks: TickRecord[]): Record<CommuteMode, (number | null)[]> {
  const out: Record<CommuteMode, (number | null)[]> = { metro: [], bus: [], car: [], bike: [], walk: [] };
  for (const t of ticks) {
    let totalWeight = 0;
    const sums: Record<CommuteMode, number> = { metro: 0, bus: 0, car: 0, bike: 0, walk: 0 };
    let any = false;
    for (const d of t.districts) {
      if (!d.mode_share) continue;
      any = true;
      const weight = d.residents || 0;
      totalWeight += weight;
      for (const m of MODES) {
        sums[m] += (d.mode_share[m] ?? 0) * weight;
      }
    }
    for (const m of MODES) {
      out[m].push(any && totalWeight > 0 ? sums[m] / totalWeight : any ? 0 : null);
    }
  }
  return out;
}

export class StackedAreaPanel {
  private chart: echarts.ECharts;
  private tooltipsEnabled: boolean;

  constructor(container: HTMLElement, opts: { tooltipsEnabled?: boolean } = {}) {
    this.tooltipsEnabled = opts.tooltipsEnabled ?? true;
    this.chart = echarts.init(container, CHART_THEME_NAME, { renderer: "canvas" });
    const option: echarts.EChartsOption = {
      title: { text: "Commute mode share (city-wide)", left: 8, top: 4, textStyle: { fontSize: 13, fontWeight: 600 } },
      grid: { left: 44, right: 16, top: 40, bottom: 40 },
      xAxis: { type: "category", data: [] },
      yAxis: {
        type: "value",
        min: 0,
        max: 1,
        splitNumber: 4,
        axisLabel: { formatter: (v: number) => `${Math.round(v * 100)}%` },
      },
      legend: { bottom: 0, left: 8, right: 8, itemWidth: 12, itemHeight: 8, textStyle: { fontSize: 10.5 }, type: "scroll" },
      tooltip: this.tooltipsEnabled
        ? { trigger: "axis", valueFormatter: (v) => `${((v as number) * 100).toFixed(0)}%` }
        : { show: false },
      axisPointer: this.tooltipsEnabled ? undefined : { show: false },
      animation: false,
      series: [],
    };
    this.chart.setOption(option);
  }

  setData(ticks: TickRecord[], policies: Policy[] = []): void {
    const dates = ticks.map((t) => t.date);
    const shares = cityModeShareSeries(ticks);
    const series: echarts.SeriesOption[] = MODES.map((m) => ({
      id: m,
      name: commuteModeLabel(m),
      type: "line",
      stack: "mode-share",
      areaStyle: { opacity: 0.55 },
      lineStyle: { width: 1, color: `rgb(${COMMUTE_MODE_COLORS[m]!.join(",")})` },
      itemStyle: { color: `rgb(${COMMUTE_MODE_COLORS[m]!.join(",")})` },
      showSymbol: false,
      data: shares[m],
    }));
    series.push(policyMarkLineSeries(policyMarks(policies)));
    this.chart.setOption({ xAxis: { data: dates }, series }, { replaceMerge: ["series"] });
  }

  setCursor(i: number): void {
    this.chart.setOption({
      series: [
        {
          id: "cursor",
          type: "line",
          data: [],
          markLine: {
            symbol: "none",
            silent: true,
            animation: false,
            lineStyle: { color: "#8b93a7", width: 1, type: "solid" },
            label: { show: false },
            data: [{ xAxis: i }],
          },
        },
      ],
    });
  }

  resize(): void {
    this.chart.resize();
  }

  dispose(): void {
    this.chart.dispose();
  }
}
