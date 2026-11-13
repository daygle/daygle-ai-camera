"""Tests for ``app/camera_lifecycle.py`` Pool A back-compat identity.

Confirms that all four lifecycle helpers re-exported via Pool A in
``app/main.py`` point to the same function objects as their canonical
homes in ``app/camera_lifecycle.py``, and that the ``_state.*``
callback slots are wired to the same functions.

Import order follows the anti-circular-import pattern established in
``test_camera_config.py``: load ``app.main`` first so the module is
fully initialized before ``app.camera_lifecycle`` resolves its own
``import app.state as _state`` reach.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.main as app_main  # noqa: E402  -- must precede the import below
import app.camera_lifecycle as cl_mod  # noqa: E402
assert app_main is sys.modules["app.main"]


@pytest.fixture
def main():
    assert app_main is not None
    return sys.modules["app.main"]


@pytest.fixture
def cl():
    assert cl_mod is not None
    return sys.modules["app.camera_lifecycle"]


@pytest.fixture
def state():
    return sys.modules["app.state"]


# ---------------------------------------------------------------------------
# Pool A identity: main.<name> is camera_lifecycle.<name>
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _state callback slots: _state.<name> is camera_lifecycle.<name>
# ---------------------------------------------------------------------------

def test_state_camera_event_recording_config_is_lifecycle(state, cl):
    assert state.camera_event_recording_config is cl.camera_event_recording_config


def test_state_apply_cameras_settings_is_lifecycle(state, cl):
    assert state.apply_cameras_settings is cl.apply_cameras_settings


def test_state_apply_storage_and_recording_settings_is_lifecycle(state, cl):
    assert state.apply_storage_and_recording_settings is cl.apply_storage_and_recording_settings


def test_state_reload_detector_is_lifecycle(state, cl):
    assert state.reload_detector is cl.reload_detector


# ---------------------------------------------------------------------------
# _state.camera is initialized
# ---------------------------------------------------------------------------

def test_state_camera_attribute_exists(state):
    """``_state.camera`` must exist (set at main.py module load); may be
    None if no cameras are configured, but must not be missing entirely."""
    assert hasattr(state, 'camera'), "_state.camera attribute not found"


# ---------------------------------------------------------------------------
# Deleted cameras must not leave entries in the per-camera runtime dicts
# ---------------------------------------------------------------------------

def test_cleanup_camera_runtime_state_removes_all_dicts(state):
    """Removing a camera clears every per-camera runtime dict, so deleted
    camera ids cannot accumulate in-memory state for the life of the process."""
    from collections import deque
    import app.detection_state as detection_state
    from app.camera_lifecycle import _cleanup_camera_runtime_state

    # The per-camera motion-gate log throttle lives in app.detection_state, not
    # app.state; it is the one per-camera map that used to survive removal.
    detection_state._motion_log_last_at['cam-old'] = 1.0
    with state.live_detection_history_lock:
        state.live_detection_history['cam-old'] = deque(maxlen=10)
    with state.live_detection_confirm_lock:
        state.live_detection_confirm_history['cam-old'] = deque(maxlen=10)
    with state.live_detection_status_lock:
        state.live_detection_status['cam-old'] = {'state': 'checked'}
    with state.live_event_presence_lock:
        state.live_event_presence['cam-old'] = {'person': {'high': 1, 'pending_cycles': 0, 'last_seen': 1.0}}
    with state._still_dwell_lock:
        state._still_dwell['cam-old'] = {'person': {'still_since': 1.0, 'alerted': False}}
    with state._object_tracks_lock:
        state._object_tracks['cam-old'] = {'tracks': [], 'next_id': 1}
    with state._motion_confirm_lock:
        state._motion_confirm_streaks['cam-old'] = {'zone-1': 2}
    state._frame_motion_prev['cam-old'] = 'bg'
    state._frame_motion_last_frame['cam-old'] = 'f'
    state._frame_motion_last_gray['cam-old'] = 'g'
    state._frame_motion_mog2['cam-old'] = 'mog2'
    state._frame_motion_mog2_meta['cam-old'] = ('sig',)
    state._frame_motion_scene_streak['cam-old'] = 3
    state._frame_motion_shadow_mask['cam-old'] = object()
    state._frame_motion_error_cameras.add('cam-old')
    with state._live_backoff_lock:
        state.live_detection_retry_after['cam-old'] = 999.0
        state.live_detection_failure_count['cam-old'] = 2
    with state.live_detection_worker_lock:
        state.live_detection_last_checked['cam-old'] = 1.0
        state.active_live_detection_cameras.add('cam-old')
    with state._sound_statuses_lock:
        state._sound_statuses['cam-old'] = {'state': 'listening'}
    state._periodic_scan_last_ts['cam-old'] = 1.0
    state.motion_object_arbiter.resolve(
        'cam-old', now=1.0, grace_seconds=3.0,
        motion_detections=[{'zone_id': 'z', 'box': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}],
        object_zone_keys={'other'}, image=None, image_is_numpy=False, has_objects=False,
    )
    assert state.motion_object_arbiter.has_held('cam-old')

    _cleanup_camera_runtime_state({'cam-old'})

    assert not state.motion_object_arbiter.has_held('cam-old')

    assert 'cam-old' not in state.live_detection_history
    assert 'cam-old' not in state.live_detection_confirm_history
    assert 'cam-old' not in state.live_detection_status
    assert 'cam-old' not in state.live_event_presence
    assert 'cam-old' not in state._still_dwell
    assert 'cam-old' not in state._object_tracks
    assert 'cam-old' not in state._motion_confirm_streaks
    assert 'cam-old' not in state._frame_motion_prev
    assert 'cam-old' not in state._frame_motion_last_frame
    assert 'cam-old' not in state._frame_motion_last_gray
    assert 'cam-old' not in state._frame_motion_mog2
    assert 'cam-old' not in state._frame_motion_mog2_meta
    assert 'cam-old' not in state._frame_motion_scene_streak
    assert 'cam-old' not in state._frame_motion_shadow_mask
    assert 'cam-old' not in state._frame_motion_error_cameras
    assert 'cam-old' not in state.live_detection_retry_after
    assert 'cam-old' not in state.live_detection_failure_count
    assert 'cam-old' not in state.live_detection_last_checked
    assert 'cam-old' not in state.active_live_detection_cameras
    assert 'cam-old' not in state._sound_statuses
    assert 'cam-old' not in state._periodic_scan_last_ts
    assert 'cam-old' not in detection_state._motion_log_last_at


def test_cleanup_camera_runtime_state_keeps_other_cameras(state):
    import app.detection_state as detection_state
    from app.camera_lifecycle import _cleanup_camera_runtime_state

    with state.live_detection_history_lock:
        state.live_detection_history['cam-keep'] = 'data'
        state.live_detection_history['cam-old'] = 'data'
    with state._still_dwell_lock:
        state._still_dwell['cam-keep'] = {'package': {'still_since': 1.0, 'alerted': True}}
    detection_state._motion_log_last_at['cam-keep'] = 1.0
    detection_state._motion_log_last_at['cam-old'] = 1.0

    _cleanup_camera_runtime_state({'cam-old'})

    assert 'cam-keep' in state.live_detection_history
    assert 'cam-keep' in state._still_dwell
    assert 'cam-keep' in detection_state._motion_log_last_at
    assert 'cam-old' not in state.live_detection_history
    assert 'cam-old' not in detection_state._motion_log_last_at


# ---------------------------------------------------------------------------
# Settings-apply exception safety
# ---------------------------------------------------------------------------

def test_apply_storage_settings_failure_keeps_old_service_published(cl, state, monkeypatch):
    """A failing RecordingService construction must not orphan the sentinel.

    Publishing ``_SentinelRecordingService`` before constructing the new
    service meant an unwritable recordings dir (or full disk) left the
    sentinel published forever: every ingest prime / should_record became a
    silent no-op and recording stopped until restart."""
    class _OldService:
        def stop_prebuffer_workers(self):
            recorders.append('stop_prebuffer')

        def stop_all_continuous_recordings(self):
            recorders.append('stop_continuous')

    recorders = []
    old_service = _OldService()
    monkeypatch.setattr(state, 'recording_service', old_service)

    def _boom(_config):
        raise OSError('recordings directory is not writable')

    monkeypatch.setattr(cl, 'RecordingService', _boom)
    monkeypatch.setattr(cl, 'log_camera_diagnostic', lambda *a, **k: None)

    with pytest.raises(OSError):
        cl.apply_storage_and_recording_settings()

    assert state.recording_service is old_service, (
        'old service must stay published when the new one cannot be built'
    )
    assert not isinstance(state.recording_service, cl._SentinelRecordingService)
    assert recorders == [], 'old workers must not be torn down for a failed swap'


def test_reload_detector_construction_failure_publishes_error_not_sentinel(cl, state, monkeypatch):
    """A raising create_detector must leave a failing detector, not the
    generic "reloading" sentinel that never resolves."""
    monkeypatch.setattr(state, 'detector', None)
    monkeypatch.setattr(state, 'face_detector', None)

    def _boom(_settings):
        raise RuntimeError('model file is corrupt')

    monkeypatch.setattr(cl, 'create_detector', _boom)
    monkeypatch.setattr(cl, 'log_detector_initialization', lambda *_a, **_k: None)

    ok, error = cl.reload_detector({'backend': 'onnx'})

    assert ok is False
    assert 'model file is corrupt' in (error or '')
    detector = state.detector
    assert not isinstance(detector, cl._SentinelDetector) or 'failed to load' in (
        detector.unavailable_reason or ''
    ), 'sentinel left in place would report "reloading" forever'
    assert getattr(detector, 'available', True) is False
