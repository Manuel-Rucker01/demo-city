import { describe, expect, it } from "vitest";
import { monthlyMigration } from "./migration";
import type { AgentSnapshot, TickRecord } from "./types";

function usageStub() {
  return { requests: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0, estimated: true, retries: 0, errors: 0, cache_hits: 0 };
}

function arrival(id: number): AgentSnapshot {
  return {
    id,
    age: 28,
    occupation: "mid_skill",
    home: "gracia",
    employed: true,
    job_district: "gracia",
    wage_monthly: 2000,
    rent_monthly: 1100,
    satisfaction: 0.6,
  };
}

function tick(date: string, arrivals: AgentSnapshot[], departures: number[]): TickRecord {
  return {
    tick: 0,
    date,
    districts: [],
    events_by_kind: {},
    actions_by_kind: {},
    gated_decisions: 0,
    moves: [],
    changes: [],
    usage_tick: usageStub(),
    usage_total: usageStub(),
    arrivals,
    departures,
  };
}

describe("monthlyMigration", () => {
  it("groups arrivals and departures by calendar month, sorted", () => {
    const ticks = [
      tick("2026-01-05", [arrival(100)], []),
      tick("2026-01-20", [arrival(101), arrival(102)], [7]),
      tick("2026-02-02", [], [8, 9]),
    ];
    const out = monthlyMigration(ticks);
    expect(out).toEqual([
      { month: "2026-01", arrivals: 3, departures: 1 },
      { month: "2026-02", arrivals: 0, departures: 2 },
    ]);
  });

  it("treats missing arrivals/departures fields as zero (older runs)", () => {
    const ticks: TickRecord[] = [
      {
        tick: 1,
        date: "2026-03-01",
        districts: [],
        events_by_kind: {},
        actions_by_kind: {},
        gated_decisions: 0,
        moves: [],
        changes: [],
        usage_tick: usageStub(),
        usage_total: usageStub(),
      },
    ];
    expect(monthlyMigration(ticks)).toEqual([{ month: "2026-03", arrivals: 0, departures: 0 }]);
  });

  it("returns an empty list for no ticks", () => {
    expect(monthlyMigration([])).toEqual([]);
  });
});
