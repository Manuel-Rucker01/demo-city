#!/usr/bin/env python
"""Smoke test: one tiny real call to whichever paid Jev provider is selected via JEV_PROVIDER.

    JEV_PROVIDER=typesafe|openrouter|vercel uv run python scripts/jev_smoke.py

Prints the canonical response, the resolved model, and usage/cost. Refuses to run against
`mock` (nothing to smoke-test) or when the provider's API key env var isn't set, so it never
makes an accidental network call.

NOTE for anyone running this: it makes one real, billed HTTP request. Do not run it unless you
have a provider account and have set the matching API key env var yourself.
"""

from __future__ import annotations

import asyncio
import os
import sys

from jevcity.jev import make_backend
from jevcity.jev import resolve_provider as _resolve_provider
from jevcity.types import DecisionRequest, JevConfig


async def main() -> int:
    cfg = JevConfig()
    provider, settings = _resolve_provider(cfg)

    if provider == "mock":
        print(
            "jev_smoke: JEV_PROVIDER is 'mock' (or unset); nothing to smoke-test against a "
            "real provider. Set JEV_PROVIDER=typesafe|openrouter|vercel.",
            file=sys.stderr,
        )
        return 1

    if settings.api_key_env and not os.environ.get(settings.api_key_env):
        print(
            f"jev_smoke: {settings.api_key_env} is not set; refusing to run (no real network "
            f"calls without a key you provided yourself).",
            file=sys.stderr,
        )
        return 1

    req = DecisionRequest(
        request_id="smoke-1",
        tick=0,
        agent_ids=[0],
        state="Help! My payouts have been failing for 3 days.",
        questions={
            "is_urgent": {
                "type": "noul",
                "instructions": "Does this convey urgency?",
                "criteria": {"true": "Explicitly time-sensitive", "false": "No urgency expressed"},
            }
        },
    )

    backend = make_backend(cfg)
    try:
        [response] = await backend.evaluate_many([req])
    finally:
        await backend.aclose()

    print(f"provider:        {provider}")
    print(f"resolved model:  {response.model}")
    print(f"answers:         {response.answers}")
    print(f"usage:           {response.usage}")
    print(f"cumulative:      {backend.usage()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
