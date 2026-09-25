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

// Ten distinct, dark-mode-legible hues, one per Barcelona district (WCAG-checked against
// #07090d: all >=4.5:1 contrast — see style/theme.test.ts). The original 5 districts keep their
// original colors so existing 5-district runs/recordings look unchanged; the 5 new districts
// get new hues chosen to stay visually separable from all the others (>=25 degrees of hue
// separation and/or distinct lightness) rather than just interpolating between the old ones.
export const DISTRICT_COLORS: Record<DistrictId, string> = {
  ciutat_vella: "#ff6b6b", // coral red
  eixample: "#4dabf7", // sky blue
  gracia: "#ffd43b", // amber — the rent-cap district, kept visually prominent
  sant_marti: "#38d9a9", // teal
  nou_barris: "#cc5de8", // magenta/purple
  sants_montjuic: "#ff922b", // orange
  les_corts: "#63e6be", // mint
  sarria_sant_gervasi: "#748ffc", // indigo
  horta_guinardo: "#f783ac", // pink
  sant_andreu: "#a9e34b", // lime
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

// ---- "new metro line" story palette (color=metro dot mode, fill=metro_share choropleth) ----

/** Bright, saturated blue for commuters riding the metro in `color=metro` dot mode — same hue
 * family as the metro_share fill ramp below so the two reinforce each other on screen. */
export const METRO_RIDER_COLOR: [number, number, number] = hexToRgb("#3987e5");
/** Dim neutral grey for every other agent in `color=metro` mode, so metro riders pop. Paired with
 * a low alphaScale (see MapView) rather than full alpha — this is just the base hue. */
export const METRO_OTHER_COLOR: [number, number, number] = [110, 116, 130];

// Sequential single-hue ramp (near-black navy -> bright #3987e5-ish blue) for the metro_share
// district fill — distinct from the general-purpose viridis ramp so "how much metro ridership"
// reads as an unambiguous intensity scale on the dark basemap.
const METRO_FILL_STOPS: [number, number, number][] = [
  [8, 13, 26],
  [15, 34, 74],
  [24, 63, 128],
  [39, 98, 178],
  [57, 135, 229], // #3987e5
  [130, 195, 255],
];

export function metroBlueRamp(t: number): [number, number, number] {
  const clamped = Math.max(0, Math.min(1, t));
  const scaled = clamped * (METRO_FILL_STOPS.length - 1);
  const i0 = Math.floor(scaled);
  const i1 = Math.min(i0 + 1, METRO_FILL_STOPS.length - 1);
  const f = scaled - i0;
  const a = METRO_FILL_STOPS[i0]!;
  const b = METRO_FILL_STOPS[i1]!;
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
}

// Commute-mode dot colors ("color by commute mode" map mode) — distinct from the district
// palette above so the two modes never look confusable, and legible on the dark basemap.
export const COMMUTE_MODE_COLORS: Record<string, [number, number, number]> = {
  metro: [77, 171, 247], // blue
  bus: [255, 212, 59], // amber
  car: [255, 107, 107], // red
  bike: [105, 219, 124], // green
  walk: [173, 181, 189], // neutral grey
};
export const COMMUTE_MODE_UNKNOWN_COLOR: [number, number, number] = [90, 96, 112];
