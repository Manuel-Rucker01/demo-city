"""Turn agents + events into Jev DecisionRequests. Owner: T4.

Rules from docs/jev-reference/model-jaggedness_jev-1.13.md: English text, semantic buckets
instead of raw numbers (code computes 'rent burden: severe'), only relevant fields,
direct literal questions, criteria aligned with instructions. Target <= ~300 state tokens
per agent (estimate tokens as len(json)/4).

Example K=1 state for one agent (real output of build_state_k1 against the 5 districts in
tests/conftest.py's `profiles` fixture). As compact JSON (the shape actually estimated /
sent) this is 1205 chars -> ~301 estimated tokens, right at the ~300-token target; per-district
facts are collapsed into `rent`/`jobs` (instead of 8 separate keys) specifically so all 5
districts fit inside that budget alongside the `person` block::

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
        {"name": "Ciutat Vella", "rent": "€1050/mo 48% strained, rising, normal 6%vacant",
         "jobs": "steady 11%unemp, commute long", "transit": "excellent", "you_live_here": false},
        {"name": "Eixample", "rent": "€1250/mo 57% unaffordable, rising, normal 4%vacant",
         "jobs": "hiring 7%unemp, commute short", "transit": "excellent", "you_live_here": false},
        {"name": "Gràcia", "rent": "€1150/mo 52% unaffordable, rising, scarce 3%vacant",
         "jobs": "hiring 7%unemp, commute same", "transit": "excellent", "you_live_here": true},
        {"name": "Nou Barris", "rent": "€800/mo 36% tight, rising, normal 5%vacant",
         "jobs": "steady 12%unemp, commute long", "transit": "good", "you_live_here": false},
        {"name": "Sant Martí", "rent": "€1100/mo 50% unaffordable, rising, normal 4%vacant",
         "jobs": "steady 9%unemp, commute long", "transit": "excellent", "you_live_here": false}
      ]
    }

And the matching (K=1, cacheable) `questions` map has 4 entries keyed
``question_key(agent_id, name)`` for ``action``/``destination``/``spending``/``satisfaction``;
see prompts/questions.py for the exact instructions/criteria text.
"""

from __future__ import annotations

from jevcity.prompts.buckets import (
    affordability_label,
    affordability_text,
    commute_text,
    job_market_text,
    monthly_expenses,
    rent_burden_text,
    rent_trend_text,
    satisfaction_bucket,
    savings_text,
    transit_text,
    vacancy_text,
    work_text,
    years_in_home_text,
)
from jevcity.prompts.questions import (
    action_question,
    destination_question,
    mock_priors_for_agent,
    satisfaction_question,
    spending_question,
)
from jevcity.types import (
    QUESTION_NAMES,
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
    return "Something happened today."


def _person_block(agent: Agent, world: World, tick: int) -> dict:
    home_profile = world.profiles[agent.home]
    return {
        "age": agent.age,
        "household": agent.household_size,
        "work": work_text(agent, world),
        "income": round(agent.income_monthly),
        "rent_burden": rent_burden_text(agent.rent_burden),
        "savings": savings_text(agent.savings, monthly_expenses(agent)),
        "home": home_profile.name,
        "years_in_current_home": years_in_home_text(tick, agent.lease_start_tick),
        "satisfaction": satisfaction_bucket(agent.satisfaction),
    }


def _district_block_generic(did: DistrictId, world: World) -> dict:
    """Shared, per-agent-independent district facts (K>1's `state.districts`).

    Rent/trend/availability collapse into one `rent` field and job-market facts into
    one `jobs` field (rather than 8 separate keys) to keep 5 districts' worth of
    context inside the ~300-token K=1 budget; see the module docstring's worked example.
    """
    profile = world.profiles[did]
    state = world.states[did]
    history = world.rent_history.get(did) if world.rent_history else None
    return {
        "name": profile.name,
        "rent": (
            f"€{round(state.avg_rent)}/mo new lease, {rent_trend_text(history)}, "
            f"{vacancy_text(state.vacancy_rate)}"
        ),
        "jobs": job_market_text(profile.unemployment_rate, state.job_vacancies, state.jobs),
        "transit": transit_text(profile.transit_score),
    }


def _district_block_for_agent(did: DistrictId, world: World, agent: Agent) -> dict:
    """Per-agent district facts (K=1's `state.districts`): adds this agent's own
    affordability, commute and whether they already live there."""
    profile = world.profiles[did]
    state = world.states[did]
    history = world.rent_history.get(did) if world.rent_history else None
    return {
        "name": profile.name,
        "rent": (
            f"{affordability_text(state.avg_rent, agent.income_monthly)}, "
            f"{rent_trend_text(history)}, {vacancy_text(state.vacancy_rate)}"
        ),
        "jobs": (
            f"{job_market_text(profile.unemployment_rate, state.job_vacancies, state.jobs)}, "
            f"commute {commute_text(agent, did, world)}"
        ),
        "transit": transit_text(profile.transit_score),
        "you_live_here": did == agent.home,
    }


def build_state_k1(world: World, agent: Agent, events: list[Event], tick: int) -> dict:
    """K=1 state shape: {"person", "today", "districts"}. All fields see docs/CONTRACTS.md."""
    return {
        "person": _person_block(agent, world, tick),
        "today": [_event_sentence(ev, world) for ev in events],
        "districts": [
            _district_block_for_agent(did, world, agent) for did in sorted(world.profiles)
        ],
    }


def _affordability_map(agent: Agent, world: World) -> dict[str, str]:
    return {
        did: affordability_label(world.states[did].avg_rent, agent.income_monthly)
        for did in sorted(world.profiles)
    }


def build_state_kn(
    world: World, agents: list[Agent], events_by_agent: dict[int, list[Event]], tick: int
) -> dict:
    """K>1 state shape: {"districts" (generic), "people": {"p<id>": {...}}}."""
    districts = [_district_block_generic(did, world) for did in sorted(world.profiles)]
    people = {}
    for agent in agents:
        people[f"p{agent.id}"] = {
            "person": _person_block(agent, world, tick),
            "today": [_event_sentence(ev, world) for ev in events_by_agent.get(agent.id, [])],
            "affordability": _affordability_map(agent, world),
        }
    return {"districts": districts, "people": people}


def _questions_for_agent(agent_id: int, world: World, *, k1: bool) -> dict[str, dict]:
    ref = None if k1 else f"people.p{agent_id}"
    builders = {
        "action": action_question(person_ref=ref),
        "destination": destination_question(world, person_ref=ref),
        "spending": spending_question(person_ref=ref),
        "satisfaction": satisfaction_question(person_ref=ref),
    }
    return {question_key(agent_id, name): q for name, q in builders.items()}


def build_requests(
    world: World,
    agents: dict[int, Agent],
    events: list[Event],
    tick: int,
    agents_per_request: int = 1,
) -> list[DecisionRequest]:
    """One DecisionRequest per group of K agents with events. Each agent gets the four
    QUESTION_NAMES questions keyed with question_key(agent_id, name). Fills mock_priors."""
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

        if k1:
            (only_id,) = chunk_ids
            state = build_state_k1(world, agents[only_id], events_by_agent[only_id], tick)
        else:
            state = build_state_kn(world, chunk_agents, events_by_agent, tick)

        questions: dict[str, dict] = {}
        mock_priors: dict[str, dict[str, float]] = {}
        for aid in chunk_ids:
            questions.update(_questions_for_agent(aid, world, k1=k1))
            priors = mock_priors_for_agent(agents[aid], events_by_agent[aid], world)
            for name in QUESTION_NAMES:
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
