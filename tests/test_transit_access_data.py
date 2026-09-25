"""Sanity checks on data/processed/transit_access.json (built by scripts/build_transit_access.py).

Skipped entirely if the file is missing (it is not committed if the raw fetch/build wasn't run in
this environment). Owner: T12 (transit data). Contract: docs/TRANSIT_ACCESS.md.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from jevcity.types import TransitAccess

ROOT = Path(__file__).resolve().parent.parent
ACCESS_PATH = ROOT / "data" / "processed" / "transit_access.json"
DISTRICTS_PATH = ROOT / "data" / "processed" / "districts.json"

pytestmark = pytest.mark.skipif(not ACCESS_PATH.exists(), reason="transit_access.json not built")


@pytest.fixture(scope="module")
def access() -> TransitAccess:
    return TransitAccess.model_validate_json(ACCESS_PATH.read_text())


@pytest.fixture(scope="module")
def zone_index(access: TransitAccess) -> dict[str, int]:
    return {z.id: i for i, z in enumerate(access.zones)}


@pytest.fixture(scope="module")
def name_index(access: TransitAccess) -> dict[str, int]:
    return {z.name: i for i, z in enumerate(access.zones)}


def test_validates_against_schema():
    # model_validate_json already ran in the `access` fixture; re-run explicitly per the contract's
    # acceptance wording ("validate it by loading with TransitAccess.model_validate_json").
    TransitAccess.model_validate_json(ACCESS_PATH.read_text())


def test_73_zones(access: TransitAccess):
    assert len(access.zones) == 73
    assert len({z.id for z in access.zones}) == 73


def test_variants_and_modes(access: TransitAccess):
    assert access.variants[0] == "base"
    assert set(access.variants) == {"base", "l9_central"}
    assert set(access.modes) == {"metro", "bus", "car", "bike", "walk"}


def test_matrix_sizes(access: TransitAccess):
    n = len(access.zones)
    for variant in access.variants:
        assert set(access.times[variant]) == set(access.modes)
        for mode in access.modes:
            assert len(access.times[variant][mode]) == n * n


def test_every_district_has_a_zone(access: TransitAccess):
    districts = json.loads(DISTRICTS_PATH.read_text())
    district_ids = {d["id"] for d in districts}
    zone_districts = {z.district for z in access.zones}
    for d in district_ids:
        assert d in zone_districts, f"district {d} has no zone"
    for z in access.zones:
        assert z.district in district_ids


def test_job_weight_sums_to_one_per_district(access: TransitAccess):
    totals: dict[str, float] = {}
    for z in access.zones:
        totals[z.district] = totals.get(z.district, 0.0) + z.job_weight
    for district, total in totals.items():
        assert math.isclose(total, 1.0, abs_tol=1e-3), f"{district}: job_weight sums to {total}"


def test_all_times_positive_and_finite(access: TransitAccess):
    for variant in access.variants:
        for mode in access.modes:
            for t in access.times[variant][mode]:
                assert math.isfinite(t)
                assert t > 0


def test_l9_central_metro_never_slower_than_base(access: TransitAccess):
    base = access.times["base"]["metro"]
    l9 = access.times["l9_central"]["metro"]
    assert len(base) == len(l9)
    violations = [(a, b) for a, b in zip(base, l9) if b > a + 1e-6]
    assert not violations, f"{len(violations)} zone pairs got slower under l9_central: {violations[:5]}"


def test_l9_central_strictly_faster_for_some_pairs_near_the_line(access: TransitAccess, zone_index):
    base = access.times["base"]["metro"]
    l9 = access.times["l9_central"]["metro"]
    n = len(access.zones)
    target_districts = {"sarria_sant_gervasi", "gracia", "horta_guinardo"}
    found = False
    for i, zi in enumerate(access.zones):
        if zi.district not in target_districts:
            continue
        for j in range(n):
            m = i * n + j
            if base[m] - l9[m] > 1e-6:
                found = True
                break
        if found:
            break
    assert found, "expected at least one strictly-faster l9_central metro pair touching Sarria-Sant Gervasi/Gracia/Horta-Guinardo"


def test_walk_sarria_to_sant_andreu_over_60_min(access: TransitAccess, name_index):
    n = len(access.zones)
    i = name_index["Sarrià"]
    j = name_index["Sant Andreu"]
    t = access.times["base"]["walk"][i * n + j]
    assert t > 60


def test_new_coverage_bounds_and_ciutat_vella_zero(access: TransitAccess):
    for z in access.zones:
        cov = z.new_coverage.get("l9_central", 0.0)
        assert 0.0 <= cov <= 1.0
        assert 0.0 <= z.rail_coverage <= 1.0
    for z in access.zones:
        if z.district == "ciutat_vella":
            assert z.new_coverage.get("l9_central", 0.0) == 0.0, z.name


def test_stations_have_lines_and_coordinates(access: TransitAccess):
    assert access.stations
    for st in access.stations:
        assert st.lines
        assert -180 <= st.lon <= 180
        assert -90 <= st.lat <= 90
        assert st.variant in access.variants


def test_new_l9_stations_present(access: TransitAccess):
    # Names are matched by prefix: "El Putxet" is disambiguated to "El Putxet (L9)" because an
    # existing (different) FGC station already has that exact name -- see build_l9_central.
    new_names = {st.name for st in access.stations if st.variant == "l9_central"}
    expected = {
        "Campus Nord", "Manuel Girona", "Prat de la Riba", "Mandri", "El Putxet",
        "Travessera de Dalt", "Sanllehy", "Guinardó-Hospital de Sant Pau",
    }
    for name in expected:
        assert any(n == name or n.startswith(name + " ") for n in new_names), name
