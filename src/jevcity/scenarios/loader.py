"""YAML scenario loading. Owner: T6."""

from pathlib import Path

from jevcity.types import Scenario


def load_scenario(path: str | Path) -> Scenario:
    """Load YAML; supports `extends: base.yaml` (relative path) with deep merge, child wins."""
    raise NotImplementedError
