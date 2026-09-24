"""Load static district data. Owner: T1."""

from pathlib import Path

from jevcity.types import DistrictProfile


def load_profiles(path: str | Path) -> list[DistrictProfile]:
    """Read data/processed/districts.json (a JSON list of DistrictProfile) and validate it."""
    raise NotImplementedError
