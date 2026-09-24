import "./style/app.css";
import { fetchRunIndex, loadRun, type LoadedRun } from "./data/loadRun";
import { loadDistrictsGeo } from "./map/districts";
import { MapView, type ColorMode, type FillMetric } from "./map/MapView";
import { frameAt } from "./data/reconstruct";
import { LineChartPanel } from "./charts/LineChartPanel";
import { SankeyPanel } from "./charts/SankeyPanel";
import { StackedAreaPanel } from "./charts/StackedAreaPanel";
import { BarPanel } from "./charts/BarPanel";
import { KpiTiles } from "./charts/KpiTiles";
import { registerChartTheme } from "./charts/theme";
import { aggregateMoves } from "./data/sankey";
import { Playback, type SpeedMultiplier } from "./playback/Playback";
import { parseRecordParams, mountRecordOverlay } from "./record/RecordMode";
import { DISTRICT_COLORS } from "./style/theme";
import { DISTRICT_IDS, type RunIndex } from "./data/types";
import { districtDisplayName, formatCurrencyCompact } from "./data/format";

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
        <button data-mode="commute">Commute</button>
      </div>
      <select id="fill-metric">
        <option value="avg_rent">Fill: avg rent</option>
        <option value="unemployment_rate">Fill: unemployment</option>
        <option value="avg_satisfaction">Fill: satisfaction</option>
        <option value="tourist_units">Fill: tourist units</option>
        <option value="shops_open">Fill: shops open</option>
      </select>
    </div>
  </header>
  <div class="app-main">
    <div class="map-pane single" id="map-pane"></div>
    <div class="side-panel" id="side-panel">
      <div class="loading-overlay" id="loading">Loading run…</div>
      <div id="kpi-tiles"></div>
      <div class="legend-row" id="legend-row"></div>
      <div class="chart-tabs" id="chart-tabs">
        <button data-tab="housing" class="active">Housing</button>
        <button data-tab="tourism">Tourism &amp; commerce</button>
        <button data-tab="mobility">Mobility &amp; migration</button>
      </div>
      <div class="tab-panel" data-tab-panel="housing">
        <div class="chart-slot" id="rent-chart"></div>
        <div class="chart-slot" id="unemployment-chart"></div>
      </div>
      <div class="tab-panel" data-tab-panel="tourism" style="display:none">
        <div class="chart-grid-2">
          <div class="chart-slot compact" id="tourist-chart"></div>
          <div class="chart-slot compact" id="shops-chart"></div>
        </div>
      </div>
      <div class="tab-panel" data-tab-panel="mobility" style="display:none">
        <div class="chart-slot" id="mode-share-chart"></div>
        <div class="chart-slot" id="migration-chart"></div>
      </div>
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
const chartTabs = document.getElementById("chart-tabs") as HTMLDivElement;
const playBtn = document.getElementById("play-btn") as HTMLButtonElement;
const speedGroup = document.getElementById("speed-group") as HTMLDivElement;
const scrub = document.getElementById("scrub") as HTMLInputElement;
const tickLabel = document.getElementById("tick-label") as HTMLDivElement;

legendRow.innerHTML = DISTRICT_IDS.map(
  (d) => `<span class="legend-item"><span class="legend-dot" style="background:${DISTRICT_COLORS[d]}"></span>${districtDisplayName(d)}</span>`,
).join("");

// ---- chart tabs: keeps the side panel readable at 1920x1080 with 6 time-series charts ----
const tabPanels = new Map<string, HTMLElement>(
  [...document.querySelectorAll<HTMLElement>("[data-tab-panel]")].map((el) => [el.dataset.tabPanel!, el]),
);
function activateTab(tab: string): void {
  for (const [name, el] of tabPanels) el.style.display = name === tab ? "" : "none";
  [...chartTabs.children].forEach((c) => c.classList.toggle("active", (c as HTMLElement).dataset.tab === tab));
  // Charts inside a tab that was hidden at construction time were sized 0x0 by ECharts; resize
  // them now that the container has real dimensions.
  if (tab === "housing") {
    rentChart.resize();
    unemploymentChart.resize();
  } else if (tab === "tourism") {
    touristChart.resize();
    shopsChart.resize();
  } else if (tab === "mobility") {
    modeShareChart.resize();
    migrationChart.resize();
  }
}
chartTabs.addEventListener("click", (e) => {
  const btn = (e.target as HTMLElement).closest("button");
  if (!btn?.dataset.tab) return;
  activateTab(btn.dataset.tab);
});

const kpiTiles = new KpiTiles(document.getElementById("kpi-tiles") as HTMLDivElement);
const tooltipsEnabled = !record.enabled;
const rentChart = new LineChartPanel(
  document.getElementById("rent-chart") as HTMLDivElement,
  "Avg market rent / district",
  formatCurrencyCompact,
  { tooltipsEnabled },
);
const unemploymentChart = new LineChartPanel(
  document.getElementById("unemployment-chart") as HTMLDivElement,
  "Unemployment rate / district",
  (v) => `${(v * 100).toFixed(0)}%`,
  { tooltipsEnabled },
);
const touristChart = new LineChartPanel(
  document.getElementById("tourist-chart") as HTMLDivElement,
  "Tourist units / district",
  (v) => `${Math.round(v)}`,
  { tooltipsEnabled },
);
const shopsChart = new LineChartPanel(
  document.getElementById("shops-chart") as HTMLDivElement,
  "Shops open / district",
  (v) => `${Math.round(v)}`,
  { tooltipsEnabled },
);
const modeShareChart = new StackedAreaPanel(document.getElementById("mode-share-chart") as HTMLDivElement, { tooltipsEnabled });
const migrationChart = new BarPanel(document.getElementById("migration-chart") as HTMLDivElement, { tooltipsEnabled });
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

const SCENARIO_LABELS: Record<string, string> = {
  base: "Base",
  new_metro_line: "New metro line",
  rent_cap_gracia: "Rent cap · Gràcia",
  rent_cap_sant_andreu: "Rent cap · Sant Andreu",
  hut_ban_2028: "Tourist-flat ban",
  low_emission_zone: "Low-emission zone",
  combo_policies: "Rent cap + tourist-flat ban",
};

function scenarioLabel(name: string): string {
  return SCENARIO_LABELS[name] ?? name.replace(/_/g, " ");
}

async function buildMapSlot(runId: string, label: "base" | "scenario"): Promise<RunSlot> {
  const slotEl = document.createElement("div");
  slotEl.className = "map-slot";
  const labelEl = document.createElement("div");
  labelEl.className = "map-slot-label";
  labelEl.textContent = label === "base" ? "Base" : "Scenario";
  slotEl.appendChild(labelEl);
  mapPane.appendChild(slotEl);

  const geo = await loadDistrictsGeo();
  const loaded = await loadRun(runId, (loadedTicks, total) => {
    loadingEl.textContent = `Loading ${label} run… ${loadedTicks}/${total} ticks`;
  });
  labelEl.textContent = scenarioLabel(loaded.meta.scenario.name);
  // MapView is built with the FULL agent roster (initial population + every arrival across the
  // run, in the same dense column order reconstructRun uses) since the whole run is already
  // loaded by this point — arrivals aren't a "future unknown", just initially-inactive columns.
  const mapView = new MapView({ container: slotEl, geo, agents: loaded.combinedAgents, pitch: record.enabled ? 20 : 0 });
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
    const provider = runIndex.find((r) => r.run_id === baseRunId)?.provider ?? null;
    const overlay = mountRecordOverlay(document.body, { title: record.title ?? undefined, provider });
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
  const primary = slots[0];
  const policies = primary?.loaded.meta.scenario.policies ?? [];

  const rentSpecs = slots.map((s) => ({
    runLabel: s.label,
    ticks: s.loaded.ticks,
    metric: (d: { avg_rent: number }) => d.avg_rent,
    highlightDistrict: s.loaded.meta.scenario.policies.find((p) => p.type === "rent_cap")?.district ?? null,
  }));
  rentChart.setData(rentSpecs, policies);

  const unemploymentSpecs = slots.map((s) => ({
    runLabel: s.label,
    ticks: s.loaded.ticks,
    metric: (d: { unemployment_rate: number }) => d.unemployment_rate,
    highlightDistrict: s.loaded.meta.scenario.policies.find((p) => p.type === "rent_cap")?.district ?? null,
  }));
  unemploymentChart.setData(unemploymentSpecs, policies);

  const touristSpecs = slots.map((s) => ({
    runLabel: s.label,
    ticks: s.loaded.ticks,
    metric: (d: { tourist_units?: number }) => d.tourist_units ?? 0,
  }));
  touristChart.setData(touristSpecs, policies);

  const shopsSpecs = slots.map((s) => ({
    runLabel: s.label,
    ticks: s.loaded.ticks,
    metric: (d: { shops_open?: number }) => d.shops_open ?? 0,
  }));
  shopsChart.setData(shopsSpecs, policies);

  if (primary) {
    modeShareChart.setData(primary.loaded.ticks, policies);
    migrationChart.setData(primary.loaded.ticks);
  }
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
    s.mapView.setAgentState(frame.home, frame.employed, frame.satisfaction, frame.active);
    if (stepSize === 1 && state.tickIndex >= 1) {
      const rec = s.loaded.ticks[state.tickIndex - 1];
      if (rec) {
        for (const mv of rec.moves) {
          if (mv.src !== mv.dst) s.mapView.animateMove(mv.agent_id, mv.dst);
        }
        for (const a of rec.arrivals ?? []) {
          s.mapView.animateArrival(a.id, a.home);
        }
        for (const id of rec.departures ?? []) {
          s.mapView.animateDeparture(id);
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
    const cursor = Math.max(0, state.tickIndex - 1);
    rentChart.setCursor(cursor);
    unemploymentChart.setCursor(cursor);
    touristChart.setCursor(cursor);
    shopsChart.setCursor(cursor);
    modeShareChart.setCursor(cursor);
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
  touristChart.resize();
  shopsChart.resize();
  modeShareChart.resize();
  migrationChart.resize();
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

  // `?run=A&compare=B` opens compare mode directly on load, in both record mode and the normal
  // interactive app — not only when `record=1` is also set.
  const urlParams = new URLSearchParams(window.location.search);
  const urlRun = urlParams.get("run") ?? record.run;
  const urlCompare = urlParams.get("compare") ?? record.compare;
  const initialRunId = (urlRun && runIndex.some((r) => r.run_id === urlRun)) ? urlRun : base?.run_id ?? "";
  const initialCompareId = (urlCompare && runIndex.some((r) => r.run_id === urlCompare)) ? urlCompare : null;

  if (initialRunId) runSelect.value = initialRunId;
  if (initialCompareId) compareSelect.value = initialCompareId;

  compareMode = !!initialCompareId;
  if (compareMode) {
    compareToggle.classList.add("active");
    compareSelect.style.display = "inline-block";
  }
  await setup(initialRunId, initialCompareId);
}

void boot();
