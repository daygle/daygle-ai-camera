"""Regression tests for the audio-mapping crash fix.

Video-only RTSP cameras crash ffmpeg with
``Output file #N does not contain any stream`` when their RTSP stream has no
audio track, because the per-camera ingest's third ffmpeg output had no
mapped streams. These tests pin the contract:

* the worker writes a ``.no_audio`` marker the first time it sees that error
  and respawns ffmpeg video-only (drops ONLY the audio output),
* a fresh worker that finds the marker skips the doomed probe from iter 0,
* ``audio_segments_after`` raises ``RuntimeError("no audio track in stream")``
  for any consumer (test the sound-detector path here too),
* the sound detector's ``_run_ingest`` catches that prefix, sets a stable
  ``unavailable`` status, and never classifies audio for a no-audio stream.

All ffmpeg invocations are mocked via ``subprocess.Popen`` so the suite runs
without an installed ffmpeg.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Worker behaviour: detect, write marker, drop audio output, respawn video-only
# ─────────────────────────────────────────────────────────────────────────────


def test_prebuffer_worker_disables_audio_when_stream_lacks_it(tmp_path, monkeypatch):
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService(
        {
            "storage": {"recordings_dir": str(tmp_path / "rec")},
            "recording": {},
        }
    )
    camera_key = RecordingService._camera_key("cam")
    audio_dir = service.audio_dir / camera_key
    audio_dir.mkdir(parents=True, exist_ok=True)

    stop = threading.Event()
    captured: list[list[str]] = []

    class _FakeProc:
        def __init__(self, return_code: int = 0) -> None:
            self._rc = return_code

        def poll(self) -> int:
            return self._rc

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 - stub
            return 0

        def kill(self) -> None:
            return None

    iteration = {"count": 0}

    def fake_popen(cmd, stderr=None, **_kwargs):
        n = iteration["count"]
        iteration["count"] += 1
        captured.append(list(cmd))
        if n == 0:
            # First attempt: simulate ffmpeg refusing to build the audio
            # output on a video-only stream. Write the canonical stderr
            # line that the worker matches. rc=2 so we also exercise the
            # "ffmpeg exited unexpectedly" branch in production.
            Path(stderr.name).write_text(
                "[NULL] Output file #2 does not contain any stream\n",
                encoding="utf-8",
            )
            return _FakeProc(return_code=2)
        if n == 1:
            stop.set()  # one successful video-only iter then exit
        return _FakeProc(return_code=0)

    monkeypatch.setattr(recordings_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(recordings_module.subprocess, "Popen", fake_popen)

    service._run_prebuffer_worker(
        camera_key,
        "rtsp://example/stream",
        {
            "stop_event": stop,
            "stream_url": "rtsp://example/stream",
            "buffer_seconds": 20,
            "camera_id": "cam",
        },
    )

    assert iteration["count"] == 2, (
        f"expected worker to respawn ffmpeg once, got {iteration['count']} invocations"
    )
    first, second = captured
    first_str = " ".join(first)
    second_str = " ".join(second)

    # First attempt had audio output in the command.
    assert "0:a:0?" in first_str, "first attempt should map audio"
    assert "aud-" in first_str, "first attempt should target the WAV pattern"

    # Second attempt shed the audio output (no map, no pattern) but kept the
    # video segments + latest.jpg frame output so detection and live views
    # still work end-to-end.
    assert "0:a:0?" not in second_str, (
        "second attempt must not map audio after the no-stream error"
    )
    assert "aud-" not in second_str, (
        "second attempt must not target the WAV pattern after the no-stream error"
    )
    assert any(str(a).endswith("latest.jpg") for a in second), "frame output preserved"
    assert any(
        "segment-" in str(a) and str(a).endswith(".mp4") for a in second
    ), "video segments preserved"

    # Marker persisted so a restart on the same URL skips the doomed probe.
    marker = service._audio_disabled_marker(camera_key)
    assert marker.exists(), (
        ".no_audio marker must be written when ffmpeg cannot build the audio output"
    )


def test_prebuffer_worker_skips_audio_when_marker_already_present(tmp_path, monkeypatch):
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService(
        {
            "storage": {"recordings_dir": str(tmp_path / "rec")},
            "recording": {},
        }
    )
    camera_key = RecordingService._camera_key("cam")
    audio_dir = service.audio_dir / camera_key
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / RecordingService.NO_AUDIO_MARKER_FILENAME).touch()

    stop = threading.Event()
    captured: list[list[str]] = []

    class _FakeProc:
        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 - stub
            return 0

        def kill(self) -> None:
            return None

    def fake_popen(cmd, **_kwargs):
        captured.append(list(cmd))
        stop.set()  # one iteration only
        return _FakeProc()

    monkeypatch.setattr(recordings_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(recordings_module.subprocess, "Popen", fake_popen)

    service._run_prebuffer_worker(
        camera_key,
        "rtsp://example/stream",
        {
            "stop_event": stop,
            "stream_url": "rtsp://example/stream",
            "buffer_seconds": 20,
            "camera_id": "cam",
        },
    )

    assert len(captured) == 1
    cmd_str = " ".join(captured[0])
    assert "0:a:0?" not in cmd_str, (
        "worker with pre-existing marker must skip the audio map on attempt 0"
    )
    assert "aud-" not in cmd_str, (
        "worker with pre-existing marker must skip the WAV pattern on attempt 0"
    )
    assert any(str(a).endswith("latest.jpg") for a in captured[0]), (
        "frame output preserved"
    )
    assert any(
        "segment-" in str(a) and str(a).endswith(".mp4") for a in captured[0]
    ), "video segments preserved"


# ─────────────────────────────────────────────────────────────────────────────
# Consumer side: the marker must surface cleanly to anyone reading audio
# ─────────────────────────────────────────────────────────────────────────────


def test_audio_segments_after_raises_when_no_audio_marker(tmp_path):
    from app.recordings import RecordingService

    service = RecordingService(
        {
            "storage": {"recordings_dir": str(tmp_path / "rec")},
            "recording": {},
        }
    )
    audio_dir = service.audio_dir / service._camera_key("cam")
    audio_dir.mkdir(parents=True, exist_ok=True)
    marker = audio_dir / RecordingService.NO_AUDIO_MARKER_FILENAME

    # Without marker: returns [] (the no-audio case never wrote any segments).
    assert service.audio_segments_after("cam", 0.0) == []

    # With marker: clean RuntimeError carrying the consumer-facing prefix.
    marker.touch()
    try:
        with pytest.raises(RuntimeError, match=r"no audio track in stream"):
            service.audio_segments_after("cam", 0.0)
    finally:
        marker.unlink(missing_ok=True)

    # Marker removed: empty list again (no audio segments written yet).
    assert service.audio_segments_after("cam", 0.0) == []


def test_sound_detector_ingest_idles_when_stream_has_no_audio():
    # When ``audio_segments_after`` raises ``no audio track in stream`` the
    # sound detector must catch it, surface a stable 'unavailable' status,
    # throttle probes to once every 30s (so it doesn't hammer the file
    # system on a queue that can never produce a chunk), and never call
    # ``_handle_chunk`` on a non-audio stream.
    from app.sound_detector import NO_AUDIO_EXC_PREFIX, SoundDetector

    handle_chunk_calls: list[object] = []

    def provider(_after_ts: float) -> list[tuple[object, float]]:
        raise RuntimeError(
            f"{NO_AUDIO_EXC_PREFIX}: cam (RTSP stream has no audio; "
            f"per-camera ingest is running video-only)"
        )

    det = SoundDetector(
        on_detect=lambda *a, **k: None,
        rules=[],
        source="ingest",
        audio_segment_provider=provider,
    )
    det._handle_chunk = lambda audio: handle_chunk_calls.append(audio)
    det.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            if NO_AUDIO_EXC_PREFIX in det.status:
                break
            time.sleep(0.1)
        assert NO_AUDIO_EXC_PREFIX in det.status, (
            f"expected unavailable status, got {det.status!r}"
        )
        assert handle_chunk_calls == [], (
            "no-audio streams must not produce classified chunks"
        )
    finally:
        det.stop()
