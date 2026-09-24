#!/usr/bin/env node
/**
 * Generates two contract-conforming fake runs (base + rent_cap_gracia) into web/public/runs/,
 * plus web/public/runs/index.json, so the web app can be developed and recorded before the
 * Python simulation produces real output. Also copies/creates web/public/data/districts.geojson.
 *
 * Shapes mirror src/jevcity/types.py exactly (see docs/CONTRACTS.md) — snake_case fields.
 */

import { mkdirSync, writeFileSync, existsSync, copyFileSync, createWriteStream } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(WEB_ROOT, "..");
const RUNS_OUT = path.join(WEB_ROOT, "public", "runs");
const DATA_OUT = path.join(WEB_ROOT, "public", "data");

const N_AGENTS = 1000;
const TICKS = 365;
const START_DATE = "2026-01-01";

const DISTRICTS = [
  {
    id: "ciutat_vella",
    name: "Ciutat Vella",
    population: 100_000,
    share: 0.12,
    avg_rent_monthly: 1150,
    unemployment_rate: 0.14,
    vacancy_rate: 0.05,
    income_per_capita_annual: 21000,
    jobs_per_resident: 0.85,
    shops: 3200,
    transit_score: 0.9,
    centroid: [2.176, 41.381],
  },
  {
    id: "eixample",
    name: "Eixample",
    population: 265_000,
    share: 0.28,
    avg_rent_monthly: 1350,
    unemployment_rate: 0.09,
    vacancy_rate: 0.04,
    income_per_capita_annual: 27000,
    jobs_per_resident: 1.1,
    shops: 6100,
    transit_score: 0.95,
    centroid: [2.162, 41.391],
  },
  {
    id: "gracia",
    name: "Gràcia",
    population: 121_000,
    share: 0.16,
    avg_rent_monthly: 1250,
    unemployment_rate: 0.1,
    vacancy_rate: 0.035,
    income_per_capita_annual: 24500,
    jobs_per_resident: 0.7,
    shops: 2400,
    transit_score: 0.82,
    centroid: [2.156, 41.404],
  },
  {
    id: "sant_marti",
    name: "Sant Martí",
    population: 236_000,
    share: 0.26,
    avg_rent_monthly: 1200,
    unemployment_rate: 0.11,
    vacancy_rate: 0.055,
    income_per_capita_annual: 23000,
    jobs_per_resident: 0.95,
    shops: 3900,
    transit_score: 0.78,
    centroid: [2.199, 41.407],
  },
  {
    id: "nou_barris",
    name: "Nou Barris",
    population: 165_000,
    share: 0.18,
    avg_rent_monthly: 850,
    unemployment_rate: 0.17,
    vacancy_rate: 0.06,
    income_per_capita_annual: 16500,
    jobs_per_resident: 0.5,
    shops: 1800,
    transit_score: 0.65,
    centroid: [2.177, 41.441],
  },
];

const OCCUPATIONS = ["student", "low_skill", "mid_skill", "high_skill", "retired"];
const OCCUPATION_WEIGHTS = [0.12, 0.28, 0.33, 0.17, 0.1];

function mulberry32(seed) {
  let a = seed >>> 0;
  return function next() {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function weightedPick(rand, items, weights) {
  const total = weights.reduce((s, w) => s + w, 0);
  let r = rand() * total;
  for (let i = 0; i < items.length; i++) {
    r -= weights[i];
    if (r <= 0) return items[i];
  }
  return items[items.length - 1];
}

function isoDate(dayOffset) {
  const d = new Date(`${START_DATE}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + dayOffset);
  return d.toISOString().slice(0, 10);
}

function makeAgents(rand) {
  const agents = [];
  let id = 0;
  for (const d of DISTRICTS) {
    const count = Math.round(N_AGENTS * d.share);
    for (let i = 0; i < count && id < N_AGENTS; i++, id++) {
      const occupation = weightedPick(rand, OCCUPATIONS, OCCUPATION_WEIGHTS);
      const age =
        occupation === "student" ? 18 + Math.floor(rand() * 12) : occupation === "retired" ? 65 + Math.floor(rand() * 20) : 25 + Math.floor(rand() * 40);
      const baseWage = { student: 500, low_skill: 1400, mid_skill: 2200, high_skill: 3600, retired: 1300 }[occupation];
      const wage_monthly = Math.round(baseWage * (0.8 + rand() * 0.4));
      const employed = occupation === "retired" ? true : rand() > d.unemployment_rate;
      agents.push({
        id,
        age,
        occupation,
        home: d.id,
        employed,
        job_district: employed ? (rand() < 0.75 ? d.id : DISTRICTS[Math.floor(rand() * DISTRICTS.length)].id) : null,
        wage_monthly,
        rent_monthly: Math.round(d.avg_rent_monthly * (0.85 + rand() * 0.3)),
        satisfaction: Math.min(1, Math.max(0, 0.55 + (rand() - 0.5) * 0.4)),
      });
    }
  }
  // fill any rounding shortfall into the last district
  while (agents.length < N_AGENTS) {
    const d = DISTRICTS[DISTRICTS.length - 1];
    agents.push({
      id: agents.length,
      age: 30,
      occupation: "mid_skill",
      home: d.id,
      employed: true,
      job_district: d.id,
      wage_monthly: 2000,
      rent_monthly: Math.round(d.avg_rent_monthly),
      satisfaction: 0.6,
    });
  }
  return agents;
}

function districtProfiles() {
  return DISTRICTS.map((d) => ({
    id: d.id,
    name: d.name,
    population: d.population,
    age_distribution: { "0-17": 0.15, "18-34": 0.27, "35-49": 0.23, "50-64": 0.2, "65+": 0.15 },
    income_per_capita_annual: d.income_per_capita_annual,
    avg_rent_monthly: d.avg_rent_monthly,
    vacancy_rate: d.vacancy_rate,
    unemployment_rate: d.unemployment_rate,
    jobs_per_resident: d.jobs_per_resident,
    shops: d.shops,
    transit_score: d.transit_score,
    centroid: d.centroid,
    sources: Object.fromEntries(
      ["population", "age_distribution", "income_per_capita_annual", "avg_rent_monthly", "vacancy_rate", "unemployment_rate", "jobs_per_resident", "shops", "transit_score"].map(
        (k) => [k, "plausible"],
      ),
    ),
    refs: {},
  }));
}

function buildScenario(name, description, withRentCap) {
  return {
    name,
    description,
    seed: 42,
    ticks: TICKS,
    n_agents: N_AGENTS,
    data_path: "data/processed/districts.json",
    jev: { mode: "mock", agents_per_request: 1, confidence_threshold: 0.35, max_cost_usd: 5.0 },
    market: {},
    events: {},
    policies: withRentCap
      ? [{ type: "rent_cap", district: "gracia", start_tick: 30, cap_pct_of_initial: 1.0, max_increase_pct: 0.0 }]
      : [],
  };
}

function generateRun(runId, scenarioName, description, withRentCap, seed) {
  const rand = mulberry32(seed);
  const agents = makeAgents(rand);
  const agentById = new Map(agents.map((a) => [a.id, a]));

  // Live district market state, mutated tick by tick.
  const state = new Map(
    DISTRICTS.map((d) => [
      d.id,
      {
        avg_rent: d.avg_rent_monthly,
        initial_rent: d.avg_rent_monthly,
        residents: agents.filter((a) => a.home === d.id).length,
        unemployment_rate: d.unemployment_rate,
        avg_satisfaction: 0.55,
        vacancy_rate: d.vacancy_rate,
        jobs: Math.round(d.population * d.jobs_per_resident * (N_AGENTS / DISTRICTS.reduce((s, x) => s + x.population, 0)) * 1e-3) || 400,
      },
    ]),
  );

  let usageTotal = { requests: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0, estimated: true, retries: 0, errors: 0, cache_hits: 0 };

  const runDir = path.join(RUNS_OUT, runId);
  mkdirSync(runDir, { recursive: true });
  const ndjsonStream = createWriteStream(path.join(runDir, "ticks.ndjson"), { encoding: "utf-8" });

  let totalMoves = 0;
  let finalDistricts = null;

  for (let t = 1; t <= TICKS; t++) {
    const capActive = withRentCap && t >= 30;
    const moves = [];
    const changes = [];
    const eventsByKind = { payday: Math.round(N_AGENTS / 30), job_loss: 0, job_offer: 0, rent_burden: 0, life_event: 0 };
    const actionsByKind = { stay: 0, move: 0, job_search: 0, spend: 0, save: 0 };
    let gated = 0;

    for (const d of DISTRICTS) {
      const s = state.get(d.id);
      // Rent drifts up ~0.3%/month with noise; capped districts freeze at (or drift back toward) initial rent after start_tick.
      const monthPhase = Math.sin((t / 365) * Math.PI * 2 * 1.5) * 0.004;
      const drift = 0.00035 + monthPhase * 0.3 + (rand() - 0.5) * 0.0015;
      if (capActive && d.id === "gracia") {
        s.avg_rent = s.initial_rent; // cap_pct_of_initial = 1.0, max_increase_pct = 0.0
      } else {
        s.avg_rent = Math.max(400, s.avg_rent * (1 + drift));
      }
      // Unemployment random-walks gently, rent cap in gracia slightly boosts local demand/employment over time.
      const uDrift = (rand() - 0.5) * 0.004 + (capActive && d.id === "gracia" ? -0.00025 : 0);
      s.unemployment_rate = Math.min(0.3, Math.max(0.03, s.unemployment_rate + uDrift));
      const satDrift = (rand() - 0.5) * 0.01 + (capActive && d.id === "gracia" ? 0.0006 : 0) - Math.max(0, s.avg_rent / s.initial_rent - 1) * 0.02;
      s.avg_satisfaction = Math.min(1, Math.max(0, s.avg_satisfaction + satDrift));
      s.vacancy_rate = Math.min(0.2, Math.max(0.01, s.vacancy_rate + (rand() - 0.5) * 0.002));
    }

    // Moves: a small share of agents relocate each day.
    const moveCount = Math.round(N_AGENTS * (0.001 + rand() * 0.003));
    for (let i = 0; i < moveCount; i++) {
      const agent = agents[Math.floor(rand() * agents.length)];
      const dst = DISTRICTS[Math.floor(rand() * DISTRICTS.length)].id;
      if (dst === agent.home) continue;
      // Rent cap makes Gràcia a relatively more attractive destination after it kicks in.
      const bias = capActive && dst === "gracia" ? 0.6 : 1;
      if (rand() > bias) continue;
      moves.push({ agent_id: agent.id, src: agent.home, dst });
      state.get(agent.home).residents -= 1;
      agent.home = dst;
      state.get(dst).residents += 1;
      totalMoves += 1;
      actionsByKind.move += 1;
    }

    // Changes: a subset of agents get satisfaction/employment updates.
    const changeCount = Math.round(N_AGENTS * (0.03 + rand() * 0.05));
    for (let i = 0; i < changeCount; i++) {
      const agent = agents[Math.floor(rand() * agents.length)];
      const districtState = state.get(agent.home);
      const newSat = Math.min(1, Math.max(0, agent.satisfaction + (rand() - 0.5) * 0.08 + (districtState.avg_satisfaction - 0.55) * 0.05));
      let employedChanged = null;
      if (agent.occupation !== "retired" && rand() < 0.02) {
        agent.employed = !agent.employed;
        employedChanged = agent.employed;
      }
      agent.satisfaction = newSat;
      changes.push({ agent_id: agent.id, employed: employedChanged, satisfaction: Number(newSat.toFixed(4)) });
      actionsByKind[rand() < 0.5 ? "spend" : "save"] += 1;
    }
    actionsByKind.stay = N_AGENTS - moveCount - changeCount;
    if (actionsByKind.stay < 0) actionsByKind.stay = 0;
    gated = Math.round(N_AGENTS * 0.01 * rand());

    const districtsSnapshot = DISTRICTS.map((d) => {
      const s = state.get(d.id);
      const residents = Math.max(1, s.residents);
      return {
        id: d.id,
        avg_rent: Math.round(s.avg_rent * 100) / 100,
        avg_paid_rent: Math.round(s.avg_rent * 0.94 * 100) / 100,
        residents,
        vacancy_rate: Math.round(s.vacancy_rate * 1000) / 1000,
        unemployment_rate: Math.round(s.unemployment_rate * 1000) / 1000,
        jobs: s.jobs,
        filled_jobs: Math.round(s.jobs * (1 - s.unemployment_rate * 0.5)),
        shop_revenue: Math.round(residents * 12 * (0.8 + rand() * 0.4)),
        avg_satisfaction: Math.round(s.avg_satisfaction * 1000) / 1000,
        avg_rent_burden: Math.round(((s.avg_rent * 12) / (DISTRICTS.find((x) => x.id === d.id).income_per_capita_annual || 1)) * 1000) / 1000,
        rent_cap_active: capActive && d.id === "gracia",
      };
    });

    const requests = N_AGENTS; // agents_per_request = 1 in these scenarios
    const inputTokens = Math.round(requests * (180 + rand() * 60));
    const outputTokens = 0;
    const cost = (inputTokens / 1_000_000) * 0.042;
    const usageTick = {
      requests,
      input_tokens: inputTokens,
      output_tokens: outputTokens,
      cost_usd: Number(cost.toFixed(6)),
      estimated: true,
      retries: 0,
      errors: 0,
      cache_hits: 0,
    };
    usageTotal = {
      requests: usageTotal.requests + usageTick.requests,
      input_tokens: usageTotal.input_tokens + usageTick.input_tokens,
      output_tokens: usageTotal.output_tokens + usageTick.output_tokens,
      cost_usd: Number((usageTotal.cost_usd + usageTick.cost_usd).toFixed(6)),
      estimated: true,
      retries: 0,
      errors: 0,
      cache_hits: 0,
    };

    const record = {
      tick: t,
      date: isoDate(t),
      districts: districtsSnapshot,
      events_by_kind: eventsByKind,
      actions_by_kind: actionsByKind,
      gated_decisions: gated,
      moves,
      changes,
      usage_tick: usageTick,
      usage_total: usageTotal,
    };
    ndjsonStream.write(JSON.stringify(record) + "\n");
    if (t === TICKS) finalDistricts = districtsSnapshot;
  }
  ndjsonStream.end();

  void agentById; // kept for clarity of intent (agents mutated in place above)

  const meta = {
    run_id: runId,
    created_at: new Date().toISOString(),
    scenario: buildScenario(scenarioName, description, withRentCap),
    profiles: districtProfiles(),
    start_date: START_DATE,
    jev_model: "jev-1.13.0",
  };
  writeFileSync(path.join(runDir, "meta.json"), JSON.stringify(meta, null, 2));
  // agents.json is the INITIAL population, captured before this function mutated `agents` in place.
  writeFileSync(path.join(runDir, "agents.json"), JSON.stringify(initialAgentsSnapshot(runId), null, 2));

  const summary = {
    run_id: runId,
    ticks: TICKS,
    usage: usageTotal,
    total_moves: totalMoves,
    final_districts: finalDistricts,
    wall_time_s: Number((TICKS * 0.08).toFixed(2)),
  };
  writeFileSync(path.join(runDir, "summary.json"), JSON.stringify(summary, null, 2));

  console.log(`  wrote ${runId}: ${TICKS} ticks, ${agents.length} agents, ${totalMoves} total moves`);
  return { run_id: runId, scenario: scenarioName, description, ticks: TICKS, n_agents: N_AGENTS };
}

// agents.json must reflect the INITIAL population (pre-simulation), captured before generateRun
// mutates `agents` in place tick by tick. We snapshot it right after makeAgents() runs, keyed
// by run id, since generateRun() needs the same `agents` array reference for its live state.
const initialSnapshots = new Map();
function initialAgentsSnapshot(runId) {
  return initialSnapshots.get(runId);
}

function generateRunWithSnapshot(runId, scenarioName, description, withRentCap, seed) {
  return generateRun(runId, scenarioName, description, withRentCap, seed);
}

function ensureDistrictsGeojson() {
  mkdirSync(DATA_OUT, { recursive: true });
  const realPath = path.join(REPO_ROOT, "data", "processed", "districts.geojson");
  const outPath = path.join(DATA_OUT, "districts.geojson");
  if (existsSync(realPath)) {
    copyFileSync(realPath, outPath);
    console.log("  copied real districts.geojson from data/processed/");
    return;
  }
  // Rough irregular-pentagon placeholders around each district centroid (~1.6km "radius").
  const R = 0.014;
  const features = DISTRICTS.map((d) => {
    const [lon, lat] = d.centroid;
    const coords = [];
    const sides = 5 + Math.floor(Math.abs(Math.sin(lon * 97)) * 3); // 5-7 sides, deterministic per district
    for (let i = 0; i <= sides; i++) {
      const angle = (i / sides) * Math.PI * 2 + lon * 3; // deterministic rotation per district
      const wobble = 1 + 0.25 * Math.sin(angle * 3 + lat * 17);
      coords.push([lon + Math.cos(angle) * R * wobble, lat + Math.sin(angle) * R * wobble * 0.82]);
    }
    return {
      type: "Feature",
      properties: { id: d.id, name: d.name, placeholder: true, note: "PLACEHOLDER geometry — not real district boundaries" },
      geometry: { type: "Polygon", coordinates: [coords] },
    };
  });
  const fc = { type: "FeatureCollection", features };
  writeFileSync(outPath, JSON.stringify(fc, null, 2));
  console.log("  wrote PLACEHOLDER districts.geojson (real data/processed/districts.geojson not found yet)");
}

function main() {
  console.log("Generating fake jevcity runs for web development...");
  mkdirSync(RUNS_OUT, { recursive: true });

  const rand1 = mulberry32(42);
  initialSnapshots.set("fake-base-001", makeAgents(rand1).map(cloneAgent));
  const rand2 = mulberry32(42); // same seed/base population as base, so districts start identical
  initialSnapshots.set("fake-rent-cap-gracia-001", makeAgents(rand2).map(cloneAgent));

  const entries = [];
  entries.push(generateRunWithSnapshot("fake-base-001", "base", "Baseline Barcelona, no policy intervention. (fake dev data)", false, 42));
  entries.push(
    generateRunWithSnapshot(
      "fake-rent-cap-gracia-001",
      "rent_cap_gracia",
      "Rent cap in Gràcia from day 30 — capped at initial rent, renewals frozen. (fake dev data)",
      true,
      42,
    ),
  );

  writeFileSync(path.join(RUNS_OUT, "index.json"), JSON.stringify(entries, null, 2));
  ensureDistrictsGeojson();
  console.log(`Done. Wrote ${entries.length} runs to ${path.relative(REPO_ROOT, RUNS_OUT)}/`);
}

function cloneAgent(a) {
  return { ...a };
}

main();
