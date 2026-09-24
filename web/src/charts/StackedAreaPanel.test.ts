import { describe, expect, it } from "vitest";
import { cityModeShareSeries } from "./StackedAreaPanel";
import type { DistrictSnapshot, TickRecord } from "../data/types";

function usageStub() {
  return { requests: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0, estimated: true, retries: 0, errors: 0, cache_hits: 0 };
}

function district(id: string, residents: number, modeShare: Partial<Record<string, number>>): DistrictSnapshot {
  return {
    id,
    avg_rent: 1000,
    avg_paid_rent: 950,
    residents,
    vacancy_rate: 0.05,
    unemployment_rate: 0.1,
    jobs: 100,
    filled_jobs: 90,
    shop_revenue: 100,
    avg_satisfaction: 0.6,
    avg_rent_burden: 0.3,
    rent_cap_active: false,
    mode_share: modeShare,
  };
}

function tick(districts: DistrictSnapshot[]): TickRecord {
  return {
    tick: 1,
    date: "2026-01-01",
    districts,
    events_by_kind: {},
    actions_by_kind: {},
    gated_decisions: 0,
    moves: [],
    changes: [],
    usage_tick: usageStub(),
    usage_total: usageStub(),
  };
}

describe("cityModeShareSeries", () => {
  it("weights each district's mode share by its resident count", () => {
    const ticks = [
      tick([
        district("a", 100, { metro: 0.8, car: 0.2 }),
        district("b", 300, { metro: 0.2, car: 0.8 }),
      ]),
    ];
    const out = cityModeShareSeries(ticks);
    // city metro share = (100*0.8 + 300*0.2) / 400 = 0.35
    expect(out.metro[0]).toBeCloseTo(0.35, 5);
    expect(out.car[0]).toBeCloseTo(0.65, 5);
    expect(out.bus[0]).toBeCloseTo(0, 5);
  });

  it("sums to (approximately) 1 across modes when the district data does", () => {
    const ticks = [tick([district("a", 100, { metro: 0.4, bus: 0.1, car: 0.3, bike: 0.1, walk: 0.1 })])];
    const out = cityModeShareSeries(ticks);
    const total = ["metro", "bus", "car", "bike", "walk"].reduce((s, m) => s + (out[m as keyof typeof out][0] ?? 0), 0);
    expect(total).toBeCloseTo(1, 5);
  });

  it("returns null (not 0) for a tick with no mode_share data at all — an older run", () => {
    const ticks = [
      tick([
        {
          id: "a",
          avg_rent: 1000,
          avg_paid_rent: 950,
          residents: 100,
          vacancy_rate: 0.05,
          unemployment_rate: 0.1,
          jobs: 100,
          filled_jobs: 90,
          shop_revenue: 100,
          avg_satisfaction: 0.6,
          avg_rent_burden: 0.3,
          rent_cap_active: false,
        },
      ]),
    ];
    const out = cityModeShareSeries(ticks);
    expect(out.metro[0]).toBeNull();
  });
});
