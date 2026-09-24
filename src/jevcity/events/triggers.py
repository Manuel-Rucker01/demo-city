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
- Inactive agents (`Agent.active is False`, i.e. households that have left the city) are
  skipped entirely: no events, no state mutation, no rng draws consumed for them.
- SCHOOL_YEAR: fires for every agent with `children > 0` on `params.school_year_tick` and
  every 365 ticks after (`(tick - school_year_tick) % 365 == 0`, `tick >= school_year_tick`).
  No rng draw (deterministic like PAYDAY/LEASE_RENEWAL).
- TOURISM_PRESSURE: renters only, w.p. `params.tourism_pressure_daily_prob * tourist_share`
  where `tourist_share = state.tourist_units / state.housing_units` (0 if no housing) of the
  agent's home district; payload `tourist_share`.
- TRANSIT_CHANGE: `DecisionRequest`/`EventParams` carry no policy info, so this is detected
  from `DistrictState` changes rather than from `scenario.policies` directly (see
  docs/CONTRACTS.md - policies are applied to `DistrictState` by `world/market.py`'s
  `apply_policies`, upstream of `detect_events` in the tick pipeline). Two per-world caches on
  the World instance (`world._prev_transit_boost`, `world._prev_low_emission_zone`, both plain
  dict[DistrictId, ...], lazily seeded on first call without firing - same pattern as
  `_prev_rent_burden`) hold the previous tick's values per district. A district's
  `transit_boost` increasing fires `kind="new_line"`; `low_emission_zone` flipping
  False->True fires `kind="low_emission_zone"`. **Awareness is spread, not instant**: instead
  of firing for every affected agent the tick the district state changes (unrealistic - "the
  whole neighbourhood notices a new metro line on opening day"), each affected agent is
  scheduled to notice on one deterministic pseudo-random day in
  `[tick, tick + params.transit_awareness_days)`, seeded by `(agent_id, kind, tick)` (see
  `_awareness_offset`) so it's reproducible without consuming the shared rng. Scheduled
  (agent_id, kind) pairs live in `world._pending_transit_awareness`
  (dict[int tick -> list[(agent_id, kind)]]), popped and fired on the day they're due. Each
  affected agent is scheduled (and later fires) exactly once per district-level change.
- SHOP_CLOSED: another World-cached dict, `world._prev_shops_open` (per district, same lazy
  seeding), detects `state.shops_open` dropping tick over tick (shops only actually change on
  month boundaries in `world/market.py`, so this only ever fires around those boundaries).
  Fires w.p. 0.3 for residents (`agent.home`) of a district whose `shops_open` dropped;
  payload `closed_pct = (prev - cur) / prev`.
- ARRIVED is emitted by the engine when it calls `population.generator.spawn_arrivals`, not
  by this module.
"""

from __future__ import annotations

import hashlib

import numpy as np

from jevcity.types import Agent, DistrictId, Event, EventKind, EventParams, Tenure, World
from jevcity.world._helpers import clip, is_working_age

JOB_OFFER_VACANCY_SCALE = 10.0
RENEWAL_INCREASE_CAP_DEFAULT = 0.10  # kept in sync with world/market.py's constant of the same name
LIFE_EVENT_KINDS = ("new_child", "partner", "health", "inheritance")
SCHOOL_YEAR_CYCLE_TICKS = 365
SHOP_CLOSED_FIRE_PROB = 0.3  # w.p. a resident of a district with fewer shops_open gets the event


def _awareness_offset(agent_id: int, kind: str, start_tick: int, awareness_days: int) -> int:
    """Deterministic pseudo-random day offset in [0, awareness_days) for when `agent_id`
    notices a `kind` transit/LEZ change that started at `start_tick`. Seeded by the inputs
    (not the shared rng) so it's reproducible without consuming a draw from it and without
    needing the scenario seed here."""
    if awareness_days <= 1:
        return 0
    digest = hashlib.sha256(f"{agent_id}:{kind}:{start_tick}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % awareness_days


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
    agent_ids = sorted(aid for aid, a in agents.items() if a.active)
    n = len(agent_ids)
    if n == 0:
        return events

    ids_arr = np.array(agent_ids, dtype=np.int64)
    payday_mask = (tick + ids_arr) % 30 == 0
    school_year_mask = (
        tick >= params.school_year_tick
        and (tick - params.school_year_tick) % SCHOOL_YEAR_CYCLE_TICKS == 0
    )

    job_loss_draws = rng.random(n)
    job_offer_draws = rng.random(n)
    life_event_draws = rng.random(n)
    life_event_kind_draws = rng.random(n)
    tourism_draws = rng.random(n)
    shop_closed_draws = rng.random(n)

    prev_burden: dict[int, float] | None = getattr(world, "_prev_rent_burden", None)
    if prev_burden is None:
        prev_burden = {}
        world._prev_rent_burden = prev_burden  # type: ignore[attr-defined]

    # --- district-level state-change caches (TRANSIT_CHANGE / SHOP_CLOSED) -----------------
    prev_transit_boost: dict[DistrictId, float] | None = getattr(world, "_prev_transit_boost", None)
    first_transit_seen = prev_transit_boost is None
    if prev_transit_boost is None:
        prev_transit_boost = {}
        world._prev_transit_boost = prev_transit_boost  # type: ignore[attr-defined]

    prev_lez: dict[DistrictId, bool] | None = getattr(world, "_prev_low_emission_zone", None)
    first_lez_seen = prev_lez is None
    if prev_lez is None:
        prev_lez = {}
        world._prev_low_emission_zone = prev_lez  # type: ignore[attr-defined]

    prev_shops_open: dict[DistrictId, int] | None = getattr(world, "_prev_shops_open", None)
    first_shops_seen = prev_shops_open is None
    if prev_shops_open is None:
        prev_shops_open = {}
        world._prev_shops_open = prev_shops_open  # type: ignore[attr-defined]

    pending_transit: dict[int, list[tuple[int, str]]] | None = getattr(
        world, "_pending_transit_awareness", None
    )
    if pending_transit is None:
        pending_transit = {}
        world._pending_transit_awareness = pending_transit  # type: ignore[attr-defined]
    due_today: dict[int, list[str]] = {}
    for aid, kind in pending_transit.pop(tick, []):
        due_today.setdefault(aid, []).append(kind)

    transit_changed: set[DistrictId] = set()
    lez_started: set[DistrictId] = set()
    shops_closed_pct: dict[DistrictId, float] = {}
    for did, state in world.states.items():
        if not first_transit_seen and state.transit_boost > prev_transit_boost.get(did, state.transit_boost):
            transit_changed.add(did)
        prev_transit_boost[did] = state.transit_boost

        if not first_lez_seen and state.low_emission_zone and not prev_lez.get(did, False):
            lez_started.add(did)
        prev_lez[did] = state.low_emission_zone

        prev_open = prev_shops_open.get(did, state.shops_open)
        if not first_shops_seen and state.shops_open < prev_open and prev_open > 0:
            shops_closed_pct[did] = (prev_open - state.shops_open) / prev_open
        prev_shops_open[did] = state.shops_open

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

        if school_year_mask and agent.children > 0:
            events.append(Event(agent_id=aid, kind=EventKind.SCHOOL_YEAR))

        if agent.tenure is not Tenure.OWNER:
            home_state = world.states.get(agent.home)
            if home_state is not None and home_state.housing_units > 0:
                tourist_share = home_state.tourist_units / home_state.housing_units
                if tourist_share > 0 and tourism_draws[idx] < params.tourism_pressure_daily_prob * tourist_share:
                    events.append(
                        Event(
                            agent_id=aid,
                            kind=EventKind.TOURISM_PRESSURE,
                            payload={"tourist_share": tourist_share},
                        )
                    )

        # Fire today's due (previously scheduled) transit-awareness events for this agent.
        for kind in due_today.get(aid, ()):
            events.append(Event(agent_id=aid, kind=EventKind.TRANSIT_CHANGE, payload={"kind": kind}))

        # Schedule awareness for a district-level change detected this tick, spread over
        # params.transit_awareness_days (see module docstring / _awareness_offset). An offset
        # of 0 means "notices today" -- fire it directly, since `pending_transit[tick]` was
        # already popped above and would otherwise never be revisited.
        if transit_changed or lez_started:
            agent_districts = {d for d in (agent.home, agent.job_district) if d is not None}
            for kind, changed in (("new_line", transit_changed), ("low_emission_zone", lez_started)):
                if agent_districts & changed:
                    offset = _awareness_offset(aid, kind, tick, params.transit_awareness_days)
                    if offset == 0:
                        events.append(Event(agent_id=aid, kind=EventKind.TRANSIT_CHANGE, payload={"kind": kind}))
                    else:
                        pending_transit.setdefault(tick + offset, []).append((aid, kind))

        closed_pct = shops_closed_pct.get(agent.home)
        if closed_pct is not None and shop_closed_draws[idx] < SHOP_CLOSED_FIRE_PROB:
            events.append(
                Event(agent_id=aid, kind=EventKind.SHOP_CLOSED, payload={"closed_pct": closed_pct})
            )

    return events
