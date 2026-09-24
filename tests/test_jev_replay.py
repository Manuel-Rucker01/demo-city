"""Replay: mock -> CallRecords jsonl -> replay returns identical answers/usage; miss raises."""

from __future__ import annotations

import pytest

from jevcity.jev import make_backend
from jevcity.jev.errors import JevReplayMiss
from jevcity.types import CallRecord, DecisionRequest, JevConfig


def make_req(**kw) -> DecisionRequest:
    defaults = {
        "request_id": "r1",
        "tick": 0,
        "agent_ids": [1],
        "state": "Help! My payouts have been failing for 3 days.",
        "questions": {"urgent": {"type": "noul", "instructions": "..."}},
    }
    return DecisionRequest(**{**defaults, **kw})


class JsonlSink:
    """Appends each CallRecord as one JSON line; opened fresh per write to avoid holding a
    long-lived file handle in a test double."""

    def __init__(self, path):
        self.path = path
        self.path.write_text("", encoding="utf-8")

    def write_call(self, rec: CallRecord) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(rec.model_dump_json() + "\n")

    def close(self):
        pass


async def test_replay_returns_identical_answers_and_usage(tmp_path):
    log_path = tmp_path / "jev_calls.ndjson"
    sink = JsonlSink(log_path)

    mock_cfg = JevConfig(provider="mock")
    mock_backend = make_backend(mock_cfg, sink=sink)
    reqs = [make_req(request_id=f"r{i}", state=f"state {i}") for i in range(5)]
    original_results = await mock_backend.evaluate_many(reqs)
    original_usage = mock_backend.usage()
    await mock_backend.aclose()
    sink.close()

    replay_cfg = JevConfig(provider="mock", replay_from=str(log_path))
    replay_backend = make_backend(replay_cfg)
    replayed_results = await replay_backend.evaluate_many(reqs)
    replayed_usage = replay_backend.usage()
    await replay_backend.aclose()

    for orig, replayed in zip(original_results, replayed_results):
        assert replayed.answers == orig.answers
        assert replayed.model == orig.model

    assert replayed_usage.input_tokens == original_usage.input_tokens
    assert replayed_usage.cost_usd == pytest.approx(original_usage.cost_usd)
    assert replayed_usage.cache_hits == len(reqs)
    assert replayed_usage.requests == 0  # replay never counts as a new provider request


async def test_replay_miss_raises_and_never_calls_network(tmp_path):
    log_path = tmp_path / "jev_calls.ndjson"
    sink = JsonlSink(log_path)
    mock_backend = make_backend(JevConfig(provider="mock"), sink=sink)
    await mock_backend.evaluate_many([make_req(request_id="r1", state="known")])
    await mock_backend.aclose()
    sink.close()

    replay_cfg = JevConfig(provider="mock", replay_from=str(log_path))
    replay_backend = make_backend(replay_cfg)
    with pytest.raises(JevReplayMiss):
        await replay_backend.evaluate_many([make_req(request_id="r2", state="never seen before")])
    await replay_backend.aclose()
