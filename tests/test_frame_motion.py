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
import app.event_debounce as ed  # noqa: E402
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


def test_steady_state_backoff_clear_does_not_wipe_motion_background():
    """``clear_live_camera_backoff`` runs on EVERY successful frame, before the
    motion gate. In steady state (camera not backed off) it must NOT reset the
    per-camera motion state, otherwise the diff engine's adaptive background is
    destroyed every cycle and it can never accumulate one -- so it never reports
    motion (and the periodic-scan clock resets every frame). It may only reset
    on a genuine transition out of backoff."""
    cam = "motion-backoff-steady"
    st._frame_motion_prev.pop(cam, None)
    st.live_detection_failure_count.pop(cam, None)
    st.live_detection_retry_after.pop(cam, None)
    st._periodic_scan_last_ts[cam] = 12345.0

    base = np.full((120, 160, 3), 100, dtype=np.uint8)
    moved = base.copy()
    moved[20:90, 40:130] = 255  # a large, clearly-alertable change

    # Simulate the live-monitor order: clear backoff, then run the gate.
    ed.clear_live_camera_backoff(cam)
    ds.detect_frame_motion(cam, base, algorithm="diff")  # seeds the background
    # Steady-state clear must leave the seeded background and the scan clock in place.
    assert cam in st._frame_motion_prev
    assert st._periodic_scan_last_ts.get(cam) == 12345.0

    ed.clear_live_camera_backoff(cam)
    has_motion, _conf, _mask, frac = ds.detect_frame_motion(cam, moved, algorithm="diff")
    assert has_motion is True and frac > 0.0


def test_recovery_backoff_clear_resets_motion_state():
    """A genuine transition out of backoff DOES reset the per-camera motion
    state (both engines) so a scene that changed during the outage reseeds
    instead of producing a spurious first-frame motion event."""
    cam = "motion-backoff-recovery"
    base = np.full((120, 160, 3), 100, dtype=np.uint8)
    ds.detect_frame_motion(cam, base, algorithm="diff")  # seed a background
    assert cam in st._frame_motion_prev

    st.live_detection_failure_count[cam] = 3  # mark the camera as backed off
    ed.clear_live_camera_backoff(cam)

    assert cam not in st._frame_motion_prev
    assert cam not in st._frame_motion_mog2
    assert cam not in st._periodic_scan_last_ts


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
    """A SUSTAINED camera-wide change (exposure/reconnect) must settle to no
    motion within a few frames -- either reseeded by the persistence guard or
    absorbed -- rather than becoming a persistent motion event. (A TRANSIENT
    large change is a passing object and is covered by
    ``test_mog2_transient_large_change_reports_motion``.)"""
    cam = "motion-scene-reset"
    st._frame_motion_scene_streak.pop(cam, None)
    base = np.full((120, 160, 3), 50, dtype=np.uint8)
    _seed_background(cam, base)

    shifted = np.full((120, 160, 3), 200, dtype=np.uint8)  # full-frame change
    results = [ds.detect_frame_motion(cam, shifted)[0] for _ in range(8)]
    assert results[-1] is False, f"sustained scene change must settle quiet, got {results}"

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


def test_mog2_shadow_suppression_drops_cast_shadow_but_not_brightness_change():
    """MOG2 must classify a daylight cast shadow as shadow (127), so the
    suppression switch drops it from the motion mask; disabling the switch
    includes the same pixels as foreground motion."""
    cam_on = "mog2-shadow-on"
    cam_off = "mog2-shadow-off"
    for cam in (cam_on, cam_off):
        for state in (st._frame_motion_mog2, st._frame_motion_mog2_meta,
                      st._frame_motion_scene_streak):
            state.pop(cam, None)

    base = np.full((240, 320, 3), 200, dtype=np.uint8)
    shadow = base.copy()
    shadow[60:190, 50:260] = 105
    for cam, suppression in ((cam_on, 'on'), (cam_off, 'off')):
        for _ in range(5):
            ds.detect_frame_motion(
                cam, base, denoise=False, shadow_suppression=suppression,
            )

    has_shadow_motion, _shadow_conf, shadow_mask, shadow_fraction = ds.detect_frame_motion(
        cam_on, shadow, denoise=False, shadow_suppression='on',
    )
    has_foreground_motion, _foreground_conf, foreground_mask, foreground_fraction = ds.detect_frame_motion(
        cam_off, shadow, denoise=False, shadow_suppression='off',
    )

    assert has_shadow_motion is False
    assert shadow_fraction == 0.0
    assert shadow_mask is not None and not np.any(shadow_mask)
    assert has_foreground_motion is True
    assert foreground_fraction > 0.05
    assert foreground_mask is not None and np.any(foreground_mask)


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


def test_mog2_absorbs_stopped_subject_but_keeps_moving_one():
    """MOG2's native behavior (freeze-on-motion removed):
    - a subject that STOPS (identical frame repeated) is absorbed into the
      background, so a parked car no longer pins the motion signal at ~30%
      forever (the reported stuck-motion-bar bug);
    - a subject that keeps MOVING (new position each frame) stays detected."""
    # Stopped subject -> fades to background.
    cam = "mog2-stop"
    for d in (st._frame_motion_mog2, st._frame_motion_mog2_meta, st._frame_motion_scene_streak):
        d.pop(cam, None)
    base = np.full((240, 320, 3), 60, dtype=np.uint8)
    for _ in range(4):
        ds.detect_frame_motion(cam, base)
    parked = base.copy()
    parked[60:200, 60:180] = 210  # a static "parked car" (~24% of the frame)
    _, first_conf, _, _ = ds.detect_frame_motion(cam, parked)
    assert first_conf > 0.0, "the car must register when it first appears"
    for _ in range(40):
        _, _c, _m, frac = ds.detect_frame_motion(cam, parked)
    assert frac < 0.02, f"a stopped subject must be absorbed, still at {frac}"

    # Moving subject -> keeps firing across frames.
    cam2 = "mog2-move"
    for d in (st._frame_motion_mog2, st._frame_motion_mog2_meta, st._frame_motion_scene_streak):
        d.pop(cam2, None)
    for _ in range(4):
        ds.detect_frame_motion(cam2, base)
    detected = 0
    for i in range(20):
        frame = base.copy()
        x = 20 + i * 12  # a block sweeping across the frame
        frame[100:160, x:x + 40] = 210
        if ds.detect_frame_motion(cam2, frame)[0]:
            detected += 1
    assert detected >= 15, f"a moving subject must keep firing motion, only {detected}/20"


def test_mog2_transient_large_change_reports_motion():
    """A big object sweeping >50% of the frame for a frame or two is reported as
    motion, NOT silently suppressed as a scene reset -- the passing-car fix."""
    cam = "mog2-transient-big"
    for d in (st._frame_motion_mog2, st._frame_motion_mog2_meta, st._frame_motion_scene_streak):
        d.pop(cam, None)
    base = np.full((240, 320, 3), 100, dtype=np.uint8)
    for _ in range(5):
        ds.detect_frame_motion(cam, base)
    big = np.full((240, 320, 3), 240, dtype=np.uint8)  # whole-frame (>50%) change
    has_motion, confidence, _m, _f = ds.detect_frame_motion(cam, big)
    assert has_motion is True and confidence > 0.0, "a transient >50% change must report motion"


def test_diff_engine_background_freezes_during_motion():
    """The LEGACY diff engine still freezes its single running-average background
    while motion is above the gate (pinned to algorithm='diff'). MOG2 no longer
    does -- it absorbs a stopped subject natively, covered by
    ``test_mog2_absorbs_stopped_subject_but_keeps_moving_one``.
    """
    cam = "motion-freeze-diff"
    st._frame_motion_prev.pop(cam, None)
    base = np.full((120, 160, 3), 50, dtype=np.uint8)
    ds.detect_frame_motion(cam, base, algorithm='diff')  # seed the background

    loud = base.copy()
    loud[0:48, :] = 200  # localized 40% change, above gate but below scene-reset

    _, first_conf, _, _ = ds.detect_frame_motion(cam, loud, algorithm='diff')
    assert first_conf > 0.0, "First motion frame must be detected"

    # The diff engine freezes its background during motion, so the diff does not
    # shrink and confidence does not decay across repeated identical frames.
    for _ in range(40):
        _, conf, _, _ = ds.detect_frame_motion(cam, loud, algorithm='diff')

    assert conf == first_conf, (
        f"Diff-engine confidence decayed {first_conf}->{conf} -- freeze broken"
    )
