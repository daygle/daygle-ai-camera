from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path


def test_recording_stream_path_replaces_manual_rtsp_url_path():
    from app.utils import build_recording_stream_url

    settings = {
        'stream_url': 'rtsp://viewer:secret@example.test:8554/sub/main?profile=1',
        'recording_stream_path': '/main/high',
    }

    assert build_recording_stream_url(settings) == (
        'rtsp://viewer:secret@example.test:8554/main/high?profile=1'
    )


def test_recording_stream_path_query_overrides_primary_query():
    from app.utils import build_recording_stream_url

    settings = {
        'stream_url': 'rtsp://camera.example/sub?profile=low',
        'recording_stream_path': '/main?profile=high',
    }

    assert build_recording_stream_url(settings) == 'rtsp://camera.example/main?profile=high'


def test_recording_stream_path_builds_host_based_url_with_encoded_credentials():
    from app.utils import build_recording_stream_url

    settings = {
        'host': 'camera.example.test',
        'port': 554,
        'username': 'user@example',
        'password': 'p@ss word',
        'recording_stream_path': 'stream2',
    }

    assert build_recording_stream_url(settings) == (
        'rtsp://user%40example:p%40ss%20word@camera.example.test:554/stream2'
    )


def test_continuous_recording_uses_high_resolution_stream(tmp_path, monkeypatch):
    # The application integration test for this path lives in test_api.py. Keep
    # this focused unit test importable even on developer machines with a broken
    # FastAPI/Pydantic installation by asserting the source-selection contract
    # from the same pure URL helpers used by live_monitor.
    from app.utils import build_recording_stream_url, build_stream_url

    camera = {
        'id': 'front',
        'stream_url': 'rtsp://camera/substream',
        'recording_stream_path': '/mainstream',
    }

    assert build_stream_url(camera) == 'rtsp://camera/substream'
    assert build_recording_stream_url(camera) == 'rtsp://camera/mainstream'


def test_dedicated_recording_stream_does_not_downgrade_to_detection_buffer(tmp_path, monkeypatch):
    from app.recordings import RecordingService
    import app.recordings as recordings_module

    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'recordings')}, 'recording': {}})
    ensured: list[str] = []
    captured: dict[str, str] = {}

    monkeypatch.setattr(service, '_ensure_prebuffer_worker', lambda _key, url, _seconds, **_kwargs: ensured.append(url))
    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')

    def fake_live_capture(stream_url, file_path, _duration):
        captured['stream_url'] = stream_url
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        Path(file_path).write_bytes(b'captured')
        now = datetime.now(timezone.utc).timestamp()
        return now, 1.0

    monkeypatch.setattr(service, '_live_capture', fake_live_capture)

    recording_url = 'rtsp://camera/mainstream'
    detection_url = 'rtsp://camera/substream'
    result = service.write_rtsp_clip_with_prebuffer(
        stream_url=recording_url,
        detection_stream_url=detection_url,
        camera_id='front',
        file_path=tmp_path / 'recordings' / 'event.mp4',
        triggered_at=datetime.now(timezone.utc),
        pre_seconds=5,
        post_seconds=10,
        max_duration_seconds=15,
    )

    assert ensured == [detection_url]
    assert captured['stream_url'] == recording_url
    assert result[1] == 1.0


def test_continuous_chunk_command_preserves_recording_source_video(monkeypatch, tmp_path):
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService({'storage': {'recordings_dir': str(tmp_path / 'recordings')}, 'recording': {}})
    stop_event = threading.Event()
    captured: list[list[str]] = []

    class FakeProcess:
        def poll(self):
            return 0

    def fake_popen(command, **_kwargs):
        captured.append(command)
        stop_event.set()
        return FakeProcess()

    monkeypatch.setattr(recordings_module.shutil, 'which', lambda _name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(recordings_module.subprocess, 'Popen', fake_popen)

    service._run_continuous_chunk_worker(
        'front',
        'rtsp://camera/mainstream',
        Path(tmp_path / 'recordings' / 'continuous-front'),
        60,
        None,
        stop_event,
    )

    command = captured[0]
    assert command[command.index('-i') + 1] == 'rtsp://camera/mainstream'
    assert command[command.index('-c:v') + 1] == 'copy'
    assert command[command.index('-segment_time') + 1] == '60'
