import { describe, expect, it } from "vitest";
import { BG, DISTRICT_COLORS, hexToRgb } from "./theme";
import { DISTRICT_IDS } from "../data/types";

/** Relative luminance per WCAG 2.x (sRGB). */
function relativeLuminance([r, g, b]: [number, number, number]): number {
  const chan = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  const [rl, gl, bl] = [chan(r), chan(g), chan(b)];
  return 0.2126 * rl + 0.7152 * gl + 0.0722 * bl;
}

function contrastRatio(a: [number, number, number], b: [number, number, number]): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  const [lighter, darker] = la >= lb ? [la, lb] : [lb, la];
  return (lighter + 0.05) / (darker + 0.05);
}

function rgbDistance(a: [number, number, number], b: [number, number, number]): number {
  return Math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2);
}

describe("district palette", () => {
  it("has one color for every one of the 10 districts", () => {
    for (const id of DISTRICT_IDS) {
      expect(DISTRICT_COLORS[id]).toBeDefined();
    }
    expect(Object.keys(DISTRICT_COLORS)).toHaveLength(10);
  });

  it("every district color is legible against the dark app background", () => {
    const bg = hexToRgb(BG);
    for (const id of DISTRICT_IDS) {
      const rgb = hexToRgb(DISTRICT_COLORS[id]!);
      const ratio = contrastRatio(rgb, bg);
      expect(ratio, `${id} (${DISTRICT_COLORS[id]}) contrast vs bg`).toBeGreaterThanOrEqual(2.5);
    }
  });

  it("keeps the original 5 districts' colors unchanged", () => {
    expect(DISTRICT_COLORS.ciutat_vella).toBe("#ff6b6b");
    expect(DISTRICT_COLORS.eixample).toBe("#4dabf7");
    expect(DISTRICT_COLORS.gracia).toBe("#ffd43b");
    expect(DISTRICT_COLORS.sant_marti).toBe("#38d9a9");
    expect(DISTRICT_COLORS.nou_barris).toBe("#cc5de8");
  });

  it("all 10 colors are pairwise distinct enough to tell apart at a glance", () => {
    const ids = [...DISTRICT_IDS];
    for (let i = 0; i < ids.length; i++) {
      for (let j = i + 1; j < ids.length; j++) {
        const a = hexToRgb(DISTRICT_COLORS[ids[i]!]!);
        const b = hexToRgb(DISTRICT_COLORS[ids[j]!]!);
        const dist = rgbDistance(a, b);
        expect(dist, `${ids[i]} vs ${ids[j]} rgb distance`).toBeGreaterThan(45);
      }
    }
  });
});
