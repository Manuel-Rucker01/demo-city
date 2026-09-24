"""Shared fixtures. Plausible values only - tests must not depend on real data files."""

import numpy as np
import pytest

from jevcity.types import AGE_BUCKETS, DistrictProfile, Source

_ROWS = [
    # id, name, pop, income/yr, rent, vacancy, unemp, jobs/res, shops, transit, lon, lat
    ("ciutat_vella", "Ciutat Vella", 109_000, 15_500, 1_050, 0.06, 0.11, 1.10, 6_500, 0.95, 2.176, 41.381),
    ("eixample", "Eixample", 270_000, 25_500, 1_250, 0.04, 0.07, 1.40, 14_000, 0.95, 2.162, 41.391),
    ("gracia", "Gràcia", 124_000, 23_000, 1_150, 0.03, 0.07, 0.55, 5_000, 0.80, 2.156, 41.404),
    ("sant_marti", "Sant Martí", 243_000, 18_500, 1_100, 0.04, 0.09, 0.75, 7_500, 0.80, 2.199, 41.407),
    ("nou_barris", "Nou Barris", 175_000, 12_500, 800, 0.05, 0.12, 0.30, 4_000, 0.60, 2.177, 41.441),
]


def make_profiles() -> list[DistrictProfile]:
    ages = dict(zip(AGE_BUCKETS, (0.14, 0.24, 0.24, 0.19, 0.19)))
    out = []
    for i, n, pop, inc, rent, vac, un, jpr, shops, tr, lon, lat in _ROWS:
        out.append(
            DistrictProfile(
                id=i, name=n, population=pop, age_distribution=ages,
                income_per_capita_annual=inc, avg_rent_monthly=rent, vacancy_rate=vac,
                unemployment_rate=un, jobs_per_resident=jpr, shops=shops, transit_score=tr,
                centroid=(lon, lat),
                sources={f: Source.PLAUSIBLE for f in (
                    "population", "age_distribution", "income_per_capita_annual",
                    "avg_rent_monthly", "vacancy_rate", "unemployment_rate",
                    "jobs_per_resident", "shops", "transit_score")},
            )
        )
    return out


@pytest.fixture
def profiles() -> list[DistrictProfile]:
    return make_profiles()


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(1234)
