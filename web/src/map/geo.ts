/**
 * Deterministic point-in-polygon sampling: place agent dots at a reproducible pseudo-random
 * point inside their home district polygon, seeded by agent id so the layout is stable across
 * reloads/frames (only "breathing" jitter is added on top, in the render layer).
 */

export type Ring = [number, number][]; // [lon, lat][]
export type Polygon = Ring[]; // outer ring + holes
export type MultiPolygon = Polygon[];

/** Mulberry32 — small, fast, deterministic PRNG seeded by a 32-bit integer. */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return function next() {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Derive a stable 32-bit seed from an agent id (and optional salt) for mulberry32. */
export function seedFromId(agentId: number, salt = 0): number {
  let h = (agentId * 2654435761) ^ salt;
  h = Math.imul(h ^ (h >>> 16), 0x45d9f3b);
  h = Math.imul(h ^ (h >>> 16), 0x45d9f3b);
  h ^= h >>> 16;
  return h >>> 0;
}

/** Ray-casting point-in-polygon test against the outer ring (holes ignored — none in practice). */
export function pointInRing(pt: [number, number], ring: Ring): boolean {
  const [x, y] = pt;
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i]!;
    const [xj, yj] = ring[j]!;
    const intersects = yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi;
    if (intersects) inside = !inside;
  }
  return inside;
}

export function pointInPolygon(pt: [number, number], polygon: Polygon): boolean {
  if (!pointInRing(pt, polygon[0]!)) return false;
  for (let h = 1; h < polygon.length; h++) {
    if (pointInRing(pt, polygon[h]!)) return false; // inside a hole
  }
  return true;
}

function ringBBox(ring: Ring): [number, number, number, number] {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const [x, y] of ring) {
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  }
  return [minX, minY, maxX, maxY];
}

/**
 * Deterministic point inside `polygon`, seeded by `agentId`, via rejection sampling within the
 * bounding box. Falls back to the bbox centroid after `maxAttempts` (degenerate polygons only).
 */
export function samplePointInPolygon(
  polygon: Polygon,
  agentId: number,
  maxAttempts = 64,
): [number, number] {
  const rand = mulberry32(seedFromId(agentId));
  const [minX, minY, maxX, maxY] = ringBBox(polygon[0]!);
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    const x = minX + rand() * (maxX - minX);
    const y = minY + rand() * (maxY - minY);
    if (pointInPolygon([x, y], polygon)) return [x, y];
  }
  return [(minX + maxX) / 2, (minY + maxY) / 2];
}

/** Normalize a GeoJSON Polygon/MultiPolygon geometry to MultiPolygon coordinate form. */
export function toMultiPolygon(geometry: {
  type: "Polygon" | "MultiPolygon";
  coordinates: unknown;
}): MultiPolygon {
  if (geometry.type === "Polygon") {
    return [geometry.coordinates as Polygon];
  }
  return geometry.coordinates as MultiPolygon;
}

/** Sample a point inside a (possibly multi-part) polygon, picking a part deterministically. */
export function samplePointInMultiPolygon(mp: MultiPolygon, agentId: number): [number, number] {
  if (mp.length === 1) return samplePointInPolygon(mp[0]!, agentId);
  const rand = mulberry32(seedFromId(agentId, 0x9e3779b9));
  const partIdx = Math.floor(rand() * mp.length);
  return samplePointInPolygon(mp[partIdx]!, agentId);
}
