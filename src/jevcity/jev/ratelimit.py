"""Async rate limiting for the real Jev backends.

Two token buckets (requests/min, input tokens/s) plus a concurrency semaphore, adaptive
throttling on 429 (halve-ish the buckets, slow recovery), and a global pause that all callers
honor while a `retry-after` is in effect. Time and sleep are injectable (`Clock`) so tests can
run many simulated seconds without real wall-clock delay.

Limits are `rate_safety * configured limit` (`None` = unlimited, never throttled).
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    def time(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    """Default clock: wall-clock time.monotonic() + asyncio.sleep()."""

    def time(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class _TokenBucket:
    """A single token bucket: `capacity` tokens, refilled at `rate` tokens/second.

    `capacity` and `rate` can be changed live (adaptive throttling); a shrink clamps any
    tokens currently banked above the new capacity.
    """

    def __init__(self, capacity: float, rate: float, clock: Clock) -> None:
        self.capacity = capacity
        self.rate = rate
        self._clock = clock
        self._tokens = capacity
        self._last = clock.time()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock.time()
        elapsed = max(now - self._last, 0.0)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last = now

    def set_rate(self, capacity: float, rate: float) -> None:
        self._refill()
        self.capacity = capacity
        self.rate = rate
        self._tokens = min(self._tokens, capacity)

    async def acquire(self, amount: float) -> None:
        # amount may exceed capacity (e.g. a single big request); cap the wait target instead
        # of looping forever.
        while True:
            async with self._lock:
                self._refill()
                want = min(amount, self.capacity) if self.capacity > 0 else amount
                if self._tokens >= want:
                    self._tokens -= want
                    return
                deficit = want - self._tokens
                wait_s = deficit / self.rate if self.rate > 0 else 0.0
            await self._clock.sleep(wait_s)


class RateLimiter:
    """Combines a request/min bucket, a token/s bucket, a concurrency cap, adaptive throttling
    on 429, and a global pause honoring `retry-after`.

    `rpm_limit` / `tps_limit` are already `rate_safety`-scaled by the caller; `None` means no
    client-side limit for that dimension (still subject to the global 429 pause).
    """

    _BACKOFF_FACTOR = 0.7
    _FLOOR_FRACTION = 0.1
    _RECOVERY_FRACTION = 0.05
    _RECOVERY_INTERVAL_S = 60.0

    def __init__(
        self,
        rpm_limit: int | None,
        tps_limit: int | None,
        max_concurrency: int,
        clock: Clock | None = None,
    ) -> None:
        self._clock = clock or RealClock()
        self._configured_rpm = float(rpm_limit) if rpm_limit else None
        self._configured_tps = float(tps_limit) if tps_limit else None
        self._current_rpm = self._configured_rpm
        self._current_tps = self._configured_tps

        self._requests: _TokenBucket | None = None
        if self._configured_rpm is not None:
            self._requests = _TokenBucket(
                capacity=self._current_rpm, rate=self._current_rpm / 60.0, clock=self._clock
            )
        self._tokens: _TokenBucket | None = None
        if self._configured_tps is not None:
            self._tokens = _TokenBucket(
                capacity=self._current_tps, rate=self._current_tps, clock=self._clock
            )

        self._sem = asyncio.Semaphore(max_concurrency)
        self._pause_until: float = 0.0
        self._pause_lock = asyncio.Lock()
        self._last_recovery = self._clock.time()
        self._last_backoff = float("-inf")

    # --- adaptive throttling ------------------------------------------------------------------

    async def pause_for(self, seconds: float) -> None:
        """Called on a 429 with retry-after: all future acquires wait until then."""
        target = self._clock.time() + max(seconds, 0.0)
        async with self._pause_lock:
            self._pause_until = max(self._pause_until, target)

    _BACKOFF_DEBOUNCE_S = 15.0

    def note_rate_limited(self) -> None:
        """Multiply current rates by 0.7 (floor 10% of configured), at most once per
        _BACKOFF_DEBOUNCE_S: a burst of concurrent 429s is one congestion signal, not N.
        (Seen live 2026-09-24: 8 simultaneous 429s collapsed the rate to the floor.)"""
        now = self._clock.time()
        if now - self._last_backoff < self._BACKOFF_DEBOUNCE_S:
            return
        self._last_backoff = now
        if self._configured_rpm is not None and self._requests is not None:
            floor = self._configured_rpm * self._FLOOR_FRACTION
            self._current_rpm = max(self._current_rpm * self._BACKOFF_FACTOR, floor)
            self._requests.set_rate(self._current_rpm, self._current_rpm / 60.0)
        if self._configured_tps is not None and self._tokens is not None:
            floor = self._configured_tps * self._FLOOR_FRACTION
            self._current_tps = max(self._current_tps * self._BACKOFF_FACTOR, floor)
            self._tokens.set_rate(self._current_tps, self._current_tps)

    def note_success(self) -> None:
        """Recover +5% of configured per elapsed minute since the last recovery, capped at
        the configured rate."""
        now = self._clock.time()
        elapsed = now - self._last_recovery
        if elapsed < self._RECOVERY_INTERVAL_S:
            return
        minutes = int(elapsed // self._RECOVERY_INTERVAL_S)
        self._last_recovery += minutes * self._RECOVERY_INTERVAL_S
        if self._configured_rpm is not None and self._requests is not None:
            step = self._configured_rpm * self._RECOVERY_FRACTION * minutes
            self._current_rpm = min(self._configured_rpm, self._current_rpm + step)
            self._requests.set_rate(self._current_rpm, self._current_rpm / 60.0)
        if self._configured_tps is not None and self._tokens is not None:
            step = self._configured_tps * self._RECOVERY_FRACTION * minutes
            self._current_tps = min(self._configured_tps, self._current_tps + step)
            self._tokens.set_rate(self._current_tps, self._current_tps)

    def apply_server_limits(
        self,
        limit_requests: float | None,
        remaining_requests: int | None,
        reset_s: float | None,
        safety: float = 0.9,
    ) -> None:
        """Adopt limits the server advertises (e.g. Vercel's `x-ratelimit-limit-requests: 30`,
        observed 2026-09-24). The advertised per-minute limit caps the configured one for the
        rest of the run; `remaining == 0` pauses everyone until the window resets."""
        if limit_requests is not None and limit_requests > 0:
            advertised = limit_requests * safety
            if self._configured_rpm is None or advertised < self._configured_rpm:
                self._configured_rpm = advertised
                self._current_rpm = min(self._current_rpm or advertised, advertised)
                if self._requests is None:
                    self._requests = _TokenBucket(
                        capacity=self._current_rpm, rate=self._current_rpm / 60.0, clock=self._clock
                    )
                else:
                    self._requests.set_rate(self._current_rpm, self._current_rpm / 60.0)
        if remaining_requests == 0 and reset_s:
            self._pause_until = max(self._pause_until, self._clock.time() + reset_s)

    @property
    def current_rpm(self) -> float | None:
        return self._current_rpm

    @property
    def current_tps(self) -> float | None:
        return self._current_tps

    # --- acquisition ---------------------------------------------------------------------------

    async def _wait_out_pause(self) -> None:
        while True:
            async with self._pause_lock:
                remaining = self._pause_until - self._clock.time()
            if remaining <= 0:
                return
            await self._clock.sleep(remaining)

    def acquire(self, estimated_tokens: float) -> _LimiterCtx:
        return _LimiterCtx(self, estimated_tokens)

    async def _acquire(self, estimated_tokens: float) -> None:
        await self._wait_out_pause()
        await self._sem.acquire()
        try:
            if self._requests is not None:
                await self._requests.acquire(1)
            if self._tokens is not None:
                await self._tokens.acquire(estimated_tokens)
            await self._wait_out_pause()
        except BaseException:
            self._sem.release()
            raise

    def _release(self) -> None:
        self._sem.release()


class _LimiterCtx:
    """`async with limiter.acquire(tokens):` acquires request+token budget and a concurrency slot."""

    def __init__(self, limiter: RateLimiter, estimated_tokens: float) -> None:
        self._limiter = limiter
        self._tokens = estimated_tokens

    async def __aenter__(self) -> None:
        await self._limiter._acquire(self._tokens)

    async def __aexit__(self, *exc: object) -> None:
        self._limiter._release()
