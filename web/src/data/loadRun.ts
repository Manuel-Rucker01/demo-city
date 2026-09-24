/** Fetches a run's static files from public/runs/<run_id>/ and reconstructs per-tick state. */

import { streamNdjson } from "./ndjson";
import { buildCombinedAgents, reconstructRun, type ReconstructedRun } from "./reconstruct";
import type { AgentSnapshot, RunIndex, RunMeta, RunSummary, TickRecord } from "./types";

const RUNS_BASE = "/runs";

export async function fetchRunIndex(): Promise<RunIndex> {
  const res = await fetch(`${RUNS_BASE}/index.json`);
  if (!res.ok) throw new Error(`Failed to load run index: ${res.status}`);
  return (await res.json()) as RunIndex;
}

export async function fetchRunMeta(runId: string): Promise<RunMeta> {
  const res = await fetch(`${RUNS_BASE}/${runId}/meta.json`);
  if (!res.ok) throw new Error(`Failed to load meta.json for ${runId}: ${res.status}`);
  return (await res.json()) as RunMeta;
}

export async function fetchAgents(runId: string): Promise<AgentSnapshot[]> {
  const res = await fetch(`${RUNS_BASE}/${runId}/agents.json`);
  if (!res.ok) throw new Error(`Failed to load agents.json for ${runId}: ${res.status}`);
  return (await res.json()) as AgentSnapshot[];
}

export async function fetchSummary(runId: string): Promise<RunSummary> {
  const res = await fetch(`${RUNS_BASE}/${runId}/summary.json`);
  if (!res.ok) throw new Error(`Failed to load summary.json for ${runId}: ${res.status}`);
  return (await res.json()) as RunSummary;
}

export async function fetchTicks(
  runId: string,
  onProgress?: (loaded: number) => void,
): Promise<TickRecord[]> {
  const res = await fetch(`${RUNS_BASE}/${runId}/ticks.ndjson`);
  if (!res.ok) throw new Error(`Failed to load ticks.ndjson for ${runId}: ${res.status}`);
  const ticks: TickRecord[] = [];
  await streamNdjson<TickRecord>(res, (rec) => {
    ticks.push(rec);
    if (onProgress) onProgress(ticks.length);
  });
  ticks.sort((a, b) => a.tick - b.tick);
  return ticks;
}

export interface LoadedRun {
  meta: RunMeta;
  /** Initial population only (agents.json), as before. */
  agents: AgentSnapshot[];
  /** Initial population + every arrival across the run, in column order — matches `state`'s
   * dense agent index 1:1, so this is what MapView should be constructed with. */
  combinedAgents: AgentSnapshot[];
  summary: RunSummary;
  ticks: TickRecord[];
  state: ReconstructedRun;
}

export async function loadRun(runId: string, onProgress?: (loaded: number, total: number) => void): Promise<LoadedRun> {
  const [meta, agents, summary] = await Promise.all([
    fetchRunMeta(runId),
    fetchAgents(runId),
    fetchSummary(runId),
  ]);
  const totalTicks = meta.scenario.ticks;
  const ticks = await fetchTicks(runId, (loaded) => onProgress?.(loaded, totalTicks));
  const state = reconstructRun(agents, ticks);
  const combinedAgents = buildCombinedAgents(agents, ticks);
  return { meta, agents, combinedAgents, summary, ticks, state };
}
