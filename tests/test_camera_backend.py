"""Regression tests for camera source-FPS reporting."""

from __future__ import annotations

import subprocess

from app.camera_backend import OpenCvStreamCamera


def test_effective_fps_prefers_configured_override() -> None:
    camera = OpenCvStreamCamera('rtsp://example/stream', fps=10)
    camera.detected_fps = 25.0

    assert camera.effective_fps == 10.0
    assert camera.fps_source == 'configured'
    assert camera.get_frame()['fps'] == 10.0
    assert camera.get_frame()['effective_fps'] == 10.0


def test_effective_fps_uses_sane_detected_rate() -> None:
    camera = OpenCvStreamCamera('rtsp://example/stream', fps=None)
    camera.detected_fps = 25.0

    frame = camera.get_frame()

    assert frame['configured_fps'] is None
    assert frame['detected_fps'] == 25.0
    assert frame['effective_fps'] == 25.0
    assert frame['fps_source'] == 'detected'
    assert frame['fps'] == 25.0


def test_effective_fps_keeps_fallback_for_invalid_metadata() -> None:
    camera = OpenCvStreamCamera('rtsp://example/stream', fps=None)
    camera.detected_fps = 90000.0

    assert camera.effective_fps == 15.0
    assert camera.fps_source == 'fallback'


def test_get_frame_probes_declared_rtsp_rate_once_and_parses_fraction(monkeypatch) -> None:
    camera = OpenCvStreamCamera('rtsp://user:secret@example/stream', fps=None)
    calls: list[list[str]] = []

    monkeypatch.setattr('app.camera_backend.shutil.which', lambda name: '/usr/bin/ffprobe' if name == 'ffprobe' else None)

    def fake_run(command, **kwargs):
        calls.append(command)
        assert kwargs == {
            'capture_output': True,
            'text': True,
            'timeout': 3,
            'check': False,
        }
        return subprocess.CompletedProcess(command, 0, stdout='25/1\n25/1\n', stderr='')

    monkeypatch.setattr('app.camera_backend.subprocess.run', fake_run)

    camera._probe_declared_fps()
    first = camera.get_frame()
    second = camera.get_frame()

    assert first['detected_fps'] == 25.0
    assert first['effective_fps'] == 25.0
    assert second['fps'] == 25.0
    assert len(calls) == 1
    assert calls[0][-2:] == ['-i', 'rtsp://user:secret@example/stream']


def test_get_frame_schedules_probe_in_background(monkeypatch) -> None:
    camera = OpenCvStreamCamera('rtsp://example/stream', fps=None)
    started: list[object] = []

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def is_alive(self):
            return False

        def start(self):
            started.append(self)

    monkeypatch.setattr('app.camera_backend.threading.Thread', FakeThread)
    monkeypatch.setattr('app.camera_backend.shutil.which', lambda _name: '/usr/bin/ffprobe')
    monkeypatch.setattr(
        'app.camera_backend.subprocess.run',
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, stdout='25/1\n', stderr=''),
    )

    first = camera.get_frame()
    assert first['fps_source'] == 'fallback'
    assert first['effective_fps'] == 15.0
    assert len(started) == 1
    assert started[0].name == 'camera-fps-probe'
    assert started[0].daemon is True

    started[0].target()
    assert camera.get_frame()['fps'] == 25.0


def test_probe_ignores_unusable_ffprobe_rate(monkeypatch) -> None:
    camera = OpenCvStreamCamera('rtsp://example/stream', fps=None)
    monkeypatch.setattr('app.camera_backend.shutil.which', lambda _name: '/usr/bin/ffprobe')
    monkeypatch.setattr(
        'app.camera_backend.subprocess.run',
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, stdout='0/0\n90000/1\n', stderr=''),
    )

    camera._probe_declared_fps()

    assert camera.detected_fps is None
    assert camera.effective_fps == 15.0
    assert camera.fps_source == 'fallback'
