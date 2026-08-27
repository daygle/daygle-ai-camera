"""Regression tests for the three live-alert integration audit fixes.

1. **Per-camera detector confidence floor.** ``compute_minimum_rule_confidence``
   with ``camera_settings`` scopes the ONNX floor to that camera's own rules, so
   a low-threshold rule on one camera can no longer drag every camera's floor
   down. A camera with no enabled object rules falls back to the AI-settings
   confidence (motion rules are always skipped).

2. **Per-label event debounce.** ``live_event_fresh_labels`` gives each label
   its OWN cooldown window: a fast label can fire its own event while a slower
   label on the same camera is still cooling. Motion-only events after a
   non-motion event keep the short trailing suppression window.

3. **Motion Record/Email/Push independence.** A motion detection is stamped
   ``alert_triggered`` ONLY from the motion rule's own Record flag - an enabled
   Email/Push alert fires the alert but must not silently force a recording
   when Record is off.

The app-level test reuses the ``_load_app`` pattern from ``tests/test_api.py``
(config.yaml + app-namespace wipe + explicit ``_startup``), while the unit
tests exercise the extracted modules directly. ``import app.main`` is preloaded
at module top to break the circular-import gate (same pattern as
``tests/test_ai_settings.py``).
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest  # noqa: E402  -- used below

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Top-level preload to break the Phase-N circular-import gate (same pattern as
# tests/test_ai_settings.py): populating sys.modules['app.main'] first lets
# sibling app modules resolve their own ``import app.main`` from the cache.
import app.main as app_main  # noqa: E402  -- must precede the imports below
assert app_main is sys.modules["app.main"]
import app.alert_dispatch as _ad  # noqa: E402
import app.event_debounce as _ed  # noqa: E402
import app.state as _state  # noqa: E402


# ---------------------------------------------------------------------------
# Finding 1: per-camera detector confidence floor
# ---------------------------------------------------------------------------


def test_min_rule_confidence_camera_scoped(monkeypatch):
    """camera_settings scopes the floor to that camera's OWN rules; the global
    no-camera form still returns the lowest across all cameras."""
    camera_a = {'id': 'cam-a', 'detection': {'zones': [{'id': 'z', 'object_rules': [{'label': 'person', 'min_confidence': 0.35, 'enabled': True}]}]}}
    camera_b = {'id': 'cam-b', 'detection': {'zones': [{'id': 'z', 'object_rules': [{'label': 'person', 'min_confidence': 0.10, 'enabled': True}]}]}}
    monkeypatch.setattr(_ad, 'effective_cameras_config', lambda: [camera_a, camera_b])
    monkeypatch.setattr(_ad, 'effective_ai_config', lambda: {'backend': 'onnx', 'confidence': 0.45, 'model_path': 'fake.onnx'})
    _ad._min_rule_confidence_cache = None
    _ad._per_camera_min_rule_confidence_cache.clear()
    try:
        assert _ad.compute_minimum_rule_confidence(camera_settings=camera_a) == pytest.approx(0.35)
        assert _ad.compute_minimum_rule_confidence(camera_settings=camera_b) == pytest.approx(0.10)
        # The global floor still reflects the lowest across all cameras.
        assert _ad.compute_minimum_rule_confidence() == pytest.approx(0.10)
    finally:
        _ad._min_rule_confidence_cache = None
        _ad._per_camera_min_rule_confidence_cache.clear()


def test_min_rule_confidence_camera_scoped_no_rules(monkeypatch):
    """A camera with no enabled object rules falls back to the AI-settings
    confidence for its own floor (motion rules are skipped too)."""
    monkeypatch.setattr(_ad, 'effective_ai_config', lambda: {'backend': 'onnx', 'confidence': 0.42, 'model_path': 'fake.onnx'})
    camera = {'id': 'cam-c', 'detection': {'zones': [{'id': 'z', 'object_rules': [{'label': 'motion', 'min_confidence': 0.05, 'enabled': True}]}]}}
    _ad._min_rule_confidence_cache = None
    _ad._per_camera_min_rule_confidence_cache.clear()
    try:
        assert _ad.compute_minimum_rule_confidence(camera_settings=camera) == pytest.approx(0.42)
    finally:
        _ad._min_rule_confidence_cache = None
        _ad._per_camera_min_rule_confidence_cache.clear()


# ---------------------------------------------------------------------------
# Finding 2: per-label event debounce
# ---------------------------------------------------------------------------


def test_live_event_fresh_labels_per_label_windows():
    """Each label's window is its own: a fast label can fire while a slower
    label on the same camera is still cooling."""
    _state.live_event_last_emitted.clear()
    try:
        # First appearance: everything is fresh.
        assert _ed.live_event_fresh_labels('camera-1', {'person': 60.0, 'motion': 300.0}) == {'person', 'motion'}
        _ed.remember_live_event('camera-1', {'person', 'motion'})
        # Both still inside their own windows: nothing fresh.
        assert _ed.live_event_fresh_labels('camera-1', {'person': 60.0, 'motion': 300.0}) == set()
        # Backdate only person's window past its 60s cooldown.
        with _state.live_event_last_emitted_lock:
            _state.live_event_last_emitted['camera-1']['label_times']['person'] = time.time() - 61
        assert _ed.live_event_fresh_labels('camera-1', {'person': 60.0, 'motion': 300.0}) == {'person'}
        # Backdating the legacy event timestamp also ages every label (min-anchor),
        # so person stays fresh while motion remains inside its 300s window.
        with _state.live_event_last_emitted_lock:
            _state.live_event_last_emitted['camera-1']['timestamp'] = time.time() - 200
        assert _ed.live_event_fresh_labels('camera-1', {'person': 60.0, 'motion': 300.0}) == {'person'}
    finally:
        _state.live_event_last_emitted.clear()


def test_live_event_fresh_labels_motion_trailing_suppression():
    """Motion-only events are suppressed within the short trailing window after a
    non-motion event, mirroring live_event_is_debounced."""
    _state.live_event_last_emitted.clear()
    try:
        _ed.remember_live_event('camera-1', {'person'})
        assert _ed.live_event_fresh_labels('camera-1', {'motion': 60.0}) == set()
        # Beyond the trailing window motion fires independently.
        with _state.live_event_last_emitted_lock:
            _state.live_event_last_emitted['camera-1']['timestamp'] = time.time() - _ed._MOTION_TRAILING_SUPPRESSION_SECONDS - 1
        assert _ed.live_event_fresh_labels('camera-1', {'motion': 60.0}) == {'motion'}
    finally:
        _state.live_event_last_emitted.clear()


# ---------------------------------------------------------------------------
# App harness (mirrors tests/support.py::_load_app)
# ---------------------------------------------------------------------------


def _load_app(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    database_path = tmp_path / "data" / "daygle.sqlite3"
    config_path.write_text(
        f"""
server:
  host: 127.0.0.1
  port: 8080
auth:
  enabled: true
ai:
  backend: onnx
  confidence: 0.45
storage:
  data_dir: {tmp_path / 'data'}
  database: {database_path}
  snapshots_dir: {tmp_path / 'data' / 'snapshots'}
  events_dir: {tmp_path / 'data' / 'events'}
  recordings_dir: {tmp_path / 'data' / 'recordings'}
recording:
  enabled: true
  mode: motion
  continuous: false
  pre_event_seconds: 5
  post_event_seconds: 10
  max_clip_seconds: 60
  format: mp4
  retention_days: 14
  max_storage_gb: 20
  auto_purge_enabled: true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("DAYGLE_CONFIG", str(config_path))
    for mod in list(sys.modules.keys()):
        if mod == "app" or mod.startswith("app."):
            sys.modules.pop(mod, None)
    main_mod = importlib.import_module("app.main")
    main_mod._startup()
    return main_mod, database_path


# ---------------------------------------------------------------------------
# Finding 3: motion Record/Email/Push independence
# ---------------------------------------------------------------------------


def test_motion_alert_does_not_record_when_record_off(tmp_path, monkeypatch):
    """Motion Record/Email/Push are independent channels: an enabled Email/Push
    alert on the motion rule fires the alert but must NOT attach a recording when
    the motion rule's Record flag is off."""
    main, _database_path = _load_app(tmp_path, monkeypatch)
    import app.live_monitor as _lm

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _bytes, confidence=None):
            return []

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    monkeypatch.setattr(_lm, 'detect_frame_motion', lambda *a, **k: (False, 0.0, None, 0.0))

    def _motion_zone_settings(record_on_detect: bool) -> dict:
        return {
            'id': 'camera-1',
            'name': 'Front Door',
            'detection': {
                'zones': [{
                    'id': 'driveway',
                    'name': 'Driveway',
                    'x': 0, 'y': 0, 'width': 1, 'height': 1,
                    'monitor_motion': True,
                    'monitor_objects': False,
                    'object_rules': [{
                        'label': 'motion',
                        'record_on_detect': record_on_detect,
                        'email_enabled': True,
                        'min_confidence': 0.0,
                        'cooldown_seconds': 0,
                    }],
                }],
            },
            'recording': {'continuous': False},
        }

    motion_detection = {'zone_id': 'driveway', 'zone_name': 'Driveway', 'confidence': 0.9, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.4, 'height': 0.4}}
    monkeypatch.setattr(_lm, 'zone_motion_detections', lambda *a, **k: [dict(motion_detection)])

    attached: list[list] = []

    def fake_attach(event_id, event_time, source, detections, camera_id=None, recording_config=None):
        attached.append(list(detections))
        return 'rec-fake'
    monkeypatch.setattr(_lm, 'attach_event_recording', fake_attach)

    def _reset_state() -> None:
        main._state.live_detection_last_checked.clear()
        main._state.live_event_last_emitted.clear()
        main._state._motion_confirm_streaks.clear()
        main.alerts.last_triggered.clear()
        attached.clear()

    # Record OFF + Email ON: alert fires (event created, alert_matched) but NO recording.
    _reset_state()
    settings = _motion_zone_settings(record_on_detect=False)
    first_motion = _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False)
    assert first_motion is None, 'the first motion frame must wait for confirmation'
    event_id = _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False)
    assert event_id is not None
    assert len(attached) == 1
    motion_rec = attached[0][-1]
    assert motion_rec['label'] == 'motion'
    assert motion_rec['alert_triggered'] is False, 'an Email/Push alert must not force a recording when Record is off'
    assert motion_rec['alert_matched'] is True, 'the motion alert itself must still fire'

    # Record ON + Email ON: the same motion now records.
    _reset_state()
    settings = _motion_zone_settings(record_on_detect=True)
    first_motion = _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False)
    assert first_motion is None, 'the first motion frame must wait for confirmation'
    event_id = _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720}, settings, enforce_interval=False)
    assert event_id is not None
    assert len(attached) == 1
    assert attached[0][-1]['alert_triggered'] is True


def test_live_pipeline_captures_unknown_face_for_review(tmp_path, monkeypatch):
    """End-to-end guard for the whole live face pipeline: a detected, unrecognised
    face on the live path is stored in the unknown-faces review queue.

    This reproduces the field bug where ``process_live_stream_alerts`` handed
    ``annotate_face_identities`` the frame-metadata dict instead of the decoded
    numpy image. With no ``.shape`` on that dict, annotation early-returned every
    cycle, so identity was never applied and NO unknown face was ever captured --
    exactly what a user reports as "unknown faces not appearing for review", even
    though face boxes (from the separate detector) still showed.
    """
    import numpy as np

    # Load the app FIRST: _load_app wipes and re-imports the ``app.*`` namespace,
    # so the live-monitor / face-identity modules must be imported afterwards to
    # bind the freshly-started app.state (with its configured database).
    main, _database_path = _load_app(tmp_path, monkeypatch)
    import app.live_monitor as _lm
    import app.face_identity as _fi
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())

    class FakeObjectDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_frame(self, _frame, confidence=None):
            return []  # no COCO objects; faces come from the secondary detector

        def detect_image(self, _bytes, confidence=None):
            return []

    class FakeFaceDetector:
        available = True

        def detect_frame(self, _frame, confidence=None):
            return [{'label': 'face', 'confidence': 0.9,
                     'box': {'x': 0.4, 'y': 0.3, 'width': 0.2, 'height': 0.3}}]

    class FakeRecognition:
        available = True
        auto_enrich_enabled = False
        model_id = 'arcface'
        matcher_generation = 1

        def recognize(self, _crop):
            return None  # unknown -> should be captured for review

        def recognizable(self, _crop):
            return True

        def embed_face(self, _crop):
            return np.ones(512, dtype=np.float32)

    monkeypatch.setattr(main._state, 'detector', FakeObjectDetector())
    monkeypatch.setattr(main._state, 'face_detector', FakeFaceDetector(), raising=False)
    monkeypatch.setattr(_fi, 'get_face_recognition_service', lambda: FakeRecognition())
    monkeypatch.setattr(_lm, 'detect_frame_motion', lambda *a, **k: (False, 0.0, None, 0.0))

    _fi.reset_camera_identities('camera-face')
    settings = {
        'id': 'camera-face', 'name': 'Front Door',
        'detection': {'object_detection_enabled': True, 'zones': []},
        'recording': {'continuous': False},
    }
    # A real numpy frame is the live (RTSP) path -- exactly where recognition
    # must run. ``frame`` is only per-frame metadata.
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    meta = {'width': 1280, 'height': 720, 'timestamp': time.time()}

    assert main.database.count_unknown_faces(status='pending') == 0
    _lm.process_live_stream_alerts(image, meta, settings, enforce_interval=False)

    # The capture stores off the hot path in a background thread; give it a beat.
    for _ in range(40):
        if main.database.count_unknown_faces(status='pending') >= 1:
            break
        time.sleep(0.05)
    assert main.database.count_unknown_faces(status='pending') == 1, (
        'an unrecognised face on the live path must be captured for review'
    )
