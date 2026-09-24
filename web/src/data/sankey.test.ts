import { describe, expect, it } from "vitest";
import { aggregateMoves } from "./sankey";
import type { TickRecord } from "./types";

function tick(t: number, moves: TickRecord["moves"]): TickRecord {
  return {
    tick: t,
    date: "2026-01-01",
    districts: [],
    events_by_kind: {},
    actions_by_kind: {},
    gated_decisions: 0,
    moves,
    changes: [],
    usage_tick: { requests: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0, estimated: true, retries: 0, errors: 0, cache_hits: 0 },
    usage_total: { requests: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0, estimated: true, retries: 0, errors: 0, cache_hits: 0 },
  };
}

describe("aggregateMoves", () => {
  const ticks: TickRecord[] = [
    tick(1, [
      { agent_id: 1, src: "gracia", dst: "eixample" },
      { agent_id: 2, src: "gracia", dst: "eixample" },
    ]),
    tick(2, [{ agent_id: 3, src: "eixample", dst: "gracia" }]),
    tick(3, [
      { agent_id: 4, src: "gracia", dst: "eixample" },
      { agent_id: 5, src: "gracia", dst: "gracia" }, // not a real move, should be excluded
    ]),
  ];

  it("aggregates counts per (src, dst) pair up to the given tick index", () => {
    const links = aggregateMoves(ticks, 1); // include ticks[0..1]
    expect(links).toEqual([
      { source: "gracia", target: "eixample", value: 2 },
      { source: "eixample", target: "gracia", value: 1 },
    ]);
  });

  it("excludes same-district non-moves", () => {
    const links = aggregateMoves(ticks, 2);
    const total = links.reduce((s, l) => s + l.value, 0);
    expect(total).toBe(4); // 2 + 1 + 1, not 5
  });

  it("returns an empty array when uptoIndex is negative (no ticks yet)", () => {
    expect(aggregateMoves(ticks, -1)).toEqual([]);
  });

  it("is cumulative: later indices include earlier flows", () => {
    const at1 = aggregateMoves(ticks, 1).find((l) => l.source === "gracia" && l.target === "eixample")!;
    const at2 = aggregateMoves(ticks, 2).find((l) => l.source === "gracia" && l.target === "eixample")!;
    expect(at2.value).toBeGreaterThanOrEqual(at1.value);
  });
});
