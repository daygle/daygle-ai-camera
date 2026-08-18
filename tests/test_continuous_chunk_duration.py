"""Regression tests for continuous-recording chunk duration + start time.

The continuous chunker names each segment file with ffmpeg's ``-strftime 1``,
which uses the host's LOCAL wall clock. The registration callback used to
(1) parse that filename AS UTC and (2) derive the chunk duration from
``file_mtime_utc - filename_time``. On any non-UTC host those two offsets
fought each other: a real one-hour chunk on a UTC+1 host computed to
``3600 - 3600 = 0`` seconds and floored to the 1.0s minimum, and the stored
``started_at`` was shifted by the tz offset (mis-placing the clip on the
timeline and reading as a future "Just now").

The fix: interpret the filename as local time (converted to UTC) and take the
duration from the container itself via ffprobe, anchoring ``ended_at`` to
``started_at + duration``.
"""
import os
import time
from datetime import datetime, timezone

import pytest

from tests.support import *  # noqa: F401,F403 - shared harness + stdlib re-exports

from app.recording_extension import _make_continuous_chunk_callback, _parse_chunk_start_time


def test_parse_chunk_start_time_interprets_filename_as_local_tz():
    # ffmpeg writes the segment filename in local time; the parser must convert
    # local -> UTC rather than stamping the wall-clock value as UTC. Restore the
    # process timezone (env AND libc's cached tz via tzset) afterwards so later
    # tests that read local time are unaffected.
    if not hasattr(time, 'tzset'):
        pytest.skip('tzset is only available on Unix')
    original_tz = os.environ.get('TZ')
    os.environ['TZ'] = 'Etc/GMT-1'  # POSIX sign is inverted: GMT-1 == UTC+1
    time.tzset()
    try:
        parsed = _parse_chunk_start_time(Path('continuous_camera-2_20260818T174900.mp4'))
        # Local 17:49 in a UTC+1 zone is 16:49 UTC.
        assert parsed == datetime(2026, 8, 18, 16, 49, 0, tzinfo=timezone.utc)
    finally:
        if original_tz is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = original_tz
        time.tzset()


def test_parse_chunk_start_time_rejects_unparseable_names():
    assert _parse_chunk_start_time(Path('not-a-chunk.mp4')) is None
    assert _parse_chunk_start_time(Path('continuous_camera-2_notatimestamp.mp4')) is None


def _stub_chunk_callback_deps(monkeypatch, *, probed):
    """Neutralise the callback's side effects so the test isolates the
    duration/timestamp math, and force ffprobe to return ``probed``."""
    import app.recording_extension as rext
    import app.detection_state as ds
    import app.backup as backup
    monkeypatch.setattr(rext, 'probe_video_duration', lambda _p: probed)
    monkeypatch.setattr(rext, 'write_live_history_detection_track', lambda *a, **k: True)
    monkeypatch.setattr(ds, 'build_track_from_live_history', lambda *a, **k: [])
    monkeypatch.setattr(backup, 'purge_recordings_by_policy', lambda *a, **k: None)


def _write_segment(tmp_path, name='continuous_camera-2_20260818T174900.mp4'):
    chunks_dir = tmp_path / 'recordings' / 'continuous-camera-2'
    chunks_dir.mkdir(parents=True, exist_ok=True)
    seg = chunks_dir / name
    seg.write_bytes(b'\x00' * 2048)
    return seg


def test_continuous_chunk_records_true_media_duration(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main

    _stub_chunk_callback_deps(monkeypatch, probed=3600.0)
    seg = _write_segment(tmp_path)

    _make_continuous_chunk_callback('camera-2')('camera-2', seg)

    recs = main.database.list_recordings(camera_id='camera-2')
    assert len(recs) == 1
    rec = recs[0]
    # The whole point: an hour-long chunk stores ~3600s, never the 1.0s floor.
    assert rec['duration_seconds'] == pytest.approx(3600.0)
    assert rec['trigger_type'] == 'continuous'
    # ended_at is anchored to started_at + duration so the two never disagree.
    started = datetime.fromisoformat(rec['started_at'])
    ended = datetime.fromisoformat(rec['ended_at'])
    assert (ended - started).total_seconds() == pytest.approx(3600.0)


def test_continuous_chunk_falls_back_to_wallclock_when_probe_fails(tmp_path, monkeypatch):
    _load_app(tmp_path, monkeypatch)
    import app.main as main

    # ffprobe unavailable / failed -> None; the callback falls back to the
    # start-filename vs mtime gap (floored at 1.0s), still a positive duration.
    _stub_chunk_callback_deps(monkeypatch, probed=None)
    seg = _write_segment(tmp_path)

    _make_continuous_chunk_callback('camera-2')('camera-2', seg)

    recs = main.database.list_recordings(camera_id='camera-2')
    assert len(recs) == 1
    assert recs[0]['duration_seconds'] >= 1.0
