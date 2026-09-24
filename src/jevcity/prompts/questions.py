"""Static (per-K) Jev question definitions and mock-priors heuristics.

The `action` / `spending` / `satisfaction` question text and criteria are identical
for every agent at K=1 (see docs/jev-reference/primitives.md: "criteria aligned with
instructions", and model-jaggedness "literal reading" -> spell out each option).
That makes them cacheable by the real backend. `destination`'s criteria depend on the
agent's own relevant-districts shortlist (see state_builder._select_relevant_districts),
and `commute_mode`/`shopping_place` are only built (and only asked) when relevant --
see state_builder.wants_commute_question / wants_shopping_question.
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
    LEAVE_CITY,
    Action,
    Agent,
    CommuteMode,
    DistrictId,
    Event,
    EventKind,
    LandlordAction,
    LandlordType,
    Occupation,
    ShoppingPlace,
    Tenure,
    Vacancy,
    World,
)

OWNER_MOVE_PRIOR_FACTOR = 0.2  # owners are much less likely to move than renters
TICKS_PER_YEAR = 360  # kept in sync with population/generator.py and buckets.py
COMMUTE_HABIT_STICKINESS_PER_YEAR = 0.5  # extra weight on the current mode per year of habit
COMMUTE_HABIT_MAX_YEARS = 6.0  # habit stickiness saturates here (well-worn routine)

ACTION_INSTRUCTIONS = (
    "What will this person most likely do today given their situation and today's events?"
)

ACTION_CRITERIA: dict[str, str] = {
    Action.STAY: "Keep the current home and job unchanged today.",
    Action.MOVE: "Start moving home now: current one is unaffordable, unsuitable, or a clearly better option exists.",
    Action.JOB_SEARCH: "Look for a new job: unemployed, at risk of job loss, or a clearly better offer appeared.",
    Action.SPEND: "Spend freely on non-essential purchases today.",
    Action.SAVE: "Cut discretionary spending and prioritize saving today.",
}

DESTINATION_INSTRUCTIONS = (
    "If this person moved now, which district (or leaving Barcelona) would they most likely "
    "choose? Respect what `districts` shows about affordability, rent trend, jobs and commute."
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

COMMUTE_MODE_INSTRUCTIONS = "How will this person most likely commute to work now?"

SHOPPING_PLACE_INSTRUCTIONS = "Where will this person most likely do their shopping this month?"

SHOPPING_PLACE_CRITERIA: dict[str, str | None] = {
    ShoppingPlace.LOCAL: "shops in their own home district",
    ShoppingPlace.WORK_DISTRICT: "shops near their job",
    ShoppingPlace.CENTRE: "Ciutat Vella / Eixample shopping streets",
    ShoppingPlace.ONLINE: None,
}


def destination_criteria(district_ids: list[DistrictId], world: World) -> dict[str, str]:
    """Option -> 2-4 word hint; state's `districts` already describes each one in full."""
    criteria = {did: f"{world.profiles[did].name} district" for did in district_ids}
    criteria[LEAVE_CITY] = "Leave Barcelona altogether"
    return criteria


def action_question(*, person_ref: str | None = None) -> dict:
    instructions = ACTION_INSTRUCTIONS
    if person_ref is not None:
        instructions = (
            f"What will `{person_ref}` most likely do today given their situation and "
            f"today's events in `{person_ref}.today`?"
        )
    return {"type": "choice", "instructions": instructions, "criteria": dict(ACTION_CRITERIA)}


def destination_question(
    district_ids: list[DistrictId], world: World, *, person_ref: str | None = None
) -> dict:
    instructions = DESTINATION_INSTRUCTIONS
    if person_ref is not None:
        instructions = (
            f"If `{person_ref}` moved now, which district (or leaving Barcelona) would they "
            f"most likely choose? Respect what `districts` and `{person_ref}.affordability` "
            "show about affordability, rent trend, jobs and commute for each option."
        )
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": destination_criteria(district_ids, world),
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


def commute_mode_question(*, has_car: bool, person_ref: str | None = None) -> dict:
    instructions = COMMUTE_MODE_INSTRUCTIONS
    if person_ref is not None:
        instructions = f"How will `{person_ref}` most likely commute to work now?"
    criteria = {m.value: None for m in CommuteMode if has_car or m is not CommuteMode.CAR}
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def shopping_place_question(*, person_ref: str | None = None) -> dict:
    instructions = SHOPPING_PLACE_INSTRUCTIONS
    if person_ref is not None:
        instructions = f"Where will `{person_ref}` most likely do their shopping this month?"
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": dict(SHOPPING_PLACE_CRITERIA),
    }


# --- landlord question ----------------------------------------------------------------------

LANDLORD_INSTRUCTIONS = "What will this landlord most likely do with the empty flat?"

LANDLORD_CRITERIA: dict[str, str] = {
    LandlordAction.RELET: (
        "Rent the flat again long-term to a new tenant, at market rent or the capped rent "
        "if one applies."
    ),
    LandlordAction.SELL: "Sell the flat to an owner-occupier, leaving the long-term rental market for good.",
    LandlordAction.SEASONAL: "Switch the flat to short-term/seasonal lets instead of a long-term tenant.",
    LandlordAction.RENOVATE: "Take the flat off the market for renovation, then relet it later.",
}


def landlord_question() -> dict:
    return {
        "type": "choice",
        "instructions": LANDLORD_INSTRUCTIONS,
        "criteria": dict(LANDLORD_CRITERIA),
    }


# --- mock priors --------------------------------------------------------------------------


def mock_priors_for_agent(
    agent: Agent,
    events: Iterable[Event],
    world: World,
    *,
    districts: list[DistrictId] | None = None,
) -> dict[str, dict[str, float]]:
    """Hand-written heuristics telling the MOCK backend how to weight random answers.

    Never sent to the real API. Kept as one readable function per question name.
    `districts` restricts the `destination` weights to this agent's relevant shortlist
    (+ leave_city); defaults to every district in the world when omitted (unit tests).
    """
    events = list(events)
    burden = agent.rent_burden

    # --- action ---
    action_w = {
        Action.STAY: 6.0,
        Action.MOVE: 0.05,
        Action.JOB_SEARCH: 0.2,
        Action.SPEND: 1.0,
        Action.SAVE: 1.0,
    }
    label = burden_label(burden)
    if label == "high":
        action_w[Action.MOVE] += 0.4
    elif label == "severe":
        action_w[Action.MOVE] += 0.9
    for ev in events:
        if ev.kind is EventKind.LEASE_RENEWAL:
            increase = float(ev.payload.get("increase_pct", 0.0))
            if increase > 0.08:
                action_w[Action.MOVE] += 1.5
        if ev.kind is EventKind.LIFE_EVENT and ev.payload.get("kind") in ("new_child", "partner"):
            action_w[Action.MOVE] += 0.8
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

    if agent.tenure is Tenure.OWNER:
        # Owners are much less likely to move (selling + buying/renting elsewhere is a
        # bigger step than a renter simply not renewing a lease); never affected by lease
        # renewals since they don't get LEASE_RENEWAL events in the first place.
        action_w[Action.MOVE] *= OWNER_MOVE_PRIOR_FACTOR

    # --- destination ---
    dest_districts = list(world.profiles) if districts is None else districts
    dest_w = _destination_weights(agent, world, dest_districts, label)

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
        "commute_mode": _commute_mode_weights(agent, world.tick),
        "shopping_place": _shopping_place_weights(),
    }


def _destination_weights(
    agent: Agent, world: World, districts: list[DistrictId], burden_lbl: str
) -> dict[str, float]:
    weights: dict[str, float] = {}
    income = agent.income_monthly
    for did in districts:
        state = world.states[did]
        pct = state.avg_rent / income if income > 0 else 9.99
        afford_factor = max(0.05, 1.0 - min(pct, 2.0) / 2.0)
        job_ratio = state.job_vacancies / state.jobs if state.jobs else 0.0
        job_factor = 1.0 + job_ratio
        w = afford_factor * job_factor
        if did == agent.home:
            w *= 1.2
        weights[did] = w
    leave_w = {"ok": 0.05, "stretched": 0.08, "high": 0.2, "severe": 0.4}[burden_lbl]
    weights[LEAVE_CITY] = leave_w
    return weights


def _commute_mode_weights(agent: Agent, tick: int) -> dict[str, float]:
    """Mock priors for the commute_mode question. Beyond the plain mode-popularity weights,
    a commuter's *current* mode gets extra "sticky" weight proportional to how long they've
    been doing it (Agent.commute_since_tick) -- people who've commuted the same way for years
    are less likely to switch than one about to try something new (see the metro-line launch
    realism fix in the final report: without this, mock runs over-predict mode switching)."""
    modes = [m for m in CommuteMode if agent.has_car or m is not CommuteMode.CAR]
    weights = {m.value: 1.0 for m in modes}
    weights[CommuteMode.METRO.value] = 3.0
    if agent.has_car:
        weights[CommuteMode.CAR.value] = 2.0
    if agent.commute_mode is not None and agent.commute_mode.value in weights:
        habit_years = 0.0
        if agent.commute_since_tick is not None:
            habit_years = max(tick - agent.commute_since_tick, 0) / TICKS_PER_YEAR
        stickiness = 1.0 + min(habit_years, COMMUTE_HABIT_MAX_YEARS) * COMMUTE_HABIT_STICKINESS_PER_YEAR
        weights[agent.commute_mode.value] *= stickiness
    return weights


def _shopping_place_weights() -> dict[str, float]:
    return {
        ShoppingPlace.LOCAL.value: 3.0,
        ShoppingPlace.WORK_DISTRICT.value: 1.0,
        ShoppingPlace.CENTRE.value: 1.0,
        ShoppingPlace.ONLINE.value: 1.0,
    }


def _triangular_weights(n: int, target: int) -> list[float]:
    target = max(0, min(n - 1, target))
    return [max(0.1, 1.0 - abs(i - target) * 0.4) for i in range(n)]


# --- landlord mock priors --------------------------------------------------------------------

LANDLORD_BASE_WEIGHTS: dict[LandlordAction, float] = {
    LandlordAction.RELET: 6.0,
    LandlordAction.SELL: 0.3,
    LandlordAction.SEASONAL: 0.3,
    LandlordAction.RENOVATE: 0.3,
}
LANDLORD_CAP_GAP_THRESHOLD = 0.10  # cap must bite by more than this share of market rent
LANDLORD_LOW_QUALITY_THRESHOLD = 0.5


def mock_priors_for_landlord(vacancy: Vacancy, world: World) -> dict[str, float]:
    """Heuristic mock-backend weights for the landlord_action question.

    Relet dominates by default (most Barcelona vacancies get relet). A rent cap that bites
    hard (gap to market > LANDLORD_CAP_GAP_THRESHOLD) shifts weight away from relet: small
    landlords lean toward selling (a single flat is easier to sell than to run as a seasonal
    let), large landlords lean toward seasonal (a portfolio can absorb the switch, and
    seasonal often escapes the cap -- see RentalSupplyParams.seasonal_capped). Poor flat
    condition (low DistrictState.quality) shifts weight toward renovate.
    """
    state = world.states[vacancy.district]
    market_rent = state.avg_rent
    w = dict(LANDLORD_BASE_WEIGHTS)
    if state.rent_cap is not None and market_rent > 0 and state.rent_cap < market_rent:
        gap = (market_rent - state.rent_cap) / market_rent
        if gap > LANDLORD_CAP_GAP_THRESHOLD:
            if vacancy.landlord_type is LandlordType.LARGE:
                w[LandlordAction.SEASONAL] += 2.5 * gap
                w[LandlordAction.SELL] += 1.0 * gap
            else:
                w[LandlordAction.SELL] += 2.5 * gap
                w[LandlordAction.SEASONAL] += 1.0 * gap
    if state.quality < LANDLORD_LOW_QUALITY_THRESHOLD:
        w[LandlordAction.RENOVATE] += (LANDLORD_LOW_QUALITY_THRESHOLD - state.quality) * 4.0
    return {k.value: v for k, v in w.items()}
