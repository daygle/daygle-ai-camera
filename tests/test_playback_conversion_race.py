"""Browser-playback conversion of HEVC recordings (app/media_utils.py)."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.media_utils as mu  # noqa: E402


def _hevc_clip(tmp_path, monkeypatch, convert):
    clip = tmp_path / 'event_1.mp4'
    clip.write_bytes(b'hevc')
    monkeypatch.setattr(mu, 'mp4_is_browser_playable', lambda _path: False)
    monkeypatch.setattr(mu, 'transcode_recording_to_mp4', convert)
    return clip


def test_concurrent_plays_share_one_conversion(tmp_path, monkeypatch):
    """Recording 21395: two stream requests for one clip each converted it into
    the same temporary file, both failed, and the clip was marked unplayable."""
    calls = []

    def slow_convert(source, output):
        calls.append(source)
        time.sleep(0.2)
        output.write_bytes(b'h264')

    clip = _hevc_clip(tmp_path, monkeypatch, slow_convert)
    served = []
    threads = [threading.Thread(target=lambda: served.append(mu.recording_stream_path(clip))) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1
    assert served == [mu.recording_playback_sidecar_path(clip)] * 3
    assert not clip.with_name('event_1.playback.failed').exists()


def test_a_failed_conversion_is_retried_after_a_while(tmp_path, monkeypatch):
    attempts = []

    def failing_convert(source, output):
        attempts.append(source)
        raise RuntimeError('boom')

    clip = _hevc_clip(tmp_path, monkeypatch, failing_convert)
    marker = clip.with_name('event_1.playback.failed')
    assert mu.recording_stream_path(clip) == clip
    assert marker.exists()
    assert mu.recording_stream_path(clip) == clip
    assert len(attempts) == 1  # not retried on every play...
    old = time.time() - mu.PLAYBACK_FAILURE_RETRY_SECONDS - 5
    os.utime(clip, (old - 10, old - 10))
    os.utime(marker, (old, old))
    mu.recording_stream_path(clip)
    assert len(attempts) == 2  # ...but retried once the marker is old


def test_conversion_cleans_up_its_temporary_file(tmp_path, monkeypatch):
    source = tmp_path / 'event_2.mp4'
    source.write_bytes(b'x')
    output = mu.recording_playback_sidecar_path(source)

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(b'partial')
        raise mu.subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(mu, '_FFMPEG', '/usr/bin/ffmpeg')
    monkeypatch.setattr(mu, 'probe_video_duration', lambda _path: 1.0)
    monkeypatch.setattr(mu.subprocess, 'run', fake_run)
    try:
        mu.transcode_recording_to_mp4(source, output)
    except mu.subprocess.TimeoutExpired:
        pass
    assert sorted(p.name for p in tmp_path.iterdir()) == ['event_2.mp4']
