"""Token/cost accounting shared by all backends.

TypeSafe bills input tokens only (output tokens are free, per docs/jev-reference/models.md);
price is `ProviderSettings.price_per_mtok_usd`, configuration, not a code constant.
"""

from __future__ import annotations

import math
from typing import Any

from jevcity.types import CostSource, Usage

from .cachekey import canonical_json

CHARS_PER_TOKEN = 2.3


def estimate_tokens(body: dict[str, Any]) -> int:
    """Rough token estimate for a request body, used only when a real count isn't available.

    No provider documents its tokenizer (see providers.yaml `todo`). Calibrated on 114 real
    calls via Vercel AI Gateway (2026-09-24): 2.3 characters of canonical JSON per reported
    input token (JSON punctuation and short keys tokenize densely; the usual ~4 undercounts ~1.7x).
    """
    return math.ceil(len(canonical_json(body)) / CHARS_PER_TOKEN)


def computed_cost_usd(input_tokens: int, price_per_mtok_usd: float | None) -> float:
    """Cost computed locally from a price-per-Mtok, when the provider does not report cost."""
    if price_per_mtok_usd is None:
        return 0.0
    return input_tokens * price_per_mtok_usd / 1_000_000


class UsageMeter:
    """Accumulates `Usage` across calls. Not thread-safe beyond asyncio's single-threaded model."""

    def __init__(self) -> None:
        self._usage = Usage()

    def record(self, usage: Usage) -> None:
        self._usage = self._usage.add(usage)

    def snapshot(self) -> Usage:
        return self._usage

    @property
    def cost_so_far(self) -> float:
        return self._usage.cost_usd


def usage_from_response(
    *,
    input_tokens: int,
    output_tokens: int,
    reported_cost_usd: float | None,
    price_per_mtok_usd: float | None,
    estimated: bool = False,
) -> tuple[Usage, CostSource]:
    """Build the per-call `Usage` (requests=1) from one response's token counts.

    cost_source is "reported" when the provider gave a cost, "computed" when we derive it
    from price_per_mtok_usd, "estimated" when both tokens and cost are estimates (mock only),
    or "none" when neither is available.
    """
    if reported_cost_usd is not None:
        cost = reported_cost_usd
        source: CostSource = "estimated" if estimated else "reported"
    else:
        cost = computed_cost_usd(input_tokens, price_per_mtok_usd)
        source = "estimated" if estimated else ("computed" if price_per_mtok_usd else "none")
    usage = Usage(
        requests=1,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        cost_source=source,
        estimated=estimated,
    )
    return usage, source
