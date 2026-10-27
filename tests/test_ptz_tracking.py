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


def _zoomed_in(camera, mover, now=10.0):
    """Drive the tracker into a zoomed-in state with a centred, small target."""
    _step(camera, now, candidates=[_det('cat', 0.5, 0.5, size=0.1)], mover=mover)
    assert mover.calls[-1][3] > 0 and pt._states['cam'].zoom_in_seconds > 0
    return _next_ready(mover, now)


def test_zoom_riding_on_a_long_pan_pulse_changes_the_lens_by_one_step():
    mover = _Mover()
    camera = _camera(zoom=True, target_size=0.3)
    now = _zoomed_in(camera, mover)
    # Target jumps to the edge: pan towards it and zoom out at the same time.
    _step(camera, now, candidates=[_det('cat', 0.95, 0.5, size=0.1)], mover=mover)
    _action, pan, _tilt, zoom, duration = mover.calls[-1]
    assert pan > 0 and zoom < 0
    assert abs(zoom) * duration == pytest.approx(pt.ZOOM_VELOCITY * pt.PULSE_SECONDS)


def test_zoom_in_waits_until_the_target_has_settled():
    mover = _Mover()
    camera = _camera(zoom=True, target_size=0.4)
    _step(camera, 10.0, candidates=[_det('cat', 0.8, 0.5, size=0.1)], mover=mover)
    assert mover.calls[-1][1] > 0 and mover.calls[-1][3] == 0  # steer, no zoom
    # Centred right after steering (still walking through the middle): no zoom.
    now = _next_ready(mover, 10.0)
    calls = len(mover.calls)
    _step(camera, now, candidates=[_det('cat', 0.5, 0.5, size=0.1)], mover=mover)
    assert len(mover.calls) == calls
    # Still centred after ZOOM_SETTLED_SECONDS: now it zooms in.
    _step(camera, 10.0 + pt.ZOOM_SETTLED_SECONDS + 0.1, candidates=[_det('cat', 0.5, 0.5, size=0.1)], mover=mover)
    assert mover.calls[-1][3] > 0


def test_zoomed_in_pulses_are_shorter_and_never_boosted():
    mover = _Mover()
    wide = _Mover()
    _step(_camera(), 10.0, candidates=[_det('cat', 0.8, 0.5)], mover=wide)
    pt.reset_auto_tracking()
    camera = _camera(zoom=True, target_size=0.3)
    now = _zoomed_in(camera, mover)
    _step(camera, now, candidates=[_det('cat', 0.8, 0.5, size=0.2)], mover=mover)
    assert mover.calls[-1][-1] < wide.calls[-1][-1]
    for _ in range(3):  # escaping, but zoomed in: no boost
        now = _next_ready(mover, now)
        _step(camera, now, candidates=[_det('cat', 0.8, 0.5, size=0.2)], mover=mover)
    assert pt._states['cam'].boost == [1.0, 1.0]


def test_manual_control_forgets_the_trackers_zoom():
    mover = _Mover()
    camera = _camera(zoom=True, target_size=0.4)
    _zoomed_in(camera, mover)
    pt.note_manual_ptz('cam', now=11.0)
    assert pt._states['cam'].zoom_in_seconds == 0
    # After the pause, nothing tries to "undo" a zoom the operator may have changed.
    calls = len(mover.calls)
    _step(camera, 50.0, mover=mover)
    assert len(mover.calls) == calls or mover.calls[-1][0] == 'home'


def test_edge_push_while_zoomed_in_is_shorter_and_widens_out():
    mover = _Mover()
    camera = _camera(zoom=True, target_size=0.4, lost_seconds=10)
    now = _zoomed_in(camera, mover)
    now = _zoomed_in(camera, mover, now)  # two steps in, so one is left after the chase
    _step(camera, now, candidates=[_det('cat', 0.95, 0.5, size=0.1)], mover=mover)
    now = _next_ready(mover, now)
    _step(camera, now, mover=mover)  # vanished at the edge
    _action, pan, _tilt, zoom, duration = mover.calls[-1]
    assert pan > 0 and zoom < 0 and duration < pt.MAX_PULSE_SECONDS


def test_pelcod_never_zooms_during_a_pan_pulse():
    mover = _Mover()
    camera = _camera(zoom=True, target_size=0.3)
    camera['ptz']['protocol'] = 'tcp_pelcod'
    now = _zoomed_in(camera, mover)
    _step(camera, now, candidates=[_det('cat', 0.95, 0.5, size=0.1)], mover=mover)
    _action, pan, _tilt, zoom, _duration = mover.calls[-1]
    assert pan > 0 and zoom == 0


def test_tilt_is_scaled_to_the_frame_shape():
    """A 16:9 frame moves ~16/9 times further vertically per turn, so tilt is
    steered proportionally gentler than pan for the same offset."""
    assert pt.tilt_scale(16 / 9) == pytest.approx(9 / 16)
    assert pt.tilt_scale(4 / 3) == pytest.approx(0.75)
    assert pt.tilt_scale(None) == pytest.approx(9 / 16)
    assert pt.tilt_scale(0.5) == 1.0  # portrait: never stronger than pan
    mover = _Mover()
    det = _det('cat', 0.8, 0.2)
    det['_frame_aspect'] = 16 / 9
    _step(_camera(speed=4), 10.0, candidates=[det], mover=mover)
    _action, pan, tilt, _zoom, _duration = mover.calls[-1]
    assert tilt > 0  # target above centre: tilt up
    assert abs(tilt) == pytest.approx(abs(pan) * 9 / 16)


def test_tilt_is_never_boosted():
    """A distant person at the top edge: repeated tilt pulses that do not gain
    must not grow (that is what tilted the camera into the sky)."""
    mover = _Mover()
    camera = _camera(speed=8, labels=['person'])
    now = 10.0
    _step(camera, now, candidates=[_det('person', 0.5, 0.1)], mover=mover)
    first_tilt, first_duration = mover.calls[-1][2], mover.calls[-1][-1]
    for _ in range(4):
        now = _next_ready(mover, now)
        _step(camera, now, candidates=[_det('person', 0.5, 0.1)], mover=mover)
    assert mover.calls[-1][2] == pytest.approx(first_tilt)
    assert mover.calls[-1][-1] == pytest.approx(first_duration)


def _frame(*, shift=0):
    """A 360x640 grey frame with a gradient; ``shift`` pans the scene."""
    import numpy as np

    cols = (np.arange(640) + shift) % 640
    row = (cols * 255 // 639).astype(np.uint8)
    frame = np.tile(row, (360, 1))
    return np.stack([frame] * 3, axis=2)


def test_picture_change_tells_a_pan_from_a_still_picture():
    still = pt.picture_change(pt.picture_thumbnail(_frame()), pt.picture_thumbnail(_frame()))
    panned = pt.picture_change(pt.picture_thumbnail(_frame()), pt.picture_thumbnail(_frame(shift=120)))
    assert still is not None and still < pt.PICTURE_STILL_DIFF
    assert panned is not None and panned >= pt.PICTURE_MOVING_DIFF
    assert pt.picture_thumbnail(None) is None
    assert pt.picture_change(None, pt.picture_thumbnail(_frame())) is None


def _step_image(camera, now, image, *, candidates=(), mover=None):
    return pt.update_auto_tracking(
        'cam', camera, candidates=list(candidates), visible=list(candidates), image=image, now=now, mover=mover,
    )


def test_next_pulse_waits_until_the_picture_has_moved_and_settled():
    mover = _Mover()
    camera = _camera()
    target = _det('cat', 0.9, 0.5)
    status = _step_image(camera, 10.0, _frame(), candidates=[target], mover=mover)
    assert status['event'] == 'move' and status['move']['pan'] > 0
    duration = mover.calls[0][4]
    after = 10.0 + duration + pt.SETTLE_SECONDS + 0.1
    # The stream lags: past the fixed settle the picture has not moved yet.
    status = _step_image(camera, after, _frame(), candidates=[target], mover=mover)
    assert status['event'] == 'wait_picture' and len(mover.calls) == 1
    # The pan reaches the picture...
    status = _step_image(camera, after + 0.3, _frame(shift=120), candidates=[target], mover=mover)
    assert status['event'] == 'wait_picture' and len(mover.calls) == 1
    # ...and once two frames agree, steering resumes on the new view.
    status = _step_image(camera, after + 0.6, _frame(shift=120), candidates=[target], mover=mover)
    assert status['event'] == 'move' and len(mover.calls) == 2


def test_a_move_the_picture_never_shows_stops_being_waited_for():
    mover = _Mover()
    camera = _camera()
    target = _det('cat', 0.9, 0.5)
    _step_image(camera, 10.0, _frame(), candidates=[target], mover=mover)
    pulse_end = 10.0 + mover.calls[0][4]
    status = _step_image(camera, pulse_end + 1.0, _frame(), candidates=[target], mover=mover)
    assert status['event'] == 'wait_picture'
    status = _step_image(camera, pulse_end + pt.PICTURE_NO_MOVE_WAIT + 0.1, _frame(), candidates=[target], mover=mover)
    assert status['event'] == 'move' and len(mover.calls) == 2


def test_status_reports_each_decision():
    mover = _Mover()
    camera = _camera(dead_zone=0.1)
    status = _step(camera, 10.0, candidates=[_det('cat', 0.52, 0.5, track_id=7)], mover=mover)
    assert status['acquired'] is True and status['event'] == 'centred'
    assert status['track_id'] == 7 and status['error'] == [0.02, 0.0]
    # Followed by position onto a different track id: reported as a switch.
    status = _step(camera, 11.0, candidates=[], visible=[_det('cat', 0.55, 0.5, track_id=9)], mover=mover)
    assert status['switched_from'] == 7 and status['track_id'] == 9
    status = _step(camera, 12.0, candidates=[], visible=[], mover=mover)
    assert status['event'] == 'holding'
    status = _step(camera, 20.0, candidates=[], visible=[], mover=mover)
    assert status['event'] == 'lost' and status['lost_label'] == 'cat' and status['lost_track_id'] == 9


def test_a_person_walking_close_to_the_camera_does_not_read_as_the_picture_moving():
    still = _frame()
    walking = still.copy()
    walking[100:360, 400:560] = 255 - walking[100:360, 400:560]  # a big subject, a fifth of the frame
    change = pt.picture_change(pt.picture_thumbnail(still), pt.picture_thumbnail(walking))
    assert change is not None and change < pt.PICTURE_STILL_DIFF


def test_target_is_found_again_where_the_pulse_put_it():
    mover = _Mover()
    camera = _camera()
    status = _step(camera, 10.0, candidates=[_det('cat', 0.85, 0.5, track_id=1)], mover=mover)
    assert status['event'] == 'move'
    # The pan shifted the picture: the same cat is 0.4 away under a new track
    # id, beyond the normal follow distance, next to another cat at the edge.
    later = 10.0 + mover.calls[0][4] + pt.SETTLE_SECONDS + 0.1
    visible = [_det('cat', 0.45, 0.5, track_id=2, age=1), _det('cat', 0.97, 0.5, track_id=3, age=1)]
    status = _step(camera, later, candidates=[], visible=visible, mover=mover)
    assert status['state'] == 'tracking' and status['track_id'] == 2 and status['switched_from'] == 1
    assert status['event'] != 'holding'


def test_a_parked_car_is_never_taken_on_over_a_walking_person():
    mover = _Mover()
    camera = _camera(labels=['person', 'car'])
    parked = {**_det('car', 0.3, 0.6, track_id=1, size=0.4), 'motion_state': 'still'}
    walker = {**_det('person', 0.8, 0.5, track_id=2), 'motion_state': 'moving'}
    status = _step(camera, 10.0, candidates=[parked, walker], mover=mover)
    assert status['label'] == 'person' and status['track_id'] == 2
    # Alone, the parked car is not followed; nor is anything judged while the camera moves.
    pt.reset_auto_tracking()
    assert _step(camera, 20.0, candidates=[parked], mover=mover)['state'] == 'idle'
    assert _step(camera, 21.0, candidates=[{**walker, 'motion_state': 'unknown'}], mover=mover)['state'] == 'idle'


def test_a_target_that_turns_out_still_is_dropped_for_one_that_moves():
    mover = _Mover()
    camera = _camera(labels=['person', 'car'])
    car = {**_det('car', 0.5, 0.5, track_id=1, size=0.4), 'motion_state': 'moving'}
    assert _step(camera, 10.0, candidates=[car], mover=mover)['label'] == 'car'
    walker = {**_det('person', 0.8, 0.5, track_id=2), 'motion_state': 'moving'}
    status = _step(camera, 11.0, candidates=[{**car, 'motion_state': 'still'}, walker], mover=mover)
    assert status['label'] == 'person' and status['track_id'] == 2 and status['switched_from'] == 1


@pytest.fixture
def default_groups(monkeypatch):
    import app.label_groups as label_groups
    import app.zone_schema as zone_schema

    groups = dict(label_groups.DEFAULT_LABEL_GROUPS)
    monkeypatch.setattr(label_groups, 'cached_label_groups', lambda: groups)
    monkeypatch.setattr(zone_schema, 'cached_label_groups', lambda: groups)


def test_a_group_target_is_kept_when_its_label_changes(default_groups):
    # An IR-lit cat read as bird, then dog (recording 26881): following
    # ``animal`` keeps it as one target instead of losing the bird.
    mover = _Mover()
    camera = _camera(labels=['animal', 'person'])
    status = _step(camera, 10.0, candidates=[_det('bird', 0.8, 0.5, track_id=1)], mover=mover)
    assert status['label'] == 'bird'
    status = _step(camera, 11.2, candidates=[], visible=[_det('dog', 0.75, 0.5, track_id=2)], mover=mover)
    assert status['state'] == 'tracking' and status['track_id'] == 2 and status['label'] == 'dog'


def test_a_concrete_target_never_switches_label(default_groups):
    mover = _Mover()
    camera = _camera(labels=['cat'])
    _step(camera, 10.0, candidates=[_det('cat', 0.8, 0.5, track_id=1)], mover=mover)
    status = _step(camera, 11.2, candidates=[], visible=[_det('dog', 0.75, 0.5, track_id=2)], mover=mover)
    assert status['track_id'] == 1 and status['label'] == 'cat'


def test_a_person_target_never_turns_into_an_animal(default_groups):
    mover = _Mover()
    camera = _camera(labels=['animal', 'person'])
    _step(camera, 10.0, candidates=[_det('person', 0.8, 0.5, track_id=1)], mover=mover)
    status = _step(camera, 11.2, candidates=[], visible=[_det('bird', 0.75, 0.5, track_id=2)], mover=mover)
    assert status['track_id'] == 1 and status['label'] == 'person'


def test_auto_track_transitions_reach_the_camera_log(caplog, monkeypatch):
    # The acquire/switch/lost transitions used to be logger.info lines on the
    # application log; they now belong in the per-camera diagnostics log.
    events = []
    monkeypatch.setattr(
        pt, 'log_camera_diagnostic',
        lambda camera_id, event_type, message='', **kw: events.append(
            {'camera_id': camera_id, 'event_type': event_type, 'message': message, 'details': kw.get('details')},
        ),
    )
    with caplog.at_level('INFO'):
        mover = _Mover()
        camera = _camera(labels=['person', 'car'])
        assert _step(camera, 10.0, candidates=[_det('person', 0.8, 0.5, track_id=1)], mover=mover)['label'] == 'person'
        # Switch: the followed person parks, a moving car takes over.
        person = _det('person', 0.8, 0.5, track_id=1)
        car = _det('car', 0.2, 0.5, track_id=2)
        status = _step(
            camera, 11.0,
            candidates=[{**person, 'motion_state': 'still'}, {**car, 'motion_state': 'moving'}],
            mover=mover,
        )
        assert status['track_id'] == 2 and status['switched_from'] == 1
        # Lost: the target leaves the frame and stays gone past lost_seconds.
        status = _step(camera, 11.0 + 3.0 + 0.1 + mover.calls[-1][4], candidates=[], mover=mover)
        assert status['event'] == 'lost'

    assert [entry['event_type'] for entry in events] == ['ptz_track_acquired', 'ptz_track_switched', 'ptz_track_lost']
    assert all(entry['camera_id'] == 'cam' for entry in events)
    assert events[0]['details'] == {'label': 'person', 'track_id': 1}
    assert events[1]['details']['from_track_id'] == 1 and events[1]['details']['track_id'] == 2
    # The lost label names what was last followed (its latest label).
    assert events[2]['details']['label'] == 'car'
    assert not [record for record in caplog.records if record.levelname == 'INFO']
