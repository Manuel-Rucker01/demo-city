/** Arrivals vs departures per month, as a grouped bar chart. */

import * as echarts from "echarts";
import { CHART_THEME_NAME } from "./theme";
import type { TickRecord } from "../data/types";
import { monthlyMigration } from "../data/migration";

const ARRIVALS_COLOR = "#38d9a9";
const DEPARTURES_COLOR = "#ff6b6b";

export class BarPanel {
  private chart: echarts.ECharts;
  private tooltipsEnabled: boolean;

  constructor(container: HTMLElement, opts: { tooltipsEnabled?: boolean } = {}) {
    this.tooltipsEnabled = opts.tooltipsEnabled ?? true;
    this.chart = echarts.init(container, CHART_THEME_NAME, { renderer: "canvas" });
    this.chart.setOption({
      title: { text: "Arrivals vs departures / month", left: 8, top: 4, textStyle: { fontSize: 13, fontWeight: 600 } },
      grid: { left: 40, right: 16, top: 40, bottom: 40 },
      xAxis: { type: "category", data: [] },
      yAxis: { type: "value", splitNumber: 4 },
      legend: { bottom: 0, left: 8, right: 8, itemWidth: 12, itemHeight: 8, textStyle: { fontSize: 10.5 } },
      tooltip: this.tooltipsEnabled ? { trigger: "axis" } : { show: false },
      axisPointer: this.tooltipsEnabled ? undefined : { show: false },
      animation: false,
      series: [
        { id: "arrivals", name: "Arrivals", type: "bar", itemStyle: { color: ARRIVALS_COLOR }, data: [] as number[] },
        { id: "departures", name: "Departures", type: "bar", itemStyle: { color: DEPARTURES_COLOR }, data: [] as number[] },
      ],
    });
  }

  setData(ticks: TickRecord[]): void {
    const months = monthlyMigration(ticks);
    this.chart.setOption({
      xAxis: { data: months.map((m) => m.month) },
      series: [
        { id: "arrivals", data: months.map((m) => m.arrivals) },
        { id: "departures", data: months.map((m) => -m.departures) },
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
