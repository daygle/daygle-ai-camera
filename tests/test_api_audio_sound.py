"""API integration tests: sound detector ingest plus audio timeline/retention and prebuffer audio mux.

Split out of the former monolithic tests/test_api.py; the shared harness
(LocalClient, _load_app, _server, _login, _setup_admin, …) lives in
tests/support.py.
"""
from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports


def test_sound_detector_diagnostics_and_reason():
    import app.main as main
    from app.sound_detector import SoundDetector
    mods = _m()

    rules = [
        {'class': 'car_alarm', 'name': 'Car Alarm', 'enabled': True, 'confidence_threshold': 0.35, 'cooldown_seconds': 60},
        {'class': 'dog_bark', 'name': 'Dog Bark', 'enabled': True, 'confidence_threshold': 0.40, 'cooldown_seconds': 20},
    ]
    det = SoundDetector(on_detect=lambda *a, **k: None, rules=rules, source='ingest')

    # Nothing heard yet -> no diagnostics confidence, no reason.
    assert mods.sound_monitor._sound_status_reason(det.diagnostics()) is None

    # A class heard below its threshold -> 'below_threshold'.
    with det._status_lock:
        det._last_confidences = {'car_alarm': 0.28, 'dog_bark': 0.05}
    diag = det.diagnostics()
    assert diag[0]['class'] == 'car_alarm'  # sorted by confidence, highest first
    reason = mods.sound_monitor._sound_status_reason(diag)
    assert reason['code'] == 'below_threshold'
    assert reason['class'] == 'car_alarm'
    assert reason['threshold'] == 0.35

    # A class at/above threshold that just fired -> suppressed by 'cooldown'.
    with det._status_lock:
        det._last_confidences = {'car_alarm': 0.50}
        det._last_triggered = {'car_alarm': time.time()}
    cooldown_reason = mods.sound_monitor._sound_status_reason(det.diagnostics())
    assert cooldown_reason['code'] == 'cooldown'
    assert cooldown_reason['cooldown_remaining'] > 0

    # Same class above threshold once its cooldown has elapsed -> 'detected'.
    with det._status_lock:
        det._last_triggered = {'car_alarm': time.time() - 120}
    assert mods.sound_monitor._sound_status_reason(det.diagnostics())['code'] == 'detected'


def test_sound_detector_ingest_consumes_audio_segments(tmp_path):
    import shutil as _shutil
    if not _shutil.which('ffmpeg'):
        pytest.skip('ffmpeg not available')
    from app.sound_detector import SoundDetector

    wav = tmp_path / 'chunk.wav'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000',
        '-t', '1', '-ac', '1', '-acodec', 'pcm_s16le', str(wav),
    ], check=True)

    captured = []
    # The ingest writes fresh segments continuously; simulate one arriving after
    # the detector's startup cutoff by stamping it with the current time once.
    state = {'served': False}

    def provider(after, _w=wav, _state=state):
        if _state['served']:
            return []
        _state['served'] = True
        return [(_w, time.time())]

    det = SoundDetector(on_detect=lambda *a, **k: None, rules=[], source='ingest', audio_segment_provider=provider)
    det._handle_chunk = lambda audio: captured.append(audio)
    det.start()
    try:
        deadline = time.time() + 4
        while not captured and time.time() < deadline:
            time.sleep(0.1)
    finally:
        det.stop()

    assert captured, 'ingest sound source should classify the audio segment'
    assert captured[0].shape[0] == 16000  # 1s of 16 kHz mono


def test_sound_detector_ingest_overlaps_windows_across_segments(tmp_path):
    import shutil as _shutil
    if not _shutil.which('ffmpeg'):
        pytest.skip('ffmpeg not available')
    from app.sound_detector import SoundDetector

    wav = tmp_path / 'chunk.wav'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000',
        '-t', '1', '-ac', '1', '-acodec', 'pcm_s16le', str(wav),
    ], check=True)

    captured = []
    state = {'served': False}

    # Three consecutive 1s segments. With 50% overlap they must yield MORE than
    # three classification windows (~5), proving sounds crossing a segment
    # boundary are analysed in a window that spans it.
    def provider(after, _w=wav, _state=state):
        if _state['served']:
            return []
        _state['served'] = True
        base = time.time()
        return [(_w, base + 0.1), (_w, base + 0.2), (_w, base + 0.3)]

    det = SoundDetector(on_detect=lambda *a, **k: None, rules=[], source='ingest', audio_segment_provider=provider)
    det._handle_chunk = lambda audio: captured.append(audio.shape[0])
    det.start()
    try:
        deadline = time.time() + 4
        while len(captured) < 4 and time.time() < deadline:
            time.sleep(0.1)
    finally:
        det.stop()

    assert len(captured) >= 4, f'expected overlapping windows (>3) from 3 segments, got {len(captured)}'
    assert all(n == 16000 for n in captured), 'each classified window is a full 1s of 16 kHz mono'


def test_audio_timeline_preserves_missing_segment_gap(tmp_path):
    from app.recordings import RecordingService

    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}})
    audio_dir = service.audio_dir / 'cam'
    audio_dir.mkdir(parents=True)
    now = time.time()
    # The second WAV finishes two seconds after the first: one nominal 1s WAV
    # is missing. The derived timeline must retain that missing second as
    # silence instead of collapsing the audio against the video.
    for name, end_ts in (('aud-00.wav', now - 2), ('aud-02.wav', now)):
        segment = audio_dir / name
        segment.write_bytes(b'wav')
        os.utime(segment, (end_ts, end_ts))

    timeline = service._segment_timeline(audio_dir, 'aud-*.wav', 1.0)
    assert len(timeline) == 2
    assert timeline[1][1] == pytest.approx(now - 1, abs=0.05)
    assert timeline[1][1] > timeline[0][2]


def test_audio_retention_follows_prebuffer_window(tmp_path):
    # Audio sidecar segments must be retained for the full prebuffer window, not
    # a fixed 20s - otherwise long event clips get audio shorter than the video
    # and the player's buffered bar stops short (buffered = where all tracks exist).
    from app.recordings import RecordingService

    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}})
    audio_dir = service.audio_dir / 'cam'
    audio_dir.mkdir(parents=True)
    now = time.time()
    old = audio_dir / 'aud-old.wav'
    old.write_bytes(b'x')
    os.utime(old, (now - 60, now - 60))  # 60s old

    # Default floor (20s) prunes the 60s-old segment.
    service._prune_audio_segments(audio_dir)
    assert not old.exists()

    # With a large keep window (long max_clip), the same-age segment is retained.
    old.write_bytes(b'x')
    os.utime(old, (now - 60, now - 60))
    service._prune_audio_segments(audio_dir, keep_seconds=200)
    assert old.exists()


def test_mux_prebuffer_audio_pads_audio_to_video(tmp_path, monkeypatch):
    # The audio mux must align the sidecar audio clock with video, resample
    # small clock drift, and pad audio to the video length (-shortest) so the
    # player's buffered bar reaches the clip end.
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}})
    audio_dir = service.audio_dir / 'cam'
    audio_dir.mkdir(parents=True)
    now = time.time()
    seg = audio_dir / 'aud-000.wav'
    seg.write_bytes(b'x')
    os.utime(seg, (now, now))

    captured = {}

    def fake_run(command, *_args, **_kwargs):
        captured['cmd'] = command
        Path(command[-1]).write_bytes(b'muxed')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)
    monkeypatch.setattr(RecordingService, 'clip_has_video_stream', staticmethod(lambda _p: True))
    # This test exercises the mux command build from a placeholder WAV; skip the
    # readability probe (covered by tests/test_mux_audio_readable.py) so the tiny
    # stand-in file isn't discarded before the command is assembled.
    monkeypatch.setattr(RecordingService, '_readable_audio_segments', lambda self, segments: segments)

    video = tmp_path / 'rec' / 'event.mp4'
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b'video')
    service._mux_prebuffer_audio('cam', video, now - 2, 2.0)

    cmd = captured.get('cmd', [])
    assert '-filter_complex' in cmd
    filter_graph = cmd[cmd.index('-filter_complex') + 1]
    assert 'adelay=1000:all=1' in filter_graph, 'audio must preserve the one-second video/audio wall-clock offset'
    assert 'amix=inputs=1' in filter_graph
    assert 'aresample=async=1' in filter_graph, 'audio clock drift must be corrected'
    assert 'apad' in filter_graph, 'audio must be padded to video length'
    assert cmd.count('-i') == 2, 'video plus one sidecar WAV input'
    assert '[aout]' in cmd
    assert '-shortest' in cmd


def test_mux_prebuffer_audio_trims_audio_that_begins_before_video(tmp_path, monkeypatch):
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}})
    audio_dir = service.audio_dir / 'cam'
    audio_dir.mkdir(parents=True)
    now = time.time()
    segment = audio_dir / 'aud-000.wav'
    segment.write_bytes(b'x')
    os.utime(segment, (now, now))

    captured = {}

    def fake_run(command, *_args, **_kwargs):
        captured['cmd'] = command
        Path(command[-1]).write_bytes(b'muxed')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)
    monkeypatch.setattr(RecordingService, 'clip_has_video_stream', staticmethod(lambda _p: True))
    # Skip the readability probe (covered by tests/test_mux_audio_readable.py) so
    # the placeholder WAV survives to the command-build step under test.
    monkeypatch.setattr(RecordingService, '_readable_audio_segments', lambda self, segments: segments)

    video = tmp_path / 'rec' / 'event.mp4'
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b'video')
    # The WAV timeline starts at now - 1s; video starts at now - 0.5s.
    service._mux_prebuffer_audio('cam', video, now - 0.5, 1.5)

    cmd = captured['cmd']
    filter_graph = cmd[cmd.index('-filter_complex') + 1]
    assert 'atrim=start=' in filter_graph
    assert 'adelay=' not in filter_graph
    assert 'amix=inputs=1' in filter_graph
    assert 'aresample=async=1' in filter_graph
    assert '-t' in cmd and cmd[cmd.index('-t') + 1] == '1.500'
