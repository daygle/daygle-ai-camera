"""Bounded worker pool for expensive post-processing jobs.

Item 12 of the performance roadmap. Several of the most expensive things the
NVR does happen AFTER detection has already decided to alert, and each one
used to spawn its own daemon thread per job:

* ``app.recording_extension.start_rtsp_recording_capture`` started an
  ``rtsp-recording-<event_id>`` thread per event, and that thread runs ffmpeg
  (clip render, audio mux, sidecar/thumbnail writes).
* ``app.face_identity`` started a thread per enriched track and a thread per
  unknown-face capture, each doing an embedding + image encode + DB write.

A thread-per-job design has no ceiling. The realistic failure is a burst: a car
crosses the driveway and twelve overlapping events fire within a couple of
seconds, so twelve ffmpeg renders start at once and compete for the same cores
and the same disk that the detection and continuous-recording workers are
using. That is the same CPU oversubscription item 9 fixed for inference, and it
lands in the worst possible place - the moments when the system is already
under the most load and the footage the user is waiting for is the thing that
slows down.

This module replaces the unbounded fan-out with a real queue:

* **Fixed concurrency limit.** A fixed number of worker threads, sized well
  below the core count because every job is ffmpeg/ffprobe-bound rather than
  CPU-bound in Python.
* **Backpressure.** ``submit()`` blocks once the backlog is full instead of
  letting jobs pile up in memory. The caller here is the detection thread, so
  backpressure is deliberate: it is better for a detection cycle to wait
  briefly than for the process to accumulate unbounded work.
* **Priority.** Event clip finalization outranks background enrichment. A user
  waiting on a clip matters more than a face embedding nobody is watching yet,
  and a starved clip queue during a burst is the visible failure.
* **Shutdown cancellation.** ``shutdown()`` stops accepting work, lets
  in-flight jobs finish, and returns without waiting on the backlog, so a
  service restart is never held open by a queue of renders nobody will read.
* **Metrics.** Queue depth, submitted/completed/failed/dropped counts and
  run-duration totals, so "the recorder fell behind" is a number rather than a
  guess.

Like ``app.inference_scheduler``, this module deliberately has no app imports:
the caller injects the job body, which keeps it unit-testable without cv2 /
ONNX Runtime / ffmpeg present.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger('daygle.ai')

# Priority tiers. Lower runs first. A clip the user asked for beats background
# enrichment, which beats work nobody is waiting on.
PRIORITY_CLIP = 0
PRIORITY_BACKGROUND = 10

# Default backlog. Deep enough to absorb a burst of events, shallow enough that
# submit() rarely has to block the detection thread and that a shutdown does
# not leave a meaningful backlog behind.
DEFAULT_MAX_PENDING = 64


class _Job:
    """One queued unit of post-processing work."""

    __slots__ = ('fn', 'args', 'kwargs', 'priority', 'sequence', 'label', 'submitted_at')

    def __init__(
        self,
        fn: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        priority: int,
        sequence: int,
        label: str,
    ):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.priority = priority
        self.sequence = sequence
        self.label = label
        self.submitted_at = time.monotonic()

    def __lt__(self, other: '_Job') -> bool:
        # Priority first, then FIFO within a tier: heapq needs a total order,
        # and the sequence tiebreak is what makes equal-priority jobs fair.
        return (self.priority, self.sequence) < (other.priority, other.sequence)


class PostProcessPool:
    """A fixed-size, priority-aware, bounded worker pool.

    Usage mirrors the ThreadPoolExecutor it replaces, with two additions:
    ``priority`` (see the PRIORITY_* constants) and a boolean return from
    ``submit`` so a caller that would rather skip than block can opt out via
    ``block=False``.
    """

    def __init__(
        self,
        name: str,
        *,
        max_workers: int = 2,
        max_pending: int = DEFAULT_MAX_PENDING,
    ):
        self._name = name
        self._max_workers = max(1, int(max_workers))
        self._max_pending = max(1, int(max_pending))
        # A Condition rather than a Queue: the worker must distinguish "the
        # pool is shutting down, exit now" from "the queue is empty, park",
        # and a higher-priority arrival has to wake it immediately.
        self._condition = threading.Condition()
        self._ready: list[_Job] = []
        self._pending = 0
        self._sequence = 0
        self._shutdown = False
        self._threads: list[threading.Thread] = []
        self._inflight = 0
        # Metrics. Guarded by _condition; read via stats().
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._rejected = 0
        self._run_seconds = 0.0
        self._wait_seconds = 0.0

    # ── lifecycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        """Spin up the workers. Idempotent."""
        with self._condition:
            if self._threads or self._shutdown:
                return
            for index in range(self._max_workers):
                thread = threading.Thread(
                    target=self._worker,
                    name=f'{self._name}-{index}',
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Stop accepting work and let in-flight jobs finish.

        Pending jobs are abandoned rather than drained: on a service restart
        nobody is going to read a clip that finishes rendering after the
        process would have exited anyway, and blocking shutdown on a deep
        backlog is how a deploy turns into a hang.
        """
        with self._condition:
            if self._shutdown:
                return
            self._shutdown = True
            dropped = self._pending
            self._ready.clear()
            self._pending = 0
            self._rejected += dropped
            self._condition.notify_all()
            threads = list(self._threads)
        if dropped:
            logger.info(
                '%s pool shut down, abandoning %d queued job(s)', self._name, dropped,
            )
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    # ── submission ──────────────────────────────────────────────────────

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        priority: int = PRIORITY_BACKGROUND,
        label: str = '',
        block: bool = True,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> bool:
        """Queue ``fn(*args, **kwargs)``. Returns False if it was rejected.

        Rejection happens only in two cases: the pool is shut down, or
        ``block=False`` and the backlog is full. A blocking submit waits for
        room, which is the backpressure mechanism - the detection thread slows
        down instead of the process growing an unbounded backlog.
        """
        job = _Job(fn, args, kwargs, int(priority), 0, label or getattr(fn, '__name__', 'job'))
        with self._condition:
            if self._shutdown:
                self._rejected += 1
                return False
            if self._pending >= self._max_pending and not block:
                self._rejected += 1
                return False
            job.sequence = self._sequence
            self._sequence += 1
            if self._pending >= self._max_pending:
                # Backpressure: hold the submitting thread until a worker (or a
                # shutdown) makes room. The deadline guard stops a caller that
                # passed no timeout from parking forever on a wedged pool.
                deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
                while self._pending >= self._max_pending and not self._shutdown:
                    if deadline is not None and time.monotonic() >= deadline:
                        self._rejected += 1
                        return False
                    self._condition.wait(timeout=0.05)
                if self._shutdown:
                    self._rejected += 1
                    return False
            self._ready.append(job)
            self._pending += 1
            self._submitted += 1
            self._condition.notify()
        return True

    # ── worker ──────────────────────────────────────────────────────────

    def _take(self) -> Optional[_Job]:
        """Pop the highest-priority job, or None when shutting down.

        A linear scan for the minimum rather than heapq: the backlog is
        bounded by max_pending (64 by default) and this is not a hot loop, so
        the queue stays trivially inspectable for tests and the priority
        ordering is obvious at a glance.
        """
        with self._condition:
            while True:
                if not self._ready:
                    if self._shutdown:
                        return None
                    self._condition.wait(timeout=0.25)
                    continue
                best = 0
                for index in range(1, len(self._ready)):
                    if self._ready[index] < self._ready[best]:
                        best = index
                job = self._ready.pop(best)
                self._pending -= 1
                self._inflight += 1
                # Queue wait is tracked apart from run time, so a slow job and
                # a starved pool stay distinguishable in stats().
                self._wait_seconds += time.monotonic() - job.submitted_at
                return job

    def _worker(self) -> None:
        while True:
            job = self._take()
            if job is None:
                return
            started = time.monotonic()
            try:
                job.fn(*job.args, **job.kwargs)
            except Exception:
                # A failed render must not take the worker down with it: the
                # rest of the queue still needs draining, and the event's own
                # error handling has already recorded whatever went wrong.
                logger.warning('%s pool job %s failed', self._name, job.label, exc_info=True)
                with self._condition:
                    self._failed += 1
            else:
                with self._condition:
                    self._completed += 1
            finally:
                with self._condition:
                    self._run_seconds += time.monotonic() - started
                    self._inflight -= 1
                    self._condition.notify()

    # ── introspection ───────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """A snapshot for diagnostics and the status endpoints."""
        with self._condition:
            completed = self._completed
            failed = self._failed
            return {
                'name': self._name,
                'max_workers': self._max_workers,
                'max_pending': self._max_pending,
                'pending': self._pending,
                'inflight': self._inflight,
                'submitted': self._submitted,
                'completed': completed,
                'failed': failed,
                'rejected': self._rejected,
                'shutdown': self._shutdown,
                'run_seconds': round(self._run_seconds, 3),
                'queue_wait_seconds': round(self._wait_seconds, 3),
                'mean_job_seconds': (
                    round(self._run_seconds / (completed + failed), 3) if (completed + failed) else None
                ),
            }


# ── Process-wide pools ────────────────────────────────────────────────────
# Two pools rather than one, because the work has genuinely different urgency
# and different cost. Sharing a single pool would let a burst of cheap face
# enrichments delay a clip render the user is waiting on, which is exactly the
# inversion the priority tier exists to prevent - but only within one pool.
# Keeping them separate also means a wedged clip queue cannot starve identity
# bookkeeping, and vice versa.

_clip_pool: Optional[PostProcessPool] = None
_enrichment_pool: Optional[PostProcessPool] = None
_pool_lock = threading.Lock()

# ffmpeg clip render / audio mux. Deliberately small: each job is an external
# process doing decode+encode+remux, so these are IO- and CPU-heavy and
# oversubscribing them is what causes the stall in the first place. Two is
# enough to keep a second clip moving while one waits on a muxer, without
# letting a burst of twelve events run twelve renders at once.
CLIP_POOL_WORKERS = 2
CLIP_POOL_MAX_PENDING = 32

# Face-embedding writes and unknown-face captures. Cheap relative to a render,
# nobody is waiting on them, and the callers are the detection hot path, so a
# slightly wider pool keeps the queue from ever applying backpressure to
# detection.
ENRICHMENT_POOL_WORKERS = 2
ENRICHMENT_POOL_MAX_PENDING = 64


def clip_pool() -> PostProcessPool:
    """The shared event-clip pool, created on first use."""
    global _clip_pool
    with _pool_lock:
        if _clip_pool is None:
            _clip_pool = PostProcessPool(
                'postprocess-clip',
                max_workers=CLIP_POOL_WORKERS,
                max_pending=CLIP_POOL_MAX_PENDING,
            )
            _clip_pool.start()
        return _clip_pool


def enrichment_pool() -> PostProcessPool:
    """The shared face-enrichment pool, created on first use."""
    global _enrichment_pool
    with _pool_lock:
        if _enrichment_pool is None:
            _enrichment_pool = PostProcessPool(
                'postprocess-enrichment',
                max_workers=ENRICHMENT_POOL_WORKERS,
                max_pending=ENRICHMENT_POOL_MAX_PENDING,
            )
            _enrichment_pool.start()
        return _enrichment_pool


def shutdown_pools(timeout: float = 5.0) -> None:
    """Stop both pools at service shutdown, abandoning queued work."""
    global _clip_pool, _enrichment_pool
    with _pool_lock:
        pools = [pool for pool in (_clip_pool, _enrichment_pool) if pool is not None]
        _clip_pool = None
        _enrichment_pool = None
    for pool in pools:
        pool.shutdown(timeout=timeout)


def pool_stats() -> dict[str, Any]:
    """Both pools' stats, for diagnostics and the status endpoints."""
    with _pool_lock:
        pools = [pool for pool in (_clip_pool, _enrichment_pool) if pool is not None]
    return {pool.stats()['name']: pool.stats() for pool in pools}
