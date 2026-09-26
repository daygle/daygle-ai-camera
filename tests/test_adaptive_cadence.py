"""Tests for adaptive detection cadence (Item 16).

The property that matters most is the safety one: a still camera may be
sampled less often, but a stationary subject must never be missed
indefinitely. Every stretching test is paired with a staleness-floor test, so
a future change that makes the system greedier fails here rather than in
production as a person standing unnoticed in a quiet driveway.

Pure-python module with no app imports, so these run in the sandbox.
"""

from __future__ import annotations

import pytest

from app.adaptive_cadence import (
    DEFAULT_MAX_STALE_SECONDS,
    MAX_STABILITY_STRETCH,
    AdaptiveCadenceTracker,
    stretch_for_load,
)


class Clock:
    """A manually advanced monotonic clock, so tests never sleep."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def tracker(clock: Clock) -> AdaptiveCadenceTracker:
    return AdaptiveCadenceTracker(max_stale_seconds=10.0, now=clock)


# ─── the safety floor ─────────────────────────────────────────────────────

def test_a_camera_with_no_motion_history_keeps_full_cadence(tracker: AdaptiveCadenceTracker) -> None:
    # Never seen motion: "unknown", not "stable". A brand-new camera must not
    # start on the slow cadence or it could miss its first subject.
    assert tracker.effective_interval('front', 0.5) == 0.5


def test_a_recently_motioning_camera_keeps_full_cadence(
    tracker: AdaptiveCadenceTracker,
    clock: Clock,
) -> None:
    tracker.note_cycle('front', had_motion=True)
    # Immediately after motion the scene is not quiet yet, so no stretching.
    assert tracker.effective_interval('front', 0.5) == 0.5
    clock.advance(1.0)
    assert tracker.effective_interval('front', 0.5) == 0.5


def test_cadence_stretches_only_after_a_long_quiet_streak(
    tracker: AdaptiveCadenceTracker,
    clock: Clock,
) -> None:
    tracker.note_cycle('front', had_motion=True)
    # Just under half the budget: the first half is deliberately untouched.
    clock.advance(4.9)
    tracker.note_cycle('front', had_motion=False)
    assert tracker.effective_interval('front', 0.5) == 0.5

    # Past the halfway point the interval starts growing.
    clock.advance(4.0)
    tracker.note_cycle('front', had_motion=False)
    stretched = tracker.effective_interval('front', 0.5)
    assert stretched > 0.5
    assert stretched <= 0.5 * MAX_STABILITY_STRETCH


def test_a_stationary_subject_is_never_missed_indefinitely(
    tracker: AdaptiveCadenceTracker,
    clock: Clock,
) -> None:
    # The core guarantee. Walk the clock forward the way a live monitor would,
    # sampling at whatever cadence the tracker asks for, and assert that the
    # elapsed time between consecutive samples never exceeds the budget.
    tracker.note_cycle('front', had_motion=True)
    max_gap = 0.0
    last = tracker._now()
    for _ in range(40):
        clock.advance(tracker.effective_interval('front', 0.5))
        tracker.note_sample('front')
        now = tracker._now()
        max_gap = max(max_gap, now - last)
        last = now
    assert max_gap <= DEFAULT_MAX_STALE_SECONDS + 0.001, f'gap of {max_gap}s exceeded the budget'


def test_a_camera_due_for_a_full_scan_returns_to_base_cadence(
    tracker: AdaptiveCadenceTracker,
    clock: Clock,
) -> None:
    tracker.note_cycle('front', had_motion=True)
    clock.advance(9.0)
    tracker.note_cycle('front', had_motion=False)
    assert tracker.effective_interval('front', 0.5) > 0.5

    # Long enough without a sample that the staleness floor forces a scan.
    clock.advance(DEFAULT_MAX_STALE_SECONDS)
    assert tracker.effective_interval('front', 0.5) == 0.5


def test_the_interval_never_exceeds_the_staleness_budget(
    tracker: AdaptiveCadenceTracker,
    clock: Clock,
) -> None:
    tracker.note_cycle('front', had_motion=True)
    clock.advance(8.0)
    tracker.note_cycle('front', had_motion=False)
    # A long base interval must not be stretched past the guarantee either.
    interval = tracker.effective_interval('front', 1.0)
    assert interval <= tracker._max_stale


def test_a_base_interval_already_longer_than_the_budget_is_never_slowed(
    tracker: AdaptiveCadenceTracker,
    clock: Clock,
) -> None:
    # If the operator asked for a 30s interval, that IS the contract; the
    # tracker must not second-guess it downward.
    tracker.note_cycle('front', had_motion=True)
    clock.advance(8.0)
    tracker.note_cycle('front', had_motion=False)
    assert tracker.effective_interval('front', 30.0) == 30.0


# ─── load-based stretching ────────────────────────────────────────────────

def test_stretch_for_load_is_flat_below_half_a_queue() -> None:
    assert stretch_for_load(0, 4) == 1.0
    assert stretch_for_load(2, 4) == 1.0


def test_stretch_for_load_ramps_then_saturates() -> None:
    # Halfway between the thresholds is halfway between the multipliers.
    mid = stretch_for_load(5, 4)  # ratio 1.25, midpoint of 0.5..2.0
    assert 1.0 < mid < MAX_STABILITY_STRETCH
    assert stretch_for_load(8, 4) == MAX_STABILITY_STRETCH
    assert stretch_for_load(999, 4) == MAX_STABILITY_STRETCH


def test_stretch_for_load_survives_nonsense_input() -> None:
    assert stretch_for_load(0, 0) == 1.0 or stretch_for_load(0, 0) >= 1.0
    assert stretch_for_load(-5, 4) == 1.0


def test_load_pressure_stretches_a_steady_camera_further(
    tracker: AdaptiveCadenceTracker,
    clock: Clock,
) -> None:
    tracker.note_cycle('front', had_motion=True)
    clock.advance(8.0)
    tracker.note_cycle('front', had_motion=False)

    idle = tracker.effective_interval('front', 0.5, pending=0, max_workers=2)
    loaded = tracker.effective_interval('front', 0.5, pending=8, max_workers=2)
    assert loaded > idle, 'a saturated queue should buy more relief on a still camera'


# ─── motion resets the streak ─────────────────────────────────────────────

def test_motion_resets_the_quiet_streak(
    tracker: AdaptiveCadenceTracker,
    clock: Clock,
) -> None:
    tracker.note_cycle('front', had_motion=True)
    clock.advance(8.0)
    tracker.note_cycle('front', had_motion=False)
    assert tracker.effective_interval('front', 0.5) > 0.5

    # Something moves again: back to full rate immediately.
    tracker.note_cycle('front', had_motion=True)
    assert tracker.effective_interval('front', 0.5) == 0.5


def test_cameras_are_tracked_independently(
    tracker: AdaptiveCadenceTracker,
    clock: Clock,
) -> None:
    # Two cameras running their own cycles: one goes quiet, one keeps seeing
    # motion. Each cadence must follow its own camera, not a shared streak.
    tracker.note_cycle('front', had_motion=True)
    tracker.note_cycle('driveway', had_motion=True)
    clock.advance(8.0)
    tracker.note_cycle('front', had_motion=False)
    tracker.note_cycle('driveway', had_motion=True)

    assert tracker.effective_interval('front', 0.5) > 0.5
    assert tracker.effective_interval('driveway', 0.5) == 0.5


# ─── lifecycle ────────────────────────────────────────────────────────────

def test_clearing_a_camera_drops_its_quiet_streak(tracker: AdaptiveCadenceTracker, clock: Clock) -> None:
    # A removed-and-re-added camera must not inherit "quiet for 10 minutes"
    # and resume on the slow cadence.
    tracker.note_cycle('front', had_motion=True)
    clock.advance(8.0)
    tracker.note_cycle('front', had_motion=False)
    assert tracker.effective_interval('front', 0.5) > 0.5

    tracker.clear_camera('front')
    assert tracker.effective_interval('front', 0.5) == 0.5


def test_an_override_pins_the_interval(tracker: AdaptiveCadenceTracker, clock: Clock) -> None:
    tracker.note_cycle('front', had_motion=True)
    clock.advance(8.0)
    tracker.note_cycle('front', had_motion=False)

    # An override may speed a camera back UP - forcing one to full rate is
    # exactly what it is for - as well as hold it slow.
    tracker.set_override('front', 0.25)
    assert tracker.effective_interval('front', 0.5) == 0.25

    tracker.set_override('front', 0.75)
    assert tracker.effective_interval('front', 0.5) == 0.75

    tracker.set_override('front', None)
    assert tracker.effective_interval('front', 0.5) > 0.5


def test_an_override_cannot_exceed_the_staleness_ceiling(
    tracker: AdaptiveCadenceTracker,
    clock: Clock,
) -> None:
    tracker.note_cycle('front', had_motion=True)
    clock.advance(8.0)
    tracker.note_cycle('front', had_motion=False)
    # Asking for a 60s pinned interval on a still camera must not breach the
    # guarantee, so the ceiling clamps it.
    tracker.set_override('front', 60.0)
    assert tracker.effective_interval('front', 0.5) == tracker._max_stale


def test_stats_report_quiet_time_and_overrides(
    tracker: AdaptiveCadenceTracker,
    clock: Clock,
) -> None:
    tracker.note_cycle('front', had_motion=True)
    clock.advance(3.0)
    tracker.set_override('front', 1.0)
    stats = tracker.stats()
    assert stats['max_stale_seconds'] == 10.0
    assert stats['cameras']['front']['quiet_for_seconds'] == pytest.approx(3.0)
    assert stats['cameras']['front']['overridden'] is True


def test_set_max_stale_seconds_tightens_the_guarantee(
    tracker: AdaptiveCadenceTracker,
    clock: Clock,
) -> None:
    tracker.set_max_stale_seconds(2.0)
    tracker.note_cycle('front', had_motion=True)
    clock.advance(1.8)
    tracker.note_cycle('front', had_motion=False)
    # With a 2s budget, a 1.8s quiet streak is already deep into the ramp.
    assert tracker.effective_interval('front', 0.5) > 0.5
    assert tracker.effective_interval('front', 0.5) <= 2.0
