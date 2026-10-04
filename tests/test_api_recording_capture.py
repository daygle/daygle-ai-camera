"""API integration tests: RTSP event/continuous capture, clip writing, pre-roll, transcode, and camera diagnostics.

Split out of the former monolithic tests/test_api.py; the shared harness
(LocalClient, _load_app, _server, _login, _setup_admin, …) lives in
tests/support.py.
"""
import json
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.support import _load_app, _m, _run_capture_with_previous_end


def test_camera_diagnostics_log_crud_and_retention(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main

    db = main.database
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    db.add_camera_diagnostic(
        created_at=old, camera_id='front-yard', camera_name='Front Yard',
        event_type='prebuffer_fallback', severity='warning', message='no buffer',
        details={'reason': 'no_segments'},
    )
    db.add_camera_diagnostic(
        created_at=old, camera_id='driveway', camera_name='Driveway',
        event_type='detection_backoff', severity='warning', message='read error',
    )

    assert db.count_camera_diagnostics() == 2
    assert db.count_camera_diagnostics(camera_id='front-yard') == 1
    assert db.count_camera_diagnostics(event_type='prebuffer_fallback') == 1
    today = datetime.now(timezone.utc).date()
    day_start = f'{today.isoformat()}T00:00:00+00:00'
    day_end = f'{(today + timedelta(days=1)).isoformat()}T00:00:00+00:00'
    assert db.count_camera_diagnostics(created_after=day_start, created_before=day_end) == 0
    front = db.list_camera_diagnostics(camera_id='front-yard')
    assert front[0]['details'] == {'reason': 'no_segments'}
    assert front[0]['camera_name'] == 'Front Yard'

    # Age-based purge removes entries older than the cutoff, keeps recent ones.
    db.add_camera_diagnostic(
        created_at=datetime.now(timezone.utc).isoformat(), camera_id='driveway', camera_name='Driveway',
        event_type='detection_recovered', severity='info', message='recovered',
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    removed = db.purge_camera_diagnostics_older_than(cutoff)
    assert removed == 2
    assert db.count_camera_diagnostics() == 1
    assert db.count_camera_diagnostics(created_after=day_start, created_before=day_end) == 1
    assert db.list_camera_diagnostics(created_after=day_start, created_before=day_end)[0]['event_type'] == 'detection_recovered'


def test_camera_diagnostics_purge_follows_retention_days(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    main.database.set_setting('recording', {'retention_days': 3}, main.utc_now())
    now = datetime.now(timezone.utc)
    # One event just inside the 3-day window, one well outside it.
    main.database.add_camera_diagnostic(
        created_at=(now - timedelta(days=1)).isoformat(), camera_id='front-yard', camera_name='Front Yard',
        event_type='detection_recovered', severity='info', message='recent',
    )
    main.database.add_camera_diagnostic(
        created_at=(now - timedelta(days=10)).isoformat(), camera_id='front-yard', camera_name='Front Yard',
        event_type='detection_backoff', severity='warning', message='old',
    )

    removed = mods.backup.purge_camera_diagnostics_by_policy()
    assert removed == 1
    remaining = main.database.list_camera_diagnostics()
    assert len(remaining) == 1
    assert remaining[0]['message'] == 'recent'


def test_detection_backoff_writes_camera_diagnostic(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    mods.event_debounce.schedule_live_camera_backoff('front-yard', 'frame read failed')
    entries = main.database.list_camera_diagnostics(camera_id='front-yard')
    assert any(e['event_type'] == 'detection_backoff' for e in entries)

    # A second failure in the same streak must not add another row (no flooding).
    mods.event_debounce.schedule_live_camera_backoff('front-yard', 'frame read failed again')
    assert main.database.count_camera_diagnostics(event_type='detection_backoff') == 1

    # Recovery after a backoff streak logs a recovered event.
    mods.event_debounce.clear_live_camera_backoff('front-yard')
    assert main.database.count_camera_diagnostics(event_type='detection_recovered') == 1


def test_extend_active_rtsp_recording_updates_trigger_label_to_specific_object(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    now = datetime.now(timezone.utc)
    started_at = (now - timedelta(seconds=5)).isoformat()
    ended_at = now.isoformat()
    file_path = tmp_path / 'data' / 'recordings' / 'extend-trigger.mp4'
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b'placeholder')

    recording_id = main.database.add_recording(
        event_id=None,
        camera_id='camera-1',
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=5.0,
        file_path=str(file_path),
        thumbnail_path=None,
        source='rtsp',
        created_at=started_at,
        trigger_type='motion',
        trigger_label='motion',
    )

    with main._state.active_rtsp_recordings_lock:
        main._state.active_rtsp_recordings['camera-1'] = {
            'recording_id': recording_id,
            'start_capture_ts': (now - timedelta(seconds=5)).timestamp(),
            'capture_deadline_ts': now.timestamp(),
            'max_capture_deadline_ts': (now + timedelta(seconds=20)).timestamp(),
        }

    extended_id = mods.recording_extension.extend_active_rtsp_recording(
        camera_id='camera-1',
        event_time=now.isoformat(),
        recording_config={'extension_step_seconds': 10},
        detections=[{'label': 'dog', 'confidence': 0.88, 'alert_triggered': True}],
    )

    assert extended_id == recording_id
    updated = main.database.get_recording(recording_id)
    assert updated is not None
    assert updated['trigger_label'] == 'dog'
    assert updated['trigger_type'] == 'alert'

    with main._state.active_rtsp_recordings_lock:
        main._state.active_rtsp_recordings.pop('camera-1', None)


@pytest.mark.parametrize(
    ('previous_end_offset', 'expect_info'),
    [
        (None, False),   # no clip has run for this camera
        (-5.0, False),   # the event is after the last clip ended: routine
        (5.0, True),     # the event lies inside an already-closed clip: failure
    ],
)
def test_extend_without_session_logs_only_events_inside_a_closed_clip(
    tmp_path, monkeypatch, caplog, previous_end_offset, expect_info,
):
    """With no active capture the extender is called every detection cycle, so
    it logs only the real failure: an event inside footage a closed clip covered,
    which landed in that clip's window but could not extend it."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(main._state, 'last_rtsp_capture_end', {})
    if previous_end_offset is not None:
        main._state.last_rtsp_capture_end['camera-1'] = now.timestamp() + previous_end_offset
    with main._state.active_rtsp_recordings_lock:
        main._state.active_rtsp_recordings.pop('camera-1', None)

    with caplog.at_level('DEBUG', logger='daygle.ai'):
        result = mods.recording_extension.extend_active_rtsp_recording(
            camera_id='camera-1',
            event_time=now.isoformat(),
            recording_config={'extension_step_seconds': 10},
        )

    assert result is None
    records = [r for r in caplog.records if 'Recording extension' in r.getMessage()]
    if expect_info:
        assert len(records) == 1, caplog.text
        assert records[0].levelname == 'INFO'
        line = records[0].getMessage()
        assert 'reason=no_active_session inside_closed_clip=True' in line
        assert 'camera camera-1' in line
        assert now.isoformat() in line
    else:
        assert not records, caplog.text


def test_recording_table_creation(tmp_path):
    from app.database import EventDatabase

    database_path = tmp_path / 'recordings.sqlite3'
    EventDatabase(str(database_path))
    with sqlite3.connect(database_path) as db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(recordings)').fetchall()}
    assert {
        'id',
        'event_id',
        'camera_id',
        'started_at',
        'ended_at',
        'duration_seconds',
        'file_path',
        'thumbnail_path',
        'source',
        'trigger_type',
        'trigger_label',
        'created_at',
    } <= columns


def test_rtsp_recording_metadata_can_skip_generated_placeholder(tmp_path):
    from app.recordings import RecordingService

    service = RecordingService({
        'storage': {'recordings_dir': str(tmp_path / 'recordings')},
        'recording': {'format': 'mp4'},
    })

    metadata = service.event_recording_metadata(
        42,
        '2026-06-06T00:00:00+00:00',
        'rtsp',
        [{'label': 'car', 'confidence': 0.8, 'alert_triggered': True}],
        write_clip=False,
    )

    assert metadata is not None
    assert metadata['source'] == 'rtsp'
    assert metadata['file_path'].endswith('.mp4')
    assert not Path(metadata['file_path']).exists()


def test_alert_recording_prefers_specific_object_label_over_motion(tmp_path):
    from app.recordings import RecordingService

    service = RecordingService({
        'storage': {'recordings_dir': str(tmp_path / 'recordings')},
        'recording': {
            'format': 'mp4',
        },
    })

    metadata = service.event_recording_metadata(
        43,
        '2026-06-06T00:00:00+00:00',
        'rtsp',
        [
            {'label': 'person', 'confidence': 0.91, 'alert_triggered': False},
            {'label': 'motion', 'confidence': 0.99, 'alert_triggered': True},
        ],
        write_clip=False,
    )

    assert metadata is not None
    assert metadata['trigger_type'] == 'alert'
    assert metadata['trigger_label'] == 'person'


def test_rtsp_recording_errors_redact_stream_password():
    from app.recordings import RecordingService

    message = RecordingService.redact_stream_credentials(
        'Error opening input file rtsp://admin:secret-password@192.168.40.101:554/live/0/MAIN.'
    )

    assert 'secret-password' not in message
    assert 'rtsp://admin:***@192.168.40.101:554/live/0/MAIN' in message


@pytest.mark.skipif(
    not shutil.which("ffmpeg"),
    reason="ffmpeg not installed; install or set PATH to run this ffmpeg-dependent test",
)
def test_rtsp_recording_capture_falls_back_on_stream_error(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    class FakeRecordingService:
        def __init__(self):
            self.rtsp_calls = 0
            self.fallback_calls = 0

        def write_rtsp_clip(self, *_args):
            self.rtsp_calls += 1
            raise RuntimeError('Stream unavailable')

        def write_event_clip(self, file_path, *_args):
            self.fallback_calls += 1
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            Path(file_path).write_text('fallback', encoding='utf-8')

    service = FakeRecordingService()
    monkeypatch.setattr(main._state, 'recording_service', service)
    stream_url = 'rtsp://admin:secret-password@192.168.40.101:554/live/0/MAIN'
    main._state.active_rtsp_recordings.clear()

    file_path = tmp_path / 'recordings' / 'event_1.mp4'
    mods.recording_extension.start_rtsp_recording_capture(
        stream_url,
        {'file_path': str(file_path), 'duration_seconds': 10, 'trigger_type': 'motion'},
        1,
        [{'label': 'person'}],
        recording_id=1,
    )

    deadline = time.time() + 3
    while not file_path.exists() and time.time() < deadline:
        time.sleep(0.05)

    assert service.rtsp_calls == 1
    assert service.fallback_calls == 1
    assert file_path.read_text(encoding='utf-8') == 'fallback'
    main._state.active_rtsp_recordings.clear()


def test_pre_roll_clamped_to_previous_clip_end(tmp_path, monkeypatch):
    """When the requested pre-roll would reach back into the previous clip for the
    same camera, it is trimmed to the gap so the clips do not overlap."""
    pre_seconds = _run_capture_with_previous_end(
        tmp_path, monkeypatch, camera_id='cam-clamp', previous_gap_seconds=4, pre_event_seconds=10,
    )
    assert pre_seconds == 4, f'pre-roll should clamp to the 4s gap, got {pre_seconds}'


def test_pre_roll_not_clamped_when_events_well_separated(tmp_path, monkeypatch):
    """When the previous clip ended well before the pre-roll window, the full
    configured pre-roll is kept untouched."""
    pre_seconds = _run_capture_with_previous_end(
        tmp_path, monkeypatch, camera_id='cam-free', previous_gap_seconds=30, pre_event_seconds=10,
    )
    assert pre_seconds == 10, f'well-separated event should keep full pre-roll, got {pre_seconds}'


def test_continuous_chunk_recording_maps_optional_audio_to_aac(tmp_path, monkeypatch):
    import app.recordings as recordings_module
    RecordingService = recordings_module.RecordingService

    service = RecordingService({
        'storage': {'recordings_dir': str(tmp_path / 'recordings')},
        'recording': {'format': 'mp4'},
    })
    stop_event = threading.Event()
    commands = []

    class FakeProcess:
        def __init__(self, command, *_args, **_kwargs):
            commands.append(command)
            stop_event.set()

        def poll(self):
            return 0

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(recordings_module.subprocess, 'Popen', FakeProcess)

    service._run_continuous_chunk_worker(
        'camera-1',
        'rtsp://example/stream',
        tmp_path / 'recordings' / 'continuous-camera-1',
        60,
        None,
        stop_event,
    )

    command = commands[0]
    assert command[command.index('-map') + 1] == '0:v:0'
    assert '0:a:0?' in command
    assert command[command.index('-c:a') + 1] == 'aac'
    assert command[command.index('-b:a') + 1] == '128k'


@pytest.mark.skipif(
    not shutil.which("ffmpeg"),
    reason="ffmpeg not installed; install or set PATH to run this ffmpeg-dependent test",
)
def test_rtsp_capture_anchors_timing_and_track_to_actual_media_window(tmp_path, monkeypatch):
    """After capture, the recording's stored started_at/ended_at and the baked
    detection track must describe the window the written media actually covers,
    not the nominal triggered_at - pre_seconds - any mismatch shows up as
    overlay boxes drifting against the video during playback."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    from collections import deque
    mods = _m()

    now = time.time()
    actual_start = now - 12.0
    clip = tmp_path / 'data' / 'recordings' / 'event_anchor.mp4'

    class FakeRecordingService:
        def prebuffer_window_seconds(self, _config=None):
            return 70

        def write_rtsp_clip_with_prebuffer(self, **kwargs):
            path = Path(kwargs['file_path'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'clip')
            return actual_start, 15.0

    monkeypatch.setattr(main._state, 'recording_service', FakeRecordingService())
    main._state.active_rtsp_recordings.clear()
    box = {'x': 0.2, 'y': 0.2, 'width': 0.3, 'height': 0.3}
    main._state.live_detection_history['camera-1'] = deque(
        [(actual_start + 1.0, [{'label': 'person', 'confidence': 0.9, 'box': box}])],
        maxlen=1200,
    )

    triggered_iso = datetime.fromtimestamp(now - 11, tz=timezone.utc).isoformat()
    recording_id = main.database.add_recording(
        event_id=None,
        camera_id='camera-1',
        started_at=datetime.fromtimestamp(now - 16, tz=timezone.utc).isoformat(),
        ended_at=datetime.fromtimestamp(now - 1, tz=timezone.utc).isoformat(),
        duration_seconds=15.0,
        file_path=str(clip),
        thumbnail_path=None,
        source='rtsp',
        created_at=main.utc_now(),
    )
    mods.recording_extension.start_rtsp_recording_capture(
        'rtsp://example/stream',
        {'file_path': str(clip), 'duration_seconds': 15, 'trigger_type': 'motion'},
        1,
        [],
        recording_id=recording_id,
        camera_id='camera-1',
        event_time=triggered_iso,
        recording_config={'pre_event_seconds': 5, 'post_event_seconds': 10, 'max_clip_seconds': 60},
    )

    sidecar = mods.recording_extension.recording_track_sidecar_path(clip)
    deadline = time.time() + 3
    while not sidecar.exists() and time.time() < deadline:
        time.sleep(0.05)

    recording = main.database.get_recording(recording_id)
    assert datetime.fromisoformat(recording['started_at']).timestamp() == pytest.approx(actual_start, abs=0.01)
    assert recording['duration_seconds'] == pytest.approx(15.0)
    track = json.loads(sidecar.read_text(encoding='utf-8'))
    # The history sample 1s into the actual media window must land at t=1.0.
    assert track[0]['t'] == pytest.approx(1.0, abs=0.01)
    assert track[0]['detections'][0]['label'] == 'person'
    main._state.active_rtsp_recordings.clear()


def test_late_event_after_capture_timing_write_keeps_rendered_duration(tmp_path, monkeypatch):
    """A late event that lands after the capture saved the rendered timing (but
    before it retired the session) must not rewrite started/ended/duration: the
    row has to keep describing the file, not the extension horizon."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    now = time.time()
    clip = tmp_path / 'data' / 'recordings' / 'event_late_timing.mp4'
    late_results: list = []

    class FakeRecordingService:
        def prebuffer_window_seconds(self, _config=None):
            return 70

        def write_rtsp_clip_with_prebuffer(self, **kwargs):
            path = Path(kwargs['file_path'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'clip')
            return now - 30.0, 25.0

        def should_record(self, detections, config):
            return False, 'motion', None

    monkeypatch.setattr(main._state, 'recording_service', FakeRecordingService())
    real_track_writer = mods.recording_extension.write_live_history_detection_track

    def track_writer_with_late_event(*args, **kwargs):
        # Runs right after capture() wrote the rendered timing and before its
        # ``finally`` pops the session: the exact window of the race.
        late_results.append(mods.recording_extension.extend_active_rtsp_recording(
            camera_id='camera-1',
            event_time=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            recording_config={'extension_step_seconds': 30},
            detections=[{'label': 'person', 'confidence': 0.9}],
        ))
        return real_track_writer(*args, **kwargs)

    monkeypatch.setattr(
        mods.recording_extension, 'write_live_history_detection_track', track_writer_with_late_event,
    )
    main._state.active_rtsp_recordings.clear()
    main._state.last_rtsp_capture_end.pop('camera-1', None)

    recording_id = main.database.add_recording(
        event_id=None,
        camera_id='camera-1',
        started_at=datetime.fromtimestamp(now - 30, tz=timezone.utc).isoformat(),
        ended_at=datetime.fromtimestamp(now - 15, tz=timezone.utc).isoformat(),
        duration_seconds=15.0,
        file_path=str(clip),
        thumbnail_path=None,
        source='rtsp',
        created_at=main.utc_now(),
    )
    mods.recording_extension.start_rtsp_recording_capture(
        'rtsp://example/stream',
        {'file_path': str(clip), 'duration_seconds': 15, 'trigger_type': 'motion'},
        1,
        [],
        recording_id=recording_id,
        camera_id='camera-1',
        event_time=datetime.fromtimestamp(now - 25, tz=timezone.utc).isoformat(),
        recording_config={'pre_event_seconds': 5, 'post_event_seconds': 10, 'max_clip_seconds': 60},
    )
    wait_until = time.time() + 3
    while time.time() < wait_until:
        if late_results and 'camera-1' not in main._state.active_rtsp_recordings:
            break
        time.sleep(0.05)

    # The capture's deadline had frozen, so the late extension is refused (its
    # caller would open a new clip) instead of being absorbed by this one.
    assert late_results == [None]
    recording = main.database.get_recording(recording_id)
    assert recording['duration_seconds'] == pytest.approx(25.0)
    assert datetime.fromisoformat(recording['started_at']).timestamp() == pytest.approx(now - 30.0, abs=0.01)
    assert datetime.fromisoformat(recording['ended_at']).timestamp() == pytest.approx(now - 5.0, abs=0.01)
    main._state.active_rtsp_recordings.clear()





def test_extension_still_writes_provisional_timing_before_capture_writes(tmp_path, monkeypatch):
    """Until the capture has written the rendered timing, an extension keeps
    writing its provisional ended_at/duration - that is what leaves a plausible
    duration on the row if the capture later dies."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    now = datetime.now(timezone.utc)
    recording_id = main.database.add_recording(
        event_id=None,
        camera_id='camera-1',
        started_at=(now - timedelta(seconds=10)).isoformat(),
        ended_at=now.isoformat(),
        duration_seconds=10.0,
        file_path=str(tmp_path / 'data' / 'recordings' / 'provisional.mp4'),
        thumbnail_path=None,
        source='rtsp',
        created_at=now.isoformat(),
    )
    with main._state.active_rtsp_recordings_lock:
        main._state.active_rtsp_recordings['camera-1'] = {
            'recording_id': recording_id,
            'start_capture_ts': now.timestamp() - 10,
            'capture_deadline_ts': now.timestamp(),
            'max_capture_deadline_ts': now.timestamp() + 300,
        }

    assert mods.recording_extension.extend_active_rtsp_recording(
        camera_id='camera-1',
        event_time=now.isoformat(),
        recording_config={'extension_step_seconds': 30},
    ) == recording_id
    assert main.database.get_recording(recording_id)['duration_seconds'] == pytest.approx(40.0)

    # Once the capture flags its final write, the extension stops touching timing.
    with main._state.active_rtsp_recordings_lock:
        main._state.active_rtsp_recordings['camera-1']['timing_written'] = True
    later = now + timedelta(seconds=20)
    assert mods.recording_extension.extend_active_rtsp_recording(
        camera_id='camera-1',
        event_time=later.isoformat(),
        recording_config={'extension_step_seconds': 30},
    ) == recording_id
    assert main.database.get_recording(recording_id)['duration_seconds'] == pytest.approx(40.0)
    with main._state.active_rtsp_recordings_lock:
        main._state.active_rtsp_recordings.pop('camera-1', None)


def test_extend_refuses_a_session_whose_deadline_has_frozen(tmp_path, monkeypatch):
    """Once the capture has stopped waiting on its deadline, its window can no
    longer grow: an extension must refuse (so the caller starts a new clip)
    without moving the deadline or touching the old clip's row."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    now = datetime.now(timezone.utc)
    recording_id = main.database.add_recording(
        event_id=None,
        camera_id='camera-1',
        started_at=(now - timedelta(seconds=10)).isoformat(),
        ended_at=now.isoformat(),
        duration_seconds=10.0,
        file_path=str(tmp_path / 'data' / 'recordings' / 'frozen.mp4'),
        thumbnail_path=None,
        source='rtsp',
        created_at=now.isoformat(),
    )
    with main._state.active_rtsp_recordings_lock:
        main._state.active_rtsp_recordings['camera-1'] = {
            'recording_id': recording_id,
            'start_capture_ts': now.timestamp() - 10,
            'capture_deadline_ts': now.timestamp(),
            'max_capture_deadline_ts': now.timestamp() + 300,
            'deadline_frozen': True,
        }

    assert mods.recording_extension.extend_active_rtsp_recording(
        camera_id='camera-1',
        event_time=now.isoformat(),
        recording_config={'extension_step_seconds': 30},
        detections=[{'label': 'person', 'confidence': 0.9}],
    ) is None
    with main._state.active_rtsp_recordings_lock:
        assert main._state.active_rtsp_recordings['camera-1']['capture_deadline_ts'] == now.timestamp()
        main._state.active_rtsp_recordings.pop('camera-1', None)
    assert main.database.get_recording(recording_id)['duration_seconds'] == pytest.approx(10.0)


class _LateEventHarness:
    """Drive a first clip's capture and deliver a second, fresh event through
    ``attach_event_recording`` at a chosen point after the first clip's
    deadline wait has ended."""

    CONFIG = {'pre_event_seconds': 5, 'post_event_seconds': 10, 'max_clip_seconds': 60}
    # No post-roll for the late event so its own clip renders without waiting.
    LATE_CONFIG = {'pre_event_seconds': 5, 'post_event_seconds': 0, 'max_clip_seconds': 60}

    def __init__(self, tmp_path, monkeypatch):
        _load_app(tmp_path, monkeypatch)
        import app.main as main
        import app.utils
        self.main = main
        self.mods = _m()
        self.tmp_path = tmp_path
        self.renders: list[dict] = []
        self.late: dict = {}
        harness = self

        class FakeRecordingService:
            def prebuffer_window_seconds(self, _config=None):
                return 70

            def event_recording_metadata(
                self, event_id, event_time, source, detections, write_clip=False, recording_config=None,
            ):
                return {
                    'event_id': event_id,
                    'camera_id': 'camera-1',
                    'started_at': event_time,
                    'ended_at': event_time,
                    'duration_seconds': 15,
                    'file_path': str(tmp_path / 'data' / 'recordings' / f'clip_{event_id}.mp4'),
                    'thumbnail_path': None,
                    'source': source,
                    'trigger_type': 'motion',
                }

            def should_record(self, detections, config):
                return False, 'motion', None

            def write_rtsp_clip_with_prebuffer(self, **kwargs):
                harness.renders.append(kwargs)
                if harness.deliver_at == 'render' and len(harness.renders) == 1:
                    harness.deliver_late_event()
                path = Path(kwargs['file_path'])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'clip')
                return time.time() - 20.0, 15.0

        monkeypatch.setattr(main._state, 'recording_service', FakeRecordingService())
        monkeypatch.setattr(app.utils, 'build_stream_url', lambda _cfg: 'rtsp://example/stream')
        main._state.active_rtsp_recordings.clear()
        main._state.last_rtsp_capture_end.pop('camera-1', None)
        import app.postprocess_pool as postprocess_pool
        self.pool = postprocess_pool.clip_pool()

    def deliver_late_event(self):
        event_id = self.main.database.add_event_with_alerts(
            created_at=self.main.utc_now(), source='rtsp', snapshot_path=None, thumbnail_path=None,
            detections=[], alerts=[], alert_triggered=False, metadata={'camera_id': 'camera-1'},
        )
        self.late['event_id'] = event_id
        self.late['recording_id'] = self.mods.recording_extension.attach_event_recording(
            event_id, datetime.now(timezone.utc).isoformat(), 'rtsp',
            [{'label': 'person', 'confidence': 0.9}], camera_id='camera-1',
            recording_config=self.LATE_CONFIG,
        )

    def run_first_clip(self, deliver_at: str) -> int:
        self.deliver_at = deliver_at
        now = time.time()
        clip = self.tmp_path / 'data' / 'recordings' / 'clip_first.mp4'
        recording_id = self.main.database.add_recording(
            event_id=None,
            camera_id='camera-1',
            started_at=datetime.fromtimestamp(now - 30, tz=timezone.utc).isoformat(),
            ended_at=datetime.fromtimestamp(now - 15, tz=timezone.utc).isoformat(),
            duration_seconds=15.0,
            file_path=str(clip),
            thumbnail_path=None,
            source='rtsp',
            created_at=self.main.utc_now(),
        )
        # Trigger 25s ago with a 10s post window: the deadline has passed, so
        # the wait ends (and freezes) straight away.
        self.mods.recording_extension.start_rtsp_recording_capture(
            'rtsp://example/stream',
            {'file_path': str(clip), 'duration_seconds': 15, 'trigger_type': 'motion'},
            1,
            [],
            recording_id=recording_id,
            camera_id='camera-1',
            event_time=datetime.fromtimestamp(now - 25, tz=timezone.utc).isoformat(),
            recording_config=self.CONFIG,
        )
        return recording_id

    def wait_for_renders(self, count: int) -> None:
        """Wait until ``count`` renders have started AND every clip job has
        finished (render, timing write, session retirement), so assertions and
        cleanup never race a still-running capture."""
        deadline = time.time() + 5
        while time.time() < deadline and len(self.renders) < count:
            time.sleep(0.05)
        assert self.pool.wait_until_idle(timeout=5), 'clip jobs did not finish'
        with self.main._state.active_rtsp_recordings_lock:
            assert 'camera-1' not in self.main._state.active_rtsp_recordings, 'a session was not retired'
        for render in self.renders:
            assert Path(render['file_path']).exists(), f"{render['file_path']} was not written"

    def cleanup(self):
        with self.main._state.active_rtsp_recordings_lock:
            self.main._state.active_rtsp_recordings.clear()


def test_event_arriving_while_previous_clip_renders_gets_its_own_clip(tmp_path, monkeypatch):
    """Render phase: the first clip's window is already fixed. The late event
    used to be linked to that clip (which does not contain it) and no capture
    started, losing its footage; it must open its own clip instead."""
    harness = _LateEventHarness(tmp_path, monkeypatch)
    first_id = harness.run_first_clip(deliver_at='render')
    harness.wait_for_renders(2)

    late_id = harness.late.get('recording_id')
    assert late_id is not None and late_id != first_id
    assert harness.main.database.get_event(harness.late['event_id'])['recording_id'] == late_id
    assert len(harness.renders) == 2
    assert Path(harness.renders[1]['file_path']).name == f"clip_{harness.late['event_id']}.mp4"
    harness.cleanup()


def test_event_arriving_while_previous_clip_waits_in_render_queue_gets_its_own_clip(tmp_path, monkeypatch):
    """Queue phase: the deadline wait has ended but the render has not started.
    The late event used to be absorbed into the first clip with its post-roll
    cut off at the render start; it must open its own clip instead, and the
    first clip's render is unaffected."""
    harness = _LateEventHarness(tmp_path, monkeypatch)
    import app.postprocess_pool as postprocess_pool
    real_pool = harness.pool
    frozen_at_submit: list[bool] = []

    class QueueDelayingPool:
        def submit(self, fn, *args, **kwargs):
            if not harness.late:
                # The first clip is now queued: its wait has ended (frozen) and
                # its render has not started. Deliver the late event here.
                with harness.main._state.active_rtsp_recordings_lock:
                    session = harness.main._state.active_rtsp_recordings.get('camera-1') or {}
                    frozen_at_submit.append(bool(session.get('deadline_frozen')))
                harness.deliver_late_event()
            return real_pool.submit(fn, *args, **kwargs)

    monkeypatch.setattr(postprocess_pool, 'clip_pool', lambda: QueueDelayingPool())
    first_id = harness.run_first_clip(deliver_at='queue')
    harness.wait_for_renders(2)

    # The freeze happens in wait_for_deadline, before the clip is queued.
    assert frozen_at_submit == [True]
    late_id = harness.late.get('recording_id')
    assert late_id is not None and late_id != first_id
    assert harness.main.database.get_event(harness.late['event_id'])['recording_id'] == late_id
    assert len(harness.renders) == 2
    rendered_files = sorted(Path(r['file_path']).name for r in harness.renders)
    assert rendered_files == sorted(['clip_first.mp4', f"clip_{harness.late['event_id']}.mp4"])
    harness.cleanup()


def test_capture_end_boundary_never_moves_backwards(tmp_path, monkeypatch):
    """Two captures for one camera can render at once (a late event opens a new
    clip while the frozen one finishes). If the older finishes last, it must not
    move ``last_rtsp_capture_end`` backwards: the next clip's pre-roll clamp
    would re-record footage the newer clip already captured."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.postprocess_pool as postprocess_pool
    mods = _m()

    now = time.time()
    clip = tmp_path / 'data' / 'recordings' / 'older_clip.mp4'

    class FakeRecordingService:
        def prebuffer_window_seconds(self, _config=None):
            return 70

        def write_rtsp_clip_with_prebuffer(self, **kwargs):
            path = Path(kwargs['file_path'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'clip')
            # The older clip's footage ends 5s ago...
            return now - 30.0, 25.0

    monkeypatch.setattr(main._state, 'recording_service', FakeRecordingService())
    main._state.active_rtsp_recordings.clear()
    # ...but a newer clip for the same camera already finished, ending later.
    newer_end = now + 10.0
    main._state.last_rtsp_capture_end['camera-1'] = newer_end

    recording_id = main.database.add_recording(
        event_id=None,
        camera_id='camera-1',
        started_at=datetime.fromtimestamp(now - 30, tz=timezone.utc).isoformat(),
        ended_at=datetime.fromtimestamp(now - 15, tz=timezone.utc).isoformat(),
        duration_seconds=15.0,
        file_path=str(clip),
        thumbnail_path=None,
        source='rtsp',
        created_at=main.utc_now(),
    )
    mods.recording_extension.start_rtsp_recording_capture(
        'rtsp://example/stream',
        {'file_path': str(clip), 'duration_seconds': 15, 'trigger_type': 'motion'},
        1,
        [],
        recording_id=recording_id,
        camera_id='camera-1',
        event_time=datetime.fromtimestamp(now - 25, tz=timezone.utc).isoformat(),
        recording_config={'pre_event_seconds': 5, 'post_event_seconds': 10, 'max_clip_seconds': 60},
    )
    wait_until = time.time() + 5
    while time.time() < wait_until and not clip.exists():
        time.sleep(0.05)
    assert postprocess_pool.clip_pool().wait_until_idle(timeout=5)

    assert main._state.last_rtsp_capture_end['camera-1'] == pytest.approx(newer_end, abs=0.001)
    main._state.last_rtsp_capture_end.pop('camera-1', None)
    main._state.active_rtsp_recordings.clear()


def _session_at_max_clip_ceiling(main, now, recording_id):
    """Register a capture whose deadline is pinned at its Max Clip Duration
    ceiling, with the ceiling still 20s in the future (still recording)."""
    ceiling = now.timestamp() + 20
    with main._state.active_rtsp_recordings_lock:
        main._state.active_rtsp_recordings['camera-1'] = {
            'recording_id': recording_id,
            'start_capture_ts': ceiling - 300,
            'capture_deadline_ts': ceiling,
            'max_capture_deadline_ts': ceiling,
        }
    return ceiling


def test_event_while_clip_sits_at_max_clip_ceiling_starts_a_follow_on_clip(tmp_path, monkeypatch):
    """A clip whose deadline is already at max_clip_seconds cannot grow. A fresh
    event then used to be linked to it (its post-roll past the ceiling lost)
    and no follow-on clip started; it must start its own clip - the split at
    Max Clip Duration the pre-roll clamp comment describes."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.utils
    mods = _m()

    now = datetime.now(timezone.utc)
    old_id = main.database.add_recording(
        event_id=None,
        camera_id='camera-1',
        started_at=(now - timedelta(seconds=280)).isoformat(),
        ended_at=(now + timedelta(seconds=20)).isoformat(),
        duration_seconds=300.0,
        file_path=str(tmp_path / 'data' / 'recordings' / 'capped.mp4'),
        thumbnail_path=None,
        source='rtsp',
        created_at=now.isoformat(),
    )
    _session_at_max_clip_ceiling(main, now, old_id)

    class FakeRecordingService:
        def event_recording_metadata(
            self, event_id, event_time, source, detections, write_clip=False, recording_config=None,
        ):
            return {
                'event_id': event_id,
                'started_at': event_time,
                'ended_at': event_time,
                'duration_seconds': 15,
                'file_path': str(tmp_path / 'data' / 'recordings' / f'clip_{event_id}.mp4'),
                'thumbnail_path': None,
                'source': source,
                'trigger_type': 'motion',
            }

    started: list[dict] = []
    monkeypatch.setattr(main._state, 'recording_service', FakeRecordingService())
    monkeypatch.setattr(app.utils, 'build_stream_url', lambda _cfg: 'rtsp://example/stream')
    monkeypatch.setattr(
        mods.recording_extension, 'start_rtsp_recording_capture',
        lambda *args, **kwargs: started.append(kwargs),
    )
    event_id = main.database.add_event_with_alerts(
        created_at=now.isoformat(), source='rtsp', snapshot_path=None, thumbnail_path=None,
        detections=[], alerts=[], alert_triggered=False, metadata={'camera_id': 'camera-1'},
    )

    new_id = mods.recording_extension.attach_event_recording(
        event_id, now.isoformat(), 'rtsp', [{'label': 'person', 'confidence': 0.9}],
        camera_id='camera-1',
        recording_config={'pre_event_seconds': 5, 'post_event_seconds': 30,
                          'extension_step_seconds': 30, 'max_clip_seconds': 300},
    )

    assert new_id is not None and new_id != old_id
    assert main.database.get_event(event_id)['recording_id'] == new_id
    assert [call['recording_id'] for call in started] == [new_id]
    # The capped clip is left as it was: still ending at its ceiling.
    with main._state.active_rtsp_recordings_lock:
        main._state.active_rtsp_recordings.pop('camera-1', None)
    assert main.database.get_recording(old_id)['duration_seconds'] == pytest.approx(300.0)


def test_extension_within_a_capped_clip_still_links_to_it(tmp_path, monkeypatch):
    """Only an event the capped clip cannot cover is refused: one whose horizon
    the clip already covers is linked as before, and an extension below the
    ceiling still grows the clip up to it."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    now = datetime.now(timezone.utc)
    recording_id = 77
    ceiling = _session_at_max_clip_ceiling(main, now, recording_id)
    config = {'extension_step_seconds': 10}

    # Horizon now+10 is inside the clip (ceiling is now+20): already covered.
    assert mods.recording_extension.extend_active_rtsp_recording(
        camera_id='camera-1', event_time=now.isoformat(), recording_config=config,
    ) == recording_id
    # Horizon now+30 is past the ceiling the deadline already sits at: refused.
    assert mods.recording_extension.extend_active_rtsp_recording(
        camera_id='camera-1', event_time=(now + timedelta(seconds=20)).isoformat(), recording_config=config,
    ) is None
    with main._state.active_rtsp_recordings_lock:
        assert main._state.active_rtsp_recordings['camera-1']['capture_deadline_ts'] == ceiling
        # Below the ceiling: an extension past it still grows the clip to it.
        main._state.active_rtsp_recordings['camera-1']['capture_deadline_ts'] = ceiling - 15
    monkeypatch.setattr(main.database, 'update_recording_timing', lambda *a, **k: None)
    assert mods.recording_extension.extend_active_rtsp_recording(
        camera_id='camera-1', event_time=(now + timedelta(seconds=15)).isoformat(), recording_config=config,
    ) == recording_id
    with main._state.active_rtsp_recordings_lock:
        assert main._state.active_rtsp_recordings['camera-1']['capture_deadline_ts'] == ceiling
        main._state.active_rtsp_recordings.pop('camera-1', None)


def test_capture_that_loses_its_slot_still_waits_out_its_deadline(tmp_path, monkeypatch):
    """When a follow-on clip takes the camera's slot before the old capture's
    deadline (a fresh event at the Max Clip Duration ceiling), the old capture
    must keep waiting in its own thread and render after its deadline - not
    queue the render at once and sleep inside a clip-pool worker."""
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    import app.postprocess_pool as postprocess_pool
    mods = _m()

    now = time.time()
    clip = tmp_path / 'data' / 'recordings' / 'replaced.mp4'
    render_started: list[float] = []

    class FakeRecordingService:
        def prebuffer_window_seconds(self, _config=None):
            return 70

        def write_rtsp_clip_with_prebuffer(self, **kwargs):
            render_started.append(time.time())
            path = Path(kwargs['file_path'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'clip')
            return now - 5.0, 7.0

    monkeypatch.setattr(main._state, 'recording_service', FakeRecordingService())
    main._state.active_rtsp_recordings.clear()
    recording_id = main.database.add_recording(
        event_id=None,
        camera_id='camera-1',
        started_at=datetime.fromtimestamp(now - 5, tz=timezone.utc).isoformat(),
        ended_at=datetime.fromtimestamp(now + 2, tz=timezone.utc).isoformat(),
        duration_seconds=7.0,
        file_path=str(clip),
        thumbnail_path=None,
        source='rtsp',
        created_at=main.utc_now(),
    )
    # Trigger 1s ago with a 3s post window: the deadline is 2s away.
    mods.recording_extension.start_rtsp_recording_capture(
        'rtsp://example/stream',
        {'file_path': str(clip), 'duration_seconds': 7.0, 'trigger_type': 'motion'},
        1,
        [],
        recording_id=recording_id,
        camera_id='camera-1',
        event_time=datetime.fromtimestamp(now - 1, tz=timezone.utc).isoformat(),
        recording_config={'pre_event_seconds': 4, 'post_event_seconds': 3, 'max_clip_seconds': 60},
    )
    deadline = now + 2.0
    # A follow-on clip takes the slot straight away.
    with main._state.active_rtsp_recordings_lock:
        main._state.active_rtsp_recordings['camera-1'] = {
            'recording_id': recording_id + 1000,
            'start_capture_ts': now,
            'capture_deadline_ts': now + 60,
            'max_capture_deadline_ts': now + 60,
        }
    wait_until = time.time() + 5
    while time.time() < wait_until and not render_started:
        time.sleep(0.05)
    assert postprocess_pool.clip_pool().wait_until_idle(timeout=5)

    assert render_started, 'the old capture never rendered'
    assert render_started[0] >= deadline - 0.05, 'rendered before its deadline'
    with main._state.active_rtsp_recordings_lock:
        # The old capture never touched (froze or retired) the new session.
        session = main._state.active_rtsp_recordings.get('camera-1')
        assert session is not None and session['recording_id'] == recording_id + 1000
        assert 'deadline_frozen' not in session
        main._state.active_rtsp_recordings.clear()


def test_write_rtsp_clip_rejects_videoless_output(tmp_path, monkeypatch):
    # ffmpeg can exit 0 while discarding every corrupt frame, leaving a non-empty
    # file with no video stream. write_rtsp_clip must reject it (so the caller
    # falls back to a playable clip) rather than saving an unplayable recording.
    import app.recordings as recordings_module
    RecordingService = recordings_module.RecordingService

    service = RecordingService({
        'storage': {'recordings_dir': str(tmp_path / 'recordings')},
        'recording': {'format': 'mp4'},
    })

    def fake_run(command, *_args, **_kwargs):
        # The output path is the last positional arg in the ffmpeg command.
        Path(command[-1]).write_bytes(b'not-a-real-video')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)
    monkeypatch.setattr(RecordingService, 'clip_has_video_stream', staticmethod(lambda _path: False))

    file_path = tmp_path / 'recordings' / 'event_videoless.mp4'
    with pytest.raises(RuntimeError, match='no decodable video stream'):
        service.write_rtsp_clip('rtsp://example/stream', file_path, 5.0)

    # Neither the final clip nor the temp file should survive a videoless capture.
    assert not file_path.exists()
    assert not file_path.with_name(f'{file_path.stem}.recording.tmp{file_path.suffix}').exists()


def test_clip_has_video_stream_rejects_declared_stream_with_zero_packets(tmp_path, monkeypatch):
    import app.recordings as recordings_module
    RecordingService = recordings_module.RecordingService

    clip = tmp_path / 'audio_only_with_video_header.mp4'
    clip.write_bytes(b'not-empty')
    calls = []

    def fake_run(command, *_args, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"streams":[{"codec_name":"h264","duration":"2.5","nb_read_packets":"0"}],"format":{"duration":"2.5"}}',
            stderr='',
        )

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffprobe')
    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)

    assert RecordingService.clip_has_video_stream(clip) is False
    assert RecordingService.clip_video_packet_count(clip) == 0
    assert RecordingService.clip_duration_seconds(clip) == pytest.approx(2.5)
    assert len(calls) == 1


def test_write_rtsp_clip_keeps_clip_with_video_stream(tmp_path, monkeypatch):
    import app.recordings as recordings_module
    RecordingService = recordings_module.RecordingService

    service = RecordingService({
        'storage': {'recordings_dir': str(tmp_path / 'recordings')},
        'recording': {'format': 'mp4'},
    })

    def fake_run(command, *_args, **_kwargs):
        Path(command[-1]).write_bytes(b'valid-video-bytes')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)
    monkeypatch.setattr(RecordingService, 'clip_has_video_stream', staticmethod(lambda _path: True))

    file_path = tmp_path / 'recordings' / 'event_ok.mp4'
    service.write_rtsp_clip('rtsp://example/stream', file_path, 5.0)

    assert file_path.exists()
    assert not file_path.with_name(f'{file_path.stem}.recording.tmp{file_path.suffix}').exists()


def test_write_rtsp_clip_explicitly_records_optional_audio_as_aac(tmp_path, monkeypatch):
    import app.recordings as recordings_module
    RecordingService = recordings_module.RecordingService

    service = RecordingService({
        'storage': {'recordings_dir': str(tmp_path / 'recordings')},
        'recording': {'format': 'mp4'},
    })

    commands = []

    def fake_run(command, *_args, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b'valid-video-bytes')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)
    monkeypatch.setattr(RecordingService, 'clip_has_video_stream', staticmethod(lambda _path: True))

    service.write_rtsp_clip('rtsp://example/stream', tmp_path / 'recordings' / 'event_audio.mp4', 5.0)

    command = commands[0]
    assert command[command.index('-map') + 1] == '0:v:0'
    assert '0:a:0?' in command
    assert command[command.index('-c:a') + 1] == 'aac'
    assert command[command.index('-b:a') + 1] == '128k'


def test_playback_transcode_preserves_optional_audio_stream(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    mods = _m()

    commands = []

    def fake_run(command, *_args, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b'playback-video')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    import app.media_utils as _media_utils
    monkeypatch.setattr(_media_utils.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(_media_utils.subprocess, 'run', fake_run)
    monkeypatch.setattr(_media_utils, 'probe_video_duration', lambda _path: 5.0)
    monkeypatch.setattr(_media_utils, 'mp4_has_video_stream', lambda _path: True)

    source_path = tmp_path / 'source.mkv'
    output_path = mods.media_utils.recording_playback_sidecar_path(source_path)
    source_path.write_bytes(b'input-video')

    mods.media_utils.transcode_recording_to_mp4(source_path, output_path)

    command = commands[0]
    assert output_path.name == 'source.h264-audio.mp4'
    assert '-an' not in command
    assert command[command.index('-map') + 1] == '0:v:0'
    assert '0:a:0?' in command
    assert command[command.index('-c:a') + 1] == 'aac'
    assert output_path.exists()


def test_h264_mp4_with_browser_playable_audio_streams_directly(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    mods = _m()

    import app.media_utils as _media_utils
    source_path = tmp_path / 'source.mp4'
    source_path.write_bytes(b'input-video')
    monkeypatch.setattr(_media_utils, 'probe_video_codec', lambda _path: 'h264')
    monkeypatch.setattr(_media_utils, 'probe_audio_codec', lambda _path: 'aac')

    def fail_transcode(*_args, **_kwargs):
        raise AssertionError('browser-playable MP4 should not be transcoded')

    monkeypatch.setattr(_media_utils, 'transcode_recording_to_mp4', fail_transcode)

    assert mods.media_utils.recording_stream_path(source_path) == source_path


def test_h264_mp4_without_audio_streams_directly(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    mods = _m()
    import app.media_utils as _media_utils

    source_path = tmp_path / 'source.mp4'
    source_path.write_bytes(b'input-video')
    monkeypatch.setattr(_media_utils, 'probe_video_codec', lambda _path: 'h264')
    monkeypatch.setattr(_media_utils, 'probe_audio_codec', lambda _path: None)

    def fail_transcode(*_args, **_kwargs):
        raise AssertionError('video-only MP4 should not be transcoded')

    monkeypatch.setattr(_media_utils, 'transcode_recording_to_mp4', fail_transcode)

    assert mods.media_utils.recording_stream_path(source_path) == source_path


@pytest.mark.skipif(
    not shutil.which("ffmpeg"),
    reason="ffmpeg not installed; install or set PATH to run this ffmpeg-dependent test",
)
def test_h264_mp4_with_unsupported_audio_is_transcoded_for_playback(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.media_utils as _media_utils

    source_path = tmp_path / 'source.mp4'
    source_path.write_bytes(b'input-video')
    monkeypatch.setattr(_media_utils, 'probe_video_codec', lambda _path: 'h264')
    monkeypatch.setattr(_media_utils, 'probe_audio_codec', lambda _path: 'pcm_mulaw')

    transcoded = []

    def fake_transcode(input_path, output_path):
        transcoded.append((input_path, output_path))
        output_path.write_bytes(b'playback-video-with-aac')

    monkeypatch.setattr(_media_utils, 'transcode_recording_to_mp4', fake_transcode)

    import app.media_utils as _mu
    stream_path = _mu.recording_stream_path(source_path)

    assert stream_path == _mu.recording_playback_sidecar_path(source_path)
    assert stream_path.exists()
    assert transcoded == [(source_path, stream_path)]


def test_hevc_mp4_is_preserved_but_uses_h264_playback_sidecar(tmp_path, monkeypatch):
    import app.media_utils as _media_utils

    source_path = tmp_path / 'source.mp4'
    source_path.write_bytes(b'hevc-video')
    transcoded = []

    def fake_transcode(input_path, output_path):
        transcoded.append((input_path, output_path))
        output_path.write_bytes(b'browser-h264-video')

    monkeypatch.setattr(_media_utils, 'probe_video_codec', lambda _path: 'hevc')
    monkeypatch.setattr(_media_utils, 'probe_audio_codec', lambda _path: 'aac')
    monkeypatch.setattr(_media_utils, 'transcode_recording_to_mp4', fake_transcode)

    stream_path = _media_utils.recording_stream_path(source_path)

    assert stream_path == _media_utils.recording_playback_sidecar_path(source_path)
    assert stream_path.exists()
    assert transcoded == [(source_path, stream_path)]
    assert _media_utils.is_hevc_codec('hevc')
    assert _media_utils.is_hevc_codec('h265+')
    assert _media_utils.normalize_video_codec('h265+') == 'hevc'
    assert _media_utils.video_codec_label('h265+') == 'H.265/HEVC'


def test_ffmpeg_decoder_availability_handles_h264_and_hevc(monkeypatch):
    import app.media_utils as _media_utils

    monkeypatch.setattr(_media_utils, '_FFMPEG', '/usr/bin/ffmpeg')

    def fake_run(command, **_kwargs):
        assert command == ['/usr/bin/ffmpeg', '-hide_banner', '-decoders']
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=' V.....D h264 ...\n VFS..D hevc ...\n',
            stderr='',
        )

    monkeypatch.setattr(_media_utils.subprocess, 'run', fake_run)

    assert _media_utils.ffmpeg_decoder_available('h264') is True
    assert _media_utils.ffmpeg_decoder_available('h265+') is True
    assert _media_utils.ffmpeg_decoder_available('vp9') is None
