"""Unit tests for the camera-motion behavioural pause token.

Suppressing the behavioural engines while the camera moves is not sufficient on
its own: a PTZ pan moves every tracked box in image space, so the FIRST sample
after resume could otherwise read a pre-pan visit as a long loiter or a
pan-induced line crossing. ``sync_behaviour_pause`` makes that suppression
explicit and drops per-camera transitional state on both the onset and the
resume edge.

These tests pin: transition detection, the reset of each per-camera store, and
-- importantly -- that LEARNED baselines and other cameras survive, because a
0.4 s nudge must not erase an hour of learning.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import behaviour_monitor as bm  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    """Isolate the module-level state maps these functions own."""
    saved = (
        dict(bm._loiter_presence), dict(bm._loiter_baselines), dict(bm._loiter_cooldowns),
        dict(bm._time_today), dict(bm._time_baselines), dict(bm._time_cooldowns),
        dict(bm._tripwire_last_fired), dict(bm._behaviour_pause),
    )
    bm._loiter_presence.clear()
    bm._loiter_baselines.clear()
    bm._loiter_cooldowns.clear()
    bm._time_today.clear()
    bm._time_baselines.clear()
    bm._time_cooldowns.clear()
    bm._tripwire_last_fired.clear()
    bm._behaviour_pause.clear()
    yield
    (
        bm._loiter_presence, bm._loiter_baselines, bm._loiter_cooldowns,
        bm._time_today, bm._time_baselines, bm._time_cooldowns,
        bm._tripwire_last_fired, bm._behaviour_pause,
    ) = saved


def _seed(camera_id: str = 'cam-a', other: str = 'cam-b') -> None:
    """Give the camera a full pre-pan state: presence, baselines, cooldowns."""
    bm._loiter_presence[camera_id] = {'cam-a|zone|1': {'first_seen': 1.0}}
    bm._loiter_presence[other] = {'cam-b|zone|1': {'first_seen': 2.0}}
    bm._loiter_baselines['cam-a|zone|person'] = {'count': 5.0, 'mean': 30.0, 'm2': 4.0}
    bm._loiter_cooldowns['cam-a|zone|person'] = 100.0
    bm._time_today[camera_id] = {('cam-a', 'zone', 'person'): 3}
    bm._time_today[other] = {('cam-b', 'zone', 'person'): 4}
    bm._time_baselines['cam-a|zone|person'] = [[3, 0.2], [4, 0.3]]
    bm._tripwire_last_fired[f'{camera_id}|zone|in|1'] = 10.0
    bm._tripwire_last_fired[f'{camera_id}|zone|out|1'] = 11.0
    bm._tripwire_last_fired[f'{other}|zone|in|1'] = 12.0


def test_transitions_are_reported_once():
    _seed()
    assert bm.sync_behaviour_pause('cam-a', False) is None
    assert bm.sync_behaviour_pause('cam-a', True) == bm.PAUSE_ONSET
    # Still active: no new transition.
    assert bm.sync_behaviour_pause('cam-a', True) is None
    assert bm.sync_behaviour_pause('cam-a', False) == bm.PAUSE_RESUME
    assert bm.sync_behaviour_pause('cam-a', False) is None


def test_onset_drops_transitional_state_but_keeps_learned_baselines():
    _seed()
    bm.sync_behaviour_pause('cam-a', True)

    # Transitional: an in-progress visit and today's observation tally.
    assert 'cam-a' not in bm._loiter_presence
    assert 'cam-a' not in bm._time_today
    # Cooldowns are per-camera too and would suppress a real post-pan crossing.
    assert not [k for k in bm._tripwire_last_fired if k.startswith('cam-a|')]
    # Learned distributions survive: a 0.4s nudge must not erase an hour of it.
    assert bm._loiter_baselines['cam-a|zone|person'] == {'count': 5.0, 'mean': 30.0, 'm2': 4.0}
    assert bm._loiter_cooldowns['cam-a|zone|person'] == 100.0
    assert bm._time_baselines['cam-a|zone|person'] == [[3, 0.2], [4, 0.3]]


def test_resume_also_resets_so_the_first_sample_starts_fresh():
    _seed()
    bm.sync_behaviour_pause('cam-a', True)
    # A pan in progress accumulates presence again; it must not reach an engine.
    bm._loiter_presence['cam-a'] = {'cam-a|zone|1': {'first_seen': 99.0}}
    bm._tripwire_last_fired['cam-a|zone|in|1'] = 99.0

    assert bm.sync_behaviour_pause('cam-a', False) == bm.PAUSE_RESUME

    assert 'cam-a' not in bm._loiter_presence
    assert not [k for k in bm._tripwire_last_fired if k.startswith('cam-a|')]


def test_reset_is_scoped_to_one_camera():
    _seed()
    bm.sync_behaviour_pause('cam-a', True)

    assert bm._loiter_presence['cam-b'] == {'cam-b|zone|1': {'first_seen': 2.0}}
    assert bm._time_today['cam-b'] == {('cam-b', 'zone', 'person'): 4}
    assert bm._tripwire_last_fired['cam-b|zone|in|1'] == 12.0


def test_behaviour_paused_reflects_state():
    assert bm.behaviour_paused('cam-a') is False
    bm.sync_behaviour_pause('cam-a', True)
    assert bm.behaviour_paused('cam-a') is True
    bm.sync_behaviour_pause('cam-a', False)
    assert bm.behaviour_paused('cam-a') is False


def test_clear_behavioural_state_drops_the_pause_flag_too():
    """A removed-and-re-added camera must not inherit a stale pause verdict."""
    bm.sync_behaviour_pause('cam-a', True)
    assert bm.behaviour_paused('cam-a') is True

    bm.clear_behavioural_state('cam-a')

    assert bm.behaviour_paused('cam-a') is False
    # And the next call reads as a fresh onset, not a no-op.
    assert bm.sync_behaviour_pause('cam-a', True) == bm.PAUSE_ONSET


def test_first_call_while_active_reports_onset():
    """A camera that is already moving when the monitor starts must still reset."""
    _seed()
    assert bm.sync_behaviour_pause('cam-a', True) == bm.PAUSE_ONSET
    assert 'cam-a' not in bm._loiter_presence
