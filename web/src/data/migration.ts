/** Aggregates per-tick arrivals/departures into per-month totals, for the arrivals-vs-departures
 * bar chart. A "month" here is the calendar month of the tick's ISO date (not a fixed 30-tick
 * window), so it lines up with the date axis other charts use. */

import type { TickRecord } from "./types";

export interface MonthlyMigration {
  /** "2026-01" */
  month: string;
  arrivals: number;
  departures: number;
}

export function monthlyMigration(ticks: TickRecord[]): MonthlyMigration[] {
  const byMonth = new Map<string, MonthlyMigration>();
  for (const t of ticks) {
    const month = t.date.slice(0, 7);
    let bucket = byMonth.get(month);
    if (!bucket) {
      bucket = { month, arrivals: 0, departures: 0 };
      byMonth.set(month, bucket);
    }
    bucket.arrivals += t.arrivals?.length ?? 0;
    bucket.departures += t.departures?.length ?? 0;
  }
  return [...byMonth.values()].sort((a, b) => (a.month < b.month ? -1 : a.month > b.month ? 1 : 0));
}
