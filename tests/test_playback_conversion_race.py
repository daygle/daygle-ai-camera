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

    def slow_convert(source, output, **_kwargs):
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

    def failing_convert(source, output, **_kwargs):
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
    # CPU only, so the GPU capability check does not run through fake_run.
    monkeypatch.setattr(mu, '_playback_encode_attempts', lambda: [('cpu', [], ['-c:v', 'libx264'])])
    monkeypatch.setattr(mu.subprocess, 'run', fake_run)
    try:
        mu.transcode_recording_to_mp4(source, output)
    except mu.subprocess.TimeoutExpired:
        pass
    assert sorted(p.name for p in tmp_path.iterdir()) == ['event_2.mp4']


# ---------------------------------------------------------------------------
# H.265 / H.265+ support
# ---------------------------------------------------------------------------

def _fake_mp4(path, fourcc, *, at_end=False):
    """Minimal bytes carrying an ``stsd`` sample entry with ``fourcc``."""
    entry = b'\x00\x00\x00\x10stsd\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x50' + fourcc + b'\x00' * 40
    filler = b'\x00' * (300 * 1024)
    path.write_bytes(filler + entry if at_end else entry + filler)
    return path


def test_codec_is_read_from_the_mp4_header_or_tail(tmp_path):
    assert mu.mp4_sample_entry_codec(_fake_mp4(tmp_path / 'a.mp4', b'hev1')) == 'hevc'
    assert mu.mp4_sample_entry_codec(_fake_mp4(tmp_path / 'b.mp4', b'hvc1', at_end=True)) == 'hevc'
    assert mu.mp4_sample_entry_codec(_fake_mp4(tmp_path / 'c.mp4', b'avc1')) == 'h264'
    assert mu.mp4_sample_entry_codec(tmp_path / 'missing.mp4') is None


def test_hvc1_tag_only_for_h265(tmp_path):
    """ffmpeg copies H.265 into MP4 as hev1, which Apple players refuse; the
    hvc1 tag must never be set on H.264 (ffmpeg then fails to write)."""
    assert mu.hevc_mp4_tag_args(_fake_mp4(tmp_path / 'h265.mp4', b'hev1')) == ['-tag:v', 'hvc1']
    assert mu.hevc_mp4_tag_args(_fake_mp4(tmp_path / 'h264.mp4', b'avc1')) == []
    assert mu.hevc_mp4_tag_args(None) == []


def test_newest_video_file_skips_temporary_files(tmp_path):
    old = _fake_mp4(tmp_path / 'chunk_1.mp4', b'hev1')
    os.utime(old, (1, 1))
    new = _fake_mp4(tmp_path / 'chunk_2.mp4', b'hev1')
    _fake_mp4(tmp_path / 'chunk_3.tmp.mp4', b'avc1')
    assert mu.newest_video_file(tmp_path) == new
    assert mu.newest_video_file(tmp_path / 'nope') is None


def test_gpu_conversion_falls_back_to_the_cpu(tmp_path, monkeypatch):
    source = tmp_path / 'event_3.mp4'
    source.write_bytes(b'x')
    attempts = []
    monkeypatch.setattr(mu, '_playback_encode_attempts', lambda: [('gpu', ['-hwaccel', 'cuda'], ['-c:v', 'h264_nvenc']), ('cpu', [], ['-c:v', 'libx264'])])

    def fake_attempt(_ffmpeg, _source, output, input_args, video_args, *_args):
        attempts.append(video_args[1])
        if video_args[1] == 'h264_nvenc':
            raise RuntimeError('no CUDA device')
        output.write_bytes(b'h264')

    monkeypatch.setattr(mu, '_FFMPEG', '/usr/bin/ffmpeg')
    monkeypatch.setattr(mu, 'probe_video_duration', lambda _path: 1.0)
    monkeypatch.setattr(mu, '_transcode_attempt', fake_attempt)
    output = mu.recording_playback_sidecar_path(source)
    mu.transcode_recording_to_mp4(source, output)
    assert attempts == ['h264_nvenc', 'libx264']
    assert output.exists()


def test_background_conversion_runs_once_per_clip(tmp_path, monkeypatch):
    done = threading.Event()
    converted = []
    monkeypatch.setattr(mu, 'mp4_is_browser_playable', lambda _path: False)

    def fake_stream_path(path, *, low_priority=False):
        converted.append((path.name, low_priority))
        done.set()
        return path

    monkeypatch.setattr(mu, 'recording_stream_path', fake_stream_path)
    clip = tmp_path / 'event_4.mp4'
    clip.write_bytes(b'hevc')
    assert mu.schedule_playback_conversion(clip) is True
    assert done.wait(5)
    mu._preconvert_queue.join()
    assert converted == [('event_4.mp4', True)]
    assert mu.schedule_playback_conversion(None) is False
