from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config.yaml")
CONFIG_ENV_VAR = "DAYGLE_CONFIG"

DEFAULT_CONFIG: dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 8080},
    "ai": {
        "enabled": True,
        "backend": "onnx",
        "confidence": 0.45,
        "iou_threshold": 0.45,
        "input_size": 640,
        "model_path": "models/yolov8n.onnx",
        "labels_path": "models/coco.names",
    },
    "alerts": {
        "enabled": True,
        "email": {
            "enabled": False,
            "host": "",
            "port": 587,
            "username": "",
            "password": "",
            "from_address": "",
            "use_tls": True,
            "use_ssl": False,
        },
        "push_notification": {
            "enabled": False,
            "server_url": "https://ntfy.sh",
            "topic": "",
            "priority": "default",
            "username": "",
            "password": "",
        },
        "rules": [],
    },
    "recording": {
        "enabled": True,
        "mode": "motion",
        "continuous": False,
        "record_on_motion": True,
        "record_on_human": False,
        "record_on_objects": [],
        "pre_event_seconds": 10,
        "post_event_seconds": 15,
        "extension_step_seconds": 45,
        "max_clip_seconds": 300,
        "format": "mp4",
        "chunk_duration_seconds": 3600,
        "retention_days": 14,
        "max_storage_gb": 20,
        "auto_purge_enabled": True,
    },
    "auth": {
        "enabled": True,
        "session_timeout_hours": 12,
        "max_login_attempts": 5,
        "lockout_minutes": 15,
        "cookie_name": "daygle_session",
    },
    "storage": {
        "data_dir": "data",
        "database": "data/daygle_ai_camera.sqlite3",
        "snapshots_dir": "data/snapshots",
        "events_dir": "data/events",
        "recordings_dir": "data/recordings",
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return a recursive merge without mutating either input dictionary."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_settings(path: str | Path | None = None) -> dict[str, Any]:
    """Load YAML settings, falling back to defaults when no config file exists.

    The DAYGLE_CONFIG environment variable is honored so systemd installations can
    keep mutable configuration in /etc while the application runs from /opt.
    """
    config_source = path if path is not None else os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH
    config_path = Path(config_source)
    if not config_path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)

    with config_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}

    if not isinstance(loaded, dict):
        raise ValueError(f"Configuration file must contain a YAML mapping: {config_path}")

    return deep_merge(DEFAULT_CONFIG, loaded)
