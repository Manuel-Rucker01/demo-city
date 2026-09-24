"""Small private helpers shared by world/market.py and events/triggers.py.

Not part of the public contract (see docs/CONTRACTS.md); safe to change freely
within T5's scope.
"""

from __future__ import annotations

from jevcity.types import Occupation


def clip(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def is_working_age(occupation: Occupation) -> bool:
    """True for agents who can hold/lose/search for a job (excludes students & retirees)."""
    return occupation not in (Occupation.STUDENT, Occupation.RETIRED)
