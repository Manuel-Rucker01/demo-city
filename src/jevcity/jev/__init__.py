"""Isolated Jev adapter: providers (mock, typesafe, openrouter, vercel), codecs, rate limiting,
retries, cost metering and replay. Nothing outside this package knows which provider is used.
Owner: T3."""

from jevcity.types import CallSink, JevBackend, JevConfig, ProviderName, ProviderSettings


def resolve_provider(cfg: JevConfig) -> tuple[ProviderName, ProviderSettings]:
    """Provider name from env JEV_PROVIDER (wins) or cfg.provider; settings from
    cfg.providers_file, then cfg.overrides, then env JEV_RPM_LIMIT / JEV_TPS_LIMIT /
    JEV_MODEL / JEV_BASE_URL."""
    raise NotImplementedError


def choose_agents_per_request(
    cfg: JevConfig, settings: ProviderSettings, est_tokens_per_agent: int, est_shared_tokens: int
) -> int:
    """K for this run: cfg.agents_per_request in 'quality' mode; in 'throughput' mode the largest
    K <= cfg.max_agents_per_request with shared + K*per_agent <= 0.8 * max_context_tokens."""
    raise NotImplementedError


def make_backend(cfg: JevConfig, sink: CallSink | None = None) -> JevBackend:
    """Build the backend for the resolved provider (or a replay backend if cfg.replay_from).
    Paid providers read their key from settings.api_key_env and fail fast if it is missing."""
    raise NotImplementedError
