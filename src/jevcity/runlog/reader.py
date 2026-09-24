"""Run log reader. Owner: T7."""

from collections.abc import Iterator
from pathlib import Path

from jevcity.types import AgentSnapshot, CallRecord, RunMeta, RunSummary, TickRecord


class RunReader:
    def __init__(self, run_dir: str | Path) -> None:
        raise NotImplementedError

    def meta(self) -> RunMeta: ...
    def agents(self) -> list[AgentSnapshot]: ...
    def ticks(self) -> Iterator[TickRecord]: ...
    def calls(self) -> Iterator[CallRecord]: ...
    def summary(self) -> RunSummary | None: ...
