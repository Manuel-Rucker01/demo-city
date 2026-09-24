"""Event detection: which agents must decide this tick. Owner: T5.

Draws are vectorized (one rng call per event type, across all agents in agent-id order)
for speed and determinism; the resulting per-agent state changes are then applied in a
plain loop over the (typically small) set of triggered agents.

- PAYDAY: deterministic stagger, no rng draw - agent fires when (tick + agent.id) % 30 == 0.
- LEASE_RENEWAL: deterministic (lease_length_ticks cadence). new_rent computed and applied
  here; see world/market.py module docstring for the exact formula (the same one, since a
  renewal is really a market event) - duplicated in miniature below to avoid a cross-import
  cycle between events/ and world/. Owners never fire LEASE_RENEWAL: their housing cost
  (mortgage/fees) is fixed and unaffected by market rent or rent caps (see Agent.tenure).
- JOB_LOSS: employed, working-age agents, w.p. job_loss_daily_prob.
- JOB_OFFER: unemployed, working-age agents, w.p. job_offer_daily_prob * availability, where
  availability = min(1, vacancies_in_offer_district / JOB_OFFER_VACANCY_SCALE). The offer
  targets the home district if it has vacancies, else the district with the most vacancies.
  This does not employ the agent - only a later job_search decision does.
- RENT_BURDEN: fires the tick the agent's rent_burden first crosses rent_burden_threshold
  from below. Previous-tick burden is cached on the World instance itself
  (world._prev_rent_burden, a plain dict[agent_id, float]) since neither Agent nor World
  carry a field for it in types.py; see the final report for this contract note.
- LIFE_EVENT: w.p. life_event_daily_prob, kind uniformly sampled from LIFE_EVENT_KINDS.
"""

from __future__ import annotations

import numpy as np

from jevcity.types import Agent, Event, EventKind, EventParams, Tenure, World
from jevcity.world._helpers import clip, is_working_age

JOB_OFFER_VACANCY_SCALE = 10.0
RENEWAL_INCREASE_CAP_DEFAULT = 0.10  # kept in sync with world/market.py's constant of the same name
LIFE_EVENT_KINDS = ("new_child", "partner", "health", "inheritance")


def detect_events(
    world: World,
    agents: dict[int, Agent],
    tick: int,
    params: EventParams,
    rng: np.random.Generator,
) -> list[Event]:
    """Return this tick's events (an agent may have several). Only agents with events decide.

    Also performs the event's own state change when it is not a decision (e.g. JOB_LOSS sets
    employed=False; LEASE_RENEWAL computes new rent from market rent and caps, applied here).
    PAYDAY is staggered: agent fires when (tick + agent.id) % 30 == 0.
    """
    events: list[Event] = []
    agent_ids = sorted(agents.keys())
    n = len(agent_ids)
    if n == 0:
        return events

    ids_arr = np.array(agent_ids, dtype=np.int64)
    payday_mask = (tick + ids_arr) % 30 == 0

    job_loss_draws = rng.random(n)
    job_offer_draws = rng.random(n)
    life_event_draws = rng.random(n)
    life_event_kind_draws = rng.random(n)

    prev_burden: dict[int, float] | None = getattr(world, "_prev_rent_burden", None)
    if prev_burden is None:
        prev_burden = {}
        world._prev_rent_burden = prev_burden  # type: ignore[attr-defined]

    for idx, aid in enumerate(agent_ids):
        agent = agents[aid]

        if payday_mask[idx]:
            events.append(Event(agent_id=aid, kind=EventKind.PAYDAY))

        if (
            agent.tenure is not Tenure.OWNER
            and tick > agent.lease_start_tick
            and (tick - agent.lease_start_tick) % params.lease_length_ticks == 0
        ):
            state = world.states.get(agent.home)
            if state is not None:
                cap = state.max_increase_pct if state.max_increase_pct is not None else RENEWAL_INCREASE_CAP_DEFAULT
                if state.avg_rent > 0:
                    relative_position = clip(agent.rent_monthly / state.avg_rent, 0.7, 1.4)
                else:
                    relative_position = 1.0
                target = max(agent.rent_monthly, state.avg_rent * relative_position)
                new_rent = min(target, agent.rent_monthly * (1 + cap))
                if state.rent_cap is not None:
                    new_rent = min(new_rent, state.rent_cap)
                new_rent = max(new_rent, 0.0)
                increase_pct = (
                    (new_rent - agent.rent_monthly) / agent.rent_monthly if agent.rent_monthly > 0 else 0.0
                )
                agent.rent_monthly = new_rent
                agent.lease_start_tick = tick
                events.append(
                    Event(
                        agent_id=aid,
                        kind=EventKind.LEASE_RENEWAL,
                        payload={"new_rent": new_rent, "increase_pct": increase_pct},
                    )
                )

        working_age = is_working_age(agent.occupation)

        if agent.employed and working_age:
            if job_loss_draws[idx] < params.job_loss_daily_prob:
                lost_district = agent.job_district
                if lost_district is not None:
                    state = world.states.get(lost_district)
                    if state is not None:
                        state.filled_jobs = max(state.filled_jobs - 1, 0)
                agent.employed = False
                agent.job_district = None
                agent.days_unemployed = 0
                events.append(Event(agent_id=aid, kind=EventKind.JOB_LOSS))
        elif (not agent.employed) and working_age:
            home_state = world.states.get(agent.home)
            offer_state = None
            if home_state is not None and home_state.job_vacancies > 0:
                offer_state = home_state
            else:
                best = None
                best_vac = 0
                for s in world.states.values():
                    if s.job_vacancies > best_vac:
                        best = s
                        best_vac = s.job_vacancies
                offer_state = best
            if offer_state is not None:
                availability = min(1.0, offer_state.job_vacancies / JOB_OFFER_VACANCY_SCALE)
                prob = params.job_offer_daily_prob * availability
                if job_offer_draws[idx] < prob:
                    events.append(
                        Event(
                            agent_id=aid,
                            kind=EventKind.JOB_OFFER,
                            payload={"district": offer_state.id, "wage": agent.wage_monthly},
                        )
                    )

        burden = agent.rent_burden
        prev = prev_burden.get(aid)
        if prev is not None and prev < params.rent_burden_threshold <= burden:
            events.append(Event(agent_id=aid, kind=EventKind.RENT_BURDEN, payload={"burden": burden}))
        prev_burden[aid] = burden

        if life_event_draws[idx] < params.life_event_daily_prob:
            kind_idx = min(int(life_event_kind_draws[idx] * len(LIFE_EVENT_KINDS)), len(LIFE_EVENT_KINDS) - 1)
            events.append(
                Event(
                    agent_id=aid,
                    kind=EventKind.LIFE_EVENT,
                    payload={"kind": LIFE_EVENT_KINDS[kind_idx]},
                )
            )

    return events
