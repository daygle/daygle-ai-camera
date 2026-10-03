"""Regression tests for prebuffer segment durations.

Prebuffer segments close on the first keyframe AFTER ``PREBUFFER_SEGMENT_SECONDS``
(4s), so a camera with a 6s keyframe interval writes 6s segments. The timeline
used to estimate each segment's start from mtime gaps, accepting a gap only up to
``4 * 1.5 = 6.0``s: real spacing jitters around 6.0, so most segments fell back
to ``end - 4`` and lost ~2s. Those estimates were also written into the concat
list as ``duration`` directives, which made ffmpeg squeeze every frame into a
compressed timeline and ``-t`` cut the clip short.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

recordings_module = importlib.import_module('app.recordings')  # noqa: E402
from app.recordings import RecordingService  # noqa: E402


def _service(tmp_path: Path) -> RecordingService:
    return RecordingService(
        {'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}}
    )


def _write_segments(camera_dir: Path, ends: list[float]) -> list[Path]:
    camera_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, end in enumerate(ends):
        segment = camera_dir / f'segment-{index:03d}.mp4'
        segment.write_bytes(b'segment')
        os.utime(segment, (end, end))
        paths.append(segment)
    return paths


def _fake_ffprobe(monkeypatch, durations: dict[str, str], calls: list[str] | None = None):
    """Answer ffprobe ``format=duration`` calls from ``durations`` (by file name)."""
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffprobe')

    def fake_run(command, *args, **kwargs):
        name = Path(command[-1]).name
        if calls is not None:
            calls.append(name)
        value = durations.get(name)
        if value is None:
            return subprocess.CompletedProcess(command, 1, stdout='', stderr='invalid data')
        return subprocess.CompletedProcess(command, 0, stdout=f'{value}\n', stderr='')

    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)


def test_timeline_uses_probed_duration_when_spacing_exceeds_estimate_threshold(tmp_path, monkeypatch):
    """6s segments spaced fractionally above 6.0s: the estimate falls back to
    ``end - 4`` for each, the probe gives the real 6s."""
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    now = time.time()
    ends = [now - 6.03 * (4 - i) for i in range(5)]
    _write_segments(camera_dir, ends)

    estimated = service._segment_timeline(camera_dir, service.PREBUFFER_SEGMENT_GLOB, service.PREBUFFER_SEGMENT_SECONDS)
    # The old model: every segment collapses to the 4s nominal length.
    assert [round(end - start, 2) for _, start, end in estimated] == [4.0] * 5

    _fake_ffprobe(monkeypatch, {f'segment-{i:03d}.mp4': '6.000000' for i in range(5)})
    service._invalidate_segment_timeline_cache()
    probed = service._segment_timeline(
        camera_dir, service.PREBUFFER_SEGMENT_GLOB, service.PREBUFFER_SEGMENT_SECONDS, probe_durations=True,
    )
    assert [round(end - start, 2) for _, start, end in probed] == [6.0] * 5
    assert [end for _, _, end in probed] == pytest.approx(ends, abs=0.01)


def test_collect_reports_content_start_from_probed_duration(tmp_path, monkeypatch):
    """The first selected segment's real start anchors the clip's timing and the
    detection track; with the estimate it was reported up to 2s late."""
    service = _service(tmp_path)
    now = time.time()
    ends = [now - 6.03 * (2 - i) for i in range(3)]
    _write_segments(service.prebuffer_dir / 'cam', ends)
    _fake_ffprobe(monkeypatch, {f'segment-{i:03d}.mp4': '6.000000' for i in range(3)})

    segments, content_start = service._collect_prebuffer_segments('cam', ends[0] - 1.0, now)
    assert len(segments) == 3
    assert content_start == pytest.approx(ends[0] - 6.0, abs=0.01)


def test_timeline_falls_back_to_estimate_when_probe_fails(tmp_path, monkeypatch):
    """The segment still being written (empty moov until its one fragment
    lands) and unreadable files fall back to the gap estimate."""
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    now = time.time()
    ends = [now - 8, now - 4, now]
    _write_segments(camera_dir, ends)
    # Only the first segment probes; the rest fail.
    _fake_ffprobe(monkeypatch, {'segment-000.mp4': '6.000000'})

    timed = service._segment_timeline(
        camera_dir, service.PREBUFFER_SEGMENT_GLOB, service.PREBUFFER_SEGMENT_SECONDS, probe_durations=True,
    )
    starts = [start for _, start, _ in timed]
    assert starts[0] == pytest.approx(ends[0] - 6.0, abs=0.01)
    # Contiguous within 1.5x nominal: chained to the previous segment's end.
    assert starts[1] == pytest.approx(ends[0], abs=0.01)
    assert starts[2] == pytest.approx(ends[1], abs=0.01)


@pytest.mark.parametrize('bad_value', ['0.000000', '-1', '600.0', 'N/A'])
def test_timeline_rejects_implausible_probe_results(tmp_path, monkeypatch, bad_value):
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    now = time.time()
    _write_segments(camera_dir, [now])
    _fake_ffprobe(monkeypatch, {'segment-000.mp4': bad_value})

    timed = service._segment_timeline(
        camera_dir, service.PREBUFFER_SEGMENT_GLOB, service.PREBUFFER_SEGMENT_SECONDS, probe_durations=True,
    )
    _, start, end = timed[0]
    assert end - start == pytest.approx(service.PREBUFFER_SEGMENT_SECONDS, abs=0.01)


def test_segment_probe_is_cached_per_file_identity_and_pruned(tmp_path, monkeypatch):
    """A finished segment is probed once for its life in the buffer; a segment
    whose size/mtime changes is re-probed; deleted segments are forgotten."""
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    now = time.time()
    paths = _write_segments(camera_dir, [now - 6, now])
    calls: list[str] = []
    _fake_ffprobe(monkeypatch, {p.name: '6.000000' for p in paths}, calls)

    def scan():
        service._invalidate_segment_timeline_cache()
        return service._segment_timeline(
            camera_dir, service.PREBUFFER_SEGMENT_GLOB, service.PREBUFFER_SEGMENT_SECONDS, probe_durations=True,
        )

    scan()
    scan()
    assert sorted(calls) == ['segment-000.mp4', 'segment-001.mp4']

    # The newest segment grows (a fragment landed): only it is re-probed.
    paths[1].write_bytes(b'segment-with-a-fragment')
    os.utime(paths[1], (now + 1, now + 1))
    calls.clear()
    scan()
    assert calls == ['segment-001.mp4']

    paths[0].unlink()
    scan()
    assert str(paths[0]) not in service._segment_duration_cache
    assert str(paths[1]) in service._segment_duration_cache


def test_audio_timeline_is_not_probed(tmp_path, monkeypatch):
    """Only the video prebuffer probes; other timelines keep the estimate."""
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    _write_segments(camera_dir, [time.time()])
    calls: list[str] = []
    _fake_ffprobe(monkeypatch, {'segment-000.mp4': '6.000000'}, calls)

    service._segment_timeline(camera_dir, service.PREBUFFER_SEGMENT_GLOB, service.PREBUFFER_SEGMENT_SECONDS)
    assert calls == []


@pytest.mark.skipif(
    not (shutil.which('ffmpeg') and shutil.which('ffprobe')),
    reason='needs real ffmpeg/ffprobe',
)
def test_render_of_six_second_keyframe_segments_keeps_full_window(tmp_path, monkeypatch):
    """End to end with real ffmpeg: segments written exactly like the ingest
    (4s segment_time, fragmented MP4, -c copy) from a 6s-keyframe source come
    out 6s long; the rendered clip must cover the full requested window instead
    of a compressed, truncated one."""
    ffmpeg = shutil.which('ffmpeg')
    source = tmp_path / 'source.mp4'
    subprocess.run(
        [ffmpeg, '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=25', '-t', '48',
         '-c:v', 'libx264', '-g', '150', '-keyint_min', '150', '-sc_threshold', '0',
         '-pix_fmt', 'yuv420p', str(source)],
        check=True,
    )
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    camera_dir.mkdir(parents=True)
    subprocess.run(
        [ffmpeg, '-loglevel', 'error', '-i', str(source), '-map', '0:v:0', '-c:v', 'copy', '-an',
         '-f', 'segment', '-segment_time', str(service.PREBUFFER_SEGMENT_SECONDS),
         '-segment_format', 'mp4',
         '-segment_format_options', 'movflags=+frag_keyframe+empty_moov+default_base_moof',
         str(camera_dir / 'segment-%03d.mp4')],
        check=True,
    )
    segments = sorted(camera_dir.glob('segment-*.mp4'))
    assert len(segments) == 8
    # Wall-clock mtimes as the ingest leaves them: one segment closing every
    # ~6.03s (spacing just above the old estimate's 6.0s threshold).
    now = time.time()
    for index, segment in enumerate(segments):
        end = now - 1 - 6.03 * (len(segments) - 1 - index)
        os.utime(segment, (end, end))

    monkeypatch.setattr(service, '_ensure_prebuffer_worker', lambda *a, **k: None)
    monkeypatch.setattr(service, '_mux_prebuffer_audio', lambda *a, **k: False)
    monkeypatch.setattr(service, '_live_capture', lambda *a, **k: pytest.fail('must render from the prebuffer'))

    # A 10s pre-roll / 26s post window well inside the buffered footage.
    first_end = now - 1 - 6.03 * (len(segments) - 1)
    triggered_at = datetime.fromtimestamp(first_end + 10, tz=timezone.utc)
    content_start_ts, content_seconds = service.write_rtsp_clip_with_prebuffer(
        stream_url='rtsp://cam/stream',
        camera_id='cam',
        file_path=tmp_path / 'clip.mp4',
        triggered_at=triggered_at,
        pre_seconds=10,
        post_seconds=26,
        max_duration_seconds=36,
    )

    requested_end = triggered_at.timestamp() + 26
    requested_seconds = requested_end - content_start_ts
    # The first selected segment starts on a real 6s boundary at or before the
    # requested pre-roll start, and the clip runs to the requested end.
    assert content_start_ts <= triggered_at.timestamp() - 10 + 0.05
    assert content_seconds == pytest.approx(requested_seconds, abs=0.5)
    probed = RecordingService.clip_duration_seconds(tmp_path / 'clip.mp4')
    assert probed == pytest.approx(requested_seconds, abs=0.5)
