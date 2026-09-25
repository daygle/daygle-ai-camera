from __future__ import annotations

import importlib
import threading
from datetime import datetime, timezone
from pathlib import Path


def test_stream_url_prefers_configured_url_and_injects_credentials():
    from app.utils import build_stream_url

    settings = {
        'stream_url': 'rtsp://example.test/main?profile=1',
        'username': 'viewer',
        'password': 'secret',
    }

    assert build_stream_url(settings) == 'rtsp://viewer:secret@example.test/main?profile=1'


def test_stream_url_builds_host_based_url_with_encoded_credentials():
    from app.utils import build_stream_url

    settings = {
        'host': 'camera.example.test',
        'port': 554,
        'username': 'user@example',
        'password': 'p@ss word',
    }

    assert build_stream_url(settings) == 'rtsp://user%40example:p%40ss%20word@camera.example.test:554/stream1'


def test_event_clip_uses_the_single_configured_stream(tmp_path, monkeypatch):
    """One stream feeds detection ingest, event clips and live preview: the
    prebuffer worker and the direct-capture fallback must both see exactly the
    URL the camera is configured with."""
    from app.recordings import RecordingService
    recordings_module = importlib.import_module('app.recordings')

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

    stream_url = 'rtsp://camera/mainstream'
    result = service.write_rtsp_clip_with_prebuffer(
        stream_url=stream_url,
        camera_id='front',
        file_path=tmp_path / 'recordings' / 'event.mp4',
        triggered_at=datetime.now(timezone.utc),
        pre_seconds=5,
        post_seconds=10,
        max_duration_seconds=15,
    )

    assert ensured == [stream_url]
    assert captured['stream_url'] == stream_url
    assert result[1] == 1.0


def test_continuous_chunk_command_preserves_source_video(monkeypatch, tmp_path):
    recordings_module = importlib.import_module('app.recordings')
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
