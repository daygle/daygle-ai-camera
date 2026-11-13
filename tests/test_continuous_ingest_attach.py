"""Regression test for handing continuous recording to the shared ingest.

``_attach_continuous_to_ingest`` used to publish the shared-ingest request
before stopping the camera's dedicated chunk recorder. The ingest notices the
request on its next tick, so its new ffmpeg could start writing chunks in
``continuous-<key>/`` while the dedicated recorder was still in its
several-second SIGTERM teardown - two recorders in one directory, the overlap
the rest of the service joins threads to prevent.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.recordings import RecordingService  # noqa: E402


def _service(tmp_path: Path) -> RecordingService:
    return RecordingService(
        {'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}}
    )


def _start_worker() -> tuple[threading.Thread, threading.Event]:
    started = threading.Event()
    stop_event = threading.Event()

    def run():
        started.set()
        stop_event.wait(5)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(5)
    return thread, stop_event


def test_attach_stops_the_dedicated_recorder_before_the_ingest_can_start(tmp_path, monkeypatch):
    service = _service(tmp_path)
    ingest_thread, ingest_stop = _start_worker()
    service._prebuffer_workers['cam'] = {
        'thread': ingest_thread,
        'stop_event': ingest_stop,
        'stream_url': 'rtsp://cam/stream',
        'buffer_seconds': 15,
        'camera_id': 'cam',
    }
    dedicated_thread, dedicated_stop = _start_worker()
    service._continuous_workers['cam'] = {
        'thread': dedicated_thread,
        'stop_event': dedicated_stop,
        'stream_url': 'rtsp://cam/stream',
        'chunk_seconds': 60,
        'camera_id': 'cam',
    }
    published_at_stop = []
    real_stop = RecordingService._stop_worker

    def spy(worker, join_timeout=None):
        published_at_stop.append('cam' in service._shared_continuous)
        real_stop(worker, join_timeout=join_timeout)

    monkeypatch.setattr(RecordingService, '_stop_worker', staticmethod(spy))

    attached = service._attach_continuous_to_ingest(
        'cam', 'rtsp://cam/stream', tmp_path / 'chunks', 60, None
    )

    assert attached is True
    # The ingest must not see the request while the dedicated ffmpeg is still
    # shutting down.
    assert published_at_stop == [False]
    assert service._shared_continuous['cam'] == {
        'chunks_dir': tmp_path / 'chunks',
        'chunk_seconds': 60,
        'on_chunk_complete': None,
    }
    assert not dedicated_thread.is_alive()
    ingest_stop.set()
    ingest_thread.join(5)
