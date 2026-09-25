"""Regression tests: the secondary face model must not run on every cycle.

Both the object model and the face model scan the same frame, so an
always-on face pass roughly doubles per-cycle inference for a detection that
may then be thrown away. Two gates now sit in front of it:

* the pass is skipped entirely for a camera nothing can consume faces for
  (no face-detection rule scoped to it, no zone ``face`` rule, face
  recognition off);
* when faces ARE wanted, the pass runs on its own clock
  (``face_detection_interval_seconds``, default 1 s) instead of the object
  detection interval.

Object detection keeps its own cadence and its sub-second alert latency.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.state as _state  # noqa: E402
import app.live_monitor as live_monitor  # noqa: E402
from app.config_facades import DEFAULT_LIVE_CONFIG  # noqa: E402
from app.recording_settings import CAMERA_MOTION_PROFILE_FIELDS  # noqa: E402


class _FakeFaceDetector:
    """Minimal stand-in for the secondary OnnxYoloDetector."""

    available = True

    def __init__(self):
        self.calls: list = []

    def detect_frame(self, image, confidence=None):
        self.calls.append(image)
        return [{'label': 'face', 'confidence': 0.9, 'box': {'x': 0, 'y': 0, 'width': 10, 'height': 10}}]

    def detect_image(self, image, confidence=None):
        return self.detect_frame(image, confidence=confidence)


@pytest.fixture
def face_env(monkeypatch):
    """A camera with no face consumers and an available face detector."""
    detector = _FakeFaceDetector()
    monkeypatch.setattr(_state, 'face_detector', detector)
    monkeypatch.setattr(live_monitor, 'effective_face_detection_rules', lambda: {'rules': []})
    monkeypatch.setattr(live_monitor, 'effective_face_recognition_config', lambda: {'enabled': False})
    monkeypatch.setattr(_state, 'face_detection_last_checked', {})
    return detector


def _camera(**detection):
    return {'id': 'front', 'name': 'Front', 'detection': detection}


# ─── The "is anybody using faces here?" gate ───────────────────────────────


def test_face_pass_is_skipped_when_nothing_consumes_faces(face_env):
    merged = live_monitor.merge_secondary_face_detections(
        b'frame', [{'label': 'person'}], camera_id='front', settings=_camera(),
    )
    assert merged == [{'label': 'person'}]
    assert face_env.calls == [], 'the face model must not run for an idle camera'


def test_zone_face_rule_keeps_the_face_pass_enabled(face_env):
    settings = _camera(zones=[{
        'id': 'door', 'enabled': True,
        'object_rules': [{'label': 'face', 'enabled': True}],
    }])
    assert live_monitor.camera_uses_face_detections('front', settings) is True
    merged = live_monitor.merge_secondary_face_detections(
        b'frame', [], camera_id='front', settings=settings, live_settings={},
    )
    assert [d['label'] for d in merged] == ['face']


def test_disabled_zone_face_rule_does_not_count(face_env):
    settings = _camera(zones=[{
        'id': 'door', 'enabled': True,
        'object_rules': [{'label': 'face', 'enabled': False}],
    }])
    assert live_monitor.camera_uses_face_detections('front', settings) is False


def test_enabled_face_rule_for_this_camera_keeps_the_pass_enabled(face_env, monkeypatch):
    monkeypatch.setattr(live_monitor, 'effective_face_detection_rules', lambda: {'rules': [
        {'id': 'r1', 'name': 'Alice', 'enabled': True, 'camera_id': 'front'},
    ]})
    assert live_monitor.camera_uses_face_detections('front', _camera()) is True


def test_face_rule_scoped_to_another_camera_does_not_count(face_env, monkeypatch):
    monkeypatch.setattr(live_monitor, 'effective_face_detection_rules', lambda: {'rules': [
        {'id': 'r1', 'name': 'Alice', 'enabled': True, 'camera_id': 'driveway'},
    ]})
    assert live_monitor.camera_uses_face_detections('front', _camera()) is False


def test_disabled_face_rule_does_not_count(face_env, monkeypatch):
    monkeypatch.setattr(live_monitor, 'effective_face_detection_rules', lambda: {'rules': [
        {'id': 'r1', 'name': 'Alice', 'enabled': False},
    ]})
    assert live_monitor.camera_uses_face_detections('front', _camera()) is False


def test_face_recognition_enabled_keeps_the_pass_enabled(face_env, monkeypatch):
    """Identities are stamped on events and unknown faces are captured for
    review whether or not any rule alerts on them, so an enabled, loaded
    recognition model keeps faces flowing."""
    monkeypatch.setattr(live_monitor, 'effective_face_recognition_config', lambda: {'enabled': True})
    import app.face_recognition_service as recognition

    class _Available:
        available = True

    monkeypatch.setattr(recognition, 'get_face_recognition_service', lambda: _Available())
    assert live_monitor.camera_uses_face_detections('front', _camera()) is True


def test_unavailable_recognition_model_does_not_keep_the_pass_enabled(face_env, monkeypatch):
    monkeypatch.setattr(live_monitor, 'effective_face_recognition_config', lambda: {'enabled': True})
    import app.face_recognition_service as recognition

    class _Unavailable:
        available = False

    monkeypatch.setattr(recognition, 'get_face_recognition_service', lambda: _Unavailable())
    assert live_monitor.camera_uses_face_detections('front', _camera()) is False


# ─── The interval ─────────────────────────────────────────────────────────


def test_face_pass_runs_at_its_own_slower_cadence(face_env, monkeypatch):
    monkeypatch.setattr(live_monitor, 'effective_face_detection_rules', lambda: {'rules': [
        {'id': 'r1', 'name': 'Alice', 'enabled': True},
    ]})
    now = 1000.0
    monkeypatch.setattr(live_monitor.time, 'time', lambda: now)

    live_monitor.merge_secondary_face_detections(b'a', [], camera_id='front', settings=_camera(), live_settings={})
    assert len(face_env.calls) == 1

    # Object cycles keep coming (0.5 s apart) but the face model does not.
    now += 0.5
    live_monitor.merge_secondary_face_detections(b'b', [], camera_id='front', settings=_camera(), live_settings={})
    assert len(face_env.calls) == 1, 'face pass must be throttled to its own interval'

    now += 0.6
    live_monitor.merge_secondary_face_detections(b'c', [], camera_id='front', settings=_camera(), live_settings={})
    assert len(face_env.calls) == 2


def test_face_interval_is_configurable(face_env, monkeypatch):
    monkeypatch.setattr(live_monitor, 'effective_face_detection_rules', lambda: {'rules': [
        {'id': 'r1', 'name': 'Alice', 'enabled': True},
    ]})
    now = 2000.0
    monkeypatch.setattr(live_monitor.time, 'time', lambda: now)
    slow = {'face_detection_interval_seconds': 5.0}

    live_monitor.merge_secondary_face_detections(b'a', [], camera_id='front', settings=_camera(), live_settings=slow)
    now += 1.0
    live_monitor.merge_secondary_face_detections(b'b', [], camera_id='front', settings=_camera(), live_settings=slow)
    assert len(face_env.calls) == 1
    now += 4.5
    live_monitor.merge_secondary_face_detections(b'c', [], camera_id='front', settings=_camera(), live_settings=slow)
    assert len(face_env.calls) == 2


def test_interval_state_is_per_camera(face_env, monkeypatch):
    monkeypatch.setattr(live_monitor, 'effective_face_detection_rules', lambda: {'rules': [
        {'id': 'r1', 'name': 'Alice', 'enabled': True},
    ]})
    live_monitor.merge_secondary_face_detections(b'a', [], camera_id='front', settings=_camera(), live_settings={})
    live_monitor.merge_secondary_face_detections(b'b', [], camera_id='garage', settings={'id': 'garage', 'detection': {}}, live_settings={})
    assert len(face_env.calls) == 2, 'one camera\'s throttled pass must not starve another'


def test_callers_without_a_camera_id_keep_running_every_cycle(face_env):
    """The snapshot/event paths pass no camera id; the historical
    unconditional behaviour must be preserved for them."""
    for _ in range(3):
        merged = live_monitor.merge_secondary_face_detections(b'frame', [])
        assert [d['label'] for d in merged] == ['face']
    assert len(face_env.calls) == 3


def test_missing_or_broken_interval_falls_back_to_the_default(face_env, monkeypatch):
    monkeypatch.setattr(live_monitor, 'effective_face_detection_rules', lambda: {'rules': [
        {'id': 'r1', 'name': 'Alice', 'enabled': True},
    ]})
    now = 3000.0
    monkeypatch.setattr(live_monitor.time, 'time', lambda: now)
    live_monitor.merge_secondary_face_detections(b'a', [], camera_id='front', settings=_camera(), live_settings=None)
    now += 0.2
    live_monitor.merge_secondary_face_detections(b'b', [], camera_id='front', settings=_camera(), live_settings={'face_detection_interval_seconds': 'nonsense'})
    assert len(face_env.calls) == 1


# ─── The setting itself ────────────────────────────────────────────────────


def test_face_interval_default_is_one_second():
    # 1 Hz face inference on top of the (default) 2 Hz object pass: half the
    # face-model cost, with object alerts still sub-second.
    assert DEFAULT_LIVE_CONFIG['face_detection_interval_seconds'] == 1.0


def test_face_interval_is_a_per_camera_profile_field():
    assert 'face_detection_interval_seconds' in CAMERA_MOTION_PROFILE_FIELDS


def test_face_interval_survives_settings_validation():
    from fastapi import HTTPException

    from app.payload_validators import validate_live_settings

    assert validate_live_settings({'face_detection_interval_seconds': 2.5})['face_detection_interval_seconds'] == 2.5
    with pytest.raises(HTTPException):
        validate_live_settings({'face_detection_interval_seconds': 0.01})
    with pytest.raises(HTTPException):
        validate_live_settings({'face_detection_interval_seconds': 30})
    with pytest.raises(HTTPException):
        validate_live_settings({'face_detection_interval_seconds': 'soon'})


def test_face_interval_is_clamped_by_the_camera_profile():
    from app.recording_settings import normalize_camera_detection_profiles

    profiles = normalize_camera_detection_profiles(
        {'active': 'day', 'day': {'face_detection_interval_seconds': 99}, 'night': {'face_detection_interval_seconds': 0.0}},
        {},
    )
    assert profiles['day']['face_detection_interval_seconds'] == 10.0
    # 0.0 is falsy-but-present: it clamps up to the floor rather than being
    # dropped, so a day profile can pin the face pass to every cycle.
    assert profiles['night']['face_detection_interval_seconds'] == 0.1


def test_face_pass_stamp_does_not_disturb_object_detection_cadence(face_env, monkeypatch):
    """The two cadences must not share a clock: throttling faces cannot slow
    (or speed up) the object-detection interval gate."""
    monkeypatch.setattr(live_monitor, 'effective_face_detection_rules', lambda: {'rules': [
        {'id': 'r1', 'name': 'Alice', 'enabled': True},
    ]})
    object_stamp = time.time()
    monkeypatch.setattr(_state, 'live_detection_last_checked', {'front': object_stamp})
    live_monitor.merge_secondary_face_detections(b'a', [], camera_id='front', settings=_camera(), live_settings={})
    assert _state.live_detection_last_checked['front'] == object_stamp, 'object cadence must be untouched'
    assert 'front' in _state.face_detection_last_checked
