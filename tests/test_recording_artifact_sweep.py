"""Tests for the start-up sweep of orphaned recording artifacts.

The pipeline writes render/transcode intermediates beside the final clip and
renames or deletes them on success, and registers continuous chunks through a
callback as ffmpeg closes them. A crash between those steps leaves a temp file
or an unregistered chunk behind forever, because retention is row-driven. The
sweep reclaims those, and must never touch referenced footage, young files or
the artifacts that are meant to stay (playback sidecars, failure markers).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from app.database import EventDatabase
from app.recording_files import (
    ORPHAN_ARTIFACT_MIN_AGE_SECONDS,
    sweep_orphaned_recording_artifacts,
)

_OLD = time.time() - ORPHAN_ARTIFACT_MIN_AGE_SECONDS - 60


def _write(path: Path, *, old: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x' * 64)
    if old:
        os.utime(path, (_OLD, _OLD))
    return path


def _recording_row(database: EventDatabase, path: Path) -> None:
    database.add_recording(
        event_id=None,
        camera_id='front',
        started_at='2026-06-06T00:00:00+00:00',
        ended_at='2026-06-06T00:00:05+00:00',
        duration_seconds=5,
        file_path=str(path),
        thumbnail_path=None,
        source='camera',
        created_at='2026-06-06T00:00:00+00:00',
    )


def test_sweep_removes_stale_temps_and_orphans_but_keeps_live_media(tmp_path):
    root = tmp_path / 'recordings'
    database = EventDatabase(str(tmp_path / 'sweep.sqlite3'))

    clip = _write(root / 'front' / 'clip.mp4')
    _recording_row(database, clip)
    referenced_chunk = _write(
        root / 'continuous-front' / 'continuous_front_20260101T000000.mp4', old=True
    )
    _recording_row(database, referenced_chunk)

    # Intermediates the pipeline renames or deletes on success.
    stale_temps = [
        _write(root / 'front' / 'clip.recording.tmp.mp4', old=True),
        _write(root / 'front' / 'clip.prebuffer.tmp.mp4', old=True),
        _write(root / 'front' / 'clip.concat.txt', old=True),
        _write(root / 'front' / 'clip.audio.mp4', old=True),
    ]
    young_temp = _write(root / 'front' / 'next.recording.tmp.mp4')
    # Kept on purpose: the browser-playback copy and the failed-conversion
    # retry marker are real artifacts, not intermediates.
    playback_copy = _write(root / 'front' / 'clip.h264-audio.mp4', old=True)
    failure_marker = _write(root / 'front' / 'clip.playback.failed', old=True)

    orphan_chunk = _write(
        root / 'continuous-front' / 'continuous_front_20260101T010000.mp4', old=True
    )
    young_chunk = _write(
        root / 'continuous-front' / 'continuous_front_20260102T000000.mp4'
    )

    # Rolling ingest state from a previous session.
    stale_segment = _write(root / '.prebuffer' / 'front' / 'segment-000.mp4', old=True)
    live_segment = _write(root / '.prebuffer' / 'front' / 'segment-001.mp4')
    stale_audio = _write(root / '.audio' / 'front' / 'aud-00.wav', old=True)
    live_audio = _write(root / '.audio' / 'front' / 'aud-01.wav')
    staging = root / '.prebuffer' / 'daygle-audio-mux-abandoned1'
    _write(staging / 'assembled.wav')
    os.utime(staging, (_OLD, _OLD))
    # Detection frames are a live fallback for offline cameras, not leftovers.
    frame = _write(root / '.frames' / 'front' / 'frame-000.jpg', old=True)

    report = sweep_orphaned_recording_artifacts(
        root, referenced_paths=database.recording_file_paths()
    )

    for removed in [*stale_temps, orphan_chunk, stale_segment, stale_audio]:
        assert not removed.exists(), removed
    assert not staging.exists()
    for kept in [
        clip,
        referenced_chunk,
        young_temp,
        playback_copy,
        failure_marker,
        young_chunk,
        live_segment,
        live_audio,
        frame,
    ]:
        assert kept.exists(), kept
    assert report['files_deleted'] == 7
    assert report['temp_files'] == 4
    assert report['orphaned_chunks'] == 1
    assert report['ingest_segments'] == 2
    assert report['staging_dirs'] == 1
    assert report['bytes_deleted'] == 7 * 64


def test_lifespan_sweeps_before_the_ingest_workers_start(tmp_path, monkeypatch):
    import asyncio

    import app.main as main
    import app.state as state

    root = tmp_path / 'recordings'
    orphan = _write(
        root / 'continuous-front' / 'continuous_front_20260101T000000.mp4', old=True
    )
    order: list[str] = []

    class FakeDatabase:
        def cleanup_incomplete_recordings(self):
            return []

        def recording_file_paths(self):
            return set()

        def start_maintenance(self, callback=None):
            return None

        def stop_maintenance(self):
            return None

    class FakeRecordingService:
        recordings_dir = root

        def stop_prebuffer_workers(self):
            return None

        def stop_all_continuous_recordings(self):
            return None

    class FakeTunnel:
        autostart = False

        def stop(self):
            return None

    monkeypatch.setattr(main, '_startup', lambda: None)
    monkeypatch.setattr(state, 'database', FakeDatabase())
    monkeypatch.setattr(state, 'recording_service', FakeRecordingService())
    monkeypatch.setattr(state, 'cloudflare_tunnel_manager', FakeTunnel())
    monkeypatch.setattr(state, 'auth', None)
    monkeypatch.setattr(main, 'log_detector_initialization', lambda: None)
    monkeypatch.setattr(main, 'start_profile_monitor', lambda: None)
    monkeypatch.setattr(main, 'stop_live_alert_monitor', lambda: None)
    monkeypatch.setattr(main, 'stop_profile_monitor', lambda: None)
    monkeypatch.setattr(main, 'stop_sound_monitor', lambda: None)
    monkeypatch.setattr(main, 'apply_sound_settings', lambda: None)
    monkeypatch.setattr(main, 'shutdown_pools', lambda timeout=None: None)

    def start_monitors() -> None:
        # The first ingest/continuous worker starts here, so every leftover
        # must already be gone - otherwise a stale chunk and a live one could
        # be confused while the sweep walks the tree.
        assert not orphan.exists(), 'the sweep must run before the monitors start'
        order.append('monitor')

    monkeypatch.setattr(main, 'start_live_alert_monitor', start_monitors)

    async def run() -> None:
        async with main.app_lifespan(main.app):
            order.append('body')

    asyncio.run(run())

    assert not orphan.exists()
    assert order == ['monitor', 'body']


def test_sweep_is_a_noop_without_a_recordings_tree(tmp_path):
    report = sweep_orphaned_recording_artifacts(
        tmp_path / 'missing', referenced_paths=set()
    )

    assert report['files_deleted'] == 0
    assert report['bytes_deleted'] == 0
