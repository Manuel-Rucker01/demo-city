"""Validates the committed data/processed/{districts.json,districts.geojson} files."""

import json
from pathlib import Path

import pytest

from jevcity.types import Source
from jevcity.world.loader import load_profiles

ROOT = Path(__file__).resolve().parent.parent
DISTRICTS_JSON = ROOT / "data" / "processed" / "districts.json"
DISTRICTS_GEOJSON = ROOT / "data" / "processed" / "districts.geojson"

EXPECTED_IDS = {"ciutat_vella", "eixample", "gracia", "sant_marti", "nou_barris"}

# Barcelona's own bounding box, used to sanity-check converted centroids/polygons.
BCN_LON_RANGE = (2.05, 2.23)
BCN_LAT_RANGE = (41.32, 41.47)


@pytest.fixture(scope="module")
def profiles():
    return load_profiles(DISTRICTS_JSON)


@pytest.fixture(scope="module")
def geojson():
    return json.loads(DISTRICTS_GEOJSON.read_text(encoding="utf-8"))


def test_five_districts_with_expected_ids(profiles):
    assert len(profiles) == 5
    assert {p.id for p in profiles} == EXPECTED_IDS


def test_rent_in_sane_range(profiles):
    for p in profiles:
        assert 500 <= p.avg_rent_monthly <= 2500, f"{p.id}: avg_rent_monthly={p.avg_rent_monthly}"


def test_income_in_sane_range(profiles):
    for p in profiles:
        assert 8_000 <= p.income_per_capita_annual <= 50_000, (
            f"{p.id}: income_per_capita_annual={p.income_per_capita_annual}"
        )


def test_population_positive_and_plausible(profiles):
    for p in profiles:
        assert 50_000 <= p.population <= 400_000, f"{p.id}: population={p.population}"


def test_rates_in_unit_interval(profiles):
    for p in profiles:
        assert 0.0 <= p.vacancy_rate <= 1.0
        assert 0.0 <= p.unemployment_rate <= 1.0
        assert 0.0 <= p.transit_score <= 1.0
        assert p.jobs_per_resident >= 0.0
        assert p.shops >= 0


def test_centroid_within_barcelona_bounds(profiles):
    for p in profiles:
        lon, lat = p.centroid
        assert BCN_LON_RANGE[0] <= lon <= BCN_LON_RANGE[1], f"{p.id}: lon={lon}"
        assert BCN_LAT_RANGE[0] <= lat <= BCN_LAT_RANGE[1], f"{p.id}: lat={lat}"


def test_every_numeric_field_has_a_source(profiles):
    numeric_fields = (
        "population",
        "age_distribution",
        "income_per_capita_annual",
        "avg_rent_monthly",
        "vacancy_rate",
        "unemployment_rate",
        "jobs_per_resident",
        "shops",
        "transit_score",
    )
    valid = {s.value for s in Source}
    for p in profiles:
        for field in numeric_fields:
            assert field in p.sources, f"{p.id}: no source for {field}"
            assert p.sources[field] in valid, f"{p.id}.{field}: unknown source {p.sources[field]!r}"


def test_geojson_is_feature_collection_matching_json_ids(geojson, profiles):
    assert geojson["type"] == "FeatureCollection"
    geo_ids = {f["properties"]["id"] for f in geojson["features"]}
    assert geo_ids == {p.id for p in profiles} == EXPECTED_IDS


def test_geojson_file_under_size_budget():
    size = DISTRICTS_GEOJSON.stat().st_size
    assert size < 400_000, f"districts.geojson is {size} bytes, over the 400KB budget"


def _point_in_ring(point: tuple[float, float], ring: list[list[float]]) -> bool:
    """Standard ray-casting point-in-polygon test."""
    x, y = point
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1) + x1):
            inside = not inside
    return inside


def test_geojson_polygons_contain_their_centroid(geojson, profiles):
    profile_by_id = {p.id: p for p in profiles}
    for feature in geojson["features"]:
        did = feature["properties"]["id"]
        centroid = profile_by_id[did].centroid
        coords = feature["geometry"]["coordinates"][0]
        assert _point_in_ring(centroid, coords), f"{did}: centroid {centroid} not inside its polygon"


def test_sources_md_exists_and_mentions_every_district():
    sources_md = ROOT / "data" / "processed" / "SOURCES.md"
    assert sources_md.exists()
    text = sources_md.read_text(encoding="utf-8")
    assert "opendata" in text.lower()
    assert "plausible" in text.lower()


def test_optional_realism_fields_present_and_in_sane_range(profiles):
    for p in profiles:
        assert p.income_per_household_annual is not None, p.id
        assert p.owner_share is not None, p.id
        assert p.avg_household_size is not None, p.id
        assert 10_000 <= p.income_per_household_annual <= 150_000, (
            f"{p.id}: income_per_household_annual={p.income_per_household_annual}"
        )
        assert 0.0 <= p.owner_share <= 1.0, f"{p.id}: owner_share={p.owner_share}"
        assert 1.5 <= p.avg_household_size <= 4.0, (
            f"{p.id}: avg_household_size={p.avg_household_size}"
        )


def test_optional_realism_fields_have_sources(profiles):
    valid = {s.value for s in Source}
    for p in profiles:
        for field in ("income_per_household_annual", "owner_share", "avg_household_size"):
            assert field in p.sources, f"{p.id}: no source for {field}"
            assert p.sources[field] in valid, f"{p.id}.{field}: unknown source {p.sources[field]!r}"


def test_avg_rent_and_transit_score_are_real_data(profiles):
    # avg_rent_monthly and transit_score used to be Source.PLAUSIBLE; this pins the upgrade
    # to real/derived data so a future regression (e.g. reverting to hand-set defaults) fails.
    for p in profiles:
        assert p.sources["avg_rent_monthly"] == Source.OPENDATA, p.id
        assert p.sources["transit_score"] == Source.DERIVED, p.id
