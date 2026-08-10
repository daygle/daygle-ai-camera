"""Regression tests for Bug 4: ``stop_*`` methods must hold their per-camera
lock through ``_stop_worker``'s join, so a concurrent ``_ensure_*_worker``
call cannot start a new ffmpeg writing into the just-vacated per-camera
directory while the old ffmpeg is still alive in its SIGTERM teardown.

Without the fix:
* ``stop_prebuffer_workers`` / ``stop_continuous_chunk_recording`` did
  ``.pop()`` under the lock and then called ``_stop_worker`` *outside*
  the lock. A concurrent ``_ensure_*_worker`` could grab the lock the
  instant it was released, see an empty dict, skip the Bug-2 join path,
  and ``subprocess.Popen`` a fresh ffmpeg writing into the per-camera
  sidecar directories (``frames/<key>/latest.jpg``,
  ``.prebuffer/<key>/segment-*.mp4``, ``.audio/<key>/aud-*.wav``,
  ``continuous-<key>/``) that the old ffmpeg hadn't yet released.

The fix:
* The stop helpers now join under the same lock transaction that
  removed the worker entry, mirroring the pattern ``_ensure_*_worker``
  already uses internally to close Bug 2.

Each test mocks ``_stop_worker`` to insert a small stall BEFORE the
underlying ``thread.join``, then asserts ``stop_worker_joined`` precedes
the new ``thread.start`` from a concurrent ``_ensure_*_worker`` call.
Without the lock fix the ordering inverts because the new thread starts
during stop's pre-join stall (the one window where stop used to hold the
dict open without holding the lock).
"""

from __future__ import annotations

import threading
import time


# ─────────────────────────────────────────────────────────────────────────────
# Test scaffolding
# ─────────────────────────────────────────────────────────────────────────────


class _SlowFakeProc:
    """Stand-in for ``subprocess.Popen`` whose terminate() sleeps briefly so
    ``_stop_worker``'s ``thread.join`` takes measurable wall time. Without
    this the join returns instantly and the lock-held-through-join race
    never opens."""

    def __init__(self) -> None:
        self.terminated = False

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminated = True
        time.sleep(0.2)

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 - mock
        return 0

    def kill(self) -> None:
        return None


def _install_slow_fake_popen(monkeypatch, recordings_module) -> None:
    monkeypatch.setattr(
        recordings_module.subprocess,
        "Popen",
        lambda cmd, stderr=None, **_kw: _SlowFakeProc(),
    )


def _make_event_recorder(monkeypatch, recordings_module, target_cls, *, stall_seconds: float = 0.3):
    """Patch ``_stop_worker`` and ``threading.Thread.start`` so each call
    appends a tagged timestamp to a shared ``events`` list.

    The patched ``_stop_worker`` sleeps ``stall_seconds`` BEFORE delegating
    to the underlying ``_stop_worker`` so stop's lock-held window opens
    enough for a concurrent ``_ensure_*_worker`` to either be blocked on
    it (NEW behavior) or to slip past the lock and start a fresh thread
    (OLD bug). Note: the stall is INTENTIONAL and intentionally lower
    than the proper join_timeout - the join_timeout governs the
    *real* join time, the stall governs only the test-injected delay
    to expose the race."""

    events_lock = threading.Lock()
    events: list[tuple[float, str]] = []

    real_thread_start = recordings_module.threading.Thread.start

    # Only log starts for thread names the recording service spawns - the
    # test itself spawns a ``concurrent_ensure`` plumbing thread whose
    # ``start()`` would otherwise inflate the perceived count.
    worker_name_prefixes = ("prebuffer-", "continuous-recorder-")

    def tracking_start(self) -> None:
        if self.name.startswith(worker_name_prefixes):
            with events_lock:
                events.append((time.monotonic(), "thread_start", self.name))
        real_thread_start(self)

    monkeypatch.setattr(recordings_module.threading.Thread, "start", tracking_start)

    # On Python 3.10+, ``target_cls._stop_worker`` returns the underlying
    # ``function`` directly (static methods no longer expose ``__func__``).
    # Calling it the same way ``RecordingService._stop_worker(w, t)`` is
    # called in production preserves the original behaviour.
    real_stop_worker = target_cls._stop_worker

    def tracking_stop_worker(worker, join_timeout: float = 2.0) -> None:  # noqa: ARG001
        # Append a 3-tuple matching ``tracking_start`` so consumers can
        # destructure events uniformly; the name slot is None for stop
        # events (no thread to name yet).
        with events_lock:
            events.append((time.monotonic(), "stop_worker_enter", None))
        time.sleep(stall_seconds)
        real_stop_worker(worker, join_timeout)
        with events_lock:
            events.append((time.monotonic(), "stop_worker_joined", None))

    monkeypatch.setattr(target_cls, "_stop_worker", staticmethod(tracking_stop_worker))

    return events, events_lock


def _assert_precedes(events_list, expected_first_tag, expected_second_tag):
    """Assert ``expected_first_tag`` event precedes ``expected_second_tag``.
    Returns the time of the first occurrence of each tag for debugging."""
    first_idx = next(
        (idx for idx, (_, tag, _) in enumerate(events_list) if tag == expected_first_tag),
        None,
    )
    second_idx = next(
        (idx for idx, (_, tag, _) in enumerate(events_list) if tag == expected_second_tag),
        None,
    )
    assert first_idx is not None, f"event {expected_first_tag!r} missing from events={events_list}"
    assert second_idx is not None, f"event {expected_second_tag!r} missing from events={events_list}"
    assert first_idx < second_idx, (
        f"{expected_first_tag} must precede {expected_second_tag}; events={events_list}"
    )


def _stop_worker_thread(service, lock_attr: str, key: str):
    """Stop the current worker under the named lock so a test can exit
    cleanly regardless of which path leave a worker alive."""
    workers_attr = {
        "_continuous_lock": "_continuous_workers",
        "_prebuffer_lock": "_prebuffer_workers",
    }[lock_attr]
    with getattr(service, lock_attr):
        workers = getattr(service, workers_attr)
        worker = workers.get(key)
    if worker:
        worker["stop_event"].set()
        thread = worker.get("thread")
        if isinstance(thread, threading.Thread):
            thread.join(timeout=10)


# ─────────────────────────────────────────────────────────────────────────────
# stop_continuous_chunk_recording
# ─────────────────────────────────────────────────────────────────────────────


def test_stop_continuous_chunk_recording_holds_lock_through_join(tmp_path, monkeypatch):
    """Bug 4 regression for the continuous chunk worker.

    Sequence:
      1. ``_ensure_continuous_chunk_worker`` starts worker #1 (its
         ``thread.start`` runs BEFORE the event-recording patch is in
         place, so it doesn't show up in the recorded events).
      2. ``_make_event_recorder`` patches ``_stop_worker`` and
         ``Thread.start``.
      3. A concurrent ``_ensure_continuous_chunk_worker`` thread is
         spawned with a brief ``time.sleep`` BEFORE its lock-acquire
         attempt so the main test thread's ``stop_continuous_chunk_recording``
         call wins the race for the lock.
      4. With the fix, the lock is held through stop's _stop_worker
         stall (≈0.3s). The concurrent ensure blocks on the lock for
         that window, and its ``thread.start`` fires only after stop's
         ``stop_worker_joined``. Without the fix, ensure grabs the
         lock the instant stop's pop releases it (during the patched
         stall); its ``thread.start`` therefore fires BEFORE
         ``stop_worker_joined``. The assertion inverts.
    """
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService(
        {
            "storage": {"recordings_dir": str(tmp_path / "rec")},
            "recording": {},
        }
    )
    monkeypatch.setattr(recordings_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    _install_slow_fake_popen(monkeypatch, recordings_module)

    cam = "cam"
    chunks_dir = tmp_path / "rec" / f"continuous-{cam}"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    # Track these in the outer scope so the ``finally`` block can clean them
    # up even when an earlier assertion already fired and returned control
    # before the in-body join/set could run.
    ensure_thread: threading.Thread | None = None
    try:
        # 1. Start worker #1 BEFORE applying event-recording patches so its
        # Thread.start() uses the real implementation rather than recorder.
        service._ensure_continuous_chunk_worker(
            cam, "rtsp://example/stream", chunks_dir, 60, on_chunk_complete=None
        )
        time.sleep(0.15)  # give #1 a moment to reach its inner polling loop

        # 2. Apply event-tracking patches.
        events, _events_lock = _make_event_recorder(
            monkeypatch, recordings_module, RecordingService, stall_seconds=0.3
        )

        # 3. Spawn ensure with a brief delay so stop's lock-acquire wins. Under
        # CPython's GIL the main thread acquires ``_continuous_lock`` immediately
        # on entering ``stop_continuous_chunk_recording`` before yielding to the
        # scheduler, so the 0.05s warm-up is a deliberate margin rather than a
        # load-bearing timing dependency.
        def concurrent_ensure() -> None:
            time.sleep(0.05)  # let main's stop call reach lock first
            service._ensure_continuous_chunk_worker(
                cam, "rtsp://example/stream/v2", chunks_dir, 120, on_chunk_complete=None
            )

        ensure_thread = threading.Thread(target=concurrent_ensure, daemon=True)
        ensure_thread.start()

        # 4. Run stop in the main thread. With the fix this holds the lock
        # through stop's _stop_worker stall; ensure blocks until then.
        service.stop_continuous_chunk_recording(cam)
        ensure_thread.join(timeout=10)

        # 5. Inspect events.
        with _events_lock:
            snapshot = list(events)

        # Exactly: one stop_worker enter+joined pair (from stop's _stop_worker)
        # and one worker thread_start event (from ensure's replacement).
        starts = [ev for ev in snapshot if ev[1] == "thread_start"]
        join_events = [tag for _, tag, _ in snapshot if tag in ("stop_worker_enter", "stop_worker_joined")]
        assert len(starts) == 1, (
            f"expected exactly 1 worker thread_start (replace #2 spawn), "
            f"got {len(starts)}: {snapshot}"
        )
        assert join_events == ["stop_worker_enter", "stop_worker_joined"], (
            f"expected stop's _stop_worker enter+joined pair in that order; "
            f"got join_events={join_events}, snapshot={snapshot}"
        )
        assert not ensure_thread.is_alive(), (
            f"concurrent ensure thread did not finish within 10s; snapshot={snapshot}"
        )

        _assert_precedes(snapshot, "stop_worker_joined", "thread_start")
    finally:
        # Cleanup MUST run even when one of the assertions above fires: an
        # active ``_run_continuous_chunk_worker`` thread on the orphaned
        # worker entry would still be writing into
        # ``continuous-{cam}/`` against the per-camera directory and would
        # leak into any subsequent test that opens the same ``tmp_path``.
        # ``ensure_thread`` may still be alive if an exception surfaced
        # before the in-body ``join``; join it here too so a future
        # regression in this test can't leak the plumbing thread either.
        if ensure_thread is not None and ensure_thread.is_alive():
            ensure_thread.join(timeout=10)
        _stop_worker_thread(service, "_continuous_lock", cam)


# ─────────────────────────────────────────────────────────────────────────────
# stop_prebuffer_workers
# ─────────────────────────────────────────────────────────────────────────────


def test_stop_prebuffer_workers_holds_lock_through_join(tmp_path, monkeypatch):
    """Bug 4 regression for the prebuffer (per-camera ingest) worker.

    Same shape as the continuous test: stop_prebuffer_workers must hold
    ``_prebuffer_lock`` through its ``_stop_worker`` join so a concurrent
    ``_ensure_prebuffer_worker`` cannot start a fresh ffmpeg during the
    stall. Without the fix, the concurrent ensure's ``thread.start`` would
    fire BEFORE stop's ``stop_worker_joined`` because the lock is free
    the moment the pop completes.
    """
    import app.recordings as recordings_module
    from app.recordings import RecordingService

    service = RecordingService(
        {
            "storage": {"recordings_dir": str(tmp_path / "rec")},
            "recording": {},
        }
    )
    monkeypatch.setattr(recordings_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    _install_slow_fake_popen(monkeypatch, recordings_module)

    cam = "cam"
    # Track ensure_thread in the outer scope so the ``finally`` block can
    # clean up even when an earlier assertion fires before the in-body join.
    ensure_thread: threading.Thread | None = None
    try:
        service._ensure_prebuffer_worker(
            cam, "rtsp://example/stream", buffer_seconds=60, camera_id=cam
        )
        time.sleep(0.15)  # give #1 a moment to reach its inner polling loop

        events, _events_lock = _make_event_recorder(
            monkeypatch, recordings_module, RecordingService, stall_seconds=0.3
        )

        # Same as the continuous test: spawn the concurrent ensure with a brief
        # warm-up so stop's lock-acquire (CPython GIL → immediately on entry) wins
        # under the main thread, then run stop in main and confirm ensure waits
        # out the stall.
        def concurrent_ensure() -> None:
            time.sleep(0.05)  # let main's stop call reach lock first
            service._ensure_prebuffer_worker(
                cam,
                "rtsp://example/stream/v2",
                buffer_seconds=60,
                camera_id=cam,
            )

        ensure_thread = threading.Thread(target=concurrent_ensure, daemon=True)
        ensure_thread.start()

        service.stop_prebuffer_workers()
        ensure_thread.join(timeout=10)

        with _events_lock:
            snapshot = list(events)

        starts = [ev for ev in snapshot if ev[1] == "thread_start"]
        join_events = [tag for _, tag, _ in snapshot if tag in ("stop_worker_enter", "stop_worker_joined")]
        assert len(starts) == 1, (
            f"expected exactly 1 worker thread_start (replace #2 spawn), "
            f"got {len(starts)}: {snapshot}"
        )
        assert join_events == ["stop_worker_enter", "stop_worker_joined"], (
            f"expected stop's _stop_worker enter+joined pair in that order; "
            f"got join_events={join_events}, snapshot={snapshot}"
        )
        assert not ensure_thread.is_alive(), (
            f"concurrent ensure thread did not finish within 10s; snapshot={snapshot}"
        )

        _assert_precedes(snapshot, "stop_worker_joined", "thread_start")
    finally:
        # Cleanup MUST run even when one of the assertions above fires: an
        # active ``_run_prebuffer_worker`` thread on an orphaned entry would
        # still be writing into the per-camera sidecar directories
        # (``.prebuffer/<key>/segment-*.mp4``, ``.audio/<key>/aud-*.wav``,
        # ``.frames/<key>/latest.jpg``) and would leak into any subsequent
        # test that opens the same ``tmp_path``. ``ensure_thread`` may still
        # be alive if an exception surfaced before the in-body ``join``; join
        # it here too so a future regression in this test can't leak the
        # plumbing thread either.
        if ensure_thread is not None and ensure_thread.is_alive():
            ensure_thread.join(timeout=10)
        _stop_worker_thread(service, "_prebuffer_lock", cam)
