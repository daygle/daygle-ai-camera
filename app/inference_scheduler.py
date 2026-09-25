"""Central latest-frame-wins scheduler for live camera inference.

Background detection used to spawn one daemon thread per camera from
``run_live_alert_monitor_once``. Each thread read its own frame and then
blocked on the shared detector's inference semaphore, so with more cameras
than the semaphore allows, N-1 threads sat parked in ffmpeg-free, GPU-free
staging while holding their thread slots: on a busy box every camera's cycle
got later, and a thread that finally acquired the semaphore could be looking
at a decision it had already started before the backlog existed.

This module replaces that fan-out with one admission point:

* **One job per camera, newest wins.** A camera that already has a job waiting
  has it REPLACED by the newer request rather than queued behind it, so a
  camera can never accumulate a backlog of stale work. The frame itself is
  read when the job runs, not when it was submitted, so what gets processed is
  always the newest frame available at that moment.
* **Fair round-robin.** Pending jobs are ordered by arrival within a priority
  tier, so a camera can never be starved by a busier neighbour.
* **Priority.** A camera that is actively recording, or whose previous job
  finished more than two detection intervals ago (it is already behind), is
  served before the rest.
* **One global concurrency limit.** The number of worker threads is the same
  ``max_concurrent_inferences`` the detector's own semaphore enforces, so the
  limit is applied at the door instead of by N threads piling onto one lock.
* **Wait time is measured separately from run time.** Queue wait and inference
  (or, more precisely, whole-cycle) duration are recorded independently, so a
  slow model and a starved queue are no longer the same number.

The module deliberately has no app imports: the caller injects the runner, the
priority predicate and the completion hooks, which keeps it unit-testable
without cv2 / ONNX Runtime present.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger('daygle.ai')

# How long an idle worker parks on the condition variable before re-checking the
# stop flag. Short enough that ``stop()`` is prompt, long enough that an idle
# scheduler costs nothing.
_IDLE_WAIT_SECONDS = 0.25
# A job counts as "stale tracking" once its predecessor finished more than this
# multiple of the camera's detection interval ago: the camera is already behind
# and should be served next.
_STALE_INTERVAL_MULTIPLE = 2.0


class InferenceJob:
    """One camera's pending detection request."""

    __slots__ = ('camera_id', 'settings', 'read_frame', 'runner', 'submitted_at', 'interval', 'sequence')

    def __init__(
        self,
        camera_id: str,
        settings: dict[str, Any],
        read_frame: Callable[[], Optional[tuple[Any, dict[str, Any]]]],
        *,
        runner: Callable[[Any, dict[str, Any], dict[str, Any]], Any] | None = None,
        interval: float = 0.5,
        submitted_at: float = 0.0,
        sequence: int = 0,
    ):
        self.camera_id = camera_id
        self.settings = settings
        self.read_frame = read_frame
        self.runner = runner
        self.submitted_at = submitted_at
        self.interval = interval
        self.sequence = sequence


class LiveInferenceScheduler:
    """Admits at most ``max_workers`` camera detection jobs at a time.

    ``runner(image, frame, settings)`` performs the detection cycle.
    ``is_priority(camera_id)`` may be supplied to lift actively-recording
    cameras ahead of the rest; ``on_claim`` / ``on_release`` / ``on_complete``
    are lifecycle hooks used by the caller to keep its own bookkeeping (and the
    live status payload) in sync.
    """

    def __init__(
        self,
        runner: Callable[[Any, dict[str, Any], dict[str, Any]], Any],
        *,
        is_priority: Callable[[str], bool] | None = None,
        on_claim: Callable[[str], None] | None = None,
        on_release: Callable[[str], None] | None = None,
        on_complete: Callable[[str, dict[str, Any]], None] | None = None,
        max_workers: int = 1,
        clock: Callable[[], float] = time.time,
    ):
        self._runner = runner
        self._is_priority = is_priority
        self._on_claim = on_claim
        self._on_release = on_release
        self._on_complete = on_complete
        self._clock = clock
        self._condition = threading.Condition()
        self._pending: dict[str, InferenceJob] = {}
        self._running: set[str] = set()
        self._sequence = 0
        self._max_workers = max(1, int(max_workers or 1))
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._started = False
        self._last_finished: dict[str, float] = {}
        self._timings: dict[str, dict[str, Any]] = {}
        self._stats: dict[str, int] = {
            'submitted': 0, 'completed': 0, 'superseded': 0,
            'no_frame': 0, 'failed': 0, 'dropped': 0,
        }

    # ─── Configuration ────────────────────────────────────────────────────

    @property
    def max_workers(self) -> int:
        with self._condition:
            return self._max_workers

    def set_max_workers(self, count: int) -> None:
        """Apply a new global concurrency limit, starting workers as needed.

        Shrinking never kills a running job: surplus workers notice on their
        next loop and exit, and they are restarted if the limit grows again.
        """
        wanted = max(1, int(count or 1))
        with self._condition:
            if wanted == self._max_workers and len(self._threads) >= wanted:
                return
            self._max_workers = wanted
            self._ensure_threads_locked()
            self._condition.notify_all()

    # ─── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._condition:
            if self._started:
                return
            self._started = True
            self._stopping.clear()
            self._ensure_threads_locked()
            self._condition.notify_all()

    def stop(self, timeout: float = 5.0) -> None:
        with self._condition:
            if not self._started:
                return
            self._stopping.set()
            self._started = False
            # Nothing queued can be served any more: drop it rather than let a
            # worker pick up a request whose camera has since been removed.
            dropped = list(self._pending)
            self._pending.clear()
            self._stats['dropped'] += len(dropped)
            self._condition.notify_all()
        # A dropped job never reaches a worker, so release its camera here or
        # the caller would consider it scheduled forever.
        for camera_id in dropped:
            self._release(camera_id)
        for thread in list(self._threads):
            thread.join(timeout=timeout)
        with self._condition:
            self._threads = [t for t in self._threads if t.is_alive()]

    def _ensure_threads_locked(self) -> None:
        self._threads = [t for t in self._threads if t.is_alive()]
        while len(self._threads) < self._max_workers:
            index = len(self._threads)
            thread = threading.Thread(
                target=self._worker_loop,
                name=f'live-inference-{index}',
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    # ─── Submission ───────────────────────────────────────────────────────

    def submit(
        self,
        camera_id: str,
        settings: dict[str, Any],
        read_frame: Callable[[], Optional[tuple[Any, dict[str, Any]]]],
        *,
        runner: Callable[[Any, dict[str, Any], dict[str, Any]], Any] | None = None,
        interval: float = 0.5,
    ) -> bool:
        """Queue one detection job for ``camera_id``; never blocks.

        Returns ``False`` when the scheduler is stopped (the caller should fall
        back to its own path). A second submission for a camera that still has
        a job waiting REPLACES it -- the older request is obsolete by
        definition, since the frame is read at execution time.
        """
        with self._condition:
            if not self._started or self._stopping.is_set():
                return False
            self._sequence += 1
            job = InferenceJob(
                camera_id,
                settings,
                read_frame,
                runner=runner,
                interval=max(0.1, float(interval or 0.5)),
                submitted_at=self._clock(),
                sequence=self._sequence,
            )
            if camera_id in self._pending:
                self._stats['superseded'] += 1
            self._pending[camera_id] = job
            self._stats['submitted'] += 1
            self._ensure_threads_locked()
            self._condition.notify()
        # Claim the camera as soon as it is scheduled, not when a worker picks
        # it up: between submit and pick the camera is still "busy" for the
        # caller's duplicate-suppression check, exactly as when every camera
        # had its own thread.
        if self._on_claim is not None:
            try:
                self._on_claim(camera_id)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug('Scheduler claim hook failed for %s: %s', camera_id, exc)
        return True

    def _release(self, camera_id: str) -> None:
        if self._on_release is None:
            return
        try:
            self._on_release(camera_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug('Scheduler release hook failed for %s: %s', camera_id, exc)

    def pending_cameras(self) -> list[str]:
        with self._condition:
            return sorted(self._pending)

    def is_busy(self, camera_id: str) -> bool:
        with self._condition:
            return camera_id in self._pending or camera_id in self._running

    def stats(self) -> dict[str, Any]:
        with self._condition:
            return {
                **self._stats,
                'pending': len(self._pending),
                'running': len(self._running),
                'max_workers': self._max_workers,
            }

    def last_timing(self, camera_id: str) -> dict[str, Any] | None:
        with self._condition:
            timing = self._timings.get(camera_id)
            return dict(timing) if timing else None

    def timings(self) -> dict[str, dict[str, Any]]:
        with self._condition:
            return {key: dict(value) for key, value in self._timings.items()}

    def clear_timings(self) -> None:
        with self._condition:
            self._timings.clear()

    # ─── Selection ────────────────────────────────────────────────────────

    def _priority(self, job: InferenceJob) -> int:
        """2 = actively recording, 1 = already falling behind, 0 = normal."""
        if self._is_priority is not None:
            try:
                if self._is_priority(job.camera_id):
                    return 2
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug('Scheduler priority check failed for %s: %s', job.camera_id, exc)
        last = self._last_finished.get(job.camera_id)
        if last is not None and (self._clock() - last) > _STALE_INTERVAL_MULTIPLE * job.interval:
            return 1
        return 0

    def _take_next(self) -> tuple[InferenceJob, int] | None:
        with self._condition:
            if not self._pending:
                return None
            depth = len(self._pending)
            # Highest priority first, then oldest request first: that is the
            # round-robin guarantee, expressed as a stable sort key rather than
            # a rotating cursor, so a camera that keeps submitting cannot push
            # itself ahead of one that has been waiting longer.
            best = min(
                self._pending.values(),
                key=lambda job: (-self._priority(job), job.submitted_at, job.sequence),
            )
            del self._pending[best.camera_id]
            self._running.add(best.camera_id)
            return best, depth

    # ─── Worker ───────────────────────────────────────────────────────────

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                if self._stopping.is_set() or not self._started:
                    return
            taken = self._take_next()
            if taken is None:
                with self._condition:
                    if self._stopping.is_set() or not self._started:
                        return
                    self._condition.wait(_IDLE_WAIT_SECONDS)
                continue
            job, depth = taken
            try:
                self._run_job(job, depth)
            finally:
                with self._condition:
                    self._running.discard(job.camera_id)
                    self._last_finished[job.camera_id] = self._clock()
                self._release(job.camera_id)

    def _run_job(self, job: InferenceJob, depth: int) -> None:
        started = self._clock()
        wait_seconds = max(0.0, started - job.submitted_at)
        outcome = 'completed'
        try:
            sample = job.read_frame()
            if sample is None:
                outcome = 'no_frame'
                return
            image, frame = sample
            run = job.runner or self._runner
            run(image, frame, job.settings)
        except Exception as exc:
            outcome = 'failed'
            logger.warning('Scheduled live detection failed for camera %s: %s', job.camera_id, exc)
        finally:
            finished = self._clock()
            run_seconds = max(0.0, finished - started)
            with self._condition:
                # Queue wait and execution are recorded separately: a slow
                # model and a starved queue are different problems and must not
                # be averaged into one number.
                self._stats[outcome] = self._stats.get(outcome, 0) + 1
                self._timings[job.camera_id] = {
                    'wait_seconds': round(wait_seconds, 3),
                    'run_seconds': round(run_seconds, 3),
                    'queue_depth': depth,
                    'finished_at': finished,
                }
            if self._on_complete is not None:
                try:
                    self._on_complete(job.camera_id, self.last_timing(job.camera_id) or {})
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug('Scheduler completion hook failed for %s: %s', job.camera_id, exc)
            logger.debug(
                'Inference scheduler: camera %s waited %.3fs (depth %d), ran %.3fs',
                job.camera_id, wait_seconds, depth, run_seconds,
            )
