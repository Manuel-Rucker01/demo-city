"""HTTP backend shared by the typesafe / openrouter / vercel providers.

One `HttpBackend` instance per provider, built by `make_backend`. Talks whatever wire format
`settings.wire` says (via `codecs.py`), retries transient failures, meters usage/cost, enforces
the cost budget, and writes `CallRecord`s.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from collections.abc import Sequence

import httpx

from jevcity.types import (
    CallRecord,
    CallSink,
    DecisionRequest,
    JevConfig,
    JevResponse,
    ProviderName,
    ProviderSettings,
    Usage,
)

from . import codecs
from .cachekey import cache_key, request_body
from .errors import (
    JevAuthError,
    JevBudgetExceeded,
    JevError,
    JevRequestError,
    JevRetryExhausted,
)
from .meter import UsageMeter, estimate_tokens, usage_from_response
from .ratelimit import RateLimiter

logger = logging.getLogger("jevcity.jev.http")

_RETRYABLE_STATUSES = {408, 429, *range(500, 600)}
_BACKOFF_INITIAL = 0.5
_BACKOFF_CAP = 16.0
_BACKOFF_MULTIPLIER = 2.0
_JITTER_FRACTION = 0.25


def _parse_retry_after(headers: httpx.Headers) -> float | None:
    """`retry-after` (seconds or HTTP date) or `retry-after-ms`, whichever is present."""
    ms = headers.get("retry-after-ms")
    if ms is not None:
        try:
            return max(float(ms) / 1000.0, 0.0)
        except ValueError:
            pass
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return max((dt.timestamp() - time.time()), 0.0)


def _float_header(headers: httpx.Headers, name: str) -> float | None:
    raw = headers.get(name)
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


def _duration_header(raw: str | None) -> float | None:
    """'32s', '1m30s', '250ms' or plain seconds -> seconds."""
    if not raw:
        return None
    import re

    total = 0.0
    matched = False
    for value, unit in re.findall(r"([0-9.]+)(ms|s|m|h)?", raw.strip()):
        matched = True
        v = float(value)
        total += {"ms": v / 1000, "s": v, "m": v * 60, "h": v * 3600, "": v}[unit]
    return total if matched else None


def _backoff_delay(attempt: int, rng: random.Random) -> float:
    """Exponential backoff: 0.5s * 2^n, capped at 16s, with up to 25% jitter subtracted."""
    base = min(_BACKOFF_INITIAL * (_BACKOFF_MULTIPLIER**attempt), _BACKOFF_CAP)
    jitter = base * _JITTER_FRACTION * rng.random()
    return max(base - jitter, 0.0)


class HttpBackend:
    provider: ProviderName
    settings: ProviderSettings

    def __init__(
        self,
        provider: ProviderName,
        settings: ProviderSettings,
        cfg: JevConfig,
        sink: CallSink | None = None,
        client: httpx.AsyncClient | None = None,
        rng: random.Random | None = None,
        clock: object | None = None,
        sleep: object | None = None,
    ) -> None:
        import os

        self.provider = provider
        self.settings = settings
        self._cfg = cfg
        self._sink = sink
        self._meter = UsageMeter()
        self._rng = rng or random.Random()
        self._model = settings.default_model
        self._seen_models: set[str] = set()
        # Injectable so tests can run many simulated seconds of retry backoff without real
        # wall-clock delay; defaults to real asyncio.sleep.
        self._sleep = sleep or asyncio.sleep

        api_key = None
        if settings.api_key_env is not None:
            api_key = os.environ.get(settings.api_key_env)
            if not api_key:
                raise JevAuthError(
                    f"{provider}: environment variable {settings.api_key_env!r} is not set "
                    "(required to call this provider)"
                )
        headers = {"Content-Type": "application/json"}
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"

        self._client = client or httpx.AsyncClient(
            base_url=settings.base_url, headers=headers, timeout=cfg.timeout_s
        )
        limiter_rpm = int(settings.rpm_limit * cfg.rate_safety) if settings.rpm_limit else None
        limiter_tps = int(settings.tps_limit * cfg.rate_safety) if settings.tps_limit else None
        self._limiter = RateLimiter(limiter_rpm, limiter_tps, cfg.max_concurrency, clock=clock)

    async def evaluate_many(self, reqs: Sequence[DecisionRequest]) -> list[JevResponse]:
        results: list[JevResponse | None] = [None] * len(reqs)

        async def run(i: int, req: DecisionRequest) -> None:
            results[i] = await self._evaluate_one(req)

        await asyncio.gather(*(run(i, r) for i, r in enumerate(reqs)))
        return results  # type: ignore[return-value]

    async def _evaluate_one(self, req: DecisionRequest) -> JevResponse:
        if self._meter.cost_so_far >= self._cfg.max_cost_usd:
            raise JevBudgetExceeded(self._meter.cost_so_far, self._cfg.max_cost_usd)

        body = request_body(req, self._model)
        wire_body = codecs.encode(self.settings.wire, body, self._model)
        key = cache_key(req)
        est_tokens = estimate_tokens(body)

        t0 = time.monotonic()
        attempts = 0
        last_error: BaseException | None = None

        # One limiter slot per attempt: a retry is a new request and must respect the limits.
        while attempts <= self._cfg.max_retries:
            attempts += 1
            try:
                async with self._limiter.acquire(est_tokens):
                    resp = await self._client.post(self.settings.path, json=wire_body)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempts > self._cfg.max_retries:
                    break
                await self._sleep(_backoff_delay(attempts - 1, self._rng))
                continue

            self._observe_rate_headers(resp.headers)

            if resp.status_code == 200:
                self._limiter.note_success()
                return self._handle_success(req, resp, body, wire_body, key, t0, attempts)

            self._handle_error_status(resp)  # raises for non-retryable statuses

            # retryable status: 408 / 429 / 5xx
            usage = Usage(requests=0, rate_limited=1 if resp.status_code == 429 else 0)
            self._meter.record(usage)
            retry_after = _parse_retry_after(resp.headers)
            if resp.status_code == 429:
                self._limiter.note_rate_limited()
                if retry_after is not None:
                    await self._limiter.pause_for(retry_after)
            last_error = JevError(
                f"{self.provider}: HTTP {resp.status_code}: "
                f"{codecs.parse_error_message(resp.status_code, _safe_json(resp))}"
            )
            if attempts > self._cfg.max_retries:
                break
            delay = retry_after if retry_after is not None else _backoff_delay(attempts - 1, self._rng)
            await self._sleep(delay)

        self._meter.record(Usage(requests=0, errors=1))
        raise JevRetryExhausted(attempts, last_error or JevError("unknown error"))

    def _observe_rate_headers(self, headers: httpx.Headers) -> None:
        """OpenAI-style `x-ratelimit-*` headers (sent by Vercel AI Gateway; not documented by
        TypeSafe or OpenRouter). Absent headers change nothing."""
        limit = _float_header(headers, "x-ratelimit-limit-requests")
        remaining = _float_header(headers, "x-ratelimit-remaining-requests")
        reset = _duration_header(headers.get("x-ratelimit-reset-requests"))
        if limit is None and remaining is None:
            return
        before = self._limiter.current_rpm
        self._limiter.apply_server_limits(
            limit, int(remaining) if remaining is not None else None, reset, self._cfg.rate_safety
        )
        after = self._limiter.current_rpm
        if before != after:
            logger.warning(
                "%s advertises %s requests/min; limiting to %.1f req/min", self.provider, limit, after
            )

    def _handle_error_status(self, resp: httpx.Response) -> None:
        status = resp.status_code
        if status in _RETRYABLE_STATUSES:
            return
        body = _safe_json(resp)
        message = f"{self.provider}: {codecs.parse_error_message(status, body)}"
        self._meter.record(Usage(requests=0, errors=1))
        if status in (401, 403):
            raise JevAuthError(message)
        if status == 402:
            raise JevBudgetExceeded(
                self._meter.cost_so_far,
                self._cfg.max_cost_usd,
                reason=f"{self.provider}: provider reports insufficient credits ({message})",
            )
        # 400, 413, 422, and any other non-retryable status.
        raise JevRequestError(message, status_code=status, detail=body)

    def _handle_success(
        self,
        req: DecisionRequest,
        resp: httpx.Response,
        body: dict,
        wire_body: dict,
        key: str,
        t0: float,
        attempts: int,
    ) -> JevResponse:
        wire_json = resp.json()
        response = codecs.decode(self.settings.wire, wire_json, req, self._model)

        if response.model not in self._seen_models:
            if self._seen_models:
                logger.warning(
                    "%s: resolved model changed from %s to %r",
                    self.provider,
                    sorted(self._seen_models),
                    response.model,
                )
            self._seen_models.add(response.model)

        usage, _source = usage_from_response(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            reported_cost_usd=response.usage.cost_usd,
            price_per_mtok_usd=self.settings.price_per_mtok_usd,
        )
        usage.models_seen[response.model] = 1
        if attempts > 1:
            usage.retries += attempts - 1
        self._meter.record(usage)

        if self._sink is not None:
            self._sink.write_call(
                CallRecord(
                    tick=req.tick,
                    request_id=req.request_id,
                    cache_key=key,
                    provider=self.provider,
                    requested_model=self._model,
                    resolved_model=response.model,
                    request=body,
                    wire_body=wire_body if wire_body != body else None,
                    response=response,
                    latency_ms=(time.monotonic() - t0) * 1000.0,
                    attempts=attempts,
                )
            )
        return response

    def usage(self) -> Usage:
        return self._meter.snapshot()

    async def aclose(self) -> None:
        await self._client.aclose()


def _safe_json(resp: httpx.Response) -> object:
    try:
        return resp.json()
    except ValueError:
        return resp.text
