"""Bounded worker pool for expensive post-processing jobs.

Several expensive operations happen after event detection (clip finalization,
face enrichment, and outbound notifications). A thread-per-job design has no
ceiling, so this module provides fixed worker counts, bounded backlogs, and
non-blocking rejection for latency-sensitive callers.

The caller injects each job body, keeping this module independent of the app's
camera, database, and detection services.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger('daygle.ai')

# Priority tiers. Lower runs first. A clip the user asked for beats background
# enrichment within a shared pool; dedicated pools can isolate unrelated work.
PRIORITY_CLIP = 0
PRIORITY_BACKGROUND = 10
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
        return (self.priority, self.sequence) < (other.priority, other.sequence)


class PostProcessPool:
    """A fixed-size, priority-aware, bounded worker pool."""

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
        self._condition = threading.Condition()
        self._ready: list[_Job] = []
        self._pending = 0
        self._sequence = 0
        self._shutdown = False
        self._threads: list[threading.Thread] = []
        self._inflight = 0
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._rejected = 0
        self._run_seconds = 0.0
        self._wait_seconds = 0.0

    def start(self) -> None:
        """Spin up workers. Idempotent."""
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

    def wait_until_idle(self, timeout: float = 10.0) -> bool:
        """Wait for queued and running jobs to finish; return False on timeout."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while self._pending or self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Stop accepting work, abandoning queued jobs and joining workers boundedly."""
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
            logger.info('%s pool shut down, abandoning %d queued job(s)', self._name, dropped)
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

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
        """Queue a job, returning False if shut down or a nonblocking queue is full."""
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

    def _take(self) -> Optional[_Job]:
        """Pop the highest-priority job, or None when shutting down."""
        with self._condition:
            while True:
                if not self._ready:
                    if self._shutdown:
                        return None
                    self._condition.wait(timeout=0.25)
                    continue
                best = min(range(len(self._ready)), key=self._ready.__getitem__)
                job = self._ready.pop(best)
                self._pending -= 1
                self._inflight += 1
                # A slot opened for a blocked producer; notify one waiter only.
                self._condition.notify()
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
                    # Wake idle barriers and any blocked submitters.
                    self._condition.notify_all()

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


# Separate pools keep expensive clip work, identity bookkeeping, and alert
# deliveries from starving one another during bursts.
_clip_pool: Optional[PostProcessPool] = None
_enrichment_pool: Optional[PostProcessPool] = None
_notification_pool: Optional[PostProcessPool] = None
_pool_lock = threading.Lock()

CLIP_POOL_WORKERS = 2
CLIP_POOL_MAX_PENDING = 32
ENRICHMENT_POOL_WORKERS = 2
ENRICHMENT_POOL_MAX_PENDING = 64
NOTIFICATION_POOL_WORKERS = 2
NOTIFICATION_POOL_MAX_PENDING = 128


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


def notification_pool() -> PostProcessPool:
    """The bounded asynchronous pool for rule-specific alert deliveries."""
    global _notification_pool
    with _pool_lock:
        if _notification_pool is None or _notification_pool.stats()['shutdown']:
            _notification_pool = PostProcessPool(
                'alert-notification',
                max_workers=NOTIFICATION_POOL_WORKERS,
                max_pending=NOTIFICATION_POOL_MAX_PENDING,
            )
            _notification_pool.start()
        return _notification_pool


def shutdown_pools(timeout: float = 5.0) -> None:
    """Stop all pools at service shutdown, abandoning remaining queued work."""
    global _clip_pool, _enrichment_pool, _notification_pool
    with _pool_lock:
        pools = [pool for pool in (_clip_pool, _enrichment_pool, _notification_pool) if pool is not None]
        _clip_pool = None
        _enrichment_pool = None
        _notification_pool = None
    for pool in pools:
        pool.shutdown(timeout=timeout)


def pool_stats() -> dict[str, Any]:
    """All created pools' stats, for diagnostics and the status endpoints."""
    with _pool_lock:
        pools = [pool for pool in (_clip_pool, _enrichment_pool, _notification_pool) if pool is not None]
    return {pool.stats()['name']: pool.stats() for pool in pools}
