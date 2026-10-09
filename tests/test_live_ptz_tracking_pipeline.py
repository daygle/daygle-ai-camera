"""PTZ auto-tracking wired into the live detection cycle."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

import app.main as app_main  # noqa: E402  -- break the circular-import gate
assert app_main is sys.modules["app.main"]

from tests.test_live_track_ordering import _load_app  # noqa: E402


class _CatDetector:
    backend = 'onnx'
    available = True
    unavailable_reason = None

    def detect_frame(self, image, confidence=None):
        return [{'label': 'cat', 'confidence': 0.9, 'box': {'x': 0.8, 'y': 0.4, 'width': 0.1, 'height': 0.1}}]


def _camera():
    return {
        'id': 'camera-ptz', 'name': 'Garden', 'host': '192.0.2.40',
        'detection': {'object_detection_enabled': True, 'zones': []},
        'recording': {'continuous': False},
        'ptz': {'enabled': True, 'protocol': 'onvif', 'auto_track': {'enabled': True, 'labels': ['cat']}},
    }


def test_cycle_hands_detections_to_the_tracker_and_publishes_status(tmp_path, monkeypatch):
    main = _load_app(tmp_path, monkeypatch)
    import app.live_monitor as _lm
    import app.ptz_tracking as pt

    monkeypatch.setattr(main._state, 'detector', _CatDetector())
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    monkeypatch.setattr(_lm, 'detect_frame_motion', lambda *a, **k: (False, 0.0, None, 0.0))
    monkeypatch.setattr(_lm, 'filter_detections_by_motion_mode', lambda detections, *a, **k: detections)

    moves = []

    class _Inline:
        def submit(self, fn):
            fn()

    monkeypatch.setattr(pt, '_get_executor', lambda: _Inline())
    monkeypatch.setattr(pt, '_default_mover', lambda action, camera_id, conn, *args: moves.append((action, camera_id, conn.host, *args)))
    pt.reset_auto_tracking()

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    for offset in range(3):
        _lm.process_live_stream_alerts(
            frame, {'width': 1280, 'height': 720, 'timestamp': time.time() + offset},
            _camera(), enforce_interval=False,
        )

    # The cat is right of centre: once its track is old enough, the camera pans right.
    assert moves, 'the tracker should have steered the camera'
    action, camera_id, host, pan, tilt, zoom, duration = moves[0]
    assert (action, camera_id, host) == ('move', 'camera-ptz', '192.0.2.40')
    assert pan > 0 and tilt == 0 and zoom == 0
    assert pt.MIN_PULSE_SECONDS <= duration <= pt.MAX_PULSE_SECONDS

    status = main._state.live_detection_status['camera-ptz']['ptz_tracking']
    assert status['state'] == 'tracking' and status['label'] == 'cat'
    pt.reset_auto_tracking()
