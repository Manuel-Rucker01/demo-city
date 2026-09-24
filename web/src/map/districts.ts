/** Loads public/data/districts.geojson and precomputes deterministic agent dot positions. */

import type { AgentSnapshot, DistrictId } from "../data/types";
import { samplePointInMultiPolygon, toMultiPolygon, type MultiPolygon } from "./geo";

export interface DistrictFeature {
  id: DistrictId;
  name: string;
  multiPolygon: MultiPolygon;
  raw: GeoJSON.Feature;
}

export interface DistrictsGeo {
  featureCollection: GeoJSON.FeatureCollection;
  byId: Map<DistrictId, DistrictFeature>;
}

export async function loadDistrictsGeo(): Promise<DistrictsGeo> {
  const res = await fetch("/data/districts.geojson");
  if (!res.ok) throw new Error(`Failed to load districts.geojson: ${res.status}`);
  const fc = (await res.json()) as GeoJSON.FeatureCollection;
  const byId = new Map<DistrictId, DistrictFeature>();
  for (const f of fc.features) {
    const id = (f.properties as Record<string, unknown> | null)?.id as DistrictId | undefined;
    if (!id) continue;
    const geom = f.geometry as { type: "Polygon" | "MultiPolygon"; coordinates: unknown };
    byId.set(id, {
      id,
      name: (f.properties as Record<string, unknown>)?.name as string | undefined ?? id,
      multiPolygon: toMultiPolygon(geom),
      raw: f,
    });
  }
  return { featureCollection: fc, byId };
}

/** Precompute one deterministic [lon, lat] per agent, seeded by agent id, inside their home district. */
export function precomputeAgentPositions(
  agents: AgentSnapshot[],
  geo: DistrictsGeo,
): Float64Array {
  const positions = new Float64Array(agents.length * 2);
  for (let i = 0; i < agents.length; i++) {
    const a = agents[i]!;
    const feature = geo.byId.get(a.home);
    if (!feature) continue;
    const [lon, lat] = samplePointInMultiPolygon(feature.multiPolygon, a.id);
    positions[i * 2] = lon;
    positions[i * 2 + 1] = lat;
  }
  return positions;
}

/**
 * Precompute a position for every agent inside EVERY district (not just home), so that when an
 * agent moves we already have a deterministic destination point to animate towards without
 * resampling (keeps positions stable/reproducible across a run).
 */
export function precomputeAllDistrictPositions(
  agentIds: number[],
  geo: DistrictsGeo,
  districtIds: DistrictId[],
): Map<DistrictId, Float64Array> {
  const out = new Map<DistrictId, Float64Array>();
  for (const did of districtIds) {
    const feature = geo.byId.get(did);
    const arr = new Float64Array(agentIds.length * 2);
    if (feature) {
      for (let i = 0; i < agentIds.length; i++) {
        const [lon, lat] = samplePointInMultiPolygon(feature.multiPolygon, agentIds[i]!);
        arr[i * 2] = lon;
        arr[i * 2 + 1] = lat;
      }
    }
    out.set(did, arr);
  }
  return out;
}
