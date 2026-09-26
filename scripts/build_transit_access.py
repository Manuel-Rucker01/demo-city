"""Build data/processed/transit_access.json from data/raw/transit/ (see fetch_transit_raw.py).

Owner: T12 (transit data). Contract: docs/TRANSIT_ACCESS.md section 1. Do not change
src/jevcity/types.py or src/jevcity/world/network.py (owned by T3).

Usage:
    uv run python scripts/build_transit_access.py [--refresh] [--no-fetch]

Produces:
    data/processed/transit_access.json          (TransitAccess JSON document)
    data/processed/transit_access_report.md     (sanity report)
    media/l9_access_map.png                     (coverage map)

Also appends a "Transit access (T12)" section to data/processed/SOURCES.md the first time it is
missing (idempotent).
"""

from __future__ import annotations

import argparse
import csv
import heapq
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fetch_opendata import (
    parse_wkt_polygon_rings,
    polygon_centroid,
    utm_to_wgs84,
)

from jevcity.types import BCN_DISTRICTS, Station, TransitAccess, Zone

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "transit"
PROCESSED = ROOT / "data" / "processed"
MEDIA = ROOT / "media"

CODI_TO_DISTRICT = {f"{i:02d}": BCN_DISTRICTS[i - 1] for i in range(1, 11)}

ACCESS_DATE = "2026-09-25"

# --- Travel-time model constants (docs/TRANSIT_ACCESS.md "Travel-time model") ------------------
#
# Bike and walk times are elevation-aware (added to make bike stop dominating every commute --
# Barcelona's real bike mode share is ~3% per EMEF 2024, but crow-fly-only bike times made it the
# fastest mode in ~83% of OD pairs, since the flat model ignored the climb from the sea up to
# Collserola). Both legs use net ascent only: max(0, elevation[destination] - elevation[origin]),
# in the direction of travel -- descents get no time bonus (real cyclists/walkers do not save time
# going downhill the way they lose it going uphill; braking/care on descents roughly cancels the
# saved effort). Elevation is looked up per point (census-section population point, zone centroid,
# station) from data/raw/transit/elevations_eudem25m.json -- see `fetch_elevations()` and
# `sources["elevation"]`.
ASSUMPTIONS: dict[str, float | str] = {
    "walk_detour_factor": 1.3,
    "walk_speed_kmh": 4.8,
    "walk_climb_m_per_min": 10.0,  # Naismith-style: +1 min per 10m of net ascent
    "bike_detour_factor": 1.3,
    "bike_speed_kmh": 13.0,  # urban average with traffic lights (lowered from 14; the extra 5min
    # fixed time now also covers Bicing dock/parking at both ends, not just unlock/lock)
    "bike_fixed_min": 5.0,  # unlock/lock + dock or parking, both ends
    "bike_climb_m_per_min": 6.0,  # casual rider climbing ~360 vertical m/hour
    "car_detour_factor": 1.3,
    "car_speed_kmh": 20.0,
    "car_fixed_min": 8.0,
    "bus_access_min": 3.0,
    "bus_wait_min": 5.0,
    "bus_detour_factor": 1.3,
    "bus_speed_kmh": 11.0,
    "bus_egress_min": 3.0,
    "bus_note": "no Barcelona bus route/headway data used; a flat access+wait+crow-fly+egress model, as stated in the contract",
    "rail_stop_detour_factor": 1.15,
    "rail_walk_detour_factor": 1.3,
    "rail_walk_speed_kmh": 4.8,
    "rail_access_radius_m": 1500,
    "rail_access_max_stations": 5,
    "rail_transfer_walk_min": 5.0,
    "coverage_radius_m": 600,
    "speed_metro_kmh": 27.0,
    "speed_fgc_kmh": 30.0,
    "speed_tram_kmh": 18.0,
    "speed_rodalies_kmh": 40.0,
    "headway_metro_min": 4.0,
    "headway_l9l10_min": 6.0,
    "headway_fgc_min": 6.0,
    "headway_tram_min": 8.0,
    "headway_rodalies_min": 15.0,
    "logit_beta_minutes": -0.1,
    "walk_max_considered_min": 40.0,
}

METRO_LINES = {
    f"L{n}" for n in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
} | {"L9N", "L9S", "L10N", "L10S"}
FGC_SUBWAY_LINES = {"L6", "L7", "L8", "L12"}  # already tagged route=subway in OSM


def line_kind(line: str) -> str:
    if line in ("L9N", "L9S", "L10N", "L10S", "L9", "L10"):
        return "l9l10"
    if line in FGC_SUBWAY_LINES or line in ("S1", "S2"):
        return "fgc"
    if line.startswith("T"):
        return "tram"
    if line.startswith("R") or line in ("RG1", "RT2"):
        return "rodalies"
    return "metro"


SPEED_KMH = {
    "metro": ASSUMPTIONS["speed_metro_kmh"],
    "l9l10": ASSUMPTIONS["speed_metro_kmh"],
    "fgc": ASSUMPTIONS["speed_fgc_kmh"],
    "tram": ASSUMPTIONS["speed_tram_kmh"],
    "rodalies": ASSUMPTIONS["speed_rodalies_kmh"],
}
HEADWAY_MIN = {
    "metro": ASSUMPTIONS["headway_metro_min"],
    "l9l10": ASSUMPTIONS["headway_l9l10_min"],
    "fgc": ASSUMPTIONS["headway_fgc_min"],
    "tram": ASSUMPTIONS["headway_tram_min"],
    "rodalies": ASSUMPTIONS["headway_rodalies_min"],
}


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# =================================================================================================
# 0. Elevation (OpenTopoData EU-DEM 25m; cached in data/raw/transit/, never invented)
# =================================================================================================

ELEVATION_SOURCE = "OpenTopoData eudem25m (Copernicus EU-DEM v1.1, ~25m resolution)"
ELEVATION_URL = "https://api.opentopodata.org/v1/eudem25m"
# eudem25m returns null for a handful of points right on Barcelona's coastline/port breakwaters
# (masked as sea in that dataset). aster30m (NASA/METI ASTER GDEM, ~30m) has real land/harbour
# coverage there and returns 0m, which is plausible for sea-level port/beach points; used only as
# a fallback for points eudem25m can't answer, never as the primary source.
ELEVATION_FALLBACK_URL = "https://api.opentopodata.org/v1/aster30m"
ELEVATION_FALLBACK_SOURCE = "OpenTopoData aster30m (NASA/METI ASTER GDEM, ~30m resolution)"
ELEV_CACHE_PATH_NAME = "elevations_eudem25m.json"
ELEV_UA = "jevcity-research/1.0 (contact: claudecodemail2026@gmail.com)"


def elev_key(lon: float, lat: float) -> str:
    """Cache key at 5-decimal precision (~1.1m), matching the rounding already used for station
    and zone-centroid coordinates elsewhere in this file."""
    return f"{round(lat, 5)},{round(lon, 5)}"


def fetch_elevations(
    points: list[tuple[float, float]], no_fetch: bool = False
) -> dict[str, float]:
    """points: list of (lon, lat). Returns {elev_key(lon,lat): elevation_m} for every requested
    point, backed by a persistent cache at data/raw/transit/elevations_eudem25m.json so re-running
    the build doesn't re-fetch. Missing points are fetched from OpenTopoData in batches of <=100
    (its documented per-request max), 1 request/second (its documented rate limit). Never
    fabricates a value: if points are missing from the cache and `no_fetch` is set, raises."""
    cache_path = RAW / ELEV_CACHE_PATH_NAME
    cache: dict[str, float] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())

    wanted = {elev_key(lon, lat): (lon, lat) for lon, lat in points}
    missing = {k: v for k, v in wanted.items() if k not in cache}
    if missing:
        if no_fetch:
            raise RuntimeError(
                f"{len(missing)} elevation points are missing from {cache_path} and --no-fetch "
                "was passed; run without --no-fetch at least once to populate the cache."
            )
        print(f"[build] fetching {len(missing)} elevations from OpenTopoData ({ELEVATION_SOURCE})...")
        items = list(missing.items())
        nulls: list[tuple[str, tuple[float, float]]] = []
        with httpx.Client(timeout=30.0, headers={"User-Agent": ELEV_UA}) as client:
            for i in range(0, len(items), 100):
                batch = items[i : i + 100]
                locs = "|".join(f"{lat},{lon}" for _, (lon, lat) in batch)
                resp = client.get(ELEVATION_URL, params={"locations": locs})
                resp.raise_for_status()
                data = resp.json()
                results = data["results"]
                assert len(results) == len(batch), "OpenTopoData result count mismatch"
                for (key, latlon), r in zip(batch, results):
                    elev = r["elevation"]
                    if elev is None:
                        nulls.append((key, latlon))  # resolved via the fallback dataset below
                    else:
                        cache[key] = float(elev)
                if i + 100 < len(items):
                    time.sleep(1.0)  # OpenTopoData public instance: 1 request/second

            if nulls:
                # eudem25m masks a handful of Barcelona coastline/port points as sea (no elevation
                # value); resolved with aster30m instead, never invented -- see ELEVATION_FALLBACK_*.
                print(
                    f"[build] {len(nulls)} points had no eudem25m elevation (coastline/port, masked "
                    f"as sea); falling back to {ELEVATION_FALLBACK_SOURCE}..."
                )
                time.sleep(1.0)
                for i in range(0, len(nulls), 100):
                    batch = nulls[i : i + 100]
                    locs = "|".join(f"{lat},{lon}" for _, (lon, lat) in batch)
                    resp = client.get(ELEVATION_FALLBACK_URL, params={"locations": locs})
                    resp.raise_for_status()
                    data = resp.json()
                    results = data["results"]
                    assert len(results) == len(batch), "OpenTopoData result count mismatch"
                    for (key, _), r in zip(batch, results):
                        elev = r["elevation"]
                        if elev is None:
                            raise RuntimeError(
                                f"OpenTopoData returned null elevation for {key} on both "
                                f"{ELEVATION_SOURCE} and the {ELEVATION_FALLBACK_SOURCE} fallback"
                            )
                        cache[key] = float(elev)
                    if i + 100 < len(nulls):
                        time.sleep(1.0)

        cache_path.write_text(json.dumps(cache, sort_keys=True))
        print(f"[build] cached {len(cache)} elevations -> {cache_path}")

    return {k: cache[k] for k in wanted}


# =================================================================================================
# 1. Barris + census sections + population + job proxy
# =================================================================================================


def load_barris() -> list[dict[str, Any]]:
    raw = json.loads((RAW / "barcelonaciutat_barris.json").read_text())
    out = []
    for row in raw:
        out.append(
            {
                "id": row["codi_barri"],
                "name": row["nom_barri"],
                "district": CODI_TO_DISTRICT[row["codi_districte"]],
            }
        )
    out.sort(key=lambda r: r["id"])
    assert len(out) == 73, f"expected 73 barris, got {len(out)}"
    return out


def load_census_sections() -> list[dict[str, Any]]:
    """One row per (barri, seccio censal) with its polygon centroid in WGS84."""
    raw = json.loads((RAW / "barcelonaciutat_seccionscensals.json").read_text())
    out = []
    for row in raw:
        rings = parse_wkt_polygon_rings(row["geometria_etrs89"])
        cx, cy = polygon_centroid(rings[0])
        lon, lat = utm_to_wgs84(cx, cy)
        out.append(
            {
                "district": row["codi_districte"],
                "barri": row["codi_barri"],
                "seccio": row["codi_seccio_censal"],
                "lon": lon,
                "lat": lat,
            }
        )
    return out


def load_section_population() -> dict[tuple[str, str], int]:
    """(codi_districte padded 2, seccio_censal LOCAL padded 3) -> population, summed over EDAT_Q
    bands. The padro's own Seccio_Censal column is district-wide (district_code*1000 + local
    section number, e.g. district 1 section 1 = 1001); BarcelonaCiutat_SeccionsCensals.json's
    codi_seccio_censal is just the local part -- both are district-wide sequential (not reset per
    barri), so the join key is (district, local section), not (barri, section)."""
    pop: dict[tuple[str, str], int] = defaultdict(int)
    with open(RAW / "pad_mdbas_edat-q_2025.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            district = f"{int(row['Codi_Districte']):02d}"
            seccio_local = int(row["Seccio_Censal"]) % 1000
            seccio = f"{seccio_local:03d}"
            raw_val = row["Valor"].strip()
            # small counts are suppressed as ".." in this padro export, same convention as
            # districts.json's car_ownership (rebuts-quota-padro-vehicle-bcn): treated as the
            # midpoint of the plausible [1,4] range.
            val = 2 if raw_val == ".." else int(raw_val)
            pop[(district, seccio)] += val
    return dict(pop)


def load_job_proxy() -> dict[str, int]:
    """Ground-floor commercial premises per barri (codi_barri, 2-digit), vacant premises excluded.
    Proxy for job_weight (docs/TRANSIT_ACCESS.md: 'documented proxy... e.g. ground-floor
    commercial premises')."""
    vacant = {
        "Locals buits en lloguer",
        "Locals buits en venda",
        "Locals buits en venda i lloguer",
    }
    counts: dict[str, int] = defaultdict(int)
    with open(RAW / "cens_comercial_bcn_2024.csv", encoding="latin-1") as f:
        for row in csv.DictReader(f):
            if row.get("Nom_Sector_Activitat") in vacant:
                continue
            try:
                barri = f"{int(row['Codi_Barri']):02d}"
            except (KeyError, ValueError):
                continue
            counts[barri] += 1
    return dict(counts)


# =================================================================================================
# 2. Rail network from OSM (Overpass raw JSON in data/raw/transit)
# =================================================================================================


def load_overpass(name: str) -> dict[str, Any]:
    return json.loads((RAW / name).read_text())


def relations_and_nodes(doc: dict[str, Any]) -> tuple[list[dict], dict[int, dict]]:
    rels = [e for e in doc["elements"] if e["type"] == "relation"]
    nodes = {e["id"]: e for e in doc["elements"] if e["type"] == "node"}
    return rels, nodes


def nearest_named(
    lon: float, lat: float, named: list[tuple[str, float, float]], max_m: float = 350.0
) -> str | None:
    best = None
    best_d = max_m
    for name, nlon, nlat in named:
        d = haversine_m(lon, lat, nlon, nlat)
        if d < best_d:
            best_d = d
            best = name
    return best


class StationRegistry:
    """Merges platforms of the same station (same name, within ~250m) into one node, per
    docs/TRANSIT_ACCESS.md."""

    def __init__(self, merge_radius_m: float = 250.0):
        self.merge_radius_m = merge_radius_m
        # canonical name -> {"lon","lat","lines": set(), "variant": str, "approx": bool, "n": int}
        self.stations: dict[str, dict[str, Any]] = {}

    def add(
        self, name: str, lon: float, lat: float, line: str, variant: str = "base", approx: bool = False
    ) -> str:
        key = self._match(name, lon, lat)
        if key is None:
            key = name
            self.stations[key] = {
                "lon": lon,
                "lat": lat,
                "lines": set(),
                "variant": variant,
                "approx": approx,
                "n": 1,
            }
        else:
            st = self.stations[key]
            # running average position (multiple platform fixes for the same station)
            st["lon"] = (st["lon"] * st["n"] + lon) / (st["n"] + 1)
            st["lat"] = (st["lat"] * st["n"] + lat) / (st["n"] + 1)
            st["n"] += 1
            if variant == "base":
                st["variant"] = "base"  # a station real today is never "new"
        self.stations[key]["lines"].add(line)
        return key

    def _match(self, name: str, lon: float, lat: float) -> str | None:
        if name in self.stations:
            st = self.stations[name]
            if haversine_m(lon, lat, st["lon"], st["lat"]) <= self.merge_radius_m:
                return name
        # fuzzy: same name ignoring case/diacritics-ish already handled by exact OSM names in
        # practice; fall back to proximity-only match against any station within radius/2 to catch
        # minor name punctuation differences (e.g. "Sant Andreu" vs "Barcelona-Sant Andreu").
        for key, st in self.stations.items():
            if haversine_m(lon, lat, st["lon"], st["lat"]) <= self.merge_radius_m / 2:
                return key
        return None


# Real, well-known Barcelona interchanges between operators (TMB metro <-> FGC <-> Rodalies)
# whose OSM names differ by operator convention and whose platform-center coordinates are more
# than 250m apart (checked; see the merge diagnostic run during development), so the automatic
# same-name/proximity merge in StationRegistry does not catch them on its own. Station positions
# are still the real OSM coordinates; this list only says "these two OSM nodes are the same
# real-world interchange complex", hand-encoded from public knowledge of the network, not
# invented. Without this, the graph splits into near-disconnected metro/FGC/Rodalies islands and
# produces implausible (100min+) cross-network rail times.
KNOWN_INTERCHANGE_ALIASES: list[tuple[str, str]] = [
    ("Catalunya", "Barcelona-Plaça Catalunya"),
    ("Diagonal", "Provença"),
    ("Sants Estació", "Barcelona - Sants"),
    ("Espanya", "Barcelona-Plaça Espanya"),
    ("El Clot", "Barcelona-El Clot"),
    ("Fabra i Puig", "Barcelona-Fabra i Puig"),
    ("Gornal", "Bellvitge-Gornal"),
    ("Sant Andreu", "Barcelona-Sant Andreu"),
    ("Torre Baró | Vallbona", "Barcelona-Torre Baró | Vallbona"),
    ("Barceloneta", "Barcelona-Estació de França"),
    ("Passeig de Gràcia", "Barcelona-Passeig de Gràcia"),
]


def merge_known_interchanges(reg: StationRegistry, lines: dict[str, list[str]]) -> None:
    for keep, drop in KNOWN_INTERCHANGE_ALIASES:
        if keep not in reg.stations or drop not in reg.stations:
            continue
        reg.stations[keep]["lines"] |= reg.stations[drop]["lines"]
        if reg.stations[drop]["variant"] == "base":
            reg.stations[keep]["variant"] = "base"
        del reg.stations[drop]
        for line, seq in lines.items():
            lines[line] = [keep if s == drop else s for s in seq]


def build_base_stations() -> tuple[StationRegistry, dict[str, list[str]]]:
    """Returns (registry, {line: [station_key,...] in order}) for the network that exists today:
    metro L1-L11 (+L9N/S,L10N/S), FGC L6/L7/L8/L12 (OSM route=subway), tram T1-T6, plus Rodalies
    and FGC S1/S2 stations connected as documented approximate trunks (see fetch_transit_raw.py
    and REPORT note on Rodalies/S1S2 topology)."""
    reg = StationRegistry()
    lines: dict[str, list[str]] = defaultdict(list)

    # named station points, used to attach real names to anonymous stop-position nodes
    named_docs = [load_overpass("overpass_named_stations.json"), load_overpass("overpass_tramstops.json")]
    named_docs.append(load_overpass("overpass_extra_nodes.json"))
    named: list[tuple[str, float, float]] = []
    for doc in named_docs:
        for e in doc["elements"]:
            nm = e.get("tags", {}).get("name")
            if nm:
                named.append((nm, e["lon"], e["lat"]))

    # metro + FGC (L6/L7/L8/L12): route=subway relations, ordered 'stop' members
    subway_rels, subway_nodes = relations_and_nodes(load_overpass("overpass_subway.json"))
    for rel in subway_rels:
        ref = rel["tags"].get("ref")
        if not ref:
            continue
        seq: list[str] = []
        for m in rel["members"]:
            if m["role"] != "stop" or m["type"] != "node":
                continue
            node = subway_nodes.get(m["ref"])
            if node is None:
                continue
            nm = nearest_named(node["lon"], node["lat"], named, max_m=350.0)
            if nm is None:
                continue
            if seq and seq[-1] == nm:
                continue  # duplicate platform stop for the same station
            seq.append(nm)
        if len(seq) < 2:
            continue
        # merge the two directional relations per line (e.g. "L3: A=>B" and "L3: B=>A") into one
        # station order: keep the longer/first-seen ordering, since both directions list the same
        # stops.
        key_line = ref  # keep directional variants distinct in the dict, merged below
        station_keys = [reg.add(nm, *_lookup(named, nm), key_line, variant="base") for nm in seq]
        if len(station_keys) > len(lines.get(key_line, [])):
            lines[key_line] = station_keys

    # merge directional duplicates (…=>… and …<=…) and L9N/L9S/L10N/L10S pairs into single lines
    merged_lines: dict[str, list[str]] = {}
    seen_refs: set[str] = set()
    for ref, seq in lines.items():
        base_ref = ref
        if base_ref in seen_refs:
            continue
        seen_refs.add(base_ref)
        merged_lines[base_ref] = seq

    # tram T1-T6
    tram_rels, tram_nodes = relations_and_nodes(load_overpass("overpass_tram.json"))
    tram_by_ref: dict[str, list[str]] = {}
    for rel in tram_rels:
        ref = rel["tags"].get("ref")
        if not ref:
            continue
        seq = []
        for m in rel["members"]:
            if m["role"] != "stop" or m["type"] != "node":
                continue
            node = tram_nodes.get(m["ref"])
            if node is None:
                continue
            nm = nearest_named(node["lon"], node["lat"], named, max_m=250.0)
            if nm is None:
                continue
            if seq and seq[-1] == nm:
                continue
            seq.append(nm)
        if len(seq) > len(tram_by_ref.get(ref, [])):
            tram_by_ref[ref] = seq
    for ref, seq in tram_by_ref.items():
        station_keys = [reg.add(nm, *_lookup(named, nm), ref, variant="base") for nm in seq]
        merged_lines[ref] = station_keys

    # Rodalies + FGC S1/S2: no usable OSM route relation (Overpass times out on route=train even
    # with ref/operator filters -- see fetch_transit_raw.py). Built as a single approximate trunk
    # per group, ordered by a greedy nearest-neighbour chain from a known hub. This is a documented
    # simplification: real branching (e.g. R1 vs R4 north of Sagrera) is not modeled, only a
    # plausible consecutive-stop ordering for in-vehicle time estimation.
    rodalies_doc = load_overpass("overpass_rodalies_nodes.json")
    rodalies_pts = [(e["tags"]["name"], e["lon"], e["lat"]) for e in rodalies_doc["elements"]]
    rodalies_pts.append(("Barcelona - Sants", 2.1400042, 41.3790068))
    rodalies_order = _nearest_neighbour_chain(rodalies_pts, start_name="Barcelona - Sants")
    station_keys = [reg.add(nm, lon, lat, "Rodalies", variant="base") for nm, lon, lat in rodalies_order]
    merged_lines["Rodalies"] = station_keys

    fgc_doc = load_overpass("overpass_fgc_nodes.json")
    fgc_pts = [
        (e["tags"]["name"], e["lon"], e["lat"])
        for e in fgc_doc["elements"]
        if e["tags"].get("station") != "funicular"
    ]
    pl_cat = ("Barcelona-Plaça Catalunya", 2.168329, 41.3857711)
    if pl_cat[0] not in [p[0] for p in fgc_pts]:
        fgc_pts.append(pl_cat)
    fgc_order = _nearest_neighbour_chain(fgc_pts, start_name="Barcelona-Plaça Catalunya")
    station_keys = [reg.add(nm, lon, lat, "S1S2", variant="base") for nm, lon, lat in fgc_order]
    merged_lines["S1S2"] = station_keys

    merge_known_interchanges(reg, merged_lines)
    return reg, merged_lines


def _lookup(named: list[tuple[str, float, float]], name: str) -> tuple[float, float]:
    for nm, lon, lat in named:
        if nm == name:
            return lon, lat
    raise KeyError(name)


def _nearest_neighbour_chain(
    pts: list[tuple[str, float, float]], start_name: str
) -> list[tuple[str, float, float]]:
    by_name = {p[0]: p for p in pts}
    remaining = dict(by_name)
    if start_name not in remaining:
        start_name = pts[0][0]
    order = [remaining.pop(start_name)]
    while remaining:
        cur = order[-1]
        nxt = min(remaining.values(), key=lambda p: haversine_m(cur[1], cur[2], p[1], p[2]))
        remaining.pop(nxt[0])
        order.append(nxt)
    return order


# --- L9 central section --------------------------------------------------------------------------

# Coordinates sourced from OSM railway=construction / name-matched nodes (Overpass, see
# fetch_transit_raw.py "overpass_l9_construction.json"), except "Prat de la Riba" (no
# railway=construction node found; placed via Nominatim geocoding of the street it is named
# after -- approx: true) and the existing interchange stations (already in the base registry).
L9_CENTRAL_NEW = {
    "Campus Nord": (2.114267, 41.3892097, False),
    "Manuel Girona": (2.1235937, 41.3909701, False),
    "Mandri": (2.1313204, 41.4052093, False),
    "El Putxet": (2.1390337, 41.4056283, False),
    "Travessera de Dalt": (2.1549530, 41.4095455, False),  # OSM node "Muntanya", railway=proposed
    "Sanllehy": (2.1612686, 41.4136148, False),
    "Guinardó-Hospital de Sant Pau": (2.1743937, 41.4160421, False),
}
# approx: true -- no railway=construction node found; midpoint of the "Prat de la Riba" bus stops
# (OSM names "Pg Sant Joan Bosco - Prat de la Riba" / "Av Sarrià - Prat de la Riba"), confirming
# the street exists there, geocoded street centre cross-checked via Nominatim.
L9_CENTRAL_NEW["Prat de la Riba"] = ((2.1293624 + 2.1308227) / 2, (41.3929304 + 41.392521) / 2, True)

L9_CENTRAL_ORDER = [
    "Zona Universitària",
    "Campus Nord",
    "Manuel Girona",
    "Prat de la Riba",
    "Sarrià",
    "Mandri",
    "El Putxet",
    "Lesseps",
    "Travessera de Dalt",
    "Sanllehy",
    "Guinardó-Hospital de Sant Pau",
    "Plaça de Maragall",
    "La Sagrera",
]

EXISTING_L9_ANCHOR_NAMES = {
    "Zona Universitària": ["Zona Universitària"],
    "Sarrià": ["Sarrià"],
    "Lesseps": ["Lesseps"],
    "Plaça de Maragall": ["Maragall", "Plaça de Maragall"],
    "La Sagrera": ["la Sagrera", "La Sagrera"],
}


def build_l9_central(reg: StationRegistry, base_lines: dict[str, list[str]]) -> dict[str, list[str]]:
    """Returns the l9_central variant's line dict (base_lines plus the new "L9" continuous line);
    also adds the new stations to `reg` with variant="l9_central"."""
    lines = {k: list(v) for k, v in base_lines.items()}
    seq: list[str] = []
    for name in L9_CENTRAL_ORDER:
        if name in L9_CENTRAL_NEW:
            lon, lat, approx = L9_CENTRAL_NEW[name]
            # Insert as a genuinely new, separate node even if it happens to sit within the
            # registry's auto-merge radius of an existing station with the same/similar name (this
            # occurs for "El Putxet" and "Guinardó-Hospital de Sant Pau", which are close to
            # existing FGC/L4 stations of almost the same name): docs/TRANSIT_ACCESS.md only lists
            # Zona Universitaria, Sarria, Lesseps, Placa de Maragall and La Sagrera as becoming
            # interchanges with the new line, so the other 8 new stations are kept as distinct
            # nodes rather than silently merged by the name/proximity heuristic.
            key = name if name not in reg.stations else f"{name} (L9)"
            reg.stations[key] = {
                "lon": lon,
                "lat": lat,
                "lines": {"L9"},
                "variant": "l9_central",
                "approx": approx,
                "n": 1,
            }
        else:
            anchors = EXISTING_L9_ANCHOR_NAMES[name]
            key = None
            for a in anchors:
                if a in reg.stations:
                    key = a
                    break
            if key is None:
                raise KeyError(f"existing L9-central anchor station not found in base registry: {name}")
            reg.stations[key]["lines"].add("L9")
        seq.append(key)
    lines["L9"] = seq
    return lines


# =================================================================================================
# 3. Line graph + shortest paths (Dijkstra over (station, line) states)
# =================================================================================================


def build_time_graph(
    reg: StationRegistry, lines: dict[str, list[str]]
) -> tuple[dict[str, dict[str, float]], list[str]]:
    """Builds a graph over states "station|line" plus a synthetic "station|ENTRY" node per station,
    and returns its adjacency (state -> {state: minutes}), plus the list of physical station keys.
    """
    adj: dict[str, dict[str, float]] = defaultdict(dict)
    station_keys = list(reg.stations.keys())

    def add_edge(a: str, b: str, w: float) -> None:
        if w < adj[a].get(b, math.inf):
            adj[a][b] = w
        if w < adj[b].get(a, math.inf):
            adj[b][a] = w

    for line, seq in lines.items():
        kind = line_kind(line)
        speed = SPEED_KMH[kind]
        headway = HEADWAY_MIN[kind]
        for st in seq:
            entry = f"{st}|ENTRY"
            state = f"{st}|{line}"
            # board: entry -> this line's state, cost = half headway wait
            w = headway / 2
            if w < adj[entry].get(state, math.inf):
                adj[entry][state] = w
            # transfer: any other line already at this station -> this line, 5 min walk + half
            # headway wait of the new line (docs/TRANSIT_ACCESS.md: "5 min transfer penalty +
            # wait")
        # in-vehicle edges between consecutive stops
        for i in range(len(seq) - 1):
            a, b = seq[i], seq[i + 1]
            sa, sb = reg.stations[a], reg.stations[b]
            dist_km = haversine_m(sa["lon"], sa["lat"], sb["lon"], sb["lat"]) / 1000.0
            in_vehicle = dist_km * ASSUMPTIONS["rail_stop_detour_factor"] / speed * 60.0
            add_edge(f"{a}|{line}", f"{b}|{line}", in_vehicle)

    # transfer edges: for every station with >=2 lines, connect each pair of line-states with the
    # 5-minute walking penalty plus the destination line's half-headway wait.
    station_lines: dict[str, set[str]] = defaultdict(set)
    for line, seq in lines.items():
        for st in seq:
            station_lines[st].add(line)
    for st, sls in station_lines.items():
        sls = sorted(sls)
        for i, la in enumerate(sls):
            for lb in sls[i + 1 :]:
                w_b = ASSUMPTIONS["rail_transfer_walk_min"] + HEADWAY_MIN[line_kind(lb)] / 2
                w_a = ASSUMPTIONS["rail_transfer_walk_min"] + HEADWAY_MIN[line_kind(la)] / 2
                add_edge(f"{st}|{la}", f"{st}|{lb}", max(w_a, w_b))
        # entry -> every line at this station (initial board)
        for la in sls:
            w = HEADWAY_MIN[line_kind(la)] / 2
            entry = f"{st}|ENTRY"
            if w < adj[entry].get(f"{st}|{la}", math.inf):
                adj[entry][f"{st}|{la}"] = w

    return dict(adj), station_keys


def dijkstra_from_entries(
    adj: dict[str, dict[str, float]], station_keys: list[str]
) -> dict[str, dict[str, float]]:
    """station -> {station: minutes}, best over all lines/transfers, entry-to-arrival (arrival at
    any line-state of the destination station, no extra egress cost)."""
    result: dict[str, dict[str, float]] = {}
    for src in station_keys:
        entry = f"{src}|ENTRY"
        dist: dict[str, float] = {entry: 0.0}
        pq: list[tuple[float, str]] = [(0.0, entry)]
        visited: set[str] = set()
        while pq:
            d, u = heapq.heappop(pq)
            if u in visited:
                continue
            visited.add(u)
            for v, w in adj.get(u, {}).items():
                nd = d + w
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        best: dict[str, float] = {}
        for state, d in dist.items():
            if "|" not in state or state.endswith("|ENTRY"):
                continue
            st = state.rsplit("|", 1)[0]
            if d < best.get(st, math.inf):
                best[st] = d
        result[src] = best
    return result


# =================================================================================================
# 4. Combine into zone x zone matrices
# =================================================================================================


def walk_min(dist_m: float, elev_gain_m: float = 0.0) -> float:
    """elev_gain_m: net ascent from origin to destination (already clamped to >=0 by the caller,
    but clamped again here defensively -- descents never get a time bonus)."""
    return (
        dist_m / 1000.0 * ASSUMPTIONS["walk_detour_factor"] / ASSUMPTIONS["walk_speed_kmh"] * 60.0
        + max(0.0, elev_gain_m) / ASSUMPTIONS["walk_climb_m_per_min"]
    )


def bike_min(dist_m: float, elev_gain_m: float = 0.0) -> float:
    """elev_gain_m: net ascent from origin to destination; see walk_min."""
    return (
        dist_m / 1000.0 * ASSUMPTIONS["bike_detour_factor"] / ASSUMPTIONS["bike_speed_kmh"] * 60.0
        + max(0.0, elev_gain_m) / ASSUMPTIONS["bike_climb_m_per_min"]
        + ASSUMPTIONS["bike_fixed_min"]
    )


def car_min(dist_m: float) -> float:
    return (
        dist_m / 1000.0 * ASSUMPTIONS["car_detour_factor"] / ASSUMPTIONS["car_speed_kmh"] * 60.0
        + ASSUMPTIONS["car_fixed_min"]
    )


def bus_min(dist_m: float) -> float:
    return (
        ASSUMPTIONS["bus_access_min"]
        + ASSUMPTIONS["bus_wait_min"]
        + dist_m / 1000.0 * ASSUMPTIONS["bus_detour_factor"] / ASSUMPTIONS["bus_speed_kmh"] * 60.0
        + ASSUMPTIONS["bus_egress_min"]
    )


def nearest_stations(
    lon: float, lat: float, station_pts: list[tuple[str, float, float]], k: int, radius_m: float
) -> list[tuple[str, float]]:
    dists = [(name, haversine_m(lon, lat, slon, slat)) for name, slon, slat in station_pts]
    dists = [d for d in dists if d[1] <= radius_m]
    dists.sort(key=lambda d: d[1])
    return dists[:k]


def rail_time_from_point(
    lon: float,
    lat: float,
    dest_lon: float,
    dest_lat: float,
    station_pts: list[tuple[str, float, float]],
    station_times: dict[str, dict[str, float]],
    origin_elev: float,
    dest_elev: float,
    station_elev: dict[str, float],
    origin_near: list[tuple[str, float]] | None = None,
    dest_near: list[tuple[str, float]] | None = None,
) -> float | None:
    """The walking legs at both ends of the rail trip (origin -> boarding station, alighting
    station -> destination) carry the same ascent penalty as a standalone walk trip
    (docs/TRANSIT_ACCESS.md: 'Also apply the ascent penalty to the walking legs to/from rail
    stations inside the metro time')."""
    o_near = origin_near if origin_near is not None else nearest_stations(
        lon, lat, station_pts, ASSUMPTIONS["rail_access_max_stations"], ASSUMPTIONS["rail_access_radius_m"]
    )
    d_near = dest_near if dest_near is not None else nearest_stations(
        dest_lon, dest_lat, station_pts, ASSUMPTIONS["rail_access_max_stations"], ASSUMPTIONS["rail_access_radius_m"]
    )
    if not o_near or not d_near:
        return None
    best = math.inf
    for o_name, o_dist in o_near:
        access = walk_min(o_dist, station_elev.get(o_name, origin_elev) - origin_elev)
        times_from_o = station_times.get(o_name, {})
        for d_name, d_dist in d_near:
            t = times_from_o.get(d_name)
            if t is None:
                continue
            egress = walk_min(d_dist, dest_elev - station_elev.get(d_name, dest_elev))
            total = access + t + egress
            best = min(best, total)
    return None if math.isinf(best) else best


# =================================================================================================
# main
# =================================================================================================


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="re-fetch raw inputs")
    parser.add_argument("--no-fetch", action="store_true", help="skip the fetch step entirely")
    args = parser.parse_args()

    t0 = time.time()

    # snapshot the currently-committed output (if any) before it gets overwritten, purely so the
    # sanity report can show a before/after comparison of this change (elevation-aware bike/walk).
    before_access: TransitAccess | None = None
    before_path = PROCESSED / "transit_access.json"
    if before_path.exists():
        try:
            before_access = TransitAccess.model_validate_json(before_path.read_text())
        except (OSError, ValueError):
            before_access = None

    if not args.no_fetch:
        from fetch_transit_raw import fetch_raw

        fetch_raw(refresh=args.refresh)

    print("[build] loading barris / census sections / population / jobs...")
    barris = load_barris()
    sections = load_census_sections()
    section_pop = load_section_population()

    for s in sections:
        s["population"] = section_pop.get((s["district"], s["seccio"]), 0)

    sections_by_barri: dict[str, list[dict]] = defaultdict(list)
    for s in sections:
        sections_by_barri[s["barri"]].append(s)

    job_counts = load_job_proxy()
    district_job_totals: dict[str, int] = defaultdict(int)
    for b in barris:
        district_job_totals[b["district"]] += job_counts.get(b["id"], 0)

    zones_meta = []
    for b in barris:
        secs = sections_by_barri[b["id"]]
        total_pop = sum(s["population"] for s in secs)
        if total_pop > 0:
            clon = sum(s["lon"] * s["population"] for s in secs) / total_pop
            clat = sum(s["lat"] * s["population"] for s in secs) / total_pop
        elif secs:
            clon = sum(s["lon"] for s in secs) / len(secs)
            clat = sum(s["lat"] for s in secs) / len(secs)
        else:
            raise ValueError(f"barri {b['id']} has no census sections")
        jw = job_counts.get(b["id"], 0) / district_job_totals[b["district"]] if district_job_totals[b["district"]] else 0.0
        zones_meta.append(
            {
                "id": b["id"],
                "name": b["name"],
                "district": b["district"],
                "population": total_pop,
                "job_weight": jw,
                "centroid": (round(clon, 5), round(clat, 5)),
                "sections": secs,
            }
        )

    print(f"[build] {len(zones_meta)} zones, {sum(z['population'] for z in zones_meta):,} residents")

    print("[build] loading rail network from OSM...")
    reg, base_lines = build_base_stations()
    l9_lines = build_l9_central(reg, base_lines)
    print(f"[build] {len(reg.stations)} merged stations, {len(base_lines)} base lines, "
          f"{len(l9_lines)} l9_central lines")

    print("[build] fetching/loading elevations for census points, zone centroids and stations...")
    elev_points: list[tuple[float, float]] = []
    elev_points += [(s["lon"], s["lat"]) for s in sections]
    elev_points += [z["centroid"] for z in zones_meta]
    elev_points += [(st["lon"], st["lat"]) for st in reg.stations.values()]
    elev_cache = fetch_elevations(elev_points, no_fetch=args.no_fetch)
    for s in sections:
        s["elev"] = elev_cache[elev_key(s["lon"], s["lat"])]
    for z in zones_meta:
        z["elev"] = elev_cache[elev_key(*z["centroid"])]
    for st in reg.stations.values():
        st["elev"] = elev_cache[elev_key(st["lon"], st["lat"])]
    station_elev: dict[str, float] = {name: st["elev"] for name, st in reg.stations.items()}
    print(
        f"[build] elevation range: sections {min(s['elev'] for s in sections):.0f}-"
        f"{max(s['elev'] for s in sections):.0f}m, zone centroids "
        f"{min(z['elev'] for z in zones_meta):.0f}-{max(z['elev'] for z in zones_meta):.0f}m"
    )

    # top-10 largest uphill bike climb penalties, zone-centroid to zone-centroid (report only; the
    # actual matrix uses section-level points, this is a simple, reportable per-zone-pair proxy).
    bike_climb_penalties = []
    for zi in zones_meta:
        for zj in zones_meta:
            if zi["id"] == zj["id"]:
                continue
            gain = zj["elev"] - zi["elev"]
            if gain <= 0:
                continue
            bike_climb_penalties.append(
                (gain / ASSUMPTIONS["bike_climb_m_per_min"], zi["name"], zj["name"], zi["elev"], zj["elev"])
            )
    bike_climb_penalties.sort(reverse=True)
    bike_climb_penalties = bike_climb_penalties[:10]

    variants = {"base": base_lines, "l9_central": l9_lines}

    print("[build] building line graphs + station-to-station shortest times...")
    station_times: dict[str, dict[str, dict[str, float]]] = {}
    for variant, lines in variants.items():
        adj, station_keys = build_time_graph(reg, lines)
        station_times[variant] = dijkstra_from_entries(adj, station_keys)
        print(f"        {variant}: {len(station_keys)} stations")

    # station point lists per variant, for nearest-station lookups: base variant only has
    # variant="base" stations; l9_central has all stations (base + new).
    def station_points(variant: str) -> list[tuple[str, float, float]]:
        out = []
        for name, st in reg.stations.items():
            if variant == "base" and st["variant"] != "base":
                continue
            out.append((name, st["lon"], st["lat"]))
        return out

    variant_station_pts = {v: station_points(v) for v in variants}
    base_pts_set = variant_station_pts["base"]
    new_pts_only = [p for p in variant_station_pts["l9_central"] if p not in base_pts_set]

    def nearest_for_variant(lon: float, lat: float, variant: str) -> list[tuple[str, float]]:
        """Top-5-within-1.5km station shortlist for `variant`. For l9_central this is the union of
        base's own top-5 shortlist and the new stations' top-5 shortlist (rather than a plain top-5
        over the combined point set), so a new L9 station that is merely geographically closer than
        an already-good base station can never displace it from the shortlist and make the best
        rail time worse than in "base" -- l9_central must be a strict superset of base's options at
        every point, since it is the same network plus one more line."""
        base_near = nearest_stations(
            lon, lat, variant_station_pts["base"], ASSUMPTIONS["rail_access_max_stations"], ASSUMPTIONS["rail_access_radius_m"]
        )
        if variant == "base":
            return base_near
        new_near = nearest_stations(
            lon, lat, new_pts_only, ASSUMPTIONS["rail_access_max_stations"], ASSUMPTIONS["rail_access_radius_m"]
        )
        seen = {n for n, _ in base_near}
        return base_near + [p for p in new_near if p[0] not in seen]

    print("[build] computing zone x zone travel times (this is the slow part)...")
    n = len(zones_meta)
    times: dict[str, dict[str, list[float]]] = {v: {} for v in variants}

    # precompute, per zone's census sections, the crow-fly distance to every other zone's centroid
    # is not needed directly for walk/bike/car/bus -- those are simple functions of distance, so we
    # compute the population-weighted mean analytically without a full section x zone matrix pass
    # by re-deriving from each section's own coordinates each time (n is small: 73 zones).
    for v in variants:
        walk_mat = [0.0] * (n * n)
        bike_mat = [0.0] * (n * n)
        car_mat = [0.0] * (n * n)
        bus_mat = [0.0] * (n * n)
        metro_mat = [0.0] * (n * n)
        pts = variant_station_pts[v]
        st_times = station_times[v]

        # cache nearest-station lookups per section point and per destination centroid
        centroid_near_cache: dict[int, list[tuple[str, float]]] = {}
        for zi, zj_meta in enumerate(zones_meta):
            clon, clat = zj_meta["centroid"]
            centroid_near_cache[zi] = nearest_for_variant(clon, clat, v)

        sec_near_by_zone: dict[int, list[tuple[dict, list[tuple[str, float]]]]] = {}
        for zi, zmeta in enumerate(zones_meta):
            lst = []
            for s in zmeta["sections"]:
                near = nearest_for_variant(s["lon"], s["lat"], v)
                lst.append((s, near))
            sec_near_by_zone[zi] = lst

        for i, zi_meta in enumerate(zones_meta):
            secs_near = sec_near_by_zone[i]
            total_pop_i = sum(s["population"] for s, _ in secs_near) or 1
            for j, zj_meta in enumerate(zones_meta):
                clon, clat = zj_meta["centroid"]
                d_near = centroid_near_cache[j]
                w_sum = {"walk": 0.0, "bike": 0.0, "car": 0.0, "bus": 0.0, "metro": 0.0}
                pop_used_for_metro = 0
                for s, o_near in secs_near:
                    p = max(s["population"], 1) if total_pop_i == 1 else s["population"]
                    if p == 0 and total_pop_i != 1:
                        continue
                    d = haversine_m(s["lon"], s["lat"], clon, clat)
                    elev_gain = zj_meta["elev"] - s["elev"]  # net ascent, may be negative (descent)
                    w_sum["walk"] += p * walk_min(d, elev_gain)
                    w_sum["bike"] += p * bike_min(d, elev_gain)
                    w_sum["car"] += p * car_min(d)
                    w_sum["bus"] += p * bus_min(d)
                    rt = rail_time_from_point(
                        s["lon"], s["lat"], clon, clat, pts, st_times,
                        s["elev"], zj_meta["elev"], station_elev,
                        origin_near=o_near, dest_near=d_near,
                    )
                    if rt is None:
                        rt = car_min(d) + 15.0  # no rail access at all: fall back, penalised
                    w_sum["metro"] += p * rt
                    pop_used_for_metro += p
                denom = total_pop_i if total_pop_i else 1
                idx = i * n + j
                if i == j:
                    # within-zone trips: use a nominal intra-zone distance instead of section->own
                    # centroid (would be ~0 for many sections); approximate as sqrt(area)/2 via the
                    # zone's own section spread, floored at 300m.
                    pass
                walk_mat[idx] = round(w_sum["walk"] / denom, 1)
                bike_mat[idx] = round(w_sum["bike"] / denom, 1)
                car_mat[idx] = round(w_sum["car"] / denom, 1)
                bus_mat[idx] = round(w_sum["bus"] / denom, 1)
                metro_mat[idx] = round(w_sum["metro"] / denom, 1)

        times[v] = {
            "metro": metro_mat,
            "bus": bus_mat,
            "car": car_mat,
            "bike": bike_mat,
            "walk": walk_mat,
        }
        print(f"        {v} done")

    # floor times at a small positive value (avoid 0.0 exactly for same-zone trips with ~0 distance)
    for v, mode_times in times.items():
        for mode in mode_times:
            mode_times[mode] = [max(t, 0.5) for t in mode_times[mode]]

    print("[build] computing coverage (600m crow-fly on census-section population points)...")
    radius = ASSUMPTIONS["coverage_radius_m"]
    base_pts = base_pts_set

    for zmeta in zones_meta:
        secs = zmeta["sections"]
        total = sum(s["population"] for s in secs) or 1
        covered = 0
        newly_covered = 0
        for s in secs:
            near_base = any(
                haversine_m(s["lon"], s["lat"], slon, slat) <= radius for _, slon, slat in base_pts
            )
            if near_base:
                covered += s["population"]
                continue
            near_new = any(
                haversine_m(s["lon"], s["lat"], slon, slat) <= radius for _, slon, slat in new_pts_only
            )
            if near_new:
                newly_covered += s["population"]
        zmeta["rail_coverage"] = round(covered / total, 4)
        zmeta["new_coverage"] = {"l9_central": round(newly_covered / total, 4)}

    print("[build] assembling TransitAccess...")
    zones = [
        Zone(
            id=z["id"],
            name=z["name"],
            district=z["district"],
            population=z["population"],
            job_weight=round(z["job_weight"], 6),
            centroid=z["centroid"],
            rail_coverage=z["rail_coverage"],
            new_coverage=z["new_coverage"],
        )
        for z in zones_meta
    ]

    stations = []
    for name, st in reg.stations.items():
        stations.append(
            Station(
                name=name,
                lines=sorted(st["lines"]),
                lon=round(st["lon"], 5),
                lat=round(st["lat"], 5),
                variant=st["variant"],
                approx=st["approx"],
            )
        )
    stations.sort(key=lambda s: (s.variant, s.name))

    sources = {
        "zones.barris": "Open Data BCN 20170706-districtes-barris, resource BarcelonaCiutat_Barris.json "
        f"(73 barris, codes/names/district), accessed {ACCESS_DATE}. "
        "https://opendata-ajuntament.barcelona.cat/data/dataset/20170706-districtes-barris",
        "zones.population_and_centroid": "Open Data BCN pad_mdbas_edat-q (padro population by census "
        "section, 2025) summed per section, weighted mean of census-section centroids from "
        "BarcelonaCiutat_SeccionsCensals.json (1068 sections), same dataset family, "
        f"accessed {ACCESS_DATE}. https://opendata-ajuntament.barcelona.cat/data/dataset/pad_mdbas_edat-q",
        "zones.job_weight": "proxy: ground-floor commercial premises per barri (Open Data BCN "
        "cens-locals-planta-baixa-act-economica, 2024), vacant/for-rent/for-sale rows excluded, "
        "normalized to sum to 1 within each district. No real jobs/workplaces-by-barri dataset was "
        "found on Open Data BCN or Idescat (checked package_search for 'llocs de treball', "
        "'afiliacions', 'centres de treball', 'cens d'activitats economiques per barri' on "
        f"{ACCESS_DATE}; only district-resolution proxies exist, per data/processed/SOURCES.md's "
        "own jobs_per_resident entry). "
        "https://opendata-ajuntament.barcelona.cat/data/dataset/cens-locals-planta-baixa-act-economica",
        "stations.metro_fgc_tram": "OpenStreetMap Overpass API, route=subway (metro L1-L11 incl. "
        "L9 Nord/Sud, L10 Nord/Sud, and FGC L6/L7/L8/L12) and route=tram (T1-T6) relations, ordered "
        f"'stop' members, bbox [41.28,1.95,41.50,2.30], accessed {ACCESS_DATE} via overpass-api.de. "
        "Station names attached by nearest named railway=station/tram_stop node (<=350m for "
        "metro/FGC, <=250m for tram); platforms merged within 250m by name.",
        "stations.rodalies_fgc_s1s2": "OpenStreetMap Overpass API station-node query "
        "(railway=station/halt, network~Rodalies|Renfe for Rodalies; operator~FGC|Generalitat for "
        "the Barcelona-Valles S1/S2 trunk), same bbox/date. No usable route=train relation could be "
        "fetched (Overpass's public instance times out on route=train even filtered by ref regex + "
        "bbox -- the underlying relations span all of Catalonia); station POSITIONS are real OSM "
        "data, but the CONNECTIVITY/ordering between them is a documented approximation: a greedy "
        "nearest-neighbour chain from Barcelona-Sants (Rodalies) / Barcelona-Placa Catalunya "
        "(FGC S1/S2), not the real branching topology. This affects in-vehicle time estimates for "
        "trips that use Rodalies or FGC S1/S2 as an intermediate leg; it does not affect the L9 "
        "central-section comparison (base vs l9_central), which is metro-only.",
        "stations.l9_central_construction": "7 of 8 new L9/L10-central stations (Campus Nord, Manuel "
        "Girona, Mandri, El Putxet, Sanllehy, Guinardo-Hospital de Sant Pau, and Travessera de Dalt "
        "[OSM node named 'Muntanya', tagged railway=proposed]) are real OSM nodes tagged "
        "railway=construction or railway=proposed with their planned/surveyed coordinates, fetched "
        f"via the Overpass API, bbox [41.37,2.09,41.44,2.20], accessed {ACCESS_DATE}. Official route "
        "reference: https://www.amb.cat/es/web/territori/infraestructures-metropolitanes/"
        "projectes-infraestructures/detall/-/infraestructura/metro-l9-l10--zona-universitaria-"
        "sagrera/339081/11656 . The 5 existing stations that become L9 interchanges (Zona "
        "Universitaria, Sarria, Lesseps, Placa de Maragall, La Sagrera) reuse their existing base "
        "network coordinates.",
        "stations.l9_central_prat_de_la_riba_approx": "approx: true -- no railway=construction/"
        "proposed OSM node named 'Prat de la Riba' was found. Placed at the midpoint of two OSM bus "
        "stops named after the street it will sit under (\"Pg Sant Joan Bosco - Prat de la Riba\", "
        "\"Av Sarria - Prat de la Riba\", both on Avinguda de Sarria near Carrer de Prat de la Riba), "
        "cross-checked against a Nominatim geocode of \"Avinguda de Prat de la Riba, Barcelona, Spain\" "
        f"({ACCESS_DATE}); Nominatim's Barcelona-area match for the street is a housing-estate road "
        "in Pallejà, not this Sarria street, so the bus-stop names (not the Nominatim geocode) were "
        "used as the coordinate source.",
        "travel_time_model": "docs/TRANSIT_ACCESS.md 'Travel-time model' table, implemented as-is; "
        "every constant is in `assumptions`.",
        "coverage_radius": "docs/TRANSIT_ACCESS.md: 600m crow-fly on census-section population "
        "points (this build).",
        "elevation": f"{ELEVATION_SOURCE} via {ELEVATION_URL} (max 100 points/request, 1 "
        f"request/second), accessed {ACCESS_DATE}. Ground elevation (m) looked up directly for "
        "every point used by the build: all 1068 census-section population points (bike/walk trip "
        "origins), all 73 zone centroids (bike/walk trip destinations), and all "
        f"{len(reg.stations)} merged rail stations (both existing and new L9/L10-central), so the "
        "metro mode's walking access/egress legs are also elevation-aware. Cached in "
        f"data/raw/transit/{ELEV_CACHE_PATH_NAME} (gitignored, keyed by 5-decimal lat,lon), never "
        "invented; re-running the build without --no-fetch fetches only points missing from the "
        f"cache. A handful of coastline/port points come back null from eudem25m (masked as sea); "
        f"those fall back to {ELEVATION_FALLBACK_SOURCE} instead, same caching/never-invented rule. "
        "Used for the bike/walk climb penalty -- see `assumptions` "
        "(walk_climb_m_per_min, bike_climb_m_per_min) and the build docstring at the top of this "
        "file.",
    }

    access = TransitAccess(
        radius_m=ASSUMPTIONS["coverage_radius_m"],
        zones=zones,
        variants=["base", "l9_central"],
        modes=["metro", "bus", "car", "bike", "walk"],
        times=times,
        stations=stations,
        sources=sources,
        assumptions=ASSUMPTIONS,
    )

    PROCESSED.mkdir(parents=True, exist_ok=True)
    out_path = PROCESSED / "transit_access.json"
    payload = access.model_dump_json(indent=2)
    out_path.write_text(payload)
    # round-trip validate exactly as the contract requires
    TransitAccess.model_validate_json(out_path.read_text())
    print(f"[build] wrote {out_path} ({out_path.stat().st_size:,} bytes)")

    elapsed = time.time() - t0
    write_sanity_report(access, elapsed, before_access=before_access, bike_climb_penalties=bike_climb_penalties)
    write_map(access)
    append_sources_md()

    print(f"[build] done in {elapsed:.1f}s")
    return 0


# =================================================================================================
# Sanity report + map
# =================================================================================================


def dest_weight_by_zone_id(
    zones: list[Zone], districts_json: list[dict] | None
) -> tuple[dict[str, float], bool]:
    """destination weight for the fastest-mode-share report: destination job_weight, scaled by its
    district's share of citywide jobs when a jobs source is available (districts.json's
    jobs_per_resident x population, per-district), otherwise just job_weight on its own (per the
    task: 'population x destination job_weight x destination district's jobs share if available,
    otherwise population x job_weight' -- the population factor is applied separately, per OD
    pair, by the caller). Returns (weights, used_district_jobs_share)."""
    if districts_json:
        district_jobs = {
            d["id"]: d.get("jobs_per_resident", 0.0) * d.get("population", 0.0) for d in districts_json
        }
        total_jobs = sum(district_jobs.values())
        if total_jobs > 0:
            jobs_share = {d: j / total_jobs for d, j in district_jobs.items()}
            return {z.id: z.job_weight * jobs_share.get(z.district, 0.0) for z in zones}, True
    return {z.id: z.job_weight for z in zones}, False


def fastest_mode_shares(ta: TransitAccess, weight_by_zone_id: dict[str, float], variant: str) -> dict[str, float]:
    """Share of OD pairs (i != j), weighted by origin population x dest_weight_by_zone_id[j], where
    each mode has the lowest door-to-door time in `variant`. Unlike the multinomial-logit sanity
    section above, this asks a simpler question ('which mode wins the race') with no car-ownership
    gating and no distance-decay -- see the task/report caveat printed next to it."""
    zones = ta.zones
    n = len(zones)
    mode_w: dict[str, float] = defaultdict(float)
    total = 0.0
    times = ta.times[variant]
    for i, zi in enumerate(zones):
        for j, zj in enumerate(zones):
            if i == j:
                continue
            w = zi.population * weight_by_zone_id.get(zj.id, 0.0)
            if w <= 0:
                continue
            idx = i * n + j
            best_mode = min(ta.modes, key=lambda m: times[m][idx])
            mode_w[best_mode] += w
            total += w
    return {m: mode_w.get(m, 0.0) / total for m in ta.modes} if total else {}


def write_sanity_report(
    access: TransitAccess,
    elapsed: float,
    before_access: TransitAccess | None = None,
    bike_climb_penalties: list[tuple[float, str, str, float, float]] | None = None,
) -> None:
    zones = access.zones
    n = len(zones)

    # multinomial logit on base times
    car_ownership_by_district = {
        "ciutat_vella": 0.30, "eixample": 0.45, "sants_montjuic": 0.45, "les_corts": 0.55,
        "sarria_sant_gervasi": 0.65, "gracia": 0.35, "horta_guinardo": 0.45, "nou_barris": 0.40,
        "sant_andreu": 0.45, "sant_marti": 0.40,
    }  # data/processed/districts.json::car_ownership (T1 output), duplicated here to avoid an
    # import-time coupling to that file's exact schema; see districts.json for the sourced values.
    districts_json: list[dict] | None = None
    try:
        districts_json = json.loads((PROCESSED / "districts.json").read_text())
        car_ownership_by_district = {d["id"]: d["car_ownership"] for d in districts_json}
    except (OSError, json.JSONDecodeError, KeyError):
        pass  # fall back to the hand-copied values above (this file is optional here)

    beta = access.assumptions["logit_beta_minutes"]
    walk_max = access.assumptions["walk_max_considered_min"]
    mode_totals = defaultdict(float)
    total_weight = 0.0
    # Trip-pair weight = population_i * population_j (a gravity-flavoured proxy for "how many
    # all-purpose trips happen between these two zones"). The contract only specifies the logit
    # formula itself, not an OD-pair weighting; a uniform weighting over all 73x73 pairs regardless
    # of distance was tried first and produced a much worse match to EMEF (walk share collapsed to
    # ~3%, bike inflated to ~45%) because it treats a cross-city pair the same as a next-door one,
    # when in reality most trips are short. population_i*population_j is still NOT
    # distance-decayed (no gravity/friction term), so it still overweights long trips relative to
    # real travel demand -- see the caveat printed below the table.
    for i, zi in enumerate(zones):
        car_share = car_ownership_by_district.get(zi.district, 0.4)
        for j, zj in enumerate(zones):
            pair_w = zi.population * zj.population
            if pair_w == 0:
                continue
            m = i * n + j
            utils = {}
            for mode in access.modes:
                t = access.times["base"][mode][m]
                if mode == "walk" and t > walk_max:
                    continue
                utils[mode] = math.exp(beta * t)
            if "car" in utils:
                # split car utility mass between car-owning and non-owning population
                car_u = utils.pop("car")
                z_noncar = sum(utils.values())
                z_car = z_noncar + car_u
                for mode, u in utils.items():
                    mode_totals[mode] += pair_w * ((1 - car_share) * (u / z_noncar if z_noncar else 0) + car_share * (u / z_car if z_car else 0))
                mode_totals["car"] += pair_w * car_share * (car_u / z_car if z_car else 0)
            else:
                z = sum(utils.values())
                for mode, u in utils.items():
                    mode_totals[mode] += pair_w * (u / z if z else 0)
            total_weight += pair_w
    shares = {m: mode_totals[m] / total_weight for m in access.modes}

    emef = {"walk": 0.513, "bike": 0.032, "metro": 0.173, "bus": 0.116, "car": 0.167}

    # top-20 zone pairs by L9 metro time gain
    gains = []
    for i, zi in enumerate(zones):
        for j, zj in enumerate(zones):
            if i == j:
                continue
            m = i * n + j
            before = access.times["base"]["metro"][m]
            after = access.times["l9_central"]["metro"][m]
            gains.append((before - after, zi.name, zj.name, before, after))
    gains.sort(reverse=True)

    # district new_coverage, population-weighted
    district_pop = defaultdict(float)
    district_new = defaultdict(float)
    for z in zones:
        district_pop[z.district] += z.population
        district_new[z.district] += z.population * z.new_coverage.get("l9_central", 0.0)
    district_cov = {d: (district_new[d] / district_pop[d] if district_pop[d] else 0.0) for d in district_pop}

    line_station_counts: dict[str, int] = defaultdict(int)
    for st in access.stations:
        for line in st.lines:
            line_station_counts[line] += 1
    approx_stations = [st.name for st in access.stations if st.approx]

    lines_out = []
    lines_out.append("# Transit access sanity report\n")
    lines_out.append(f"Build time: {elapsed:.1f}s. Zones: {n}. Stations: {len(access.stations)}.\n")
    lines_out.append("\n## Mode shares (multinomial logit on base times, utility = -0.1 x minutes) vs EMEF 2024\n")
    lines_out.append("| mode | model | EMEF 2024 |\n|---|---|---|\n")
    for mode in access.modes:
        lines_out.append(f"| {mode} | {shares.get(mode, 0):.1%} | {emef.get(mode, float('nan')):.1%} |\n")
    lines_out.append(
        "\nOD-pair weight = population_i x population_j (see build_transit_access.py comment). "
        "This has no distance-decay/friction term, so it still overweights long cross-city trips "
        "relative to real travel demand (EMEF is dominated by short, often sub-1km trips); walk "
        "and low-speed-mode shares are correspondingly understated here and metro/bike/car "
        "overstated. This is a sanity report, not a gate (docs/TRANSIT_ACCESS.md).\n"
    )
    weights, used_jobs_share = dest_weight_by_zone_id(zones, districts_json)
    weight_note = (
        "population_i x job_weight_j x (district_j's share of citywide jobs, from districts.json "
        "jobs_per_resident x population)" if used_jobs_share else
        "population_i x job_weight_j (districts.json jobs data unavailable; district jobs-share "
        "factor dropped, per the task's documented fallback)"
    )
    after_shares = fastest_mode_shares(access, weights, "base")
    lines_out.append("\n## Fastest mode by OD pair, before vs after this change (elevation-aware bike/walk)\n")
    lines_out.append(
        f"OD-pair weight = {weight_note}. Share of all i!=j zone pairs (base variant; metro/bus/car "
        "identical before/after) where each mode has the lowest door-to-door time.\n\n"
    )
    if before_access is not None:
        before_shares = fastest_mode_shares(before_access, weights, "base")
        lines_out.append("| mode | before (flat bike/walk) | after (elevation-aware) |\n|---|---|---|\n")
        for mode in access.modes:
            lines_out.append(f"| {mode} | {before_shares.get(mode, 0):.1%} | {after_shares.get(mode, 0):.1%} |\n")
    else:
        lines_out.append(
            "(no previously-built data/processed/transit_access.json found -- before/after "
            "comparison skipped this run; showing after only)\n\n"
        )
        lines_out.append("| mode | after (elevation-aware) |\n|---|---|\n")
        for mode in access.modes:
            lines_out.append(f"| {mode} | {after_shares.get(mode, 0):.1%} |\n")

    lines_out.append("\n## Top 10 largest uphill bike climb penalties (zone centroid to zone centroid)\n")
    lines_out.append(
        f"Climb penalty = net ascent / {access.assumptions['bike_climb_m_per_min']:.0f} m per minute "
        "(assumptions.bike_climb_m_per_min); descents get no bonus.\n\n"
    )
    lines_out.append("| climb penalty (min) | from (elev) | to (elev) | ascent (m) |\n|---|---|---|---|\n")
    for penalty, a, b, elev_a, elev_b in bike_climb_penalties or []:
        lines_out.append(
            f"| {penalty:.1f} | {a} ({elev_a:.0f}m) | {b} ({elev_b:.0f}m) | {elev_b - elev_a:.0f} |\n"
        )

    lines_out.append("\n## Top 20 zone pairs by L9 metro time gain (base - l9_central)\n")
    lines_out.append("| gain (min) | from | to | base | l9_central |\n|---|---|---|---|---|\n")
    for gain, a, b, before, after in gains[:20]:
        lines_out.append(f"| {gain:.1f} | {a} | {b} | {before:.1f} | {after:.1f} |\n")
    lines_out.append("\n## Population-weighted new_coverage by district (l9_central)\n")
    lines_out.append("| district | new coverage |\n|---|---|\n")
    for d in sorted(district_cov, key=lambda d: -district_cov[d]):
        lines_out.append(f"| {d} | {district_cov[d]:.1%} |\n")
    lines_out.append("\n## Stations per line\n")
    lines_out.append("| line | stations |\n|---|---|\n")
    for line in sorted(line_station_counts):
        lines_out.append(f"| {line} | {line_station_counts[line]} |\n")
    lines_out.append(f"\n## Stations flagged approx: true ({len(approx_stations)})\n")
    for name in approx_stations:
        lines_out.append(f"- {name}\n")

    (PROCESSED / "transit_access_report.md").write_text("".join(lines_out))
    print("[build] wrote", PROCESSED / "transit_access_report.md")
    print("".join(lines_out)[:2000])


def write_map(access: TransitAccess) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[build] matplotlib not available, skipping media/l9_access_map.png")
        return

    MEDIA.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(9, 9))
    covs = [z.new_coverage.get("l9_central", 0.0) for z in access.zones]
    xs = [z.centroid[0] for z in access.zones]
    ys = [z.centroid[1] for z in access.zones]
    sc = ax.scatter(xs, ys, c=covs, cmap="Reds", s=220, edgecolors="black", linewidths=0.3, vmin=0, vmax=max(covs) if max(covs) > 0 else 1)
    for z, x, y in zip(access.zones, xs, ys):
        ax.annotate(z.id, (x, y), fontsize=5, ha="center", va="center")
    existing = [st for st in access.stations if st.variant == "base"]
    new = [st for st in access.stations if st.variant == "l9_central"]
    ax.scatter([s.lon for s in existing], [s.lat for s in existing], c="grey", s=8, alpha=0.6, label="existing stations")
    ax.scatter([s.lon for s in new], [s.lat for s in new], c="red", s=40, marker="*", label="new L9 stations")
    for s in new:
        ax.annotate(s.name, (s.lon, s.lat), fontsize=6, color="red", xytext=(3, 3), textcoords="offset points")
    plt.colorbar(sc, ax=ax, label="new_coverage (l9_central)")
    ax.legend(loc="lower left", fontsize=7)
    ax.set_title("L9 central section: barri new_coverage + stations")
    ax.set_xlabel("lon")
    ax.set_ylabel("lat")
    fig.tight_layout()
    fig.savefig(MEDIA / "l9_access_map.png", dpi=150)
    plt.close(fig)
    print("[build] wrote", MEDIA / "l9_access_map.png")


SOURCES_MD_SECTION_MARK = "## Transit access (T12)"


def append_sources_md() -> None:
    path = PROCESSED / "SOURCES.md"
    text = path.read_text()
    if SOURCES_MD_SECTION_MARK in text:
        return
    addition = f"""
{SOURCES_MD_SECTION_MARK}

Generated by `scripts/build_transit_access.py` (raw inputs fetched by
`scripts/fetch_transit_raw.py` into `data/raw/transit/`, gitignored). Produces
`data/processed/transit_access.json`, `data/processed/transit_access_report.md` and
`media/l9_access_map.png`.

| Field | Source type | Dataset id | Year | URL | Notes |
|---|---|---|---|---|---|
| zones (73 barris): id/name/district | opendata | `20170706-districtes-barris` | 2017 | https://opendata-ajuntament.barcelona.cat/data/dataset/20170706-districtes-barris | `BarcelonaCiutat_Barris.json` resource; `codi_districte` maps 1:1 to `jevcity.types.BCN_DISTRICTS`. |
| zones: population, centroid | opendata | `pad_mdbas_edat-q` (2025) + `20170706-districtes-barris` (2017) | 2025 / 2017 | https://opendata-ajuntament.barcelona.cat/data/dataset/pad_mdbas_edat-q | Population summed per census section (Seccio_Censal x EDAT_Q), centroid is the population-weighted mean of each barri's census-section polygon centroids (`BarcelonaCiutat_SeccionsCensals.json`, 1068 sections). |
| zones: job_weight | proxy | `cens-locals-planta-baixa-act-economica` (2024) | 2024 | https://opendata-ajuntament.barcelona.cat/data/dataset/cens-locals-planta-baixa-act-economica | Ground-floor commercial premises per barri (vacant/for-sale/for-rent rows excluded), normalized to sum to 1 within each district. No real jobs-by-barri dataset was found (checked Open Data BCN and Idescat); documented proxy per `docs/TRANSIT_ACCESS.md`. |
| existing rail network (stations + line topology) | opendata (OSM) | OpenStreetMap via Overpass API | 2026-09-25 snapshot | https://overpass-api.de/api/interpreter | Metro L1-L11 (incl. L9N/S, L10N/S), FGC L6/L7/L8/L12 and tram T1-T6: `route=subway`/`route=tram` relations, ordered `stop` members, station names attached by nearest named station/tram-stop node, platforms merged within 250m. Rodalies and FGC S1/S2 (Barcelona-Valles): station positions are real OSM nodes, but no usable route relation could be fetched (Overpass times out on `route=train` for this area even filtered by ref/operator); their connectivity is a documented approximation (nearest-neighbour chain from Barcelona-Sants / Barcelona-Placa Catalunya), not the real branching topology. |
| L9/L10 central section: 7 of 8 new stations | opendata (OSM) | OpenStreetMap via Overpass API | 2026-09-25 snapshot | https://overpass-api.de/api/interpreter | Campus Nord, Manuel Girona, Mandri, El Putxet, Sanllehy, Guinardo-Hospital de Sant Pau, Travessera de Dalt: real OSM nodes tagged `railway=construction` or `railway=proposed` (Travessera de Dalt is OSM's working name "Muntanya") with surveyed/planned coordinates. Official project reference: https://www.amb.cat/es/web/territori/infraestructures-metropolitanes/projectes-infraestructures/detall/-/infraestructura/metro-l9-l10--zona-universitaria-sagrera/339081/11656 |
| L9/L10 central section: Prat de la Riba | approximate | -- | -- | -- | No `railway=construction`/`proposed` OSM node found for this station. Placed at the midpoint of two OSM bus stops named after the street ("Pg Sant Joan Bosco - Prat de la Riba", "Av Sarria - Prat de la Riba"); `approx: true` in `transit_access.json`. A Nominatim geocode of "Avinguda de Prat de la Riba, Barcelona, Spain" resolved to an unrelated street in Palleja, so was not used as the coordinate. |
| travel-time model, coverage radius | derived | -- | -- | -- | Implements `docs/TRANSIT_ACCESS.md`'s "Travel-time model" table exactly; every constant is recorded in `transit_access.json`'s `assumptions`. |
| elevation (bike/walk climb penalty) | opendata (via OpenTopoData) | `eudem25m` (Copernicus EU-DEM v1.1, ~25m) | -- | https://api.opentopodata.org/v1/eudem25m | Ground elevation (m) looked up per point (all census-section population points, all zone centroids, all rail stations) via the OpenTopoData API, cached in `data/raw/transit/elevations_eudem25m.json` (gitignored), never invented. Used for the bike/walk net-ascent climb penalty; see `assumptions.walk_climb_m_per_min` / `bike_climb_m_per_min`. |
"""
    path.write_text(text + addition)
    print("[build] appended Transit access section to", path)


if __name__ == "__main__":
    sys.exit(main())
