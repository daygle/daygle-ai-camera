"""Regression test: a transient failure must not kill the live-alert monitor.

``live_alert_monitor_loop`` previously ran every cycle bare: one raised
exception (e.g. a SQLite read surfacing through ``_check_cameras_health`` while
a settings write holds the database) escaped the ``while`` body, the thread
died, and background detection + camera offline/recovery alerts stayed
silently disabled until the next service restart. The loop now catches
per-cycle failures, logs them, and retries on a short delay.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.state as _state  # noqa: E402
import app.live_monitor as live_monitor  # noqa: E402

_LIVE_SETTINGS = {'detection_interval_seconds': 0.1}


def test_monitor_loop_survives_transient_cycle_failure(monkeypatch):
    _state.live_alert_monitor_stop.clear()
    cycles = {'n': 0}
    waits: list[float] = []

    def flaky_cycle(_live_settings):
        cycles['n'] += 1
        if cycles['n'] == 1:
            raise RuntimeError('database is locked')
        _state.live_alert_monitor_stop.set()
        return 1

    monkeypatch.setattr(live_monitor, 'effective_live_config', lambda: dict(_LIVE_SETTINGS))
    monkeypatch.setattr(live_monitor, 'run_live_alert_monitor_once', flaky_cycle)
    monkeypatch.setattr(live_monitor, '_check_cameras_health', lambda: None)
    monkeypatch.setattr(_state.live_alert_monitor_stop, 'wait', waits.append)

    live_monitor.live_alert_monitor_loop()

    assert cycles['n'] == 2, 'monitor must retry the cycle after a failure'
    assert waits and waits[0] == 1.0, 'failed cycle must retry on the short delay'


def test_monitor_loop_exits_when_stop_is_set(monkeypatch):
    _state.live_alert_monitor_stop.clear()
    calls = {'cycle': 0, 'prune': 0, 'health': 0}

    def cycle(_live_settings):
        calls['cycle'] += 1
        if calls['cycle'] == 3:
            _state.live_alert_monitor_stop.set()
        return 1

    monkeypatch.setattr(live_monitor, 'effective_live_config', lambda: dict(_LIVE_SETTINGS))
    monkeypatch.setattr(live_monitor, 'run_live_alert_monitor_once', cycle)
    monkeypatch.setattr(live_monitor, '_check_cameras_health', lambda: calls.__setitem__('health', calls['health'] + 1))
    monkeypatch.setattr(live_monitor, '_prune_frame_motion_state', lambda: calls.__setitem__('prune', calls['prune'] + 1))
    monkeypatch.setattr(live_monitor, 'purge_camera_diagnostics_by_policy', lambda: 0)

    real_wait = threading.Event().wait
    waits: list[float] = []

    def record_wait(interval):
        waits.append(interval)
        return real_wait(0)

    monkeypatch.setattr(_state.live_alert_monitor_stop, 'wait', record_wait)

    live_monitor.live_alert_monitor_loop()

    assert calls['cycle'] == 3
    assert calls['prune'] == 1, 'periodic prune should run on the first cycle'
    assert calls['health'] == 3
    assert waits and waits[-1] == 0.1, 'healthy cycle waits the configured interval'
