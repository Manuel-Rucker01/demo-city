import { describe, expect, it } from "vitest";
import { mulberry32, pointInPolygon, samplePointInMultiPolygon, samplePointInPolygon, seedFromId } from "./geo";
import type { Polygon } from "./geo";

// A simple square around Gràcia's rough centroid, plus a pentagon, to exercise both shapes.
const SQUARE: Polygon = [
  [
    [2.14, 41.39],
    [2.17, 41.39],
    [2.17, 41.42],
    [2.14, 41.42],
    [2.14, 41.39],
  ],
];

const PENTAGON: Polygon = [
  [
    [2.15, 41.40],
    [2.17, 41.395],
    [2.18, 41.41],
    [2.16, 41.42],
    [2.14, 41.41],
    [2.15, 41.40],
  ],
];

describe("mulberry32", () => {
  it("is deterministic for the same seed", () => {
    const a = mulberry32(123);
    const b = mulberry32(123);
    const seqA = [a(), a(), a()];
    const seqB = [b(), b(), b()];
    expect(seqA).toEqual(seqB);
  });

  it("produces values in [0, 1)", () => {
    const r = mulberry32(7);
    for (let i = 0; i < 100; i++) {
      const v = r();
      expect(v).toBeGreaterThanOrEqual(0);
      expect(v).toBeLessThan(1);
    }
  });
});

describe("pointInPolygon", () => {
  it("detects a point inside a square", () => {
    expect(pointInPolygon([2.155, 41.405], SQUARE)).toBe(true);
  });

  it("detects a point outside a square", () => {
    expect(pointInPolygon([2.2, 41.405], SQUARE)).toBe(false);
  });
});

describe("samplePointInPolygon", () => {
  it("is deterministic for the same agent id", () => {
    const p1 = samplePointInPolygon(SQUARE, 42);
    const p2 = samplePointInPolygon(SQUARE, 42);
    expect(p1).toEqual(p2);
  });

  it("gives different agents different points (with high probability)", () => {
    const p1 = samplePointInPolygon(SQUARE, 1);
    const p2 = samplePointInPolygon(SQUARE, 2);
    expect(p1).not.toEqual(p2);
  });

  it("always returns a point inside the polygon, for many agent ids and shapes", () => {
    for (const polygon of [SQUARE, PENTAGON]) {
      for (let id = 0; id < 500; id++) {
        const pt = samplePointInPolygon(polygon, id);
        expect(pointInPolygon(pt, polygon)).toBe(true);
      }
    }
  });
});

describe("samplePointInMultiPolygon", () => {
  it("is deterministic and stays inside one of the parts", () => {
    const mp = [SQUARE, PENTAGON];
    for (let id = 0; id < 100; id++) {
      const pt1 = samplePointInMultiPolygon(mp, id);
      const pt2 = samplePointInMultiPolygon(mp, id);
      expect(pt1).toEqual(pt2);
      const insideAny = pointInPolygon(pt1, SQUARE) || pointInPolygon(pt1, PENTAGON);
      expect(insideAny).toBe(true);
    }
  });
});

describe("seedFromId", () => {
  it("is deterministic and varies with agent id", () => {
    expect(seedFromId(5)).toBe(seedFromId(5));
    expect(seedFromId(5)).not.toBe(seedFromId(6));
  });
});
