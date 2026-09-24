/** ?record=1&run=<id>&compare=<id>&speed=4 — fixed 1920x1080, no controls, autoplay, title card. */

export interface RecordParams {
  enabled: boolean;
  run: string | null;
  compare: string | null;
  speed: number;
  title: string | null;
}

export function parseRecordParams(search: string = window.location.search): RecordParams {
  const p = new URLSearchParams(search);
  return {
    enabled: p.get("record") === "1",
    run: p.get("run"),
    compare: p.get("compare"),
    speed: Number(p.get("speed") ?? "4") || 4,
    title: p.get("title"),
  };
}

const DEFAULT_TITLE = "What happens if Gràcia caps rents?";

/** Builds the footer/subtitle line honestly: when the run's decisions came from the mock
 * provider (no real LLM calls), it must say "simulated by a mock", not "decisions by Jev" —
 * staying accurate matters more than the punchier phrasing. */
export function footerText(provider: string | null | undefined): string {
  if (provider && provider !== "mock") {
    return `Simulation — not a forecast · decisions by Jev (${provider})`;
  }
  return "Simulation — not a forecast · decisions simulated by a mock (not Jev)";
}

function subtitleText(provider: string | null | undefined): string {
  if (provider && provider !== "mock") return `1,000 AI citizens · decisions by Jev (${provider})`;
  return "1,000 AI citizens · decisions simulated by a mock, not Jev";
}

/** Builds the title-card overlay (fades after ~4s) and footer/date-ticker DOM, appended to `root`. */
export function mountRecordOverlay(
  root: HTMLElement,
  opts: { title?: string; subtitle?: string; provider?: string | null },
): { setDate: (isoDate: string) => void } {
  const titleCard = document.createElement("div");
  titleCard.className = "record-title-card";
  titleCard.innerHTML = `
    <div class="record-title">${opts.title ?? DEFAULT_TITLE}</div>
    <div class="record-subtitle">${opts.subtitle ?? subtitleText(opts.provider)}</div>
  `;
  root.appendChild(titleCard);
  // Fade out after a beat, then remove from flow entirely.
  window.setTimeout(() => titleCard.classList.add("fade-out"), 3200);
  window.setTimeout(() => titleCard.remove(), 4400);

  const dateTicker = document.createElement("div");
  dateTicker.className = "record-date-ticker";
  root.appendChild(dateTicker);

  const footer = document.createElement("div");
  footer.className = "record-footer";
  footer.textContent = footerText(opts.provider);
  root.appendChild(footer);

  return {
    setDate(isoDate: string) {
      dateTicker.textContent = isoDate;
    },
  };
}
