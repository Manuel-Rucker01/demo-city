import "./style/app.css";
import { fetchRunIndex, loadRun, type LoadedRun } from "./data/loadRun";
import { loadDistrictsGeo } from "./map/districts";
import { MapView, type ColorMode, type FillMetric } from "./map/MapView";
import { frameAt } from "./data/reconstruct";
import { LineChartPanel } from "./charts/LineChartPanel";
import { SankeyPanel } from "./charts/SankeyPanel";
import { KpiTiles } from "./charts/KpiTiles";
import { registerChartTheme } from "./charts/theme";
import { aggregateMoves } from "./data/sankey";
import { Playback, type SpeedMultiplier } from "./playback/Playback";
import { parseRecordParams, mountRecordOverlay } from "./record/RecordMode";
import { DISTRICT_COLORS } from "./style/theme";
import { DISTRICT_IDS, type RunIndex } from "./data/types";
import { districtDisplayName } from "./data/format";

registerChartTheme();

const app = document.getElementById("app")!;
const record = parseRecordParams();
if (record.enabled) document.body.classList.add("record-mode");

app.innerHTML = `
  <header class="app-header">
    <div class="app-title">Jev<span class="accent">City</span> <span style="color:var(--fg-dim);font-weight:500;font-size:12px;margin-left:6px">Barcelona · 1 tick = 1 day</span></div>
    <div class="header-controls">
      <select id="run-select"></select>
      <button id="compare-toggle">Compare</button>
      <select id="compare-select" style="display:none"></select>
      <div class="btn-group" id="color-mode-group">
        <button data-mode="district" class="active">District</button>
        <button data-mode="employed">Employment</button>
        <button data-mode="satisfaction">Satisfaction</button>
      </div>
      <select id="fill-metric">
        <option value="avg_rent">Fill: avg rent</option>
        <option value="unemployment_rate">Fill: unemployment</option>
        <option value="avg_satisfaction">Fill: satisfaction</option>
      </select>
    </div>
  </header>
  <div class="app-main">
    <div class="map-pane single" id="map-pane"></div>
    <div class="side-panel" id="side-panel">
      <div class="loading-overlay" id="loading">Loading run…</div>
      <div id="kpi-tiles"></div>
      <div class="legend-row" id="legend-row"></div>
      <div class="chart-slot" id="rent-chart"></div>
      <div class="chart-slot" id="unemployment-chart"></div>
      <div class="chart-slot sankey" id="sankey-chart"></div>
    </div>
  </div>
  <div class="playback-bar">
    <button class="play-btn" id="play-btn">▶</button>
    <div class="btn-group" id="speed-group">
      <button data-speed="1" class="active">1x</button>
      <button data-speed="2">2x</button>
      <button data-speed="5">5x</button>
      <button data-speed="10">10x</button>
    </div>
    <input type="range" id="scrub" min="0" max="365" value="0" />
    <div class="tick-label" id="tick-label">Day 0</div>
  </div>
`;

const mapPane = document.getElementById("map-pane") as HTMLDivElement;
const runSelect = document.getElementById("run-select") as HTMLSelectElement;
const compareToggle = document.getElementById("compare-toggle") as HTMLButtonElement;
const compareSelect = document.getElementById("compare-select") as HTMLSelectElement;
const colorModeGroup = document.getElementById("color-mode-group") as HTMLDivElement;
const fillMetricSelect = document.getElementById("fill-metric") as HTMLSelectElement;
const loadingEl = document.getElementById("loading") as HTMLDivElement;
const legendRow = document.getElementById("legend-row") as HTMLDivElement;
const playBtn = document.getElementById("play-btn") as HTMLButtonElement;
const speedGroup = document.getElementById("speed-group") as HTMLDivElement;
const scrub = document.getElementById("scrub") as HTMLInputElement;
const tickLabel = document.getElementById("tick-label") as HTMLDivElement;

legendRow.innerHTML = DISTRICT_IDS.map(
  (d) => `<span class="legend-item"><span class="legend-dot" style="background:${DISTRICT_COLORS[d]}"></span>${districtDisplayName(d)}</span>`,
).join("");

const kpiTiles = new KpiTiles(document.getElementById("kpi-tiles") as HTMLDivElement);
const rentChart = new LineChartPanel(document.getElementById("rent-chart") as HTMLDivElement, "Avg market rent / district", (v) => `€${Math.round(v)}`);
const unemploymentChart = new LineChartPanel(
  document.getElementById("unemployment-chart") as HTMLDivElement,
  "Unemployment rate / district",
  (v) => `${(v * 100).toFixed(0)}%`,
);
const sankeyChart = new SankeyPanel(document.getElementById("sankey-chart") as HTMLDivElement);

interface RunSlot {
  runId: string;
  label: "base" | "scenario";
  loaded: LoadedRun;
  mapView: MapView;
  slotEl: HTMLDivElement;
}

let slots: RunSlot[] = [];
let playback: Playback | null = null;
let colorMode: ColorMode = "district";
let fillMetric: FillMetric = "avg_rent";
let lastTickIndex = 0;
let compareMode = false;
let runIndex: RunIndex = [];

async function buildMapSlot(runId: string, label: "base" | "scenario"): Promise<RunSlot> {
  const slotEl = document.createElement("div");
  slotEl.className = "map-slot";
  const labelEl = document.createElement("div");
  labelEl.className = "map-slot-label";
  labelEl.textContent = label === "base" ? "Base" : "Rent cap · Gràcia";
  slotEl.appendChild(labelEl);
  mapPane.appendChild(slotEl);

  const geo = await loadDistrictsGeo();
  const loaded = await loadRun(runId, (loadedTicks, total) => {
    loadingEl.textContent = `Loading ${label} run… ${loadedTicks}/${total} ticks`;
  });
  const mapView = new MapView({ container: slotEl, geo, agents: loaded.agents, pitch: record.enabled ? 20 : 0 });
  mapView.setColorMode(colorMode);
  mapView.setFillMetric(fillMetric);
  return { runId, label, loaded, mapView, slotEl };
}

function destroySlots(): void {
  for (const s of slots) {
    s.mapView.destroy();
    s.slotEl.remove();
  }
  slots = [];
}

async function setup(baseRunId: string, compareRunId: string | null): Promise<void> {
  loadingEl.style.display = "flex";
  loadingEl.textContent = "Loading run…";
  destroySlots();
  mapPane.className = compareRunId ? "map-pane split" : "map-pane single";

  const newSlots: RunSlot[] = [await buildMapSlot(baseRunId, "base")];
  if (compareRunId) newSlots.push(await buildMapSlot(compareRunId, "scenario"));
  slots = newSlots;

  const maxTick = Math.min(...slots.map((s) => s.loaded.state.nTicks));
  playback = new Playback(maxTick);
  scrub.max = String(maxTick);
  lastTickIndex = 0;
  playback.onChange(onPlaybackChange);
  playback.seek(0);
  renderCharts();
  loadingEl.style.display = "none";

  if (record.enabled) {
    const overlay = mountRecordOverlay(document.body, {});
    window.setTimeout(() => {
      playback?.setSpeed(Math.max(1, Math.min(10, record.speed)) as SpeedMultiplier);
      playback?.play();
    }, 1000);
    const unsub = playback.onChange((s) => {
      const primary = slots[0]?.loaded.ticks[s.tickIndex - 1];
      if (primary) overlay.setDate(primary.date);
    });
    void unsub;
  }
}

function renderCharts(): void {
  const rentSpecs = slots.map((s) => ({
    runLabel: s.label,
    ticks: s.loaded.ticks,
    metric: (d: { avg_rent: number }) => d.avg_rent,
    highlightDistrict: s.loaded.meta.scenario.policies[0]?.district ?? null,
  }));
  rentChart.setData(rentSpecs);
  const unemploymentSpecs = slots.map((s) => ({
    runLabel: s.label,
    ticks: s.loaded.ticks,
    metric: (d: { unemployment_rate: number }) => d.unemployment_rate,
    highlightDistrict: s.loaded.meta.scenario.policies[0]?.district ?? null,
  }));
  unemploymentChart.setData(unemploymentSpecs);
}

function onPlaybackChange(state: { tickIndex: number; playing: boolean; speed: number }): void {
  playBtn.textContent = state.playing ? "⏸" : "▶";
  scrub.value = String(state.tickIndex);
  const primary = slots[0];
  const tickRec = primary?.loaded.ticks[state.tickIndex - 1] ?? null;
  tickLabel.textContent = `Day ${state.tickIndex}${tickRec ? ` · ${tickRec.date}` : ""}`;

  const stepSize = state.tickIndex - lastTickIndex;
  for (const s of slots) {
    const frame = frameAt(s.loaded.state, state.tickIndex);
    s.mapView.setAgentState(frame.home, frame.employed, frame.satisfaction);
    if (stepSize === 1 && state.tickIndex >= 1) {
      const rec = s.loaded.ticks[state.tickIndex - 1];
      if (rec) {
        for (const mv of rec.moves) {
          if (mv.src !== mv.dst) s.mapView.animateMove(mv.agent_id, mv.dst);
        }
      }
    }
    if (state.tickIndex > 0) {
      const rec = s.loaded.ticks[state.tickIndex - 1];
      if (rec) s.mapView.setDistrictSnapshots(rec.districts);
    }
  }
  lastTickIndex = state.tickIndex;

  if (primary) {
    kpiTiles.update({
      meta: primary.loaded.meta,
      tick: tickRec,
      tickIndex: state.tickIndex,
      totalTicks: primary.loaded.state.nTicks,
    });
    rentChart.setCursor(Math.max(0, state.tickIndex - 1));
    unemploymentChart.setCursor(Math.max(0, state.tickIndex - 1));
    sankeyChart.setLinks(aggregateMoves(primary.loaded.ticks, state.tickIndex - 1));
  }
}

// ---- wiring ----

runSelect.addEventListener("change", () => {
  void setup(runSelect.value, compareMode ? compareSelect.value : null);
});
compareSelect.addEventListener("change", () => {
  if (compareMode) void setup(runSelect.value, compareSelect.value);
});
compareToggle.addEventListener("click", () => {
  compareMode = !compareMode;
  compareToggle.classList.toggle("active", compareMode);
  compareSelect.style.display = compareMode ? "inline-block" : "none";
  void setup(runSelect.value, compareMode ? compareSelect.value : null);
});

colorModeGroup.addEventListener("click", (e) => {
  const btn = (e.target as HTMLElement).closest("button");
  if (!btn) return;
  colorMode = btn.dataset.mode as ColorMode;
  [...colorModeGroup.children].forEach((c) => c.classList.remove("active"));
  btn.classList.add("active");
  slots.forEach((s) => s.mapView.setColorMode(colorMode));
});

fillMetricSelect.addEventListener("change", () => {
  fillMetric = fillMetricSelect.value as FillMetric;
  slots.forEach((s) => s.mapView.setFillMetric(fillMetric));
});

playBtn.addEventListener("click", () => playback?.toggle());
speedGroup.addEventListener("click", (e) => {
  const btn = (e.target as HTMLElement).closest("button");
  if (!btn) return;
  const speed = Number(btn.dataset.speed) as SpeedMultiplier;
  playback?.setSpeed(speed);
  [...speedGroup.children].forEach((c) => c.classList.remove("active"));
  btn.classList.add("active");
});
scrub.addEventListener("input", () => playback?.seek(Number(scrub.value)));

window.addEventListener("resize", () => {
  rentChart.resize();
  unemploymentChart.resize();
  sankeyChart.resize();
});

async function boot(): Promise<void> {
  runIndex = await fetchRunIndex();
  const optionsHtml = runIndex.map((r) => `<option value="${r.run_id}">${r.scenario} · ${r.description || r.run_id}</option>`).join("");
  runSelect.innerHTML = optionsHtml;
  compareSelect.innerHTML = optionsHtml;

  const base = runIndex.find((r) => r.scenario === "base") ?? runIndex[0];
  const scenario = runIndex.find((r) => r.scenario !== "base") ?? runIndex[1];
  if (base) runSelect.value = base.run_id;
  if (scenario) compareSelect.value = scenario.run_id;

  window.addEventListener("keydown", (e) => {
    if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
    if (!playback) return;
    if (e.code === "Space") {
      e.preventDefault();
      playback.toggle();
    } else if (e.code === "ArrowRight") {
      e.preventDefault();
      playback.step(1);
    } else if (e.code === "ArrowLeft") {
      e.preventDefault();
      playback.step(-1);
    }
  });

  if (record.enabled) {
    compareMode = !!record.compare;
    if (compareMode) {
      compareToggle.classList.add("active");
      compareSelect.style.display = "inline-block";
    }
    await setup(record.run ?? base?.run_id ?? "", record.compare ?? null);
  } else {
    await setup(base?.run_id ?? "", null);
  }
}

void boot();
