"""Regression test: a stale-sized motion background must self-heal, not pin the
camera to fail-open motion.

Motion engines use a per-camera model signature and self-heal when a camera's
local thumbnail size changes.  The live monitor passes that size explicitly so
cameras with different profiles cannot resize one another's state.  The
regression tests also preserve the shape-mismatch self-healing guard used by
standalone callers that rely on the legacy global defaults.
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
    """Legacy diff engine: a stale-sized background self-heals on the next frame.

    Pinned to ``algorithm='diff'`` because this guard is an internal of the diff
    engine (``_frame_motion_prev``); the MOG2 engine handles a size change via
    its parameter signature, covered by the test below."""
    cam = "shape-guard-cam"
    _state._frame_motion_prev.pop(cam, None)
    _state._frame_motion_error_cameras.discard(cam)

    # Seed a background at 30x40 (HxW).
    monkeypatch.setattr(_state, "_MOTION_FRAME_W", 40)
    monkeypatch.setattr(_state, "_MOTION_FRAME_H", 30)
    has_motion, conf, mask, _frac = detect_frame_motion(cam, _img(100, 120), algorithm='diff')
    assert has_motion is False and mask is None  # first frame seeds background
    assert _state._frame_motion_prev[cam].shape == (30, 40)

    # Frame size changes but the (now stale) background survives -- the race the
    # guard defends against. The next frame decodes to the NEW size.
    monkeypatch.setattr(_state, "_MOTION_FRAME_W", 60)
    monkeypatch.setattr(_state, "_MOTION_FRAME_H", 45)
    has_motion, conf, mask, _frac = detect_frame_motion(cam, _img(100, 120), algorithm='diff')

    # Self-heal: reported as a fresh (no-motion) frame, background re-seeded at the
    # new size -- NOT the fail-open (True, 0.5) the except path would produce.
    assert has_motion is False, "shape mismatch must self-heal, not fail open"
    assert conf == 0.0 and mask is None
    assert _state._frame_motion_prev[cam].shape == (45, 60)
    assert cam not in _state._frame_motion_error_cameras

    _state._frame_motion_prev.pop(cam, None)


def test_mog2_rebuilds_model_on_frame_size_change(monkeypatch):
    """MOG2 engine: a live frame-size change rebuilds the per-camera model (via
    its parameter signature) and reports the resized frame as a fresh no-motion
    seed rather than raising on the mismatched mask shape."""
    cam = "mog2-shape-cam"
    _state._frame_motion_mog2.pop(cam, None)
    _state._frame_motion_mog2_meta.pop(cam, None)
    _state._frame_motion_error_cameras.discard(cam)

    monkeypatch.setattr(_state, "_MOTION_FRAME_W", 40)
    monkeypatch.setattr(_state, "_MOTION_FRAME_H", 30)
    has_motion, conf, mask, _frac = detect_frame_motion(cam, _img(100, 120), algorithm='mog2')
    assert has_motion is False and mask is None  # seed frame
    assert _state._frame_motion_mog2_meta[cam][:2] == (40, 30)

    # Grow the motion frame; the signature no longer matches so the model is
    # rebuilt and this frame is a fresh seed (no motion, no error).
    monkeypatch.setattr(_state, "_MOTION_FRAME_W", 60)
    monkeypatch.setattr(_state, "_MOTION_FRAME_H", 45)
    has_motion, conf, mask, _frac = detect_frame_motion(cam, _img(100, 120), algorithm='mog2')
    assert has_motion is False and conf == 0.0 and mask is None
    assert _state._frame_motion_mog2_meta[cam][:2] == (60, 45)
    _state._frame_motion_mog2.pop(cam, None)
    _state._frame_motion_mog2_meta.pop(cam, None)


def test_camera_local_frame_sizes_do_not_invalidate_each_other(monkeypatch):
    """Two cameras may use different Day/Night thumbnail sizes concurrently."""
    cam_small = "camera-local-small"
    cam_large = "camera-local-large"
    for camera_id in (cam_small, cam_large):
        _state._frame_motion_prev.pop(camera_id, None)
        _state._frame_motion_last_frame.pop(camera_id, None)
        _state._frame_motion_last_gray.pop(camera_id, None)
        _state._frame_motion_mog2.pop(camera_id, None)
        _state._frame_motion_mog2_meta.pop(camera_id, None)

    monkeypatch.setattr(_state, "_MOTION_FRAME_W", 99)
    monkeypatch.setattr(_state, "_MOTION_FRAME_H", 77)
    detect_frame_motion(cam_small, _img(100, 120), algorithm="diff", frame_size=(40, 30))
    detect_frame_motion(cam_large, _img(100, 120), algorithm="diff", frame_size=(60, 45))
    assert _state._frame_motion_prev[cam_small].shape == (30, 40)
    assert _state._frame_motion_prev[cam_large].shape == (45, 60)

    detect_frame_motion(cam_small, _img(100, 120), algorithm="mog2", frame_size=(40, 30))
    detect_frame_motion(cam_large, _img(100, 120), algorithm="mog2", frame_size=(60, 45))
    assert _state._frame_motion_mog2_meta[cam_small][:2] == (40, 30)
    assert _state._frame_motion_mog2_meta[cam_large][:2] == (60, 45)

    for camera_id in (cam_small, cam_large):
        _state._frame_motion_prev.pop(camera_id, None)
        _state._frame_motion_last_frame.pop(camera_id, None)
        _state._frame_motion_last_gray.pop(camera_id, None)
        _state._frame_motion_mog2.pop(camera_id, None)
        _state._frame_motion_mog2_meta.pop(camera_id, None)
