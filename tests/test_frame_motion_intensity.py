"""Regression tests for ``app.detection_state.detect_frame_motion``.

The /live page's "Live motion" sparkline records the per-frame motion
*intensity* (the fourth return value) rather than the gated alert
*confidence*. Without the ungated intensity, every frame whose motion fell
below the alert gate was logged as exactly ``0.0`` and the chart drew a flat,
unchanging line even when the scene had real sub-gate activity.

These tests exercise ``detect_frame_motion`` directly with synthetic frames
(no live camera ingest required).

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
    has_motion, _conf, _mask, intensity = ds.detect_frame_motion(camera_id, image)
    # The very first frame has no prior background, so it reports no motion and
    # zero intensity by construction.
    assert has_motion is False
    assert intensity == 0.0


def test_detect_frame_motion_returns_four_tuple():
    cam = "motion-shape"
    base = np.full((120, 160, 3), 100, dtype=np.uint8)
    result = ds.detect_frame_motion(cam, base)
    assert len(result) == 4
    has_motion, confidence, _diff_mask, intensity = result
    assert isinstance(has_motion, bool)
    assert isinstance(confidence, float)
    assert isinstance(intensity, float)


def test_sub_gate_motion_keeps_confidence_zero_but_reports_intensity():
    """A few changed pixels (below the alert gate) must NOT alert, but the
    ungated intensity must still be non-zero so the sparkline can animate."""
    cam = "motion-subgate"
    base = np.full((120, 160, 3), 100, dtype=np.uint8)
    _seed_background(cam, base)

    # Change a small patch: 5 px out of 160*120 = ~0.026%, well under the
    # default 0.3% gate.
    nudged = base.copy()
    nudged[0:1, 0:5] = 255
    has_motion, confidence, _diff_mask, intensity = ds.detect_frame_motion(cam, nudged)

    assert has_motion is False
    assert confidence == 0.0  # alert path still ignores noise
    assert intensity > 0.0  # but the chart sees real activity


def test_above_gate_motion_reports_matching_confidence_and_intensity():
    """When motion clears the gate, gated confidence and ungated intensity
    agree (both are the same scaled value)."""
    cam = "motion-abovegate"
    base = np.full((120, 160, 3), 100, dtype=np.uint8)
    _seed_background(cam, base)

    loud = base.copy()
    loud[:, :] = 250  # whole frame changes
    has_motion, confidence, _diff_mask, intensity = ds.detect_frame_motion(cam, loud)

    assert has_motion is True
    assert confidence > 0.0
    assert intensity == confidence


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
    loud[:, :] = 200  # large whole-frame change, well above the default gate

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
