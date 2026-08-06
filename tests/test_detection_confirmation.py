"""Tests for the temporal N-of-M confirmation gate
(``app.detection_state.confirm_object_detections``).

The gate suppresses object detections until their label has persisted across
several detection cycles, filtering transient single-frame false positives
while remaining a pass-through no-op at the default ``required_frames=1``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def ds():
    from app import detection_state as _ds
    return _ds


@pytest.fixture(autouse=True)
def _clear_confirm_state():
    """Each test starts with an empty per-camera confirmation history."""
    import app.state as _state
    with _state.live_detection_confirm_lock:
        _state.live_detection_confirm_history.clear()
    yield
    with _state.live_detection_confirm_lock:
        _state.live_detection_confirm_history.clear()


def _det(label: str) -> dict:
    return {'label': label, 'confidence': 0.9, 'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1}}


def test_required_one_is_pass_through_no_state(ds):
    import app.state as _state
    detections = [_det('cat'), _det('person')]
    out = ds.confirm_object_detections('cam-1', detections, required_frames=1, window_frames=3)
    # Same list contents, and no per-camera state accumulated (feature off).
    assert out == detections
    assert 'cam-1' not in _state.live_detection_confirm_history


def test_two_of_three_confirms_on_second_consecutive_cycle(ds):
    # First cycle: 'cat' seen once -> below the 2-frame requirement -> held.
    assert ds.confirm_object_detections('cam-1', [_det('cat')], required_frames=2, window_frames=3) == []
    # Second consecutive cycle: 'cat' now seen in 2 of the last 3 cycles -> passes.
    out = ds.confirm_object_detections('cam-1', [_det('cat')], required_frames=2, window_frames=3)
    assert [d['label'] for d in out] == ['cat']


def test_transient_single_frame_label_is_suppressed(ds):
    # A one-off blip that never repeats within the window is never confirmed.
    assert ds.confirm_object_detections('cam-1', [_det('cat')], required_frames=2, window_frames=2) == []
    # Next cycle sees a DIFFERENT label; the stale 'cat' rolls out of the 2-wide
    # window and is never confirmed.
    assert ds.confirm_object_detections('cam-1', [_det('dog')], required_frames=2, window_frames=2) == []


def test_labels_are_independent(ds):
    # 'person' persists across two cycles; 'cat' appears only once.
    ds.confirm_object_detections('cam-1', [_det('person'), _det('cat')], required_frames=2, window_frames=3)
    out = ds.confirm_object_detections('cam-1', [_det('person')], required_frames=2, window_frames=3)
    assert [d['label'] for d in out] == ['person']


def test_cameras_are_isolated(ds):
    ds.confirm_object_detections('cam-1', [_det('cat')], required_frames=2, window_frames=3)
    # A first-ever cycle on cam-2 has no shared history with cam-1.
    assert ds.confirm_object_detections('cam-2', [_det('cat')], required_frames=2, window_frames=3) == []


def test_window_smaller_than_required_is_clamped(ds):
    # window_frames < required_frames would be unsatisfiable; the helper clamps
    # the window up so confirmation can still succeed.
    assert ds.confirm_object_detections('cam-1', [_det('cat')], required_frames=3, window_frames=1) == []
    ds.confirm_object_detections('cam-1', [_det('cat')], required_frames=3, window_frames=1)
    out = ds.confirm_object_detections('cam-1', [_det('cat')], required_frames=3, window_frames=1)
    assert [d['label'] for d in out] == ['cat']


def test_invalid_params_fall_back_safely(ds):
    # Non-numeric required disables the gate (treated as 1).
    detections = [_det('cat')]
    assert ds.confirm_object_detections('cam-1', detections, required_frames='x', window_frames='y') == detections


def test_window_resize_preserves_recent_cycles(ds):
    import app.state as _state
    ds.confirm_object_detections('cam-1', [_det('cat')], required_frames=2, window_frames=3)
    # Resize the window; the existing deque is rebuilt with the new maxlen while
    # keeping recent cycles, so the second 'cat' cycle still confirms.
    out = ds.confirm_object_detections('cam-1', [_det('cat')], required_frames=2, window_frames=4)
    assert [d['label'] for d in out] == ['cat']
    assert _state.live_detection_confirm_history['cam-1'].maxlen == 4
