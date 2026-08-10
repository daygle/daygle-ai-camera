"""Regression test: each background detection thread must use ITS OWN camera's
config, not a later camera's.

``run_live_alert_monitor_once`` spawns a daemon thread per camera. The thread's
closure previously read ``selected_config`` as a free variable, which late-binds
to whatever the loop variable points at when the thread actually runs -- in a
multi-camera setup that is a LATER camera's config, so a camera's frame was
evaluated against the wrong camera's zones/rules. The per-iteration config is
now snapshotted as a default argument (like the camera id already was).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.state as _state  # noqa: E402
import app.live_monitor as live_monitor  # noqa: E402


class _CapturingThread:
    """Stand-in for threading.Thread that records the target without running it,
    so the test can invoke the closures AFTER the dispatch loop has advanced
    ``selected_config`` to the last camera -- the exact condition that exposes a
    late-bound free variable."""

    captured: list = []

    def __init__(self, *args, target=None, name=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        _CapturingThread.captured.append(self._target)


def test_direct_camera_without_ingest_stream_is_monitored(monkeypatch):
    """A camera that has a direct frame source but no constructible RTSP URL
    must still feed the live monitor and motion status path."""
    import cv2
    import numpy as np

    _CapturingThread.captured = []
    recorded: list[tuple[str, str]] = []
    ok, encoded = cv2.imencode('.jpg', np.zeros((8, 8, 3), dtype=np.uint8))
    assert ok

    class _DirectCamera:
        def read_jpeg(self):
            return encoded.tobytes(), {'timestamp': time.time(), 'width': 8, 'height': 8}

    monkeypatch.setattr(live_monitor.threading, 'Thread', _CapturingThread)
    monkeypatch.setattr(live_monitor, '_camera_has_live_alert_stream', lambda _cfg: False)
    monkeypatch.setattr(live_monitor, 'build_stream_url', lambda _cfg: '')
    monkeypatch.setattr(live_monitor, 'read_ingest_frame', lambda _cid: None)
    monkeypatch.setattr(_state, 'camera_event_recording_config', lambda _cfg: {}, raising=False)
    monkeypatch.setattr(_state, 'camera_instances', {'cam-direct': _DirectCamera()})
    monkeypatch.setattr(_state, 'cameras_config', [{'id': 'cam-direct', 'name': 'Direct'}])
    monkeypatch.setattr(_state, 'active_live_detection_cameras', set())
    monkeypatch.setattr(_state, 'live_detection_last_checked', {})
    monkeypatch.setattr(_state, 'live_detection_retry_after', {})

    def _fake_process(image, frame, settings, *, enforce_interval=True):
        recorded.append((str(settings.get('id')), str(frame.get('width'))))

    monkeypatch.setattr(live_monitor, 'process_live_stream_alerts', _fake_process)

    live_monitor.run_live_alert_monitor_once(
        {'background_detection_enabled': True, 'detection_interval_seconds': 0}
    )

    assert len(_CapturingThread.captured) == 1
    _CapturingThread.captured[0]()
    assert recorded == [('cam-direct', '8')]


def test_each_detection_thread_uses_its_own_camera_config(monkeypatch):
    _CapturingThread.captured = []
    recorded: list[tuple[str, str]] = []

    monkeypatch.setattr(live_monitor.threading, 'Thread', _CapturingThread)
    monkeypatch.setattr(live_monitor, '_camera_has_live_alert_stream', lambda cfg: True)
    monkeypatch.setattr(live_monitor, 'build_stream_url', lambda cfg: '')  # skip prebuffer
    monkeypatch.setattr(_state, 'camera_event_recording_config', lambda cfg: {}, raising=False)
    monkeypatch.setattr(live_monitor, 'read_ingest_frame',
                        lambda cid: (f'img-{cid}', {'timestamp': time.time(), 'width': 10, 'height': 10}))
    monkeypatch.setattr(live_monitor, 'clear_live_camera_backoff', lambda *a, **k: None)

    def _fake_process(image, frame, settings, *, enforce_interval=True):
        recorded.append((str(image), str(settings.get('id'))))

    monkeypatch.setattr(live_monitor, 'process_live_stream_alerts', _fake_process)

    _state.cameras_config = [
        {'id': 'cam-a', 'name': 'A'},
        {'id': 'cam-b', 'name': 'B'},
    ]
    _state.active_live_detection_cameras = set()
    _state.live_detection_last_checked = {}
    _state.live_detection_retry_after = {}

    live_monitor.run_live_alert_monitor_once(
        {'background_detection_enabled': True, 'detection_interval_seconds': 0}
    )

    # The loop has finished; selected_config now points at the LAST camera.
    # Running the captured closures now is what exposed the late-binding bug.
    assert len(_CapturingThread.captured) == 2
    for target in _CapturingThread.captured:
        target()

    # Each recorded (frame, config-id) pair must be self-consistent: the frame
    # read for camera X must have been evaluated against camera X's config.
    assert sorted(recorded) == [('img-cam-a', 'cam-a'), ('img-cam-b', 'cam-b')], recorded


class _ExplodingDetector:
    """Stand-in detector whose inference methods raise if ever reached --
    proves the AI-disabled gate short-circuits before any ONNX work."""

    def detect_frame(self, image, confidence=None):
        raise AssertionError('ONNX inference must not run when AI is disabled')

    def detect_image(self, image_bytes, confidence=None):
        raise AssertionError('ONNX inference must not run when AI is disabled')


def test_process_live_stream_alerts_gates_on_ai_disabled(monkeypatch):
    """The master AI toggle (``ai.enabled=False``) short-circuits
    ``process_live_stream_alerts`` before any motion or ONNX inference work:
    the status becomes 'skipped' with the disabled reason and the function
    returns None without touching the detector."""
    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(live_monitor, 'effective_ai_config', lambda: {'enabled': False})
    monkeypatch.setattr(
        live_monitor,
        'update_live_detection_status',
        lambda camera_id, **kwargs: recorded.append((camera_id, kwargs)),
    )
    # Any inference attempt would raise, proving the gate fires first.
    monkeypatch.setattr(_state, 'detector', _ExplodingDetector())

    result = live_monitor.process_live_stream_alerts(
        b'jpeg-bytes',
        {'timestamp': 1.0, 'width': 10, 'height': 10},
        {'id': 'cam-1'},
    )

    assert result is None
    assert recorded == [(
        'cam-1',
        {'state': 'skipped', 'reason': 'AI detection is disabled.', 'detections': []},
    )], recorded


def test_process_live_stream_alerts_proceeds_when_ai_enabled(monkeypatch):
    """With ``ai.enabled`` unset (default True), the gate does NOT fire --
    the function proceeds into the normal pipeline (motion detection path).
    The status must not be marked as disabled."""
    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(live_monitor, 'effective_ai_config', lambda: {})
    monkeypatch.setattr(
        live_monitor,
        'update_live_detection_status',
        lambda camera_id, **kwargs: recorded.append((camera_id, kwargs)),
    )
    # The gate passes, so the flow reaches the detector-loaded check; stub
    # the status payload so the DB-backed source helper isn't hit.
    monkeypatch.setattr(
        live_monitor,
        'ai_status_payload',
        lambda: {
            'detector_loaded': True,
            'last_detector_error': None,
            'configured_backend': 'onnx',
            'active_backend': 'onnx',
        },
    )
    # Empty motion -> 'No motion detected' branch returns before ONNX.
    monkeypatch.setattr(live_monitor, 'detect_frame_motion',
                        lambda cid, image, **kw: (False, 0.0, None, 0.0))
    monkeypatch.setattr(live_monitor, 'zone_motion_detections',
                        lambda settings, conf, **kw: [])
    monkeypatch.setattr(_state, 'detector', _ExplodingDetector())
    monkeypatch.setattr(_state, '_MOTION_PIXEL_THRESHOLD', 30)
    monkeypatch.setattr(_state, '_MOTION_GATE_FRACTION', 0.003)
    monkeypatch.setattr(_state, '_MOTION_SCALE_FRACTION', 0.03)
    monkeypatch.setattr(_state, '_MOTION_BACKGROUND_ALPHA', 0.05)
    monkeypatch.setattr(_state, '_MOTION_FRAME_W', 160)
    monkeypatch.setattr(_state, '_MOTION_FRAME_H', 120)
    monkeypatch.setattr(_state, '_frame_motion_prev', {})
    monkeypatch.setattr(_state, '_periodic_scan_last_ts', {})

    result = live_monitor.process_live_stream_alerts(
        b'jpeg-bytes',
        {'timestamp': 1.0, 'width': 10, 'height': 10},
        {'id': 'cam-1'},
        enforce_interval=False,
    )

    assert result is None
    # No 'AI detection is disabled' entry -- the gate did not fire.
    assert all('disabled' not in str(reason) for _, kwargs in recorded for reason in [kwargs.get('reason', '')])


def test_motion_status_exposes_sub_gate_signal_for_live_bar(monkeypatch):
    """The bar shows real pixel changes even when alert confidence is gated to zero."""
    recorded: list[dict] = []
    monkeypatch.setattr(live_monitor, 'effective_ai_config', lambda: {})
    monkeypatch.setattr(
        live_monitor,
        'ai_status_payload',
        lambda: {
            'detector_loaded': True,
            'last_detector_error': None,
            'configured_backend': 'onnx',
            'active_backend': 'onnx',
        },
    )
    monkeypatch.setattr(
        live_monitor,
        'effective_live_config',
        lambda: {
            'detection_interval_seconds': 0.5,
            'motion_pixel_threshold': 30,
            'motion_gate_fraction': 0.005,
            'motion_scale_fraction': 0.03,
            'motion_background_alpha': 0.05,
            'periodic_scan_interval_seconds': 0,
        },
    )
    monkeypatch.setattr(
        live_monitor,
        'detect_frame_motion',
        lambda cid, image, **kwargs: (False, 0.0, None, 0.003),
    )
    monkeypatch.setattr(live_monitor, 'update_live_detection_status', lambda camera_id, **kwargs: recorded.append(kwargs))
    _state._frame_motion_error_cameras.discard('cam-1')

    result = live_monitor.process_live_stream_alerts(
        b'jpeg-bytes',
        {'timestamp': time.time(), 'width': 10, 'height': 10},
        {'id': 'cam-1'},
        enforce_interval=False,
    )

    assert result is None
    assert recorded
    assert recorded[0]['motion_confidence'] == 0.0
    assert recorded[0]['motion_fraction'] == 0.003
    assert recorded[0]['motion_signal'] == 0.1
