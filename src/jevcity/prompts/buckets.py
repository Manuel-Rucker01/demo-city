"""Arithmetic-in-code helpers that turn raw numbers into short English buckets.

Per docs/jev-reference/model-jaggedness_jev-1.13.md: Jev is bad at math, so every
number that matters for a judgment is pre-digested here into a semantic label
(optionally still carrying the number, e.g. "severe: rent takes 52% of income").
Nothing in this module talks to Jev; it is pure formatting for state_builder.
"""

from __future__ import annotations

import math

from jevcity.types import Agent, LandlordType, Occupation, Tenure, World

TICKS_PER_MONTH = 30
MORTGAGE_AGE_CUTOFF = 55  # kept in sync with population/generator.py's constant of the same name
LOCK_IN_BELOW_MARKET_THRESHOLD = 0.85  # kept in sync with DistrictSnapshot.below_market_share


# --- rent burden -----------------------------------------------------------------------------


def burden_label(burden: float) -> str:
    if burden < 0.30:
        return "ok"
    if burden < 0.40:
        return "stretched"
    if burden < 0.50:
        return "high"
    return "severe"


def rent_burden_text(burden: float) -> str:
    pct = round(min(burden, 9.99) * 100)
    return f"{burden_label(burden)}: rent takes {pct}% of income"


def housing_cost_text(agent: Agent) -> str:
    """Like rent_burden_text, but words it as "housing cost" for owners (their rent_monthly
    is a mortgage/fees payment, not rent) and "rent" for renters."""
    pct = round(min(agent.rent_burden, 9.99) * 100)
    label = burden_label(agent.rent_burden)
    if agent.tenure is Tenure.OWNER:
        return f"{label}: housing cost takes {pct}% of income"
    return f"{label}: rent takes {pct}% of income"


# --- tenure ------------------------------------------------------------------------------


def tenure_text(agent: Agent) -> str:
    """Plain statement of housing tenure for the person state block.

    Owners' mortgage-vs-outright split isn't stored on Agent (only Tenure is); this infers
    it from age using the same MORTGAGE_AGE_CUTOFF the generator uses to decide it, which is
    an approximation but consistent with how the housing cost itself was generated.
    """
    if agent.tenure is Tenure.RENTER:
        return "rents their home"
    if agent.age < MORTGAGE_AGE_CUTOFF:
        return "owns their home (mortgage)"
    return "owns their home outright"


def housing_text(agent: Agent, market_rent: float | None = None) -> str:
    """One compact `person.rent_burden`-field sentence combining tenure_text and
    housing_cost_text (a separate "housing" key would push some K=1 states over the
    ~300-token budget; see state_builder.py's module docstring).

    When `market_rent` (the agent's home district's current new-lease market rent) is given
    and the agent is a renter paying below LOCK_IN_BELOW_MARKET_THRESHOLD of it, appends a
    short lock-in note so Jev can weigh staying put against a below-market lease -- see
    DistrictSnapshot.below_market_share, the same threshold used for reporting."""
    text = f"{tenure_text(agent)}; {housing_cost_text(agent)}"
    below_market = (
        agent.tenure is Tenure.RENTER
        and market_rent is not None
        and market_rent > 0
        and agent.rent_monthly < LOCK_IN_BELOW_MARKET_THRESHOLD * market_rent
    )
    if below_market:
        pct = round((1 - agent.rent_monthly / market_rent) * 100)
        text += f"; {pct}% below market"
    return text


# --- affordability of a (possibly different) district's new-lease rent ---------------------


def affordability_label(rent: float, income_monthly: float) -> str:
    if income_monthly <= 0:
        return "unaffordable"
    pct = rent / income_monthly
    return {
        "ok": "affordable",
        "stretched": "tight",
        "high": "strained",
        "severe": "unaffordable",
    }[burden_label(pct)]


def affordability_text(rent: float, income_monthly: float) -> str:
    """Compact rent + share-of-income + verdict, used in the K=1 per-agent `districts` entries.

    Kept terse (target ~40 chars) so 5 districts x 8 fields still fits the K=1 char budget.
    """
    label = affordability_label(rent, income_monthly)
    if income_monthly <= 0:
        return f"€{round(rent)}/mo, no income, {label}"
    pct = round(rent / income_monthly * 100)
    return f"€{round(rent)}/mo {pct}% {label}"


# --- savings -----------------------------------------------------------------------------


def savings_months(savings: float, monthly_expenses: float) -> float:
    if monthly_expenses <= 0:
        return 99.0
    return max(savings, 0.0) / monthly_expenses


def savings_label(months: float) -> str:
    if months < 0.5:
        return "none"
    if months < 2:
        return "thin"
    if months < 6:
        return "some"
    return "solid"


def savings_text(savings: float, monthly_expenses: float) -> str:
    months = savings_months(savings, monthly_expenses)
    months_r = round(min(months, 99.0), 1)
    return f"{savings_label(months)}: {months_r}mo saved"


# --- rent trend --------------------------------------------------------------------------


def rent_trend_text(history: list[float] | None) -> str:
    if not history or len(history) < 2 or history[-2] <= 0:
        return "stable"
    change = (history[-1] - history[-2]) / history[-2]
    if change < -0.02:
        return "falling"
    if change <= 0.02:
        return "stable"
    if change <= 0.08:
        return "rising"
    return "rising fast"


# --- vacancy -------------------------------------------------------------------------------


def vacancy_label(vacancy_rate: float) -> str:
    if vacancy_rate < 0.02:
        return "very scarce"
    if vacancy_rate < 0.04:
        return "scarce"
    if vacancy_rate < 0.08:
        return "normal"
    return "plenty"


def vacancy_text(vacancy_rate: float) -> str:
    pct = round(vacancy_rate * 100)
    return f"{vacancy_label(vacancy_rate)} {pct}%vacant"


# --- job market ----------------------------------------------------------------------------


def job_market_text(unemployment_rate: float, job_vacancies: int, jobs: int) -> str:
    ratio = job_vacancies / jobs if jobs > 0 else 0.0
    pct = round(unemployment_rate * 100)
    if unemployment_rate < 0.08 and ratio > 0.05:
        label = "hiring"
    elif unemployment_rate > 0.12 or ratio < 0.01:
        label = "difficult"
    else:
        label = "steady"
    return f"{label} {pct}%unemp"


# --- transit ------------------------------------------------------------------------------


def transit_text(transit_score: float) -> str:
    if transit_score >= 0.8:
        return "excellent"
    if transit_score >= 0.6:
        return "good"
    if transit_score >= 0.4:
        return "limited"
    return "poor"


# --- satisfaction bucket (person overview, distinct from the satisfaction question) --------


def satisfaction_bucket(satisfaction: float) -> str:
    if satisfaction < 0.2:
        return "very unhappy"
    if satisfaction < 0.4:
        return "unhappy"
    if satisfaction < 0.6:
        return "neutral"
    if satisfaction < 0.8:
        return "happy"
    return "very happy"


# --- work / person text ---------------------------------------------------------------------

_OCC_LABEL = {
    Occupation.LOW_SKILL: "low-skill",
    Occupation.MID_SKILL: "mid-skill",
    Occupation.HIGH_SKILL: "high-skill",
}


def work_text(agent: Agent, world: World) -> str:
    if agent.occupation is Occupation.STUDENT:
        return "student"
    if agent.occupation is Occupation.RETIRED:
        return "retired"
    if agent.employed:
        district_name = agent.job_district
        if district_name and district_name in world.profiles:
            district_name = world.profiles[district_name].name
        occ = _OCC_LABEL.get(agent.occupation, str(agent.occupation))
        return f"employed as {occ} worker in {district_name}"
    months = agent.days_unemployed // TICKS_PER_MONTH
    if months < 1:
        return "unemployed for less than a month"
    unit = "month" if months == 1 else "months"
    return f"unemployed for {months} {unit}"


def years_in_home_text(tick: int, lease_start_tick: int) -> str:
    days = max(tick - lease_start_tick, 0)
    years = days / (TICKS_PER_MONTH * 12)
    if years < 1:
        return "less than 1 year"
    years_r = round(years)
    unit = "year" if years_r == 1 else "years"
    return f"{years_r} {unit}"


def monthly_expenses(agent: Agent) -> float:
    """Proxy for a household's recurring monthly cost, used only for the savings bucket."""
    return agent.rent_monthly if agent.rent_monthly > 0 else max(agent.income_monthly * 0.5, 1.0)


def commute_text(agent: Agent, district_id: str, world: World) -> str:
    if agent.occupation in (Occupation.STUDENT, Occupation.RETIRED) or not agent.job_district:
        return "n.a."
    if agent.job_district == district_id:
        return "same"
    home_c = world.profiles[agent.job_district].centroid
    dst_c = world.profiles[district_id].centroid
    dist = math.hypot(home_c[0] - dst_c[0], home_c[1] - dst_c[1])
    return "short" if dist < 0.03 else "long"


def own_commute_length_text(agent: Agent, world: World) -> str:
    """Bucketed commute length between the agent's own home and job district (reuses
    commute_text with district_id=home, which already computes the job<->home distance)."""
    return commute_text(agent, agent.home, world)


def commute_habit_text(tick: int, commute_since_tick: int | None) -> str:
    """Compact "how long they've commuted this way" label for the person block's `commute`
    field, e.g. "6y" / "<1y" (see Agent.commute_since_tick; None = never explicitly set,
    e.g. a hand-built test agent -> treated as brand new)."""
    if commute_since_tick is None:
        return "<1y"
    days = max(tick - commute_since_tick, 0)
    years = days / (TICKS_PER_MONTH * 12)
    if years < 1:
        return "<1y"
    return f"{round(years)}y"


# --- tourism / local commerce / transit boost / LEZ (new district-state fields) -----------


def tourism_label(tourist_units: int, housing_units: int) -> str:
    if housing_units <= 0 or tourist_units <= 0:
        return "none"
    share = tourist_units / housing_units
    if share < 0.02:
        return "none"
    if share < 0.08:
        return "low"
    if share < 0.15:
        return "high"
    return "very high"


def shops_trend_label(shops_open: int, baseline_shops: float) -> str:
    """shops_open (current) vs baseline_shops (profile.shops at run start). 0 on either side
    means the market model hasn't populated shop counts yet -> treat as "stable" (no signal)."""
    if shops_open <= 0 or baseline_shops <= 0:
        return "stable"
    ratio = shops_open / baseline_shops
    if ratio < 0.9:
        return "closing"
    if ratio > 1.1:
        return "opening"
    return "stable"


def transit_bucket_text(transit_score: float, transit_boost: float) -> str:
    """Transit label from profile.transit_score + state.transit_boost, with a short note
    when a boost (new line) is in effect."""
    label = transit_text(min(transit_score + transit_boost, 1.0))
    return f"{label}, new metro line" if transit_boost > 0 else label


def lez_note_text(car_cost_extra_monthly: float) -> str:
    return f"low-emission zone: +€{round(car_cost_extra_monthly)}/mo by car"


# --- landlord (rental-supply decisions) -----------------------------------------------------


def landlord_type_text(landlord_type: LandlordType) -> str:
    if landlord_type is LandlordType.LARGE:
        return "company owning many flats"
    return "small landlord, owns this one flat"


def flat_condition_text(quality: float) -> str:
    """Bucketed from DistrictState.quality (rental stock maintenance level, 0..1)."""
    if quality >= 0.8:
        return "good condition"
    if quality >= 0.5:
        return "some wear"
    return "run down"


def tenant_years_text(years: float) -> str:
    if years < 1:
        return "less than 1 year"
    years_r = round(years)
    unit = "year" if years_r == 1 else "years"
    return f"{years_r} {unit}"


def rent_cap_note_text(rent_cap: float) -> str:
    return f"new leases limited to €{round(rent_cap)}/mo, below market"


def seasonal_option_text(
    market_rent: float, seasonal_rent_multiple: float, *, rent_cap_active: bool, seasonal_capped: bool
) -> str:
    """Seasonal-let income estimate. Only mentions cap coverage when a cap is actually active
    for this district (RentalSupplyParams.seasonal_capped otherwise has nothing to bite)."""
    seasonal_rent = round(market_rent * seasonal_rent_multiple)
    if not rent_cap_active:
        return f"seasonal lets earn about €{seasonal_rent}/mo"
    coverage = "covered by the cap" if seasonal_capped else "not covered by the cap"
    return f"seasonal lets earn about €{seasonal_rent}/mo, {coverage}"


def sale_buyers_text(vacancy_rate: float) -> str:
    """Qualitative buyer demand for a sale. DistrictState has no dedicated owner-market
    signal, so this is proxied from the district's overall residential vacancy rate: a tight
    market (scarce vacancy) plausibly means keen buyer demand too, and vice versa."""
    buyers = "plentiful" if vacancy_rate < 0.05 else "few"
    return f"selling is possible; buyers are {buyers}"


def renovation_text(renovation_ticks: int) -> str:
    months = round(renovation_ticks / TICKS_PER_MONTH)
    return f"renovation takes about {months} months, then relet"
