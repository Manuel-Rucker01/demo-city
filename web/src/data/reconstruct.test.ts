import { describe, expect, it } from "vitest";
import { agentStateAt, frameAt, naiveStateAtTick, reconstructRun } from "./reconstruct";
import { codeToDistrict } from "./reconstruct";
import type { AgentSnapshot, TickRecord } from "./types";

function districtSnapshotStub(id: string) {
  return {
    id,
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
  };
}

function usageStub() {
  return { requests: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0, estimated: true, retries: 0, errors: 0, cache_hits: 0 };
}

const AGENTS: AgentSnapshot[] = [
  { id: 0, age: 30, occupation: "mid_skill", home: "gracia", employed: true, job_district: "gracia", wage_monthly: 2000, rent_monthly: 1200, satisfaction: 0.5 },
  { id: 1, age: 40, occupation: "low_skill", home: "eixample", employed: false, job_district: null, wage_monthly: 1400, rent_monthly: 1300, satisfaction: 0.3 },
  { id: 2, age: 22, occupation: "student", home: "nou_barris", employed: false, job_district: null, wage_monthly: 500, rent_monthly: 800, satisfaction: 0.7 },
];

function tick(t: number, moves: TickRecord["moves"], changes: TickRecord["changes"]): TickRecord {
  return {
    tick: t,
    date: `2026-01-${String(t + 1).padStart(2, "0")}`,
    districts: ["ciutat_vella", "eixample", "gracia", "sant_marti", "nou_barris"].map(districtSnapshotStub),
    events_by_kind: {},
    actions_by_kind: {},
    gated_decisions: 0,
    moves,
    changes,
    usage_tick: usageStub(),
    usage_total: usageStub(),
  };
}

const TICKS: TickRecord[] = [
  tick(1, [{ agent_id: 0, src: "gracia", dst: "eixample" }], [{ agent_id: 1, employed: true, satisfaction: null }]),
  tick(2, [], [{ agent_id: 2, employed: null, satisfaction: 0.9 }]),
  tick(3, [{ agent_id: 1, src: "eixample", dst: "nou_barris" }], [{ agent_id: 0, employed: false, satisfaction: 0.1 }]),
];

describe("reconstructRun", () => {
  it("row 0 matches the initial population", () => {
    const run = reconstructRun(AGENTS, TICKS);
    for (const a of AGENTS) {
      const s = agentStateAt(run, 0, a.id)!;
      expect(s.home).toBe(a.home);
      expect(s.employed).toBe(a.employed);
      // satisfaction is stored as Float32 for O(1) scrubbing at scale, so compare with tolerance.
      expect(s.satisfaction).toBeCloseTo(a.satisfaction, 6);
    }
  });

  it("applies moves and changes cumulatively per row", () => {
    const run = reconstructRun(AGENTS, TICKS);
    expect(agentStateAt(run, 1, 0)!.home).toBe("eixample"); // moved tick 1
    expect(agentStateAt(run, 1, 1)!.employed).toBe(true); // changed tick 1
    expect(agentStateAt(run, 2, 2)!.satisfaction).toBeCloseTo(0.9);
    expect(agentStateAt(run, 3, 1)!.home).toBe("nou_barris"); // moved tick 3
    expect(agentStateAt(run, 3, 0)!.employed).toBe(false); // changed tick 3
    expect(agentStateAt(run, 3, 0)!.satisfaction).toBeCloseTo(0.1);
    // untouched-after-move fields persist forward
    expect(agentStateAt(run, 3, 0)!.home).toBe("eixample");
  });

  it("matches a naive from-scratch replay at every tick for every agent", () => {
    const run = reconstructRun(AGENTS, TICKS);
    for (let row = 0; row <= TICKS.length; row++) {
      for (const a of AGENTS) {
        const fast = agentStateAt(run, row, a.id)!;
        const naive = naiveStateAtTick(AGENTS, TICKS, row, a.id)!;
        expect(fast.home).toBe(naive.home);
        expect(fast.employed).toBe(naive.employed);
        // satisfaction is stored as Float32 for O(1) scrubbing at scale, so compare with tolerance.
        expect(fast.satisfaction).toBeCloseTo(naive.satisfaction, 6);
      }
    }
  });

  it("frameAt returns a contiguous slice equal to per-agent lookups", () => {
    const run = reconstructRun(AGENTS, TICKS);
    const frame = frameAt(run, 3);
    for (let i = 0; i < AGENTS.length; i++) {
      const agentId = run.agentIndex.indexToId[i]!;
      const single = agentStateAt(run, 3, agentId)!;
      expect(codeToDistrict(frame.home[i]!)).toBe(single.home);
      expect(frame.employed[i] === 1).toBe(single.employed);
      expect(frame.satisfaction[i]).toBeCloseTo(single.satisfaction);
    }
  });

  it("returns null for an unknown agent id", () => {
    const run = reconstructRun(AGENTS, TICKS);
    expect(agentStateAt(run, 0, 9999)).toBeNull();
  });
});
