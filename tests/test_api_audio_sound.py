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
    headroom = service.AUDIO_MUX_FINALIZATION_HEADROOM_SECONDS
    old = audio_dir / 'aud-old.wav'
    old.write_bytes(b'x')
    # Older than even the finalization headroom -> pruned with the default window.
    os.utime(old, (now - headroom - 60, now - headroom - 60))
    service._prune_audio_segments(audio_dir)
    assert not old.exists()

    # With a large keep window (long max_clip), the same-age segment is retained.
    old.write_bytes(b'x')
    os.utime(old, (now - headroom - 60, now - headroom - 60))
    service._prune_audio_segments(audio_dir, keep_seconds=200)
    assert old.exists()


def test_audio_retention_includes_finalization_headroom(tmp_path):
    # The head of a clip's audio must survive while the clip is still being
    # rendered/finalized: retention is the prebuffer window PLUS a finalization
    # margin, so the mux is not racing the pruner (the "Audio sidecars
    # disappeared before mux" partial-audio loss).
    from app.recordings import RecordingService

    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}})
    audio_dir = service.audio_dir / 'cam'
    audio_dir.mkdir(parents=True)
    now = time.time()
    keep = 60  # e.g. pre + max_clip window
    headroom = service.AUDIO_MUX_FINALIZATION_HEADROOM_SECONDS

    # A segment older than the raw window but within window + headroom: this is
    # the clip head that the render latency would otherwise let the pruner delete.
    within = audio_dir / 'aud-within.wav'
    within.write_bytes(b'x')
    os.utime(within, (now - (keep + 30), now - (keep + 30)))

    # A segment older than window + headroom: genuinely stale, must be pruned.
    beyond = audio_dir / 'aud-beyond.wav'
    beyond.write_bytes(b'x')
    os.utime(beyond, (now - (keep + headroom + 30), now - (keep + headroom + 30)))

    service._prune_audio_segments(audio_dir, keep_seconds=keep)

    assert within.exists()
    assert not beyond.exists()


def _mono_wav(path, seconds, value=1000, rate=16000):
    """Write a valid 16 kHz mono ``pcm_s16le`` WAV of constant amplitude."""
    import struct
    import wave

    frames = int(round(seconds * rate))
    with wave.open(str(path), 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(struct.pack('<%dh' % frames, *([value] * frames)))
    return path


def _read_wav_frames(path):
    """Return ``(rate, [samples])`` for a mono ``pcm_s16le`` WAV."""
    import struct
    import wave

    with wave.open(str(path), 'rb') as reader:
        rate = reader.getframerate()
        frames = struct.unpack('<%dh' % reader.getnframes(), reader.readframes(reader.getnframes()))
    return rate, frames


def _capture_assembled_audio(captured):
    """A ``subprocess.run`` stand-in that snapshots the assembled audio track
    (the mux's second ``-i`` input) before the staging dir is cleaned up."""
    def fake_run(command, *_args, **_kwargs):
        captured['cmd'] = command
        input_indices = [index for index, value in enumerate(command) if value == '-i']
        captured['rate'], captured['frames'] = _read_wav_frames(command[input_indices[1] + 1])
        Path(command[-1]).write_bytes(b'muxed')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    return fake_run


def test_mux_prebuffer_audio_pads_audio_to_video(tmp_path, monkeypatch):
    # The audio mux assembles the sidecars onto a single track aligned to video
    # time: a one-second wall-clock offset becomes a leading second of silence,
    # and the track is padded with silence to the full video length so the
    # player's buffered bar reaches the clip end.
    import app.recordings as recordings_module
    RecordingService = recordings_module.RecordingService

    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}})
    audio_dir = service.audio_dir / 'cam'
    audio_dir.mkdir(parents=True)
    now = time.time()
    seg = _mono_wav(audio_dir / 'aud-000.wav', 1.0)

    captured = {}

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(recordings_module.subprocess, 'run', _capture_assembled_audio(captured))
    monkeypatch.setattr(RecordingService, 'clip_has_video_stream', staticmethod(lambda _p: True))
    monkeypatch.setattr(RecordingService, '_readable_audio_segments', lambda self, segments: segments)
    # Deterministic timing: one 1s segment sitting 1s into a 2s clip window.
    monkeypatch.setattr(RecordingService, '_segment_timeline', lambda self, *a, **k: [(seg, now - 1, now)])

    video = tmp_path / 'rec' / 'event.mp4'
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b'video')
    assert service._mux_prebuffer_audio('cam', video, now - 2, 2.0) is True

    cmd = captured['cmd']
    assert cmd.count('-i') == 2, 'exactly video plus one assembled audio track -- no per-second inputs'
    assert '-filter_complex' not in cmd, 'the fragile per-second filtergraph must be gone'
    assert '-shortest' in cmd
    assert cmd[cmd.index('-t') + 1] == '2.000'
    rate, frames = captured['rate'], captured['frames']
    assert len(frames) == 2 * rate, 'assembled track spans the full 2s video length'
    assert set(frames[:rate]) == {0}, 'the 1s wall-clock offset becomes leading silence'
    assert any(sample != 0 for sample in frames[rate:2 * rate]), 'segment audio lands after the delay'


def test_mux_prebuffer_audio_trims_audio_that_begins_before_video(tmp_path, monkeypatch):
    import app.recordings as recordings_module
    RecordingService = recordings_module.RecordingService

    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}})
    audio_dir = service.audio_dir / 'cam'
    audio_dir.mkdir(parents=True)
    now = time.time()
    segment = _mono_wav(audio_dir / 'aud-000.wav', 1.0)

    captured = {}

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(recordings_module.subprocess, 'run', _capture_assembled_audio(captured))
    monkeypatch.setattr(RecordingService, 'clip_has_video_stream', staticmethod(lambda _p: True))
    monkeypatch.setattr(RecordingService, '_readable_audio_segments', lambda self, segments: segments)
    # The WAV spans [now-1, now]; the clip starts at now-0.5, so the segment's
    # first 0.5s is pre-roll overlap that must be trimmed to align to video t=0.
    monkeypatch.setattr(RecordingService, '_segment_timeline', lambda self, *a, **k: [(segment, now - 1, now)])

    video = tmp_path / 'rec' / 'event.mp4'
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b'video')
    assert service._mux_prebuffer_audio('cam', video, now - 0.5, 1.5) is True

    cmd = captured['cmd']
    assert '-filter_complex' not in cmd
    assert cmd[cmd.index('-t') + 1] == '1.500'
    rate, frames = captured['rate'], captured['frames']
    assert len(frames) == int(round(1.5 * rate)), 'assembled track spans the full clip length'
    # Leading 0.5s overlap trimmed: the remaining 0.5s of content starts at t=0...
    assert any(sample != 0 for sample in frames[:rate // 2]), 'trimmed content begins at video t=0'
    # ...and everything from t=0.5s on (past the segment) is silence padding.
    assert set(frames[rate // 2:]) == {0}, 'no audio beyond the trimmed segment'
