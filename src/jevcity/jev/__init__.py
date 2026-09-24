"""Isolated Jev adapter: providers (mock, typesafe, openrouter, vercel), codecs, rate limiting,
retries, cost metering and replay. Nothing outside this package knows which provider is used.
Owner: T3."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, get_args

import yaml

from jevcity.types import (
    CallSink,
    JevBackend,
    JevConfig,
    ProviderName,
    ProviderSettings,
    WireFormat,
)

from .http import HttpBackend
from .mock import MockBackend
from .replay import ReplayBackend, load_call_records

_VALID_PROVIDERS = get_args(ProviderName)

# Known wire-format -> (base_url, path) defaults, used when `cfg.wire` switches a provider to a
# wire format its config/providers.yaml entry doesn't already point at (e.g. vercel's default
# `systemone` wire switched to its native `vercel_evaluate`, or openrouter's default `systemone`
# switched to the alpha `openrouter_decisions`). Never applied over an explicit override.
_WIRE_DEFAULTS: dict[WireFormat, dict[str, str]] = {
    "vercel_evaluate": {"base_url": "https://ai-gateway.vercel.sh", "path": "/v1/evaluate"},
    "openrouter_decisions": {"base_url": "https://openrouter.ai", "path": "/api/alpha/decisions"},
}


def _repo_root() -> Path:
    # src/jevcity/jev/__init__.py -> jev, jevcity, src, <repo root>
    return Path(__file__).resolve().parents[3]


def _providers_file_path(providers_file: str) -> Path:
    candidate = Path(providers_file)
    if candidate.is_file():
        return candidate
    fallback = _repo_root() / providers_file
    if fallback.is_file():
        return fallback
    raise FileNotFoundError(
        f"jev providers file not found at {candidate} (cwd) or {fallback} (repo root)"
    )


def _load_all_settings(providers_file: str) -> dict[str, ProviderSettings]:
    path = _providers_file_path(providers_file)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {name: ProviderSettings.model_validate(entry) for name, entry in raw.items()}


def resolve_provider(cfg: JevConfig) -> tuple[ProviderName, ProviderSettings]:
    """Provider name from env JEV_PROVIDER (wins) or cfg.provider; settings from
    cfg.providers_file, then cfg.overrides, then env JEV_RPM_LIMIT / JEV_TPS_LIMIT /
    JEV_MODEL / JEV_BASE_URL, then cfg.wire / cfg.model."""
    name = os.environ.get("JEV_PROVIDER") or cfg.provider
    if name not in _VALID_PROVIDERS:
        raise ValueError(
            f"unknown Jev provider {name!r}; valid providers are {', '.join(_VALID_PROVIDERS)}"
        )

    all_settings = _load_all_settings(cfg.providers_file)
    if name not in all_settings:
        raise ValueError(
            f"provider {name!r} has no entry in {cfg.providers_file!r}; "
            f"entries present: {', '.join(sorted(all_settings))}"
        )
    settings = all_settings[name]

    if cfg.overrides:
        settings = settings.model_copy(update=cfg.overrides)

    base_url_explicit = "base_url" in cfg.overrides
    path_explicit = "path" in cfg.overrides

    env_rpm = os.environ.get("JEV_RPM_LIMIT")
    if env_rpm is not None:
        settings = settings.model_copy(update={"rpm_limit": int(env_rpm)})
    env_tps = os.environ.get("JEV_TPS_LIMIT")
    if env_tps is not None:
        settings = settings.model_copy(update={"tps_limit": int(env_tps)})
    env_model = os.environ.get("JEV_MODEL")
    if env_model is not None:
        settings = settings.model_copy(update={"default_model": env_model})
    env_base_url = os.environ.get("JEV_BASE_URL")
    if env_base_url is not None:
        settings = settings.model_copy(update={"base_url": env_base_url})
        base_url_explicit = True

    if cfg.wire is not None and cfg.wire != settings.wire:
        update: dict[str, Any] = {"wire": cfg.wire}
        defaults = _WIRE_DEFAULTS.get(cfg.wire)
        if defaults is not None:
            if not base_url_explicit:
                update["base_url"] = defaults["base_url"]
            if not path_explicit:
                update["path"] = defaults["path"]
        settings = settings.model_copy(update=update)

    if cfg.model is not None:
        settings = settings.model_copy(update={"default_model": cfg.model})

    return name, settings  # type: ignore[return-value]


def choose_agents_per_request(
    cfg: JevConfig, settings: ProviderSettings, est_tokens_per_agent: int, est_shared_tokens: int
) -> int:
    """K for this run: cfg.agents_per_request in 'quality' mode; in 'throughput' mode the largest
    K <= cfg.max_agents_per_request with shared + K*per_agent <= 0.8 * max_context_tokens."""
    if cfg.batching == "quality":
        return max(cfg.agents_per_request, 1)

    budget = 0.8 * settings.max_context_tokens
    if est_tokens_per_agent <= 0:
        return cfg.max_agents_per_request
    max_k_by_budget = int((budget - est_shared_tokens) // est_tokens_per_agent)
    k = min(cfg.max_agents_per_request, max_k_by_budget)
    return max(k, 1)


def make_backend(cfg: JevConfig, sink: CallSink | None = None) -> JevBackend:
    """Build the backend for the resolved provider (or a replay backend if cfg.replay_from).
    Paid providers read their key from settings.api_key_env and fail fast if it is missing."""
    name, settings = resolve_provider(cfg)
    all_settings = _load_all_settings(cfg.providers_file)

    if cfg.replay_from:
        records = load_call_records(cfg.replay_from)
        return ReplayBackend(
            records,
            provider=name,
            settings=settings,
            cfg=cfg,
            provider_settings_by_name=all_settings,
            sink=sink,
        )

    if name == "mock":
        mock_settings = all_settings.get(cfg.mock_as, settings)
        return MockBackend(settings=mock_settings, cfg=cfg, sink=sink)

    return HttpBackend(name, settings, cfg, sink=sink)
