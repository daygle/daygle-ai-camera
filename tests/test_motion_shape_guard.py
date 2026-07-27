"""Regression test: a stale-sized motion background must self-heal, not pin the
camera to fail-open motion.

``_MOTION_FRAME_W/H`` are global and read outside the ``_frame_motion_lock``, so
a concurrent live-settings frame-size change can leave a per-camera background
sized to the OLD dimensions. The subsequent ``current - background`` would then
raise on every frame, and the ``except`` handler (which never resets the
background) would pin the camera to fail-open ``motion=True`` forever.
``detect_frame_motion`` now treats a shape mismatch like a first frame.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytest.importorskip("cv2")

import app.state as _state  # noqa: E402
from app.detection_state import detect_frame_motion  # noqa: E402


def _img(h: int, w: int) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def test_shape_mismatch_resets_instead_of_failing_open(monkeypatch):
    cam = "shape-guard-cam"
    _state._frame_motion_prev.pop(cam, None)
    _state._frame_motion_error_cameras.discard(cam)

    # Seed a background at 30x40 (HxW).
    monkeypatch.setattr(_state, "_MOTION_FRAME_W", 40)
    monkeypatch.setattr(_state, "_MOTION_FRAME_H", 30)
    has_motion, conf, mask, intensity = detect_frame_motion(cam, _img(100, 120))
    assert has_motion is False and mask is None  # first frame seeds background
    assert _state._frame_motion_prev[cam].shape == (30, 40)

    # Frame size changes but the (now stale) background survives -- the race the
    # guard defends against. The next frame decodes to the NEW size.
    monkeypatch.setattr(_state, "_MOTION_FRAME_W", 60)
    monkeypatch.setattr(_state, "_MOTION_FRAME_H", 45)
    has_motion, conf, mask, intensity = detect_frame_motion(cam, _img(100, 120))

    # Self-heal: reported as a fresh (no-motion) frame, background re-seeded at the
    # new size -- NOT the fail-open (True, 0.4) the old except path produced.
    assert has_motion is False, "shape mismatch must self-heal, not fail open"
    assert conf == 0.0 and mask is None
    assert _state._frame_motion_prev[cam].shape == (45, 60)
    assert cam not in _state._frame_motion_error_cameras

    _state._frame_motion_prev.pop(cam, None)
