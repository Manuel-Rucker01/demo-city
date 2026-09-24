"""Simulation loop. Owner: T6.

Per tick: apply_policies -> detect_events -> build_requests -> backend.evaluate_many ->
parse_decisions -> apply_decisions -> daily_update -> write TickRecord.
"""

from pathlib import Path

from jevcity.types import JevBackend, RunSummary, Scenario


async def run_simulation(
    scenario: Scenario, run_dir: str | Path, backend: JevBackend | None = None
) -> RunSummary:
    """Run a whole scenario. If backend is None, build one with make_backend(scenario.jev,
    sink=writer). Stops early (and records it) if real-mode cost exceeds jev.max_cost_usd."""
    raise NotImplementedError
