from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config.yaml")

DEFAULT_CONFIG: dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 8080},
    "camera": {
        "backend": "mock",
        "device": 0,
        "width": 1280,
        "height": 720,
        "fps": 15,
        "flip": "none",
    },
    "ai": {
        "enabled": True,
        "backend": "mock",
        "confidence": 0.45,
        "model_path": "models/model.onnx",
        "categories": ["person", "cat", "dog", "car", "bicycle", "bird", "package"],
    },
    "alerts": {
        "enabled": True,
        "rules": [
            {
                "name": "Cat alert",
                "object": "cat",
                "min_confidence": 0.50,
                "cooldown_seconds": 60,
                "enabled": True,
            }
        ],
    },
    "storage": {
        "data_dir": "data",
        "database": "data/daygle_ai_camera.sqlite3",
        "snapshots_dir": "data/snapshots",
        "events_dir": "data/events",
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        return DEFAULT_CONFIG

    with config_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}

    return deep_merge(DEFAULT_CONFIG, loaded)
