/** Dark ECharts theme registered once at startup, matching the app's near-black palette. */

import * as echarts from "echarts";
import { BG_PANEL, BORDER, DISTRICT_COLORS, FG, FG_DIM } from "../style/theme";
import { DISTRICT_IDS } from "../data/types";

export const CHART_THEME_NAME = "jevcity-dark";

export function registerChartTheme(): void {
  echarts.registerTheme(CHART_THEME_NAME, {
    backgroundColor: "transparent",
    textStyle: { color: FG, fontFamily: "Inter, system-ui, sans-serif" },
    title: { textStyle: { color: FG }, subtextStyle: { color: FG_DIM } },
    color: DISTRICT_IDS.map((d) => DISTRICT_COLORS[d]),
    legend: { textStyle: { color: FG_DIM } },
    grid: { borderColor: BORDER },
    categoryAxis: {
      axisLine: { lineStyle: { color: BORDER } },
      axisLabel: { color: FG_DIM },
      splitLine: { show: false },
    },
    valueAxis: {
      axisLine: { show: false },
      axisLabel: { color: FG_DIM },
      splitLine: { lineStyle: { color: BORDER, type: "dashed" } },
    },
    tooltip: {
      backgroundColor: BG_PANEL,
      borderColor: BORDER,
      textStyle: { color: FG },
    },
  });
}
