"""API integration tests: RTSP event/continuous capture, clip writing, pre-roll, transcode, and camera diagnostics.

Split out of the former monolithic tests/test_api.py; the shared harness
(LocalClient, _load_app, _server, _login, _setup_admin, …) lives in
tests/support.py.
"""
from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports


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
        if '-show_entries' in command and command[command.index('-show_entries') + 1] == 'stream=codec_name':
            return subprocess.CompletedProcess(command, 0, stdout='h264\n', stderr='')
        if '-show_entries' in command and command[command.index('-show_entries') + 1] == 'stream=nb_read_packets':
            return subprocess.CompletedProcess(command, 0, stdout='0\n', stderr='')
        raise AssertionError(f'unexpected ffprobe command: {command}')

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffprobe')
    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)

    assert RecordingService.clip_has_video_stream(clip) is False
    assert len(calls) == 2


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
    import app.main as main
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
    import app.main as main
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
    import app.main as main
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
    import app.main as main
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
