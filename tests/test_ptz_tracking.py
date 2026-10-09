"""PTZ auto-tracking controller (app/ptz_tracking.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.ptz_tracking as pt  # noqa: E402


class _Inline:
    """Executor stand-in that runs jobs immediately."""

    def submit(self, fn):
        fn()


@pytest.fixture(autouse=True)
def _inline_executor(monkeypatch):
    monkeypatch.setattr(pt, '_get_executor', lambda: _Inline())
    pt.reset_auto_tracking()
    yield
    pt.reset_auto_tracking()


def _camera(**auto_track):
    return {
        'id': 'cam', 'host': '10.0.0.5',
        'ptz': {'enabled': True, 'protocol': 'onvif', 'auto_track': {'enabled': True, 'labels': ['cat'], **auto_track}},
    }


def _det(label, cx, cy, *, track_id=1, age=3, size=0.1):
    return {
        'label': label, 'confidence': 0.8, 'track_id': track_id, 'track_age': age,
        'box': {'x': cx - size / 2, 'y': cy - size / 2, 'width': size, 'height': size},
    }


class _Mover:
    def __init__(self):
        self.calls = []

    def __call__(self, action, camera_id, conn, *args):
        self.calls.append((action, *args))


def _step(camera, now, *, candidates=(), visible=None, mover=None):
    visible = list(candidates) if visible is None else list(visible)
    return pt.update_auto_tracking(
        'cam', camera, candidates=list(candidates), visible=visible, now=now, mover=mover,
    )


def test_settings_normalization():
    s = pt.normalize_auto_track_settings({'enabled': 'true', 'labels': 'Cat, animal', 'dead_zone': 9, 'speed': 0, 'target_size': 'x'})
    assert s['enabled'] is True
    assert s['labels'] == ['cat', 'animal']
    assert s['dead_zone'] == 0.4 and s['speed'] == 1 and s['target_size'] == 0.3
    assert pt.normalize_auto_track_settings(None)['enabled'] is False


def test_disabled_or_without_ptz_does_nothing():
    mover = _Mover()
    camera = _camera(enabled=False)
    assert _step(camera, 10.0, candidates=[_det('cat', 0.9, 0.5)], mover=mover) is None
    camera = _camera()
    camera['ptz']['enabled'] = False
    assert _step(camera, 10.0, candidates=[_det('cat', 0.9, 0.5)], mover=mover) is None
    assert mover.calls == []


def test_only_configured_labels_are_followed():
    mover = _Mover()
    status = _step(_camera(), 10.0, candidates=[_det('person', 0.9, 0.5)], mover=mover)
    assert status == {'state': 'idle'} and mover.calls == []
    status = _step(_camera(), 11.0, candidates=[_det('cat', 0.9, 0.5, track_id=7)], mover=mover)
    assert status['state'] == 'tracking' and status['label'] == 'cat' and status['track_id'] == 7
    action, pan, tilt, zoom, _duration = mover.calls[-1]
    assert action == 'move' and pan > 0 and tilt == 0 and zoom == 0


def test_group_label_matches_member():
    mover = _Mover()
    status = _step(_camera(labels=['animal']), 10.0, candidates=[_det('cat', 0.1, 0.9)], mover=mover)
    assert status['state'] == 'tracking'
    _action, pan, tilt, _zoom, _duration = mover.calls[-1]
    assert pan < 0 and tilt < 0  # left and down


def test_new_track_must_persist_before_the_camera_moves():
    mover = _Mover()
    status = _step(_camera(), 10.0, candidates=[_det('cat', 0.9, 0.5, age=1)], mover=mover)
    assert status['state'] == 'idle' and mover.calls == []


def test_dead_zone_and_pulse_spacing():
    mover = _Mover()
    camera = _camera()
    _step(camera, 10.0, candidates=[_det('cat', 0.52, 0.48)], mover=mover)
    assert mover.calls == []  # already centred
    _step(camera, 11.0, candidates=[_det('cat', 0.9, 0.5)], mover=mover)
    duration = mover.calls[-1][-1]
    _step(camera, 11.0 + duration + pt.SETTLE_SECONDS - 0.05, candidates=[_det('cat', 0.9, 0.5)], mover=mover)
    assert len(mover.calls) == 1  # pulse still running or settling
    _step(camera, 11.0 + duration + pt.SETTLE_SECONDS + 0.01, candidates=[_det('cat', 0.9, 0.5)], mover=mover)
    assert len(mover.calls) == 2


def test_pulse_length_grows_with_distance_from_centre():
    """Small corrections are short nudges; a target near the edge gets a long pulse."""
    assert pt.pulse_seconds(0.16, 0.15) == pytest.approx(pt.MIN_PULSE_SECONDS, abs=0.05)
    assert pt.pulse_seconds(0.5, 0.15) == pytest.approx(pt.MAX_PULSE_SECONDS)
    assert pt.pulse_seconds(0.3, 0.15) < pt.pulse_seconds(0.45, 0.15)
    mover = _Mover()
    _step(_camera(), 10.0, candidates=[_det('cat', 0.7, 0.5, track_id=1)], mover=mover)
    near = mover.calls[-1][-1]
    pt.reset_auto_tracking()
    _step(_camera(), 10.0, candidates=[_det('cat', 0.97, 0.5, track_id=2)], mover=mover)
    far = mover.calls[-1][-1]
    assert near < far <= pt.MAX_PULSE_SECONDS


def test_follows_outside_zones_and_by_nearest_position():
    mover = _Mover()
    camera = _camera()
    _step(camera, 10.0, candidates=[_det('cat', 0.8, 0.5, track_id=1)], mover=mover)
    # Camera panned: no zone-scoped candidates, the tracker issued a new id.
    status = _step(camera, 11.2, candidates=[], visible=[_det('cat', 0.7, 0.5, track_id=9)], mover=mover)
    assert status['state'] == 'tracking' and status['track_id'] == 9
    # A cat far away is a different cat - not followed.
    status = _step(camera, 11.4, candidates=[], visible=[_det('cat', 0.1, 0.1, track_id=4)], mover=mover)
    assert status['state'] == 'tracking' and status['track_id'] == 9  # still holding, not lost yet


def test_lost_target_then_return_home():
    mover = _Mover()
    camera = _camera(lost_seconds=2, return_home_seconds=10, home_preset='3')
    _step(camera, 10.0, candidates=[_det('cat', 0.9, 0.5)], mover=mover)
    assert _step(camera, 11.0, mover=mover)['state'] == 'tracking'  # within lost_seconds
    assert _step(camera, 12.5, mover=mover)['state'] == 'idle'
    assert all(call[0] == 'move' for call in mover.calls)
    status = _step(camera, 20.5, mover=mover)
    assert status['state'] == 'returning' and mover.calls[-1] == ('home', '3')
    _step(camera, 40.0, mover=mover)
    assert sum(call[0] == 'home' for call in mover.calls) == 1  # only once


def test_manual_use_pauses_tracking():
    mover = _Mover()
    camera = _camera()
    pt.note_manual_ptz('cam', now=10.0)
    status = _step(camera, 15.0, candidates=[_det('cat', 0.9, 0.5)], mover=mover)
    assert status['state'] == 'paused' and status['resumes_in'] == pytest.approx(25.0)
    assert mover.calls == []
    assert _step(camera, 41.0, candidates=[_det('cat', 0.9, 0.5)], mover=mover)['state'] == 'tracking'


def test_busy_camera_is_skipped_not_queued(monkeypatch):
    submitted = []

    class _Deferred:
        def submit(self, fn):
            submitted.append(fn)

    monkeypatch.setattr(pt, '_get_executor', lambda: _Deferred())
    mover = _Mover()
    camera = _camera()
    _step(camera, 10.0, candidates=[_det('cat', 0.9, 0.5)], mover=mover)
    _step(camera, 12.0, candidates=[_det('cat', 0.9, 0.5)], mover=mover)  # first still in flight
    assert len(submitted) == 1


def test_zoom_in_when_centred_and_small_then_undo_on_loss():
    mover = _Mover()
    camera = _camera(zoom=True, target_size=0.4, return_home_seconds=0, lost_seconds=1)
    _step(camera, 10.0, candidates=[_det('cat', 0.5, 0.5, size=0.1)], mover=mover)
    _action, pan, tilt, zoom, duration = mover.calls[-1]
    assert pan == 0 and tilt == 0 and zoom == pt.ZOOM_VELOCITY
    assert duration == pt.PULSE_SECONDS  # zoom-only pulses stay short
    _step(camera, 11.1, candidates=[_det('cat', 0.5, 0.5, size=0.12)], mover=mover)
    zoom_ins = sum(1 for call in mover.calls if call[3] > 0)
    assert zoom_ins == 2
    # Target lost: the tracker zooms back out by as many pulses as it zoomed in.
    _step(camera, 13.0, mover=mover)
    _step(camera, 14.1, mover=mover)
    _step(camera, 15.2, mover=mover)
    zoom_outs = sum(1 for call in mover.calls if call[3] < 0)
    assert zoom_outs == 2


def test_zoom_out_when_target_nears_the_edge_or_is_too_big():
    assert pt.zoom_velocity(0.4, 0.0, 0.1, 0.3, centred=False) < 0
    assert pt.zoom_velocity(0.0, 0.0, 0.6, 0.3, centred=True) < 0
    assert pt.zoom_velocity(0.0, 0.0, 0.3, 0.3, centred=True) == 0
    assert pt.zoom_velocity(0.1, 0.0, 0.1, 0.3, centred=False) == 0  # off-centre: steer first


def test_axis_velocity_scales_with_error_and_speed():
    assert pt.axis_velocity(0.1, 0.15, 8) == 0
    assert pt.axis_velocity(0.5, 0.15, 8) == pytest.approx(1.0)
    assert pt.axis_velocity(-0.2, 0.15, 8) == pytest.approx(-0.4)
    assert pt.axis_velocity(0.2, 0.15, 4) == pytest.approx(0.2)


def _next_ready(mover, now):
    """The time the next pulse may go out after the last recorded one at ``now``."""
    return now + mover.calls[-1][-1] + pt.SETTLE_SECONDS + 0.01


def test_target_walking_away_gets_a_stronger_pulse():
    mover = _Mover()
    camera = _camera(speed=4)
    now = 10.0
    _step(camera, now, candidates=[_det('cat', 0.75, 0.5)], mover=mover)
    first_pan, first_duration = mover.calls[-1][1], mover.calls[-1][-1]
    # Still as far off on the same side: the pulse did not gain on it.
    now = _next_ready(mover, now)
    _step(camera, now, candidates=[_det('cat', 0.76, 0.5)], mover=mover)
    second_pan, second_duration = mover.calls[-1][1], mover.calls[-1][-1]
    assert second_pan > first_pan and second_duration > first_duration
    # It keeps escaping: the boost keeps growing, up to the cap.
    for _ in range(5):
        now = _next_ready(mover, now)
        _step(camera, now, candidates=[_det('cat', 0.77, 0.5)], mover=mover)
    assert mover.calls[-1][-1] <= pt.MAX_BOOSTED_PULSE_SECONDS
    assert pt._states['cam'].boost[0] == pt.MAX_BOOST


def test_boost_resets_when_centred_or_overshot():
    mover = _Mover()
    camera = _camera(speed=4)
    now = 10.0
    _step(camera, now, candidates=[_det('cat', 0.75, 0.5)], mover=mover)
    now = _next_ready(mover, now)
    _step(camera, now, candidates=[_det('cat', 0.76, 0.5)], mover=mover)
    assert pt._states['cam'].boost[0] > 1.0
    # Overshot: now on the other side - back to a gentle pulse.
    now = _next_ready(mover, now)
    _step(camera, now, candidates=[_det('cat', 0.25, 0.5)], mover=mover)
    assert pt._states['cam'].boost[0] == 1.0
    assert mover.calls[-1][1] < 0
    # Gaining on it does not boost.
    now = _next_ready(mover, now)
    _step(camera, now, candidates=[_det('cat', 0.32, 0.5)], mover=mover)
    assert pt._states['cam'].boost[0] == 1.0


def test_lost_at_the_edge_keeps_turning_that_way():
    mover = _Mover()
    camera = _camera(lost_seconds=10)
    _step(camera, 10.0, candidates=[_det('cat', 0.95, 0.5)], mover=mover)
    now = _next_ready(mover, 10.0)
    # Half out of the picture: no detection this cycle.
    assert _step(camera, now, mover=mover)['state'] == 'tracking'
    action, pan, tilt, zoom, duration = mover.calls[-1]
    assert action == 'move' and pan > 0 and tilt == 0 and duration == pt.MAX_PULSE_SECONDS
    now = _next_ready(mover, now)
    _step(camera, now, mover=mover)
    now = _next_ready(mover, now)
    _step(camera, now, mover=mover)
    assert len(mover.calls) == 1 + pt.MAX_EDGE_PUSHES  # bounded
    # It reappears anywhere in the frame after the camera turned: followed again.
    status = _step(camera, now + 0.1, candidates=[], visible=[_det('cat', 0.3, 0.5, track_id=42)], mover=mover)
    assert status['state'] == 'tracking' and status['track_id'] == 42


def test_lost_mid_frame_holds_still():
    mover = _Mover()
    camera = _camera()
    _step(camera, 10.0, candidates=[_det('cat', 0.7, 0.5)], mover=mover)
    calls = len(mover.calls)
    _step(camera, _next_ready(mover, 10.0), mover=mover)
    assert len(mover.calls) == calls  # behind a bush, not walked off: no push
