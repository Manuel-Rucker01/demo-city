"""choose_agents_per_request: quality mode returns configured K; throughput mode fits context."""

from __future__ import annotations

from jevcity.jev import choose_agents_per_request, resolve_provider
from jevcity.types import JevConfig


def test_quality_mode_returns_configured_k():
    cfg = JevConfig(provider="typesafe", batching="quality", agents_per_request=7)
    _, settings = resolve_provider(cfg)
    k = choose_agents_per_request(cfg, settings, est_tokens_per_agent=100, est_shared_tokens=500)
    assert k == 7


def test_throughput_mode_respects_context_openrouter_smaller_than_typesafe():
    """OpenRouter's documented context (32k) is smaller than TypeSafe's (64k), so throughput
    mode should pick a smaller K for OpenRouter than for TypeSafe, all else equal."""
    cfg = JevConfig(batching="throughput", max_agents_per_request=1000)

    _, typesafe_settings = resolve_provider(JevConfig(provider="typesafe"))
    _, openrouter_settings = resolve_provider(JevConfig(provider="openrouter"))

    k_typesafe = choose_agents_per_request(
        cfg, typesafe_settings, est_tokens_per_agent=50, est_shared_tokens=1000
    )
    k_openrouter = choose_agents_per_request(
        cfg, openrouter_settings, est_tokens_per_agent=50, est_shared_tokens=1000
    )
    assert k_openrouter <= k_typesafe <= 8  # both capped by 32 questions / 4 per agent


def test_throughput_mode_caps_at_max_agents_per_request():
    cfg = JevConfig(batching="throughput", max_agents_per_request=3)
    _, settings = resolve_provider(JevConfig(provider="typesafe"))
    k = choose_agents_per_request(cfg, settings, est_tokens_per_agent=1, est_shared_tokens=0)
    assert k == 3


def test_throughput_mode_always_at_least_one():
    cfg = JevConfig(batching="throughput", max_agents_per_request=64)
    _, settings = resolve_provider(JevConfig(provider="typesafe"))
    k = choose_agents_per_request(
        cfg, settings, est_tokens_per_agent=1_000_000, est_shared_tokens=0
    )
    assert k == 1


def test_k_capped_by_max_questions_per_request():
    from jevcity.jev import choose_agents_per_request
    from jevcity.types import JevConfig, ProviderSettings

    s = ProviderSettings(base_url="x", path="", wire="systemone", api_key_env=None,
                         default_model="m", max_context_tokens=64000, max_questions_per_request=32)
    assert choose_agents_per_request(JevConfig(batching="throughput"), s, 1100, 350) == 8
    assert choose_agents_per_request(JevConfig(agents_per_request=20), s, 1100, 350) == 8
