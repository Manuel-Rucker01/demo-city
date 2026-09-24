"""Run log reader. Owner: T7."""

import json
import logging
from collections.abc import Iterator
from pathlib import Path

from jevcity.types import AgentSnapshot, CallRecord, JevResponse, RunMeta, RunSummary, TickRecord

logger = logging.getLogger(__name__)

_TICKS_FILE = "ticks.ndjson"
_CALLS_FILE = "jev_calls.ndjson"
_META_FILE = "meta.json"
_AGENTS_FILE = "agents.json"
_SUMMARY_FILE = "summary.json"


def _iter_ndjson_lines(path: Path) -> Iterator[str]:
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            is_last = i == len(lines) - 1
            if is_last:
                logger.warning(
                    "Skipping truncated last line in %s (crashed run?)", path
                )
                continue
            raise
        yield line


class RunReader:
    def __init__(self, run_dir: str | Path) -> None:
        self.run_dir = Path(run_dir)

    def meta(self) -> RunMeta:
        text = (self.run_dir / _META_FILE).read_text(encoding="utf-8")
        return RunMeta.model_validate_json(text)

    def agents(self) -> list[AgentSnapshot]:
        text = (self.run_dir / _AGENTS_FILE).read_text(encoding="utf-8")
        data = json.loads(text)
        return [AgentSnapshot.model_validate(row) for row in data]

    def ticks(self) -> Iterator[TickRecord]:
        path = self.run_dir / _TICKS_FILE
        if not path.exists():
            return
        for line in _iter_ndjson_lines(path):
            yield TickRecord.model_validate_json(line)

    def calls(self) -> Iterator[CallRecord]:
        path = self.run_dir / _CALLS_FILE
        if not path.exists():
            return
        for line in _iter_ndjson_lines(path):
            yield CallRecord.model_validate_json(line)

    def summary(self) -> RunSummary | None:
        path = self.run_dir / _SUMMARY_FILE
        if not path.exists():
            return None
        return RunSummary.model_validate_json(path.read_text(encoding="utf-8"))

    def tick_count(self) -> int:
        return sum(1 for _ in self.ticks())

    def load_calls_index(self) -> dict[str, JevResponse]:
        index: dict[str, JevResponse] = {}
        for rec in self.calls():
            index[rec.cache_key] = rec.response
        return index
