"""Regression tests for Bug 2: ensure_*_worker must join the old worker's
thread BEFORE starting a replacement worker.

Without joining:
* two ffmpegs race over the same per-camera sidecar directories
  (``frames``/``latest.jpg``, ``.prebuffer``/``segment-*.mp4``,
  ``.audio``/``aud-*.wav``, ``continuous-{key}``) and a reader can observe a
  half-written JPEG/segment written by the new ffmpeg,
* the old worker's ``finally``-block segment+audio pruner can silently
  delete freshly-written segments from the new worker — destroying event
  pre-roll footage immediately after every restart,
* on a URL change, the still-running old worker can touch the
  ``.no_audio`` marker AFTER we cleared it (P1 race from earlier review),
  locking the new URL into permanent video-only mode.

Tests assert *ordered events* (recorded via monkeypatched ``_stop_worker``
and ``threading.Thread.start``), not wall-clock timing, so the suite is
deterministic and does not flake on slow CI.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# ffmpeg stand-in
# ─────────────────────────────────────────────────────────────────────────────


class _FakeProc:
    """Stand-in for ``subprocess.Popen`` whose ``poll()`` always returns
    ``None`` so the worker thread spins until ``stop_event`` is set; the
    worker's ``finally`` block then calls ``terminate()`` and the thread
    exits naturally."""

    def __init__(self) -> None:
        self.terminated = False

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 - stub
        return 0

    def kill(self) -> None:
        self.terminated = True


def _install_fake_popen(monkeypatch, recordings_module, counter: list[int]) -> None:
    def fake_popen(cmd, stderr=None, **_kwargs):  # noqa: ARG001 - test stub
        counter[0] += 1
        return _FakeProc()

    monkeypatch.setattr(recordings_module.subprocess, "Popen", fake_popen)


# ─────────────────────────────────────────────────────────────────────────────
# Event recorder for ordered assertions
# ─────────────────────────────────────────────────────────────────────────────


def _patch_event_recording(monkeypatch, recordings_module, target_cls):
    """Monkeypatch ``_stop_worker`` and ``threading.Thread.start`` so each
    call appends a small marker to a shared ``events`` list. Tests then
    assert event-order constraints without relying on timing."""
    events_lock = threading.Lock()
    events: list[tuple[float, str]] = []

    original_start = recordings_module.threading.Thread.start

    def tracking_start(self) -> None:
        with events_lock:
            events.append((time.monotonic(), "thread_start"))
        original_start(self)

    monkeypatch.setattr(recordings_module.threading.Thread, "start", tracking_start)

    def tracking_stop_worker(worker, join_timeout: float = 2.0) -> None:  # noqa: ARG001 - match helper signature
        thread = worker.get("thread")
        with events_lock:
            events.append((time.monotonic(), "stop_worker_enter"))
        if isinstance(thread, threading.Thread):
            thread.join(timeout=join_timeout)
            with events_lock:
                events.append((time.monotonic(), "stop_worker_joined"))

    monkeypatch.setattr(target_cls, "_stop_worker", staticmethod(tracking_stop_worker))

    return events, events_lock


def _wait_for_first_popen(counter: list[int], timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while counter[0] < 1 and time.time() < deadline:
        time.sleep(0.02)


def _assert_join_precedes_replacement_start(events: list[tuple[float, str]], expected_starts: int) -> None:
    starts = [idx for idx, (_, tag) in enumerate(events) if tag == "thread_start"]
    joins = [idx for idx, (_, tag) in enumerate(events) if tag == "stop_worker_joined"]
    assert len(starts) == expected_starts, (
        f"expected exactly {expected_starts} thread_start events, got {len(starts)}: {events}"
    )
    assert joins, f"expected at least one stop_worker_joined event, got none: {events}"
    last_join = max(joins)
    second_start = starts[1]
    assert last_join < second_start, (
        "old worker thread must be joined BEFORE replacement thread starts; "
        f"events={events}"
    )


def _stop_current_worker(service, lock_attr: str) -> None:
    # ``str.rstrip("_lock")`` strips ANY of those characters from the right
    # edge, so dynamically deriving the workers-dict attribute name from the
    # lock attribute name is brittle ('_prebuffer_lock' -> '_pre' + 's' ->
    # '_pres' would be wrong). Use an explicit mapping instead.
    lock = getattr(service, lock_attr)
    workers = (
        service._prebuffer_workers
        if "prebuffer" in lock_attr
        else service._continuous_workers
    )
    with lock:
        worker = workers.get("cam")
    if worker:
        worker["stop_event"].set()
        thread = worker.get("thread")
        if isinstance(thread, threading.Thread):
            thread.join(timeout=10)


# ─────────────────────────────────────────────────────────────────────────────
# _ensure_prebuffer_worker
# ─────────────────────────────────────────────────────────────────────────────


def test_ensure_prebuffer_worker_joins_old_thread_before_replacement(
    tmp_path, monkeypatch
):
    """Bug 2 regression: replacing a live prebuffer worker (e.g. on URL change)
    must call ``_stop_worker`` on the old worker AND observe ``join`` return
    BEFORE ``thread.start`` is called for the replacement. Recorded as an
    ordered event log so the assertion does not depend on timing."""
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService(
        {
            "storage": {"recordings_dir": str(tmp_path / "rec")},
            "recording": {},
        }
    )
    monkeypatch.setattr(recordings_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    popen_count = [0]
    _install_fake_popen(monkeypatch, recordings_module, popen_count)
    events, _events_lock = _patch_event_recording(monkeypatch, recordings_module, RecordingService)

    # Start worker #1.
    service._ensure_prebuffer_worker("cam", "rtsp://example/stream", 20, camera_id="cam")
    _wait_for_first_popen(popen_count)

    # Replace via URL change → forces the join path.
    service._ensure_prebuffer_worker("cam", "rtsp://example/stream/v2", 20, camera_id="cam")

    _assert_join_precedes_replacement_start(events, expected_starts=2)
    _stop_current_worker(service, "_prebuffer_lock")


def test_ensure_prebuffer_worker_url_change_clears_marker_after_old_thread_exits(
    tmp_path, monkeypatch
):
    """Bug 2 + URL-change marker race: when URL changes, the marker is
    cleared AFTER the old worker thread is fully joined, so a still-running
    old worker (which detected no-audio on the OLD URL) cannot re-touch the
    marker file and lock the new URL into permanent video-only mode."""
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService(
        {
            "storage": {"recordings_dir": str(tmp_path / "rec")},
            "recording": {},
        }
    )
    monkeypatch.setattr(recordings_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    popen_count = [0]
    _install_fake_popen(monkeypatch, recordings_module, popen_count)
    _patch_event_recording(monkeypatch, recordings_module, RecordingService)

    # Start worker #1 against the original URL.
    service._ensure_prebuffer_worker("cam", "rtsp://example/stream", 20, camera_id="cam")
    _wait_for_first_popen(popen_count)

    # Simulate "the OLD URL was video-only": the previous run wrote the marker.
    key = RecordingService._camera_key("cam")
    marker = service._audio_disabled_marker(key)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    assert marker.exists()

    # URL change → worker is replaced; marker must be cleared AS PART of the
    # replace path, AFTER the old worker is gone.
    service._ensure_prebuffer_worker("cam", "rtsp://example/stream/v2", 20, camera_id="cam")

    assert not marker.exists(), (
        ".no_audio marker must be cleared on URL change so the new URL is "
        "probed fresh; marker present after _ensure_prebuffer_worker returned "
        "means the clear raced the old worker's `finally` block."
    )

    # The current thread is the new replacement worker; it must be alive.
    with service._prebuffer_lock:
        worker = service._prebuffer_workers.get("cam")
    assert worker is not None
    thread = worker.get("thread")
    assert isinstance(thread, threading.Thread)
    assert thread.is_alive(), "replacement worker thread should be alive after URL change"

    thread.join(timeout=10)  # let the test settle; the new worker is still running


# ─────────────────────────────────────────────────────────────────────────────
# _ensure_continuous_chunk_worker
# ─────────────────────────────────────────────────────────────────────────────


def test_ensure_continuous_chunk_worker_joins_old_thread_before_replacement(
    tmp_path, monkeypatch
):
    """Bug 2 regression for the continuous chunk worker (same pattern as
    prebuffer worker). Replacing a live chunk worker must join the old
    thread before starting the new one."""
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService(
        {
            "storage": {"recordings_dir": str(tmp_path / "rec")},
            "recording": {},
        }
    )
    monkeypatch.setattr(recordings_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    popen_count = [0]
    _install_fake_popen(monkeypatch, recordings_module, popen_count)
    events, _events_lock = _patch_event_recording(monkeypatch, recordings_module, RecordingService)

    service._ensure_continuous_chunk_worker(
        "cam",
        "rtsp://example/stream",
        tmp_path / "rec" / "continuous-cam",
        60,
        None,
    )
    _wait_for_first_popen(popen_count)

    # Replace by changing chunk_seconds so the early-return path is skipped.
    service._ensure_continuous_chunk_worker(
        "cam",
        "rtsp://example/stream",
        tmp_path / "rec" / "continuous-cam",
        120,
        None,
    )

    _assert_join_precedes_replacement_start(events, expected_starts=2)

    # Cleanup.
    with service._continuous_lock:
        worker = service._continuous_workers.get("cam")
    if worker:
        worker["stop_event"].set()
        thread = worker.get("thread")
        if isinstance(thread, threading.Thread):
            thread.join(timeout=10)
