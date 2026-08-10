"""Regression tests for ``app/camera_instance.py``.

Covers the small but important surface of ``create_camera`` that turns
stored camera settings into a live ``OpenCvStreamCamera`` instance,
with special attention to the auto-detect FPS contract: a non-positive
or missing ``fps`` value must be coerced to ``None`` so the backend can
detect the stream's actual frame rate.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Preload app.main to avoid the Phase-18 circular-import gate.
# codeql[py/unused-import] -- preload is intentional; subsequent imports use it
import app.main  # noqa: E402  -- must precede the import below
from app.camera_instance import create_camera  # noqa: E402


def _settings(**overrides) -> dict:
    base = {
        'id': 'cam-1',
        'name': 'Cam 1',
        'backend': 'onvif',
        'host': '192.168.1.100',
        'port': 554,
        'path': 'stream1',
        'username': 'admin',
        'password': 'secret',
    }
    base.update(overrides)
    return base


def test_create_camera_coerces_zero_fps_to_none() -> None:
    """A stored fps of 0 must be treated as auto-detect (None), not passed
    through as an integer."""
    cam = create_camera(_settings(fps=0))
    assert cam.configured_fps is None
    assert cam.fps == 15  # constructor fallback until stream is opened


def test_create_camera_coerces_negative_fps_to_none() -> None:
    """A stored negative fps must also be treated as auto-detect."""
    cam = create_camera(_settings(fps=-5))
    assert cam.configured_fps is None
    assert cam.fps == 15


def test_create_camera_preserves_positive_fps_as_override() -> None:
    """A stored positive fps is a user override and must be preserved."""
    cam = create_camera(_settings(fps=30))
    assert cam.configured_fps == 30
    assert cam.fps == 30


def test_create_camera_treats_missing_fps_as_auto_detect() -> None:
    """When fps is omitted from settings, the camera should use the
    auto-detect path (configured_fps=None)."""
    cam = create_camera(_settings())
    assert cam.configured_fps is None
    assert cam.fps == 15


def test_create_camera_coerces_string_zero_to_none() -> None:
    """String values from stored config or form payloads must be parsed
    before checking the sign, so '0' is treated as auto-detect."""
    cam = create_camera(_settings(fps='0'))
    assert cam.configured_fps is None
    assert cam.fps == 15


def test_create_camera_coerces_empty_string_to_none() -> None:
    """An empty string fps must be treated the same as missing."""
    cam = create_camera(_settings(fps=''))
    assert cam.configured_fps is None
    assert cam.fps == 15
