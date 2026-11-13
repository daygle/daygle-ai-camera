"""Behaviour monitors: the camera's own clock and baseline persistence.

The hour-of-day and activity-spike baselines are the SITE's local time and are
persisted to the app_settings KV store. The pure-step suites
(``test_behaviour_time.py``, ``test_behaviour_activity.py``) cannot cover either
property, so these tests drive the emit layer in ``app.behaviour_monitor``
directly.

Zone matching is exercised through real geometry (a rectangle covering the
detection) rather than by stubbing ``app.zone_detection``: ``tests/support.py``
rebuilds the ``app.*`` package tree mid-suite, so a stub on the module collected
here would not be the one a monitor imports.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, UTC
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import app.state as state  # noqa: E402
from app import behaviour_monitor as bm  # noqa: E402

# 2026-01-15T12:00:00Z: 02:00 the next day in Kiritimati (UTC+14), and nowhere
# near that hour in the zones a test host is likely to run in.
INSTANT = datetime(2026, 1, 15, 12, 0, tzinfo=UTC).timestamp()
CAMERA_ZONE = 'Pacific/Kiritimati'


class _FakeDatabase:
    """Minimal app_settings KV store with injectable read failures."""

    def __init__(self, settings=None, fail_reads=0):
        self.settings = dict(settings or {})
        self.fail_reads = fail_reads
        self.saved: list[tuple[str, dict]] = []

    def get_setting(self, key):
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise RuntimeError('database is locked')
        return self.settings.get(key)

    def set_setting(self, key, value, updated_at=None):
        self.saved.append((key, value))
        self.settings[key] = value


@pytest.fixture(autouse=True)
def _isolated_module_state():
    original_database = state.database
    bm._time_today.clear()
    bm._time_baselines.clear()
    bm._time_cooldowns.clear()
    bm._time_state.update(loaded=False, dirty=False, last_flush=0.0)
    bm._activity_today.clear()
    bm._activity_baselines.clear()
    bm._activity_cooldowns.clear()
    bm._activity_state.update(loaded=False, dirty=False, last_flush=0.0)
    try:
        yield
    finally:
        state.database = original_database


def _camera_settings(timezone_name=CAMERA_ZONE, zone=None):
    return {
        'id': 'cam-tz',
        'name': 'Test camera',
        'timezone': timezone_name,
        'detection': {'zones': [zone] if zone else []},
    }


def _time_zone():
    return {
        'id': 'z-time', 'name': 'Drive', 'enabled': True,
        'x': 0, 'y': 0, 'width': 1, 'height': 1,
        'time_of_day': {'enabled': True, 'labels': [], 'threshold': 0.15, 'cooldown_seconds': 1800},
    }


def _activity_zone():
    return {
        'id': 'z-act', 'name': 'Yard', 'enabled': True,
        'x': 0, 'y': 0, 'width': 1, 'height': 1,
        'activity_spike': {'enabled': True, 'labels': [], 'min_count': 5, 'cooldown_seconds': 900},
    }


def _detection():
    return {
        'track_id': 1, 'label': 'person', 'confidence': 0.9,
        'box': {'x': 0.4, 'y': 0.4, 'width': 0.1, 'height': 0.1},
    }


def _camera_local():
    return datetime.fromtimestamp(INSTANT, tz=ZoneInfo(CAMERA_ZONE))


def _skip_when_host_clock_matches_camera_zone():
    if datetime.fromtimestamp(INSTANT).hour == _camera_local().hour:
        pytest.skip('host clock already reads the same hour as the camera zone')


def test_time_of_day_bucket_uses_the_camera_timezone(monkeypatch):
    _skip_when_host_clock_matches_camera_zone()
    monkeypatch.setattr(state, 'database', _FakeDatabase())
    monkeypatch.setattr(bm.time, 'time', lambda: INSTANT)

    fired = bm.emit_time_of_day_anomalies(
        'cam-tz', _camera_settings(zone=_time_zone()), [_detection()],
    )

    assert fired == 0  # a fresh baseline is still learning
    local = _camera_local()
    today = bm._time_today['cam-tz|z-time|person']
    assert today['hours'] == {local.hour}
    assert today['day'] == local.toordinal()


def test_activity_bucket_uses_the_camera_timezone(monkeypatch):
    _skip_when_host_clock_matches_camera_zone()
    monkeypatch.setattr(state, 'database', _FakeDatabase())
    monkeypatch.setattr(bm.time, 'time', lambda: INSTANT)

    bm.emit_activity_spike_anomalies(
        'cam-tz', _camera_settings(zone=_activity_zone()), [_detection()],
    )

    local = _camera_local()
    bucket = bm._activity_today['cam-tz|z-act|person']
    assert bucket['hour'] == local.hour
    assert bucket['day'] == local.toordinal()
    assert bucket['baseline_key'] == f'cam-tz|z-act|person|{local.hour}'


def test_transient_baseline_read_failure_is_retried(monkeypatch):
    stored = {'cam-tz|z-time|person': {'days': 9.0, 'hours': [1.0] * 24}}
    database = _FakeDatabase({'behaviour_time_baselines': stored}, fail_reads=1)
    monkeypatch.setattr(state, 'database', database)
    monkeypatch.setattr(bm.time, 'time', lambda: INSTANT)
    settings = _camera_settings(timezone_name='UTC', zone=_time_zone())

    bm.emit_time_of_day_anomalies('cam-tz', settings, [_detection()])
    # A failed read means "try again", not "there is no history".
    assert bm._time_state['loaded'] is False
    assert 'cam-tz|z-time|person' not in bm._time_baselines

    bm.emit_time_of_day_anomalies('cam-tz', settings, [_detection()])
    assert bm._time_state['loaded'] is True
    assert bm._time_baselines['cam-tz|z-time|person']['days'] == 9.0


def test_failed_load_never_flushes_over_the_stored_baselines(monkeypatch):
    stored = {'cam-tz|z-time|person': {'days': 9.0, 'hours': [1.0] * 24}}
    database = _FakeDatabase({'behaviour_time_baselines': stored}, fail_reads=99)
    monkeypatch.setattr(state, 'database', database)
    monkeypatch.setattr(bm.time, 'time', lambda: INSTANT)
    settings = _camera_settings(timezone_name='UTC', zone=_time_zone())

    bm.emit_time_of_day_anomalies('cam-tz', settings, [_detection()])
    bm._time_state['dirty'] = True  # as if a day had just been committed
    bm.emit_time_of_day_anomalies('cam-tz', settings, [_detection()])

    assert [key for key, _ in database.saved] == []
