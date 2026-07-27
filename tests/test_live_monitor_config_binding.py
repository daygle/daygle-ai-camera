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
