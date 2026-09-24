"""Tests for jevcity.world.loader.load_profiles, using tmp_path fixtures (no dependency on
the committed data files — see tests/test_data_file.py for those)."""

import json
from pathlib import Path

import pytest

from jevcity.types import AGE_BUCKETS, DistrictProfile, Source
from jevcity.world.loader import load_profiles

_ALL_SOURCE_FIELDS = (
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


def _good_row(id_: str = "gracia", name: str = "Gràcia") -> dict:
    ages = dict(zip(AGE_BUCKETS, (0.14, 0.24, 0.24, 0.19, 0.19), strict=True))
    return {
        "id": id_,
        "name": name,
        "population": 124_000,
        "age_distribution": ages,
        "income_per_capita_annual": 23_000.0,
        "avg_rent_monthly": 1_150.0,
        "vacancy_rate": 0.03,
        "unemployment_rate": 0.07,
        "jobs_per_resident": 0.55,
        "shops": 5_000,
        "transit_score": 0.80,
        "centroid": [2.156, 41.404],
        "sources": {f: Source.PLAUSIBLE.value for f in _ALL_SOURCE_FIELDS},
        "refs": {},
    }


def _write(path: Path, rows: list[dict]) -> Path:
    f = path / "districts.json"
    f.write_text(json.dumps(rows), encoding="utf-8")
    return f


# --- Good data -------------------------------------------------------------------------


def test_loads_valid_profiles(tmp_path: Path) -> None:
    rows = [
        _good_row("gracia", "Gràcia"),
        _good_row("eixample", "Eixample"),
    ]
    f = _write(tmp_path, rows)

    profiles = load_profiles(f)

    assert len(profiles) == 2
    assert all(isinstance(p, DistrictProfile) for p in profiles)
    assert {p.id for p in profiles} == {"gracia", "eixample"}


def test_accepts_path_as_str(tmp_path: Path) -> None:
    f = _write(tmp_path, [_good_row()])
    profiles = load_profiles(str(f))
    assert len(profiles) == 1


def test_tolerates_tiny_rounding_drift_in_age_distribution(tmp_path: Path) -> None:
    row = _good_row()
    # sums to 1.0001, within the documented ~1 tolerance
    row["age_distribution"] = {"0-17": 0.1401, "18-34": 0.24, "35-49": 0.24, "50-64": 0.19, "65+": 0.19}
    f = _write(tmp_path, [row])
    profiles = load_profiles(f)
    assert len(profiles) == 1


# --- Malformed data ----------------------------------------------------------------------


def test_missing_file_raises_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="could not read"):
        load_profiles(tmp_path / "does_not_exist.json")


def test_not_json_raises_value_error(tmp_path: Path) -> None:
    f = tmp_path / "districts.json"
    f.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_profiles(f)


def test_not_a_list_raises_value_error(tmp_path: Path) -> None:
    f = tmp_path / "districts.json"
    f.write_text(json.dumps({"id": "gracia"}), encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a JSON list"):
        load_profiles(f)


def test_empty_list_raises_value_error(tmp_path: Path) -> None:
    f = _write(tmp_path, [])
    with pytest.raises(ValueError, match="no district profiles"):
        load_profiles(f)


def test_schema_violation_raises_value_error(tmp_path: Path) -> None:
    row = _good_row()
    del row["population"]  # required field
    f = _write(tmp_path, [row])
    with pytest.raises(ValueError, match="failed validation"):
        load_profiles(f)


def test_wrong_type_raises_value_error(tmp_path: Path) -> None:
    row = _good_row()
    row["population"] = "not a number"
    f = _write(tmp_path, [row])
    with pytest.raises(ValueError, match="failed validation"):
        load_profiles(f)


def test_duplicate_ids_raise_value_error(tmp_path: Path) -> None:
    f = _write(tmp_path, [_good_row("gracia"), _good_row("gracia")])
    with pytest.raises(ValueError, match="duplicate district id"):
        load_profiles(f)


def test_age_distribution_missing_bucket_raises_value_error(tmp_path: Path) -> None:
    row = _good_row()
    del row["age_distribution"]["65+"]
    f = _write(tmp_path, [row])
    with pytest.raises(ValueError, match="missing bucket"):
        load_profiles(f)


def test_age_distribution_extra_bucket_raises_value_error(tmp_path: Path) -> None:
    row = _good_row()
    row["age_distribution"]["0-100"] = 0.0
    f = _write(tmp_path, [row])
    with pytest.raises(ValueError, match="unknown bucket"):
        load_profiles(f)


def test_age_distribution_not_summing_to_one_raises_value_error(tmp_path: Path) -> None:
    row = _good_row()
    row["age_distribution"] = {"0-17": 0.5, "18-34": 0.5, "35-49": 0.5, "50-64": 0.0, "65+": 0.0}
    f = _write(tmp_path, [row])
    with pytest.raises(ValueError, match="sums to"):
        load_profiles(f)


def test_missing_source_for_numeric_field_raises_value_error(tmp_path: Path) -> None:
    row = _good_row()
    del row["sources"]["avg_rent_monthly"]
    f = _write(tmp_path, [row])
    with pytest.raises(ValueError, match="missing sources entry"):
        load_profiles(f)


def test_entry_not_an_object_raises_value_error(tmp_path: Path) -> None:
    f = _write(tmp_path, ["not an object"])
    with pytest.raises(ValueError):
        load_profiles(f)


# --- Optional realism fields -------------------------------------------------------------


def test_optional_realism_fields_default_to_none(tmp_path: Path) -> None:
    f = _write(tmp_path, [_good_row()])
    profiles = load_profiles(f)
    p = profiles[0]
    assert p.income_per_household_annual is None
    assert p.owner_share is None
    assert p.avg_household_size is None


def test_optional_realism_fields_load_when_set_with_source(tmp_path: Path) -> None:
    row = _good_row()
    row["income_per_household_annual"] = 45_000.0
    row["owner_share"] = 0.55
    row["avg_household_size"] = 2.4
    row["sources"]["income_per_household_annual"] = Source.OPENDATA.value
    row["sources"]["owner_share"] = Source.OPENDATA.value
    row["sources"]["avg_household_size"] = Source.OPENDATA.value
    f = _write(tmp_path, [row])
    profiles = load_profiles(f)
    p = profiles[0]
    assert p.income_per_household_annual == 45_000.0
    assert p.owner_share == 0.55
    assert p.avg_household_size == 2.4


def test_optional_field_set_without_source_raises_value_error(tmp_path: Path) -> None:
    row = _good_row()
    row["owner_share"] = 0.55  # no matching sources["owner_share"] entry
    f = _write(tmp_path, [row])
    with pytest.raises(ValueError, match="owner_share is set but has no sources entry"):
        load_profiles(f)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("owner_share", 1.5),
        ("owner_share", -0.1),
        ("avg_household_size", 0.5),
        ("avg_household_size", 10.0),
        ("income_per_household_annual", 1_000.0),
        ("income_per_household_annual", 1_000_000.0),
    ],
)
def test_optional_field_out_of_range_raises_value_error(
    tmp_path: Path, field: str, bad_value: float
) -> None:
    row = _good_row()
    row[field] = bad_value
    row["sources"][field] = Source.OPENDATA.value
    f = _write(tmp_path, [row])
    with pytest.raises(ValueError, match="outside sane range"):
        load_profiles(f)


# --- New realism fields (10-district expansion): area_km2, tourist_flats, car_ownership,
# commute_mode_share, households_with_children_share ---------------------------------------


def test_new_optional_fields_default_to_none(tmp_path: Path) -> None:
    f = _write(tmp_path, [_good_row()])
    p = load_profiles(f)[0]
    assert p.area_km2 is None
    assert p.tourist_flats is None
    assert p.car_ownership is None
    assert p.commute_mode_share is None
    assert p.households_with_children_share is None


def test_new_optional_fields_load_when_set_with_source(tmp_path: Path) -> None:
    row = _good_row()
    row["area_km2"] = 4.5
    row["tourist_flats"] = 500
    row["car_ownership"] = 0.4
    row["commute_mode_share"] = {"metro": 0.3, "bus": 0.2, "car": 0.2, "bike": 0.05, "walk": 0.25}
    row["households_with_children_share"] = 0.2
    for field in (
        "area_km2",
        "tourist_flats",
        "car_ownership",
        "commute_mode_share",
        "households_with_children_share",
    ):
        row["sources"][field] = Source.OPENDATA.value
    f = _write(tmp_path, [row])
    p = load_profiles(f)[0]
    assert p.area_km2 == 4.5
    assert p.tourist_flats == 500
    assert p.car_ownership == 0.4
    assert p.commute_mode_share == {
        "metro": 0.3,
        "bus": 0.2,
        "car": 0.2,
        "bike": 0.05,
        "walk": 0.25,
    }
    assert p.households_with_children_share == 0.2


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("area_km2", 0.0),
        ("area_km2", 100.0),
        ("tourist_flats", -1.0),
        ("car_ownership", 1.5),
        ("car_ownership", -0.1),
        ("households_with_children_share", 1.5),
        ("households_with_children_share", -0.1),
    ],
)
def test_new_optional_field_out_of_range_raises_value_error(
    tmp_path: Path, field: str, bad_value: float
) -> None:
    row = _good_row()
    row[field] = bad_value
    row["sources"][field] = Source.OPENDATA.value
    f = _write(tmp_path, [row])
    with pytest.raises(ValueError, match="outside sane range"):
        load_profiles(f)


def test_commute_mode_share_set_without_source_raises_value_error(tmp_path: Path) -> None:
    row = _good_row()
    row["commute_mode_share"] = {"metro": 0.5, "bus": 0.5}
    f = _write(tmp_path, [row])
    with pytest.raises(ValueError, match="commute_mode_share is set but has no sources entry"):
        load_profiles(f)


def test_commute_mode_share_unknown_mode_raises_value_error(tmp_path: Path) -> None:
    row = _good_row()
    row["commute_mode_share"] = {"metro": 0.5, "spaceship": 0.5}
    row["sources"]["commute_mode_share"] = Source.DERIVED.value
    f = _write(tmp_path, [row])
    with pytest.raises(ValueError, match="unknown mode"):
        load_profiles(f)


def test_commute_mode_share_not_summing_to_one_raises_value_error(tmp_path: Path) -> None:
    row = _good_row()
    row["commute_mode_share"] = {"metro": 0.5, "bus": 0.1}
    row["sources"]["commute_mode_share"] = Source.DERIVED.value
    f = _write(tmp_path, [row])
    with pytest.raises(ValueError, match="sums to"):
        load_profiles(f)


def test_commute_mode_share_value_out_of_range_raises_value_error(tmp_path: Path) -> None:
    row = _good_row()
    row["commute_mode_share"] = {"metro": 1.5, "bus": -0.5}
    row["sources"]["commute_mode_share"] = Source.DERIVED.value
    f = _write(tmp_path, [row])
    with pytest.raises(ValueError, match="outside sane range"):
        load_profiles(f)


def test_commute_mode_share_tolerates_tiny_rounding_drift(tmp_path: Path) -> None:
    row = _good_row()
    row["commute_mode_share"] = {
        "metro": 0.3001,
        "bus": 0.2,
        "car": 0.2,
        "bike": 0.05,
        "walk": 0.25,
    }
    row["sources"]["commute_mode_share"] = Source.DERIVED.value
    f = _write(tmp_path, [row])
    profiles = load_profiles(f)
    assert len(profiles) == 1
