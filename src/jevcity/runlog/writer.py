"""Run log writer. Owner: T7. Layout (docs/CONTRACTS.md):
runs/<run_id>/meta.json, agents.json, ticks.ndjson, jev_calls.ndjson, summary.json"""

from pathlib import Path

from jevcity.types import AgentSnapshot, CallRecord, RunMeta, RunSummary, TickRecord


class RunWriter:
    """Also a CallSink. Flushes each line so a crashed run is still readable."""

    def __init__(self, run_dir: str | Path) -> None:
        raise NotImplementedError

    def write_meta(self, meta: RunMeta) -> None: ...
    def write_agents(self, agents: list[AgentSnapshot]) -> None: ...
    def write_tick(self, rec: TickRecord) -> None: ...
    def write_call(self, rec: CallRecord) -> None: ...
    def write_summary(self, summary: RunSummary) -> None: ...
    def close(self) -> None: ...
