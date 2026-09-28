"""Regression tests for the object-detection and recordings audit fixes.

* Lowering the inference scheduler's worker limit retires surplus workers.
* The continuous chunk recorder restarts a stalled ffmpeg instead of waiting
  on a dead stream forever.
* A clip whose render could not be queued no longer leaves an active capture
  session behind (which linked every later event on the camera to a clip-less
  recording and blocked new recordings until a restart).
* A capture session long past its hard deadline is retired, not extended.
* The storage cap counts browser-playback transcodes.
* Snapshot filenames are unique even within the same microsecond.
* The face detector does not inherit the primary model's ``nms_free`` flag,
  and a string ``"false"`` ``nms_free`` is not read as True.
"""
from __future__ import annotations

import importlib
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.inference_scheduler import LiveInferenceScheduler


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def test_scheduler_retires_surplus_workers_when_limit_shrinks():
    scheduler = LiveInferenceScheduler(lambda *_args: None, max_workers=4)
    scheduler.start()
    try:
        assert _wait_for(lambda: sum(t.is_alive() for t in scheduler._threads) == 4)
        scheduler.set_max_workers(1)
        assert _wait_for(lambda: sum(t.is_alive() for t in scheduler._threads) == 1)
        # Still serves work with the reduced pool.
        ran = threading.Event()
        scheduler.submit('cam', {}, lambda: ('img', {}), runner=lambda *_a: ran.set())
        assert ran.wait(2)
    finally:
        scheduler.stop()


def test_continuous_recorder_restarts_a_stalled_ffmpeg(tmp_path, monkeypatch):
    recordings_module = importlib.import_module('app.recordings')
    service = recordings_module.RecordingService(
        {'storage': {'recordings_dir': str(tmp_path / 'rec')}, 'recording': {}}
    )
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(service, 'CONTINUOUS_STALL_SECONDS', 0.05)
    monkeypatch.setattr(service, 'CONTINUOUS_POLL_SECONDS', 0.005)
    monkeypatch.setattr(service, 'PREBUFFER_RECONNECT_BACKOFF_BASE_SECONDS', 0.01)
    camera_key = recordings_module.RecordingService._camera_key('cam')
    chunks_dir = service.recordings_dir / f'continuous-{camera_key}'
    chunks_dir.mkdir(parents=True, exist_ok=True)
    # A chunk whose size never changes: the stream has gone quiet.
    (chunks_dir / f'continuous_{camera_key}_20260101T000000.mp4').write_bytes(b'x')

    launched: list[object] = []
    stop_event = threading.Event()

    class HungProc:
        def __init__(self) -> None:
            self.returncode = None
            launched.append(self)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):  # noqa: ARG002 - mock
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(recordings_module.subprocess, 'Popen', lambda cmd, **_kw: HungProc())
    worker = threading.Thread(
        target=service._run_continuous_chunk_worker,
        args=(camera_key, 'rtsp://example/stream', chunks_dir, 60, None, stop_event),
        daemon=True,
    )
    worker.start()
    try:
        # Without stall detection the first process would run forever.
        assert _wait_for(lambda: len(launched) >= 2)
        assert launched[0].returncode is not None
    finally:
        stop_event.set()
        worker.join(timeout=5)
    assert not worker.is_alive()


class _FakeDatabase:
    def __init__(self) -> None:
        self.deleted: list[int] = []

    def delete_recording(self, recording_id: int):
        self.deleted.append(recording_id)
        return {'id': recording_id}


def test_unqueued_clip_releases_capture_session_and_row(tmp_path, monkeypatch):
    recording_extension = importlib.import_module('app.recording_extension')
    postprocess_pool = importlib.import_module('app.postprocess_pool')
    state = importlib.import_module('app.state')

    class _FullPool:
        def submit(self, *_args, **_kwargs):
            return False

    monkeypatch.setattr(postprocess_pool, 'clip_pool', lambda: _FullPool())
    database = _FakeDatabase()
    monkeypatch.setattr(state, 'database', database)
    monkeypatch.setattr(state, 'active_rtsp_recordings', {})
    monkeypatch.setattr(state, 'last_rtsp_capture_end', {})

    recording_extension.start_rtsp_recording_capture(
        'rtsp://example/stream',
        {'file_path': str(tmp_path / 'clip.mp4'), 'duration_seconds': 5},
        11,
        [],
        recording_id=7,
        camera_id='cam',
        # In the past, so the capture deadline has already elapsed.
        event_time=(datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat(),
        recording_config={'pre_event_seconds': 0, 'post_event_seconds': 5, 'max_clip_seconds': 60},
    )

    assert _wait_for(lambda: database.deleted == [7])
    assert 'cam' not in state.active_rtsp_recordings


def test_waiting_clip_does_not_hold_a_render_worker(tmp_path, monkeypatch):
    """A clip still inside its post-event window must not occupy a pool worker.

    Only the render is submitted to the bounded clip pool, and only once the
    capture deadline has passed.
    """
    recording_extension = importlib.import_module('app.recording_extension')
    postprocess_pool = importlib.import_module('app.postprocess_pool')
    state = importlib.import_module('app.state')

    submitted: list[str] = []

    class _RecordingPool:
        def submit(self, _fn, *_args, label='', **_kwargs):
            submitted.append(label)
            return True

    monkeypatch.setattr(postprocess_pool, 'clip_pool', lambda: _RecordingPool())
    monkeypatch.setattr(state, 'active_rtsp_recordings', {})
    monkeypatch.setattr(state, 'last_rtsp_capture_end', {})

    recording_extension.start_rtsp_recording_capture(
        'rtsp://example/stream',
        {'file_path': str(tmp_path / 'clip.mp4'), 'duration_seconds': 5},
        12,
        [],
        recording_id=8,
        camera_id='cam',
        event_time=datetime.now(timezone.utc).isoformat(),
        recording_config={'pre_event_seconds': 0, 'post_event_seconds': 1, 'max_clip_seconds': 60},
    )

    time.sleep(0.3)
    assert submitted == []  # still inside the post-event window
    assert _wait_for(lambda: submitted == ['rtsp-recording-12'], timeout=5)


def test_extend_retires_a_capture_session_long_past_its_deadline(monkeypatch):
    recording_extension = importlib.import_module('app.recording_extension')
    state = importlib.import_module('app.state')
    long_ago = time.time() - recording_extension.STALE_CAPTURE_SESSION_GRACE_SECONDS - 60
    monkeypatch.setattr(state, 'active_rtsp_recordings', {
        'cam': {
            'recording_id': 7,
            'start_capture_ts': long_ago - 60,
            'capture_deadline_ts': long_ago,
            'max_capture_deadline_ts': long_ago,
        },
    })

    extended = recording_extension.extend_active_rtsp_recording(
        camera_id='cam',
        event_time=datetime.now(timezone.utc).isoformat(),
        recording_config={'post_event_seconds': 10},
    )

    assert extended is None
    assert 'cam' not in state.active_rtsp_recordings


def test_storage_cap_counts_playback_transcode(tmp_path, monkeypatch):
    database_module = importlib.import_module('app.database')
    recordings_repo = importlib.import_module('app.db.recordings')
    media_utils = importlib.import_module('app.media_utils')
    database = database_module.EventDatabase(str(tmp_path / 'cap.sqlite3'))
    recordings_dir = tmp_path / 'rec'
    recordings_dir.mkdir()
    monkeypatch.setattr(
        recordings_repo, 'safe_storage_path',
        lambda raw, **_kw: Path(str(raw)) if raw else None,
    )

    ids = []
    for day, name in (('2026-09-01', 'old.mp4'), ('2026-09-02', 'new.mp4')):
        clip = recordings_dir / name
        clip.write_bytes(b'v' * 60)
        # An HEVC camera's browser-playback copy doubles the footprint.
        media_utils.recording_playback_sidecar_path(clip).write_bytes(b'p' * 60)
        started_at = f'{day}T00:00:00+00:00'
        ids.append(database.add_recording(
            event_id=None, camera_id='cam', started_at=started_at, ended_at=started_at,
            duration_seconds=1, file_path=str(clip), thumbnail_path=None,
            source='camera', created_at=started_at,
        ))

    # 60B + 60B per clip: the newest clip alone fits a 150B cap, both do not.
    purged = database.purge_recordings(max_storage_bytes=150)

    assert [row['id'] for row in purged] == [ids[0]]


def test_snapshot_filenames_are_unique_within_one_instant(tmp_path, monkeypatch):
    storage_module = importlib.import_module('app.storage')
    frozen = datetime(2026, 9, 28, 12, 0, 0, 123456, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 - fixed instant
            return frozen

    monkeypatch.setattr(storage_module, 'datetime', _FrozenDatetime)
    storage = storage_module.Storage({'storage': {
        'data_dir': str(tmp_path), 'snapshots_dir': str(tmp_path / 'snaps'),
        'events_dir': str(tmp_path / 'events'), 'recordings_dir': str(tmp_path / 'rec'),
    }})

    first = storage.save_image_snapshot(b'one', 'cam-a.jpg')
    second = storage.save_image_snapshot(b'two', 'cam-b.jpg')

    assert first != second
    assert Path(first).read_bytes() == b'one'
    assert Path(second).read_bytes() == b'two'
    assert Path(first).name.startswith('20260928_120000_123456_')


def test_face_detector_ignores_primary_nms_free_flag(tmp_path, monkeypatch):
    detector_module = importlib.import_module('app.detector')
    face_model = tmp_path / 'yolov8n-face.onnx'
    face_model.write_bytes(b'not a real model')
    captured: dict = {}

    class _Recorder:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(detector_module, 'OnnxYoloDetector', _Recorder)
    detector_module.create_face_detector({
        'face_enabled': True,
        'face_model_path': str(face_model),
        'model_path': 'models/yolo26n.onnx',
        'nms_free': True,  # describes the PRIMARY model
    })
    assert captured['nms_free'] is False

    detector_module.create_detector({'model_path': 'models/yolo11n.onnx', 'nms_free': 'false'})
    assert captured['nms_free'] is False
