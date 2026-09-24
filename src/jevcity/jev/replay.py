"""Replay backend: answers come from a previously recorded `jev_calls.ndjson`, never the network.

Used when `cfg.replay_from` is set, for any originally-recorded provider (including mock). A
request whose `cache_key` was not recorded is an error -- replay never falls back to a live call.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from jevcity.types import (
    CallRecord,
    CallSink,
    DecisionRequest,
    JevConfig,
    JevResponse,
    ProviderSettings,
    Usage,
)

from .cachekey import cache_key
from .errors import JevReplayMiss
from .meter import UsageMeter, usage_from_response


def load_call_records(path: str) -> list[CallRecord]:
    records = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(CallRecord.model_validate(json.loads(line)))
    return records


class ReplayBackend:
    """Looks answers up by `cache_key`; never calls any provider."""

    def __init__(
        self,
        records: Sequence[CallRecord],
        provider: str,
        settings: ProviderSettings,
        cfg: JevConfig,
        provider_settings_by_name: dict[str, ProviderSettings] | None = None,
        sink: CallSink | None = None,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self._cfg = cfg
        self._sink = sink
        self._meter = UsageMeter()
        self._by_key: dict[str, CallRecord] = {r.cache_key: r for r in records}
        self._provider_settings = provider_settings_by_name or {}

    async def evaluate_many(self, reqs: Sequence[DecisionRequest]) -> list[JevResponse]:
        return [self._evaluate_one(req) for req in reqs]

    def _evaluate_one(self, req: DecisionRequest) -> JevResponse:
        key = cache_key(req)
        record = self._by_key.get(key)
        if record is None:
            raise JevReplayMiss(req.request_id, key)

        response = record.response
        price = None
        settings = self._provider_settings.get(record.provider)
        if settings is not None:
            price = settings.price_per_mtok_usd
        estimated = record.provider == "mock"

        usage, _source = usage_from_response(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            reported_cost_usd=response.usage.cost_usd,
            price_per_mtok_usd=price,
            estimated=estimated,
        )
        usage.models_seen[response.model] = 1
        usage.cache_hits = 1
        usage.requests = 0  # a replayed answer is not a new request to any provider
        self._meter.record(usage)

        if self._sink is not None:
            self._sink.write_call(
                CallRecord(
                    tick=req.tick,
                    request_id=req.request_id,
                    cache_key=key,
                    provider=record.provider,
                    replayed=True,
                    requested_model=record.requested_model,
                    resolved_model=response.model,
                    request=record.request,
                    wire_body=record.wire_body,
                    response=response,
                    latency_ms=0.0,
                    attempts=1,
                )
            )
        return response

    def usage(self) -> Usage:
        return self._meter.snapshot()

    async def aclose(self) -> None:
        return None
