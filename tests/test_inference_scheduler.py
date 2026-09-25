"""Tests for the central live-inference scheduler.

Background detection used to fan out one daemon thread per camera, each of
which blocked on the shared detector's inference semaphore. These tests pin the
properties that replace that design: one job per camera with the newest
winning, fair round-robin between cameras, priority for cameras that are
recording, a single global concurrency limit, and queue wait measured
separately from run time.

The module has no app imports, so these run without cv2 / ONNX Runtime.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.inference_scheduler import LiveInferenceScheduler  # noqa: E402


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class _Clock:
    """Manually advanced clock so timing assertions are deterministic."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Gate:
    """A job that blocks its worker until released."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def read_frame(self):
        return 'frame', {'width': 10, 'height': 10}

    def run(self, _image, _frame, _settings):
        self.started.set()
        self.release.wait(timeout=2)


def _scheduler(runner, **kwargs):
    scheduler = LiveInferenceScheduler(runner, **kwargs)
    scheduler.start()
    return scheduler


# ─── Latest frame wins ────────────────────────────────────────────────────


def test_newest_submission_replaces_the_queued_one():
    """Two requests for one camera, the second newer: only the newer runs.
    A camera must never accumulate a backlog of stale work."""
    order: list[str] = []
    gate = _Gate()
    scheduler = _scheduler(lambda i, f, s: order.append(s['tag']) or gate.run(i, f, s))
    try:
        scheduler.submit('blocker', {'tag': 'blocker'}, gate.read_frame)
        assert gate.started.wait(timeout=2)

        scheduler.submit('cam', {'tag': 'old'}, lambda: ('frame', {}))
        scheduler.submit('cam', {'tag': 'new'}, lambda: ('frame', {}))
        assert scheduler.pending_cameras() == ['cam'], 'one job per camera, never a queue'

        gate.release.set()
        assert _wait_for(lambda: 'new' in order)
        time.sleep(0.05)
        assert order == ['blocker', 'new']
        assert scheduler.stats()['superseded'] == 1
    finally:
        gate.release.set()
        scheduler.stop()


def test_frame_is_read_when_the_job_runs_not_when_it_is_queued():
    """A job that waited behind a backlog must score the newest frame, not the
    one that existed when it was submitted."""
    reads: list[int] = []
    gate = _Gate()
    scheduler = _scheduler(lambda i, f, s: gate.run(i, f, s))
    try:
        scheduler.submit('blocker', {}, gate.read_frame)
        assert gate.started.wait(timeout=2)
        scheduler.submit('cam', {}, lambda: (reads.append(1), ('frame', {}))[1])
        assert reads == [], 'the frame must not be grabbed at submit time'
        gate.release.set()
        assert _wait_for(lambda: reads == [1])
    finally:
        gate.release.set()
        scheduler.stop()


# ─── Fairness and priority ────────────────────────────────────────────────


def test_pending_cameras_are_served_round_robin():
    order: list[str] = []
    gate = _Gate()
    scheduler = _scheduler(lambda i, f, s: order.append(str(s['id'])) or gate.run(i, f, s))
    try:
        scheduler.submit('blocker', {'id': 'blocker'}, gate.read_frame)
        assert gate.started.wait(timeout=2)
        for camera_id in ('a', 'b', 'c'):
            scheduler.submit(camera_id, {'id': camera_id}, lambda: ('frame', {}))
        gate.release.set()
        assert _wait_for(lambda: len(order) == 4)
        assert order[1:] == ['a', 'b', 'c']
    finally:
        gate.release.set()
        scheduler.stop()


def test_recording_camera_is_served_before_an_older_idle_request():
    """An event clip already being written is worth finishing before a quiet
    camera's next check."""
    order: list[str] = []
    priority: set[str] = set()
    gate = _Gate()
    scheduler = _scheduler(
        lambda i, f, s: order.append(str(s['id'])) or gate.run(i, f, s),
        is_priority=lambda camera_id: camera_id in priority,
    )
    try:
        scheduler.submit('blocker', {'id': 'blocker'}, gate.read_frame)
        assert gate.started.wait(timeout=2)
        scheduler.submit('idle', {'id': 'idle'}, lambda: ('frame', {}))
        priority.add('recording')
        scheduler.submit('recording', {'id': 'recording'}, lambda: ('frame', {}))
        gate.release.set()
        assert _wait_for(lambda: len(order) == 3)
        assert order[1:] == ['recording', 'idle']
    finally:
        gate.release.set()
        scheduler.stop()


def test_stale_camera_outranks_a_healthy_one_without_a_priority_flag():
    """A camera whose previous job finished more than two intervals ago is
    already falling behind and goes next."""
    order: list[str] = []
    clock = _Clock()
    gate = _Gate()
    scheduler = _scheduler(
        lambda i, f, s: order.append(str(s['id'])),
        clock=clock,
    )
    try:
        scheduler.submit('stale', {'id': 'stale'}, lambda: ('frame', {}), interval=0.5)
        assert _wait_for(lambda: order == ['stale'])
        clock.advance(5.0)  # 10x its 0.5s interval since it last finished
        order.clear()
        # Hold the worker so both requests are queued at the same instant and
        # the tie is broken by priority, not by which one arrived first.
        scheduler.submit(
            'blocker', {'id': 'blocker'}, gate.read_frame,
            runner=lambda image, frame, settings: gate.run(image, frame, settings),
        )
        assert gate.started.wait(timeout=2)
        scheduler.submit('fresh', {'id': 'fresh'}, lambda: ('frame', {}), interval=0.5)
        scheduler.submit('stale', {'id': 'stale'}, lambda: ('frame', {}), interval=0.5)
        gate.release.set()
        # The blocker's per-job runner does not record an id, so the two
        # recorded entries are the two that were queued behind it.
        assert _wait_for(lambda: order == ['stale', 'fresh']), order
        assert order == ['stale', 'fresh']
    finally:
        gate.release.set()
        scheduler.stop()


# ─── Concurrency ──────────────────────────────────────────────────────────


def test_only_max_workers_jobs_run_at_once():
    entered: list[str] = []
    release = threading.Event()
    scheduler = _scheduler(
        lambda i, f, s: (entered.append(str(s['id'])), release.wait(timeout=2)),
        max_workers=2,
    )
    try:
        for camera_id in ('a', 'b', 'c', 'd'):
            scheduler.submit(camera_id, {'id': camera_id}, lambda: ('frame', {}))
        assert _wait_for(lambda: len(entered) == 2)
        time.sleep(0.05)
        assert len(entered) == 2, 'the global limit must be enforced at admission'
        release.set()
        assert _wait_for(lambda: len(entered) == 4)
    finally:
        release.set()
        scheduler.stop()


def test_raising_the_limit_starts_more_workers():
    entered: list[str] = []
    release = threading.Event()
    scheduler = _scheduler(
        lambda i, f, s: (entered.append(str(s['id'])), release.wait(timeout=2)),
        max_workers=1,
    )
    try:
        for camera_id in ('a', 'b', 'c'):
            scheduler.submit(camera_id, {'id': camera_id}, lambda: ('frame', {}))
        assert _wait_for(lambda: len(entered) == 1)
        scheduler.set_max_workers(3)
        assert _wait_for(lambda: len(entered) == 3)
        assert scheduler.max_workers == 3
    finally:
        release.set()
        scheduler.stop()


# ─── Timing ───────────────────────────────────────────────────────────────


def test_queue_wait_and_run_time_are_recorded_separately():
    """A slow model and a starved queue are different problems; averaging them
    into one number hides both."""
    clock = _Clock()
    gate = _Gate()
    scheduler = _scheduler(
        lambda i, f, s: (clock.advance(3.0), gate.run(i, f, s))[1],
        clock=clock,
    )
    try:
        scheduler.submit('blocker', {}, gate.read_frame)
        assert gate.started.wait(timeout=2)
        scheduler.submit('cam', {}, lambda: ('frame', {}))
        clock.advance(2.0)  # the queued job waits this long behind the blocker
        gate.release.set()
        assert _wait_for(lambda: scheduler.last_timing('cam') is not None)
        timing = scheduler.last_timing('cam')
        assert timing['wait_seconds'] == 2.0
        assert timing['run_seconds'] == 3.0
    finally:
        gate.release.set()
        scheduler.stop()


def test_completion_hook_receives_the_timing():
    seen: dict[str, dict] = {}
    scheduler = _scheduler(
        lambda i, f, s: None,
        on_complete=lambda camera_id, timing: seen.__setitem__(camera_id, timing),
    )
    try:
        scheduler.submit('cam', {}, lambda: ('frame', {}))
        assert _wait_for(lambda: 'cam' in seen)
        assert 'wait_seconds' in seen['cam'] and 'run_seconds' in seen['cam']
    finally:
        scheduler.stop()


# ─── Failure handling and lifecycle ───────────────────────────────────────


def test_a_failing_job_does_not_kill_the_worker():
    order: list[str] = []

    def runner(_image, _frame, settings):
        order.append(str(settings['id']))
        if settings['id'] == 'boom':
            raise RuntimeError('provider exploded')

    scheduler = _scheduler(runner)
    try:
        scheduler.submit('boom', {'id': 'boom'}, lambda: ('frame', {}))
        assert _wait_for(lambda: 'boom' in order)
        scheduler.submit('after', {'id': 'after'}, lambda: ('frame', {}))
        assert _wait_for(lambda: 'after' in order), 'the worker must survive a failed cycle'
        assert scheduler.stats()['failed'] == 1
        assert scheduler.stats()['completed'] == 1
    finally:
        scheduler.stop()


def test_a_camera_with_no_frame_is_released_not_left_busy():
    released: list[str] = []
    scheduler = _scheduler(
        lambda i, f, s: None,
        on_release=lambda camera_id: released.append(camera_id),
    )
    try:
        scheduler.submit('cam', {}, lambda: None)
        assert _wait_for(lambda: released == ['cam'])
        assert scheduler.stats()['no_frame'] == 1
        assert not scheduler.is_busy('cam')
    finally:
        scheduler.stop()


def test_claim_and_release_bracket_the_job():
    events: list[str] = []
    scheduler = _scheduler(
        lambda i, f, s: events.append('run'),
        on_claim=lambda camera_id: events.append(f'claim:{camera_id}'),
        on_release=lambda camera_id: events.append(f'release:{camera_id}'),
    )
    try:
        scheduler.submit('cam', {}, lambda: ('frame', {}))
        assert _wait_for(lambda: len(events) == 3)
        assert events == ['claim:cam', 'run', 'release:cam']
    finally:
        scheduler.stop()


def test_stop_releases_queued_cameras_and_refuses_new_work():
    released: list[str] = []
    gate = _Gate()
    scheduler = _scheduler(
        lambda i, f, s: gate.run(i, f, s),
        on_release=lambda camera_id: released.append(camera_id),
    )
    scheduler.submit('blocker', {}, gate.read_frame)
    assert gate.started.wait(timeout=2)
    scheduler.submit('queued', {}, lambda: ('frame', {}))
    assert scheduler.is_busy('queued')

    # Stop while the worker is still occupied: 'queued' cannot be served, so it
    # must be dropped (and its camera released) rather than silently stranded.
    try:
        scheduler.stop(timeout=3)
    finally:
        gate.release.set()
    assert 'queued' in released, 'a dropped job must not leave its camera claimed forever'
    assert scheduler.stats()['dropped'] == 1
    assert scheduler.submit('after-stop', {}, lambda: ('frame', {})) is False
