"""Commuting cost/mode and transit/LEZ policy application. Owner: T5.

Formulas:

- `commute_cost_monthly`: CAR -> `market.car_cost_monthly` + `car_cost_extra_monthly` of
  whichever of home/job district has an active LEZ (the higher of the two, districts
  normally share the same policy value so this is usually just "the" LEZ charge);
  METRO/BUS -> `market.public_transport_monthly`; BIKE/WALK/None -> 0. Unemployed agents (no
  commute) pay nothing regardless of `commute_mode`.
- `apply_transit_policy` / `apply_lez_policy`: like RentCapPolicy, these are reset to the
  "off" state (transit_boost=0, low_emission_zone=False, car_cost_extra_monthly=0) at the
  top of `apply_policies` and then set for districts named by an active policy
  (tick >= start_tick) -- idempotent per tick.
"""

from __future__ import annotations

from jevcity.types import (
    Agent,
    CommuteMode,
    DistrictState,
    LowEmissionZonePolicy,
    MarketParams,
    TransitLinePolicy,
)


def reset_transport_state(state: DistrictState) -> None:
    state.transit_boost = 0.0
    state.low_emission_zone = False
    state.car_cost_extra_monthly = 0.0


def apply_transit_policy(state: DistrictState, policy: TransitLinePolicy, tick: int) -> None:
    if tick < policy.start_tick or state.id not in policy.districts:
        return
    state.transit_boost = max(state.transit_boost, policy.transit_boost)


def apply_lez_policy(state: DistrictState, policy: LowEmissionZonePolicy, tick: int) -> None:
    if tick < policy.start_tick or state.id not in policy.districts:
        return
    state.low_emission_zone = True
    state.car_cost_extra_monthly = max(state.car_cost_extra_monthly, policy.car_cost_monthly)


def commute_cost_monthly(
    agent: Agent,
    home_state: DistrictState | None,
    job_state: DistrictState | None,
    market: MarketParams,
) -> float:
    if not agent.employed or agent.commute_mode is None:
        return 0.0
    if agent.commute_mode is CommuteMode.CAR:
        extra = 0.0
        if home_state is not None and home_state.low_emission_zone:
            extra = max(extra, home_state.car_cost_extra_monthly)
        if job_state is not None and job_state.low_emission_zone:
            extra = max(extra, job_state.car_cost_extra_monthly)
        return market.car_cost_monthly + extra
    if agent.commute_mode in (CommuteMode.METRO, CommuteMode.BUS):
        return market.public_transport_monthly
    return 0.0  # BIKE, WALK


def apply_commute_decision(agent: Agent, commute_mode: CommuteMode | None, tick: int) -> None:
    """Apply a (possibly None = keep current) commute_mode decision, ignoring CAR for
    agents without a car. `commute_since_tick` resets to `tick` only when the mode actually
    changes (or is set for the first time), so a decision that just reaffirms the current
    mode doesn't reset the habit clock -- see Agent.commute_since_tick."""
    if commute_mode is None or not agent.employed:
        return
    if commute_mode is CommuteMode.CAR and not agent.has_car:
        return
    if commute_mode != agent.commute_mode:
        agent.commute_mode = commute_mode
        agent.commute_since_tick = tick


def switch_mode_on_move(agent: Agent, tick: int) -> None:
    """A WALK commuter whose job is no longer in their (new) home district switches to
    METRO -- walking to a different district isn't realistic. Counts as a mode change: resets
    commute_since_tick (see Agent.commute_since_tick)."""
    if (
        agent.commute_mode is CommuteMode.WALK
        and agent.job_district is not None
        and agent.job_district != agent.home
    ):
        agent.commute_mode = CommuteMode.METRO
        agent.commute_since_tick = tick
