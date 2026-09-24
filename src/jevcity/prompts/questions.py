"""Static (per-K) Jev question definitions and mock-priors heuristics.

The `action` / `spending` / `satisfaction` question text and criteria are identical
for every agent at K=1 (see docs/jev-reference/primitives.md: "criteria aligned with
instructions", and model-jaggedness "literal reading" -> spell out each option).
That makes them cacheable by the real backend. Only `destination`'s criteria depend
on the world's districts, which are fixed for a whole run.
"""

from __future__ import annotations

from collections.abc import Iterable

from jevcity.prompts.buckets import (
    burden_label,
    monthly_expenses,
    savings_label,
    savings_months,
)
from jevcity.types import (
    Action,
    Agent,
    Event,
    EventKind,
    Occupation,
    World,
)

ACTION_INSTRUCTIONS = (
    "What will this person most likely do today given their situation and today's events?"
)

ACTION_CRITERIA: dict[str, str] = {
    Action.STAY: "Continue with the current home and job unchanged today; nothing here forces a change.",
    Action.MOVE: (
        "Start moving to a different home now, because the current one is unaffordable, "
        "unsuitable, or a clearly better option exists."
    ),
    Action.JOB_SEARCH: (
        "Actively look for a new job today, because this person is unemployed, at risk of "
        "losing their job, or a clearly better job opportunity has appeared."
    ),
    Action.SPEND: "Spend freely on discretionary (non-essential) purchases today.",
    Action.SAVE: "Prioritize saving money today, cutting back on discretionary spending.",
}

DESTINATION_INSTRUCTIONS = (
    "If this person moved now, which district would they most likely choose? Respect what "
    "`districts` shows about affordability, rent trend, jobs and commute for each option."
)

SPENDING_INSTRUCTIONS = (
    "How much discretionary (non-essential) spending will this person do this month?"
)

SPENDING_CRITERIA: list[str] = [
    "Cuts all non-essential spending",
    "Spends cautiously, limiting extras",
    "Spends a moderate, typical amount on extras",
    "Spends comfortably on extras",
    "Spends freely on extras",
]

SATISFACTION_INSTRUCTIONS = (
    "How satisfied is this person with their housing, work and neighbourhood right now?"
)

SATISFACTION_CRITERIA: list[str] = [
    "Very unhappy with housing, work and neighbourhood",
    "Unhappy with housing, work and neighbourhood",
    "Neutral or mixed feelings about housing, work and neighbourhood",
    "Happy with housing, work and neighbourhood",
    "Very happy with housing, work and neighbourhood",
]

SPENDING_LEVELS = len(SPENDING_CRITERIA)
SATISFACTION_LEVELS = len(SATISFACTION_CRITERIA)


def destination_criteria(world: World) -> dict[str, str]:
    """Option -> display name, identical for every agent within a run (K=1 and K>1)."""
    return {
        did: f"{profile.name} district" for did, profile in sorted(world.profiles.items())
    }


def action_question(*, person_ref: str | None = None) -> dict:
    instructions = ACTION_INSTRUCTIONS
    if person_ref is not None:
        instructions = (
            f"What will `{person_ref}` most likely do today given their situation and "
            f"today's events in `{person_ref}.today`?"
        )
    return {"type": "choice", "instructions": instructions, "criteria": dict(ACTION_CRITERIA)}


def destination_question(world: World, *, person_ref: str | None = None) -> dict:
    instructions = DESTINATION_INSTRUCTIONS
    if person_ref is not None:
        instructions = (
            f"If `{person_ref}` moved now, which district would they most likely choose? "
            f"Respect what `districts` and `{person_ref}.affordability` show about "
            "affordability, rent trend, jobs and commute for each option."
        )
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": destination_criteria(world),
    }


def spending_question(*, person_ref: str | None = None) -> dict:
    instructions = SPENDING_INSTRUCTIONS
    if person_ref is not None:
        instructions = f"How much discretionary (non-essential) spending will `{person_ref}` do this month?"
    return {"type": "score", "instructions": instructions, "criteria": list(SPENDING_CRITERIA)}


def satisfaction_question(*, person_ref: str | None = None) -> dict:
    instructions = SATISFACTION_INSTRUCTIONS
    if person_ref is not None:
        instructions = (
            f"How satisfied is `{person_ref}` with their housing, work and neighbourhood "
            "right now?"
        )
    return {
        "type": "score",
        "instructions": instructions,
        "criteria": list(SATISFACTION_CRITERIA),
    }


# --- mock priors --------------------------------------------------------------------------


def mock_priors_for_agent(
    agent: Agent, events: Iterable[Event], world: World
) -> dict[str, dict[str, float]]:
    """Hand-written heuristics telling the MOCK backend how to weight random answers.

    Never sent to the real API. Kept as one readable function per question name.
    """
    events = list(events)
    burden = agent.rent_burden

    # --- action ---
    action_w = {
        Action.STAY: 6.0,
        Action.MOVE: 0.3,
        Action.JOB_SEARCH: 0.2,
        Action.SPEND: 1.0,
        Action.SAVE: 1.0,
    }
    label = burden_label(burden)
    if label == "high":
        action_w[Action.MOVE] += 2.0
    elif label == "severe":
        action_w[Action.MOVE] += 4.0
    for ev in events:
        if ev.kind is EventKind.LEASE_RENEWAL:
            increase = float(ev.payload.get("increase_pct", 0.0))
            if increase > 0.08:
                action_w[Action.MOVE] += 2.0
        if ev.kind is EventKind.LIFE_EVENT and ev.payload.get("kind") in ("new_child", "partner"):
            action_w[Action.MOVE] += 1.0
        if ev.kind is EventKind.JOB_LOSS:
            action_w[Action.JOB_SEARCH] += 6.0
        if ev.kind is EventKind.PAYDAY:
            months = savings_months(agent.savings, monthly_expenses(agent))
            if savings_label(months) == "solid":
                action_w[Action.SPEND] += 3.0

    is_unemployed = (
        not agent.employed
        and agent.occupation not in (Occupation.RETIRED, Occupation.STUDENT)
    )
    if is_unemployed:
        action_w[Action.JOB_SEARCH] += 5.0

    months = savings_months(agent.savings, monthly_expenses(agent))
    if savings_label(months) in ("thin", "none"):
        action_w[Action.SAVE] += 3.0

    # --- destination ---
    dest_w = _destination_weights(agent, world)

    # --- spending: shift weight toward extremes by burden and savings ---
    spending_target = 2  # neutral index of 5 levels (0..4)
    if label in ("high", "severe"):
        spending_target = 0
    elif savings_label(months) == "solid":
        spending_target = 3
    spending_w = _triangular_weights(SPENDING_LEVELS, spending_target)

    # --- satisfaction: centered on the agent's actual satisfaction ---
    sat_target = round(agent.satisfaction * (SATISFACTION_LEVELS - 1))
    sat_w = _triangular_weights(SATISFACTION_LEVELS, sat_target)

    return {
        "action": {k.value: v for k, v in action_w.items()},
        "destination": dest_w,
        "spending": {str(i): w for i, w in enumerate(spending_w)},
        "satisfaction": {str(i): w for i, w in enumerate(sat_w)},
    }


def _destination_weights(agent: Agent, world: World) -> dict[str, float]:
    weights: dict[str, float] = {}
    income = agent.income_monthly
    for did in world.profiles:
        state = world.states[did]
        pct = state.avg_rent / income if income > 0 else 9.99
        afford_factor = max(0.05, 1.0 - min(pct, 2.0) / 2.0)
        job_ratio = state.job_vacancies / state.jobs if state.jobs else 0.0
        job_factor = 1.0 + job_ratio
        w = afford_factor * job_factor
        if did == agent.home:
            w *= 1.2
        weights[did] = w
    return weights


def _triangular_weights(n: int, target: int) -> list[float]:
    target = max(0, min(n - 1, target))
    return [max(0.1, 1.0 - abs(i - target) * 0.4) for i in range(n)]
