"""Regression tests for the ``pre_event_seconds=0`` RTSP event-capture fix.

The per-camera rolling prebuffer runs continuously for every RTSP camera, so
footage spanning the trigger is always buffered. Previously, a configured
``pre_event_seconds`` of 0 short-circuited ``write_rtsp_clip_with_prebuffer``
straight to a *live* capture -- which starts recording only after the
post-event window elapses, capturing the aftermath rather than the event.

The fix floors the effective pre-roll to ``RTSP_EVENT_MIN_PRE_SECONDS`` and
always renders from the prebuffer, falling back to live capture only when the
buffer genuinely holds no usable segments.
"""

from __future__ import annotations

import importlib
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


def test_pre_zero_consults_prebuffer_at_floor_not_live_capture(tmp_path, monkeypatch):
    """With ``pre_seconds=0`` the render MUST consult the rolling prebuffer for a
    window that begins ``RTSP_EVENT_MIN_PRE_SECONDS`` before the trigger -- not
    short-circuit to a live capture without ever looking at the buffer.

    Regression: before the fix, ``_collect_prebuffer_segments`` was never called
    for ``pre_seconds=0`` (the method returned ``_live_capture`` immediately)."""
    service = _service(tmp_path)
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(service, '_ensure_prebuffer_worker', lambda *a, **k: None)

    collect_calls: list[tuple[float, float]] = []

    def spy_collect(camera_key, start_ts, end_ts):
        collect_calls.append((start_ts, end_ts))
        return [], None  # no usable segments -> graceful fallback below

    monkeypatch.setattr(service, '_collect_prebuffer_segments', spy_collect)

    live_calls: list[tuple] = []

    def spy_live(stream_url, file_path, max_duration_seconds):
        live_calls.append((stream_url, file_path, max_duration_seconds))
        return time.time(), max_duration_seconds

    monkeypatch.setattr(service, '_live_capture', spy_live)

    # Trigger in the past so the post-event wait resolves immediately (no sleep).
    triggered_at = datetime.now(timezone.utc).replace(microsecond=0)
    triggered_ts = triggered_at.timestamp()
    monkeypatch.setattr(recordings_module.time, 'time', lambda: triggered_ts + 100)

    service.write_rtsp_clip_with_prebuffer(
        stream_url='rtsp://cam/stream',
        camera_id='cam-1',
        file_path=tmp_path / 'clip.mp4',
        triggered_at=triggered_at,
        pre_seconds=0,
        post_seconds=5,
        max_duration_seconds=5,
    )

    assert collect_calls, (
        'pre_seconds=0 must still consult the prebuffer; it short-circuited to '
        'live capture without looking at the buffer.'
    )
    first_start, _first_end = collect_calls[0]
    # The pre-roll window starts RTSP_EVENT_MIN_PRE_SECONDS before the trigger.
    assert first_start == pytest.approx(
        triggered_ts - service.RTSP_EVENT_MIN_PRE_SECONDS, abs=0.01
    )


def test_pre_zero_renders_from_buffer_when_segments_exist(tmp_path, monkeypatch):
    """When the prebuffer holds usable segments spanning the trigger, a
    ``pre_seconds=0`` event renders from them and does NOT fall back to a live
    capture (which would record the post-event aftermath)."""
    service = _service(tmp_path)
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(service, '_ensure_prebuffer_worker', lambda *a, **k: None)

    triggered_at = datetime.now(timezone.utc).replace(microsecond=0)
    triggered_ts = triggered_at.timestamp()
    monkeypatch.setattr(recordings_module.time, 'time', lambda: triggered_ts + 100)

    fake_segment = tmp_path / 'segment-x.mp4'
    fake_segment.write_bytes(b'\x00\x01')
    content_start = triggered_ts - service.RTSP_EVENT_MIN_PRE_SECONDS

    monkeypatch.setattr(
        service, '_collect_prebuffer_segments',
        lambda camera_key, start_ts, end_ts: ([fake_segment], content_start),
    )
    monkeypatch.setattr(service, '_prebuffer_segment_durations', lambda camera_key, segments: {})
    monkeypatch.setattr(service, '_mux_prebuffer_audio', lambda *a, **k: False)
    monkeypatch.setattr(service, 'clip_has_video_stream', lambda file_path: True)
    monkeypatch.setattr(service, 'clip_duration_seconds', lambda file_path: 6.0)

    live_calls: list[tuple] = []
    monkeypatch.setattr(
        service, '_live_capture',
        lambda *a, **k: (live_calls.append(a), (time.time(), 5.0))[1],
    )

    class _FakeCompleted:
        returncode = 0
        stderr = ''

    def fake_run(command, *args, **kwargs):
        # ffmpeg render: emit the requested output file so the video checks pass.
        Path(command[-1]).write_bytes(b'\x00\x01\x02\x03')
        return _FakeCompleted()

    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)

    file_path = tmp_path / 'clip.mp4'
    content_start_ts, content_seconds = service.write_rtsp_clip_with_prebuffer(
        stream_url='rtsp://cam/stream',
        camera_id='cam-1',
        file_path=file_path,
        triggered_at=triggered_at,
        pre_seconds=0,
        post_seconds=5,
        max_duration_seconds=5,
    )

    assert not live_calls, 'must render from the prebuffer, not fall back to live capture'
    assert file_path.exists(), 'the rendered clip should be moved into place'
    # Reported window is anchored to where the buffered content actually begins,
    # which is before the trigger (the floored pre-roll).
    assert content_start_ts == pytest.approx(content_start, abs=0.01)
    assert content_seconds > 0


def _render_capturing_diagnostics(tmp_path, monkeypatch, *, pre_seconds, buffered_pre_seconds):
    """Drive ``write_rtsp_clip_with_prebuffer`` with a stubbed prebuffer whose
    oldest segment begins ``buffered_pre_seconds`` before the trigger, capturing
    any diagnostics emitted.

    ``buffered_pre_seconds`` is expressed relative to the trigger the helper
    generates internally, so the shortfall is deterministic regardless of when
    the test runs. Returns the list of ``(event_type, severity, details)``."""
    service = _service(tmp_path)
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(service, '_ensure_prebuffer_worker', lambda *a, **k: None)

    diagnostics: list[tuple[str, str, dict]] = []
    service.diagnostic_callback = (
        lambda camera_id, event_type, message, *, severity='info', details=None:
        diagnostics.append((event_type, severity, details or {}))
    )

    triggered_at = datetime.now(timezone.utc).replace(microsecond=0)
    triggered_ts = triggered_at.timestamp()
    content_start_ts = triggered_ts - buffered_pre_seconds
    monkeypatch.setattr(recordings_module.time, 'time', lambda: triggered_ts + 100)

    fake_segment = tmp_path / 'segment-x.mp4'
    fake_segment.write_bytes(b'\x00\x01')
    monkeypatch.setattr(
        service, '_collect_prebuffer_segments',
        lambda camera_key, start_ts, end_ts: ([fake_segment], content_start_ts),
    )
    monkeypatch.setattr(service, '_prebuffer_segment_durations', lambda camera_key, segments: {})
    monkeypatch.setattr(service, '_mux_prebuffer_audio', lambda *a, **k: False)
    monkeypatch.setattr(service, 'clip_has_video_stream', lambda file_path: True)
    monkeypatch.setattr(service, 'clip_duration_seconds', lambda file_path: 6.0)
    monkeypatch.setattr(service, '_live_capture', lambda *a, **k: (time.time(), 5.0))

    class _FakeCompleted:
        returncode = 0
        stderr = ''

    def fake_run(command, *args, **kwargs):
        Path(command[-1]).write_bytes(b'\x00\x01\x02\x03')
        return _FakeCompleted()

    monkeypatch.setattr(recordings_module.subprocess, 'run', fake_run)

    service.write_rtsp_clip_with_prebuffer(
        stream_url='rtsp://cam/stream',
        camera_id='cam-1',
        file_path=tmp_path / 'clip.mp4',
        triggered_at=triggered_at,
        pre_seconds=pre_seconds,
        post_seconds=5,
        max_duration_seconds=pre_seconds + 5,
    )
    return diagnostics


def test_short_preroll_emits_diagnostic_when_buffer_not_full(tmp_path, monkeypatch):
    """A buffer that holds footage but does not reach back to the requested
    pre-roll start renders a short clip; that shortfall must be surfaced as a
    ``prebuffer_short_preroll`` diagnostic instead of being silent."""
    # Requested 10s of pre-roll, but the oldest buffered segment starts only 5s
    # before the trigger -> a 5s shortfall (well over the jitter tolerance).
    diagnostics = _render_capturing_diagnostics(
        tmp_path, monkeypatch, pre_seconds=10, buffered_pre_seconds=5,
    )
    short = [d for d in diagnostics if d[0] == 'prebuffer_short_preroll']
    assert short, f'expected a prebuffer_short_preroll diagnostic, got {diagnostics}'
    _event_type, severity, details = short[0]
    assert severity == 'warning'
    assert details['reason'] == 'buffer_not_full'
    assert details['requested_pre_seconds'] == 10
    assert details['shortfall_seconds'] == pytest.approx(5.0, abs=0.1)
    assert details['actual_pre_seconds'] == pytest.approx(5.0, abs=0.1)


def test_short_preroll_silent_when_buffer_is_full(tmp_path, monkeypatch):
    """When the buffer reaches back to (or before) the requested pre-roll start,
    no shortfall diagnostic fires -- keyframe lead-in must not trip the check."""
    # Oldest segment starts 11s before the trigger for a 10s request: full
    # pre-roll plus keyframe lead-in, so content_start is BEFORE start_ts.
    diagnostics = _render_capturing_diagnostics(
        tmp_path, monkeypatch, pre_seconds=10, buffered_pre_seconds=11,
    )
    assert not [d for d in diagnostics if d[0] == 'prebuffer_short_preroll'], (
        f'full buffer must not emit a short-preroll diagnostic, got {diagnostics}'
    )
