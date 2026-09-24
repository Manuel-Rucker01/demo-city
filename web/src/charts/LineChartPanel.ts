/** A dark-themed ECharts line panel (avg rent, unemployment, etc.) with a synced time cursor.
 * In compare mode, the base run draws solid lines and the scenario run draws dashed lines,
 * with the capped district (if any) rendered thicker so the policy effect reads at a glance. */

import * as echarts from "echarts";
import { CHART_THEME_NAME } from "./theme";
import { DISTRICT_IDS, type DistrictId, type TickRecord } from "../data/types";
import { DISTRICT_COLORS, FG_DIM } from "../style/theme";
import { districtDisplayName } from "../data/format";

export interface LineSeriesSpec {
  runLabel: "base" | "scenario";
  ticks: TickRecord[];
  metric: (d: TickRecord["districts"][number]) => number;
  highlightDistrict?: DistrictId | null;
}

export class LineChartPanel {
  private chart: echarts.ECharts;
  private title: string;
  private valueFormatter: (v: number) => string;

  constructor(container: HTMLElement, title: string, valueFormatter: (v: number) => string) {
    this.title = title;
    this.valueFormatter = valueFormatter;
    this.chart = echarts.init(container, CHART_THEME_NAME, { renderer: "canvas" });
    this.chart.setOption(this.baseOption());
  }

  private baseOption(): echarts.EChartsOption {
    return {
      title: { text: this.title, left: 8, top: 4, textStyle: { fontSize: 13, fontWeight: 600 } },
      grid: { left: 48, right: 16, top: 40, bottom: 24 },
      xAxis: { type: "category", data: [] },
      yAxis: { type: "value", axisLabel: { formatter: (v: number) => this.valueFormatter(v) } },
      tooltip: { trigger: "axis", valueFormatter: (v) => this.valueFormatter(v as number) },
      animation: false,
      series: [],
    };
  }

  /** Render one or two (compare) run series, one line per district. */
  setData(specs: LineSeriesSpec[]): void {
    const dates = specs[0]?.ticks.map((t) => t.date) ?? [];
    const series: echarts.SeriesOption[] = [];
    for (const spec of specs) {
      for (const did of DISTRICT_IDS) {
        const data = spec.ticks.map((t) => {
          const d = t.districts.find((x) => x.id === did);
          return d ? spec.metric(d) : null;
        });
        const isHighlighted = spec.highlightDistrict === did;
        series.push({
          id: `${spec.runLabel}-${did}`,
          name: `${districtDisplayName(did)}${specs.length > 1 ? ` (${spec.runLabel})` : ""}`,
          type: "line",
          showSymbol: false,
          data,
          lineStyle: {
            color: DISTRICT_COLORS[did],
            type: spec.runLabel === "scenario" ? "dashed" : "solid",
            width: isHighlighted ? 3.5 : 1.75,
            opacity: isHighlighted || specs.length === 1 ? 1 : 0.85,
          },
          itemStyle: { color: DISTRICT_COLORS[did] },
          emphasis: { focus: "series" },
          z: isHighlighted ? 10 : 1,
        });
      }
    }
    this.chart.setOption({ xAxis: { data: dates }, series }, { replaceMerge: ["series"] });
  }

  /** Move the vertical time cursor to tick index `i` (synced to playback). */
  setCursor(i: number): void {
    this.chart.dispatchAction({ type: "showTip", seriesIndex: 0, dataIndex: i });
    this.chart.setOption({
      series: [
        {
          id: "cursor",
          type: "line",
          markLine: {
            symbol: "none",
            silent: true,
            animation: false,
            lineStyle: { color: FG_DIM, width: 1, type: "solid" },
            label: { show: false },
            data: [{ xAxis: i }],
          },
          data: [],
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
