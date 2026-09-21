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
