/**
 * Reconstructs per-agent state (home district, employed, satisfaction, active) at any tick from
 * agents.json (initial population) plus the cumulative moves/changes/arrivals/departures in
 * ticks.ndjson.
 *
 * Precomputes dense typed-array "frames" — one row per tick, one column per agent, laid out
 * tick-major so a given tick's slice across all agents is contiguous — so scrubbing to any
 * tick is O(agents) to read (a typed-array slice) rather than O(ticks) to replay.
 *
 * The agent set is not fixed size: households arrive (new agent ids, never seen in agents.json
 * or any earlier tick) and depart (an existing agent id stops being simulated) over the course
 * of a run. Rather than sizing arrays by the largest raw agent id (which could be sparse and
 * unbounded), every agent — initial population plus every arrival, in the order they first
 * appear — gets one dense column index; this keeps the frame arrays as compact as a
 * fixed-population run while still covering every id that ever exists during the run. A
 * separate `activeByTick` mask marks which columns are "alive" (present in the city) at each
 * row, so an agent's column exists (and is well-defined, so lookups never go out of bounds)
 * from row 0 even before they arrive, but reads as inactive until their arrival tick, and after
 * departure, forever.
 */

import type { AgentSnapshot, DistrictId, TickRecord } from "./types";
import { DISTRICT_IDS } from "./types";

export interface AgentIndex {
  /** agent_id -> dense column index [0, nAgents), for both initial and arrival agents */
  idToIndex: Map<number, number>;
  /** column index -> agent_id */
  indexToId: Int32Array;
}

/**
 * The full agent roster for a run: initial population first, then every arrival in the order it
 * appears across ticks (ticks are assumed already sorted by `tick`). This is the single source
 * of truth for "column order" — reconstructRun and MapView both build their per-agent arrays
 * from this same list so a given index always refers to the same agent everywhere.
 */
export function buildCombinedAgents(agents: AgentSnapshot[], ticks: TickRecord[]): AgentSnapshot[] {
  const combined = [...agents];
  const seen = new Set(agents.map((a) => a.id));
  for (const t of ticks) {
    for (const a of t.arrivals ?? []) {
      if (seen.has(a.id)) continue; // defensive: never double-count a repeated id
      seen.add(a.id);
      combined.push(a);
    }
  }
  return combined;
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
  /** total distinct agents that ever exist during the run: initial population + all arrivals */
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
  /** [ (nTicks+1) * nAgents ] 1 = agent is present in the city at this row, 0 = not-yet-arrived
   * or already-departed. Absent (all-1) on runs with no arrivals/departures data. */
  activeByTick: Uint8Array;
  /** tick metadata (districts snapshots, usage, moves, etc.) indexed by row-1 (row 0 has none) */
  ticks: TickRecord[];
}

/** Build the dense per-tick frames described above. O(nTicks * nAgents). */
export function reconstructRun(agents: AgentSnapshot[], ticks: TickRecord[]): ReconstructedRun {
  const combined = buildCombinedAgents(agents, ticks);
  const nAgents = combined.length;
  const nTicks = ticks.length;
  const agentIndex = buildAgentIndex(combined);
  const rows = nTicks + 1;
  const nInitial = agents.length;

  const homeByTick = new Uint8Array(rows * nAgents);
  const employedByTick = new Uint8Array(rows * nAgents);
  const satisfactionByTick = new Float32Array(rows * nAgents);
  const activeByTick = new Uint8Array(rows * nAgents);

  // Row 0: initial population is active; not-yet-arrived agents get their eventual arrival
  // snapshot's fields pre-filled (so there's no discontinuity when they turn active) but stay
  // inactive (0) until their arrival tick.
  for (let i = 0; i < nAgents; i++) {
    const a = combined[i]!;
    homeByTick[i] = districtCode(a.home);
    employedByTick[i] = a.employed ? 1 : 0;
    satisfactionByTick[i] = a.satisfaction;
    activeByTick[i] = i < nInitial ? 1 : 0;
  }

  // Rows 1..nTicks: copy-forward previous row, then apply this tick's arrivals/departures/moves/changes.
  for (let t = 0; t < nTicks; t++) {
    const prevOffset = t * nAgents;
    const curOffset = (t + 1) * nAgents;
    homeByTick.set(homeByTick.subarray(prevOffset, prevOffset + nAgents), curOffset);
    employedByTick.set(employedByTick.subarray(prevOffset, prevOffset + nAgents), curOffset);
    satisfactionByTick.set(satisfactionByTick.subarray(prevOffset, prevOffset + nAgents), curOffset);
    activeByTick.set(activeByTick.subarray(prevOffset, prevOffset + nAgents), curOffset);

    const rec = ticks[t]!;
    for (const a of rec.arrivals ?? []) {
      const idx = agentIndex.idToIndex.get(a.id);
      if (idx === undefined) continue;
      homeByTick[curOffset + idx] = districtCode(a.home);
      employedByTick[curOffset + idx] = a.employed ? 1 : 0;
      satisfactionByTick[curOffset + idx] = a.satisfaction;
      activeByTick[curOffset + idx] = 1;
    }
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
    // Departures applied last, so an agent that both changes and departs the same tick ends up
    // inactive (departure wins).
    for (const id of rec.departures ?? []) {
      const idx = agentIndex.idToIndex.get(id);
      if (idx === undefined) continue;
      activeByTick[curOffset + idx] = 0;
    }
  }

  return { nAgents, nTicks, agentIndex, homeByTick, employedByTick, satisfactionByTick, activeByTick, ticks };
}

export interface AgentState {
  home: DistrictId;
  employed: boolean;
  satisfaction: number;
  active: boolean;
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
    active: run.activeByTick[offset] === 1,
  };
}

/** Contiguous per-agent slices for an entire tick row — the hot path for rendering a frame. */
export function frameAt(
  run: ReconstructedRun,
  row: number,
): { home: Uint8Array; employed: Uint8Array; satisfaction: Float32Array; active: Uint8Array } {
  const offset = row * run.nAgents;
  return {
    home: run.homeByTick.subarray(offset, offset + run.nAgents),
    employed: run.employedByTick.subarray(offset, offset + run.nAgents),
    satisfaction: run.satisfactionByTick.subarray(offset, offset + run.nAgents),
    active: run.activeByTick.subarray(offset, offset + run.nAgents),
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
  const initial = agents.find((a) => a.id === agentId);
  let home: DistrictId;
  let employed: boolean;
  let satisfaction: number;
  let active: boolean;
  if (initial) {
    home = initial.home;
    employed = initial.employed;
    satisfaction = initial.satisfaction;
    active = true;
  } else {
    // Might be an arrival later in the run — find its arrival snapshot, if any within [0, row).
    let arrivalSnap: AgentSnapshot | null = null;
    for (let t = 0; t < row; t++) {
      const found = ticks[t]?.arrivals?.find((a) => a.id === agentId);
      if (found) {
        arrivalSnap = found;
        break;
      }
    }
    if (!arrivalSnap) return null;
    home = arrivalSnap.home;
    employed = arrivalSnap.employed;
    satisfaction = arrivalSnap.satisfaction;
    active = true;
  }
  for (let t = 0; t < row; t++) {
    const rec = ticks[t]!;
    for (const a of rec.arrivals ?? []) {
      if (a.id === agentId) {
        home = a.home;
        employed = a.employed;
        satisfaction = a.satisfaction;
        active = true;
      }
    }
    for (const mv of rec.moves) {
      if (mv.agent_id === agentId) home = mv.dst;
    }
    for (const ch of rec.changes) {
      if (ch.agent_id === agentId) {
        if (ch.employed !== undefined && ch.employed !== null) employed = ch.employed;
        if (ch.satisfaction !== undefined && ch.satisfaction !== null) satisfaction = ch.satisfaction;
      }
    }
    for (const id of rec.departures ?? []) {
      if (id === agentId) active = false;
    }
  }
  return { home, employed, satisfaction, active };
}
