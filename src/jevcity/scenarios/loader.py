"""YAML scenario loading. Owner: T6.

`extends: <relative path>` recursively deep-merges a parent scenario file with the child
(dicts merge key by key, lists replace wholesale, and on any conflict the child wins). Unknown
top-level keys (and unknown keys inside `jev`/`market`/`events`) are rejected with a ValueError
that lists every offending dotted path, because pydantic silently ignores unrecognized fields by
default and a typo in a scenario file should not pass silently. `policies` entries are validated
by pydantic itself (RentCapPolicy) rather than by this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from jevcity.types import EventParams, JevConfig, MarketParams, Scenario

# Scenario fields that hold a nested BaseModel we also want to check for unknown keys.
_NESTED_MODELS: dict[str, type[BaseModel]] = {
    "jev": JevConfig,
    "market": MarketParams,
    "events": EventParams,
}


def _repo_root() -> Path:
    """Nearest ancestor of this file that contains pyproject.toml."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """dicts merge recursively; anything else (including lists) is replaced by `override`."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read scenario file {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} is not valid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        # ValueError (not TypeError) is this loader's documented contract for malformed input.
        raise ValueError(  # noqa: TRY004
            f"{path} must contain a YAML mapping, got {type(data).__name__}"
        )
    return data


def _load_raw(path: Path) -> dict[str, Any]:
    """Load one scenario file's raw dict, resolving `extends` recursively (child wins)."""
    data = _read_yaml_mapping(path)
    extends = data.pop("extends", None)
    if extends:
        parent_path = (path.parent / extends).resolve()
        parent_data = _load_raw(parent_path)
        data = _deep_merge(parent_data, data)
    return data


def _unknown_keys(
    data: dict[str, Any], model: type[BaseModel] = Scenario, prefix: str = ""
) -> list[str]:
    unknown: list[str] = []
    fields = model.model_fields
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if key not in fields:
            unknown.append(path)
            continue
        nested = _NESTED_MODELS.get(key) if model is Scenario else None
        if nested is not None and isinstance(value, dict):
            unknown.extend(_unknown_keys(value, nested, path))
    return unknown


def _resolve_data_path(scenario: Scenario, scenario_file: Path) -> None:
    """`data_path` is resolved relative to CWD; if that doesn't exist, fall back to the repo
    root (found by walking up from this module for a pyproject.toml). Documented here rather
    than relative to the scenario file, since data/ is a repo-wide directory, not per-scenario."""
    p = Path(scenario.data_path)
    if p.is_absolute() or p.exists():
        return
    candidate = _repo_root() / p
    if candidate.exists():
        scenario.data_path = str(candidate)


def load_scenario(path: str | Path) -> Scenario:
    """Load YAML; supports `extends: base.yaml` (relative path) with deep merge, child wins."""
    path = Path(path)
    data = _load_raw(path)

    unknown = _unknown_keys(data)
    if unknown:
        raise ValueError(
            f"unknown scenario field(s) in {path} (after resolving extends): {sorted(unknown)}"
        )

    scenario = Scenario(**data)
    _resolve_data_path(scenario, path)
    return scenario
