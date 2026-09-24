"""Exceptions raised by the Jev adapter."""

from __future__ import annotations


class JevError(Exception):
    """Base class for all Jev adapter errors."""


class JevAuthError(JevError):
    """401 from the API, or the API key env var is missing/empty at backend construction."""


class JevRequestError(JevError):
    """422 (or other non-retryable 4xx) from the API. `detail` holds the response body detail."""

    def __init__(self, message: str, *, status_code: int | None = None, detail: object = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class JevBudgetExceeded(JevError):
    """Raised before sending a request that would push cumulative cost >= cfg.max_cost_usd,
    or when the provider itself reports insufficient credits/quota (HTTP 402)."""

    def __init__(self, cost_so_far: float, max_cost_usd: float, *, reason: str | None = None):
        message = reason or (
            f"Jev budget exceeded: cost_so_far=${cost_so_far:.4f} >= max_cost_usd=${max_cost_usd:.4f}"
        )
        super().__init__(message)
        self.cost_so_far = cost_so_far
        self.max_cost_usd = max_cost_usd


class JevRetryExhausted(JevError):
    """All retries were used up without a successful response."""

    def __init__(self, attempts: int, last_error: BaseException):
        super().__init__(f"Jev request failed after {attempts} attempts: {last_error!r}")
        self.attempts = attempts
        self.last_error = last_error


class JevReplayMiss(JevError):
    """A request had no matching cache_key in the loaded replay log."""

    def __init__(self, request_id: str, cache_key: str):
        super().__init__(
            f"Jev replay miss for request_id={request_id!r} cache_key={cache_key!r}: "
            "no matching call recorded in the replay log"
        )
        self.request_id = request_id
        self.cache_key = cache_key
