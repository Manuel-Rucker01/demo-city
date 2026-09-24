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
import xml.etree.ElementTree as ET
import zipfile
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
DISTRICT_BY_CODI2 = {d["codi2"]: d["id"] for d in DISTRICTS}
# Barcelona has 10 districts in total (codes 1..10); several source files (rent workbook,
# transports layer) use these codes and are shared with barris that reuse the same 1..N
# numbering, so processing code needs to know how many district rows to expect before a
# barri/neighbourhood breakdown starts reusing small integers.
DISTRICTS_ALL_CODES = [str(n) for n in range(1, 11)]

# --- Dataset resources (dataset id / year chosen = latest complete year available at fetch time) --

DATASETS = {
    "rent": {
        "dataset_id": "lloguers-bcn-districtes-barris",
        "year": 2025,
        "url": (
            "https://habitatge.gencat.cat/web/.content/home/dades/estadistiques/"
            "01_Estadistiques_de_construccio_i_mercat_immobiliari/03_Mercat_de_lloguer/"
            "03_Lloguers_Barcelona_per_districtes_i_barris/anual_bcn_lloguer.xlsx"
        ),
        "raw_name": "anual_bcn_lloguer.xlsx",
        "notes": (
            "Mitjana anual del lloguer mitjà contractual (EUR/mes) per districte, Secretaria de "
            "l'Habitatge i Renovació Urbana (Generalitat de Catalunya) / INCASÒL, from deposited "
            "rental-contract deposits (near-census of all new contracts). Not an Open Data BCN "
            "dataset (no district-level rent dataset exists there); published by the Generalitat "
            "and linked from the Ajuntament's own Barcelona Dades portal."
        ),
    },
    "household_size": {
        "dataset_id": "pad_dom_mdbas_n-persones",
        "year": 2025,
        "url": (
            "https://opendata-ajuntament.barcelona.cat/data/dataset/90a4fb83-b2df-4571-b5e3-"
            "ce08d414de15/resource/26ac594a-68fd-458a-8c61-e17cdfff169c/download"
        ),
        "raw_name": "pad_dom_mdbas_n-persones_2025.csv",
        "notes": "Households by number of registered residents (N_PERSONES_AGG, capped at 9=9+), by census section, 1 Jan 2025 padro.",
    },
    "income_household": {
        "dataset_id": "atles-renda-bruta-per-llar",
        "year": 2023,
        "url": (
            "https://opendata-ajuntament.barcelona.cat/data/dataset/d8e40c96-9f1f-4fd3-86da-"
            "2baa1599616d/resource/fd12fd1f-5fe6-4642-9ace-34503c2a9dd5/download"
        ),
        "raw_name": "atles_renda_bruta_llar_2023.csv",
        "notes": "Average gross taxable income per household (EUR/year) by census section, INE/Idescat Atlas de distribucion de renta, latest available year (2023).",
    },
    "tenure_2011": {
        "dataset_id": "habit-ppal-segons-regim-tin",
        "year": 2011,
        "url": (
            "https://opendata-ajuntament.barcelona.cat/data/dataset/f9a88bc8-3b0a-4251-9f35-"
            "b68aef6889b8/resource/7f38e560-b38e-4649-93ff-67720ed79d35/download"
        ),
        "raw_name": "habit_ppal_regim_tin_2011.csv",
        "notes": (
            "Main dwellings by tenure regime (owned outright / owned with mortgage / owned by "
            "inheritance-donation / rented / ceded / other), by barri, Cens de Poblacio i "
            "Habitatges 2011 (INE/Idescat). Most recent district-resolution tenure-regime table "
            "found; no Open Data BCN dataset carries the 2021 census results at this resolution."
        ),
    },
    "transports": {
        "dataset_id": "transports",
        "year": 2025,
        "url": (
            "https://opendata-ajuntament.barcelona.cat/data/dataset/e0c34739-823f-470d-8045-"
            "e10f28e80f2d/resource/e07dec0d-4aeb-40f3-b987-e1f35e088ce2/download"
        ),
        "raw_name": "transports.csv",
        "notes": "Point layer of transport stations/access points (metro, FGC, tram, Rodalies/RENFE, airport train, funicular, cable car, maritime station) with district code. Current snapshot, not a yearly series (CKAN metadata last modified 2025-11-06).",
    },
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
# avg_rent_monthly and transit_score are now real/derived data (see DATASETS["rent"] and
# process_transit_score below). No district-level Open Data BCN or Generalitat dataset was
# found for rental vacancy rate or job-location counts (checked package_search for "atur",
# "lloguer", "unemployment", "rent contracts", "mercat de treball", "habitatges buits",
# "llocs de treball", "afiliacions", "cotitzacio" on 2026-09-24; also checked the Ajuntament's
# Barcelona Dades portal, Idescat and the Anuari Estadistic — see SOURCES.md for the trail).
# unemployment_rate similarly has no *machine-readable, district-level* series we could fetch
# and re-parse without adding a PDF-parsing dependency (the Ajuntament's own district unemployment
# figures are only published in PDF bulletins and a JS-rendered dashboard); the values below are
# hand-set but recalibrated to the real city-wide registered-unemployment rate reported by the
# Ajuntament for 2025 (5.6% of population 16-64 in March 2025, rising to 7.67% in the "Taxa
# d'atur" series reported by beteve.cat for May 2025 - the two differ in denominator/methodology,
# both cited in SOURCES.md), keeping the well-documented relative ranking across districts
# (Ciutat Vella/Nou Barris highest, Eixample/Gracia lowest) from the Ajuntament's own district
# unemployment bulletins and Barcelona Dades portal. NOT measured per-district data.
# Every value here is tagged Source.PLAUSIBLE with a "plausible: <reasoning>" ref.
PLAUSIBLE_DEFAULTS: dict[str, dict[str, float]] = {
    "ciutat_vella": {
        "vacancy_rate": 0.06,  # elevated by short-term-rental/renovation turnover
        "unemployment_rate": 0.09,  # historically the highest of the 10 districts
        "jobs_per_resident": 1.10,  # tourism/retail/office core, net job importer
    },
    "eixample": {
        "vacancy_rate": 0.04,
        "unemployment_rate": 0.05,  # affluent, below city average
        "jobs_per_resident": 1.40,  # largest office/commercial concentration
    },
    "gracia": {
        "vacancy_rate": 0.03,
        "unemployment_rate": 0.05,
        "jobs_per_resident": 0.55,  # mostly residential, net job exporter
    },
    "nou_barris": {
        "vacancy_rate": 0.05,
        "unemployment_rate": 0.10,  # among the highest, historically lower income
        "jobs_per_resident": 0.30,  # residential dormitory district, few local jobs
    },
    "sant_marti": {
        "vacancy_rate": 0.04,
        "unemployment_rate": 0.07,
        "jobs_per_resident": 0.75,  # 22@ tech/office district raises job count
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


def process_household_size(path: Path) -> tuple[dict[str, float], dict[tuple[str, int], float]]:
    """Returns (avg_household_size per district, households per (codi, section_num)).

    N_PERSONES_AGG is the household-size band (1..9, where 9 means "9 or more"); Valor is the
    number of households in that census section with that many registered residents. The
    household-count-by-section map is reused by process_income_household to household-weight
    (rather than population-weight) the per-household income figure.
    """
    size_sum: dict[str, float] = defaultdict(float)
    hh_total: dict[str, float] = defaultdict(float)
    hh_by_section: dict[tuple[str, int], float] = defaultdict(float)
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            codi = row["Codi_Districte"]
            if codi not in DISTRICT_BY_CODI:
                continue
            did = DISTRICT_BY_CODI[codi]["id"]
            n = int(row["N_PERSONES_AGG"])
            v = float(row["Valor"])
            size_sum[did] += n * v
            hh_total[did] += v
            section_num = int(row["Seccio_Censal"]) - int(codi) * 1000
            hh_by_section[(codi, section_num)] += v
    avg_size = {d["id"]: round(size_sum[d["id"]] / hh_total[d["id"]], 3) for d in DISTRICTS}
    return avg_size, dict(hh_by_section)


def process_income_household(
    path: Path, hh_by_section: dict[tuple[str, int], float]
) -> dict[str, float]:
    """Household-weighted mean gross taxable income per household (EUR/year) per district."""
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
            w = hh_by_section.get((codi, section_num), 0.0)
            if w <= 0:
                continue
            weighted_sum[did] += float(row["Import_Renda_Bruta_€"]) * w
            weight_total[did] += w
    return {d["id"]: round(weighted_sum[d["id"]] / weight_total[d["id"]], 2) for d in DISTRICTS}


def _parse_es_thousands(s: str) -> float:
    """Parse a Spanish-locale number using '.' as the thousands separator; '..' = suppressed."""
    s = s.strip()
    if not s or s in ("..", "...", "…"):
        return 0.0
    return float(s.replace(".", ""))


def process_owner_share(path: Path) -> dict[str, float]:
    """Share of main dwellings that are owner-occupied (outright, mortgaged or inherited/donated)
    per district, aggregated from barri-level rows of the 2011 Cens de Poblacio i Habitatges.
    """
    owned: dict[str, float] = defaultdict(float)
    total: dict[str, float] = defaultdict(float)
    with path.open(encoding="latin-1") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dte = row["Dte"].strip()
            if dte not in DISTRICT_BY_CODI:
                continue
            did = DISTRICT_BY_CODI[dte]["id"]
            total[did] += _parse_es_thousands(row["Total"])
            owned[did] += (
                _parse_es_thousands(row["PropiPerCompraTotalmentPagat"])
                + _parse_es_thousands(row["PropiPerCompraAmbPagamentsPendents(hipoteques)"])
                + _parse_es_thousands(row["PropiPerHerenciaODonacio"])
            )
    return {d["id"]: round(owned[d["id"]] / total[d["id"]], 4) for d in DISTRICTS}


# Transit-equipment categories counted towards transit_score, and their weight: heavy/regional
# rail-type modes (metro, FGC, Rodalies/RENFE, airport train) get full weight; tram gets half.
# Funiculars, cable cars and the maritime station are excluded (niche/tourist, not everyday
# commuter transit). Barcelona's own district code (DISTRICTE, zero-padded) identifies rows.
TRANSIT_WEIGHTS: dict[str, float] = {
    "Metro i línies urbanes FGC": 1.0,
    "Ferrocarrils Generalitat (FGC)": 1.0,
    "RENFE": 1.0,
    "Tren a l'aeroport": 1.0,
    "Tramvia": 0.5,
}


def process_transit_score(path: Path, area_km2: dict[str, float]) -> dict[str, float]:
    """0..1 transit connectivity: weighted count of transit-equipment points (see
    TRANSIT_WEIGHTS) per km^2 of district area, normalized so the densest of the 5 districts
    scores 1.0. The dataset records access points (e.g. multiple entrances per station), so
    this is a density of transit infrastructure, not a station headcount - still a reasonable,
    reproducible proxy for connectivity.
    """
    weighted: dict[str, float] = defaultdict(float)
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            w = TRANSIT_WEIGHTS.get(row["NOM_CAPA"])
            if not w:
                continue
            codi2 = row["DISTRICTE"].strip()
            did = DISTRICT_BY_CODI2.get(codi2)
            if did is None:
                continue
            weighted[did] += w
    density = {d["id"]: weighted[d["id"]] / area_km2[d["id"]] for d in DISTRICTS}
    max_density = max(density.values())
    return {did: round(v / max_density, 4) for did, v in density.items()}


# --- Rent: minimal .xlsx reader (stdlib zipfile + xml, no openpyxl) -------------------------

_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _xlsx_col_letters(cell_ref: str) -> str:
    letters = ""
    for ch in cell_ref:
        if ch.isalpha():
            letters += ch
        else:
            break
    return letters


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        data = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(data)
    return [
        "".join(t.text or "" for t in si.findall(".//" + _XLSX_NS + "t"))
        for si in root.findall(_XLSX_NS + "si")
    ]


def _xlsx_sheet_rows(
    zf: zipfile.ZipFile, sheet: str = "xl/worksheets/sheet1.xml"
) -> dict[int, dict[str, Any]]:
    """Returns {row_number: {column_letters: resolved_value}} for one worksheet."""
    shared = _xlsx_shared_strings(zf)
    root = ET.fromstring(zf.read(sheet))
    rows: dict[int, dict[str, Any]] = {}
    sheet_data = root.find(_XLSX_NS + "sheetData")
    if sheet_data is None:
        return rows
    for row_el in sheet_data.findall(_XLSX_NS + "row"):
        cells: dict[str, Any] = {}
        for c in row_el.findall(_XLSX_NS + "c"):
            col = _xlsx_col_letters(c.get("r"))
            v_el = c.find(_XLSX_NS + "v")
            if v_el is None or v_el.text is None:
                cells[col] = None
                continue
            v = v_el.text
            if c.get("t") == "s":
                cells[col] = shared[int(v)]
            else:
                try:
                    cells[col] = float(v)
                except ValueError:
                    cells[col] = v
        rows[int(row_el.get("r"))] = cells
    return rows


def process_rent(path: Path) -> tuple[dict[str, float], int, dict[str, float], int]:
    """Returns (avg_rent_monthly per district, latest_year, prev_year_rent per district,
    prev_year). Reads the Generalitat's "Lloguers Barcelona per districtes i barris" workbook:
    one row per district code 1..10 (in that order) directly below the "Codi" header row, then
    a blank separator row before the barri-level breakdown (which reuses codes 1.. and must not
    be picked up as districts).
    """
    with zipfile.ZipFile(path) as zf:
        rows = _xlsx_sheet_rows(zf)

    header_row = next(rn for rn, cells in rows.items() if cells.get("A") == "Codi")
    year_cols: dict[int, str] = {}
    for col, val in rows[header_row].items():
        if col in ("A", "B") or val in (None, ""):
            continue
        try:
            year_cols[int(float(val))] = col
        except (TypeError, ValueError):
            continue
    latest_year = max(year_cols)
    prev_year = latest_year - 1
    latest_col = year_cols[latest_year]
    prev_col = year_cols.get(prev_year)

    latest: dict[str, float] = {}
    prev: dict[str, float] = {}
    n_district_rows = 0
    for rn in sorted(rn for rn in rows if rn > header_row):
        a = rows[rn].get("A")
        if a is None:
            continue
        n_district_rows += 1
        if n_district_rows > len(DISTRICTS_ALL_CODES):
            break
        codi = str(int(a))
        d = DISTRICT_BY_CODI.get(codi)
        if d is None:
            continue
        latest[d["id"]] = round(rows[rn][latest_col], 2)
        if prev_col is not None and rows[rn].get(prev_col) is not None:
            prev[d["id"]] = round(rows[rn][prev_col], 2)
    return latest, latest_year, prev, prev_year


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


def polygon_area(ring: list[tuple[float, float]]) -> float:
    """Unsigned area of a simple polygon ring (shoelace formula), in the ring's own units^2."""
    a = 0.0
    n = len(ring)
    for i in range(n - 1):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        a += x0 * y1 - x1 * y0
    return abs(a) * 0.5


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
    """Returns {district_id: {"centroid": (lon, lat), "geometry": geojson-geometry-dict,
    "area_km2": float}}. area_km2 is computed in the source UTM projection (equal-area enough
    at this scale), used by process_transit_score to get a station density.
    """
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
        area_km2 = polygon_area(exterior_utm) / 1e6

        exterior_wgs84 = [utm_to_wgs84(x, y) for x, y in exterior_utm]
        # simplify: ~0.00005 deg ~= 5.5m at this latitude, then round to 5 decimals (~1.1m)
        simplified = douglas_peucker(exterior_wgs84, epsilon=0.00005)
        rounded = [[round(lon, 5), round(lat, 5)] for lon, lat in simplified]
        if rounded[0] != rounded[-1]:
            rounded.append(rounded[0])

        out[d["id"]] = {
            "centroid": (round(centroid_lonlat[0], 5), round(centroid_lonlat[1], 5)),
            "geometry": {"type": "Polygon", "coordinates": [rounded]},
            "area_km2": area_km2,
        }
    return out


# --- Assembly ------------------------------------------------------------------------------


def build_profiles(
    pop_age: dict[str, dict[str, Any]],
    income: dict[str, float],
    shops: dict[str, int],
    geo: dict[str, dict[str, Any]],
    rent: dict[str, float],
    rent_year: int,
    rent_prev: dict[str, float],
    rent_prev_year: int,
    household_size: dict[str, float],
    income_household: dict[str, float],
    owner_share: dict[str, float],
    transit_score: dict[str, float],
) -> list[dict[str, Any]]:
    profiles = []
    for d in DISTRICTS:
        did = d["id"]
        plausible = PLAUSIBLE_DEFAULTS[did]
        sources = {
            "population": "opendata",
            "age_distribution": "derived",
            "income_per_capita_annual": "opendata",
            "avg_rent_monthly": "opendata",
            "vacancy_rate": "plausible",
            "unemployment_rate": "plausible",
            "jobs_per_resident": "plausible",
            "shops": "opendata",
            "transit_score": "derived",
            "income_per_household_annual": "opendata",
            "owner_share": "opendata",
            "avg_household_size": "opendata",
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
            "avg_rent_monthly": (
                f"Secretaria de l'Habitatge (Generalitat de Catalunya) / INCASO, mitjana anual "
                f"del lloguer mitja contractual, {rent_year} "
                f"(prev. year {rent_prev_year}: {rent_prev.get(did)} EUR/month); "
                f"{DATASETS['rent']['url']}"
            ),
            "vacancy_rate": "plausible: no district-level rental-market vacancy dataset found (INE census 'viviendas vacias' is a different, stock-based definition and was not found broken out by BCN district either)",
            "unemployment_rate": (
                "plausible, recalibrated to the city-wide registered-unemployment rate reported "
                "by the Ajuntament for 2025 (5.6% of pop. 16-64, March 2025; 7.67% 'taxa d'atur', "
                "May 2025, per beteve.cat) with the known relative district ranking; no "
                "machine-readable district-level series found (only PDF bulletins and a "
                "JS-rendered dashboard on portaldades.ajuntament.barcelona.cat) - see SOURCES.md"
            ),
            "jobs_per_resident": "plausible: no job-location dataset found; qualitative district role (residential vs commercial core)",
            "shops": (
                f"{DATASETS['shops']['dataset_id']} ({DATASETS['shops']['year']}), "
                "ground-floor premises, rows with Nom_Sector_Activitat in "
                f"{sorted(VACANT_SHOP_SECTORS)} excluded as vacant"
            ),
            "transit_score": (
                f"derived from {DATASETS['transports']['dataset_id']} "
                f"({DATASETS['transports']['year']}): weighted count of transit-equipment points "
                f"{sorted(TRANSIT_WEIGHTS)} per km^2 of district area (area from "
                f"{DATASETS['geometry']['dataset_id']}), normalized to the densest district = 1.0"
            ),
            "income_per_household_annual": (
                f"{DATASETS['income_household']['dataset_id']} "
                f"({DATASETS['income_household']['year']}), gross (not disposable) taxable "
                "income per household, household-weighted mean of census-section values "
                f"(weights from {DATASETS['household_size']['dataset_id']} "
                f"{DATASETS['household_size']['year']})"
            ),
            "owner_share": (
                f"{DATASETS['tenure_2011']['dataset_id']} ({DATASETS['tenure_2011']['year']}), "
                "share of main dwellings owned outright, with a mortgage, or by inheritance/"
                "donation (rented + ceded-free/cheap + other = non-owner); barri rows summed to "
                "district. Most recent district-resolution tenure table found - see SOURCES.md "
                "for why the 2021 census and 2017 Enquesta Sociodemografica were not usable here."
            ),
            "avg_household_size": (
                f"{DATASETS['household_size']['dataset_id']} "
                f"({DATASETS['household_size']['year']}), mean of N_PERSONES_AGG (household-size "
                "band, capped at 9=9+) weighted by household counts, by census section"
            ),
        }
        profiles.append(
            {
                "id": did,
                "name": d["name"],
                "population": pop_age[did]["population"],
                "age_distribution": pop_age[did]["age_distribution"],
                "income_per_capita_annual": income[did],
                "avg_rent_monthly": rent[did],
                "vacancy_rate": plausible["vacancy_rate"],
                "unemployment_rate": plausible["unemployment_rate"],
                "jobs_per_resident": plausible["jobs_per_resident"],
                "shops": shops[did],
                "transit_score": transit_score[did],
                "centroid": list(geo[did]["centroid"]),
                "sources": sources,
                "refs": refs,
                "income_per_household_annual": income_household[did],
                "owner_share": owner_share[did],
                "avg_household_size": household_size[did],
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


def write_sources_md(dest: Path, rent_year: int, rent_prev_year: int) -> None:
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
        f"| centroid, district polygons (districts.geojson) | opendata | `{DATASETS['geometry']['dataset_id']}` | {DATASETS['geometry']['year']} | https://opendata-ajuntament.barcelona.cat/data/dataset/{DATASETS['geometry']['dataset_id']} | District boundaries, published as WKT in ETRS89 / UTM zone 31N (EPSG:25831); converted to WGS84 in this script via an inverse transverse Mercator implementation (Snyder 1987 series), then Douglas-Peucker simplified (epsilon 0.00005 deg) and rounded to 5 decimals. Centroid is the polygon's area-weighted centroid (shoelace formula) in UTM, then converted. Area (km^2, used by transit_score) is the same ring's shoelace area in UTM. |",
        f"| avg_rent_monthly | opendata | `{DATASETS['rent']['dataset_id']}` | {rent_year} | {DATASETS['rent']['url']} | Mitjana anual del lloguer mitja contractual (EUR/month) per district, published by the Secretaria de l'Habitatge i Renovacio Urbana (Generalitat de Catalunya), based on INCASO rental-deposit records (near-census of new contracts). Not an Open Data BCN dataset - found via the Ajuntament's own Barcelona Dades portal (`portaldades.ajuntament.barcelona.cat`), which links to this Generalitat workbook rather than hosting district-level rent figures itself. {rent_prev_year} is also kept for comparison (rents fell slightly city-wide between {rent_prev_year} and {rent_year}). Parsed with a small stdlib `zipfile`/`xml` reader (no openpyxl). |",
        "| vacancy_rate | plausible | — | — | — | No district-level rental-market vacancy dataset found (checked `package_search` for \"habitatges buits\", \"viviendas vacias\", \"habitatge buit\", \"habitatges desocupats\" on 2026-09-24). INE Census 2021 publishes a *stock* \"viviendas vacias\" figure, a different concept from rental-market vacancy, and it was not found broken out by Barcelona district either. Hand-set, centred around city-wide rental vacancy norms (~3-6%). |",
        "| unemployment_rate | plausible | — | — | — | No *machine-readable, district-level* registered-unemployment series was found: the Ajuntament publishes district unemployment only as PDF bulletins (e.g. `barcelonactiva.cat` monthly \"Evolucio de l'atur registrat\", city-wide only) or through a JS-rendered dashboard (`portaldades.ajuntament.barcelona.cat/estadistiques/noypwz2129` \"Taxa d'atur registral\", no exposed API found), and the BCNROC Anuari Estadistic PDF that likely has the table could not be fetched (rate-limited by the host's WAF on 2026-09-24). District *headcounts* for April 2022 are reported by local press (totbarcelona.cat, citing the Ajuntament's Departament d'Estudis/Observatori Municipal de Dades) but combining a 2022 headcount with 2025 population would mix vintages, so they were not used numerically. Hand-set instead, recalibrated to the city-wide rate the Ajuntament reported for 2025 (5.6% of pop. 16-64 in March 2025, per the Departament d'Estudis bulletin; 7.67% \"taxa d'atur\", May 2025, per beteve.cat - the two use different denominators/methodologies and are both cited so the discrepancy is visible), keeping the well-documented relative ranking (Ciutat Vella/Nou Barris highest, Eixample/Gracia lowest, consistent with the 2022 headcount ordering and the 2017 Enquesta Sociodemografica). |",
        "| jobs_per_resident | plausible | — | — | — | No job-location-count dataset found (checked `package_search` for \"llocs de treball\", \"afiliacions\", \"cotitzacio\", \"centres de treball\", \"empreses\" on 2026-09-24; the closest matches, `rebuts-quota-padro-iae` and `cens-locals-planta-baixa-act-economica`, count tax quotas / ground-floor premises, not jobs). Hand-set from each district's known economic role (Eixample/Ciutat Vella = commercial/office cores and net job importers; Gràcia/Nou Barris = mostly residential). |",
        f"| transit_score | derived | `{DATASETS['transports']['dataset_id']}` | {DATASETS['transports']['year']} | https://opendata-ajuntament.barcelona.cat/data/dataset/{DATASETS['transports']['dataset_id']} | Weighted count of transit-equipment points (metro/urban-FGC, regional FGC, RENFE/Rodalies, airport train = weight 1.0; tram = weight 0.5; funicular/cable-car/maritime-station excluded) per km^2 of district area, normalized so the densest of the 5 districts scores 1.0. The dataset records access points/entrances rather than unique stations, so this is a transit-infrastructure density, not a station headcount. |",
        f"| income_per_household_annual | opendata | `{DATASETS['income_household']['dataset_id']}` | {DATASETS['income_household']['year']} | https://opendata-ajuntament.barcelona.cat/data/dataset/{DATASETS['income_household']['dataset_id']} | Average **gross** taxable income per household (not disposable income - compare with `income_per_capita_annual`, which is disposable), INE/Idescat Atles de distribucio de renda, by census section, household-weighted up to district (weights from `{DATASETS['household_size']['dataset_id']}` {DATASETS['household_size']['year']}). |",
        f"| owner_share | opendata | `{DATASETS['tenure_2011']['dataset_id']}` | {DATASETS['tenure_2011']['year']} | https://opendata-ajuntament.barcelona.cat/data/dataset/{DATASETS['tenure_2011']['dataset_id']} | Share of main dwellings owned (outright, mortgaged, or by inheritance/donation), Cens de Poblacio i Habitatges 2011, barri rows summed to district. This is the most recent *district-resolution* tenure table found: the 2021 census results were not found published at Barcelona-district resolution (INE/Idescat publish tenure regime at province/municipality level, and the Open Data BCN catalogue's only tenure-regime dataset is this 2011 one), and the 2017 Enquesta Sociodemografica de Barcelona reports only a city-wide figure (57.6% owner-occupied) plus a qualitative district ranking, not per-district percentages. The 2011 district figures are consistent with that later city-wide number (city-wide weighted average from this table: ~61.9%). |",
        f"| avg_household_size | opendata | `{DATASETS['household_size']['dataset_id']}` | {DATASETS['household_size']['year']} | https://opendata-ajuntament.barcelona.cat/data/dataset/{DATASETS['household_size']['dataset_id']} | Mean household size (persons per household), padro households by number of registered residents (N_PERSONES_AGG, capped at 9=9+), weighted by household count, by census section. |",
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
    print("[process] income per capita (population-weighted)")
    income = process_income(paths["income"], pop_by_section)
    print("[process] shops")
    shops = process_shops(paths["shops"])
    print("[process] geometry (ETRS89 UTM31N -> WGS84)")
    geo = process_geometry(paths["geometry"])
    print("[process] rent (Generalitat workbook)")
    rent, rent_year, rent_prev, rent_prev_year = process_rent(paths["rent"])
    print("[process] household size + household counts by section")
    household_size, hh_by_section = process_household_size(paths["household_size"])
    print("[process] income per household (household-weighted)")
    income_household = process_income_household(paths["income_household"], hh_by_section)
    print("[process] owner share (2011 census tenure)")
    owner_share = process_owner_share(paths["tenure_2011"])
    print("[process] transit score (station density)")
    area_km2 = {d["id"]: geo[d["id"]]["area_km2"] for d in DISTRICTS}
    transit_score = process_transit_score(paths["transports"], area_km2)

    for d in DISTRICTS:
        did = d["id"]
        lon, lat = geo[did]["centroid"]
        assert 2.05 <= lon <= 2.23, f"{did}: centroid lon {lon} outside Barcelona bounds"
        assert 41.32 <= lat <= 41.47, f"{did}: centroid lat {lat} outside Barcelona bounds"

    profiles = build_profiles(
        pop_age,
        income,
        shops,
        geo,
        rent,
        rent_year,
        rent_prev,
        rent_prev_year,
        household_size,
        income_household,
        owner_share,
        transit_score,
    )

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
    write_sources_md(sources_path, rent_year, rent_prev_year)
    print(f"[write] {sources_path} ({sources_path.stat().st_size:,} bytes)")

    for p in profiles:
        print(
            f"  {p['id']:12s} pop={p['population']:>7,} "
            f"income={p['income_per_capita_annual']:>8,.0f} "
            f"rent={p['avg_rent_monthly']:>7,.0f} "
            f"owner={p['owner_share']:.2f} "
            f"hh_size={p['avg_household_size']:.2f} "
            f"transit={p['transit_score']:.2f} "
            f"shops={p['shops']:>5,} "
            f"centroid={p['centroid']}"
        )


if __name__ == "__main__":
    sys.exit(main())
