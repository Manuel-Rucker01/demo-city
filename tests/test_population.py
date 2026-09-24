"""Tests for jevcity.population.generator.generate_population."""

from __future__ import annotations

import math
import time

import numpy as np

from jevcity.population.generator import (
    DEFAULT_AVG_HOUSEHOLD_SIZE,
    DEFAULT_OWNER_SHARE,
    _largest_remainder,
    generate_population,
)
from jevcity.types import Occupation, Tenure

N_SMALL = 1_000
N_LARGE = 10_000


def _counts_by_district(profiles, n_agents):
    weights = np.array([p.population for p in profiles], dtype=float)
    return _largest_remainder(weights, n_agents)


def test_exact_counts_per_district(profiles):
    rng = np.random.default_rng(1)
    agents = generate_population(profiles, N_SMALL, rng)
    expected = _counts_by_district(profiles, N_SMALL)
    got = {p.id: 0 for p in profiles}
    for a in agents:
        got[a.home] += 1
    for i, p in enumerate(profiles):
        assert got[p.id] == expected[i]
    assert sum(got.values()) == N_SMALL


def test_ids_are_0_to_n_minus_1(profiles, rng):
    agents = generate_population(profiles, N_SMALL, rng)
    assert sorted(a.id for a in agents) == list(range(N_SMALL))


def test_all_adults(profiles, rng):
    agents = generate_population(profiles, N_SMALL, rng)
    assert all(a.age >= 18 for a in agents)
    assert all(a.age <= 90 for a in agents)


def test_deterministic_same_seed(profiles):
    rng1 = np.random.default_rng(777)
    rng2 = np.random.default_rng(777)
    a1 = generate_population(profiles, N_SMALL, rng1)
    a2 = generate_population(profiles, N_SMALL, rng2)
    for x, y in zip(a1, a2, strict=True):
        assert x.age == y.age
        assert x.home == y.home
        assert x.occupation == y.occupation
        assert x.wage_monthly == y.wage_monthly
        assert x.employed == y.employed
        assert x.job_district == y.job_district
        assert x.rent_monthly == y.rent_monthly
        assert x.household_size == y.household_size
        assert x.lease_start_tick == y.lease_start_tick
        assert x.savings == y.savings
        assert x.spending_level == y.spending_level
        assert x.satisfaction == y.satisfaction
        assert x.days_unemployed == y.days_unemployed


def test_different_seeds_differ(profiles):
    rng1 = np.random.default_rng(1)
    rng2 = np.random.default_rng(2)
    a1 = generate_population(profiles, N_SMALL, rng1)
    a2 = generate_population(profiles, N_SMALL, rng2)
    ages1 = [a.age for a in a1]
    ages2 = [a.age for a in a2]
    wages1 = [a.wage_monthly for a in a1]
    wages2 = [a.wage_monthly for a in a2]
    assert ages1 != ages2 or wages1 != wages2


def test_retired_are_65_or_older(profiles, rng):
    agents = generate_population(profiles, N_SMALL, rng)
    for a in agents:
        if a.occupation is Occupation.RETIRED:
            assert a.age >= 65


def test_job_district_none_iff_not_employed(profiles, rng):
    agents = generate_population(profiles, N_LARGE, rng)
    for a in agents:
        if a.employed:
            assert a.job_district is not None
        else:
            assert a.job_district is None


def test_employment_rate_per_district_within_tolerance(profiles):
    rng = np.random.default_rng(42)
    agents = generate_population(profiles, N_LARGE, rng)
    by_district = {p.id: p for p in profiles}
    working_age = [a for a in agents if a.occupation is not Occupation.RETIRED]
    for did, prof in by_district.items():
        cohort = [a for a in working_age if a.home == did]
        if not cohort:
            continue
        emp_rate = sum(a.employed for a in cohort) / len(cohort)
        expected = 1.0 - prof.unemployment_rate
        # students have their own 0.3 employment rate, so allow a generous tolerance
        assert abs(emp_rate - expected) < 0.25


def test_median_rent_per_district_within_15pct(profiles):
    # Renters only: avg_rent_monthly is the district's typical *rental* flat price, and
    # owners' rent_monthly holds an unrelated housing cost (mortgage/fees).
    rng = np.random.default_rng(42)
    agents = generate_population(profiles, N_LARGE, rng)
    for p in profiles:
        rents = sorted(
            a.rent_monthly for a in agents if a.home == p.id and a.tenure is Tenure.RENTER
        )
        assert rents
        median = rents[len(rents) // 2]
        assert abs(median - p.avg_rent_monthly) / p.avg_rent_monthly < 0.15


# --- stratified allocation: exact quotas -------------------------------------------------


def test_exact_unemployment_quota_per_district_small_n(profiles):
    rng = np.random.default_rng(3)
    n = 250
    agents = generate_population(profiles, n, rng)
    by_district: dict[str, list] = {p.id: [] for p in profiles}
    for a in agents:
        by_district[a.home].append(a)
    for p in profiles:
        cohort = by_district[p.id]
        eligible = [a for a in cohort if a.occupation not in (Occupation.STUDENT, Occupation.RETIRED)]
        if not eligible:
            continue
        unemployed = sum(1 for a in eligible if not a.employed)
        expected = round(p.unemployment_rate * len(eligible))
        assert unemployed == expected


def test_exact_owner_quota_per_district(profiles):
    rng = np.random.default_rng(9)
    n = 250
    agents = generate_population(profiles, n, rng)
    by_district: dict[str, list] = {p.id: [] for p in profiles}
    for a in agents:
        by_district[a.home].append(a)
    for p in profiles:
        cohort = by_district[p.id]
        owners = sum(1 for a in cohort if a.tenure is Tenure.OWNER)
        owner_share = p.owner_share if p.owner_share is not None else DEFAULT_OWNER_SHARE
        expected = round(owner_share * len(cohort))
        assert owners == expected


def test_owners_skew_older(profiles):
    rng = np.random.default_rng(11)
    agents = generate_population(profiles, N_LARGE, rng)
    owner_ages = [a.age for a in agents if a.tenure is Tenure.OWNER]
    renter_ages = [a.age for a in agents if a.tenure is Tenure.RENTER]
    assert owner_ages and renter_ages
    assert sum(owner_ages) / len(owner_ages) > sum(renter_ages) / len(renter_ages)


def test_owner_housing_cost_far_below_market_rent(profiles):
    rng = np.random.default_rng(13)
    agents = generate_population(profiles, N_LARGE, rng)
    by_district = {p.id: p for p in profiles}
    for a in agents:
        if a.tenure is Tenure.OWNER:
            assert a.rent_monthly < by_district[a.home].avg_rent_monthly


def test_household_size_mean_matches_avg_household_size(profiles):
    rng = np.random.default_rng(21)
    agents = generate_population(profiles, N_LARGE, rng)
    by_district: dict[str, list] = {p.id: [] for p in profiles}
    for a in agents:
        by_district[a.home].append(a)
    for p in profiles:
        cohort = by_district[p.id]
        target = p.avg_household_size or DEFAULT_AVG_HOUSEHOLD_SIZE
        mean_size = sum(a.household_size for a in cohort) / len(cohort)
        assert abs(mean_size - target) < 0.3


def test_median_renter_burden_within_target_range(profiles):
    """Median renter rent burden per district should land in a plausible 25-45% band."""
    rng = np.random.default_rng(31)
    agents = generate_population(profiles, N_LARGE, rng)
    by_district: dict[str, list] = {p.id: [] for p in profiles}
    for a in agents:
        by_district[a.home].append(a)
    for p in profiles:
        renters = [a for a in by_district[p.id] if a.tenure is Tenure.RENTER]
        burdens = sorted(a.rent_burden for a in renters)
        assert burdens
        median = burdens[len(burdens) // 2]
        assert 0.25 <= median <= 0.45, f"{p.id}: median renter burden {median:.3f}"


def test_performance_10000_agents_under_1s(profiles):
    rng = np.random.default_rng(1)
    start = time.perf_counter()
    agents = generate_population(profiles, N_LARGE, rng)
    elapsed = time.perf_counter() - start
    assert len(agents) == N_LARGE
    assert elapsed < 1.0


def test_all_floats_finite_and_in_range(profiles, rng):
    agents = generate_population(profiles, N_LARGE, rng)
    for a in agents:
        assert math.isfinite(a.wage_monthly) and 450.0 <= a.wage_monthly <= 12000.0
        assert math.isfinite(a.rent_monthly) and a.rent_monthly > 0
        assert math.isfinite(a.savings) and a.savings >= 0
        assert math.isfinite(a.spending_level) and 0.0 <= a.spending_level <= 1.0
        assert math.isfinite(a.satisfaction) and 0.0 <= a.satisfaction <= 1.0
        assert 1 <= a.household_size <= 4
        assert -359 <= a.lease_start_tick <= 0
        assert 0 <= a.days_unemployed <= 600
