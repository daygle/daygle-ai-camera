"""Clip continuation and extension markers (app/recording_extension.py).

An active clip continues on what follows its object - a still object it already
followed (briefly), or motion - and records each run of extensions so the
playback bar can mark what kept the clip going and where it would have ended.

Shared harness (``_load_app``, ``_m``) lives in tests/support.py.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tests.support import _load_app, _m

CONFIG = {'extension_step_seconds': 45, 'post_event_seconds': 15, 'max_clip_seconds': 300}


@pytest.fixture()
def clip(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()
    now = datetime.now(timezone.utc)
    recording_id = main.database.add_recording(
        event_id=None, camera_id='camera-1',
        started_at=(now - timedelta(seconds=10)).isoformat(), ended_at=now.isoformat(),
        duration_seconds=10.0, file_path=str(tmp_path / 'clip.mp4'), thumbnail_path=None,
        source='rtsp', created_at=now.isoformat(), trigger_type='object', trigger_label='car',
    )
    session = {
        'recording_id': recording_id,
        'start_capture_ts': now.timestamp() - 10,
        'capture_deadline_ts': now.timestamp() + 15,
        'max_capture_deadline_ts': now.timestamp() + 290,
        'original_deadline_ts': now.timestamp() + 15,
        'track_ids': {7},
        'last_object_ts': now.timestamp(),
        'extension_runs': [],
    }
    with main._state.active_rtsp_recordings_lock:
        main._state.active_rtsp_recordings['camera-1'] = session
    yield main, mods.recording_extension, session, now.timestamp(), recording_id
    with main._state.active_rtsp_recordings_lock:
        main._state.active_rtsp_recordings.pop('camera-1', None)


def _continue(ext, ts, motion=None, still=None):
    return ext.continue_active_recording(
        camera_id='camera-1', frame_ts=ts, motion_detections=motion,
        still_detections=still, recording_config=CONFIG,
    )


def _motion():
    return [{'label': 'motion', 'zone_id': 'driveway', 'motion_fraction': 0.02}]


def _still_car(track_id=7):
    return [{'label': 'car', 'track_id': track_id, 'motion_state': 'still'}]


def test_motion_after_the_object_keeps_the_clip_going(clip):
    main, ext, session, now, recording_id = clip
    assert _continue(ext, now + 20, motion=_motion()) == recording_id
    # Extended by the post-event time, not the 45s extension step.
    assert session['capture_deadline_ts'] == pytest.approx(now + 35)
    assert session['extension_runs'][-1]['reason'] == 'motion'


def test_nothing_happening_does_not_extend(clip):
    _main, ext, session, now, _rid = clip
    assert _continue(ext, now + 5) is None
    assert session['capture_deadline_ts'] == pytest.approx(now + 15)


def test_motion_never_starts_a_clip(clip):
    main, ext, _session, now, _rid = clip
    with main._state.active_rtsp_recordings_lock:
        main._state.active_rtsp_recordings.pop('camera-1', None)
    assert _continue(ext, now + 5, motion=_motion()) is None


def test_a_still_object_the_clip_followed_continues_it_briefly(clip):
    _main, ext, session, now, recording_id = clip
    # Within the post-event time of its last moving sighting: continues.
    assert _continue(ext, now + 10, still=_still_car()) == recording_id
    assert session['extension_runs'][-1]['reason'] == 'still'
    assert session['extension_runs'][-1]['label'] == 'car'
    deadline = session['capture_deadline_ts']
    # Past that window a parked car no longer holds the clip open.
    assert _continue(ext, now + 16, still=_still_car()) is None
    assert session['capture_deadline_ts'] == deadline


def test_a_still_object_the_clip_never_followed_does_not_continue_it(clip):
    _main, ext, session, now, _rid = clip
    assert _continue(ext, now + 5, still=_still_car(track_id=99)) is None
    assert session['extension_runs'] == []


def test_extensions_coalesce_into_runs_and_are_stored(clip):
    main, ext, session, now, recording_id = clip
    for offset in (1.0, 1.5, 2.0):
        ext.extend_active_rtsp_recording(
            camera_id='camera-1', event_time=datetime.fromtimestamp(now + offset, tz=timezone.utc).isoformat(),
            recording_config=CONFIG, detections=[{'label': 'car', 'track_id': 7}],
        )
    # The object pushed the end to now+47 (45s step); motion inside that window
    # moves nothing, so it marks nothing. Motion after it does.
    _continue(ext, now + 20.0, motion=_motion())
    for offset in (50.0, 51.0):
        _continue(ext, now + offset, motion=_motion())
    runs = session['extension_runs']
    assert [run['reason'] for run in runs] == ['object', 'motion']
    assert runs[0]['label'] == 'car'
    assert runs[0]['end'] == pytest.approx(now + 2.0)
    stored = main.database.get_recording(recording_id)['extensions']
    assert [run['reason'] for run in stored['runs']] == ['object', 'motion']
    assert stored['original_end'] == datetime.fromtimestamp(now + 15, tz=timezone.utc).isoformat()


def test_object_extensions_record_the_tracks_the_clip_follows(clip):
    _main, ext, session, now, _rid = clip
    ext.extend_active_rtsp_recording(
        camera_id='camera-1', event_time=datetime.fromtimestamp(now + 3, tz=timezone.utc).isoformat(),
        recording_config=CONFIG, detections=[{'label': 'person', 'track_id': 12}],
    )
    assert session['track_ids'] == {7, 12}
    assert session['last_object_ts'] == pytest.approx(now + 3)


def test_max_clip_duration_still_caps_continuation(clip):
    _main, ext, session, now, _rid = clip
    _continue(ext, now + 285, motion=_motion())
    assert session['capture_deadline_ts'] == pytest.approx(now + 290)


def test_recording_rows_parse_extensions_safely(clip):
    main, _ext, _session, _now, recording_id = clip
    assert main.database.get_recording(recording_id)['extensions'] is None
    with main.database.connect() as db:
        db.execute("UPDATE recordings SET extensions = 'not json' WHERE id = ?", (recording_id,))
    assert main.database.get_recording(recording_id)['extensions'] is None
