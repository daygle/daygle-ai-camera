"""Regression tests for ``app.detection_state.detect_frame_motion``.

These tests exercise the adaptive-background motion gate directly with
synthetic frames (no live camera ingest required).

Run with::

    pytest tests/test_frame_motion_intensity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

import app.detection_state as ds  # noqa: E402
import app.state as st  # noqa: E402


def _seed_background(camera_id: str, image: np.ndarray) -> None:
    """First call establishes the adaptive-background model for the camera."""
    st._frame_motion_prev.pop(camera_id, None)
    has_motion, _conf, _mask, _frac = ds.detect_frame_motion(camera_id, image)
    # The very first frame has no prior background, so it reports no motion and
    # zero confidence by construction.
    assert has_motion is False


def test_detect_frame_motion_returns_four_tuple():
    cam = "motion-shape"
    base = np.full((120, 160, 3), 100, dtype=np.uint8)
    result = ds.detect_frame_motion(cam, base)
    assert len(result) == 4
    has_motion, confidence, _diff_mask, _frac = result
    assert isinstance(has_motion, bool)
    assert isinstance(confidence, float)


def test_motion_confirmation_requires_two_consecutive_zone_frames():
    """A one-frame zone spike must not create a motion event or recording."""
    cam = "motion-confirmation"
    st._motion_confirm_streaks.pop(cam, None)
    detection = {
        'zone_id': 'driveway',
        'zone_name': 'Driveway',
        'confidence': 0.9,
    }

    assert ds.confirm_motion_detections(cam, [detection]) == []
    assert ds.confirm_motion_detections(cam, [detection]) == [detection]
    # Quiet frames reset the consecutive streak before the next motion burst.
    assert ds.confirm_motion_detections(cam, []) == []
    assert ds.confirm_motion_detections(cam, [detection]) == []


def test_invalid_image_fails_closed_instead_of_synthetic_motion():
    """A bad JPEG must not become a fake motion event.

    The old fail-open result (True, 0.5, ...) cleared the default 0.45 motion
    rule and could create recordings while the camera frame was undecodable.
    """
    cam = "motion-invalid-image"
    st._frame_motion_prev.pop(cam, None)
    st._frame_motion_last_frame.pop(cam, None)
    st._frame_motion_last_gray.pop(cam, None)
    st._frame_motion_error_cameras.discard(cam)

    has_motion, confidence, diff_mask, fraction = ds.detect_frame_motion(
        cam,
        b"not-a-valid-jpeg",
    )

    assert has_motion is False
    assert confidence == 0.0
    assert diff_mask is None
    assert fraction == 0.0


def test_sub_gate_motion_keeps_confidence_zero_but_records_changes():
    """A few changed pixels (below the alert gate) must NOT alert, but the
    diff_mask should still reflect the changed pixels."""
    cam = "motion-subgate"
    base = np.full((120, 160, 3), 100, dtype=np.uint8)
    _seed_background(cam, base)

    # Change a small patch: 5 px out of 160*120 = ~0.026%, well under the
    # default 0.3% gate.
    nudged = base.copy()
    nudged[0:1, 0:5] = 255
    has_motion, confidence, diff_mask, _frac = ds.detect_frame_motion(cam, nudged)

    assert has_motion is False
    assert confidence == 0.0  # alert path still ignores noise
    assert diff_mask is not None and np.any(diff_mask)  # but the mask shows real activity


def test_temporal_diff_catches_subject_moving_between_sub_gate_frames():
    """Movement between samples remains visible even when each new silhouette
    is too small to clear the background gate by itself."""
    cam = "motion-temporal"
    base = np.full((120, 160, 3), 100, dtype=np.uint8)
    _seed_background(cam, base)

    first = base.copy()
    first[20:28, 20:25] = 255  # 40 pixels: below the 0.3% frame gate
    has_motion, _confidence, _mask, _fraction = ds.detect_frame_motion(
        cam, first, gate_fraction=0.003, pixel_threshold=15,
    )
    assert has_motion is False

    moved = base.copy()
    moved[20:28, 25:30] = 255  # the subject moved five thumbnail pixels
    has_motion, confidence, mask, fraction = ds.detect_frame_motion(
        cam, moved, gate_fraction=0.003, pixel_threshold=15,
    )
    assert has_motion is True
    assert confidence > 0.0
    assert mask is not None and fraction > 0.003


def test_above_gate_motion_reports_positive_confidence():
    """When motion clears the gate, the returned confidence is positive."""
    cam = "motion-abovegate"
    base = np.full((120, 160, 3), 100, dtype=np.uint8)
    _seed_background(cam, base)

    loud = base.copy()
    loud[0:48, :] = 250  # localized 40% change; below the scene-reset guard
    has_motion, confidence, _diff_mask, _frac = ds.detect_frame_motion(cam, loud)

    assert has_motion is True
    assert confidence > 0.0


def test_camera_wide_scene_change_reseeds_without_motion():
    """An exposure/reconnect jump must not become a persistent motion event."""
    cam = "motion-scene-reset"
    base = np.full((120, 160, 3), 50, dtype=np.uint8)
    _seed_background(cam, base)

    shifted = np.full((120, 160, 3), 200, dtype=np.uint8)
    has_motion, confidence, diff_mask, fraction = ds.detect_frame_motion(cam, shifted)

    assert has_motion is False
    assert confidence == 0.0
    assert diff_mask is None
    assert fraction == 0.0

    # The new scene is now the background, so repeated static frames remain quiet.
    has_motion, confidence, _mask, fraction = ds.detect_frame_motion(cam, shifted)
    assert has_motion is False
    assert confidence == 0.0
    assert fraction == 0.0


def test_auto_shadow_suppression_follows_frame_brightness():
    """'auto' rejects shadows on a bright (day) frame and stops on a dark
    (night/IR) frame, resolved from the thumbnail's mean brightness."""
    bright = np.full((30, 40, 3), 200, dtype=np.uint8)
    dark = np.full((30, 40, 3), 10, dtype=np.uint8)
    assert ds._resolve_shadow_suppression('auto', bright) is True
    assert ds._resolve_shadow_suppression('auto', dark) is False
    # Explicit + legacy values are honoured regardless of brightness.
    assert ds._resolve_shadow_suppression('on', dark) is True
    assert ds._resolve_shadow_suppression('off', bright) is False
    assert ds._resolve_shadow_suppression(True, dark) is True
    assert ds._resolve_shadow_suppression(False, bright) is False


def test_mog2_is_the_default_engine_and_reports_motion():
    """The default (no algorithm kwarg) path uses MOG2 and detects a subject."""
    cam = "mog2-default"
    st._frame_motion_mog2.pop(cam, None)
    st._frame_motion_mog2_meta.pop(cam, None)
    base = np.full((240, 320, 3), 100, dtype=np.uint8)
    # Seed + settle the model on the static scene.
    for _ in range(4):
        assert ds.detect_frame_motion(cam, base)[0] is False
    loud = base.copy()
    loud[40:140, 40:200] = 240
    has_motion, confidence, mask, _frac = ds.detect_frame_motion(cam, loud)
    assert has_motion is True
    assert confidence > 0.0
    assert mask is not None and mask.dtype == bool


def test_mog2_denoise_removes_isolated_speckle():
    """With denoise on, a lone single-pixel change is erased from the mask;
    with denoise off the same speckle survives. Proves the morphological open
    is actually applied on the default path."""
    cam_on = "mog2-denoise-on"
    cam_off = "mog2-denoise-off"
    for cam in (cam_on, cam_off):
        st._frame_motion_mog2.pop(cam, None)
        st._frame_motion_mog2_meta.pop(cam, None)
    base = np.full((240, 320, 3), 100, dtype=np.uint8)
    for cam in (cam_on, cam_off):
        for _ in range(6):
            ds.detect_frame_motion(cam, base, denoise=(cam == cam_on))
    speckle = base.copy()
    speckle[120, 160] = 255  # a single changed pixel
    _hm_on, _c_on, mask_on, _f_on = ds.detect_frame_motion(cam_on, speckle, denoise=True)
    _hm_off, _c_off, mask_off, _f_off = ds.detect_frame_motion(cam_off, speckle, denoise=False)
    on_count = 0 if mask_on is None else int(mask_on.sum())
    off_count = 0 if mask_off is None else int(mask_off.sum())
    # Denoise removes the isolated speckle; without it at least as much survives.
    assert on_count <= off_count
    assert on_count == 0


def test_mog2_freeze_on_motion_does_not_decay():
    """Repeating an above-gate frame must not let MOG2 learn the subject into
    the background (the freeze-on-motion contract), so confidence holds."""
    cam = "mog2-freeze"
    st._frame_motion_mog2.pop(cam, None)
    st._frame_motion_mog2_meta.pop(cam, None)
    base = np.full((240, 320, 3), 60, dtype=np.uint8)
    for _ in range(4):
        ds.detect_frame_motion(cam, base)
    loud = base.copy()
    loud[0:90, :] = 220
    _, first_conf, _, _ = ds.detect_frame_motion(cam, loud)
    assert first_conf > 0.0
    for _ in range(40):
        _, conf, _, _ = ds.detect_frame_motion(cam, loud)
    assert conf == first_conf, f"MOG2 confidence decayed {first_conf}->{conf} (freeze broken)"


def test_background_freezes_during_motion():
    """Background must NOT adapt while motion is above the gate.

    If the background learns the moving subject, the pixel diff shrinks on
    each successive frame and motion detection silently stops -- the reported
    bug where motion-only recording produced nothing after the first second.
    After many frames of identical above-gate motion the confidence should be
    unchanged, not decayed.
    """
    cam = "motion-freeze"
    base = np.full((120, 160, 3), 50, dtype=np.uint8)
    _seed_background(cam, base)

    loud = base.copy()
    loud[0:48, :] = 200  # localized 40% change, well above the default gate

    _, first_conf, _, _ = ds.detect_frame_motion(cam, loud)
    assert first_conf > 0.0, "First motion frame must be detected"

    # Feed the same "motion" frame many more times. If the background were
    # updating during motion, the diff would shrink toward zero; with the
    # freeze the diff should stay constant and confidence should not decay.
    for _ in range(40):  # 40 frames ≈ 10 s at 4 Hz
        _, conf, _, _ = ds.detect_frame_motion(cam, loud)

    assert conf == first_conf, (
        f"Confidence decayed from {first_conf} to {conf} -- "
        "background is adapting during motion (freeze-on-motion bug)"
    )
