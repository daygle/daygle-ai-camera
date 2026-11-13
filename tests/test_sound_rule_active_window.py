"""Sound rules read their active window like every other backend gate.

An empty, partial or equal (``start == end``) window means "any time" for
zone rules (``app.zone_schema._window_minutes``), alert rules
(``AlertEngine._is_active_now``) and notify windows. The sound path used to
skip the equal case, so a rule saved as ``08:00``-``08:00`` (which
``_normalize_camera_sound_settings`` accepts) only detected during that one
minute of the day while its notify window, read the shared way, said always.
"""

from __future__ import annotations

import pytest

import app.sound_detector as sound_detector
from app.sound_detector import SoundDetector


@pytest.fixture
def now_hm(monkeypatch):
    def _set(value: str) -> None:
        monkeypatch.setattr(sound_detector, '_now_hm_in_admin_tz', lambda: value)
    return _set


@pytest.mark.parametrize('now', ['00:00', '08:00', '12:34', '23:59'])
def test_equal_window_is_active_all_day(now_hm, now):
    now_hm(now)
    assert SoundDetector._rule_active_now({'active_start': '08:00', 'active_end': '08:00'})


@pytest.mark.parametrize('rule', [
    {},
    {'active_start': None, 'active_end': None},
    {'active_start': '08:00', 'active_end': ''},
    {'active_start': '', 'active_end': '17:00'},
])
def test_empty_or_partial_window_is_active(now_hm, rule):
    now_hm('03:00')
    assert SoundDetector._rule_active_now(rule)


@pytest.mark.parametrize(('now', 'expected'), [
    ('07:59', False), ('08:00', True), ('12:00', True), ('17:00', True), ('17:01', False),
])
def test_daytime_window_is_inclusive(now_hm, now, expected):
    now_hm(now)
    assert SoundDetector._rule_active_now({'active_start': '08:00', 'active_end': '17:00'}) is expected


@pytest.mark.parametrize(('now', 'expected'), [
    ('21:59', False), ('22:00', True), ('23:59', True), ('00:00', True), ('06:00', True), ('06:01', False),
])
def test_overnight_window_wraps_midnight(now_hm, now, expected):
    now_hm(now)
    assert SoundDetector._rule_active_now({'active_start': '22:00', 'active_end': '06:00'}) is expected
