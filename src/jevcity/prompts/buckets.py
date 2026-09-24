"""Arithmetic-in-code helpers that turn raw numbers into short English buckets.

Per docs/jev-reference/model-jaggedness_jev-1.13.md: Jev is bad at math, so every
number that matters for a judgment is pre-digested here into a semantic label
(optionally still carrying the number, e.g. "severe: rent takes 52% of income").
Nothing in this module talks to Jev; it is pure formatting for state_builder.
"""

from __future__ import annotations

import math

from jevcity.types import Agent, Occupation, World

TICKS_PER_MONTH = 30


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
    return f"{savings_label(months)}: {months_r} months of expenses saved"


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
