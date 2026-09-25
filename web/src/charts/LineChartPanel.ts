/** A dark-themed ECharts line panel (avg rent, unemployment, etc.) with a synced time cursor.
 * In compare mode, the base run draws solid lines and the scenario run draws dashed lines,
 * with the capped district (if any) rendered thicker so the policy effect reads at a glance;
 * other districts are dimmed so the highlighted story stands out. */

import * as echarts from "echarts";
import { CHART_THEME_NAME } from "./theme";
import { DISTRICT_IDS, type DistrictId, type Policy, type TickRecord } from "../data/types";
import { DISTRICT_COLORS, FG_DIM } from "../style/theme";
import { districtDisplayName } from "../data/format";
import { policyMarkLineSeries, policyMarks } from "./policyAnnotations";

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
  /** Record mode: tooltips are fully disabled (no hover, no programmatic showTip) — this panel
   * is view-only on a recording, so a lingering tooltip box would just be visual clutter/bug. */
  private tooltipsEnabled: boolean;
  private policies: Policy[] = [];

  constructor(container: HTMLElement, title: string, valueFormatter: (v: number) => string, opts: { tooltipsEnabled?: boolean } = {}) {
    this.title = title;
    this.valueFormatter = valueFormatter;
    this.tooltipsEnabled = opts.tooltipsEnabled ?? true;
    this.chart = echarts.init(container, CHART_THEME_NAME, { renderer: "canvas" });
    this.chart.setOption(this.baseOption());
  }

  private baseOption(): echarts.EChartsOption {
    return {
      title: { text: this.title, left: 8, top: 4, textStyle: { fontSize: 13, fontWeight: 600 } },
      grid: { left: 52, right: 16, top: 40, bottom: 40 },
      xAxis: { type: "category", data: [] },
      yAxis: {
        type: "value",
        splitNumber: 4,
        axisLabel: { formatter: (v: number) => this.valueFormatter(v) },
      },
      legend: {
        bottom: 0,
        left: 8,
        right: 8,
        itemWidth: 12,
        itemHeight: 8,
        textStyle: { fontSize: 10.5 },
        type: "scroll",
      },
      // Tooltip only ever appears on real mouse hover ("axis" trigger, default triggerOn); it is
      // never opened programmatically — the playback time cursor below is a plain markLine, not
      // a tooltip. Fully off in record mode.
      tooltip: this.tooltipsEnabled
        ? { trigger: "axis", valueFormatter: (v) => this.valueFormatter(v as number) }
        : { show: false },
      axisPointer: this.tooltipsEnabled ? undefined : { show: false },
      animation: false,
      series: [],
    };
  }

  /** Render one or two (compare) run series, one line per district (or per `districts`, when
   * given — e.g. the metro-share story chart only wants the policy districts, not all ten).
   * `policies` (usually `meta.scenario.policies`) draws a short markLine annotation per policy
   * start/end. */
  setData(specs: LineSeriesSpec[], policies: Policy[] = [], districts: readonly DistrictId[] = DISTRICT_IDS): void {
    const dates = specs[0]?.ticks.map((t) => t.date) ?? [];
    const series: echarts.SeriesOption[] = [];
    const hasHighlight = specs.some((s) => s.highlightDistrict);
    this.policies = policies;

    for (const spec of specs) {
      for (const did of districts) {
        const data = spec.ticks.map((t) => {
          const d = t.districts.find((x) => x.id === did);
          return d ? spec.metric(d) : null;
        });
        const isHighlighted = spec.highlightDistrict === did;
        const dimmed = hasHighlight && !isHighlighted;
        series.push({
          id: `${spec.runLabel}-${did}`,
          name: `${districtDisplayName(did)}${specs.length > 1 ? ` (${spec.runLabel})` : ""}`,
          type: "line",
          showSymbol: false,
          data,
          lineStyle: {
            color: DISTRICT_COLORS[did],
            type: spec.runLabel === "scenario" ? "dashed" : "solid",
            width: isHighlighted ? 3.5 : dimmed ? 1.25 : 1.75,
            opacity: isHighlighted ? 1 : dimmed ? 0.3 : specs.length === 1 ? 1 : 0.85,
          },
          itemStyle: { color: DISTRICT_COLORS[did] },
          emphasis: { focus: "series" },
          z: isHighlighted ? 10 : 1,
        });
      }
    }
    this.chart.setOption({ xAxis: { data: dates }, series }, { replaceMerge: ["series"] });
    this.renderAnnotation();
  }

  /** Draws static policy-start/end vertical markers (independent of playback position — these
   * don't move as the time cursor scrubs). */
  private renderAnnotation(): void {
    const marks = policyMarks(this.policies);
    this.chart.setOption({ series: [policyMarkLineSeries(marks)] });
  }

  /** Move the vertical time cursor to tick index `i` (synced to playback). Never opens a
   * tooltip — only a silent markLine, so it can't get "stuck" over the panel during playback. */
  setCursor(i: number): void {
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
