"""API integration tests: RTSP ingest workers, prebuffer segments, and clip-duration probing.

Split out of the former monolithic tests/test_api.py; the shared harness
(LocalClient, _load_app, _server, _login, _setup_admin, …) lives in
tests/support.py.
"""
from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports


def test_detection_backoff_keeps_prebuffer_warm(tmp_path, monkeypatch):
    # A camera in *detection* backoff (transient frame-read/inference errors)
    # must still have its recording prebuffer maintained, otherwise the next
    # event has no pre-roll footage and is captured too late, missing the
    # triggering subject.
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    camera = {
        'id': 'front-yard',
        'name': 'Front Yard',
        'backend': 'rtsp',
        'stream_url': 'rtsp://example/front',
    }
    monkeypatch.setattr(main._state, 'cameras_config', [camera])

    primed: list[str] = []
    monkeypatch.setattr(
        main.recording_service,
        'prime_rtsp_prebuffer',
        lambda **kwargs: primed.append(kwargs.get('camera_id')) or True,
    )

    threads_started: list[str] = []
    real_thread = main.threading.Thread

    def _track_thread(*args, **kwargs):
        name = str(kwargs.get('name') or '')
        if name.startswith('live-detection-'):
            threads_started.append(name)

            class _Noop:
                def start(self):
                    return None

            return _Noop()
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(main.threading, 'Thread', _track_thread)

    # Put the camera into detection backoff.
    with main._state._live_backoff_lock:
        main._state.live_detection_retry_after['front-yard'] = time.time() + 300

    mods.live_monitor.run_live_alert_monitor_once({'background_detection_enabled': True, 'detection_interval_seconds': 0.5})

    assert primed == ['front-yard'], 'prebuffer should be primed despite detection backoff'
    assert threads_started == [], 'detection should remain throttled during backoff'


def test_prebuffer_render_degenerate_detection():
    from app.recordings import RecordingService

    # A near-still clip (1s) rendered for a real window (65s) is degenerate.
    assert RecordingService._clip_is_degenerate(1.0, 65.0) is True
    assert RecordingService._clip_is_degenerate(2.9, 10.0) is True
    assert RecordingService._clip_is_degenerate(10.0, 87.0) is True
    assert RecordingService._clip_is_degenerate(15.0, 48.0) is True
    # A clip close to its requested window is fine.
    assert RecordingService._clip_is_degenerate(63.0, 65.0) is False
    # A legitimately short window (no full pre-roll yet) is not flagged.
    assert RecordingService._clip_is_degenerate(1.0, 5.0) is False
    # Unknown duration (probe unavailable) is never treated as degenerate.
    assert RecordingService._clip_is_degenerate(None, 65.0) is False
    assert RecordingService._clip_is_degenerate(0.0, 65.0) is False


def test_clip_duration_seconds_reads_real_length(tmp_path, monkeypatch):
    import shutil as _shutil
    if not (_shutil.which('ffmpeg') and _shutil.which('ffprobe')):
        import pytest as _pytest
        _pytest.skip('ffmpeg/ffprobe not available')
    _load_app(tmp_path, monkeypatch)
    from app.recordings import RecordingService

    service = RecordingService({'storage': {'data_dir': str(tmp_path / 'rec')}, 'recording': {}})
    clip = tmp_path / 'clip.mp4'
    service._write_ffmpeg_placeholder_clip(clip, 3.0)
    measured = service.clip_duration_seconds(clip)
    assert measured is not None
    assert 2.0 <= measured <= 4.5


def test_clip_duration_seconds_prefers_video_stream_duration(tmp_path, monkeypatch):
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    clip = tmp_path / 'video_short_audio_long.mp4'
    clip.write_bytes(b'not-empty')

    def fake_run(command, *_args, **_kwargs):
        show_entries = command[command.index('-show_entries') + 1]
        if show_entries == 'stream=duration':
            return subprocess.CompletedProcess(command, 0, stdout='3.250000\n', stderr='')
        if show_entries == 'format=duration':
            return subprocess.CompletedProcess(command, 0, stdout='87.000000\n', stderr='')
        raise AssertionError(f'unexpected ffprobe command: {command}')

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffprobe')
    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)

    assert RecordingService.clip_duration_seconds(clip) == pytest.approx(3.25)


def test_prime_ingest_runs_without_pre_event_seconds(tmp_path, monkeypatch):
    # The shared ingest is the single RTSP connection feeding detection + sound +
    # events, so it must start even when pre_event_seconds is 0 (old prebuffer
    # gated on pre > 0 and would never start for detection-only setups).
    from app.recordings import RecordingService

    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}})
    calls = []
    monkeypatch.setattr(RecordingService, '_worker_ffmpeg_available', lambda self, *a, **k: True)
    monkeypatch.setattr(RecordingService, '_ensure_prebuffer_worker', lambda self, *a, **k: calls.append((a, k)))

    assert service.prime_rtsp_prebuffer(stream_url='rtsp://x/s', camera_id='cam', recording_config={'pre_event_seconds': 0}) is True
    assert calls, 'ingest worker should be ensured even with pre_event_seconds=0'


def test_ingest_frame_and_audio_accessors(tmp_path):
    from app.recordings import RecordingService

    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}})
    key = RecordingService._camera_key('Front Yard')

    frames_dir = service.frames_dir / key
    frames_dir.mkdir(parents=True)
    (frames_dir / 'latest.jpg').write_bytes(b'jpeg-bytes')
    got = service.latest_frame_jpeg('Front Yard')
    assert got is not None and got[0] == b'jpeg-bytes'
    # A stale frame is rejected so detection never runs on an old image.
    old = time.time() - 60
    os.utime(frames_dir / 'latest.jpg', (old, old))
    assert service.latest_frame_jpeg('Front Yard', max_age_seconds=10) is None

    audio_dir = service.audio_dir / key
    audio_dir.mkdir(parents=True)
    now = time.time()
    for index in range(3):
        seg = audio_dir / f'aud-{index:02d}.wav'
        seg.write_bytes(b'x')
        os.utime(seg, (now - 3 + index, now - 3 + index))
    after = service.audio_segments_after('Front Yard', now - 2.5)
    assert [path.name for path, _mtime in after] == ['aud-01.wav', 'aud-02.wav']


def test_read_ingest_frame_decodes_latest_jpeg(tmp_path, monkeypatch):
    cv2 = pytest.importorskip('cv2')
    np = pytest.importorskip('numpy')
    _load_app(tmp_path, monkeypatch)
    import app.main as main
    mods = _m()

    key = main.RecordingService._camera_key('cam1')
    frames_dir = main.recording_service.frames_dir / key
    frames_dir.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode('.jpg', np.zeros((48, 64, 3), dtype=np.uint8))
    assert ok
    (frames_dir / 'latest.jpg').write_bytes(encoded.tobytes())

    sample = mods.camera_instance.read_ingest_frame('cam1')
    assert sample is not None
    image, frame = sample
    assert frame['width'] == 64 and frame['height'] == 48 and frame['timestamp'] > 0
    assert mods.camera_instance.read_ingest_frame('no-such-camera') is None


def test_shared_ingest_worker_command_fans_out_three_outputs(tmp_path, monkeypatch):
    # One ffmpeg invocation, one input (-i), three outputs: video .ts segments,
    # a latest.jpg frame, and PCM-WAV audio segments. (The 3-output ffmpeg itself
    # is validated manually; here we assert the command the worker builds.)
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}})
    stop = threading.Event()
    captured: dict[str, list[str]] = {}

    class _FakeProc:
        def poll(self):
            return 0

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def fake_popen(cmd, **_kwargs):
        captured['cmd'] = cmd
        stop.set()  # one iteration only
        return _FakeProc()

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(recordings_module.subprocess, 'Popen', fake_popen)

    service._run_prebuffer_worker('cam', 'rtsp://example/stream', {
        'stop_event': stop,
        'stream_url': 'rtsp://example/stream',
        'buffer_seconds': 20,
    })

    cmd = captured['cmd']
    assert cmd.count('-i') == 1, 'a single input == a single RTSP connection'
    assert any(str(a).endswith('latest.jpg') for a in cmd), 'frame output for detection/snapshots'
    assert any('segment-' in str(a) and str(a).endswith('.mp4') for a in cmd), 'video segments for events'
    assert any('aud-' in str(a) and str(a).endswith('.wav') for a in cmd), 'audio segments for sound'
    video_output_index = next(index for index, value in enumerate(cmd) if 'segment-' in str(value) and str(value).endswith('.mp4'))
    assert '-an' in cmd[:video_output_index]
    assert cmd[cmd.index('-segment_format') + 1] == 'mp4'
    assert cmd[cmd.index('-segment_format_options') + 1] == 'movflags=+frag_keyframe+empty_moov+default_base_moof'
    segment_times = [cmd[index + 1] for index, value in enumerate(cmd) if value == '-segment_time']
    assert segment_times[0] == str(RecordingService.PREBUFFER_SEGMENT_SECONDS)
    assert segment_times[1] == '1'
    assert 'image2' in cmd and cmd.count('segment') >= 2  # ts segment + wav segment muxers


def test_prebuffer_window_change_updates_worker_without_restart(tmp_path, monkeypatch):
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}})
    started: list[threading.Event] = []

    class _FakeThread:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.started = False

        def start(self):
            self.started = True
            target = self.kwargs.get('target')
            args = self.kwargs.get('args') or ()
            started.append(args[2]['stop_event'])

        def is_alive(self):
            return self.started

    monkeypatch.setattr(recordings_module.threading, 'Thread', _FakeThread)

    service._ensure_prebuffer_worker('cam', 'rtsp://example/stream', 120, camera_id='cam')
    first_worker = service._prebuffer_workers['cam']
    service._ensure_prebuffer_worker('cam', 'rtsp://example/stream', 75, camera_id='cam')

    assert len(started) == 1
    assert service._prebuffer_workers['cam'] is first_worker
    assert first_worker['buffer_seconds'] == 75
    assert not started[0].is_set()


def test_prebuffer_first_segment_uses_full_segment_length(tmp_path):
    # The first selected segment (no previous mtime to anchor to) must fall back
    # to the real segment length, not a hardcoded 1s. With 4s segments the old
    # 1s fallback put content_start ~3s late and mis-stated the concat duration.
    from app.recordings import RecordingService

    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}})
    key = 'cam'
    cam_dir = service.prebuffer_dir / key
    cam_dir.mkdir(parents=True)
    now = time.time()
    # Two contiguous 4s segments ending at now-4 and now.
    for index, end in enumerate((now - 4, now)):
        seg = cam_dir / f'segment-{index:02d}.mp4'
        seg.write_bytes(b'x')
        os.utime(seg, (end, end))

    segments, content_start = service._collect_prebuffer_segments(key, now - 7, now)
    assert len(segments) == 2
    # First segment has no predecessor, so its content start = end - segment length.
    assert content_start == pytest.approx((now - 4) - service.PREBUFFER_SEGMENT_SECONDS, abs=0.2)

    durations = service._prebuffer_segment_durations(key, segments)
    assert durations[segments[0]] == pytest.approx(service.PREBUFFER_SEGMENT_SECONDS, abs=0.2)


def test_collect_prebuffer_segments_selects_by_content_overlap(tmp_path):
    """A prebuffer segment's mtime marks when its content ENDS; selection must
    keep only segments whose footage overlaps the capture window and report
    where the first one's content starts, so the rendered clip's time 0 lines
    up with the detection track instead of leading it by a few seconds."""
    from app.recordings import RecordingService

    service = RecordingService({
        'storage': {'recordings_dir': str(tmp_path / 'recordings')},
        'recording': {'format': 'mp4'},
    })
    camera_dir = service.prebuffer_dir / 'camera-1'
    camera_dir.mkdir(parents=True, exist_ok=True)

    now = time.time()
    segments = []
    for offset in range(7):  # contiguous 1s segments ending at now-6 .. now
        end_ts = now - 6 + offset
        segment = camera_dir / f'segment-{offset:02d}.mp4'
        segment.write_bytes(b'ts')
        os.utime(segment, (end_ts, end_ts))
        segments.append(segment)

    selected, content_start = service._collect_prebuffer_segments('camera-1', now - 4.0, now - 1.0)

    # Footage entirely before the window (ends at or before start_ts) is out,
    # and a segment starting exactly at end_ts contributes nothing either.
    assert selected == segments[3:6]
    # The first selected segment's content starts where the previous one ended.
    assert content_start == pytest.approx(now - 4.0, abs=0.05)

    # No overlap at all must not fall back to tail segments: those may be
    # post-event footage and can produce clips that miss the subject entirely.
    fallback, fallback_start = service._collect_prebuffer_segments('camera-1', now + 100, now + 103)
    assert fallback == []
    assert fallback_start is None


def test_rec_prebuffer_segments_report_content_start_not_mtime(tmp_path):
    """The high-res recording prebuffer collector (``<key>-rec``) must anchor
    ``content_start_ts`` at the first selected segment's content START, exactly
    like the primary collector - not at the segment's mtime (its content END).

    Regression: ``_collect_prebuffer_segments_from_dir`` used the minimum mtime
    as ``content_start_ts``, which is up to a full segment (~4s) late. The clip
    render window, the muxed audio delay and the baked detection track all
    followed that late anchor, so dual-stream recordings played with sound
    lagging the video by roughly a segment."""
    from app.recordings import RecordingService

    service = RecordingService({
        'storage': {'recordings_dir': str(tmp_path / 'recordings')},
        'recording': {'format': 'mp4'},
    })
    rec_dir = service.prebuffer_dir / 'camera-1-rec'
    rec_dir.mkdir(parents=True, exist_ok=True)

    now = time.time()
    segments = []
    for offset in range(7):  # contiguous 1s segments ending at now-6 .. now
        end_ts = now - 6 + offset
        segment = rec_dir / f'segment-{offset:02d}.mp4'
        segment.write_bytes(b'ts')
        os.utime(segment, (end_ts, end_ts))
        segments.append(segment)

    selected, content_start = service._collect_rec_prebuffer_segments('camera-1', now - 4.0, now - 1.0)

    # Same content-overlap selection as the primary collector...
    assert selected == segments[3:6]
    # ...and the content START of the first selected segment (previous segment's
    # end = now-4), NOT its mtime (now-3, its content END).
    assert content_start == pytest.approx(now - 4.0, abs=0.05)


def test_write_rtsp_clip_with_prebuffer_returns_actual_content_window(tmp_path, monkeypatch):
    """The rendered clip starts at the first selected segment's content start
    (keyframe-aligned, so usually before triggered_at - pre_seconds) and runs
    to the capture deadline. The returned window must describe that media so
    the caller can align stored timing and the detection track with it."""
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService({
        'storage': {'recordings_dir': str(tmp_path / 'recordings')},
        'recording': {'format': 'mp4'},
    })
    camera_dir = service.prebuffer_dir / 'cam'
    camera_dir.mkdir(parents=True, exist_ok=True)

    now = time.time()
    for offset in range(17):  # contiguous 1s segments ending at now-16.5 .. now-0.5
        end_ts = now - 16.5 + offset
        segment = camera_dir / f'segment-{offset:02d}.mp4'
        segment.write_bytes(b'ts')
        os.utime(segment, (end_ts, end_ts))
    audio_dir = service.audio_dir / 'cam'
    audio_dir.mkdir(parents=True, exist_ok=True)
    for offset in range(17):
        end_ts = now - 16.5 + offset
        segment = audio_dir / f'aud-{offset:02d}.wav'
        segment.write_bytes(b'wav')
        os.utime(segment, (end_ts, end_ts))

    commands = []

    def fake_run(command, *_args, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b'clip-bytes')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(RecordingService, '_ensure_prebuffer_worker', lambda self, *a, **k: None)
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)
    monkeypatch.setattr(RecordingService, 'clip_has_video_stream', staticmethod(lambda _path: True))
    monkeypatch.setattr(RecordingService, 'clip_duration_seconds', staticmethod(lambda _path: 15.5))
    # Placeholder WAV sidecars stand in for real audio here; skip the readability
    # probe (covered by tests/test_mux_audio_readable.py) so the audio mux still
    # runs as a second ffmpeg command.
    monkeypatch.setattr(RecordingService, '_readable_audio_segments', lambda self, segments: segments)

    file_path = tmp_path / 'recordings' / 'event_window.mp4'
    triggered_at = datetime.fromtimestamp(now - 10, tz=timezone.utc)
    content_start, content_seconds = service.write_rtsp_clip_with_prebuffer(
        stream_url='rtsp://example/stream',
        camera_id='cam',
        file_path=file_path,
        triggered_at=triggered_at,
        pre_seconds=5,
        post_seconds=10,
        max_duration_seconds=15.0,
    )

    assert file_path.exists()
    # Window start now-15 selects segments from the one ending now-14.5, whose
    # content starts at the previous segment's end: now-15.5.
    assert content_start == pytest.approx(now - 15.5, abs=0.1)
    assert content_seconds == pytest.approx(15.5, abs=0.1)
    assert len(commands) == 2
    command = commands[0]
    assert command[command.index('-map') + 1] == '0:v:0'
    assert '-an' in command
    assert '0:a:0?' not in command
    assert command[command.index('-c') + 1] == 'copy'
    assert 'libx264' not in command
    assert 'aac' not in command
    render_seconds = float(commands[0][commands[0].index('-t') + 1])
    assert render_seconds == pytest.approx(content_seconds, abs=0.01)
    mux_command = commands[1]
    assert mux_command[mux_command.index('-map') + 1] == '0:v:0'
    assert mux_command[mux_command.index('-map') + 3] == '[aout]'
    assert mux_command.count('-i') == 16, 'video plus one input for each selected WAV segment'
    mux_filter = mux_command[mux_command.index('-filter_complex') + 1]
    assert 'amix=inputs=15' in mux_filter
    assert 'aresample=async=1' in mux_filter
    assert 'apad' in mux_filter
    assert mux_command[mux_command.index('-c:v') + 1] == 'copy'
    assert mux_command[mux_command.index('-c:a') + 1] == 'aac'


def test_prebuffer_and_continuous_workers_do_not_start_without_ffmpeg(tmp_path, monkeypatch):
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService({
        'storage': {'recordings_dir': str(tmp_path / 'recordings')},
        'recording': {'pre_event_seconds': 5, 'max_clip_seconds': 60, 'continuous': True},
    })

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: None)

    assert service.prime_rtsp_prebuffer(
        stream_url='rtsp://example/stream',
        camera_id='cam',
        recording_config={'pre_event_seconds': 5, 'max_clip_seconds': 60},
    ) is False
    assert service.start_continuous_chunk_recording(
        stream_url='rtsp://example/stream',
        camera_id='cam',
        recording_config={'continuous': True, 'chunk_duration_seconds': 60},
    ) is False
    assert service._prebuffer_workers == {}
    assert service._continuous_workers == {}


def test_degenerate_prebuffer_render_keeps_partial_clip_instead_of_late_live_capture(tmp_path, monkeypatch):
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService({
        'storage': {'recordings_dir': str(tmp_path / 'recordings')},
        'recording': {'format': 'mp4'},
    })
    camera_dir = service.prebuffer_dir / 'cam'
    camera_dir.mkdir(parents=True, exist_ok=True)

    now = time.time()
    for offset in range(12):
        end_ts = now - 11.5 + offset
        segment = camera_dir / f'segment-{offset:02d}.mp4'
        segment.write_bytes(b'ts')
        os.utime(segment, (end_ts, end_ts))

    def fake_run(command, *_args, **_kwargs):
        Path(command[-1]).write_bytes(b'partial-prebuffer')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    live_capture_called = False

    def fake_live_capture(*_args, **_kwargs):
        nonlocal live_capture_called
        live_capture_called = True
        return now, 10.0

    monkeypatch.setattr(RecordingService, '_ensure_prebuffer_worker', lambda self, *a, **k: None)
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)
    monkeypatch.setattr(RecordingService, 'clip_has_video_stream', staticmethod(lambda _path: True))
    monkeypatch.setattr(RecordingService, 'clip_duration_seconds', staticmethod(lambda _path: 1.0))
    monkeypatch.setattr(RecordingService, '_live_capture', fake_live_capture)

    file_path = tmp_path / 'recordings' / 'event_window.mp4'
    content_start, content_seconds = service.write_rtsp_clip_with_prebuffer(
        stream_url='rtsp://example/stream',
        camera_id='cam',
        file_path=file_path,
        triggered_at=datetime.fromtimestamp(now - 5, tz=timezone.utc),
        pre_seconds=5,
        post_seconds=5,
        max_duration_seconds=10.0,
    )

    assert file_path.read_bytes() == b'partial-prebuffer'
    assert live_capture_called is False
    assert content_start == pytest.approx(now - 10.5, abs=0.1)
    assert content_seconds == pytest.approx(1.0)


def test_prebuffer_concat_list_uses_ffmpeg_safe_absolute_paths(tmp_path, monkeypatch):
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService({
        'storage': {'recordings_dir': str(tmp_path / 'recordings')},
        'recording': {'format': 'mp4'},
    })
    camera_dir = service.prebuffer_dir / 'cam'
    camera_dir.mkdir(parents=True, exist_ok=True)

    now = time.time()
    for offset in range(4):
        end_ts = now - 3 + offset
        segment = camera_dir / f"segment-{offset:02d}.mp4"
        segment.write_bytes(b'ts')
        os.utime(segment, (end_ts, end_ts))

    concat_text = ''

    def fake_run(command, *_args, **_kwargs):
        nonlocal concat_text
        list_path = Path(command[command.index('-i') + 1])
        concat_text = list_path.read_text(encoding='utf-8')
        Path(command[-1]).write_bytes(b'clip-bytes')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(RecordingService, '_ensure_prebuffer_worker', lambda self, *a, **k: None)
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)
    monkeypatch.setattr(RecordingService, 'clip_has_video_stream', staticmethod(lambda _path: True))
    monkeypatch.setattr(RecordingService, 'clip_duration_seconds', staticmethod(lambda _path: 3.0))

    service.write_rtsp_clip_with_prebuffer(
        stream_url='rtsp://example/stream',
        camera_id='cam',
        file_path=tmp_path / 'recordings' / 'event_window.mp4',
        triggered_at=datetime.fromtimestamp(now - 1, tz=timezone.utc),
        pre_seconds=2,
        post_seconds=1,
        max_duration_seconds=3.0,
    )

    assert concat_text
    file_lines = [line for line in concat_text.splitlines() if line.startswith("file '")]
    duration_lines = [line for line in concat_text.splitlines() if line.startswith('duration ')]
    assert file_lines
    assert len(duration_lines) == len(file_lines)
    assert all(float(line.split()[1]) > 0 for line in duration_lines)
    for line in file_lines:
        assert '\\' not in line
        listed_path = line[len("file '"):-1]
        assert Path(listed_path).is_absolute()
