"""Regression test for the prebuffer render window.

The rendered clip starts at the first selected segment's content start, which
can precede the requested window: segments close on a keyframe, so a camera with
a sparse keyframe interval leads the window by up to a whole segment. The render
length was capped at ``max_duration_seconds + 10``, which kept the keyframe lead
but cut the same seconds off the post-event tail - the clip ended before the
capture deadline it was rendered for.
"""

from __future__ import annotations

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


def test_render_window_keeps_the_tail_after_a_long_keyframe_lead(tmp_path, monkeypatch):
    service = _service(tmp_path)
    triggered_ts = time.time() - 10
    # Sparse keyframes: the first segment covering the 10s pre-roll started 15s
    # before it, so the footage spans 5s more than the requested pre+post window.
    content_start_ts = triggered_ts - 25.0
    segment = tmp_path / 'segment-000.mp4'
    segment.write_bytes(b'\0' * 1024)

    monkeypatch.setattr(service, '_ensure_prebuffer_worker', lambda *a, **k: None)
    monkeypatch.setattr(
        service, '_collect_prebuffer_segments', lambda *a, **k: ([segment], content_start_ts)
    )
    monkeypatch.setattr(service, '_mux_prebuffer_audio', lambda *a, **k: False)
    monkeypatch.setattr(recordings_module, 'hevc_mp4_tag_args', lambda *_a, **_k: [])
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(RecordingService, 'clip_has_video_stream', staticmethod(lambda _path: True))
    monkeypatch.setattr(RecordingService, 'clip_duration_seconds', staticmethod(lambda _path: None))
    commands = []

    def fake_run(command, *_args, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b'clip-bytes')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)

    _start, content_seconds = service.write_rtsp_clip_with_prebuffer(
        stream_url='rtsp://cam/stream',
        camera_id='cam',
        file_path=tmp_path / 'clip.mp4',
        triggered_at=datetime.fromtimestamp(triggered_ts, tz=timezone.utc),
        pre_seconds=10,
        post_seconds=5,
        max_duration_seconds=15.0,
    )

    # The capture deadline is trigger+5, so the footage spans 30s and every one
    # of those seconds must be rendered - not the 25s a 10s lead allowance kept.
    assert content_seconds == pytest.approx(30.0, abs=0.1)
    render_seconds = float(commands[0][commands[0].index('-t') + 1])
    assert render_seconds == pytest.approx(30.0, abs=0.1)
