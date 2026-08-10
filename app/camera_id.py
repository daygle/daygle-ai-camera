"""Camera-id normalizer extracted from ``app/camera_config.py``.

Extracting this single helper breaks the ``zone_schema ↔ camera_config``
mutual import cycle: ``zone_schema`` needs ``normalize_camera_id`` (to
normalize label/zone ids), and ``camera_config`` needs the zone-schema
normalizers (``normalize_label_list``, ``normalize_monitoring_zones``).
When both lived in their respective modules the only safe place for either
import was inside a function body (Pool C).  With ``normalize_camera_id``
here, both callers can import it at module top-level with no cycle.
"""
from __future__ import annotations

import re
from typing import Any


def normalize_camera_id(value: Any, fallback: str = 'camera-1') -> str:
    camera_id = re.sub(
        '[^a-zA-Z0-9_-]+',
        '-',
        str(value or '').strip().lower(),
    ).strip('-')
    return camera_id or fallback


def camera_storage_key(value: Any) -> str:
    """Return the filesystem-safe key used for per-camera runtime data."""
    return re.sub(
        r'[^a-zA-Z0-9_-]+',
        '-',
        str(value or '').strip().lower(),
    ).strip('-') or 'camera'
