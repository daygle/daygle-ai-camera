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


# Big enough to hold a video fragment: smaller closed segments that cannot be
# probed are footage-less leftovers of an interrupted ingest and are skipped.
_SEGMENT_BYTES = b'\0' * (64 * 1024)


def _write_segments(camera_dir: Path, ends: list[float], sizes: dict[int, int] | None = None) -> list[Path]:
    camera_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, end in enumerate(ends):
        segment = camera_dir / f'segment-{index:03d}.mp4'
        size = (sizes or {}).get(index)
        segment.write_bytes(_SEGMENT_BYTES if size is None else b'\0' * size)
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


def _refined(service, camera_dir, start_ts, end_ts):
    timed = service._segment_timeline(camera_dir, service.PREBUFFER_SEGMENT_GLOB, service.PREBUFFER_SEGMENT_SECONDS)
    return service._probe_window_segment_starts(camera_dir, timed, start_ts, end_ts)


def test_window_segments_use_probed_duration_when_spacing_exceeds_estimate_threshold(tmp_path, monkeypatch):
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
    probed = _refined(service, camera_dir, ends[0] - 10, now)
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


def test_probing_falls_back_to_estimate_when_probe_fails(tmp_path, monkeypatch):
    """The segment still being written (empty moov until its one fragment
    lands) and unreadable files keep the gap estimate."""
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    now = time.time()
    ends = [now - 8, now - 4, now]
    _write_segments(camera_dir, ends)
    # Only the first segment probes; the rest fail.
    _fake_ffprobe(monkeypatch, {'segment-000.mp4': '6.000000'})

    starts = [start for _, start, _ in _refined(service, camera_dir, now - 20, now)]
    assert starts[0] == pytest.approx(ends[0] - 6.0, abs=0.01)
    # Contiguous within 1.5x nominal: chained to the previous segment's end.
    assert starts[1] == pytest.approx(ends[0], abs=0.01)
    assert starts[2] == pytest.approx(ends[1], abs=0.01)


@pytest.mark.parametrize('bad_value', ['0.000000', '-1', '600.0', 'N/A'])
def test_implausible_probe_results_keep_the_estimate(tmp_path, monkeypatch, bad_value):
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    now = time.time()
    _write_segments(camera_dir, [now])
    _fake_ffprobe(monkeypatch, {'segment-000.mp4': bad_value})

    _, start, end = _refined(service, camera_dir, now - 10, now)[0]
    assert end - start == pytest.approx(service.PREBUFFER_SEGMENT_SECONDS, abs=0.01)


def test_cold_scan_probes_only_segments_that_can_overlap_the_window(tmp_path, monkeypatch):
    """A long buffer (max_clip_seconds allows an hour) must not cost one ffprobe
    per retained segment: only segments that could overlap the window are probed."""
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    now = time.time()
    ends = [now - 6 * (199 - i) for i in range(200)]  # ~20 minutes of 6s segments
    _write_segments(camera_dir, ends)
    calls: list[str] = []
    _fake_ffprobe(monkeypatch, {f'segment-{i:03d}.mp4': '6.000000' for i in range(200)}, calls)

    segments, content_start = service._collect_prebuffer_segments('cam', now - 40, now - 10)
    # Window [now-40, now-10]: segments ending in (now-40, now-10+60) - the rest
    # end before the window (cannot overlap) and are never probed.
    assert len(calls) == sum(1 for end in ends if now - 40 < end < now + 50)
    assert len(calls) <= 15
    assert content_start == pytest.approx(min(e for e in ends if e > now - 40) - 6.0, abs=0.01)
    assert len(segments) == 6


def test_probe_is_cached_per_file_identity_and_pruned(tmp_path, monkeypatch):
    """A finished segment is probed once for its life in the buffer; a segment
    that grows or closes after the (memoised) timeline was built is re-probed,
    not read from a stale entry; deleted segments are forgotten."""
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    now = time.time()
    paths = _write_segments(camera_dir, [now - 6, now])
    calls: list[str] = []
    _fake_ffprobe(monkeypatch, {p.name: '6.000000' for p in paths}, calls)

    service._collect_prebuffer_segments('cam', now - 20, now)
    service._collect_prebuffer_segments('cam', now - 20, now)
    assert sorted(calls) == ['segment-000.mp4', 'segment-001.mp4']

    # The newest segment grows (a fragment landed) WITHOUT invalidating the
    # memoised timeline (a file write does not change the directory mtime):
    # only it is re-probed, and its fresh end is used.
    paths[1].write_bytes(b'segment-with-a-fragment')
    os.utime(paths[1], (now + 1, now + 1))
    calls.clear()
    refined = service._probe_window_segment_starts(
        camera_dir,
        service._segment_timeline(camera_dir, service.PREBUFFER_SEGMENT_GLOB, service.PREBUFFER_SEGMENT_SECONDS),
        now - 20, now + 1,
    )
    assert calls == ['segment-001.mp4']
    assert refined[1][2] == pytest.approx(now + 1, abs=0.01)
    assert refined[1][1] == pytest.approx(now + 1 - 6.0, abs=0.01)

    paths[0].unlink()
    service._invalidate_segment_timeline_cache()
    service._collect_prebuffer_segments('cam', now - 20, now + 1)
    assert str(paths[0]) not in service._segment_duration_cache
    assert str(paths[1]) in service._segment_duration_cache


def test_segment_timeline_itself_never_probes(tmp_path, monkeypatch):
    """The memoised timeline stays a cheap stat-only estimate (also used for
    audio); probing happens only for the window being rendered."""
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    _write_segments(camera_dir, [time.time()])
    calls: list[str] = []
    _fake_ffprobe(monkeypatch, {'segment-000.mp4': '6.000000'}, calls)

    service._segment_timeline(camera_dir, service.PREBUFFER_SEGMENT_GLOB, service.PREBUFFER_SEGMENT_SECONDS)
    assert calls == []


def test_render_of_six_second_keyframe_segments_keeps_full_window(tmp_path, monkeypatch):
    """End to end with real ffmpeg: segments written exactly like the ingest
    (4s segment_time, fragmented MP4, -c copy) from a 6s-keyframe source come
    out 6s long; the rendered clip must cover the full requested window instead
    of a compressed, truncated one.

    CI installs ffmpeg for this test, so there a missing binary is a failure,
    not a skip - otherwise the one test guarding this regression could go
    silently dead. Local runs without ffmpeg skip."""
    if not (shutil.which('ffmpeg') and shutil.which('ffprobe')):
        if os.environ.get('CI'):
            pytest.fail('ffmpeg/ffprobe must be installed in CI for this regression test')
        pytest.skip('needs real ffmpeg/ffprobe')
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


def test_rejected_probe_is_logged_once_with_name_and_estimate(tmp_path, monkeypatch, caplog):
    """Falling back to the mtime estimate is falling back to logic known to be
    wrong for some cameras, so a rejected probe must not be silent. It is
    logged once per file, not on every clip that includes the segment."""
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    now = time.time()
    _write_segments(camera_dir, [now - 4, now])
    _fake_ffprobe(monkeypatch, {'segment-000.mp4': '600.0', 'segment-001.mp4': '6.0'})

    with caplog.at_level('DEBUG', logger='daygle.ai'):
        _refined(service, camera_dir, now - 10, now)
        _refined(service, camera_dir, now - 10, now)

    warnings = [r.getMessage() for r in caplog.records if r.levelname == 'WARNING']
    assert len(warnings) == 1, warnings
    assert 'segment-000.mp4' in warnings[0]
    assert '600.00s' in warnings[0]
    assert 'estimated 4.00s' in warnings[0]


def test_unreadable_segment_warns_unless_it_is_still_being_written(tmp_path, monkeypatch, caplog):
    """The newest segment is normally unreadable until its single fragment
    lands (DEBUG); a closed segment ffprobe cannot read is a real problem."""
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    now = time.time()
    _write_segments(camera_dir, [now - 8, now - 4, now])
    # segment-001 (closed) and segment-002 (newest) both fail to probe.
    _fake_ffprobe(monkeypatch, {'segment-000.mp4': '4.0'})

    with caplog.at_level('DEBUG', logger='daygle.ai'):
        _refined(service, camera_dir, now - 20, now)

    by_level = {
        level: [r.getMessage() for r in caplog.records if r.levelname == level]
        for level in ('WARNING', 'DEBUG')
    }
    assert len(by_level['WARNING']) == 1, by_level
    assert 'segment-001.mp4 could not be probed' in by_level['WARNING'][0]
    assert any('segment-002.mp4' in m and 'still being written' in m for m in by_level['DEBUG']), by_level


def test_empty_leftover_segment_is_skipped_quietly(tmp_path, monkeypatch, caplog):
    """A camera reconnect leaves the segment being written as just its empty
    moov header: no footage. It is dropped from the clip and logged at DEBUG,
    not WARNING."""
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    now = time.time()
    # segment-001 is the ~1 KB header an interrupted ingest leaves behind;
    # segment-003 is the newest (still being written).
    _write_segments(camera_dir, [now - 12, now - 8, now - 4, now], sizes={1: 1200, 3: 900})
    _fake_ffprobe(monkeypatch, {'segment-000.mp4': '4.0', 'segment-002.mp4': '4.0'})

    with caplog.at_level('DEBUG', logger='daygle.ai'):
        refined = _refined(service, camera_dir, now - 30, now)

    assert [segment.name for segment, _, _ in refined] == ['segment-000.mp4', 'segment-002.mp4', 'segment-003.mp4']
    assert not [r for r in caplog.records if r.levelname == 'WARNING']
    assert any('segment-001.mp4 has no footage' in r.getMessage() for r in caplog.records if r.levelname == 'DEBUG')


def test_zero_byte_closed_segment_is_skipped(tmp_path, monkeypatch):
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    now = time.time()
    _write_segments(camera_dir, [now - 8, now - 4, now], sizes={1: 0})
    _fake_ffprobe(monkeypatch, {'segment-000.mp4': '4.0', 'segment-002.mp4': '4.0'})
    assert [segment.name for segment, _, _ in _refined(service, camera_dir, now - 20, now)] == ['segment-000.mp4', 'segment-002.mp4']


def test_nothing_is_skipped_without_ffprobe(tmp_path, monkeypatch):
    """Without ffprobe every probe fails, which says nothing about a file, so
    even a small closed segment keeps its place in the clip."""
    service = _service(tmp_path)
    camera_dir = service.prebuffer_dir / 'cam'
    now = time.time()
    _write_segments(camera_dir, [now - 8, now - 4, now], sizes={1: 1200})
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: None)
    assert len(_refined(service, camera_dir, now - 20, now)) == 3
