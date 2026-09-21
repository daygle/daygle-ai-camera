"""Regression guard: object tracking must run on the SAME detection list the
moving/still filter consumes.

Field symptom: cats (and any stop-and-go subject) flicker in and out of
detection. Cause: ``process_live_stream_alerts`` stamped track ids on a
camera-filtered copy that was then discarded, so the tracker's
``track_displacement`` annotation never reached ``filter_detections_by_motion_mode``.
With the annotation missing, the filter falls back to the per-frame pixel mask,
which reads a momentarily-still subject as ``still`` -- and the default
Moving Only mode drops it. A cat that pauses between steps is therefore lost on
every quiet frame.

These tests pin the wiring (tracking feeds the filter) and the user-visible
outcome (a paused-but-recently-moving detection survives Moving Only).
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.main as app_main  # noqa: E402  -- break the circular-import gate
assert app_main is sys.modules["app.main"]


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
  enabled: false
  mode: motion
  continuous: false
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("DAYGLE_CONFIG", str(config_path))
    for mod in list(sys.modules.keys()):
        if mod == "app" or mod.startswith("app."):
            sys.modules.pop(mod, None)
    main_mod = importlib.import_module("app.main")
    main_mod._startup()
    return main_mod


def _cat_camera_settings() -> dict:
    # Object detection on, no zones and no allow-list -> "accept all" object
    # path, so the only gate the cat must clear is the moving/still filter.
    return {
        'id': 'camera-cat',
        'name': 'Driveway',
        'detection': {'object_detection_enabled': True, 'zones': []},
        'recording': {'continuous': False},
    }


def _still_cat_detector():
    cat = {'label': 'cat', 'confidence': 0.8,
           'box': {'x': 0.45, 'y': 0.55, 'width': 0.08, 'height': 0.06}}

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _bytes, confidence=None):
            return [dict(cat, box=dict(cat['box']))]

    return FakeDetector()


def _run_still_cat(main, monkeypatch, camera_settings):
    """One cycle with a still cat and no motion; return the status detections."""
    import app.live_monitor as _lm
    monkeypatch.setattr(main._state, 'detector', _still_cat_detector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    # No motion -> diff_mask is None -> a first-seen cat classifies "still".
    monkeypatch.setattr(_lm, 'detect_frame_motion', lambda *a, **k: (False, 0.0, None, 0.0))
    captured: dict = {}
    real_status = _lm.update_live_detection_status

    def capturing_status(camera_id, **kwargs):
        captured['detections'] = kwargs.get('detections')
        return real_status(camera_id, **kwargs)
    monkeypatch.setattr(_lm, 'update_live_detection_status', capturing_status)
    _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720, 'timestamp': time.time()},
                                   camera_settings, enforce_interval=False)
    return [d for d in (captured.get('detections') or []) if d.get('label') == 'cat']


def test_camera_profile_does_not_override_object_mode(tmp_path, monkeypatch):
    """Moving/still mode is resolved from the Objects page, not a camera profile."""
    main = _load_app(tmp_path, monkeypatch)
    settings = _cat_camera_settings()
    settings['detection_profiles'] = {'active': 'day', 'day': {'object_detection_motion_mode': 'any'}}
    status_cats = _run_still_cat(main, monkeypatch, settings)
    assert not status_cats, 'a camera profile must not override the object setting'


def test_global_moving_only_drops_a_still_cat(tmp_path, monkeypatch):
    """The global Moving Only object setting drops the same still cat."""
    main = _load_app(tmp_path, monkeypatch)
    status_cats = _run_still_cat(main, monkeypatch, _cat_camera_settings())
    assert not status_cats, 'the global Moving Only default drops a still cat'


def _resolved_allow_auto_detection(main, monkeypatch, camera_settings):
    """Run one cycle and capture the ``allow_auto_detection`` the pipeline
    resolved from the camera's ptz_motion_detection switch + PTZ-enabled flag."""
    import app.live_monitor as _lm
    monkeypatch.setattr(main._state, 'detector', _still_cat_detector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    monkeypatch.setattr(_lm, 'detect_frame_motion', lambda *a, **k: (False, 0.0, None, 0.0))
    captured: dict = {}
    real = _lm.update_camera_motion

    def capturing(camera_id, fraction, *, allow_auto_detection=True):
        captured['allow'] = allow_auto_detection
        return real(camera_id, fraction, allow_auto_detection=allow_auto_detection)
    monkeypatch.setattr(_lm, 'update_camera_motion', capturing)
    _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720, 'timestamp': time.time()},
                                   camera_settings, enforce_interval=False)
    return captured.get('allow')


def test_ptz_motion_detection_switch_resolves_correctly(tmp_path, monkeypatch):
    """The per-camera ptz_motion_detection switch decides whether the auto
    ego-motion heuristic runs: on -> always, off -> never, auto -> follow PTZ."""
    main = _load_app(tmp_path, monkeypatch)

    def _settings(cam_id, mode, *, ptz_enabled):
        s = _cat_camera_settings()
        s['id'] = cam_id
        s['detection']['ptz_motion_detection'] = mode
        if ptz_enabled:
            s['ptz'] = {'enabled': True}
        return s

    # 'on' runs auto-detection even on a fixed camera; 'off' disables it even on
    # a PTZ camera; 'auto' follows the PTZ-enabled flag either way.
    assert _resolved_allow_auto_detection(main, monkeypatch, _settings('c-on', 'on', ptz_enabled=False)) is True
    assert _resolved_allow_auto_detection(main, monkeypatch, _settings('c-off', 'off', ptz_enabled=True)) is False
    assert _resolved_allow_auto_detection(main, monkeypatch, _settings('c-auto-fixed', 'auto', ptz_enabled=False)) is False
    assert _resolved_allow_auto_detection(main, monkeypatch, _settings('c-auto-ptz', 'auto', ptz_enabled=True)) is True


def test_tracking_feeds_the_motion_mode_filter(tmp_path, monkeypatch):
    """The detections handed to ``filter_detections_by_motion_mode`` must carry
    the tracker's annotation, and a paused (mask-still) but tracked-moving cat
    must survive the default Moving Only mode."""
    main = _load_app(tmp_path, monkeypatch)
    import app.live_monitor as _lm

    cat = {'label': 'cat', 'confidence': 0.8,
           'box': {'x': 0.45, 'y': 0.55, 'width': 0.08, 'height': 0.06}}

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _bytes, confidence=None):
            return [dict(cat, box=dict(cat['box']))]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    # No motion this frame: diff_mask is None, so the mask verdict alone would
    # classify the cat "still" and Moving Only would drop it.
    monkeypatch.setattr(_lm, 'detect_frame_motion', lambda *a, **k: (False, 0.0, None, 0.0))

    # Stand in for the real tracker: stamp a net-displacement large enough to be
    # classified "moving", plus a sentinel we can assert reached the filter.
    def fake_tracks(camera_id, detections, **_kwargs):
        return [{**d, 'track_id': 7, 'track_displacement': 0.5, '_tracked': True} for d in detections]
    monkeypatch.setattr(_lm, 'update_object_tracks', fake_tracks)

    seen_by_filter: list[list[dict]] = []
    real_filter = _lm.filter_detections_by_motion_mode

    def capturing_filter(detections, diff_mask, settings=None, **kwargs):
        seen_by_filter.append([dict(d) for d in detections])
        return real_filter(detections, diff_mask, settings, **kwargs)
    monkeypatch.setattr(_lm, 'filter_detections_by_motion_mode', capturing_filter)

    captured_status: dict = {}
    real_status = _lm.update_live_detection_status

    def capturing_status(camera_id, **kwargs):
        captured_status['detections'] = kwargs.get('detections')
        return real_status(camera_id, **kwargs)
    monkeypatch.setattr(_lm, 'update_live_detection_status', capturing_status)

    _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720, 'timestamp': time.time()},
                                   _cat_camera_settings(), enforce_interval=False)

    assert seen_by_filter, 'the moving/still filter must run on the object path'
    cat_inputs = [d for d in seen_by_filter[-1] if d.get('label') == 'cat']
    assert cat_inputs, 'the cat detection must reach the moving/still filter'
    assert all(d.get('_tracked') for d in cat_inputs), (
        'tracking must run BEFORE the moving/still filter so track_displacement '
        'is available to it (regression: tracks were stamped on a discarded list)'
    )

    # The user-visible outcome: the paused cat is classified moving from its
    # track history and is NOT dropped by the default Moving Only mode.
    status_cats = [d for d in (captured_status.get('detections') or []) if d.get('label') == 'cat']
    assert status_cats, 'a tracked, recently-moving cat must survive Moving Only'
    assert status_cats[0].get('motion_state') == 'moving'


def test_paused_cat_is_dropped_without_track_displacement(tmp_path, monkeypatch):
    """Control: with NO track_displacement (the regression), the same quiet-frame
    cat is classified still and dropped by Moving Only -- proving the annotation
    is what saves it."""
    main = _load_app(tmp_path, monkeypatch)
    import app.live_monitor as _lm

    cat = {'label': 'cat', 'confidence': 0.8,
           'box': {'x': 0.45, 'y': 0.55, 'width': 0.08, 'height': 0.06}}

    class FakeDetector:
        backend = 'onnx'
        available = True
        unavailable_reason = None

        def detect_image(self, _bytes, confidence=None):
            return [dict(cat, box=dict(cat['box']))]

    monkeypatch.setattr(main._state, 'detector', FakeDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    monkeypatch.setattr(_lm, 'detect_frame_motion', lambda *a, **k: (False, 0.0, None, 0.0))
    # Tracker that leaves the annotation off (too-young track): mask governs.
    monkeypatch.setattr(_lm, 'update_object_tracks',
                        lambda camera_id, detections, **_k: list(detections))

    captured_status: dict = {}
    real_status = _lm.update_live_detection_status

    def capturing_status(camera_id, **kwargs):
        captured_status['detections'] = kwargs.get('detections')
        return real_status(camera_id, **kwargs)
    monkeypatch.setattr(_lm, 'update_live_detection_status', capturing_status)

    _lm.process_live_stream_alerts(b'frame', {'width': 1280, 'height': 720, 'timestamp': time.time()},
                                   _cat_camera_settings(), enforce_interval=False)

    status_cats = [d for d in (captured_status.get('detections') or []) if d.get('label') == 'cat']
    assert not status_cats, (
        'without a displacement annotation a still-mask cat is Moving-Only dropped '
        '(this is exactly why tracking must run before the filter)'
    )
