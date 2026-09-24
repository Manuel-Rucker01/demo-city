/** Formatting helpers shared by KPI tiles, charts, and the recording overlay. */

const numberFmt = new Intl.NumberFormat("en-US");
const tabularNumberFmt = new Intl.NumberFormat("en-US", { useGrouping: true });

export function formatInt(n: number): string {
  return numberFmt.format(Math.round(n));
}

export function formatPercent(frac: number, digits = 1): string {
  return `${(frac * 100).toFixed(digits)}%`;
}

export function formatCurrencyEUR(n: number, digits = 0): string {
  return `€${tabularNumberFmt.format(Number(n.toFixed(digits)))}`;
}

/** Compact currency for KPI tiles: €1.2k, €340. */
export function formatCurrencyCompact(n: number): string {
  if (Math.abs(n) >= 1000) {
    return `€${(n / 1000).toFixed(1)}k`;
  }
  return `€${Math.round(n)}`;
}

export function formatUsdCost(usd: number, estimated: boolean): string {
  const value = usd < 1 ? usd.toFixed(4) : usd.toFixed(2);
  return estimated ? `$${value} est.` : `$${value}`;
}

/** Compact token counts: 1,234,567 -> "1.2M", 45,000 -> "45.0k". */
export function formatTokensCompact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(Math.round(n));
}

/** ISO date "2026-03-14" -> "14 Mar 2026". */
export function formatDateHuman(isoDate: string): string {
  const d = new Date(`${isoDate}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return isoDate;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

/** "jev-1.13.0+mock" (x13464) style models_seen map -> short "jev-1.13.0+mock" label(s), e.g.
 * for a small footer/KPI readout. Picks the most-used model(s); joins ties with " + ". */
export function formatModelsSeen(modelsSeen: Record<string, number> | undefined): string {
  if (!modelsSeen) return "";
  const entries = Object.entries(modelsSeen).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return "";
  return entries.map(([name]) => name).join(" + ");
}

export function districtDisplayName(id: string): string {
  const names: Record<string, string> = {
    ciutat_vella: "Ciutat Vella",
    eixample: "Eixample",
    gracia: "Gràcia",
    sant_marti: "Sant Martí",
    nou_barris: "Nou Barris",
  };
  return names[id] ?? id;
}
