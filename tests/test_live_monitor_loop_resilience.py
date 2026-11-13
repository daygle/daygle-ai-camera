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
import types
from pathlib import Path

import pytest

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


def test_running_loop_keeps_its_own_stop_event_across_a_restart(monkeypatch):
    """A restart must not revive the running loop by replacing its signal.

    ``start_live_alert_monitor`` publishes a fresh event for the successor; the
    loop in flight keeps waiting on the event it was started with, so a stop
    that timed out still ends it instead of leaving two monitors running.
    """
    original = threading.Event()
    monkeypatch.setattr(_state, 'live_alert_monitor_stop', original)
    cycles = {'n': 0}

    def cycle(_live_settings):
        cycles['n'] += 1
        if cycles['n'] > 1:
            pytest.fail('the loop kept running after its own stop event was set')
        original.set()
        # A restart in flight publishes the successor's event.
        _state.live_alert_monitor_stop = threading.Event()
        return 1

    monkeypatch.setattr(live_monitor, 'effective_live_config', lambda: dict(_LIVE_SETTINGS))
    monkeypatch.setattr(live_monitor, 'run_live_alert_monitor_once', cycle)
    monkeypatch.setattr(live_monitor, '_check_cameras_health', lambda: None)
    monkeypatch.setattr(live_monitor, '_prune_frame_motion_state', lambda: None)
    monkeypatch.setattr(live_monitor, 'purge_camera_diagnostics_by_policy', lambda: 0)
    monkeypatch.setattr(live_monitor, 'purge_ai_diagnostics_by_policy', lambda: 0)

    live_monitor.live_alert_monitor_loop()

    assert cycles['n'] == 1


def test_restart_after_a_timed_out_stop_keeps_the_old_loops_stop_signal(monkeypatch):
    """``stop`` drops the thread reference when its join times out; a restart
    must still leave that thread's own stop event set, or it never exits."""
    previous_stop = threading.Event()
    previous_stop.set()
    monkeypatch.setattr(_state, 'live_alert_monitor_stop', previous_stop)
    monkeypatch.setattr(_state, 'live_alert_monitor_thread', None)
    started: list[str] = []

    class _RecordingThread:
        def __init__(self, target=None, name=None, daemon=None):
            self.name = name
            started.append(name)

        def start(self):
            pass

        def is_alive(self):
            return True

    monkeypatch.setattr(
        live_monitor, 'threading',
        types.SimpleNamespace(Thread=_RecordingThread, Event=threading.Event),
    )

    live_monitor.start_live_alert_monitor()

    assert started == ['live-alert-monitor']
    assert _state.live_alert_monitor_stop is not previous_stop
    assert not _state.live_alert_monitor_stop.is_set()
    assert previous_stop.is_set(), 'the previous loop must keep seeing its stop signal'
