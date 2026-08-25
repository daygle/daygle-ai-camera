"""Regression tests for dropping unreadable WAV sidecar segments from the
event-clip audio mux.

The per-camera ingest writes 1-second ``aud-<ts>.wav`` sidecar segments
continuously. When an event clip is finalized, ``_mux_prebuffer_audio`` gathers
every WAV overlapping the clip window and feeds each as its own ffmpeg input.
The newest segment is frequently still being written (header/data sizes not yet
patched), so ffmpeg rejects it with "Invalid data found when processing input"
and -- because one bad input aborts the whole mux -- the entire clip is saved
silent. ``_readable_audio_segments`` filters those out so a single in-progress
or corrupt segment costs at most a ~1s gap instead of all audio.
"""

from __future__ import annotations

import errno
import logging
import os
import subprocess
import sys
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.recordings as recordings_module  # noqa: E402
RecordingService = recordings_module.RecordingService


def _service(tmp_path: Path) -> RecordingService:
    return RecordingService(
        {'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}}
    )


def _wav(path: Path, size: int) -> Path:
    """Create a placeholder WAV file of ``size`` bytes."""
    path.write_bytes(b'\x00' * size)
    return path


def _real_wav(path: Path, seconds: float = 1.0, rate: int = 16000, value: int = 1000) -> Path:
    """Write a valid 16 kHz mono ``pcm_s16le`` WAV matching the ingest format.

    The mux now assembles the sidecars into one PCM track in Python (via the
    ``wave`` module) before invoking ffmpeg, so a test that needs the mux to
    actually reach ffmpeg must supply a parseable WAV, not a byte placeholder.
    """
    import struct
    import wave

    frames = int(round(seconds * rate))
    with wave.open(str(path), 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(struct.pack('<%dh' % frames, *([value] * frames)))
    return path


def _item(path: Path, start: float) -> tuple[Path, float, float]:
    return (path, start, start + 1.0)


def test_readable_filter_drops_unprobeable_and_keeps_good(tmp_path, monkeypatch):
    """A segment ffprobe cannot open (the in-progress newest WAV) is dropped
    while the valid segments in the same window survive."""
    service = _service(tmp_path)
    good1 = _wav(tmp_path / 'aud-1.wav', 4096)
    good2 = _wav(tmp_path / 'aud-2.wav', 4096)
    bad = _wav(tmp_path / 'aud-3.wav', 4096)  # non-empty but unreadable (mid-write)

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffprobe')

    def fake_run(command, *args, **kwargs):
        target = command[-1]
        ok = target != str(bad)
        return types.SimpleNamespace(
            returncode=0 if ok else 1,
            stdout='pcm_s16le\n' if ok else '',
            stderr='' if ok else 'Invalid data found when processing input',
        )

    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)

    result = service._readable_audio_segments([_item(good1, 0.0), _item(good2, 1.0), _item(bad, 2.0)])

    assert [item[0] for item in result] == [good1, good2]


def test_readable_filter_skips_header_only_without_probing(tmp_path, monkeypatch):
    """A 44-byte (header-only) or empty file is skipped before ffprobe is ever
    invoked -- that is a segment ffmpeg has created but not yet written."""
    service = _service(tmp_path)
    header_only = _wav(tmp_path / 'aud-1.wav', 44)
    empty = _wav(tmp_path / 'aud-2.wav', 0)
    good = _wav(tmp_path / 'aud-3.wav', 4096)

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffprobe')
    probed: list[str] = []

    def fake_run(command, *args, **kwargs):
        probed.append(command[-1])
        return types.SimpleNamespace(returncode=0, stdout='pcm_s16le\n', stderr='')

    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)

    result = service._readable_audio_segments([_item(header_only, 0.0), _item(empty, 1.0), _item(good, 2.0)])

    assert [item[0] for item in result] == [good]
    # The header-only and empty files must not reach ffprobe.
    assert probed == [str(good)]


def test_readable_filter_without_ffprobe_falls_back_to_size(tmp_path, monkeypatch):
    """When ffprobe is unavailable, non-empty files are kept and empty/header-only
    files are still dropped, so the mux never regresses to feeding a 0-byte WAV."""
    service = _service(tmp_path)
    good = _wav(tmp_path / 'aud-1.wav', 4096)
    empty = _wav(tmp_path / 'aud-2.wav', 0)

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: None)

    result = service._readable_audio_segments([_item(good, 0.0), _item(empty, 1.0)])

    assert [item[0] for item in result] == [good]


def test_stage_audio_segments_skips_sidecars_that_disappear(tmp_path, caplog):
    """A WAV removed after selection must not make the whole mux fail.

    Missing sidecars are summarized in a single INFO line rather than one
    line per missing second, so a fully-pruned audio window (e.g. after an
    ingest stall) cannot flood the log with dozens of near-identical lines.
    """
    service = _service(tmp_path)
    staging_dir = tmp_path / 'staged'
    missing = tmp_path / 'aud-missing.wav'
    good = _wav(tmp_path / 'aud-good.wav', 4096)
    caplog.set_level(logging.INFO)

    staged = service._stage_audio_segments(
        [_item(missing, 0.0), _item(good, 1.0)],
        staging_dir,
    )

    assert len(staged) == 1
    staged_path, start, end = staged[0]
    assert staged_path.parent == staging_dir
    assert staged_path.read_bytes() == good.read_bytes()
    assert (start, end) == (1.0, 2.0)
    assert 'disappeared before mux' in caplog.text
    # The summary line reports the loss without per-file spam at INFO level.
    assert 'aud-missing.wav' not in caplog.text


def test_mux_skips_sidecar_disappearing_after_probe(tmp_path, monkeypatch):
    """A sidecar can vanish after probing but before the staging copy."""
    service = _service(tmp_path)
    audio_dir = service.audio_dir / 'camera-1'
    audio_dir.mkdir(parents=True, exist_ok=True)
    source = _wav(audio_dir / 'aud-000.wav', 4096)
    now = time.time()

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(service, '_segment_timeline', lambda *a, **k: [(source, now - 1, now)])
    monkeypatch.setattr(service, '_readable_audio_segments', lambda segments: segments)

    def disappear_before_copy(src, dst):
        Path(src).unlink()
        raise FileNotFoundError(src)

    monkeypatch.setattr(recordings_module.shutil, 'copyfile', disappear_before_copy)
    monkeypatch.setattr(
        recordings_module.subprocess,
        'run',
        lambda *a, **k: (_ for _ in ()).throw(AssertionError('ffmpeg must not run')),
    )

    assert service._mux_prebuffer_audio('camera-1', tmp_path / 'clip.mp4', now - 1, 1.0) is False


def test_mux_uses_staged_audio_paths_and_cleans_them(tmp_path, monkeypatch):
    """The mux must not hand live prunable sidecar paths to ffmpeg."""
    service = _service(tmp_path)
    audio_dir = service.audio_dir / 'camera-1'
    audio_dir.mkdir(parents=True, exist_ok=True)
    source = _real_wav(audio_dir / 'aud-000.wav')
    now = time.time()
    os.utime(source, (now, now))

    captured = {}

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(service, '_readable_audio_segments', lambda segments: segments)
    monkeypatch.setattr(service, '_segment_timeline', lambda *a, **k: [(source, now - 1, now)])
    monkeypatch.setattr(RecordingService, 'clip_has_video_stream', staticmethod(lambda _p: True))

    def fake_run(command, *_args, **_kwargs):
        input_indices = [index for index, value in enumerate(command) if value == '-i']
        captured['audio_path'] = Path(command[input_indices[1] + 1])
        assert captured['audio_path'].exists()
        Path(command[-1]).write_bytes(b'muxed')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)
    video = tmp_path / 'clip.mp4'
    video.write_bytes(b'video')

    assert service._mux_prebuffer_audio('camera-1', video, now - 1, 1.0) is True
    assert captured['audio_path'] != source
    assert captured['audio_path'].parent != audio_dir
    assert not captured['audio_path'].exists()
    assert video.read_bytes() == b'muxed'


def test_mux_command_omits_faststart_second_pass(tmp_path, monkeypatch):
    """The mux must not ask ffmpeg for +faststart.

    ffmpeg's faststart second pass rewrites the output in place after the
    encode and has failed with ENOSPC on a healthy recordings filesystem,
    discarding a completed mux and keeping the silent clip. The moov atom
    stays at the end of the file, which the range-capable recordings endpoint
    serves fine.
    """
    service = _service(tmp_path)
    audio_dir = service.audio_dir / 'camera-1'
    audio_dir.mkdir(parents=True, exist_ok=True)
    source = _real_wav(audio_dir / 'aud-000.wav')
    now = time.time()
    os.utime(source, (now, now))

    captured = {}

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(service, '_readable_audio_segments', lambda segments: segments)
    monkeypatch.setattr(service, '_segment_timeline', lambda *a, **k: [(source, now - 1, now)])
    monkeypatch.setattr(RecordingService, 'clip_has_video_stream', staticmethod(lambda _p: True))

    def fake_run(command, *_args, **_kwargs):
        captured['command'] = command
        Path(command[-1]).write_bytes(b'muxed')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)
    video = tmp_path / 'clip.mp4'
    video.write_bytes(b'video')

    assert service._mux_prebuffer_audio('camera-1', video, now - 1, 1.0) is True
    assert '+faststart' not in captured['command']
    assert '-movflags' not in captured['command']
    assert video.read_bytes() == b'muxed'


def test_mux_stages_audio_on_recordings_volume_not_system_temp(tmp_path, monkeypatch):
    """Sidecars must be staged on the recordings volume (under ``.prebuffer``),
    not the default ``$TMPDIR``.

    On appliance deployments ``/tmp`` is a small RAM-backed tmpfs; staging a
    long clip's worth of 1-second WAV sidecars there exhausts it and raises
    ENOSPC, which the mux then mis-reports as "the recordings disk is full"
    even though the recordings drive has ample space. Keeping the staging dir
    on the same filesystem as the mux output makes that accounting honest.
    """
    service = _service(tmp_path)
    audio_dir = service.audio_dir / 'camera-1'
    audio_dir.mkdir(parents=True, exist_ok=True)
    source = _real_wav(audio_dir / 'aud-000.wav')
    now = time.time()
    os.utime(source, (now, now))

    captured = {}

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(service, '_readable_audio_segments', lambda segments: segments)
    monkeypatch.setattr(service, '_segment_timeline', lambda *a, **k: [(source, now - 1, now)])
    monkeypatch.setattr(RecordingService, 'clip_has_video_stream', staticmethod(lambda _p: True))

    def fake_run(command, *_args, **_kwargs):
        input_indices = [index for index, value in enumerate(command) if value == '-i']
        captured['audio_path'] = Path(command[input_indices[1] + 1])
        Path(command[-1]).write_bytes(b'muxed')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)
    video = tmp_path / 'clip.mp4'
    video.write_bytes(b'video')

    assert service._mux_prebuffer_audio('camera-1', video, now - 1, 1.0) is True
    staged = captured['audio_path'].resolve()
    # Staged copy lives on the recordings volume, under its ``.prebuffer`` dir --
    # NOT wherever the default ``tempfile`` location (``$TMPDIR``) would put it.
    assert service.prebuffer_dir.resolve() in staged.parents
    assert service.recordings_dir.resolve() in staged.parents
    # Guard against a regression to a bare ``mkdtemp()``: the staging dir must be
    # a child of ``.prebuffer``, so the default-temp base can't be an ancestor
    # unless the recordings volume itself lives there.
    assert staged.parent.parent == service.prebuffer_dir.resolve()


def test_mux_returns_false_when_all_segments_unreadable(tmp_path, monkeypatch):
    """If every candidate segment is unreadable, the mux bails out (keeping the
    silent clip) instead of invoking ffmpeg with an invalid input set."""
    service = _service(tmp_path)
    audio_dir = service.audio_dir / 'camera-1'
    audio_dir.mkdir(parents=True, exist_ok=True)
    _wav(audio_dir / 'aud-20260803T162517.wav', 4096)

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffprobe')
    monkeypatch.setattr(
        service, '_segment_timeline',
        lambda *a, **k: [(audio_dir / 'aud-20260803T162517.wav', 0.0, 1.0)],
    )
    monkeypatch.setattr(service, '_readable_audio_segments', lambda segments: [])

    # subprocess.run must never be reached; make it explode if it is.
    def boom(*a, **k):
        raise AssertionError('ffmpeg must not run when no readable audio remains')

    monkeypatch.setattr(recordings_module.subprocess, 'run', boom)

    assert service._mux_prebuffer_audio('camera-1', tmp_path / 'clip.mp4', 0.0, 1.0) is False


def test_mux_skips_upfront_when_recordings_disk_full(tmp_path, monkeypatch, caplog):
    """With too little free space on the recordings filesystem the mux must
    not waste CPU on a doomed ffmpeg run: it keeps the silent clip, logs a
    clear warning, and emits a camera diagnostic."""
    service = _service(tmp_path)
    audio_dir = service.audio_dir / 'camera-1'
    audio_dir.mkdir(parents=True, exist_ok=True)
    source = _wav(audio_dir / 'aud-000.wav', 4096)
    now = time.time()
    diagnostics = []
    service.diagnostic_callback = lambda *a, **k: diagnostics.append((a, k))

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(service, '_segment_timeline', lambda *a, **k: [(source, now - 1, now)])
    monkeypatch.setattr(service, '_readable_audio_segments', lambda segments: segments)
    monkeypatch.setattr(
        recordings_module.shutil,
        'disk_usage',
        lambda _path: types.SimpleNamespace(free=1_000),  # far below needed
    )

    # ffmpeg must never be invoked when the pre-flight check already knows the
    # disk cannot hold the muxed output.
    def boom(*a, **k):
        raise AssertionError('ffmpeg must not run when the disk is full')

    monkeypatch.setattr(recordings_module.subprocess, 'run', boom)

    video = tmp_path / 'clip.mp4'
    video.write_bytes(b'video' * 1000)
    caplog.set_level(logging.WARNING)

    assert service._mux_prebuffer_audio('camera-1', video, now - 1, 1.0) is False
    assert video.exists(), 'the silent clip must be kept'
    assert 'recordings disk is full' in caplog.text
    assert diagnostics, 'a camera diagnostic must be emitted'
    assert diagnostics[0][0][1] == 'audio_mux_disk_full'
    assert diagnostics[0][1]['severity'] == 'warning'
    assert diagnostics[0][1]['details']['stage'] == 'preflight'
    assert diagnostics[0][1]['details']['free_bytes'] == 1_000
    assert diagnostics[0][1]['details']['needed_bytes'] is not None


def test_mux_detects_enospc_from_ffmpeg(tmp_path, monkeypatch, caplog):
    """When ffmpeg itself fails with ENOSPC (the disk genuinely filled between
    the pre-flight check and the write), the failure must be surfaced as a clear
    disk-full warning + diagnostic instead of ffmpeg's raw stderr."""
    service = _service(tmp_path)
    audio_dir = service.audio_dir / 'camera-1'
    audio_dir.mkdir(parents=True, exist_ok=True)
    source = _real_wav(audio_dir / 'aud-000.wav')
    now = time.time()
    diagnostics = []
    service.diagnostic_callback = lambda *a, **k: diagnostics.append((a, k))

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(service, '_segment_timeline', lambda *a, **k: [(source, now - 1, now)])
    monkeypatch.setattr(service, '_readable_audio_segments', lambda segments: segments)

    def fake_run(command, *_args, **_kwargs):
        # Mimic the ffmpeg failure from the field: the whole encode ran, then
        # the faststart second pass died with ENOSPC.
        return subprocess.CompletedProcess(
            command, -28,
            stdout='',
            stderr='[fc#0] Terminating thread with return code -28 (No space left on device)\nConversion failed!',
        )

    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)

    video = tmp_path / 'clip.mp4'
    video.write_bytes(b'video')
    caplog.set_level(logging.WARNING)

    assert service._mux_prebuffer_audio('camera-1', video, now - 1, 1.0) is False
    assert video.exists(), 'the silent clip must be kept'
    assert 'recordings disk is full' in caplog.text
    assert 'No space left on device' not in caplog.text
    assert diagnostics and diagnostics[0][0][1] == 'audio_mux_disk_full'
    assert diagnostics[0][1]['details']['stage'] == 'mux'
    assert 'No space left on device' in diagnostics[0][1]['details']['stderr_tail']


def test_mux_disk_full_diagnostic_is_rate_limited(tmp_path, monkeypatch):
    """While the disk stays full every finalized event re-hits the disk-full
    path; the camera diagnostic must be emitted at most once per camera per
    ``AUDIO_MUX_DISK_FULL_DIAGNOSTIC_MIN_SECONDS``."""
    service = _service(tmp_path)
    diagnostics = []
    service.diagnostic_callback = lambda *a, **k: diagnostics.append((a, k))
    fake_now = [1_000.0]
    monkeypatch.setattr(recordings_module.time, 'time', lambda: fake_now[0])

    def warn():
        service._warn_audio_mux_disk_full(
            'camera-1', 'camera-1', stage='preflight',
            free_bytes=1, needed_bytes=10_000,
        )

    warn()
    fake_now[0] += 60  # 1 minute later, disk still full
    warn()
    assert len(diagnostics) == 1, 'second emission within the window must be suppressed'

    fake_now[0] += 1800  # window elapsed
    warn()
    assert len(diagnostics) == 2


def test_mux_staging_enospc_surfaces_disk_full(tmp_path, monkeypatch, caplog):
    """A full staging filesystem (ENOSPC while copying sidecars) must be
    surfaced as a disk-full condition, not misread as "sidecar disappeared"."""
    service = _service(tmp_path)
    audio_dir = service.audio_dir / 'camera-1'
    audio_dir.mkdir(parents=True, exist_ok=True)
    source = _wav(audio_dir / 'aud-000.wav', 4096)
    now = time.time()
    diagnostics = []
    service.diagnostic_callback = lambda *a, **k: diagnostics.append((a, k))

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(service, '_segment_timeline', lambda *a, **k: [(source, now - 1, now)])
    monkeypatch.setattr(service, '_readable_audio_segments', lambda segments: segments)

    def enospc_copy(_src, _dst):
        raise OSError(errno.ENOSPC, 'No space left on device')

    monkeypatch.setattr(recordings_module.shutil, 'copyfile', enospc_copy)
    monkeypatch.setattr(
        recordings_module.subprocess,
        'run',
        lambda *a, **k: (_ for _ in ()).throw(AssertionError('ffmpeg must not run')),
    )

    video = tmp_path / 'clip.mp4'
    video.write_bytes(b'video')
    caplog.set_level(logging.WARNING)

    assert service._mux_prebuffer_audio('camera-1', video, now - 1, 1.0) is False
    assert 'recordings disk is full' in caplog.text
    assert diagnostics and diagnostics[0][0][1] == 'audio_mux_disk_full'
    assert diagnostics[0][1]['details']['stage'] == 'staging'
    # Not the misleading "sidecar disappeared" path.
    assert 'disappeared before mux' not in caplog.text


def _fake_statvfs(*, free_inodes: int, total_inodes: int = 10_000_000):
    """Return a statvfs stand-in reporting plenty of free bytes but a chosen
    number of free inodes, to model inode exhaustion on a roomy disk."""
    def statvfs(_path):
        # Include the block fields too so the real ``shutil.disk_usage`` (which
        # also calls ``os.statvfs``) keeps working when this stand-in is active.
        return types.SimpleNamespace(
            f_favail=free_inodes,
            f_files=total_inodes,
            f_bavail=10 ** 9,
            f_bfree=10 ** 9,
            f_blocks=10 ** 9,
            f_frsize=4096,
        )
    return statvfs


def test_mux_reports_inode_exhaustion_at_preflight(tmp_path, monkeypatch, caplog):
    """When the recordings filesystem has free bytes but no free inodes, the
    pre-flight must keep the silent clip WITHOUT running ffmpeg and report an
    honest "out of inodes" message -- a plain "disk is full" line would send
    operators to check free space that ``df -h`` shows is fine."""
    service = _service(tmp_path)
    audio_dir = service.audio_dir / 'camera-1'
    audio_dir.mkdir(parents=True, exist_ok=True)
    source = _wav(audio_dir / 'aud-000.wav', 4096)
    now = time.time()
    diagnostics = []
    service.diagnostic_callback = lambda *a, **k: diagnostics.append((a, k))

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(service, '_segment_timeline', lambda *a, **k: [(source, now - 1, now)])
    monkeypatch.setattr(service, '_readable_audio_segments', lambda segments: segments)
    # Plenty of free bytes -- the byte pre-flight passes.
    monkeypatch.setattr(
        recordings_module.shutil, 'disk_usage',
        lambda _path: types.SimpleNamespace(free=10 ** 12),
    )
    # ...but the inode table is exhausted.
    monkeypatch.setattr(recordings_module.os, 'statvfs', _fake_statvfs(free_inodes=0))

    def boom(*a, **k):
        raise AssertionError('ffmpeg must not run when inodes are exhausted')

    monkeypatch.setattr(recordings_module.subprocess, 'run', boom)

    video = tmp_path / 'clip.mp4'
    video.write_bytes(b'video')
    caplog.set_level(logging.WARNING)

    assert service._mux_prebuffer_audio('camera-1', video, now - 1, 1.0) is False
    assert video.exists(), 'the silent clip must be kept'
    assert 'out of inodes' in caplog.text
    # The misleading byte-shortage wording must NOT appear for an inode shortage.
    assert 'recordings disk is full' not in caplog.text
    assert diagnostics and diagnostics[0][0][1] == 'audio_mux_disk_full'
    assert diagnostics[0][1]['details']['stage'] == 'preflight'
    assert diagnostics[0][1]['details']['free_inodes'] == 0


def test_mux_staging_enospc_reports_inode_exhaustion(tmp_path, monkeypatch, caplog):
    """A staging ENOSPC on a disk with free bytes but no free inodes is
    reported as inode exhaustion, not the misleading "disk is full"."""
    service = _service(tmp_path)
    audio_dir = service.audio_dir / 'camera-1'
    audio_dir.mkdir(parents=True, exist_ok=True)
    source = _wav(audio_dir / 'aud-000.wav', 4096)
    now = time.time()
    diagnostics = []
    service.diagnostic_callback = lambda *a, **k: diagnostics.append((a, k))

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(service, '_segment_timeline', lambda *a, **k: [(source, now - 1, now)])
    monkeypatch.setattr(service, '_readable_audio_segments', lambda segments: segments)
    monkeypatch.setattr(recordings_module.os, 'statvfs', _fake_statvfs(free_inodes=12))

    def enospc_copy(_src, _dst):
        raise OSError(errno.ENOSPC, 'No space left on device')

    monkeypatch.setattr(recordings_module.shutil, 'copyfile', enospc_copy)
    monkeypatch.setattr(
        recordings_module.subprocess, 'run',
        lambda *a, **k: (_ for _ in ()).throw(AssertionError('ffmpeg must not run')),
    )

    video = tmp_path / 'clip.mp4'
    video.write_bytes(b'video')
    caplog.set_level(logging.WARNING)

    assert service._mux_prebuffer_audio('camera-1', video, now - 1, 1.0) is False
    assert 'out of inodes' in caplog.text
    assert diagnostics and diagnostics[0][1]['details']['stage'] == 'staging'
    assert diagnostics[0][1]['details']['free_inodes'] == 12


def test_mux_enospc_falls_back_to_disk_full_when_inodes_are_fine(tmp_path, monkeypatch, caplog):
    """An ffmpeg ENOSPC while inodes are plentiful still reports a genuine
    byte-shortage "disk is full" -- inode reporting must not swallow a real
    full-disk condition."""
    service = _service(tmp_path)
    audio_dir = service.audio_dir / 'camera-1'
    audio_dir.mkdir(parents=True, exist_ok=True)
    source = _real_wav(audio_dir / 'aud-000.wav')
    now = time.time()
    diagnostics = []
    service.diagnostic_callback = lambda *a, **k: diagnostics.append((a, k))

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(service, '_segment_timeline', lambda *a, **k: [(source, now - 1, now)])
    monkeypatch.setattr(service, '_readable_audio_segments', lambda segments: segments)
    monkeypatch.setattr(recordings_module.os, 'statvfs', _fake_statvfs(free_inodes=1_000_000))

    def fake_run(command, *_args, **_kwargs):
        return subprocess.CompletedProcess(
            command, -28, stdout='',
            stderr='No space left on device\nConversion failed!',
        )

    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)

    video = tmp_path / 'clip.mp4'
    video.write_bytes(b'video')
    caplog.set_level(logging.WARNING)

    assert service._mux_prebuffer_audio('camera-1', video, now - 1, 1.0) is False
    assert 'recordings disk is full' in caplog.text
    assert 'out of inodes' not in caplog.text
    assert diagnostics and diagnostics[0][1]['details']['stage'] == 'mux'


def test_free_inodes_returns_none_when_unavailable(tmp_path, monkeypatch):
    """Without ``os.statvfs`` (e.g. Windows) or when the FS reports no inode
    table, ``_free_inodes`` returns ``(None, None)`` so callers fall back to
    the generic wording rather than falsely claiming inode exhaustion."""
    service = _service(tmp_path)

    monkeypatch.delattr(recordings_module.os, 'statvfs', raising=False)
    assert service._free_inodes(tmp_path) == (None, None)

    # f_files == 0 -> the filesystem does not track inodes; treat as unknown.
    monkeypatch.setattr(
        recordings_module.os, 'statvfs',
        lambda _p: types.SimpleNamespace(f_favail=0, f_files=0),
        raising=False,
    )
    assert service._free_inodes(tmp_path) == (None, None)


def test_probe_writable_space_reports_success(tmp_path):
    service = _service(tmp_path)
    result = service._probe_writable_space(tmp_path)
    assert result['ok'] is True
    assert result['bytes_written'] == 4 * 1024 * 1024
    # The probe file must never outlive the check.
    assert not list(tmp_path.glob('.write-probe-*'))


def test_probe_writable_space_reports_failure_without_raising(tmp_path):
    service = _service(tmp_path)
    missing = tmp_path / 'does-not-exist'
    result = service._probe_writable_space(missing, probe_bytes=1024)
    assert result['ok'] is False
    assert 'errno' in result


def _read_frames(path: Path) -> tuple[int, tuple[int, ...]]:
    import struct
    import wave

    with wave.open(str(path), 'rb') as reader:
        rate = reader.getframerate()
        data = struct.unpack('<%dh' % reader.getnframes(), reader.readframes(reader.getnframes()))
    return rate, data


def test_assemble_gapped_audio_places_segments_and_fills_gaps(tmp_path):
    """The Python assembler must lay each segment at its wall-clock offset,
    trim a pre-roll overlap to video t=0, fill gaps with silence, and pad the
    tail to the full clip length -- the timeline the old delayed-input
    filtergraph produced, without the giant graph that tripped ffmpeg."""
    service = _service(tmp_path)
    rate = 16000
    pre = _real_wav(tmp_path / 'pre.wav', 1.0, rate=rate, value=500)   # [99.5,100.5]
    a = _real_wav(tmp_path / 'a.wav', 1.0, rate=rate, value=1000)      # [100,101]
    c = _real_wav(tmp_path / 'c.wav', 1.0, rate=rate, value=2000)      # [102,103], gap at 101-102
    segments = [(pre, 99.5, 100.5), (a, 100.0, 101.0), (c, 102.0, 103.0)]

    out = service._assemble_gapped_audio_wav(segments, 100.0, 5.0, tmp_path / 'out.wav')
    assert out is not None
    got_rate, frames = _read_frames(out)
    assert got_rate == rate
    assert len(frames) == 5 * rate, 'track spans the whole 5s clip'
    assert frames[rate // 2] == 1000, 'segment a overrides the trimmed pre-roll at t=0.5s'
    assert set(frames[int(1.2 * rate):int(1.8 * rate)]) == {0}, 'the 101-102 gap is silence'
    assert frames[int(2.5 * rate)] == 2000, 'segment c lands at its wall-clock offset'
    assert set(frames[int(4.2 * rate):]) == {0}, 'the tail is padded with silence'


def test_assemble_gapped_audio_skips_unreadable_and_returns_none_when_empty(tmp_path):
    """An unreadable/mid-write sidecar contributes a gap rather than aborting;
    if nothing readable remains the assembler returns None so the caller keeps
    the silent clip."""
    service = _service(tmp_path)
    good = _real_wav(tmp_path / 'good.wav', 1.0, value=1500)
    bad = _wav(tmp_path / 'bad.wav', 128)  # not a valid WAV

    mixed = service._assemble_gapped_audio_wav(
        [(bad, 100.0, 101.0), (good, 101.0, 102.0)], 100.0, 3.0, tmp_path / 'mixed.wav'
    )
    assert mixed is not None
    rate, frames = _read_frames(mixed)
    assert set(frames[:rate]) == {0}, 'the unreadable first second is a silent gap'
    assert frames[int(1.5 * rate)] == 1500, 'the readable segment still lands'

    assert service._assemble_gapped_audio_wav([], 100.0, 3.0, tmp_path / 'empty.wav') is None
    assert service._assemble_gapped_audio_wav(
        [(bad, 100.0, 101.0)], 100.0, 3.0, tmp_path / 'none.wav'
    ) is None


def test_first_error_line_skips_scheduler_cascade(tmp_path):
    """_first_error_line must return the originating error, not ffmpeg 7.x's
    scheduler propagation chatter that only repeats the error number."""
    service = _service(tmp_path)
    stderr = (
        'ffmpeg version 7.1\n'
        '  built with gcc\n'
        '[out#0/mp4 @ 0x1] Error muxing a packet: No space left on device\n'
        '[fc#0 @ 0x2] Task finished with error code: -28 (No space left on device)\n'
        '[fc#0 @ 0x2] Terminating thread with return code -28 (No space left on device)\n'
        'Conversion failed!\n'
    )
    origin = service._first_error_line(stderr)
    assert 'Error muxing a packet' in origin
    assert 'Task finished' not in origin
    assert 'Terminating thread' not in origin


def test_first_error_line_empty_when_no_error(tmp_path):
    service = _service(tmp_path)
    assert service._first_error_line('frame= 100 fps=25\nvideo:10KiB audio:1KiB\n') == ''
