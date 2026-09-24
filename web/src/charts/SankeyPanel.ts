/** Cumulative district-to-district move flows, rendered as an ECharts Sankey. */

import * as echarts from "echarts";
import { CHART_THEME_NAME } from "./theme";
import type { SankeyLink } from "../data/sankey";
import { DISTRICT_IDS } from "../data/types";
import { DISTRICT_COLORS } from "../style/theme";
import { districtDisplayName } from "../data/format";

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
    const data = links.map((l) => ({
      source: districtDisplayName(l.source),
      target: districtDisplayName(l.target),
      value: l.value,
    }));
    this.chart.setOption({ series: [{ links: data }] });
  }

  resize(): void {
    this.chart.resize();
  }

  dispose(): void {
    this.chart.dispose();
  }
}
