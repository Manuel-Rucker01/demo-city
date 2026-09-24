"""Run log writer. Owner: T7. Layout (docs/CONTRACTS.md):
runs/<run_id>/meta.json, agents.json, ticks.ndjson, jev_calls.ndjson, summary.json"""

from pathlib import Path
from types import TracebackType
from typing import Self

from jevcity.types import AgentSnapshot, CallRecord, RunMeta, RunSummary, TickRecord

_TICKS_FILE = "ticks.ndjson"
_CALLS_FILE = "jev_calls.ndjson"
_META_FILE = "meta.json"
_AGENTS_FILE = "agents.json"
_SUMMARY_FILE = "summary.json"


class RunWriter:
    """Also a CallSink. Flushes each line so a crashed run is still readable.

    Concurrency note: this class holds no locks. It is intended to be driven from a single
    event loop / thread (per docs/CONTRACTS.md: one engine owns the tick loop and the one
    CallSink it writes into). Do not share one RunWriter across threads or call write_tick /
    write_call concurrently from multiple coroutines without external synchronization -
    interleaved writes to the same file handle from truly concurrent callers could
    interleave partial lines.
    """

    def __init__(self, run_dir: str | Path, overwrite: bool = False) -> None:
        self.run_dir = Path(run_dir)
        ticks_path = self.run_dir / _TICKS_FILE
        if ticks_path.exists() and not overwrite:
            raise FileExistsError(
                f"{ticks_path} already exists; pass overwrite=True to reuse this run directory"
            )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._ticks_fh = None
        self._calls_fh = None
        self._closed = False

    def _ticks(self):
        # Kept open across calls (closed explicitly in close()); not a `with` block by design.
        if self._ticks_fh is None:
            self._ticks_fh = open(self.run_dir / _TICKS_FILE, "a", encoding="utf-8")  # noqa: SIM115
        return self._ticks_fh

    def _calls(self):
        if self._calls_fh is None:
            self._calls_fh = open(self.run_dir / _CALLS_FILE, "a", encoding="utf-8")  # noqa: SIM115
        return self._calls_fh

    def write_meta(self, meta: RunMeta) -> None:
        (self.run_dir / _META_FILE).write_text(meta.model_dump_json(indent=2), encoding="utf-8")

    def write_agents(self, agents: list[AgentSnapshot]) -> None:
        body = "[" + ",".join(a.model_dump_json() for a in agents) + "]"
        (self.run_dir / _AGENTS_FILE).write_text(body, encoding="utf-8")

    def write_tick(self, rec: TickRecord) -> None:
        fh = self._ticks()
        fh.write(rec.model_dump_json())
        fh.write("\n")
        fh.flush()

    def write_call(self, rec: CallRecord) -> None:
        fh = self._calls()
        fh.write(rec.model_dump_json())
        fh.write("\n")
        fh.flush()

    def write_summary(self, summary: RunSummary) -> None:
        (self.run_dir / _SUMMARY_FILE).write_text(
            summary.model_dump_json(indent=2), encoding="utf-8"
        )

    def close(self) -> None:
        if self._closed:
            return
        if self._ticks_fh is not None:
            self._ticks_fh.close()
            self._ticks_fh = None
        if self._calls_fh is not None:
            self._calls_fh.close()
            self._calls_fh = None
        self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
