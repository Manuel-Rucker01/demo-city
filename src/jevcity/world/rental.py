"""Rental-market supply dynamics: landlord decisions, construction, quality, seasonal lets.
Owner: T5. All of this is inert (fields populated, never mutated by behaviour) unless
`scenario.rental_supply.enabled` is True -- see world/market.py's module docstring and
docs/CONTRACTS.md for how this plugs into the tick pipeline.

Stock model (agent units, same scale as DistrictState.housing_units):

    housing_units = owner_units + rental_units + seasonal_units + tourist_units

`rental_units` and `owner_units` are each split into an occupied part (tracked per-district on
`world._renter_occupied` / `world._owner_occupied`, private bookkeeping dicts, not part of the
typed contract) and a vacant, immediately-take-able part (`world._vacant_rental` /
`world._vacant_owner`). A rental unit a renter just vacated is neither: it sits in
`world._pending_vacancies` (vacancy_id -> district) until `apply_landlord_decisions` resolves
it, at which point it becomes vacant rental (RELET), vacant owner-for-sale (SELL), seasonal
(SEASONAL, leaves the rental pool entirely) or `renovating_units` (RENOVATE, off-market for
`renovation_ticks` then vacant rental again, with a quality bump). Missing decisions (Jev
didn't answer, or the engine dropped the request) default to RELET -- landlords who don't
respond are modelled as doing the ordinary thing, not disappearing.

Formulas:

- `split_housing_stock`: at `init_world`, the non-tourist residential stock
  (`residential_base = owner_units + rental_units + seasonal_units`, matching
  world/market.py's `residential_base`) is split using each district's actual owner/renter
  occupancy (`owner_occ`, `renter_occ`, from the initial population): the vacant share of the
  stock (`residential_base - owner_occ - renter_occ`) is split between the two tenures in the
  same proportion as occupancy (`owner_occ / (owner_occ + renter_occ)`), falling back to
  `profile.owner_share` (or `DEFAULT_OWNER_SHARE_FALLBACK`) when there are no residents yet
  (e.g. an empty test district). `seasonal_units` then starts at `SEASONAL_INITIAL_SHARE`
  (1%) of the resulting rental stock -- a plausible small pre-existing "lloguer de temporada"
  share, PLAUSIBLE per types.Source -- carved out of the *vacant* rental share only (occupied
  renter units are never reclassified at init), capped so it can't exceed that vacant share.
  This keeps `owner_units + rental_units + seasonal_units == residential_base` exactly, so
  `housing_units == owner_units + rental_units + seasonal_units + tourist_units` holds from
  tick 0 (the invariant the tests check every tick).
- `apply_landlord_decisions` / one decision: RELET adds the unit back to
  `world._vacant_rental` (rental_units unchanged, it was never removed from the pool). SELL
  moves it rental_units -> owner_units and into `world._vacant_owner` (available to the next
  owner-occupier arrival immediately, no separate "for sale" delay is modelled). SEASONAL
  moves it rental_units -> seasonal_units (leaves the long-term pool; see
  `process_seasonal_return` for the way back). RENOVATE moves it into `renovating_units`
  (still counted inside `rental_units`, per DistrictState's docstring) and queues it on
  `world._renovation_pipeline[district]` as `(tick + renovation_ticks, 1)`; `process_renovations`
  returns it to `world._vacant_rental` once its tick arrives and raises `state.quality` by
  `renovation_quality_gain * (units_returned / rental_units)` (a district-average nudge,
  capped at 1.0 -- one renovated flat moves the average less in a bigger rental stock).
- `process_construction` (monthly starts, checked every tick for completions): new units
  started this month = `round(construction_monthly_share * housing_units * max(0, 1 +
  construction_rent_elasticity * (expected_rent / initial_rent - 1)))`, where `expected_rent
  = min(avg_rent, rent_cap)` if a cap is active else `avg_rent`, and `initial_rent =
  rent_history[district][0]` (the same "initial" `RentCapPolicy.cap_pct_of_initial` and
  `_adjust_rents` already use). A binding cap depresses `expected_rent` below what an
  uncapped market would pay, so `construction_rent_elasticity > 0` means fewer starts under a
  cap than without one -- the simplification documented in the task: Catalan rent caps also
  apply to a unit's *first* lease once it's built, so a capped district's expected return on
  new construction is capped too, exactly like an existing unit's. Starts are queued on
  `state.construction_pipeline` as `(tick + construction_lag_ticks, units)`; on completion
  `construction_rental_share` of the units join `rental_units` (and `world._vacant_rental`),
  the rest join `owner_units` (and `world._vacant_owner`), and `housing_units` grows by the
  full amount. Completions this tick are recorded in `world.completed_today: dict[district_id,
  int]` (rebuilt fresh every call -- the engine reads it once per tick for
  `DistrictSnapshot.new_units_completed`, see docs/CONTRACTS.md note in this task's final
  report).
- `process_quality` (monthly): `state.quality -= quality_decay_capped_monthly` in a district
  with an active `rent_cap`, else `state.quality += quality_recovery_monthly` (both clipped to
  [0, 1]) -- rent control's maintenance-decline effect (a capped rent no longer covers upkeep
  at the same rate) and its slow reversal once a cap lifts.
- `process_seasonal_return` (monthly): a small share of seasonal units returns to long-term
  rental when the long-term market (or capped) rent is close to what a seasonal let earns.
  `seasonal_income = seasonal_rent_multiple * (expected_rent if seasonal_capped else
  avg_rent)` -- when `seasonal_capped` is False (the default: Catalan lloguer de temporada is
  usually not subject to the long-term cap), the seasonal premium is measured against the
  *uncapped* market rent, so a binding cap widens the gap and return stays close to zero,
  reproducing the one-way pressure Diamond/McQuade/Qian document. `share =
  SEASONAL_RETURN_BASE_MONTHLY * clip(expected_rent / seasonal_income, 0, 1)`: as the
  long-term rent closes in on the seasonal income (e.g. `seasonal_capped=True`, or the
  seasonal multiple itself is modest), up to `SEASONAL_RETURN_BASE_MONTHLY` (3%) of the
  seasonal stock returns that month, so the effect is never a strict one-way ratchet.
- `below_market`: a renter is "locked in" when their own `rent_monthly` is below
  `BELOW_MARKET_FRACTION` (0.85) of their home district's current `avg_rent` -- cheap proxy
  for "this tenant would pay materially more to move today," used by prompts/snapshots to
  surface rent-cap lock-in without re-deriving it from `DistrictSnapshot`.

`world/market.py` is the only caller of `free_unit` / `take_rental_unit` / `take_owner_unit` /
`rental_vacancy` / `owner_vacancy` (from `apply_decisions` and `_spawn_arrivals`), all gated on
`scenario.rental_supply.enabled`; when disabled these bookkeeping dicts still exist (populated
at `init_world`) but nothing reads or writes them again, so behaviour is bit-identical to
before this module existed.
"""

from __future__ import annotations

import numpy as np

from jevcity.types import (
    TICKS_PER_MONTH,
    Agent,
    DistrictId,
    DistrictProfile,
    DistrictState,
    LandlordAction,
    LandlordDecision,
    LandlordType,
    RentalSupplyParams,
    Scenario,
    Tenure,
    Vacancy,
    World,
)

from ._helpers import clip

DEFAULT_OWNER_SHARE_FALLBACK = 0.4  # profile.owner_share fallback when a district has 0 residents
SEASONAL_INITIAL_SHARE = 0.01  # seasonal lets start at ~1% of the initial rental stock (PLAUSIBLE)
SEASONAL_RETURN_BASE_MONTHLY = 0.03  # max monthly share of seasonal stock returning to rental
BELOW_MARKET_FRACTION = 0.85  # renter locked in when paying < this share of current avg_rent


def split_housing_stock(
    profile: DistrictProfile, residential_base: int, owner_occ: int, renter_occ: int
) -> tuple[int, int, int, int, int]:
    """Split `residential_base` into (owner_units, rental_units, seasonal_units,
    initial_owner_vacant, initial_rental_vacant). See module docstring."""
    occupied = owner_occ + renter_occ
    vacant_total = max(residential_base - occupied, 0)
    if occupied > 0:
        owner_frac = owner_occ / occupied
    else:
        owner_frac = profile.owner_share if profile.owner_share is not None else DEFAULT_OWNER_SHARE_FALLBACK
    owner_vacant = round(vacant_total * owner_frac)
    owner_vacant = min(max(owner_vacant, 0), vacant_total)
    renter_vacant = vacant_total - owner_vacant

    owner_units = owner_occ + owner_vacant
    rental_units_raw = renter_occ + renter_vacant
    seasonal_units = min(round(SEASONAL_INITIAL_SHARE * rental_units_raw), renter_vacant)
    rental_units = rental_units_raw - seasonal_units
    rental_vacant = renter_vacant - seasonal_units
    return owner_units, rental_units, seasonal_units, owner_vacant, rental_vacant


def draw_landlord_type(rng: np.random.Generator, large_landlord_share: float) -> LandlordType:
    return LandlordType.LARGE if rng.random() < large_landlord_share else LandlordType.SMALL


def tenant_years(tick: int, lease_start_tick: int) -> float:
    return max((tick - lease_start_tick) / 365.0, 0.0)


def expected_rent(state: DistrictState) -> float:
    """Rent a landlord (or a builder) can actually expect: the market rent, or the cap when
    one is active and binding."""
    if state.rent_cap is not None:
        return min(state.avg_rent, state.rent_cap)
    return state.avg_rent


def rental_vacancy(world: World, district_id: DistrictId) -> int:
    return getattr(world, "_vacant_rental", {}).get(district_id, 0)


def owner_vacancy(world: World, district_id: DistrictId) -> int:
    return getattr(world, "_vacant_owner", {}).get(district_id, 0)


def free_unit(
    world: World,
    params: RentalSupplyParams,
    rng: np.random.Generator,
    tick: int,
    agent: Agent,
    state: DistrictState,
    vacancies: list[Vacancy],
) -> None:
    """Agent is leaving `state.id` (MOVE or LEAVE_CITY). A renter's old unit goes "pending
    landlord decision" (emits a Vacancy, not yet rentable); an owner's old unit is sold
    outright and is immediately available to the next owner-occupier."""
    did = state.id
    if agent.tenure is Tenure.RENTER:
        world._renter_occupied[did] = max(world._renter_occupied.get(did, 0) - 1, 0)
        vid = world._next_vacancy_id
        world._next_vacancy_id += 1
        vacancy = Vacancy(
            vacancy_id=vid,
            tick=tick,
            district=did,
            landlord_type=draw_landlord_type(rng, params.large_landlord_share),
            last_rent=agent.rent_monthly,
            tenant_years=tenant_years(tick, agent.lease_start_tick),
        )
        world._pending_vacancies[vid] = did
        vacancies.append(vacancy)
    else:
        world._owner_occupied[did] = max(world._owner_occupied.get(did, 0) - 1, 0)
        world._vacant_owner[did] = world._vacant_owner.get(did, 0) + 1


def take_rental_unit(world: World, district_id: DistrictId) -> None:
    world._vacant_rental[district_id] = world._vacant_rental.get(district_id, 0) - 1
    world._renter_occupied[district_id] = world._renter_occupied.get(district_id, 0) + 1


def take_owner_unit(world: World, district_id: DistrictId) -> None:
    world._vacant_owner[district_id] = world._vacant_owner.get(district_id, 0) - 1
    world._owner_occupied[district_id] = world._owner_occupied.get(district_id, 0) + 1


def _apply_action(
    world: World,
    state: DistrictState,
    action: LandlordAction,
    params: RentalSupplyParams,
    tick: int,
) -> None:
    did = state.id
    if action == LandlordAction.RELET:
        world._vacant_rental[did] = world._vacant_rental.get(did, 0) + 1
    elif action == LandlordAction.SELL:
        state.rental_units = max(state.rental_units - 1, 0)
        state.owner_units += 1
        world._vacant_owner[did] = world._vacant_owner.get(did, 0) + 1
    elif action == LandlordAction.SEASONAL:
        state.rental_units = max(state.rental_units - 1, 0)
        state.seasonal_units += 1
    elif action == LandlordAction.RENOVATE:
        state.renovating_units += 1
        world._renovation_pipeline.setdefault(did, []).append((tick + params.renovation_ticks, 1))


def apply_landlord_decisions(
    world: World,
    decisions: list[LandlordDecision],
    scenario: Scenario,
    rng: np.random.Generator,
    tick: int,
) -> dict[str, int]:
    """Apply this tick's landlord decisions (see module docstring); any vacancy emitted this
    tick that has no matching decision defaults to RELET. Returns counts by action value."""
    params = scenario.rental_supply
    pending: dict[int, DistrictId] = getattr(world, "_pending_vacancies", {})
    counts: dict[str, int] = {a.value: 0 for a in LandlordAction}

    for decision in decisions:
        state = world.states.get(decision.district)
        if state is None:
            pending.pop(decision.vacancy_id, None)
            continue
        _apply_action(world, state, decision.action, params, tick)
        counts[decision.action.value] += 1
        pending.pop(decision.vacancy_id, None)

    for vid, district in list(pending.items()):
        state = world.states.get(district)
        if state is not None:
            _apply_action(world, state, LandlordAction.RELET, params, tick)
            counts[LandlordAction.RELET.value] += 1
        pending.pop(vid, None)

    return counts


def process_renovations(world: World, scenario: Scenario, tick: int) -> None:
    """Return renovated units to the vacant-rental pool once their tick arrives, with the
    district-average quality bump. Cheap no-op when nothing is mid-renovation."""
    params = scenario.rental_supply
    pipeline: dict[DistrictId, list[tuple[int, int]]] = getattr(world, "_renovation_pipeline", {})
    for did, entries in pipeline.items():
        if not entries:
            continue
        state = world.states.get(did)
        if state is None:
            continue
        remaining: list[tuple[int, int]] = []
        completed = 0
        for ready_tick, units in entries:
            if ready_tick <= tick:
                completed += units
            else:
                remaining.append((ready_tick, units))
        pipeline[did] = remaining
        if completed:
            state.renovating_units = max(state.renovating_units - completed, 0)
            world._vacant_rental[did] = world._vacant_rental.get(did, 0) + completed
            if state.rental_units > 0:
                gain = params.renovation_quality_gain * (completed / state.rental_units)
                state.quality = min(state.quality + gain, 1.0)


def process_construction(world: World, scenario: Scenario, tick: int) -> None:
    """Monthly construction starts + completions every tick. Rebuilds `world.completed_today`
    (district_id -> units completed THIS tick) on every call, whether or not anything
    completed."""
    params = scenario.rental_supply
    rent_history = world.rent_history or {}
    completed_today: dict[DistrictId, int] = {}

    for did, state in world.states.items():
        if tick > 0 and tick % TICKS_PER_MONTH == 0:
            history = rent_history.get(did)
            initial_rent = history[0] if history else state.avg_rent
            if initial_rent > 0:
                er = expected_rent(state)
                factor = max(0.0, 1.0 + params.construction_rent_elasticity * (er / initial_rent - 1.0))
                starts = round(params.construction_monthly_share * state.housing_units * factor)
                if starts > 0:
                    state.construction_pipeline.append((tick + params.construction_lag_ticks, starts))

        if state.construction_pipeline:
            remaining: list[tuple[int, int]] = []
            completed = 0
            for ready_tick, units in state.construction_pipeline:
                if ready_tick <= tick:
                    completed += units
                else:
                    remaining.append((ready_tick, units))
            state.construction_pipeline = remaining
            if completed:
                rental_add = round(completed * params.construction_rental_share)
                owner_add = completed - rental_add
                state.rental_units += rental_add
                state.owner_units += owner_add
                state.housing_units += completed
                world._vacant_rental[did] = world._vacant_rental.get(did, 0) + rental_add
                world._vacant_owner[did] = world._vacant_owner.get(did, 0) + owner_add
                completed_today[did] = completed_today.get(did, 0) + completed

    world.completed_today = completed_today


def process_quality(world: World, scenario: Scenario, tick: int) -> None:
    """Monthly maintenance drift: decays under a binding cap, recovers otherwise."""
    params = scenario.rental_supply
    for state in world.states.values():
        if state.rent_cap is not None:
            state.quality = max(state.quality - params.quality_decay_capped_monthly, 0.0)
        else:
            state.quality = min(state.quality + params.quality_recovery_monthly, 1.0)


def process_seasonal_return(world: World, scenario: Scenario, tick: int) -> None:
    """Monthly: a share of seasonal units returns to long-term rental as the long-term rent
    closes in on seasonal income. See module docstring."""
    params = scenario.rental_supply
    for did, state in world.states.items():
        if state.seasonal_units <= 0:
            continue
        er = expected_rent(state)
        base_rent = er if params.seasonal_capped else state.avg_rent
        seasonal_income = params.seasonal_rent_multiple * base_rent
        if seasonal_income <= 0:
            continue
        share = SEASONAL_RETURN_BASE_MONTHLY * clip(er / seasonal_income, 0.0, 1.0)
        returning = min(round(state.seasonal_units * share), state.seasonal_units)
        if returning > 0:
            state.seasonal_units -= returning
            state.rental_units += returning
            world._vacant_rental[did] = world._vacant_rental.get(did, 0) + returning


def below_market(agent: Agent, world: World) -> bool:
    """True for a renter paying < BELOW_MARKET_FRACTION of their home district's current
    market rent (rent-cap lock-in proxy)."""
    if agent.tenure is not Tenure.RENTER:
        return False
    state = world.states.get(agent.home)
    if state is None or state.avg_rent <= 0:
        return False
    return agent.rent_monthly < BELOW_MARKET_FRACTION * state.avg_rent


__all__ = [
    "apply_landlord_decisions",
    "below_market",
    "draw_landlord_type",
    "expected_rent",
    "free_unit",
    "owner_vacancy",
    "process_construction",
    "process_quality",
    "process_renovations",
    "process_seasonal_return",
    "rental_vacancy",
    "split_housing_stock",
    "take_owner_unit",
    "take_rental_unit",
    "tenant_years",
]
