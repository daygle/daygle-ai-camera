"""Tests for the bounded post-processing worker pool (Item 12).

The behaviours that matter are the ones a thread-per-job fan-out cannot
provide: a hard concurrency ceiling, a bounded backlog, priority for clip
finalization over background enrichment, prompt shutdown, and metrics that
make a slow recorder measurable rather than anecdotal.

Pure-python module with no app imports, so these run in the sandbox.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.postprocess_pool import (
    PRIORITY_BACKGROUND,
    PRIORITY_CLIP,
    PostProcessPool,
)


@pytest.fixture()
def pool():
    created = PostProcessPool('test-post', max_workers=2, max_pending=8)
    created.start()
    try:
        yield created
    finally:
        created.shutdown(timeout=2.0)


class _Flag:
    """A settable boolean, for a submitter thread that records its result."""

    def __init__(self) -> None:
        self.value = None

    def is_set(self) -> bool:
        return self.value is not None


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    """Poll ``predicate`` until it holds, so tests never race the workers."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _drain(pool: PostProcessPool, timeout: float = 5.0) -> bool:
    """Wait until nothing is pending or in flight."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = pool.stats()
        if snapshot['pending'] == 0 and snapshot['inflight'] == 0:
            return True
        time.sleep(0.01)
    return False


# ─── concurrency ceiling ───────────────────────────────────────────────────

def test_pool_never_exceeds_max_workers() -> None:
    pool = PostProcessPool('cap', max_workers=3, max_pending=32)
    pool.start()
    try:
        lock = threading.Lock()
        state = {'current': 0, 'peak': 0}
        release = threading.Event()

        def job() -> None:
            with lock:
                state['current'] += 1
                state['peak'] = max(state['peak'], state['current'])
            release.wait(2.0)
            with lock:
                state['current'] -= 1

        for _ in range(20):
            pool.submit(job)

        # Let the workers pile up against the release gate.
        time.sleep(0.3)
        with lock:
            assert state['peak'] <= 3, f'ran {state["peak"]} jobs at once, limit was 3'
        release.set()
        assert _drain(pool)
        assert pool.stats()['completed'] == 20
    finally:
        release.set()
        pool.shutdown(timeout=2.0)


def test_single_worker_pool_is_fully_serial() -> None:
    pool = PostProcessPool('serial', max_workers=1, max_pending=16)
    pool.start()
    try:
        lock = threading.Lock()
        state = {'current': 0, 'peak': 0}
        release = threading.Event()

        def job() -> None:
            with lock:
                state['current'] += 1
                state['peak'] = max(state['peak'], state['current'])
            release.wait(2.0)
            with lock:
                state['current'] -= 1

        for _ in range(6):
            pool.submit(job)
        time.sleep(0.3)
        with lock:
            assert state['peak'] == 1
        release.set()
        assert _drain(pool)
    finally:
        release.set()
        pool.shutdown(timeout=2.0)


# ─── bounded backlog / backpressure ───────────────────────────────────────

def test_wait_until_idle_includes_queued_and_running_jobs() -> None:
    pool = PostProcessPool('idle-barrier', max_workers=1, max_pending=2)
    pool.start()
    release = threading.Event()
    started = threading.Event()

    def job() -> None:
        started.set()
        release.wait(2.0)

    try:
        assert pool.submit(job)
        assert started.wait(1.0)
        assert pool.submit(lambda: None)
        assert pool.wait_until_idle(timeout=0.02) is False
        release.set()
        assert pool.wait_until_idle(timeout=2.0) is True
    finally:
        release.set()
        pool.shutdown(timeout=2.0)


def test_alert_notification_dispatch_is_async_bounded_and_waitable(monkeypatch) -> None:
    import app.alert_dispatch as dispatch
    import app.postprocess_pool as pools

    pool = PostProcessPool('alert-bounded-test', max_workers=1, max_pending=1)
    pool.start()
    monkeypatch.setattr(pools, 'notification_pool', lambda: pool)
    monkeypatch.setattr(pools, '_notification_pool', pool)
    release = threading.Event()
    started = threading.Event()

    def delivery(_triggered, _event_id, _rules):
        started.set()
        release.wait(2.0)

    try:
        assert dispatch.submit_alert_notification(delivery, [{'label': 'person'}], 1, []) is True
        assert started.wait(1.0)
        assert dispatch.submit_alert_notification(delivery, [{'label': 'car'}], 2, []) is True
        started_at = time.monotonic()
        assert dispatch.submit_alert_notification(delivery, [], 3, []) is False
        assert time.monotonic() - started_at < 0.2, 'full queues must not block detection callbacks'
        assert pool.stats()['pending'] == 1
        assert dispatch.wait_for_pending_alert_notifications(timeout=0.02) is None
        assert pool.stats()['inflight'] == 1
        release.set()
        assert pool.wait_until_idle(timeout=2.0) is True
        assert pool.stats()['rejected'] == 1
    finally:
        release.set()
        pool.shutdown(timeout=2.0)


def test_non_blocking_submit_is_rejected_once_the_backlog_is_full() -> None:
    pool = PostProcessPool('bounded', max_workers=1, max_pending=2)
    pool.start()
    try:
        release = threading.Event()
        blocker = lambda: release.wait(2.0)  # noqa: E731

        assert pool.submit(blocker) is True          # becomes inflight
        assert pool.submit(blocker) is True          # first queued
        assert pool.submit(blocker) is True          # second queued, now full
        # Backlog is full: a non-blocking submit is refused rather than queued.
        assert pool.submit(blocker, block=False) is False

        stats = pool.stats()
        assert stats['rejected'] == 1
        assert stats['pending'] <= 2
        release.set()
        assert _drain(pool)
    finally:
        release.set()
        pool.shutdown(timeout=2.0)


def test_blocking_submit_waits_for_room_then_accepts() -> None:
    pool = PostProcessPool('backpressure', max_workers=1, max_pending=1)
    pool.start()
    try:
        release = threading.Event()
        blocker = lambda: release.wait(2.0)  # noqa: E731

        # Occupy the single worker FIRST and wait until it has actually taken
        # the job. Submitting blind is racy: with max_pending=1 the second
        # submit can slip in before the worker wakes, in which case there is
        # no full backlog left to block on and the test proves nothing.
        pool.submit(blocker)
        assert _wait_for(lambda: pool.stats()['inflight'] == 1), 'worker never picked up the job'

        # Now the backlog is genuinely empty-and-busy, so this fills it.
        pool.submit(blocker)
        assert _wait_for(lambda: pool.stats()['pending'] == 1), 'backlog never filled'

        accepted = _Flag()

        def submitter() -> None:
            accepted.value = pool.submit(blocker, timeout=3.0)

        thread = threading.Thread(target=submitter)
        thread.start()
        time.sleep(0.2)
        assert not accepted.is_set(), 'submitter should be parked on a full backlog'

        release.set()
        thread.join(timeout=5.0)
        assert accepted.is_set()
        assert _drain(pool)
        assert pool.stats()['completed'] == 3
    finally:
        release.set()
        pool.shutdown(timeout=2.0)


def test_blocking_submit_gives_up_at_its_deadline() -> None:
    pool = PostProcessPool('deadline', max_workers=1, max_pending=1)
    pool.start()
    try:
        release = threading.Event()
        blocker = lambda: release.wait(2.0)  # noqa: E731
        pool.submit(blocker)
        assert _wait_for(lambda: pool.stats()['inflight'] == 1), 'worker never picked up the job'
        pool.submit(blocker)
        assert _wait_for(lambda: pool.stats()['pending'] == 1), 'backlog never filled'

        started = time.monotonic()
        assert pool.submit(blocker, timeout=0.3) is False
        assert time.monotonic() - started < 2.0, 'submit must honour its timeout'
        release.set()
        assert _drain(pool)
    finally:
        release.set()
        pool.shutdown(timeout=2.0)


# ─── priority ─────────────────────────────────────────────────────────────

def test_clip_jobs_jump_ahead_of_background_jobs() -> None:
    pool = PostProcessPool('priority', max_workers=1, max_pending=16)
    pool.start()
    try:
        order: list[str] = []
        release = threading.Event()
        blocker = lambda: release.wait(2.0)  # noqa: E731

        # Occupy the single worker and fill the queue with background work.
        pool.submit(blocker)
        for index in range(5):
            pool.submit(lambda i=index: order.append(f'bg-{i}'), priority=PRIORITY_BACKGROUND)

        # A clip arrives last but must run first among the queued jobs.
        pool.submit(lambda: order.append('clip'), priority=PRIORITY_CLIP)

        release.set()
        assert _drain(pool)
        assert order[0] == 'clip', f'clip job should run first, got {order}'
        assert sorted(order[1:]) == [f'bg-{i}' for i in range(5)]
    finally:
        release.set()
        pool.shutdown(timeout=2.0)


def test_equal_priority_jobs_stay_fifo() -> None:
    pool = PostProcessPool('fifo', max_workers=1, max_pending=16)
    pool.start()
    try:
        order: list[int] = []
        release = threading.Event()
        blocker = lambda: release.wait(2.0)  # noqa: E731

        pool.submit(blocker)
        for index in range(6):
            pool.submit(lambda i=index: order.append(i), priority=PRIORITY_CLIP)

        release.set()
        assert _drain(pool)
        assert order == list(range(6)), 'equal priority must not be reordered'
    finally:
        release.set()
        pool.shutdown(timeout=2.0)


# ─── failure isolation ────────────────────────────────────────────────────

def test_a_failing_job_does_not_kill_the_worker_or_the_queue() -> None:
    pool = PostProcessPool('failure', max_workers=1, max_pending=8)
    pool.start()
    try:
        done: list[int] = []

        def boom() -> None:
            raise RuntimeError('ffmpeg exploded')

        pool.submit(boom)
        for index in range(3):
            pool.submit(lambda i=index: done.append(i))

        assert _drain(pool)
        stats = pool.stats()
        assert stats['failed'] == 1
        assert stats['completed'] == 3
        assert done == [0, 1, 2], 'jobs after the failure must still run'
    finally:
        pool.shutdown(timeout=2.0)


# ─── shutdown ─────────────────────────────────────────────────────────────

def test_shutdown_abandons_the_backlog_and_is_prompt() -> None:
    pool = PostProcessPool('shutdown', max_workers=1, max_pending=32)
    pool.start()

    release = threading.Event()
    blocker = lambda: release.wait(3.0)  # noqa: E731
    pool.submit(blocker)
    for _ in range(20):
        pool.submit(blocker)

    started = time.monotonic()
    pool.shutdown(timeout=0.5)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f'shutdown took {elapsed:.2f}s; it must not drain the backlog'
    stats = pool.stats()
    assert stats['shutdown'] is True
    assert stats['pending'] == 0, 'queued jobs are abandoned, not drained'
    release.set()


def test_submit_after_shutdown_is_refused() -> None:
    pool = PostProcessPool('closed', max_workers=1, max_pending=4)
    pool.start()
    pool.shutdown(timeout=1.0)

    ran = []
    assert pool.submit(lambda: ran.append(1)) is False
    time.sleep(0.1)
    assert ran == [], 'a shut-down pool must not run work'
    assert pool.stats()['rejected'] == 1


def test_shutdown_is_idempotent() -> None:
    pool = PostProcessPool('idempotent', max_workers=1, max_pending=4)
    pool.start()
    pool.shutdown(timeout=1.0)
    pool.shutdown(timeout=1.0)
    assert pool.stats()['shutdown'] is True


def test_start_after_shutdown_does_not_resurrect_workers() -> None:
    pool = PostProcessPool('zombie', max_workers=2, max_pending=4)
    pool.start()
    pool.shutdown(timeout=1.0)
    before = len(pool.stats()['name'])  # cheap no-op read
    pool.start()
    ran = []
    assert pool.submit(lambda: ran.append(1)) is False
    time.sleep(0.1)
    assert ran == []
    assert before > 0


# ─── metrics ──────────────────────────────────────────────────────────────

def test_stats_track_completions_failures_and_depth() -> None:
    pool = PostProcessPool('metrics', max_workers=2, max_pending=8)
    pool.start()
    try:
        pool.submit(lambda: None)
        pool.submit(lambda: (_ for _ in ()).throw(ValueError('nope')))
        pool.submit(lambda: None)
        assert _drain(pool)

        stats = pool.stats()
        assert stats['submitted'] == 3
        assert stats['completed'] == 2
        assert stats['failed'] == 1
        assert stats['pending'] == 0
        assert stats['inflight'] == 0
        assert stats['max_workers'] == 2
        assert stats['mean_job_seconds'] is not None
        assert stats['run_seconds'] >= 0
    finally:
        pool.shutdown(timeout=2.0)


def test_stats_report_no_mean_before_any_job_finishes() -> None:
    pool = PostProcessPool('empty', max_workers=1, max_pending=4)
    assert pool.stats()['mean_job_seconds'] is None
    assert pool.stats()['submitted'] == 0


def test_queue_wait_is_tracked_apart_from_run_time() -> None:
    # The known duration of the job we submit. The bounds below are derived
    # from this CONSTANT rather than from the other measured bucket: job 2
    # waits for job 1 to finish, so queue_wait ~= one job and run ~= two jobs.
    # Asserting wait >= run/2 compares two wall-clock measurements that are
    # mathematically near-identical -- it passed with a 0.5 ms margin on an
    # idle machine and failed under any load, which is a coin flip, not a
    # test. Pinning to the sleep duration keeps the same intent with real
    # headroom on both sides.
    job_seconds = 0.4
    pool = PostProcessPool('timing', max_workers=1, max_pending=4)
    pool.start()
    try:
        # The first job genuinely occupies the single worker for a while, so
        # the second has to wait for it. Both buckets are then non-zero, which
        # is the point: a starved queue and a slow job are different problems
        # and must not collapse into one number.
        def slow() -> None:
            time.sleep(job_seconds)

        pool.submit(slow)
        pool.submit(slow)
        assert _drain(pool)
        stats = pool.stats()
        assert stats['queue_wait_seconds'] > 0
        assert stats['run_seconds'] > 0
        # The waiter queued for roughly one job, not a fraction of a second and
        # not the whole pool lifetime.
        assert stats['queue_wait_seconds'] >= job_seconds / 2
        assert stats['queue_wait_seconds'] <= job_seconds * 1.5
        # Both jobs actually ran, so the run bucket is a full job, not the
        # wait being mislabelled as run time.
        assert stats['run_seconds'] >= job_seconds
    finally:
        pool.shutdown(timeout=2.0)
