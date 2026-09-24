"""Fetch and process Open Data BCN sources into data/processed/{districts.json,districts.geojson,SOURCES.md}.

Re-runnable: downloads raw files into data/raw/ (gitignored) and rebuilds the processed
outputs from them. Network access required on first run; subsequent runs reuse the cached
raw files unless --refresh is passed.

Usage:
    uv run python scripts/fetch_opendata.py [--refresh]

Owner: T1 (world data). See docs/CONTRACTS.md and src/jevcity/types.py::DistrictProfile.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"

CKAN_BASE = "https://opendata-ajuntament.barcelona.cat/data/api/3/action"

# --- Districts in scope for T1 (ids/names per task spec; Codi_Districte per Open Data BCN) --

# codi: the numeric district code used in pad_mdbas*/renda-disponible*/cens-locals CSVs
# ("Codi_Districte", plain int-as-string, e.g. "1", "10").
# codi2: the same code zero-padded to 2 digits, used in the districtes geometry JSON
# ("Codi_Districte", e.g. "01", "10").
DISTRICTS = [
    {"id": "ciutat_vella", "name": "Ciutat Vella", "codi": "1", "codi2": "01"},
    {"id": "eixample", "name": "Eixample", "codi": "2", "codi2": "02"},
    {"id": "gracia", "name": "Gràcia", "codi": "6", "codi2": "06"},
    {"id": "nou_barris", "name": "Nou Barris", "codi": "8", "codi2": "08"},
    {"id": "sant_marti", "name": "Sant Martí", "codi": "10", "codi2": "10"},
]
DISTRICT_BY_CODI = {d["codi"]: d for d in DISTRICTS}

# --- Dataset resources (dataset id / year chosen = latest complete year available at fetch time) --

DATASETS = {
    "population_age": {
        "dataset_id": "pad_mdbas_edat-q",
        "year": 2025,
        "url": "https://opendata-ajuntament.barcelona.cat/data/dataset/a45e1f19-5137-45bd-8979-645906fde55b/resource/c5af1fec-95bc-4f25-8adc-c0313cfe0144/download",
        "raw_name": "pad_mdbas_edat-q_2025.csv",
        "notes": "Padro population by district/barri/seccio censal x five-year age group (EDAT_Q), 1 Jan 2025.",
    },
    "income": {
        "dataset_id": "renda-disponible-llars-bcn",
        "year": 2023,
        "url": "https://opendata-ajuntament.barcelona.cat/data/dataset/78db0c75-fa56-4604-9510-8b92834a7fd2/resource/f18e28ed-dcee-4904-b350-52088201a7a0/download/2023_renda_disponible_llars_per_persona.csv",
        "raw_name": "renda_disponible_llars_per_persona_2023.csv",
        "notes": "Disposable household income per capita (EUR/year) by census section, latest available year (2023).",
    },
    "shops": {
        "dataset_id": "cens-locals-planta-baixa-act-economica",
        "year": 2024,
        "url": "https://opendata-ajuntament.barcelona.cat/data/dataset/fe177673-0f83-42e7-b35a-ddea901be8bc/resource/38babeec-5c47-43d3-84e7-b13a4b89004f/download/241021_censcomercialbcn_opendata_2024_v5.csv",
        "raw_name": "cens_comercial_bcn_2024.csv",
        "notes": "Ground-floor premises census; vacant/for-rent/for-sale premises excluded from the shop count.",
    },
    "geometry": {
        "dataset_id": "20170706-districtes-barris",
        "year": 2017,
        "url": "https://opendata-ajuntament.barcelona.cat/data/dataset/808daafa-d9ce-48c0-925a-fa5afdb1ed41/resource/5f8974a7-7937-4b50-acbc-89204d570df9/download",
        "raw_name": "barcelonaciutat_districtes.json",
        "notes": "District polygons, ETRS89 / UTM zone 31N (EPSG:25831) WKT, converted to WGS84 here.",
    },
}

# Vacant / for-sale / for-rent ground-floor premises: excluded from the "shops" count.
VACANT_SHOP_SECTORS = {
    "Locals buits en venda i lloguer",
    "Locals buits en lloguer",
    "Locals buits en venda",
}

# EDAT_Q is a five-year age band code: 0 -> 0-4, 1 -> 5-9, ..., 20 -> 100+.
# AGE_BUCKETS = ("0-17", "18-34", "35-49", "50-64", "65+") splits mid-band, so code 3 (15-19)
# is split 3/5 into "0-17" (ages 15-17) and 2/5 into "18-34" (ages 18-19), assuming a uniform
# age distribution within the five-year band (a standard, disclosed approximation).
AGE_CODE_TO_BUCKET_WEIGHTS: dict[int, dict[str, float]] = {
    0: {"0-17": 1.0},
    1: {"0-17": 1.0},
    2: {"0-17": 1.0},
    3: {"0-17": 0.6, "18-34": 0.4},
    4: {"18-34": 1.0},
    5: {"18-34": 1.0},
    6: {"18-34": 1.0},
    7: {"35-49": 1.0},
    8: {"35-49": 1.0},
    9: {"35-49": 1.0},
    10: {"50-64": 1.0},
    11: {"50-64": 1.0},
    12: {"50-64": 1.0},
}
for _code in range(13, 21):
    AGE_CODE_TO_BUCKET_WEIGHTS[_code] = {"65+": 1.0}

# --- Plausible fallback values (Source.PLAUSIBLE) -------------------------------------------
# No Open Data BCN dataset with district-level average asking rent, rental vacancy rate,
# registered unemployment rate, job-location counts or a transit-connectivity index was found
# via package_search (checked terms: "atur", "lloguer", "unemployment", "rent contracts",
# "mercat de treball" on 2026-09-24). Values below are hand-set, informed by well-known,
# widely reported relative characteristics of these five districts (Ciutat Vella/Eixample/
# Gràcia = dense, touristic, higher rents; Nou Barris = peripheral, lower income, higher
# unemployment; Sant Martí = mixed, ex-industrial/22@ redevelopment) - NOT measured data.
# Every value here is tagged Source.PLAUSIBLE with a "plausible: <reasoning>" ref.
PLAUSIBLE_DEFAULTS: dict[str, dict[str, float]] = {
    "ciutat_vella": {
        "avg_rent_monthly": 1050.0,  # dense historic centre, small flats, high tourist pressure
        "vacancy_rate": 0.06,  # elevated by short-term-rental/renovation turnover
        "unemployment_rate": 0.11,  # historically the highest of the 10 districts
        "jobs_per_resident": 1.10,  # tourism/retail/office core, net job importer
        "transit_score": 0.95,  # extremely well served, central, flat, walkable
    },
    "eixample": {
        "avg_rent_monthly": 1250.0,  # most expensive large district
        "vacancy_rate": 0.04,
        "unemployment_rate": 0.07,  # affluent, below city average
        "jobs_per_resident": 1.40,  # largest office/commercial concentration
        "transit_score": 0.95,  # metro grid + Sants/Passeig de Gracia hubs
    },
    "gracia": {
        "avg_rent_monthly": 1150.0,  # sought-after, gentrified, limited stock
        "vacancy_rate": 0.03,
        "unemployment_rate": 0.07,
        "jobs_per_resident": 0.55,  # mostly residential, net job exporter
        "transit_score": 0.80,  # good but less metro coverage than the centre
    },
    "nou_barris": {
        "avg_rent_monthly": 800.0,  # cheapest large district
        "vacancy_rate": 0.05,
        "unemployment_rate": 0.12,  # among the highest, historically lower income
        "jobs_per_resident": 0.30,  # residential dormitory district, few local jobs
        "transit_score": 0.60,  # hillier, sparser metro coverage
    },
    "sant_marti": {
        "avg_rent_monthly": 1100.0,  # pulled up by 22@/Poblenou/Diagonal Mar redevelopment
        "vacancy_rate": 0.04,
        "unemployment_rate": 0.09,
        "jobs_per_resident": 0.75,  # 22@ tech/office district raises job count
        "transit_score": 0.80,  # good tram/metro along the coast, patchier inland
    },
}


def http_get(url: str, retries: int = 4) -> bytes:
    import time

    last_exc: Exception | None = None
    # opendata-ajuntament.barcelona.cat's edge occasionally 502s requests carrying httpx's
    # default User-Agent; a browser/curl-like one avoids it.
    headers = {"User-Agent": "Mozilla/5.0 (compatible; jevcity-data-fetch/1.0)"}
    with httpx.Client(follow_redirects=True, timeout=60.0, headers=headers) as client:
        for attempt in range(retries):
            try:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.content
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < retries - 1:
                    time.sleep(2**attempt)
    raise last_exc  # type: ignore[misc]


def fetch_raw(refresh: bool = False) -> dict[str, Path]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for key, meta in DATASETS.items():
        dest = RAW_DIR / meta["raw_name"]
        if dest.exists() and not refresh:
            print(f"[skip] {key}: {dest} already cached")
        else:
            print(f"[fetch] {key}: {meta['url']}")
            data = http_get(meta["url"])
            dest.write_bytes(data)
            print(f"        -> {dest} ({len(data):,} bytes)")
        paths[key] = dest
    return paths


# --- Processing --------------------------------------------------------------------------


def process_population_age(path: Path) -> dict[str, dict[str, Any]]:
    """Returns {district_id: {"population": int, "age_distribution": {bucket: share}}}."""
    pop_by_district: dict[str, float] = defaultdict(float)
    bucket_by_district: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    # also collect population per (codi, section_num) for income weighting
    pop_by_section: dict[tuple[str, int], float] = defaultdict(float)

    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            codi = row["Codi_Districte"]
            if codi not in DISTRICT_BY_CODI:
                continue
            did = DISTRICT_BY_CODI[codi]["id"]
            raw_value = row["Valor"].strip()
            # dataset convention: counts below 5 are suppressed as ".." for privacy; use the
            # midpoint (2.5) of the possible range [1, 4] as an unbiased small-count estimate.
            value = 2.5 if raw_value == ".." else float(raw_value)
            code = int(row["EDAT_Q"])
            pop_by_district[did] += value
            for bucket, w in AGE_CODE_TO_BUCKET_WEIGHTS[code].items():
                bucket_by_district[did][bucket] += value * w
            sc = row["Seccio_Censal"]
            section_num = int(sc) - int(codi) * 1000
            pop_by_section[(codi, section_num)] += value

    out = {}
    for d in DISTRICTS:
        did = d["id"]
        total = pop_by_district[did]
        dist = {b: bucket_by_district[did].get(b, 0.0) / total for b in AGE_BUCKETS}
        # normalize rounding drift so shares sum to exactly 1.0
        drift = 1.0 - sum(dist.values())
        biggest = max(dist, key=dist.get)
        dist[biggest] += drift
        out[did] = {
            "population": round(total),
            "age_distribution": {k: round(v, 4) for k, v in dist.items()},
        }
    return out, pop_by_section


AGE_BUCKETS = ("0-17", "18-34", "35-49", "50-64", "65+")


def process_income(path: Path, pop_by_section: dict[tuple[str, int], float]) -> dict[str, float]:
    """Population-weighted mean disposable income per capita per district (EUR/year)."""
    weighted_sum: dict[str, float] = defaultdict(float)
    weight_total: dict[str, float] = defaultdict(float)
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            codi = row["Codi_Districte"]
            if codi not in DISTRICT_BY_CODI:
                continue
            did = DISTRICT_BY_CODI[codi]["id"]
            section_num = int(row["Seccio_Censal"])
            w = pop_by_section.get((codi, section_num), 0.0)
            if w <= 0:
                continue
            weighted_sum[did] += float(row["Import_Euros"]) * w
            weight_total[did] += w
    return {
        d["id"]: round(weighted_sum[d["id"]] / weight_total[d["id"]], 2)
        for d in DISTRICTS
    }


def process_shops(path: Path) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            codi = row["Codi_Districte"]
            if codi not in DISTRICT_BY_CODI:
                continue
            if row["Nom_Sector_Activitat"] in VACANT_SHOP_SECTORS:
                continue
            counts[DISTRICT_BY_CODI[codi]["id"]] += 1
    return dict(counts)


# --- Geometry: ETRS89 / UTM zone 31N (EPSG:25831) -> WGS84 ---------------------------------

_A = 6378137.0  # GRS80 semi-major axis (shared with WGS84 to sub-mm)
_F = 1 / 298.257222101  # GRS80 flattening
_E2 = _F * (2 - _F)
_K0 = 0.9996
_FE = 500000.0
_LON0_DEG = 3.0  # UTM zone 31 central meridian


def utm_to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    """Inverse transverse Mercator (Snyder 1987 series), ETRS89 UTM 31N -> WGS84 lon/lat."""
    x = easting - _FE
    y = northing
    m = y / _K0
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E2**2 / 64 - 5 * _E2**3 / 256))
    phi1 = (
        mu
        + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
        + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
        + (151 * e1**3 / 96) * math.sin(6 * mu)
        + (1097 * e1**4 / 512) * math.sin(8 * mu)
    )
    e2p = _E2 / (1 - _E2)
    c1 = e2p * math.cos(phi1) ** 2
    t1 = math.tan(phi1) ** 2
    n1 = _A / math.sqrt(1 - _E2 * math.sin(phi1) ** 2)
    r1 = _A * (1 - _E2) / (1 - _E2 * math.sin(phi1) ** 2) ** 1.5
    d = x / (n1 * _K0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * e2p) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * e2p - 3 * c1**2) * d**6 / 720
    )
    lon = math.radians(_LON0_DEG) + (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * e2p + 24 * t1**2) * d**5 / 120
    ) / math.cos(phi1)
    return math.degrees(lon), math.degrees(lat)


def parse_wkt_polygon_rings(wkt: str) -> list[list[tuple[float, float]]]:
    """Extract the exterior ring(s) of a WKT POLYGON or MULTIPOLYGON as (x, y) UTM coord lists."""
    is_multi = wkt.strip().upper().startswith("MULTIPOLYGON")
    body = wkt[wkt.index("(") : wkt.rindex(")") + 1]
    rings: list[list[tuple[float, float]]] = []
    if is_multi:
        # crude split: each polygon is "((...ring...), (...hole...))"; take first (exterior)
        # ring of each polygon component.
        depth = 0
        polys: list[str] = []
        current = ""
        for ch in body:
            current += ch
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 1:  # closed one polygon-component
                    polys.append(current)
                    current = ""
        for poly in polys:
            ring_str = poly[poly.index("(") + 1 : poly.index(")")]
            rings.append(_parse_ring(ring_str))
    else:
        ring_str = body[body.index("(") + 1 : body.index(")")]
        rings.append(_parse_ring(ring_str))
    return rings


def _parse_ring(ring_str: str) -> list[tuple[float, float]]:
    pts = []
    for pair in ring_str.split(","):
        pair = pair.strip().strip("()")
        x_str, y_str = pair.split()
        pts.append((float(x_str), float(y_str)))
    return pts


def polygon_centroid(ring: list[tuple[float, float]]) -> tuple[float, float]:
    """Area-weighted centroid of a simple polygon ring (shoelace formula)."""
    a = 0.0
    cx = 0.0
    cy = 0.0
    n = len(ring)
    for i in range(n - 1):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    a *= 0.5
    if abs(a) < 1e-9:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return sum(xs) / len(xs), sum(ys) / len(ys)
    cx /= 6 * a
    cy /= 6 * a
    return cx, cy


def douglas_peucker(points: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    """Simplify a polyline, keeping endpoints. epsilon in the same units as points (degrees)."""
    if len(points) < 3:
        return points

    def perp_dist(pt, a, b):
        (x, y), (ax, ay), (bx, by) = pt, a, b
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return math.hypot(x - ax, y - ay)
        t = ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))
        px, py = ax + t * dx, ay + t * dy
        return math.hypot(x - px, y - py)

    dmax = 0.0
    index = 0
    for i in range(1, len(points) - 1):
        d = perp_dist(points[i], points[0], points[-1])
        if d > dmax:
            index = i
            dmax = d
    if dmax > epsilon:
        left = douglas_peucker(points[: index + 1], epsilon)
        right = douglas_peucker(points[index:], epsilon)
        return left[:-1] + right
    return [points[0], points[-1]]


def process_geometry(path: Path) -> dict[str, dict[str, Any]]:
    """Returns {district_id: {"centroid": (lon, lat), "geometry": geojson-geometry-dict}}."""
    rows = json.loads(path.read_text(encoding="utf-8"))
    by_codi2 = {r["Codi_Districte"]: r for r in rows}
    out = {}
    for d in DISTRICTS:
        row = by_codi2[d["codi2"]]
        rings_utm = parse_wkt_polygon_rings(row["geometria_etrs89"])
        # exterior ring = largest by point count (our 5 target districts are simple POLYGONs)
        exterior_utm = max(rings_utm, key=len)
        centroid_utm = polygon_centroid(exterior_utm)
        centroid_lonlat = utm_to_wgs84(*centroid_utm)

        exterior_wgs84 = [utm_to_wgs84(x, y) for x, y in exterior_utm]
        # simplify: ~0.00005 deg ~= 5.5m at this latitude, then round to 5 decimals (~1.1m)
        simplified = douglas_peucker(exterior_wgs84, epsilon=0.00005)
        rounded = [[round(lon, 5), round(lat, 5)] for lon, lat in simplified]
        if rounded[0] != rounded[-1]:
            rounded.append(rounded[0])

        out[d["id"]] = {
            "centroid": (round(centroid_lonlat[0], 5), round(centroid_lonlat[1], 5)),
            "geometry": {"type": "Polygon", "coordinates": [rounded]},
        }
    return out


# --- Assembly ------------------------------------------------------------------------------


def build_profiles(
    pop_age: dict[str, dict[str, Any]],
    income: dict[str, float],
    shops: dict[str, int],
    geo: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    profiles = []
    for d in DISTRICTS:
        did = d["id"]
        plausible = PLAUSIBLE_DEFAULTS[did]
        sources = {
            "population": "opendata",
            "age_distribution": "derived",
            "income_per_capita_annual": "opendata",
            "avg_rent_monthly": "plausible",
            "vacancy_rate": "plausible",
            "unemployment_rate": "plausible",
            "jobs_per_resident": "plausible",
            "shops": "opendata",
            "transit_score": "plausible",
        }
        refs = {
            "population": f"{DATASETS['population_age']['dataset_id']} ({DATASETS['population_age']['year']})",
            "age_distribution": (
                f"derived from {DATASETS['population_age']['dataset_id']} "
                f"({DATASETS['population_age']['year']}) EDAT_Q five-year bands, "
                "15-19 band split 60/40 between 0-17 and 18-34 assuming uniform ages within band"
            ),
            "income_per_capita_annual": (
                f"{DATASETS['income']['dataset_id']} ({DATASETS['income']['year']}), "
                "population-weighted mean of census-section values "
                f"(weights from {DATASETS['population_age']['dataset_id']} "
                f"{DATASETS['population_age']['year']})"
            ),
            "avg_rent_monthly": "plausible: no district-level Open Data BCN rent dataset found; see PLAUSIBLE_DEFAULTS docstring",
            "vacancy_rate": "plausible: no district-level Open Data BCN housing-vacancy dataset found",
            "unemployment_rate": "plausible: no district-level Open Data BCN unemployment dataset found",
            "jobs_per_resident": "plausible: no job-location dataset found; qualitative district role (residential vs commercial core)",
            "shops": (
                f"{DATASETS['shops']['dataset_id']} ({DATASETS['shops']['year']}), "
                "ground-floor premises, rows with Nom_Sector_Activitat in "
                f"{sorted(VACANT_SHOP_SECTORS)} excluded as vacant"
            ),
            "transit_score": "plausible: subjective 0..1 based on known metro/tram density and topography",
        }
        profiles.append(
            {
                "id": did,
                "name": d["name"],
                "population": pop_age[did]["population"],
                "age_distribution": pop_age[did]["age_distribution"],
                "income_per_capita_annual": income[did],
                "avg_rent_monthly": plausible["avg_rent_monthly"],
                "vacancy_rate": plausible["vacancy_rate"],
                "unemployment_rate": plausible["unemployment_rate"],
                "jobs_per_resident": plausible["jobs_per_resident"],
                "shops": shops[did],
                "transit_score": plausible["transit_score"],
                "centroid": list(geo[did]["centroid"]),
                "sources": sources,
                "refs": refs,
            }
        )
    return profiles


def write_geojson(geo: dict[str, dict[str, Any]], dest: Path) -> None:
    features = []
    for d in DISTRICTS:
        did = d["id"]
        features.append(
            {
                "type": "Feature",
                "properties": {"id": did, "name": d["name"]},
                "geometry": geo[did]["geometry"],
            }
        )
    fc = {"type": "FeatureCollection", "features": features}
    dest.write_text(json.dumps(fc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def write_sources_md(dest: Path) -> None:
    lines = [
        "# Data sources — data/processed/districts.json / districts.geojson",
        "",
        "Generated by `scripts/fetch_opendata.py`. Do not hand-edit; re-run the script instead.",
        "",
        "| Field | Source type | Dataset id | Year | URL | Notes |",
        "|---|---|---|---|---|---|",
        f"| population | opendata | `{DATASETS['population_age']['dataset_id']}` | {DATASETS['population_age']['year']} | https://opendata-ajuntament.barcelona.cat/data/dataset/{DATASETS['population_age']['dataset_id']} | Padro population, summed over EDAT_Q age bands, per district. |",
        f"| age_distribution | derived | `{DATASETS['population_age']['dataset_id']}` | {DATASETS['population_age']['year']} | https://opendata-ajuntament.barcelona.cat/data/dataset/{DATASETS['population_age']['dataset_id']} | EDAT_Q five-year bands regrouped into AGE_BUCKETS; the 15-19 band is split 60/40 between 0-17 and 18-34 (uniform-within-band assumption). |",
        f"| income_per_capita_annual | opendata | `{DATASETS['income']['dataset_id']}` | {DATASETS['income']['year']} | https://opendata-ajuntament.barcelona.cat/data/dataset/{DATASETS['income']['dataset_id']} | Disposable income per capita by census section, population-weighted up to district (weights from the {DATASETS['population_age']['year']} padro). Latest year with published data is 2023. |",
        f"| shops | opendata | `{DATASETS['shops']['dataset_id']}` | {DATASETS['shops']['year']} | https://opendata-ajuntament.barcelona.cat/data/dataset/{DATASETS['shops']['dataset_id']} | Ground-floor premises census; rows tagged as vacant/for-sale/for-rent premises ({', '.join(sorted(VACANT_SHOP_SECTORS))}) are excluded. |",
        f"| centroid, district polygons (districts.geojson) | opendata | `{DATASETS['geometry']['dataset_id']}` | {DATASETS['geometry']['year']} | https://opendata-ajuntament.barcelona.cat/data/dataset/{DATASETS['geometry']['dataset_id']} | District boundaries, published as WKT in ETRS89 / UTM zone 31N (EPSG:25831); converted to WGS84 in this script via an inverse transverse Mercator implementation (Snyder 1987 series), then Douglas-Peucker simplified (epsilon 0.00005 deg) and rounded to 5 decimals. Centroid is the polygon's area-weighted centroid (shoelace formula) in UTM, then converted. |",
        "| avg_rent_monthly | plausible | — | — | — | No district-level Open Data BCN average-asking-rent dataset was found (checked `package_search` for \"lloguer\", \"rent contracts\", \"manteniment-lloguer\" on 2026-09-24 — the latter covers rent-burden %, not EUR amounts). Hand-set per district reasoning: dense/touristic/central districts priced highest (Eixample > Gràcia ≈ Sant Martí > Ciutat Vella), Nou Barris cheapest. See `PLAUSIBLE_DEFAULTS` in the script. |",
        "| vacancy_rate | plausible | — | — | — | No district-level housing-vacancy dataset found. Hand-set, centred around city-wide rental vacancy norms (~3-6%). |",
        "| unemployment_rate | plausible | — | — | — | No district-level registered-unemployment dataset found (checked `package_search` for \"atur\", \"unemployment\", \"mercat de treball\" on 2026-09-24). Hand-set from the well-documented relative ranking of BCN district unemployment (Nou Barris/Ciutat Vella highest, Eixample/Gràcia lowest). |",
        "| jobs_per_resident | plausible | — | — | — | No job-location-count dataset found. Hand-set from each district's known economic role (Eixample/Ciutat Vella = commercial/office cores and net job importers; Gràcia/Nou Barris = mostly residential). |",
        "| transit_score | plausible | — | — | — | Subjective 0..1 connectivity score, hand-set from known metro/tram line density and topography (flat central districts score highest; hillier Nou Barris lowest). |",
        "",
        "## Reproducing",
        "",
        "```",
        "uv run python scripts/fetch_opendata.py [--refresh]",
        "```",
        "",
        "`--refresh` re-downloads raw files into `data/raw/` even if already cached; without it,",
        "cached raw files are reused. Raw files are gitignored (`data/raw/`); only the processed",
        "outputs in `data/processed/` are committed.",
        "",
    ]
    dest.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="re-download raw files")
    args = parser.parse_args()

    paths = fetch_raw(refresh=args.refresh)

    print("[process] population + age distribution")
    pop_age, pop_by_section = process_population_age(paths["population_age"])
    print("[process] income (population-weighted)")
    income = process_income(paths["income"], pop_by_section)
    print("[process] shops")
    shops = process_shops(paths["shops"])
    print("[process] geometry (ETRS89 UTM31N -> WGS84)")
    geo = process_geometry(paths["geometry"])

    for d in DISTRICTS:
        did = d["id"]
        lon, lat = geo[did]["centroid"]
        assert 2.05 <= lon <= 2.23, f"{did}: centroid lon {lon} outside Barcelona bounds"
        assert 41.32 <= lat <= 41.47, f"{did}: centroid lat {lat} outside Barcelona bounds"

    profiles = build_profiles(pop_age, income, shops, geo)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    districts_json = PROCESSED_DIR / "districts.json"
    districts_json.write_text(
        json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[write] {districts_json} ({districts_json.stat().st_size:,} bytes)")

    geojson_path = PROCESSED_DIR / "districts.geojson"
    write_geojson(geo, geojson_path)
    print(f"[write] {geojson_path} ({geojson_path.stat().st_size:,} bytes)")

    sources_path = PROCESSED_DIR / "SOURCES.md"
    write_sources_md(sources_path)
    print(f"[write] {sources_path} ({sources_path.stat().st_size:,} bytes)")

    for p in profiles:
        print(
            f"  {p['id']:12s} pop={p['population']:>7,} "
            f"income={p['income_per_capita_annual']:>8,.0f} "
            f"shops={p['shops']:>5,} "
            f"centroid={p['centroid']}"
        )


if __name__ == "__main__":
    sys.exit(main())
