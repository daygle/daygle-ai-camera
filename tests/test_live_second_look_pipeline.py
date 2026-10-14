"""Second look and low-light enhancement wired into the live detection cycle."""
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


class _NearMissDetector:
    """Full frame: a person at 0.35 (under the 0.45 detector threshold - this
    camera is not saved, so the threshold falls back to the AI-settings 0.45
    confidence). Crops: a confident
    person centred in the crop, so the second look confirms it."""

    backend = 'onnx'
    available = True
    unavailable_reason = None

    def __init__(self):
        self.calls: list[tuple[tuple[int, int], float | None, float]] = []

    def detect_frame(self, image, confidence=None):
        self.calls.append((image.shape[:2], confidence, float(image.mean())))
        if image.shape[:2] == (720, 1280):
            found = {'label': 'person', 'confidence': 0.35,
                     'box': {'x': 0.45, 'y': 0.45, 'width': 0.04, 'height': 0.08}}
        else:
            found = {'label': 'person', 'confidence': 0.8,
                     'box': {'x': 0.3, 'y': 0.3, 'width': 0.4, 'height': 0.4}}
        # Like the real detector, nothing below the requested confidence.
        return [found] if confidence is None or found['confidence'] >= confidence else []


def _camera(profile: dict) -> dict:
    return {
        'id': 'camera-yard',
        'name': 'Yard',
        'detection': {
            'object_detection_enabled': True,
            'zones': [{
                'id': 'yard', 'name': 'Yard', 'enabled': True, 'monitor_objects': True,
                'x': 0, 'y': 0, 'width': 1, 'height': 1,
                'object_rules': [{'label': 'person', 'enabled': True, 'min_confidence': 0.5}],
            }],
        },
        'recording': {'continuous': False},
        'detection_profiles': {'active': 'day', 'day': profile},
    }


def _run(main, monkeypatch, profile):
    import app.live_monitor as _lm
    detector = _NearMissDetector()
    monkeypatch.setattr(main._state, 'detector', detector)
    main.database.set_setting('ai', {'backend': 'onnx', 'model_path': 'fake.onnx'}, main.utc_now())
    monkeypatch.setattr(_lm, 'detect_frame_motion', lambda *a, **k: (False, 0.0, None, 0.0))
    # Keep the person regardless of moving/still classification.
    monkeypatch.setattr(_lm, 'filter_detections_by_motion_mode', lambda detections, *a, **k: detections)
    captured: dict = {}
    real_status = _lm.update_live_detection_status

    def capturing_status(camera_id, **kwargs):
        if 'detections' in kwargs:
            captured['detections'] = kwargs.get('detections')
        return real_status(camera_id, **kwargs)
    monkeypatch.setattr(_lm, 'update_live_detection_status', capturing_status)
    frame = np.full((720, 1280, 3), 20, dtype=np.uint8)
    frame[300:400, 600:650] = 40
    _lm.process_live_stream_alerts(
        frame, {'width': 1280, 'height': 720, 'timestamp': time.time()},
        _camera(profile),
        enforce_interval=False,
    )
    people = [d for d in (captured.get('detections') or []) if d.get('label') == 'person']
    return detector, people


def test_second_look_off_runs_one_pass_and_logs_the_near_miss_as_faint(tmp_path, monkeypatch):
    """One pass, asked down to the detection-log floor: the 0.35 near-miss is
    still below the 0.45 threshold (no person), but the recording's detection
    log now shows it as dropped below the detection minimum."""
    import app.live_monitor as _lm
    main = _load_app(tmp_path, monkeypatch)
    main._state.live_detection_history.pop('camera-yard', None)
    detector, people = _run(main, monkeypatch, {})
    assert len(detector.calls) == 1
    assert detector.calls[0][1] == pytest.approx(_lm.FAINT_LOG_FLOOR)
    assert not people  # the 0.35 near-miss is below the threshold
    history = main._state.live_detection_history.get('camera-yard')
    assert history and history[-1][1] == []
    assert [(d['label'], d['reason']) for d in history[-1][2]] == [
        ('person', 'Below the detection minimum (0.35 < 0.45)'),
    ]


def test_second_look_confirms_a_near_miss(tmp_path, monkeypatch):
    main = _load_app(tmp_path, monkeypatch)
    detector, people = _run(main, monkeypatch, {'object_detection_second_look': True})
    # One full-frame pass (asked down to the log floor, below Second Look's
    # 0.27), then crop + mirrored crop.
    assert len(detector.calls) == 3
    assert detector.calls[0][1] == pytest.approx(min(0.45 * 0.6, 0.20))
    assert all(shape != (720, 1280) for shape, _conf, _mean in detector.calls[1:])
    assert people and people[0].get('second_look') is True
    assert people[0]['confidence'] >= 0.45


def test_low_light_feeds_the_detector_an_enhanced_copy(tmp_path, monkeypatch):
    main = _load_app(tmp_path, monkeypatch)
    detector, _people = _run(main, monkeypatch, {'object_detection_low_light': 'auto'})
    # The dark 20/255 frame reaches the detector brightened by CLAHE.
    assert detector.calls[0][2] > 21


def test_faint_detections_are_split_off_and_capped_for_the_log():
    import app.main  # noqa: F401 - import order for app.live_monitor
    import app.live_monitor as _lm
    dets = [{'label': 'person', 'confidence': c, 'box': {'x': 0.1, 'y': 0.1, 'width': 0.1, 'height': 0.2}}
            for c in (0.9, 0.48, 0.44, 0.40, 0.33, 0.30, 0.25, 0.21)]
    kept, faint = _lm._split_faint_detections(dets, 0.5)
    assert [d['confidence'] for d in kept] == [0.9]
    assert len(faint) == 7 and faint[0]['reason'] == 'Below the detection minimum (0.48 < 0.50)'
    rejected = {'label': 'car', 'confidence': 0.4, 'box': {'x': 0.5, 'y': 0.5, 'width': 0.1, 'height': 0.1},
                'reason': 'Below threshold (0.40 < 0.50); Second Look zoomed in and scored 0.41'}
    logged = _lm._faint_log_entries([rejected] + faint, {'id': 'cam', 'detection': {'zones': []}})
    assert logged[0] is rejected  # Second Look rejections are never capped
    assert [d['confidence'] for d in logged[1:]] == [0.48, 0.44, 0.40, 0.33, 0.30]
    assert _lm._faint_log_floor(0.5) == 0.20 and _lm._faint_log_floor(0.1) == 0.1
