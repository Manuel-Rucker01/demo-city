"""resolve_provider: env/override precedence, error messages."""

from __future__ import annotations

import pytest

from jevcity.jev import resolve_provider
from jevcity.jev.errors import JevAuthError
from jevcity.types import JevConfig


def test_default_is_mock():
    name, settings = resolve_provider(JevConfig())
    assert name == "mock"
    assert settings.wire == "systemone"


def test_env_provider_wins_over_scenario(monkeypatch):
    monkeypatch.setenv("JEV_PROVIDER", "openrouter")
    name, settings = resolve_provider(JevConfig(provider="mock"))
    assert name == "openrouter"
    assert settings.base_url == "https://openrouter.ai/api"


def test_unknown_provider_raises_listing_valid_ones():
    with pytest.raises(ValueError, match="mock.*typesafe.*openrouter.*vercel|unknown Jev provider"):
        resolve_provider(JevConfig(provider="bogus"))  # type: ignore[arg-type]


def test_overrides_and_env_limits_applied(monkeypatch):
    monkeypatch.setenv("JEV_RPM_LIMIT", "10")
    monkeypatch.setenv("JEV_TPS_LIMIT", "20")
    monkeypatch.setenv("JEV_MODEL", "jev-custom")
    monkeypatch.setenv("JEV_BASE_URL", "https://custom.example.com")
    cfg = JevConfig(provider="typesafe", overrides={"max_context_tokens": 1234})
    name, settings = resolve_provider(cfg)
    assert name == "typesafe"
    assert settings.rpm_limit == 10
    assert settings.tps_limit == 20
    assert settings.default_model == "jev-custom"
    assert settings.base_url == "https://custom.example.com"
    assert settings.max_context_tokens == 1234


def test_cfg_model_overrides_default_model():
    cfg = JevConfig(provider="typesafe", model="jev-pinned")
    _, settings = resolve_provider(cfg)
    assert settings.default_model == "jev-pinned"


def test_cfg_wire_switches_vercel_to_native_evaluate():
    cfg = JevConfig(provider="vercel", wire="vercel_evaluate")
    _, settings = resolve_provider(cfg)
    assert settings.wire == "vercel_evaluate"
    assert settings.path == "/v1/evaluate"
    assert settings.base_url == "https://ai-gateway.vercel.sh"


def test_cfg_wire_switches_openrouter_to_decisions():
    cfg = JevConfig(provider="openrouter", wire="openrouter_decisions")
    _, settings = resolve_provider(cfg)
    assert settings.wire == "openrouter_decisions"
    assert settings.path == "/api/alpha/decisions"
    assert settings.base_url == "https://openrouter.ai"


def test_missing_api_key_raises_at_backend_construction(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    from jevcity.jev.http import HttpBackend

    cfg = JevConfig(provider="typesafe")
    _, settings = resolve_provider(cfg)
    with pytest.raises(JevAuthError, match="TYPESAFE_API_KEY"):
        HttpBackend("typesafe", settings, cfg)
