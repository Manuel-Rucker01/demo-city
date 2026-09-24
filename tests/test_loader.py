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
