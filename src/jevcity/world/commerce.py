"""Local commerce: where discretionary spending lands, and shop open/close dynamics.
Owner: T5.

Formulas:

- `spending_targets`: routes an agent's daily discretionary EUR (already agent-unit scale)
  to district(s) by `ShoppingPlace`: LOCAL -> home; WORK_DISTRICT -> job district (home if
  the agent has none, e.g. unemployed); CENTRE -> split 50/50 between ciutat_vella and
  eixample (Barcelona's main shopping streets); ONLINE -> nowhere (leaks out of local
  commerce entirely, tracked separately as `online_leak`).
- `to_real_eur`: scales an agent-unit EUR amount to real EUR for `shop_revenue_monthly`
  bookkeeping, by dividing by the spending agent's own district `agent_scale` (agents /
  real households there -- see `_helpers.compute_agent_scale`), i.e. multiplying by how many
  real households that one agent stands in for. This is the documented factor that keeps
  `shop_revenue_monthly` at a plausible real-EUR-per-shop order of magnitude instead of
  agent-count-scale numbers.
- `shops_delta`: at a month boundary, once a `shop_revenue_baseline` exists, compares
  `shop_revenue_monthly / shop_revenue_baseline` to `shop_close_threshold` /
  `shop_open_threshold`. Below the close threshold, the share of shops that closes is
  `clip(shop_close_threshold - ratio, 0, shop_monthly_change_max)` (proportional to the
  shortfall, capped); above the open threshold, symmetrically
  `clip(ratio - shop_open_threshold, 0, shop_monthly_change_max)` open. Each shop opened or
  closed changes jobs by `jobs_per_shop * agent_scale` (real jobs -> agent units), applied by
  the caller (which must never let `jobs` fall below `filled_jobs` -- excess employed agents
  are laid off instead, see `world/market.py::_apply_shop_dynamics`).
"""

from __future__ import annotations

from jevcity.types import DistrictId, ShoppingPlace

from ._helpers import clip

CENTRE_DISTRICTS = ("ciutat_vella", "eixample")


def spending_targets(
    shopping_place: ShoppingPlace, home: DistrictId, job_district: DistrictId | None
) -> dict[DistrictId, float]:
    """Return {district_id: share_of_amount} for one agent's discretionary spend. Shares sum
    to <= 1.0 (< 1.0 for ONLINE, which leaks; the caller multiplies by the actual EUR amount)."""
    if shopping_place is ShoppingPlace.LOCAL:
        return {home: 1.0}
    if shopping_place is ShoppingPlace.WORK_DISTRICT:
        return {(job_district or home): 1.0}
    if shopping_place is ShoppingPlace.CENTRE:
        return {d: 0.5 for d in CENTRE_DISTRICTS}
    return {}  # ONLINE: leaks out of local commerce


def to_real_eur(agent_unit_amount: float, agent_scale: float) -> float:
    """Scale one agent's spending to real EUR (see module docstring)."""
    if agent_scale <= 0:
        return agent_unit_amount
    return agent_unit_amount / agent_scale


def shops_close_open_fraction(
    ratio: float, close_threshold: float, open_threshold: float, max_change: float
) -> float:
    """Signed fraction of shops_open that opens (+) or closes (-) this month."""
    if ratio < close_threshold:
        return -clip(close_threshold - ratio, 0.0, max_change)
    if ratio > open_threshold:
        return clip(ratio - open_threshold, 0.0, max_change)
    return 0.0
