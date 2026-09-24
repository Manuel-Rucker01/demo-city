"""Turn agents + events into Jev DecisionRequests. Owner: T4.

Rules from docs/jev-reference/model-jaggedness_jev-1.13.md: English text, semantic buckets
instead of raw numbers (code computes 'rent burden: severe'), only relevant fields,
direct literal questions, criteria aligned with instructions.

Token budget (enforced by tests): average <= TOKEN_BUDGET_AVG (1,400), max <= TOKEN_BUDGET_MAX
(1,900) real tokens per K=1 request (real tokens ~= len(canonical json)/2.3, see jev/meter.py).
With up to 10 districts in the world, two ways keep requests inside budget:

1. Relevant districts only (`_select_relevant_districts`): each agent's `districts` list
   (and the `destination` question's options) contains at most `max_districts_in_state`
   entries -- home, job district (if employed elsewhere), then the highest-relevance
   alternatives, ranked simply by: affordable for this agent (rent/income), short commute to
   their job if employed, and weighted more toward affordability for households with children
   (stability matters more than novelty for families). `destination`'s options are exactly
   those districts plus LEAVE_CITY.
2. Conditional questions (`wants_commute_question` / `wants_shopping_question`): commute_mode
   and shopping_place are only built (and only get a mock_priors entry) when an event makes
   them relevant -- see their docstrings for the exact trigger rules.

Example K=1 state for one agent (5-district world), compacted to show the shape sent to Jev::

    {
      "person": {
        "age": 34, "household": 2,
        "work": "employed as mid-skill worker in Gràcia",
        "income": 2200, "rent_burden": "severe: rent takes 52% of income",
        "savings": "thin: 1.3 months of expenses saved",
        "home": "Gràcia", "years_in_current_home": "4 years",
        "satisfaction": "unhappy"
      },
      "today": ["Your landlord says the rent will rise 12% at renewal next month."],
      "districts": [
        {"name": "Gràcia", "rent": "€1150/mo 52% unaffordable, rising, scarce 3%vacant",
         "jobs": "hiring 7%unemp, commute same", "transit": "excellent", "you_live_here": true},
        {"name": "Nou Barris", "rent": "€800/mo 36% tight, rising, normal 5%vacant",
         "jobs": "steady 12%unemp, commute long", "transit": "good", "you_live_here": false},
        ...
      ]
    }

See prompts/questions.py for the exact `action`/`destination`/`spending`/`satisfaction`/
`commute_mode`/`shopping_place` instructions/criteria text.
"""

from __future__ import annotations

from jevcity.prompts.buckets import (
    affordability_label,
    affordability_text,
    commute_text,
    housing_text,
    job_market_text,
    lez_note_text,
    monthly_expenses,
    own_commute_length_text,
    rent_trend_text,
    satisfaction_bucket,
    savings_text,
    shops_trend_label,
    tourism_label,
    transit_bucket_text,
    vacancy_text,
    work_text,
    years_in_home_text,
)
from jevcity.prompts.questions import (
    action_question,
    commute_mode_question,
    destination_question,
    mock_priors_for_agent,
    satisfaction_question,
    shopping_place_question,
    spending_question,
)
from jevcity.types import (
    Agent,
    DecisionRequest,
    DistrictId,
    Event,
    EventKind,
    World,
    question_key,
)

_LIFE_EVENT_TEXT = {
    "new_child": "You are expecting a new child.",
    "partner": "You've moved in with a partner.",
    "health": "You are dealing with a health issue.",
    "inheritance": "You received an inheritance.",
}

# --- conditional questions: exact trigger rules --------------------------------------------

# commute_mode: only asked when the person is employed AND today's events make commuting
# relevant -- a new/changed job, a transit policy change, or a plausible reason to move
# (which could change their commute).
_COMMUTE_RELEVANT_KINDS = frozenset(
    {
        EventKind.JOB_OFFER,
        EventKind.TRANSIT_CHANGE,
        EventKind.LEASE_RENEWAL,
        EventKind.RENT_BURDEN,
        EventKind.TOURISM_PRESSURE,
        EventKind.ARRIVED,
    }
)

# shopping_place: asked on a shop-closure, an arrival, a life event, or (deterministically,
# once every ~90 days per agent) on payday -- so it's revisited periodically without asking
# every single month.
_SHOPPING_RELEVANT_KINDS = frozenset(
    {EventKind.SHOP_CLOSED, EventKind.ARRIVED, EventKind.LIFE_EVENT}
)
_SHOPPING_PAYDAY_INTERVAL_TICKS = 90


def wants_commute_question(agent: Agent, events: list[Event]) -> bool:
    if not agent.employed:
        return False
    return any(ev.kind in _COMMUTE_RELEVANT_KINDS for ev in events)


def wants_shopping_question(agent: Agent, events: list[Event], tick: int) -> bool:
    if any(ev.kind in _SHOPPING_RELEVANT_KINDS for ev in events):
        return True
    if any(ev.kind is EventKind.PAYDAY for ev in events):
        # Deterministic by agent id and tick: exactly 1 in every 3 monthly paydays (paydays
        # are staggered by agent id, 30 ticks apart -> this fires once per 90 ticks).
        return tick % _SHOPPING_PAYDAY_INTERVAL_TICKS == agent.id % _SHOPPING_PAYDAY_INTERVAL_TICKS
    return False


def _select_relevant_districts(
    agent: Agent, world: World, max_districts: int
) -> list[DistrictId]:
    """Home + job district (if employed elsewhere) are always included; remaining slots go
    to the most relevant alternatives, ranked by: affordable for this agent (rent/income),
    short commute to their job if employed, weighted more toward affordability for households
    with children (stability matters more than novelty for families). Deliberately simple --
    see PromptParams.max_districts_in_state in types.py."""
    selected: list[DistrictId] = [agent.home]
    if agent.employed and agent.job_district and agent.job_district != agent.home:
        selected.append(agent.job_district)
    remaining = max_districts - len(selected)
    if remaining <= 0:
        return selected[:max_districts]

    income = agent.income_monthly
    commute_bonus = {"same": 1.2, "short": 1.1, "n.a.": 1.0}

    def relevance(did: DistrictId) -> float:
        state = world.states[did]
        pct = state.avg_rent / income if income > 0 else 9.99
        afford = max(0.05, 1.0 - min(pct, 2.0) / 2.0)
        if agent.children > 0:
            afford *= afford  # families weight affordability more heavily
        return afford * commute_bonus.get(commute_text(agent, did, world), 0.85)

    candidates = sorted(d for d in world.profiles if d not in selected)
    candidates.sort(key=relevance, reverse=True)
    selected.extend(candidates[:remaining])
    return selected


def _event_sentence(event: Event, world: World) -> str:
    p = event.payload
    if event.kind is EventKind.LEASE_RENEWAL:
        pct = round(float(p.get("increase_pct", 0.0)) * 100)
        if pct > 0:
            return f"Your landlord says the rent will rise {pct}% at renewal next month."
        if pct < 0:
            return f"Your landlord says the rent will fall {abs(pct)}% at renewal next month."
        return "Your lease is up for renewal next month with no change in rent."
    if event.kind is EventKind.JOB_LOSS:
        return "You lost your job today."
    if event.kind is EventKind.JOB_OFFER:
        district = str(p.get("district", ""))
        name = world.profiles[district].name if district in world.profiles else district
        wage = round(float(p.get("wage", 0.0)))
        return f"You received a job offer in {name} paying €{wage}/month."
    if event.kind is EventKind.PAYDAY:
        return "Today is payday."
    if event.kind is EventKind.RENT_BURDEN:
        burden = round(float(p.get("burden", 0.0)) * 100)
        return f"Your rent now takes {burden}% of your income."
    if event.kind is EventKind.LIFE_EVENT:
        kind = str(p.get("kind", ""))
        return _LIFE_EVENT_TEXT.get(kind, "Something changed in your life today.")
    if event.kind is EventKind.SCHOOL_YEAR:
        return "The school year is starting for your children."
    if event.kind is EventKind.TRANSIT_CHANGE:
        kind = str(p.get("kind", ""))
        if kind == "lez":
            return "A low-emission zone now applies near you."
        return "A new metro line opened near you."
    if event.kind is EventKind.SHOP_CLOSED:
        pct = round(float(p.get("closed_pct", 0.0)) * 100)
        return f"About {pct}% of local shops closed this month."
    if event.kind is EventKind.TOURISM_PRESSURE:
        return "Tourist flats are increasing in your neighbourhood."
    if event.kind is EventKind.ARRIVED:
        return "You just moved into Barcelona."
    return "Something happened today."


def _person_block(agent: Agent, world: World, tick: int) -> dict:
    home_profile = world.profiles[agent.home]
    block = {
        "age": agent.age,
        "household": agent.household_size,
        "work": work_text(agent, world),
        "income": round(agent.income_monthly),
        "rent_burden": housing_text(agent),
        "savings": savings_text(agent.savings, monthly_expenses(agent)),
        "home": home_profile.name,
        "years_in_current_home": years_in_home_text(tick, agent.lease_start_tick),
        "satisfaction": satisfaction_bucket(agent.satisfaction),
    }
    if agent.children > 0:
        block["kids"] = agent.children
    if agent.has_car:
        block["car"] = "yes"
    if agent.commute_mode is not None:
        block["commute"] = f"{agent.commute_mode.value} {own_commute_length_text(agent, world)}"
    return block


def _transit_field(did: DistrictId, world: World, *, has_car: bool) -> str:
    profile = world.profiles[did]
    state = world.states[did]
    text = transit_bucket_text(profile.transit_score, state.transit_boost)
    if state.low_emission_zone and has_car:
        text += "; " + lez_note_text(state.car_cost_extra_monthly)
    return text


def _district_block_generic(did: DistrictId, world: World) -> dict:
    """Shared, per-agent-independent district facts (K>1's `state.districts`).

    Rent/trend/availability collapse into one `rent` field and job-market facts into
    one `jobs` field (rather than 8 separate keys) to keep the state compact; see the
    module docstring's worked example and `_select_relevant_districts`.
    """
    profile = world.profiles[did]
    state = world.states[did]
    history = world.rent_history.get(did) if world.rent_history else None
    block = {
        "name": profile.name,
        "rent": (
            f"€{round(state.avg_rent)}/mo new lease, {rent_trend_text(history)}, "
            f"{vacancy_text(state.vacancy_rate)}"
        ),
        "jobs": job_market_text(profile.unemployment_rate, state.job_vacancies, state.jobs),
        "transit": transit_bucket_text(profile.transit_score, state.transit_boost),
    }
    if state.low_emission_zone:
        block["transit"] += "; " + lez_note_text(state.car_cost_extra_monthly)
    tourism = tourism_label(state.tourist_units, state.housing_units)
    if tourism != "none":
        block["tourism"] = tourism
    shops = shops_trend_label(state.shops_open, profile.shops)
    if shops != "stable":
        block["shops"] = shops
    return block


def _district_block_for_agent(did: DistrictId, world: World, agent: Agent) -> dict:
    """Per-agent district facts (K=1's `state.districts`): adds this agent's own
    affordability, commute and whether they already live there."""
    profile = world.profiles[did]
    state = world.states[did]
    history = world.rent_history.get(did) if world.rent_history else None
    block = {
        "name": profile.name,
        "rent": (
            f"{affordability_text(state.avg_rent, agent.income_monthly)}, "
            f"{rent_trend_text(history)}, {vacancy_text(state.vacancy_rate)}"
        ),
        "jobs": (
            f"{job_market_text(profile.unemployment_rate, state.job_vacancies, state.jobs)}, "
            f"commute {commute_text(agent, did, world)}"
        ),
        "transit": _transit_field(did, world, has_car=agent.has_car),
        "you_live_here": did == agent.home,
    }
    tourism = tourism_label(state.tourist_units, state.housing_units)
    if tourism != "none":
        block["tourism"] = tourism
    shops = shops_trend_label(state.shops_open, profile.shops)
    if shops != "stable":
        block["shops"] = shops
    return block


def build_state_k1(
    world: World, agent: Agent, events: list[Event], tick: int, district_ids: list[DistrictId]
) -> dict:
    """K=1 state shape: {"person", "today", "districts"}. All fields see docs/CONTRACTS.md."""
    return {
        "person": _person_block(agent, world, tick),
        "today": [_event_sentence(ev, world) for ev in events],
        "districts": [_district_block_for_agent(did, world, agent) for did in district_ids],
    }


def _affordability_map(agent: Agent, world: World, district_ids: list[DistrictId]) -> dict[str, str]:
    return {
        did: affordability_label(world.states[did].avg_rent, agent.income_monthly)
        for did in district_ids
    }


def build_state_kn(
    world: World,
    agents: list[Agent],
    events_by_agent: dict[int, list[Event]],
    tick: int,
    per_agent_districts: dict[int, list[DistrictId]],
) -> dict:
    """K>1 state shape: {"districts" (generic, union of every packed agent's relevant
    shortlist), "people": {"p<id>": {...}}}."""
    union_ids = sorted({did for ids in per_agent_districts.values() for did in ids})
    districts = [_district_block_generic(did, world) for did in union_ids]
    people = {}
    for agent in agents:
        rel = per_agent_districts[agent.id]
        people[f"p{agent.id}"] = {
            "person": _person_block(agent, world, tick),
            "today": [_event_sentence(ev, world) for ev in events_by_agent.get(agent.id, [])],
            "affordability": _affordability_map(agent, world, rel),
        }
    return {"districts": districts, "people": people}


def _questions_for_agent(
    agent: Agent,
    events: list[Event],
    world: World,
    district_ids: list[DistrictId],
    tick: int,
    *,
    k1: bool,
) -> dict[str, dict]:
    ref = None if k1 else f"people.p{agent.id}"
    builders = {
        "action": action_question(person_ref=ref),
        "destination": destination_question(district_ids, world, person_ref=ref),
        "spending": spending_question(person_ref=ref),
        "satisfaction": satisfaction_question(person_ref=ref),
    }
    if wants_commute_question(agent, events):
        builders["commute_mode"] = commute_mode_question(has_car=agent.has_car, person_ref=ref)
    if wants_shopping_question(agent, events, tick):
        builders["shopping_place"] = shopping_place_question(person_ref=ref)
    return builders


def build_requests(
    world: World,
    agents: dict[int, Agent],
    events: list[Event],
    tick: int,
    agents_per_request: int = 1,
    max_districts_in_state: int = 5,
) -> list[DecisionRequest]:
    """One DecisionRequest per group of K agents with events. Each agent gets action/
    destination/spending/satisfaction plus commute_mode/shopping_place when relevant (see
    wants_commute_question/wants_shopping_question), keyed with question_key(agent_id, name).
    Fills mock_priors for exactly the questions asked."""
    events_by_agent: dict[int, list[Event]] = {}
    for ev in events:
        if ev.agent_id not in agents:
            continue
        events_by_agent.setdefault(ev.agent_id, []).append(ev)

    agent_ids = sorted(events_by_agent)
    k = max(agents_per_request, 1)
    k1 = k == 1

    reqs: list[DecisionRequest] = []
    for n, start in enumerate(range(0, len(agent_ids), k)):
        chunk_ids = agent_ids[start : start + k]
        chunk_agents = [agents[aid] for aid in chunk_ids]
        per_agent_districts = {
            aid: _select_relevant_districts(agents[aid], world, max_districts_in_state)
            for aid in chunk_ids
        }

        if k1:
            (only_id,) = chunk_ids
            state = build_state_k1(
                world, agents[only_id], events_by_agent[only_id], tick, per_agent_districts[only_id]
            )
        else:
            state = build_state_kn(world, chunk_agents, events_by_agent, tick, per_agent_districts)

        questions: dict[str, dict] = {}
        mock_priors: dict[str, dict[str, float]] = {}
        for aid in chunk_ids:
            agent = agents[aid]
            agent_events = events_by_agent[aid]
            agent_qs = _questions_for_agent(
                agent, agent_events, world, per_agent_districts[aid], tick, k1=k1
            )
            for name, q in agent_qs.items():
                questions[question_key(aid, name)] = q
            priors = mock_priors_for_agent(
                agent, agent_events, world, districts=per_agent_districts[aid]
            )
            for name in agent_qs:
                mock_priors[question_key(aid, name)] = priors[name]

        reqs.append(
            DecisionRequest(
                request_id=f"t{tick}-r{n}",
                tick=tick,
                agent_ids=chunk_ids,
                state=state,
                questions=questions,
                mock_priors=mock_priors,
            )
        )
    return reqs
