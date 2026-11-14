"""Regression tests for prebuffer segments pruned out from under a render.

A long event clip is rendered only after its post-event window closes, when
its oldest buffered segment sits at the edge of the rolling buffer's
retention. The ingest pruner could delete that segment between selection and
ffmpeg opening it ("Impossible to open segment-...") and the whole render then
fell back to a live capture minutes after the event.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
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


def _segment(path: Path, mtime: float) -> Path:
    path.write_bytes(b'\0' * 1024)
    os.utime(path, (mtime, mtime))
    return path


def test_pruner_keeps_render_headroom_beyond_the_clip_window(tmp_path):
    service = _service(tmp_path)
    camera_dir = tmp_path / 'cam'
    camera_dir.mkdir()
    keep_seconds = 305
    now = time.time()
    # Just past the clip window: the segment a just-closed long clip still needs.
    needed = _segment(camera_dir / 'segment-needed.mp4', now - keep_seconds - 10)
    expired = _segment(
        camera_dir / 'segment-expired.mp4',
        now - keep_seconds - service.PREBUFFER_RENDER_HEADROOM_SECONDS - 10,
    )

    service._prune_prebuffer_segments(camera_dir, keep_seconds)

    assert needed.exists()
    assert not expired.exists()


def _patch_render(service, monkeypatch, collect):
    monkeypatch.setattr(service, '_ensure_prebuffer_worker', lambda *a, **k: None)
    monkeypatch.setattr(service, '_collect_prebuffer_segments', collect)
    monkeypatch.setattr(service, '_mux_prebuffer_audio', lambda *a, **k: False)
    monkeypatch.setattr(service, '_live_capture', lambda *a, **k: pytest.fail('fell back to live capture'))
    monkeypatch.setattr(recordings_module, 'hevc_mp4_tag_args', lambda *_a, **_k: [])
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(RecordingService, 'clip_has_video_stream', staticmethod(lambda path: Path(path).exists()))
    monkeypatch.setattr(RecordingService, 'clip_duration_seconds', staticmethod(lambda _path: None))


def test_render_retries_with_remaining_segments_when_one_was_pruned(tmp_path, monkeypatch):
    service = _service(tmp_path)
    triggered_ts = time.time() - 10
    first = tmp_path / 'segment-000.mp4'
    rest = [tmp_path / f'segment-00{i}.mp4' for i in (1, 2, 3)]
    for path in [first, *rest]:
        path.write_bytes(b'\0' * 1024)
    first_start = triggered_ts - 10.0
    rest_start = triggered_ts - 6.0

    def collect(_key, _start, _end):
        present = [p for p in [first, *rest] if p.exists()]
        return present, (first_start if first.exists() else rest_start)

    _patch_render(service, monkeypatch, collect)
    diagnostics = []
    service.diagnostic_callback = lambda cam, event, msg, **kw: diagnostics.append((event, kw.get('details') or {}))
    concat_lists = []

    def fake_run(command, *_args, **_kwargs):
        list_path = Path(command[command.index('-i') + 1])
        concat_lists.append(list_path.read_text(encoding='utf-8'))
        if len(concat_lists) == 1:
            # The pruner deletes the oldest segment just as ffmpeg starts.
            first.unlink()
            return subprocess.CompletedProcess(command, 254, stdout='', stderr="Impossible to open 'segment-000.mp4'")
        Path(command[-1]).write_bytes(b'clip-bytes')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)
    file_path = tmp_path / 'clip.mp4'

    content_start, content_seconds = service.write_rtsp_clip_with_prebuffer(
        stream_url='rtsp://cam/stream',
        camera_id='cam',
        file_path=file_path,
        triggered_at=datetime.fromtimestamp(triggered_ts, tz=timezone.utc),
        pre_seconds=10,
        post_seconds=5,
        max_duration_seconds=15.0,
    )

    assert file_path.read_bytes() == b'clip-bytes'
    assert len(concat_lists) == 2
    assert 'segment-000.mp4' not in concat_lists[1]
    assert content_start == pytest.approx(rest_start)
    assert content_seconds == pytest.approx(11.0, abs=0.1)
    events = dict(diagnostics)
    assert 'prebuffer_fallback' not in events
    assert events['prebuffer_short_preroll']['reason'] == 'segments_pruned'


def test_render_failure_without_missing_segments_is_not_retried(tmp_path, monkeypatch):
    service = _service(tmp_path)
    triggered_ts = time.time() - 10
    segment = tmp_path / 'segment-000.mp4'
    segment.write_bytes(b'\0' * 1024)
    _patch_render(service, monkeypatch, lambda *_a: ([segment], triggered_ts - 10.0))
    live = []
    monkeypatch.setattr(service, '_live_capture', lambda *a, **k: live.append(a) or (triggered_ts, 15.0))
    runs = []

    def fake_run(command, *_args, **_kwargs):
        runs.append(command)
        return subprocess.CompletedProcess(command, 1, stdout='', stderr='Invalid data found')

    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)

    service.write_rtsp_clip_with_prebuffer(
        stream_url='rtsp://cam/stream',
        camera_id='cam',
        file_path=tmp_path / 'clip.mp4',
        triggered_at=datetime.fromtimestamp(triggered_ts, tz=timezone.utc),
        pre_seconds=10,
        post_seconds=5,
        max_duration_seconds=15.0,
    )

    assert len(runs) == 1
    assert len(live) == 1
