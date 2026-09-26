"""Fetch raw inputs for scripts/build_transit_access.py into data/raw/transit/ (gitignored).

Re-runnable: skips files already cached unless --refresh is passed. Network access required on
first run (Open Data BCN CKAN downloads, Overpass API queries, Nominatim geocoding of two street
names). Overpass's public instance is often overloaded for tag-value queries like
`railway=construction`; this script retries with backoff and falls back across mirrors, matching
what was needed to build the cache in data/raw/transit/ during development.

Usage:
    uv run python scripts/fetch_transit_raw.py [--refresh]

Owner: T12 (transit data). See docs/TRANSIT_ACCESS.md.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "transit"

UA_OPENDATA = "Mozilla/5.0 (compatible; jevcity-data-fetch/1.0)"
UA_OSM = "jevcity-research/1.0 (contact: claudecodemail2026@gmail.com)"

CKAN_DOWNLOADS = {
    # Barris polygons + codes + names (73 rows), Open Data BCN 20170706-districtes-barris.
    "barcelonaciutat_barris.json": (
        "https://opendata-ajuntament.barcelona.cat/data/dataset/808daafa-d9ce-48c0-925a-"
        "fa5afdb1ed41/resource/75197dfe-0306-4c5e-9643-34948af07fb6/download"
    ),
    # Census section polygons (1068 rows) with barri/district codes, same dataset.
    "barcelonaciutat_seccionscensals.json": (
        "https://opendata-ajuntament.barcelona.cat/data/dataset/808daafa-d9ce-48c0-925a-"
        "fa5afdb1ed41/resource/db90a207-d125-4f80-aac5-f9d5d6e648f5/download"
    ),
    # Padro population by district/barri/seccio censal x 5-year age band (2025), same source
    # districts.json already uses (pad_mdbas_edat-q). Reused here for census-section population.
    "pad_mdbas_edat-q_2025.csv": (
        "https://opendata-ajuntament.barcelona.cat/data/dataset/a45e1f19-5137-45bd-8979-"
        "645906fde55b/resource/c5af1fec-95bc-4f25-8adc-c0313cfe0144/download"
    ),
    # Ground-floor commercial premises census by barri (2024) -- job_weight proxy input.
    "cens_comercial_bcn_2024.csv": (
        "https://opendata-ajuntament.barcelona.cat/data/dataset/fe177673-0f83-42e7-b35a-"
        "ddea901be8bc/resource/38babeec-5c47-43d3-84e7-b13a4b89004f/download/"
        "241021_censcomercialbcn_opendata_2024_v5.csv"
    ),
}

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]

# name -> Overpass QL query. Split into small, well-formed queries (Overpass's public instance
# frequently 504s on broad tag-value scans like `railway=construction` with no other filter;
# splitting by mode and backing off between calls made every one of these succeed).
OVERPASS_QUERIES = {
    # Metro L1-L11 (incl. L9 Nord/Sud, L10 Nord/Sud) + FGC L6/L7/L8/L12 route relations,
    # all tagged route=subway in OSM. Ordered stop-position members give the line topology.
    "overpass_subway.json": (
        '[out:json][timeout:120][bbox:41.28,1.95,41.50,2.30];\n'
        "relation[route=subway];\nout body;\n>;\nout skel qt;\n"
    ),
    # route=light_rail is empty for this bbox (checked); kept for provenance/reproducibility.
    "overpass_lightrail.json": (
        '[out:json][timeout:120][bbox:41.28,1.95,41.50,2.30];\n'
        "relation[route=light_rail];\nout body;\n>;\nout skel qt;\n"
    ),
    # Trambaix/Trambesos T1-T6 route relations.
    "overpass_tram.json": (
        '[out:json][timeout:120][bbox:41.28,1.95,41.50,2.30];\n'
        "relation[route=tram];\nout body;\n>;\nout skel qt;\n"
    ),
    # Named station points for metro (station=subway) + FGC (operator match), used to attach
    # names to the anonymous stop-position nodes in the route relations above.
    "overpass_named_stations.json": (
        '[out:json][timeout:90][bbox:41.28,1.95,41.50,2.30];\n'
        "(\n"
        "  node[railway=station][station=subway];\n"
        '  node[railway=station][operator~"Ferrocarrils de la Generalitat"];\n'
        ");\nout body;\n"
    ),
    # Named tram stop points.
    "overpass_tramstops.json": (
        '[out:json][timeout:90][bbox:41.28,1.95,41.50,2.30];\n'
        "node[railway=tram_stop];\nout body;\n"
    ),
    # Rodalies/Renfe station nodes (no usable route relation could be fetched: route=train
    # relations time out even with ref-regex + bbox filters on the public Overpass instance --
    # the underlying R-line relations span all of Catalonia. Line topology for these is
    # therefore built from a documented, hand-encoded real-world ordering, not from a route
    # relation -- see build_transit_access.py::RODALIES_LINES).
    "overpass_rodalies_nodes.json": (
        '[out:json][timeout:90][bbox:41.28,1.95,41.50,2.30];\n'
        "(\n"
        '  node[railway=station][network~"Rodalies|Renfe"];\n'
        '  node[railway=halt][network~"Rodalies|Renfe"];\n'
        '  node[railway=station][operator~"Renfe"];\n'
        ");\nout body;\n"
    ),
    # FGC-operator nodes not already covered above: the Barcelona-Valles trunk (S1/S2, shared
    # with L6/L7 as far as Sarria) plus funicular-adjacent points. Same caveat as Rodalies:
    # no route relation for S1/S2 could be fetched; ordering is hand-encoded (documented,
    # public real-world knowledge of the line), not from OSM.
    "overpass_fgc_nodes.json": (
        '[out:json][timeout:90][bbox:41.28,1.95,41.50,2.30];\n'
        "(\n"
        '  node[railway=station][operator~"FGC|Generalitat"];\n'
        '  node[railway=halt][operator~"FGC|Generalitat"];\n'
        ");\nout body;\n"
    ),
    # Barcelona-Sants (Adif/Rodalies) isn't tagged with a network value the query above
    # catches, and Barcelona-Placa Catalunya's FGC platform is worth double-checking by name.
    "overpass_extra_nodes.json": (
        '[out:json][timeout:60][bbox:41.28,1.95,41.50,2.30];\n'
        "(\n"
        '  node[railway=station][name~"Sants"];\n'
        '  node[railway=station][name~"Plaça Catalunya"];\n'
        ");\nout body;\n"
    ),
    # L9/L10 central-section construction: OSM contributors tag in-progress stations
    # railway=construction / construction=station with their real name and (surveyed or
    # planned) position. This is the primary source for the new stations' coordinates.
    "overpass_l9_construction.json": (
        '[out:json][timeout:90][bbox:41.37,2.09,41.44,2.20];\n'
        "(\n"
        '  node[name~"Manuel Girona|Campus Nord|Prat de la Riba|Travessera de Dalt|'
        'Sanllehy|Guinardó.*Sant Pau|Mandri"];\n'
        '  node[name~"Putxet"];\n'
        ");\nout body;\n"
    ),
}

NOMINATIM_QUERIES = {
    # Used only as a fallback for the one new L9 station (Prat de la Riba) with no
    # railway=construction node found in OSM: geocode the street it is named after,
    # per docs/TRANSIT_ACCESS.md's "approx: true" allowance.
    "nominatim_prat_de_la_riba.json": "Avinguda de Prat de la Riba, Barcelona, Spain",
    "nominatim_travessera_de_dalt.json": "Travessera de Dalt, Barcelona, Spain",
}


def http_get(url: str, headers: dict[str, str], retries: int = 4) -> bytes:
    last_exc: Exception | None = None
    with httpx.Client(follow_redirects=True, timeout=90.0, headers=headers) as client:
        for attempt in range(retries):
            try:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.content
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < retries - 1:
                    time.sleep(3 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def overpass_post(query: str, retries: int = 5) -> bytes:
    last_exc: Exception | None = None
    with httpx.Client(timeout=180.0, headers={"User-Agent": UA_OSM}) as client:
        for attempt in range(retries):
            for mirror in OVERPASS_MIRRORS:
                try:
                    resp = client.post(mirror, data={"data": query})
                    if resp.status_code == 200 and resp.content.strip().startswith(b"{"):
                        return resp.content
                    last_exc = RuntimeError(f"{mirror}: HTTP {resp.status_code}")
                except httpx.TransportError as exc:
                    last_exc = exc
            time.sleep(8 * (attempt + 1))  # Overpass's public instance needs real backoff
    raise last_exc  # type: ignore[misc]


def fetch_raw(refresh: bool = False) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    for name, url in CKAN_DOWNLOADS.items():
        dest = RAW_DIR / name
        if dest.exists() and not refresh:
            print(f"[skip] {name} (cached)")
            continue
        print(f"[fetch] {name} <- {url}")
        data = http_get(url, headers={"User-Agent": UA_OPENDATA})
        dest.write_bytes(data)
        print(f"        -> {dest} ({len(data):,} bytes)")

    for name, query in OVERPASS_QUERIES.items():
        dest = RAW_DIR / name
        if dest.exists() and not refresh:
            print(f"[skip] {name} (cached)")
            continue
        print(f"[fetch] {name} <- Overpass API")
        data = overpass_post(query)
        dest.write_bytes(data)
        print(f"        -> {dest} ({len(data):,} bytes)")
        time.sleep(2)  # be polite between successive Overpass calls

    for name, q in NOMINATIM_QUERIES.items():
        dest = RAW_DIR / name
        if dest.exists() and not refresh:
            print(f"[skip] {name} (cached)")
            continue
        print(f"[fetch] {name} <- Nominatim ({q!r})")
        url = f"https://nominatim.openstreetmap.org/search?q={httpx.QueryParams({'q': q})['q']}&format=json&limit=3"
        data = http_get(url, headers={"User-Agent": UA_OSM})
        dest.write_bytes(data)
        print(f"        -> {dest} ({len(data):,} bytes)")
        time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    fetch_raw(refresh=args.refresh)
    print("Done. Raw inputs cached in", RAW_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
