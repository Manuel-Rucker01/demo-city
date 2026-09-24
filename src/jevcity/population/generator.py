"""Synthetic population from district profiles. Owner: T2.

Modelling assumptions (documented here for the README limitations section):

- Each agent represents a household *head* (working-age or retired adult); no agents under 18
  are generated, matching `Agent` representing "one simulated household head".
- Home district counts are allocated proportional to `DistrictProfile.population` using a
  largest-remainder method, so counts are exact and stable for a given `n_agents`.
- Age is drawn from the district `age_distribution`, renormalized to drop the "0-17" bucket,
  then uniform within the chosen bucket (65+ mapped to 65..90).
- Occupation: 65+ is mostly RETIRED, with a small chance (age 65-67 only) of still working;
  18-24 has a ~40% chance of being a STUDENT; everyone else gets a LOW/MID/HIGH skill split
  whose probabilities shift with the district's income relative to the city-wide (population
  weighted) mean income per capita.
- Wages are lognormal around a household-head monthly wage derived from
  `income_per_capita_annual * 1.6 / 12` (1.6 = assumed household earners-per-capita factor),
  scaled by a skill multiplier (LOW 0.7, MID 1.0, HIGH 1.7), a small flat baseline for students
  (~600 EUR/month part-time), and 0.75x the MID base for retirees' pensions. All wages are
  clipped to [450, 12000] EUR/month.
- Employment: students are employed (part-time) with probability 0.3; retirees are never
  "employed" (their pension flows through `Agent.income_monthly` regardless); everyone else is
  employed with probability `1 - district.unemployment_rate`. Unemployed non-retirees get
  `days_unemployed` sampled uniformly in [0, 600].
- `job_district` is only set for employed agents: 50% work in their home district, the rest are
  assigned a district weighted by `jobs_per_resident * population` (which can still land on the
  home district).
- Rent: `avg_rent_monthly * lognormal noise (sigma=0.2) * mild wage-percentile scaling
  (0.9..1.1)`. **Limitation**: every agent is modelled as a renter (`rent_monthly` is always the
  market lease payment) -- homeownership is out of scope for this MVP, even though roughly 15%
  of the real population owns their home.
- `household_size` (1-4) is skewed by age bucket: young adults (18-34) skew toward 1-2, prime
  age (35-49) skews toward 3-4, older groups skew back toward smaller households.
- `lease_start_tick` is uniform in [-359, 0] (leases already "in progress" at sim start).
- `savings` is modelled as 0.5-12 months of `wage_monthly` (lognormal, median ~2 months).
- `spending_level` and `satisfaction` are Beta-distributed: spending shifts up with wage
  percentile; satisfaction shifts down as rent burden (rent / income) increases.

Everything here is vectorized with numpy and only then materialized into `Agent` dataclass
instances, so it stays well under 1s for 10_000 agents.
"""

from __future__ import annotations

import numpy as np

from jevcity.types import Agent, DistrictProfile, Occupation

_ADULT_BUCKETS = ("18-34", "35-49", "50-64", "65+")
_BUCKET_AGE_RANGE = {
    "18-34": (18, 34),
    "35-49": (35, 49),
    "50-64": (50, 64),
    "65+": (65, 90),
}
_SKILL_MULT = np.array([0.7, 1.0, 1.7])  # LOW, MID, HIGH
_HH_SIZE_PROBS = np.array(
    [
        [0.45, 0.35, 0.15, 0.05],  # 18-34
        [0.10, 0.25, 0.35, 0.30],  # 35-49
        [0.20, 0.35, 0.30, 0.15],  # 50-64
        [0.35, 0.45, 0.15, 0.05],  # 65+
    ]
)


def _largest_remainder(weights: np.ndarray, total: int) -> np.ndarray:
    """Allocate `total` integer units proportional to `weights`, exact sum, stable ties."""
    ideal = weights / weights.sum() * total
    floor = np.floor(ideal).astype(int)
    remainder = total - int(floor.sum())
    if remainder > 0:
        frac = ideal - floor
        order = np.argsort(-frac, kind="stable")
        floor[order[:remainder]] += 1
    return floor


def _inverse_cdf_sample(cum_probs: np.ndarray, u: np.ndarray) -> np.ndarray:
    """cum_probs: (n, k) cumulative probabilities per row; u: (n,) uniforms -> index in [0, k)."""
    return (cum_probs < u[:, None]).sum(axis=1)


def generate_population(
    profiles: list[DistrictProfile], n_agents: int, rng: np.random.Generator
) -> list[Agent]:
    """Create n_agents adults (18+) distributed by district population, with age from the
    district age distribution (renormalized without 0-17), wage from income per capita
    (lognormal), occupation, employment from unemployment_rate, job district weighted by
    jobs_per_resident, rent near avg_rent_monthly, staggered lease_start_tick in [-359, 0].
    Must be deterministic for a given rng seed and fast for 10_000 agents (<1s)."""
    n = n_agents
    d = len(profiles)
    ids = [p.id for p in profiles]

    pop = np.array([p.population for p in profiles], dtype=float)
    income = np.array([p.income_per_capita_annual for p in profiles], dtype=float)
    avg_rent = np.array([p.avg_rent_monthly for p in profiles], dtype=float)
    unemployment = np.array([p.unemployment_rate for p in profiles], dtype=float)
    jobs_per_resident = np.array([p.jobs_per_resident for p in profiles], dtype=float)

    # --- home district -------------------------------------------------------------------
    counts = _largest_remainder(pop, n)
    home_idx = np.repeat(np.arange(d), counts)
    assert home_idx.shape[0] == n

    home_income = income[home_idx]
    home_avg_rent = avg_rent[home_idx]
    home_unemployment = unemployment[home_idx]

    # --- age -------------------------------------------------------------------------------
    age_probs = np.empty((d, 4))
    for j, prof in enumerate(profiles):
        raw = np.array([prof.age_distribution.get(b, 0.0) for b in _ADULT_BUCKETS])
        s = raw.sum()
        age_probs[j] = raw / s if s > 0 else np.full(4, 0.25)
    cum_age = np.cumsum(age_probs, axis=1)
    u_age = rng.random(n)
    bucket_idx = _inverse_cdf_sample(cum_age[home_idx], u_age)

    low_arr = np.array([_BUCKET_AGE_RANGE[b][0] for b in _ADULT_BUCKETS])[bucket_idx]
    high_arr = np.array([_BUCKET_AGE_RANGE[b][1] for b in _ADULT_BUCKETS])[bucket_idx]
    ages = rng.integers(low_arr, high_arr + 1, size=n)

    # --- occupation (skill baseline, then retired/student overrides) -----------------------
    city_mean_income = float(np.sum(income * pop) / pop.sum())
    r = income / city_mean_income
    high_p = np.clip(0.15 * r, 0.03, 0.5)
    low_p = np.clip(0.5 / r, 0.2, 0.7)
    mid_p = np.clip(1.0 - high_p - low_p, 0.05, None)
    skill_probs = np.stack([low_p, mid_p, high_p], axis=1)
    skill_probs = skill_probs / skill_probs.sum(axis=1, keepdims=True)
    cum_skill = np.cumsum(skill_probs, axis=1)
    u_skill = rng.random(n)
    skill_idx = _inverse_cdf_sample(cum_skill[home_idx], u_skill)  # 0=LOW,1=MID,2=HIGH

    occ = np.empty(n, dtype=object)
    skill_enum = np.array([Occupation.LOW_SKILL, Occupation.MID_SKILL, Occupation.HIGH_SKILL])
    occ[:] = skill_enum[skill_idx]

    is_65plus = ages >= 65
    still_working = is_65plus & (ages <= 67) & (rng.random(n) < 0.2)
    retired_mask = is_65plus & ~still_working
    occ[retired_mask] = Occupation.RETIRED

    young_mask = (ages >= 18) & (ages <= 24)
    student_mask = young_mask & (rng.random(n) < 0.4)
    occ[student_mask] = Occupation.STUDENT

    # --- wage ------------------------------------------------------------------------------
    monthly_base = home_income * 1.6 / 12.0
    mult = _SKILL_MULT[skill_idx]
    wage_base = monthly_base * mult
    wage_base[retired_mask] = 0.75 * monthly_base[retired_mask] * 1.0
    wage_base[student_mask] = 600.0

    sigma = np.full(n, 0.3)
    sigma[retired_mask] = 0.2
    sigma[student_mask] = 0.25

    wage = rng.lognormal(mean=np.log(np.clip(wage_base, 1.0, None)), sigma=sigma)
    wage = np.clip(wage, 450.0, 12000.0)

    # --- employment --------------------------------------------------------------------
    employed = np.zeros(n, dtype=bool)
    working_mask = ~student_mask & ~retired_mask
    employed[working_mask] = rng.random(int(working_mask.sum())) < (
        1.0 - home_unemployment[working_mask]
    )
    employed[student_mask] = rng.random(int(student_mask.sum())) < 0.3
    # retired_mask stays False (pension flows through Agent.income_monthly regardless)

    days_unemployed = np.zeros(n, dtype=int)
    need_days = (~employed) & (~retired_mask)
    days_unemployed[need_days] = rng.integers(0, 601, size=int(need_days.sum()))

    # --- job_district ------------------------------------------------------------------
    job_weights = jobs_per_resident * pop
    job_probs = job_weights / job_weights.sum()
    cum_job = np.cumsum(job_probs)
    own_district = rng.random(n) < 0.5
    u_job = rng.random(n)
    other_idx = _inverse_cdf_sample(np.broadcast_to(cum_job, (n, d)), u_job)
    job_idx = np.where(own_district, home_idx, other_idx)
    ids_arr = np.array(ids, dtype=object)
    job_district_list: list[str | None] = [
        ids_arr[job_idx[i]] if employed[i] else None for i in range(n)
    ]

    # --- rent ------------------------------------------------------------------------------
    wage_rank = np.argsort(np.argsort(wage))
    percentile = wage_rank / (n - 1) if n > 1 else np.full(n, 0.5)
    scale = 0.9 + 0.2 * percentile
    rent_noise = rng.lognormal(mean=0.0, sigma=0.2, size=n)
    rent = home_avg_rent * scale * rent_noise
    rent = np.clip(rent, 200.0, 8000.0)

    # --- household size ----------------------------------------------------------------
    cum_hh = np.cumsum(_HH_SIZE_PROBS, axis=1)
    u_hh = rng.random(n)
    size_idx = _inverse_cdf_sample(cum_hh[bucket_idx], u_hh)
    household_size = size_idx + 1

    # --- lease / savings / spending / satisfaction --------------------------------------
    lease_start_tick = rng.integers(-359, 1, size=n)

    months = np.clip(rng.lognormal(mean=np.log(2.0), sigma=0.6, size=n), 0.5, 12.0)
    savings = months * wage

    mean_spend = np.clip(0.3 + 0.4 * percentile, 0.05, 0.95)
    conc = 8.0
    spending_level = rng.beta(mean_spend * conc, (1.0 - mean_spend) * conc)

    income_monthly = np.where(
        employed | retired_mask,
        wage,
        np.where(days_unemployed < 360, 0.6 * wage, 450.0),
    )
    rent_burden = rent / np.maximum(income_monthly, 1e-6)
    mean_sat = np.clip(0.75 - 0.8 * rent_burden, 0.05, 0.95)
    satisfaction = rng.beta(mean_sat * conc, (1.0 - mean_sat) * conc)

    homes = ids_arr[home_idx]

    agents: list[Agent] = []
    for i in range(n):
        agents.append(
            Agent(
                id=i,
                age=int(ages[i]),
                household_size=int(household_size[i]),
                occupation=occ[i],
                wage_monthly=float(wage[i]),
                employed=bool(employed[i]),
                job_district=job_district_list[i],
                home=homes[i],
                rent_monthly=float(rent[i]),
                lease_start_tick=int(lease_start_tick[i]),
                savings=float(savings[i]),
                spending_level=float(spending_level[i]),
                satisfaction=float(satisfaction[i]),
                days_unemployed=int(days_unemployed[i]),
            )
        )
    return agents
