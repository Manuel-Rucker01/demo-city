"""Load static district data. Owner: T1."""

import json
from pathlib import Path

from pydantic import ValidationError

from jevcity.types import AGE_BUCKETS, CommuteMode, DistrictProfile

_SUM_TOLERANCE = 1e-3

# Every numeric field on DistrictProfile must carry a provenance entry in `sources`.
_NUMERIC_FIELDS = (
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

# Optional realism fields: None (unknown) is allowed, but if set they must carry a `sources`
# entry too, and pass a sanity range check. (field name -> (min, max), exclusive of None)
_OPTIONAL_NUMERIC_FIELDS: dict[str, tuple[float, float]] = {
    "income_per_household_annual": (5_000.0, 250_000.0),
    "owner_share": (0.0, 1.0),
    "avg_household_size": (1.0, 6.0),
    "area_km2": (0.01, 50.0),
    "tourist_flats": (0.0, 20_000.0),
    "car_ownership": (0.0, 1.0),
    "households_with_children_share": (0.0, 1.0),
}

_VALID_COMMUTE_MODES = {m.value for m in CommuteMode}


def load_profiles(path: str | Path) -> list[DistrictProfile]:
    """Read data/processed/districts.json (a JSON list of DistrictProfile) and validate it."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read district data file {path}: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(payload, list):
        # ValueError (not TypeError) is this module's documented contract for any malformed input.
        raise ValueError(  # noqa: TRY004
            f"{path} must contain a JSON list of DistrictProfile, got {type(payload).__name__}"
        )
    if not payload:
        raise ValueError(f"{path} contains no district profiles")

    profiles: list[DistrictProfile] = []
    for i, item in enumerate(payload):
        try:
            profiles.append(DistrictProfile(**item))
        except ValidationError as exc:
            district_id = item.get("id", f"<index {i}>") if isinstance(item, dict) else f"<index {i}>"
            raise ValueError(f"district {district_id!r} in {path} failed validation: {exc}") from exc
        except TypeError as exc:
            raise ValueError(f"district entry at index {i} in {path} is not a JSON object: {exc}") from exc

    seen_ids: set[str] = set()
    for p in profiles:
        if p.id in seen_ids:
            raise ValueError(f"duplicate district id {p.id!r} in {path}")
        seen_ids.add(p.id)

        missing_buckets = set(AGE_BUCKETS) - set(p.age_distribution)
        if missing_buckets:
            raise ValueError(
                f"district {p.id!r} in {path}: age_distribution is missing bucket(s) "
                f"{sorted(missing_buckets)} (expected {AGE_BUCKETS})"
            )
        extra_buckets = set(p.age_distribution) - set(AGE_BUCKETS)
        if extra_buckets:
            raise ValueError(
                f"district {p.id!r} in {path}: age_distribution has unknown bucket(s) "
                f"{sorted(extra_buckets)} (expected {AGE_BUCKETS})"
            )
        total_share = sum(p.age_distribution.values())
        if abs(total_share - 1.0) > _SUM_TOLERANCE:
            raise ValueError(
                f"district {p.id!r} in {path}: age_distribution sums to {total_share!r}, "
                f"expected ~1.0 (tolerance {_SUM_TOLERANCE})"
            )

        missing_sources = [f for f in _NUMERIC_FIELDS if f not in p.sources]
        if missing_sources:
            raise ValueError(
                f"district {p.id!r} in {path}: missing sources entry for field(s) "
                f"{missing_sources}; every numeric field must carry a Source"
            )

        for field, (lo, hi) in _OPTIONAL_NUMERIC_FIELDS.items():
            value = getattr(p, field)
            if value is None:
                continue
            if field not in p.sources:
                raise ValueError(
                    f"district {p.id!r} in {path}: {field} is set but has no sources entry; "
                    "optional realism fields must carry a Source when not None"
                )
            if not (lo <= value <= hi):
                raise ValueError(
                    f"district {p.id!r} in {path}: {field}={value!r} outside sane range "
                    f"[{lo}, {hi}]"
                )

        if p.commute_mode_share is not None:
            if "commute_mode_share" not in p.sources:
                raise ValueError(
                    f"district {p.id!r} in {path}: commute_mode_share is set but has no "
                    "sources entry; optional realism fields must carry a Source when not None"
                )
            unknown_modes = set(p.commute_mode_share) - _VALID_COMMUTE_MODES
            if unknown_modes:
                raise ValueError(
                    f"district {p.id!r} in {path}: commute_mode_share has unknown mode(s) "
                    f"{sorted(unknown_modes)} (expected a subset of {sorted(_VALID_COMMUTE_MODES)})"
                )
            for mode, share in p.commute_mode_share.items():
                if not (0.0 <= share <= 1.0):
                    raise ValueError(
                        f"district {p.id!r} in {path}: commute_mode_share[{mode!r}]={share!r} "
                        "outside sane range [0.0, 1.0]"
                    )
            mode_total = sum(p.commute_mode_share.values())
            if abs(mode_total - 1.0) > _SUM_TOLERANCE:
                raise ValueError(
                    f"district {p.id!r} in {path}: commute_mode_share sums to {mode_total!r}, "
                    f"expected ~1.0 (tolerance {_SUM_TOLERANCE})"
                )

    return profiles
