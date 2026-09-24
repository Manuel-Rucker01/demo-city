/** KPI tile row: day/date, population per district, moves today, Jev calls, tokens, cost, satisfaction. */

import type { TickRecord, RunMeta } from "../data/types";
import { formatDateHuman, formatInt, formatPercent, formatTokensCompact, formatUsdCost } from "../data/format";

export interface KpiInputs {
  meta: RunMeta;
  tick: TickRecord | null;
  tickIndex: number;
  totalTicks: number;
}

const TILE_DEFS: { key: string; label: string }[] = [
  { key: "day", label: "Day" },
  { key: "population", label: "Population" },
  { key: "moves", label: "Moves today" },
  { key: "calls", label: "Jev calls" },
  { key: "tokens", label: "Tokens" },
  { key: "cost", label: "Cost" },
  { key: "satisfaction", label: "Avg satisfaction" },
];

export class KpiTiles {
  private els = new Map<string, HTMLElement>();

  constructor(container: HTMLElement) {
    container.classList.add("kpi-row");
    for (const def of TILE_DEFS) {
      const tile = document.createElement("div");
      tile.className = "kpi-tile";
      tile.innerHTML = `<div class="kpi-label">${def.label}</div><div class="kpi-value" data-key="${def.key}">—</div>`;
      container.appendChild(tile);
      this.els.set(def.key, tile.querySelector(".kpi-value") as HTMLElement);
    }
  }

  update(inputs: KpiInputs): void {
    const { tick, tickIndex, totalTicks } = inputs;
    const set = (key: string, val: string) => {
      const el = this.els.get(key);
      if (el) el.textContent = val;
    };

    set("day", `${tickIndex} / ${totalTicks}${tick ? ` · ${formatDateHuman(tick.date)}` : ""}`);

    if (!tick) {
      set("population", "—");
      set("moves", "—");
      set("calls", "—");
      set("tokens", "—");
      set("cost", "—");
      set("satisfaction", "—");
      return;
    }

    const totalPop = tick.districts.reduce((s, d) => s + d.residents, 0);
    set("population", formatInt(totalPop));
    set("moves", formatInt(tick.moves.length));
    set("calls", formatInt(tick.usage_total.requests));
    set("tokens", formatTokensCompact(tick.usage_total.input_tokens + tick.usage_total.output_tokens));
    set("cost", formatUsdCost(tick.usage_total.cost_usd, tick.usage_total.estimated));
    const avgSat = tick.districts.reduce((s, d) => s + d.avg_satisfaction, 0) / (tick.districts.length || 1);
    set("satisfaction", formatPercent(avgSat));
  }
}
