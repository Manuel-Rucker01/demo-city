/**
 * Reconstructs per-agent state (home district, employed, satisfaction) at any tick from
 * agents.json (initial population) plus the cumulative moves/changes in ticks.ndjson.
 *
 * Precomputes dense typed-array "frames" — one row per tick, one column per agent, laid out
 * tick-major so a given tick's slice across all agents is contiguous — so scrubbing to any
 * tick is O(agents) to read (a typed-array slice) rather than O(ticks) to replay.
 */

import type { AgentSnapshot, DistrictId, TickRecord } from "./types";
import { DISTRICT_IDS } from "./types";

export interface AgentIndex {
  /** agent_id -> dense column index [0, nAgents) */
  idToIndex: Map<number, number>;
  /** column index -> agent_id */
  indexToId: Int32Array;
}

export function buildAgentIndex(agents: AgentSnapshot[]): AgentIndex {
  const idToIndex = new Map<number, number>();
  const indexToId = new Int32Array(agents.length);
  agents.forEach((a, i) => {
    idToIndex.set(a.id, i);
    indexToId[i] = a.id;
  });
  return { idToIndex, indexToId };
}

const districtToCode = new Map<DistrictId, number>(DISTRICT_IDS.map((d, i) => [d, i]));

export function districtCode(id: DistrictId): number {
  const code = districtToCode.get(id);
  if (code === undefined) throw new Error(`Unknown district id: ${id}`);
  return code;
}

export function codeToDistrict(code: number): DistrictId {
  const id = DISTRICT_IDS[code];
  if (id === undefined) throw new Error(`Unknown district code: ${code}`);
  return id;
}

export interface ReconstructedRun {
  nAgents: number;
  /** number of tick rows AFTER the initial population, i.e. ticks.length */
  nTicks: number;
  agentIndex: AgentIndex;
  /** [ (nTicks+1) * nAgents ] district code per agent, row 0 = initial population */
  homeByTick: Uint8Array;
  /** [ (nTicks+1) * nAgents ] 0/1 employed flag per agent */
  employedByTick: Uint8Array;
  /** [ (nTicks+1) * nAgents ] satisfaction 0..1 per agent */
  satisfactionByTick: Float32Array;
  /** tick metadata (districts snapshots, usage, moves, etc.) indexed by row-1 (row 0 has none) */
  ticks: TickRecord[];
}

/** Build the dense per-tick frames described above. O(nTicks * nAgents). */
export function reconstructRun(agents: AgentSnapshot[], ticks: TickRecord[]): ReconstructedRun {
  const nAgents = agents.length;
  const nTicks = ticks.length;
  const agentIndex = buildAgentIndex(agents);
  const rows = nTicks + 1;

  const homeByTick = new Uint8Array(rows * nAgents);
  const employedByTick = new Uint8Array(rows * nAgents);
  const satisfactionByTick = new Float32Array(rows * nAgents);

  // Row 0: initial population.
  for (let i = 0; i < nAgents; i++) {
    const a = agents[i]!;
    homeByTick[i] = districtCode(a.home);
    employedByTick[i] = a.employed ? 1 : 0;
    satisfactionByTick[i] = a.satisfaction;
  }

  // Rows 1..nTicks: copy-forward previous row, then apply this tick's moves/changes.
  for (let t = 0; t < nTicks; t++) {
    const prevOffset = t * nAgents;
    const curOffset = (t + 1) * nAgents;
    homeByTick.set(homeByTick.subarray(prevOffset, prevOffset + nAgents), curOffset);
    employedByTick.set(employedByTick.subarray(prevOffset, prevOffset + nAgents), curOffset);
    satisfactionByTick.set(
      satisfactionByTick.subarray(prevOffset, prevOffset + nAgents),
      curOffset,
    );

    const rec = ticks[t]!;
    for (const mv of rec.moves) {
      const idx = agentIndex.idToIndex.get(mv.agent_id);
      if (idx === undefined) continue;
      homeByTick[curOffset + idx] = districtCode(mv.dst);
    }
    for (const ch of rec.changes) {
      const idx = agentIndex.idToIndex.get(ch.agent_id);
      if (idx === undefined) continue;
      if (ch.employed !== undefined && ch.employed !== null) {
        employedByTick[curOffset + idx] = ch.employed ? 1 : 0;
      }
      if (ch.satisfaction !== undefined && ch.satisfaction !== null) {
        satisfactionByTick[curOffset + idx] = ch.satisfaction;
      }
    }
  }

  return { nAgents, nTicks, agentIndex, homeByTick, employedByTick, satisfactionByTick, ticks };
}

export interface AgentState {
  home: DistrictId;
  employed: boolean;
  satisfaction: number;
}

/** O(1) lookup of one agent's state at tick `row` (0 = initial population, 1..nTicks = after that tick). */
export function agentStateAt(run: ReconstructedRun, row: number, agentId: number): AgentState | null {
  const idx = run.agentIndex.idToIndex.get(agentId);
  if (idx === undefined) return null;
  const offset = row * run.nAgents + idx;
  return {
    home: codeToDistrict(run.homeByTick[offset]!),
    employed: run.employedByTick[offset] === 1,
    satisfaction: run.satisfactionByTick[offset]!,
  };
}

/** Contiguous per-agent slices for an entire tick row — the hot path for rendering a frame. */
export function frameAt(
  run: ReconstructedRun,
  row: number,
): { home: Uint8Array; employed: Uint8Array; satisfaction: Float32Array } {
  const offset = row * run.nAgents;
  return {
    home: run.homeByTick.subarray(offset, offset + run.nAgents),
    employed: run.employedByTick.subarray(offset, offset + run.nAgents),
    satisfaction: run.satisfactionByTick.subarray(offset, offset + run.nAgents),
  };
}

/**
 * Naive reference implementation: replay from scratch up to `row` without precomputed frames.
 * Used only to cross-check `reconstructRun` in tests — not for production use (O(ticks) per call).
 */
export function naiveStateAtTick(
  agents: AgentSnapshot[],
  ticks: TickRecord[],
  row: number,
  agentId: number,
): AgentState | null {
  const agent = agents.find((a) => a.id === agentId);
  if (!agent) return null;
  let home = agent.home;
  let employed = agent.employed;
  let satisfaction = agent.satisfaction;
  for (let t = 0; t < row; t++) {
    const rec = ticks[t]!;
    for (const mv of rec.moves) {
      if (mv.agent_id === agentId) home = mv.dst;
    }
    for (const ch of rec.changes) {
      if (ch.agent_id === agentId) {
        if (ch.employed !== undefined && ch.employed !== null) employed = ch.employed;
        if (ch.satisfaction !== undefined && ch.satisfaction !== null) satisfaction = ch.satisfaction;
      }
    }
  }
  return { home, employed, satisfaction };
}
