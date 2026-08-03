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

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.recordings as recordings_module  # noqa: E402
from app.recordings import RecordingService  # noqa: E402


def _service(tmp_path: Path) -> RecordingService:
    return RecordingService(
        {'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}}
    )


def _wav(path: Path, size: int) -> Path:
    """Create a placeholder WAV file of ``size`` bytes."""
    path.write_bytes(b'\x00' * size)
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
