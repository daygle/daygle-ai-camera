"""Guard the bootstrap YAML against schema drift.

``config.example.yaml`` is intentionally only a bootstrap subset of the full
runtime defaults. Every key it declares must still exist in ``DEFAULT_CONFIG``;
otherwise a copied example silently becomes a no-op or an undocumented setting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.settings import DEFAULT_CONFIG


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = ROOT / "config.example.yaml"


def _unknown_paths(value: Any, schema: Any, prefix: str = "") -> list[str]:
    """Return mapping keys present in *value* but absent from *schema*."""
    if not isinstance(value, dict):
        return []
    if not isinstance(schema, dict):
        return [prefix or "<root>"]

    unknown: list[str] = []
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if key not in schema:
            unknown.append(path)
        else:
            unknown.extend(_unknown_paths(child, schema[key], path))
    return unknown


def test_bootstrap_example_keys_exist_in_default_config_schema():
    """A copied example config must not contain keys the loader ignores."""
    with EXAMPLE_PATH.open("r", encoding="utf-8") as handle:
        example = yaml.safe_load(handle) or {}

    assert isinstance(example, dict)
    assert _unknown_paths(example, DEFAULT_CONFIG) == []


def test_bootstrap_example_remains_minimal_and_covers_startup_controls():
    """Keep the example focused while protecting the required startup knobs."""
    with EXAMPLE_PATH.open("r", encoding="utf-8") as handle:
        example = yaml.safe_load(handle) or {}

    assert set(example) == {"server", "system", "cloudflare_tunnel", "auth", "storage"}
    assert {"host", "port"} <= set(example["server"])
    assert {"database"} <= set(example["storage"])
    assert example["auth"]["enabled"] is True
