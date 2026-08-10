"""Regression tests for Bug 5: ``_stop_worker`` must emit an operator-facing
diagnostic via ``diagnostic_callback`` when ``thread.join(timeout=...)``
returns with the thread still alive. A hung ffmpeg teardown (network stall,
SIGTERM-handler crash, kill blocked on a flush) should NOT masquerade as a
clean shutdown; the replacement worker has been started anyway because
denying camera ingest would be worse than a brief overlap, but operators
deserve the warning alongside the existing ``prebuffer_restart`` /
``ingest_restart`` / ``prebuffer_fallback`` diagnostics.

The diagnostic must carry enough detail to triage the hang:

* ``camera_id`` so the operator knows which camera is wedged
* ``requested_timeout_seconds`` so they can compare to the
  ``PREBUFFER_WORKER_JOIN_TIMEOUT_SECONDS`` /
  ``CONTINUOUS_WORKER_JOIN_TIMEOUT_SECONDS`` ceiling they configured
* ``alive_after_seconds`` (\u2264 ``requested_timeout_seconds``) so they see the
  actual wait before giving up
* ``worker_kind`` (``prebuffer`` / ``continuous``) so they know which
  worker family hung
* ``thread_name`` so they can ``ps``/``kill`` the LWP if needed
"""

from __future__ import annotations

import importlib
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.recordings import RecordingService  # noqa: E402 (import after sys.path bootstrap)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _capture_callback(captured: list) -> None:
    """Install a ``diagnostic_callback`` on the service that records every
    invocation into ``captured`` for assertions. Each captured entry is a
    dict mirroring the ``(camera_id, event_type, message, severity, details)``
    contract used by ``_emit_diagnostic``."""
    def _cb(camera_id, event_type, message, *, severity, details):  # noqa: ARG001
        captured.append(
            {
                "camera_id": camera_id,
                "event_type": event_type,
                "severity": severity,
                "message": message,
                "details": dict(details),
            }
        )

    return _cb


def _make_hung_thread(thread_name: str = "hung-worker-test") -> tuple[threading.Thread, threading.Event]:
    """Spawn a daemon thread that ignores ``stop_event`` and waits on a
    barrier event instead. Tests release the barrier in their teardown so
    we don't leak the hung thread into subsequent tests."""
    barrier = threading.Event()

    def hung_target() -> None:
        barrier.wait()

    thread = threading.Thread(target=hung_target, daemon=True, name=thread_name)
    thread.start()
    time.sleep(0.05)  # give the hung thread a moment to enter its wait
    return thread, barrier


class _NeverExitsFakeProc:
    """Stand-in ``subprocess.Popen`` whose methods are no-ops; used when
    exercising the production ``_ensure_*_worker`` path to verify that the
    worker dict carries the metadata ``_stop_worker`` needs."""

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return 0

    def kill(self) -> None:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Bug 5 positive cases (hang fires diagnostic)
# ─────────────────────────────────────────────────────────────────────────────


def test_prebuffer_kind_emit_diagnostic_on_hang(tmp_path):
    """Worker dict has ``buffer_seconds`` \u2192 worker_kind='prebuffer';
    diagnostic event_type 'worker_stop_join_timeout_prebuffer' fires
    with all expected fields populated correctly."""
    service = RecordingService(
        {"storage": {"recordings_dir": str(tmp_path / "rec")}, "recording": {}}
    )
    captured: list = []
    service.diagnostic_callback = _capture_callback(captured)

    thread, release = _make_hung_thread("hung-prebuffer-test")
    stop_event = threading.Event()
    worker = {
        "thread": thread,
        "stop_event": stop_event,
        "buffer_seconds": 60,
        "camera_id": "cam1",
        "diagnostic_callback": service.diagnostic_callback,
    }

    try:
        service._stop_worker(worker, join_timeout=0.1)
        assert thread.is_alive(), (
            "thread should still be alive after join_timeout elapses"
        )
        assert stop_event.is_set(), (
            "stop_event should have been set even though the thread is hung"
        )

        assert len(captured) == 1, (
            f"expected exactly 1 diagnostic, got {len(captured)}: {captured}"
        )
        event = captured[0]
        assert event["event_type"] == "worker_stop_join_timeout_prebuffer"
        assert event["severity"] == "warning"
        assert event["camera_id"] == "cam1"
        assert event["details"]["camera_id"] == "cam1"
        assert event["details"]["requested_timeout_seconds"] == 0.1
        # alive_after_seconds is bounded by join_timeout; allow a little
        # slack for OS scheduling jitter.
        assert 0.1 <= event["details"]["alive_after_seconds"] <= 0.5, (
            f"alive_after_seconds should be ~0.1s with small slack, "
            f"got {event['details']['alive_after_seconds']}"
        )
        assert event["details"]["worker_kind"] == "prebuffer"
        assert event["details"]["thread_name"] == "hung-prebuffer-test"
        assert "cam1" in event["message"]
        assert "did not exit within 0.1s" in event["message"]
        assert "replacement worker has been started anyway" in event["message"]
    finally:
        release.set()
        thread.join(timeout=2)


def test_continuous_kind_emit_diagnostic_on_hang(tmp_path):
    """Worker dict has ``chunk_seconds`` instead of ``buffer_seconds``;
    the diagnostic event_type becomes 'worker_stop_join_timeout_continuous'."""
    service = RecordingService(
        {"storage": {"recordings_dir": str(tmp_path / "rec")}, "recording": {}}
    )
    captured: list = []
    service.diagnostic_callback = _capture_callback(captured)

    thread, release = _make_hung_thread("hung-continuous-test")
    worker = {
        "thread": thread,
        "stop_event": threading.Event(),
        "chunk_seconds": 60,
        "camera_id": "cam2",
        "diagnostic_callback": service.diagnostic_callback,
    }

    try:
        service._stop_worker(worker, join_timeout=0.1)
        assert len(captured) == 1
        assert captured[0]["event_type"] == "worker_stop_join_timeout_continuous"
        assert captured[0]["details"]["worker_kind"] == "continuous"
        assert captured[0]["camera_id"] == "cam2"
        assert captured[0]["severity"] == "warning"
    finally:
        release.set()
        thread.join(timeout=2)


def test_diagnostic_callback_exception_is_swallowed(tmp_path):
    """Worker hung, but ``diagnostic_callback`` raises. ``_stop_worker``
    debug-logs and continues - a misbehaving callback must not crash
    the shutdown path."""
    service = RecordingService(
        {"storage": {"recordings_dir": str(tmp_path / "rec")}, "recording": {}}
    )

    def broken_callback(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("simulated downstream failure")

    service.diagnostic_callback = broken_callback

    thread, release = _make_hung_thread("hung-broken-callback")
    worker = {
        "thread": thread,
        "stop_event": threading.Event(),
        "buffer_seconds": 60,
        "camera_id": "cam3",
        "diagnostic_callback": broken_callback,
    }

    try:
        # Should NOT raise even though the callback does.
        service._stop_worker(worker, join_timeout=0.05)
    finally:
        release.set()
        thread.join(timeout=2)


def test_missing_diagnostic_callback_falls_back_to_logger_warning(tmp_path, caplog):
    """When the worker dict carries no ``diagnostic_callback`` (e.g. a
    legacy caller, a manually-constructed dict, or a pre-Bug-5 hot-reload),
    ``_stop_worker`` falls back to ``logger.warning`` so the assumption isn't
    silently degraded to no signal at all."""
    import logging

    service = RecordingService(
        {"storage": {"recordings_dir": str(tmp_path / "rec")}, "recording": {}}
    )

    thread, release = _make_hung_thread("hung-no-callback-test")
    worker = {
        "thread": thread,
        "stop_event": threading.Event(),
        "buffer_seconds": 60,
        "camera_id": "cam-no-callback",
        # no diagnostic_callback
    }

    try:
        with caplog.at_level(logging.WARNING, logger="daygle.ai"):
            service._stop_worker(worker, join_timeout=0.05)
        # Find at least one warning containing the expected camera id.
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("cam-no-callback" in r.getMessage() for r in warnings), (
            f"expected logger.warning with camera_id; got warnings={[r.getMessage() for r in warnings]}"
        )
    finally:
        release.set()
        thread.join(timeout=2)


# ─────────────────────────────────────────────────────────────────────────────
# Negative case: clean shutdown does NOT emit diagnostic
# ─────────────────────────────────────────────────────────────────────────────


def test_clean_exit_does_not_emit_diagnostic(tmp_path):
    """Worker honors ``stop_event`` and exits within ``join_timeout`` -
    no diagnostic should fire."""
    service = RecordingService(
        {"storage": {"recordings_dir": str(tmp_path / "rec")}, "recording": {}}
    )
    captured: list = []
    service.diagnostic_callback = _capture_callback(captured)

    stop_event = threading.Event()

    def exits_on_stop() -> None:
        stop_event.wait(timeout=5)

    thread = threading.Thread(target=exits_on_stop, daemon=True)
    thread.start()
    time.sleep(0.05)

    worker = {
        "thread": thread,
        "stop_event": stop_event,
        "buffer_seconds": 60,
        "camera_id": "cam4",
        "diagnostic_callback": service.diagnostic_callback,
    }

    service._stop_worker(worker, join_timeout=2.0)

    assert not captured, (
        f"no diagnostic should fire on clean shutdown, got {captured}"
    )
    assert not thread.is_alive(), "thread should have exited within join_timeout"
    assert stop_event.is_set()


# ─────────────────────────────────────────────────────────────────────────────
# Production wiring: _ensure_*_worker stores the metadata _stop_worker needs
# ─────────────────────────────────────────────────────────────────────────────


def test_prebuffer_worker_dict_carries_diagnostic_metadata_for_later_stop(tmp_path, monkeypatch):
    """The ``_ensure_prebuffer_worker`` worker entry must include
    ``camera_id`` AND ``diagnostic_callback`` so a later stop (which
    routes through ``_stop_worker``) can emit a useful diagnostic on hang.
    We don't actually stop here - just verify the dict shape so we know
    ``_stop_worker`` will see the right context."""
    recordings_module = importlib.import_module("app.recordings")
    from app.recordings import RecordingService

    service = RecordingService(
        {"storage": {"recordings_dir": str(tmp_path / "rec")}, "recording": {}}
    )
    monkeypatch.setattr(
        recordings_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg"
    )
    monkeypatch.setattr(
        recordings_module.subprocess,
        "Popen",
        lambda *args, **_kw: _NeverExitsFakeProc(),
    )

    _, captured_cb = [], []
    service.diagnostic_callback = lambda *args, **kwargs: captured_cb.append((args, kwargs))

    service._ensure_prebuffer_worker(
        "cam-meta", "rtsp://example/stream", 60, camera_id="cam-meta"
    )
    time.sleep(0.1)

    with service._prebuffer_lock:
        worker = service._prebuffer_workers.get("cam-meta")
    assert worker is not None, (
        "prebuffer worker should be in dict after _ensure_prebuffer_worker"
    )
    assert "camera_id" in worker, (
        f"prebuffer worker_dict missing camera_id; keys={sorted(worker)}"
    )
    assert worker["camera_id"] == "cam-meta"
    assert "diagnostic_callback" in worker, (
        f"prebuffer worker_dict missing diagnostic_callback; keys={sorted(worker)}"
    )
    assert worker["diagnostic_callback"] is service.diagnostic_callback

    # Cleanup the actual worker thread.
    worker["stop_event"].set()
    thread = worker.get("thread")
    if isinstance(thread, threading.Thread):
        thread.join(timeout=2)


def test_continuous_worker_dict_carries_diagnostic_metadata_for_later_stop(tmp_path, monkeypatch):
    """Same as the prebuffer test but for ``_ensure_continuous_chunk_worker``.
    The continuous worker dict previously didn't include ``camera_id``;
    ``_stop_worker`` needs it to populate the diagnostic."""
    recordings_module = importlib.import_module("app.recordings")
    from app.recordings import RecordingService

    service = RecordingService(
        {"storage": {"recordings_dir": str(tmp_path / "rec")}, "recording": {}}
    )
    monkeypatch.setattr(
        recordings_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg"
    )
    monkeypatch.setattr(
        recordings_module.subprocess,
        "Popen",
        lambda *args, **_kw: _NeverExitsFakeProc(),
    )

    service._ensure_continuous_chunk_worker(
        "cam-continuous-meta",
        "rtsp://example/stream",
        tmp_path / "rec" / "continuous-cam-continuous-meta",
        60,
        None,
    )
    time.sleep(0.1)

    with service._continuous_lock:
        worker = service._continuous_workers.get("cam-continuous-meta")
    assert worker is not None, (
        "continuous worker should be in dict after _ensure_continuous_chunk_worker"
    )
    assert "camera_id" in worker, (
        f"continuous worker_dict missing camera_id; keys={sorted(worker)}"
    )
    # Continuous worker has no friendly name above camera_key, so the
    # worker-store's ``camera_id`` equals the camera_key.
    assert worker["camera_id"] == "cam-continuous-meta"
    assert "diagnostic_callback" in worker
    assert worker["diagnostic_callback"] is service.diagnostic_callback

    # Cleanup.
    worker["stop_event"].set()
    thread = worker.get("thread")
    if isinstance(thread, threading.Thread):
        thread.join(timeout=2)
