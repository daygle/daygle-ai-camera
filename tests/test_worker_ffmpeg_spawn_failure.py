"""Regression tests for ingest workers surviving ffmpeg spawn failures.

CI caught this: a test stubbed ``shutil.which`` to a path that does not
exist, the worker's pre-loop guard passed, and ``subprocess.Popen`` raised
``FileNotFoundError`` *outside* the loop's try/finally -- killing the worker
thread with an unhandled exception. In production the same window opens when
the ffmpeg binary is removed or upgraded while a camera is ingesting.

The workers must treat a failed spawn like any other dead link: log, back
off, and keep looping until asked to stop.
"""

from __future__ import annotations

import importlib
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

recordings_module = importlib.import_module('app.recordings')  # noqa: E402


def _raise_spawn_error(*_args, **_kwargs):
    raise FileNotFoundError(2, 'No such file or directory', '/nonexistent/ffmpeg')


def _service(tmp_path: Path):
    return recordings_module.RecordingService(
        {'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}}
    )


def _run_worker(target, stop_event: threading.Event) -> list[BaseException]:
    """Run a worker target in a thread, stopping it shortly after start.

    Returns exceptions captured from the thread via ``threading.excepthook``
    so the test fails if the worker died with an unhandled error.
    """
    captured: list[BaseException] = []
    original_hook = threading.excepthook
    threading.excepthook = lambda args: captured.append(args.exc_value)
    try:
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        assert not stop_event.wait(2.0), 'worker exited before being asked to stop'
        # Give the loop time to attempt at least one spawn.
        thread.join(timeout=0.5)
        stop_event.set()
        thread.join(timeout=10)
        assert not thread.is_alive(), 'worker did not stop when requested'
    finally:
        threading.excepthook = original_hook
    return captured


def test_prebuffer_worker_survives_ffmpeg_spawn_failure(tmp_path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/nonexistent/ffmpeg')

    attempts: list[int] = []

    def _raise(*_args, **_kwargs):
        attempts.append(1)
        _raise_spawn_error()

    monkeypatch.setattr(recordings_module.subprocess, 'Popen', _raise)

    stop_event = threading.Event()

    def target():
        service._run_prebuffer_worker(
            'cam',
            'rtsp://example/stream',
            {'stop_event': stop_event, 'buffer_seconds': 15},
        )

    captured = _run_worker(target, stop_event)
    assert not captured, f'worker thread died with: {captured!r}'
    assert len(attempts) >= 1  # it kept retrying rather than crashing out


def test_rec_prebuffer_worker_survives_ffmpeg_spawn_failure(tmp_path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/nonexistent/ffmpeg')
    monkeypatch.setattr(
        recordings_module.subprocess,
        'Popen',
        _raise_spawn_error,
    )
    stop_event = threading.Event()

    def target():
        service._run_rec_prebuffer_worker(
            'cam-rec-src',
            'rtsp://example/main',
            {'stop_event': stop_event, 'buffer_seconds': 15},
        )

    captured = _run_worker(target, stop_event)
    assert not captured, f'rec worker thread died with: {captured!r}'


def test_continuous_chunk_worker_survives_ffmpeg_spawn_failure(tmp_path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/nonexistent/ffmpeg')
    monkeypatch.setattr(
        recordings_module.subprocess,
        'Popen',
        _raise_spawn_error,
    )
    stop_event = threading.Event()

    def target():
        service._run_continuous_chunk_worker(
            'cam',
            'rtsp://example/stream',
            tmp_path / 'chunks',
            60,
            None,
            stop_event,
        )

    captured = _run_worker(target, stop_event)
    assert not captured, f'continuous worker thread died with: {captured!r}'
