/** Shared design tokens: district palette, satisfaction ramp, base colors. Single source of truth
 * for both CSS (via style/tokens.css) and deck.gl/ECharts layers (which need raw RGB arrays). */

import type { DistrictId } from "../data/types";

export const BG = "#07090d";
export const BG_PANEL = "#0d1017";
export const BG_PANEL_2 = "#12151d";
export const FG = "#eef1f7";
export const FG_DIM = "#8b93a7";
export const BORDER = "#1e222c";
export const ACCENT = "#4dabf7";

// Five distinct, dark-mode-legible hues, one per district (WCAG-checked against #07090d: all >4.5:1).
export const DISTRICT_COLORS: Record<DistrictId, string> = {
  ciutat_vella: "#ff6b6b", // coral red
  eixample: "#4dabf7", // sky blue
  gracia: "#ffd43b", // amber — the rent-cap district, kept visually prominent
  sant_marti: "#38d9a9", // teal
  nou_barris: "#cc5de8", // magenta/purple
};

export function hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

export const DISTRICT_COLORS_RGB: Record<DistrictId, [number, number, number]> = Object.fromEntries(
  Object.entries(DISTRICT_COLORS).map(([id, hex]) => [id, hexToRgb(hex)]),
) as Record<DistrictId, [number, number, number]>;

// Perceptually-uniform-ish sequential ramp (viridis stops) used for satisfaction / metric fills.
const VIRIDIS_STOPS: [number, number, number][] = [
  [68, 1, 84],
  [72, 40, 120],
  [62, 74, 137],
  [49, 104, 142],
  [38, 130, 142],
  [31, 158, 137],
  [53, 183, 121],
  [109, 205, 89],
  [180, 222, 44],
  [253, 231, 37],
];

export function viridis(t: number): [number, number, number] {
  const clamped = Math.max(0, Math.min(1, t));
  const scaled = clamped * (VIRIDIS_STOPS.length - 1);
  const i0 = Math.floor(scaled);
  const i1 = Math.min(i0 + 1, VIRIDIS_STOPS.length - 1);
  const f = scaled - i0;
  const a = VIRIDIS_STOPS[i0]!;
  const b = VIRIDIS_STOPS[i1]!;
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
}

export function viridisCss(t: number): string {
  const [r, g, b] = viridis(t);
  return `rgb(${r | 0}, ${g | 0}, ${b | 0})`;
}

export const EMPLOYED_COLOR: [number, number, number] = [77, 171, 247]; // blue
export const UNEMPLOYED_COLOR: [number, number, number] = [255, 107, 107]; // red
