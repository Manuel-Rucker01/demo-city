import { describe, expect, it } from "vitest";
import { agentStateAt, buildCombinedAgents, frameAt, naiveStateAtTick, reconstructRun } from "./reconstruct";
import type { AgentSnapshot, TickRecord } from "./types";

function usageStub() {
  return { requests: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0, estimated: true, retries: 0, errors: 0, cache_hits: 0 };
}

function arrivalSnapshot(id: number, home: string): AgentSnapshot {
  return {
    id,
    age: 29,
    occupation: "mid_skill",
    home,
    employed: true,
    job_district: home,
    wage_monthly: 2100,
    rent_monthly: 1100,
    satisfaction: 0.65,
  };
}

function tick(t: number, extra: Partial<TickRecord> = {}): TickRecord {
  return {
    tick: t,
    date: `2026-01-${String(t + 1).padStart(2, "0")}`,
    districts: [],
    events_by_kind: {},
    actions_by_kind: {},
    gated_decisions: 0,
    moves: [],
    changes: [],
    usage_tick: usageStub(),
    usage_total: usageStub(),
    ...extra,
  };
}

const AGENTS: AgentSnapshot[] = [
  { id: 0, age: 30, occupation: "mid_skill", home: "gracia", employed: true, job_district: "gracia", wage_monthly: 2000, rent_monthly: 1200, satisfaction: 0.5 },
  { id: 1, age: 40, occupation: "low_skill", home: "eixample", employed: false, job_district: null, wage_monthly: 1400, rent_monthly: 1300, satisfaction: 0.3 },
];

describe("reconstructRun with arrivals/departures", () => {
  it("buildCombinedAgents appends arrivals in tick order after the initial population", () => {
    const ticks = [
      tick(1, { arrivals: [arrivalSnapshot(100, "gracia")] }),
      tick(2),
      tick(3, { arrivals: [arrivalSnapshot(101, "sant_marti"), arrivalSnapshot(102, "eixample")] }),
    ];
    const combined = buildCombinedAgents(AGENTS, ticks);
    expect(combined.map((a) => a.id)).toEqual([0, 1, 100, 101, 102]);
  });

  it("an arrival is inactive before its arrival tick and active with correct fields from it on", () => {
    const ticks = [tick(1), tick(2, { arrivals: [arrivalSnapshot(100, "sant_marti")] }), tick(3)];
    const run = reconstructRun(AGENTS, ticks);

    expect(agentStateAt(run, 0, 100)!.active).toBe(false);
    expect(agentStateAt(run, 1, 100)!.active).toBe(false);
    const atArrival = agentStateAt(run, 2, 100)!;
    expect(atArrival.active).toBe(true);
    expect(atArrival.home).toBe("sant_marti");
    expect(atArrival.employed).toBe(true);
    expect(atArrival.satisfaction).toBeCloseTo(0.65, 5);
    expect(agentStateAt(run, 3, 100)!.active).toBe(true);
  });

  it("a departure is active up through the tick before it leaves, inactive from its departure tick on", () => {
    const ticks = [tick(1), tick(2, { departures: [1] }), tick(3)];
    const run = reconstructRun(AGENTS, ticks);

    expect(agentStateAt(run, 0, 1)!.active).toBe(true);
    expect(agentStateAt(run, 1, 1)!.active).toBe(true);
    expect(agentStateAt(run, 2, 1)!.active).toBe(false);
    expect(agentStateAt(run, 3, 1)!.active).toBe(false);
    // an initial-population agent never present in `agents` after departure still resolves —
    // its last-known home/employment/satisfaction are retained, just marked inactive.
    expect(agentStateAt(run, 3, 1)!.home).toBe("eixample");
  });

  it("frames stay O(agents): nAgents grows only by the number of distinct arrivals, not by ticks", () => {
    const ticks = [
      tick(1, { arrivals: [arrivalSnapshot(100, "gracia")] }),
      tick(2, { arrivals: [arrivalSnapshot(101, "gracia")] }),
      tick(3, { departures: [0] }),
    ];
    const run = reconstructRun(AGENTS, ticks);
    expect(run.nAgents).toBe(AGENTS.length + 2); // 2 initial + 2 arrivals, departure doesn't grow the array
    expect(run.homeByTick.length).toBe((ticks.length + 1) * run.nAgents);
  });

  it("matches a naive from-scratch replay at every tick for every agent (initial + arrivals)", () => {
    const ticks = [
      tick(1, { moves: [{ agent_id: 0, src: "gracia", dst: "eixample" }], arrivals: [arrivalSnapshot(100, "gracia")] }),
      tick(2, { changes: [{ agent_id: 100, employed: false, satisfaction: 0.2 }] }),
      tick(3, { departures: [1], arrivals: [arrivalSnapshot(101, "sant_marti")] }),
    ];
    const run = reconstructRun(AGENTS, ticks);
    const combined = buildCombinedAgents(AGENTS, ticks);
    for (let row = 0; row <= ticks.length; row++) {
      for (const a of combined) {
        const fast = agentStateAt(run, row, a.id)!;
        const naive = naiveStateAtTick(AGENTS, ticks, row, a.id);
        if (naive === null) {
          // Naive replay has no concept of "not arrived yet" (it only knows an agent once its
          // arrival snapshot has appeared) — reconstructRun pre-fills those columns instead, but
          // they must still read as inactive.
          expect(fast.active).toBe(false);
          continue;
        }
        expect(fast.home).toBe(naive.home);
        expect(fast.employed).toBe(naive.employed);
        expect(fast.active).toBe(naive.active);
        expect(fast.satisfaction).toBeCloseTo(naive.satisfaction, 5);
      }
    }
  });

  it("frameAt exposes an active row aligned with per-agent lookups", () => {
    const ticks = [tick(1, { arrivals: [arrivalSnapshot(100, "gracia")] })];
    const run = reconstructRun(AGENTS, ticks);
    const frame = frameAt(run, 1);
    for (let i = 0; i < run.nAgents; i++) {
      const agentId = run.agentIndex.indexToId[i]!;
      const single = agentStateAt(run, 1, agentId)!;
      expect(frame.active[i] === 1).toBe(single.active);
    }
  });
});
