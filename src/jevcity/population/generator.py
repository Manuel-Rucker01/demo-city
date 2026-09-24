"""Synthetic population from district profiles. Owner: T2.

Modelling assumptions (documented here for the README limitations section):

- Each agent represents a household *head* (working-age or retired adult); no agents under 18
  are generated, matching `Agent` representing "one simulated household head".
- Home district counts are allocated proportional to `DistrictProfile.population` using a
  largest-remainder method, so counts are exact and stable for a given `n_agents`.
- Age is drawn from the district `age_distribution`, renormalized to drop the "0-17" bucket,
  then uniform within the chosen bucket (65+ mapped to 65..90). Age itself is *not*
  quota-exact (it stays a per-agent draw); only employment, tenure and occupation mix are.
- **Stratified (exact) allocation.** Everything downstream of age uses largest-remainder /
  exact-count quotas *within each district*, then randomly (rng, deterministic) decides
  *which* agents get each status, so small runs reproduce input rates within +-1 agent per
  district instead of drifting from independent per-agent coin flips:
    1. Still-working 65-67 year olds: `round(STILL_WORKING_65_67_SHARE * n_65_67)`, chosen
       uniformly at random from the 65-67 cohort; the rest of the 65+ cohort is RETIRED.
    2. Students: `round(STUDENT_SHARE_18_24 * n_18_24)` of the (non-retired) 18-24 cohort.
    3. Skill mix (LOW/MID/HIGH) of the remaining working-age pool, via `_largest_remainder`
       on probabilities that shift with the district's income relative to the city-wide
       (population-weighted) mean income per capita (same shape as before this rewrite).
    4. Employment: `unemployed_count = round(unemployment_rate * n_eligible)` where
       `n_eligible` = working-age, non-student, non-retired agents in the district (the same
       population `engine/snapshot.py` uses for `unemployment_rate`) -- chosen uniformly at
       random from that pool. Students get their own exact quota
       (`round(STUDENT_EMPLOYMENT_SHARE * n_students)`); retirees are never "employed" (their
       pension flows through `Agent.income_monthly` regardless).
    5. Tenure: `n_owners = round(owner_share * n_agents_in_district)` (owner_share falls back
       to `DEFAULT_OWNER_SHARE` when the profile doesn't set it -- see below), chosen with
       **age- and income-weighted** sampling-without-replacement (Efraimidis-Spirakis: each agent gets key
       `u ** (1 / propensity)` for `u ~ Uniform(0,1)`, propensity rising with age and income; the top
       `n_owners` keys become owners) so older agents are more likely to own while the
       district-wide count stays exact.
- **Household income** (wage/pension base) uses `income_per_household_annual` when the
  profile sets it: EUR/year, divided by 12 for the monthly household income. If its `refs`
  entry doesn't mention "disposable"/"disponible" it is treated as *gross* and multiplied by
  `NET_INCOME_FACTOR` (0.8, a simple documented approximation of the gross->net wedge) --
  `income_per_capita_annual` is already disposable (see scripts/fetch_opendata.py's
  "Disposable household income per capita" source note) and needs no such conversion.
  When `income_per_household_annual` is absent, the fallback is
  `income_per_capita_annual * avg_household_size` (avg_household_size falls back to
  `DEFAULT_AVG_HOUSEHOLD_SIZE` = 2.45), matching the documented fallback in types.py.
  `Agent.wage_monthly` represents the *whole household's* income (the household head is the
  sim's unit of account -- see `Agent.income_monthly`), not one person's individual earnings.
- Skill multipliers (LOW 0.7, MID 1.0, HIGH 1.7) are applied around a per-district
  `monthly_base = household_income_monthly / weighted_mean(skill multipliers, skill mix)` so
  that, once agents are distributed across the exact skill quotas above, the
  population-weighted mean household income in the district recovers `income_per_household*
  (or the fallback)`; lognormal dispersion (sigma 0.3, 0.2 for retirees, 0.25 for students)
  is layered on top of that per-agent median. All wages are clipped to [450, 12000] EUR/month.
  Retirees' pension = 0.75x the MID base; students get a flat ~600 EUR/month part-time
  baseline (employed with probability `STUDENT_EMPLOYMENT_SHARE`, exact quota per district).
- `job_district` is only set for employed agents (any occupation): 50% work in their home
  district, the rest are assigned a district weighted by `jobs_per_resident * population`
  (which can still land on the home district).
- **Tenure and housing cost.**
    - Renters (`Tenure.RENTER`): `rent_monthly` starts near `avg_rent_monthly`, scaled by the
      agent's household-income percentile within the district (`0.85 + 0.3 * percentile`,
      mean 1.0 over a uniform percentile so richer households get pricier flats on average
      without moving the district mean) times lognormal noise (sigma 0.2), then the whole
      district's renter rents are rescaled by a single factor so their mean exactly equals
      `avg_rent_monthly` (the profile's "typical rental flat" price is read as the *renter*
      market rate). Clipped to [200, 8000].
    - Owners (`Tenure.OWNER`): `rent_monthly` holds their housing cost, not a lease payment
      (see `Agent.tenure` docstring in types.py). Age < `MORTGAGE_AGE_CUTOFF` (55): still
      paying a mortgage, modelled as `MORTGAGE_RENT_FRACTION (0.55) * avg_rent_monthly *
      lognormal noise (sigma 0.15)`. Age >= 55: mortgage-free, just fees/IBI, modelled as
      `Uniform(OWNER_FEES_MIN, OWNER_FEES_MAX)` = 150-250 EUR/month. Both are plausible,
      undocumented-by-data assumptions (no Open Data BCN mortgage/fees dataset), see the
      final report.
- `household_size` (1-4) is drawn per age bucket as `1 + Binomial(3, p_bucket)`. `p_bucket`
  keeps the original age shape (young adults skew smaller, prime age larger, older groups
  skew back down) via each bucket's baseline mean (`_HH_BASE_MEAN`), shifted by one
  district-wide `delta` solved so the age-weighted mean household size matches
  `avg_household_size` (fallback `DEFAULT_AVG_HOUSEHOLD_SIZE`) exactly in expectation (the
  binomial mean is linear in `p`, so `delta = (target - base_weighted_mean) / 3`).
- `lease_start_tick` is uniform in [-359, 0] for every agent (renters: lease already "in
  progress" at sim start; owners: unused for renewals but kept populated for uniformity).
- `savings` is modelled as 0.5-12 months of `wage_monthly` (lognormal, median ~2 months).
- `spending_level` and `satisfaction` are Beta-distributed: spending shifts up with wage
  percentile; satisfaction shifts down as rent/housing-cost burden (burden / income)
  increases.

Everything here is grouped per district (5-100 districts, not 10_000 agents) and vectorized
with numpy within each district, so it stays well under 1s for 10_000 agents.
"""

from __future__ import annotations

import math

import numpy as np

from jevcity.types import Agent, DistrictProfile, Occupation, Tenure

_ADULT_BUCKETS = ("18-34", "35-49", "50-64", "65+")
_BUCKET_AGE_RANGE = {
    "18-34": (18, 34),
    "35-49": (35, 49),
    "50-64": (50, 64),
    "65+": (65, 90),
}
_SKILL_MULT = {"low": 0.7, "mid": 1.0, "high": 1.7}
# Baseline (age-bucket) household-size shape: mean of the pre-rewrite _HH_SIZE_PROBS rows,
# reused only to derive each bucket's Binomial(3, p) parameter (see _household_size_probs).
_HH_BASE_MEAN = {"18-34": 1.8, "35-49": 2.85, "50-64": 2.4, "65+": 1.9}

DEFAULT_OWNER_SHARE = 0.55
OWNER_INCOME_EXPONENT = 2.0  # how strongly ownership concentrates in richer households
DEFAULT_AVG_HOUSEHOLD_SIZE = 2.45
STILL_WORKING_65_67_SHARE = 0.2  # share of 65-67 year olds who keep working past retirement age
STUDENT_SHARE_18_24 = 0.4
STUDENT_EMPLOYMENT_SHARE = 0.3
NET_INCOME_FACTOR = 0.8  # gross -> net/disposable wedge, applied only when not already disposable
MORTGAGE_AGE_CUTOFF = 55
MORTGAGE_RENT_FRACTION = 0.55
MORTGAGE_NOISE_SIGMA = 0.15
OWNER_FEES_MIN = 150.0
OWNER_FEES_MAX = 250.0


def _largest_remainder(weights: np.ndarray, total: int) -> np.ndarray:
    """Allocate `total` integer units proportional to `weights`, exact sum, stable ties."""
    weights = np.asarray(weights, dtype=float)
    if weights.sum() <= 0:
        weights = np.ones_like(weights)
    ideal = weights / weights.sum() * total
    floor = np.floor(ideal).astype(int)
    remainder = total - int(floor.sum())
    if remainder > 0:
        frac = ideal - floor
        order = np.argsort(-frac, kind="stable")
        floor[order[:remainder]] += 1
    elif remainder < 0:  # pragma: no cover - only possible with pathological float rounding
        frac = ideal - floor
        order = np.argsort(frac, kind="stable")
        floor[order[: -remainder]] -= 1
    return floor


def _inverse_cdf_sample(cum_probs: np.ndarray, u: np.ndarray) -> np.ndarray:
    """cum_probs: (n, k) cumulative probabilities per row; u: (n,) uniforms -> index in [0, k)."""
    return (cum_probs < u[:, None]).sum(axis=1)


def _quota(rate: float, n: int) -> int:
    return int(min(max(round(rate * n), 0), n))


def _household_income_monthly(profile: DistrictProfile) -> float:
    """Whole-household monthly income used as the wage/pension base for that district."""
    if profile.income_per_household_annual is not None:
        annual = profile.income_per_household_annual
        ref = (profile.refs.get("income_per_household_annual", "") or "").lower()
        already_net = "disposable" in ref or "disponible" in ref
        if not already_net:
            annual *= NET_INCOME_FACTOR
        return annual / 12.0
    avg_hh = profile.avg_household_size or DEFAULT_AVG_HOUSEHOLD_SIZE
    # income_per_capita_annual is already disposable (scripts/fetch_opendata.py), no further
    # gross -> net conversion needed for the fallback.
    return profile.income_per_capita_annual * avg_hh / 12.0


def _household_size_probs(avg_household_size: float | None, age_probs: np.ndarray) -> np.ndarray:
    """(4, 4) matrix (adult bucket x size 1..4): 1 + Binomial(3, p_bucket), p_bucket keeping
    the original age shape but shifted so the age-weighted mean size matches
    avg_household_size (fallback DEFAULT_AVG_HOUSEHOLD_SIZE); see module docstring."""
    target = avg_household_size or DEFAULT_AVG_HOUSEHOLD_SIZE
    base_means = np.array([_HH_BASE_MEAN[b] for b in _ADULT_BUCKETS])
    base_weighted_mean = float(np.dot(age_probs, base_means))
    delta = (target - base_weighted_mean) / 3.0
    p = np.clip((base_means - 1.0) / 3.0 + delta, 0.02, 0.98)
    probs = np.empty((4, 4))
    for k in range(4):
        c = math.comb(3, k)
        probs[:, k] = c * (p**k) * ((1 - p) ** (3 - k))
    probs /= probs.sum(axis=1, keepdims=True)
    return probs


def _weighted_top_k(rng: np.random.Generator, propensity: np.ndarray, k: int) -> np.ndarray:
    """Efraimidis-Spirakis weighted sampling without replacement: returns the (exactly k)
    indices most likely to be chosen when higher `propensity` means more likely, with an
    *exact* count k (unlike independent Bernoulli draws)."""
    n = propensity.shape[0]
    k = max(0, min(k, n))
    if k == 0:
        return np.array([], dtype=int)
    if k == n:
        return np.arange(n)
    u = np.clip(rng.random(n), 1e-12, 1.0)
    safe_prop = np.clip(propensity, 1e-6, None)
    key = u ** (1.0 / safe_prop)
    order = np.argsort(-key, kind="stable")
    return order[:k]


def _generate_district(
    profile: DistrictProfile,
    cnt: int,
    id_offset: int,
    rng: np.random.Generator,
    city_mean_income: float,
    job_district_ids: np.ndarray,
    job_cum_probs: np.ndarray | None,
) -> list[Agent]:
    # --- age --------------------------------------------------------------------------
    raw = np.array([profile.age_distribution.get(b, 0.0) for b in _ADULT_BUCKETS])
    age_probs = raw / raw.sum() if raw.sum() > 0 else np.full(4, 0.25)
    cum_age = np.cumsum(age_probs)
    bucket_idx = _inverse_cdf_sample(np.broadcast_to(cum_age, (cnt, 4)), rng.random(cnt))
    low_arr = np.array([_BUCKET_AGE_RANGE[b][0] for b in _ADULT_BUCKETS])[bucket_idx]
    high_arr = np.array([_BUCKET_AGE_RANGE[b][1] for b in _ADULT_BUCKETS])[bucket_idx]
    ages = rng.integers(low_arr, high_arr + 1)

    all_idx = np.arange(cnt)

    # --- retirement (exact quota among 65-67, rest of 65+ retired) ---------------------
    occ = np.empty(cnt, dtype=object)
    occ[:] = None  # filled below
    idx_65_67 = all_idx[(ages >= 65) & (ages <= 67)]
    idx_65plus = all_idx[ages >= 65]
    n_still_working = _quota(STILL_WORKING_65_67_SHARE, len(idx_65_67))
    still_working_idx = (
        idx_65_67[rng.permutation(len(idx_65_67))[:n_still_working]]
        if len(idx_65_67)
        else np.array([], dtype=int)
    )
    retired_idx = np.setdiff1d(idx_65plus, still_working_idx, assume_unique=False)
    occ[retired_idx] = Occupation.RETIRED

    # --- students (exact quota among 18-24, excluding anyone already retired) ----------
    idx_18_24 = np.setdiff1d(all_idx[(ages >= 18) & (ages <= 24)], retired_idx)
    n_students = _quota(STUDENT_SHARE_18_24, len(idx_18_24))
    student_idx = (
        idx_18_24[rng.permutation(len(idx_18_24))[:n_students]]
        if len(idx_18_24)
        else np.array([], dtype=int)
    )
    occ[student_idx] = Occupation.STUDENT

    # --- remaining working-age pool: exact skill-mix quotas ----------------------------
    eligible_idx = np.setdiff1d(all_idx, np.concatenate([retired_idx, student_idx]))
    n_eligible = len(eligible_idx)
    r = profile.income_per_capita_annual / city_mean_income if city_mean_income > 0 else 1.0
    high_p = min(max(0.15 * r, 0.03), 0.5)
    low_p = min(max(0.5 / r, 0.2), 0.7)
    mid_p = max(1.0 - high_p - low_p, 0.05)
    skill_probs = np.array([low_p, mid_p, high_p])
    skill_probs = skill_probs / skill_probs.sum()
    skill_quotas = _largest_remainder(skill_probs, n_eligible)  # [low, mid, high]

    shuffled_eligible = eligible_idx[rng.permutation(n_eligible)] if n_eligible else eligible_idx
    low_idx = shuffled_eligible[: skill_quotas[0]]
    mid_idx = shuffled_eligible[skill_quotas[0] : skill_quotas[0] + skill_quotas[1]]
    high_idx = shuffled_eligible[skill_quotas[0] + skill_quotas[1] :]
    occ[low_idx] = Occupation.LOW_SKILL
    occ[mid_idx] = Occupation.MID_SKILL
    occ[high_idx] = Occupation.HIGH_SKILL

    # --- employment: exact unemployment quota over the eligible pool -------------------
    employed = np.zeros(cnt, dtype=bool)
    n_unemployed = _quota(profile.unemployment_rate, n_eligible)
    unemployed_idx = (
        eligible_idx[rng.permutation(n_eligible)[:n_unemployed]] if n_eligible else eligible_idx
    )
    employed[eligible_idx] = True
    employed[unemployed_idx] = False

    n_stud = len(student_idx)
    n_student_employed = _quota(STUDENT_EMPLOYMENT_SHARE, n_stud)
    student_employed_idx = (
        student_idx[rng.permutation(n_stud)[:n_student_employed]] if n_stud else student_idx
    )
    employed[student_employed_idx] = True
    # retired_idx stays False (pension flows through Agent.income_monthly regardless)

    days_unemployed = np.zeros(cnt, dtype=int)
    need_days = (~employed) & (occ != Occupation.RETIRED)
    n_need = int(need_days.sum())
    if n_need:
        days_unemployed[need_days] = rng.integers(0, 601, size=n_need)

    # --- wage / household income ---------------------------------------------------------
    household_income_monthly = _household_income_monthly(profile)
    weighted_mult_mean = (
        skill_probs[0] * _SKILL_MULT["low"]
        + skill_probs[1] * _SKILL_MULT["mid"]
        + skill_probs[2] * _SKILL_MULT["high"]
    )
    monthly_base = household_income_monthly / max(weighted_mult_mean, 1e-6)

    wage_base = np.full(cnt, monthly_base)
    wage_base[low_idx] = monthly_base * _SKILL_MULT["low"]
    wage_base[mid_idx] = monthly_base * _SKILL_MULT["mid"]
    wage_base[high_idx] = monthly_base * _SKILL_MULT["high"]
    wage_base[retired_idx] = 0.75 * monthly_base * _SKILL_MULT["mid"]
    wage_base[student_idx] = 600.0

    sigma = np.full(cnt, 0.3)
    sigma[retired_idx] = 0.2
    sigma[student_idx] = 0.25

    wage = rng.lognormal(mean=np.log(np.clip(wage_base, 1.0, None)), sigma=sigma)
    wage = np.clip(wage, 450.0, 12000.0)

    # --- tenure: exact owner quota, weighted by age AND income ---------------------------
    # Renters in Barcelona are younger and poorer than owners; weighting by age alone left
    # renters with average incomes and an unrealistically low rent burden (median ~25%).
    owner_share = profile.owner_share if profile.owner_share is not None else DEFAULT_OWNER_SHARE
    n_owners = _quota(owner_share, cnt)
    income_pct = (np.argsort(np.argsort(wage)) + 0.5) / cnt
    propensity = np.clip(ages / 70.0, 0.2, 2.5) * (0.25 + income_pct) ** OWNER_INCOME_EXPONENT
    owner_idx = _weighted_top_k(rng, propensity, n_owners)
    tenure = np.empty(cnt, dtype=object)
    tenure[:] = Tenure.RENTER  # plain assignment (not np.full) keeps the StrEnum, not str
    tenure[owner_idx] = Tenure.OWNER

    # --- job_district: 50% work in their home district, the rest weighted by
    # jobs_per_resident * population across every district (may still land back home) -------
    own_district = rng.random(cnt) < 0.5
    job_district_list: list[str | None] = [None] * cnt
    if job_cum_probs is not None:
        other_u = rng.random(cnt)
        other_idx = _inverse_cdf_sample(np.broadcast_to(job_cum_probs, (cnt, len(job_cum_probs))), other_u)
        for i in range(cnt):
            if not employed[i]:
                continue
            job_district_list[i] = profile.id if own_district[i] else job_district_ids[other_idx[i]]

    # --- household size -----------------------------------------------------------------
    hh_probs = _household_size_probs(profile.avg_household_size, age_probs)
    cum_hh = np.cumsum(hh_probs, axis=1)
    size_idx = _inverse_cdf_sample(cum_hh[bucket_idx], rng.random(cnt))
    household_size = size_idx + 1

    # --- income_monthly (mirrors Agent.income_monthly) for rent/savings calc -----------
    income_monthly = np.where(
        employed | (occ == Occupation.RETIRED),
        wage,
        np.where(days_unemployed < 360, 0.6 * wage, 450.0),
    )

    wage_rank = np.argsort(np.argsort(wage))
    percentile = wage_rank / (cnt - 1) if cnt > 1 else np.full(cnt, 0.5)

    # --- rent (renters) / housing cost (owners) -----------------------------------------
    rent = np.zeros(cnt)
    renter_mask = tenure == Tenure.RENTER
    owner_mask = ~renter_mask

    if renter_mask.any():
        scale = 0.85 + 0.3 * percentile
        noise = rng.lognormal(mean=0.0, sigma=0.2, size=cnt)
        raw_rent = profile.avg_rent_monthly * scale * noise
        mean_raw = raw_rent[renter_mask].mean()
        calib = profile.avg_rent_monthly / mean_raw if mean_raw > 0 else 1.0
        rent[renter_mask] = np.clip(raw_rent[renter_mask] * calib, 200.0, 8000.0)

    if owner_mask.any():
        n_own = int(owner_mask.sum())
        owner_ages = ages[owner_mask]
        mortgage_mask = owner_ages < MORTGAGE_AGE_CUTOFF
        housing_cost = np.empty(n_own)
        n_mort = int(mortgage_mask.sum())
        if n_mort:
            housing_cost[mortgage_mask] = (
                profile.avg_rent_monthly
                * MORTGAGE_RENT_FRACTION
                * rng.lognormal(mean=0.0, sigma=MORTGAGE_NOISE_SIGMA, size=n_mort)
            )
        n_free = n_own - n_mort
        if n_free:
            housing_cost[~mortgage_mask] = rng.uniform(OWNER_FEES_MIN, OWNER_FEES_MAX, size=n_free)
        rent[owner_mask] = np.clip(housing_cost, 50.0, 8000.0)

    rent_burden = rent / np.maximum(income_monthly, 1e-6)

    # --- lease / savings / spending / satisfaction --------------------------------------
    lease_start_tick = rng.integers(-359, 1, size=cnt)

    months = np.clip(rng.lognormal(mean=np.log(2.0), sigma=0.6, size=cnt), 0.5, 12.0)
    savings = months * wage

    mean_spend = np.clip(0.3 + 0.4 * percentile, 0.05, 0.95)
    conc = 8.0
    spending_level = rng.beta(mean_spend * conc, (1.0 - mean_spend) * conc)

    mean_sat = np.clip(0.75 - 0.8 * rent_burden, 0.05, 0.95)
    satisfaction = rng.beta(mean_sat * conc, (1.0 - mean_sat) * conc)

    agents: list[Agent] = []
    for i in range(cnt):
        agents.append(
            Agent(
                id=id_offset + i,
                age=int(ages[i]),
                household_size=int(household_size[i]),
                occupation=occ[i],
                wage_monthly=float(wage[i]),
                employed=bool(employed[i]),
                job_district=job_district_list[i],
                home=profile.id,
                rent_monthly=float(rent[i]),
                lease_start_tick=int(lease_start_tick[i]),
                savings=float(savings[i]),
                spending_level=float(spending_level[i]),
                satisfaction=float(satisfaction[i]),
                days_unemployed=int(days_unemployed[i]),
                tenure=tenure[i],
            )
        )
    return agents


def generate_population(
    profiles: list[DistrictProfile], n_agents: int, rng: np.random.Generator
) -> list[Agent]:
    """Create n_agents adults (18+) distributed by district population, with age from the
    district age distribution (renormalized without 0-17), exact per-district quotas for
    employment/tenure/occupation mix (see module docstring), household income from
    income_per_household_annual (or income_per_capita * avg_household_size), job district
    weighted by jobs_per_resident, rent near avg_rent_monthly for renters / mortgage-or-fees
    for owners, staggered lease_start_tick in [-359, 0]. Deterministic for a given rng seed
    and fast for 10_000 agents (<1s)."""
    pop = np.array([p.population for p in profiles], dtype=float)
    counts = _largest_remainder(pop, n_agents)

    total_pop = pop.sum()
    city_mean_income = (
        float(np.sum([p.income_per_capita_annual * p.population for p in profiles]) / total_pop)
        if total_pop > 0
        else float(np.mean([p.income_per_capita_annual for p in profiles]))
    )

    ids_arr = np.array([p.id for p in profiles], dtype=object)
    job_weights = np.array([p.jobs_per_resident * p.population for p in profiles], dtype=float)
    job_cum_probs = None
    if job_weights.sum() > 0:
        job_cum_probs = np.cumsum(job_weights / job_weights.sum())

    agents: list[Agent] = []
    aid = 0
    for j, profile in enumerate(profiles):
        cnt = int(counts[j])
        if cnt == 0:
            continue
        agents.extend(
            _generate_district(profile, cnt, aid, rng, city_mean_income, ids_arr, job_cum_probs)
        )
        aid += cnt

    return agents
