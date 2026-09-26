"""Tests for per-cycle pipeline stage timing (roadmap: measure before changing).

Pure-python module with no app imports, so these run in the sandbox.

The properties worth pinning are the ones an operator would otherwise discover
the hard way:

* a stage that never ran reads as ABSENT, not as instant, so "the tiling pass
  is free" cannot be confused with "tiling never ran";
* a stage that raised still records the time it burned;
* p95 is an observed sample, never an interpolation;
* and the unaccounted-overhead budget is the completeness alarm -- a large
  gap must be reported as over-budget rather than quietly averaged away.
"""

from __future__ import annotations

import pytest

from app import pipeline_timing
from app.pipeline_timing import (
    OVERHEAD_BUDGET_MS,
    STAGE_ALERTS,
    STAGE_EVENT,
    STAGE_INFERENCE,
    STAGE_MOTION,
    STAGE_TOTAL,
    STAGE_TRACKING,
    WINDOW_SIZE,
    StageTimer,
    clear_pipeline_timing,
    percentile,
    pipeline_timing_payload,
    record_frame_read,
    record_pipeline_cycle,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_pipeline_timing()
    yield
    clear_pipeline_timing()


def _timed(stages: dict[str, float], total: float) -> StageTimer:
    timer = StageTimer()
    for name, value in stages.items():
        timer.add(name, value)
    timer.mark_total(total)
    return timer


# ─── percentile ──────────────────────────────────────────────────────────────


def test_percentile_is_nearest_rank_not_interpolated():
    values = [float(v) for v in range(1, 101)]
    # ceil(0.95 * 100) = 95 -> the 95th slowest sample, i.e. values[94] = 95.0.
    # An interpolated percentile would report 95.05, a latency that was never
    # measured.
    assert percentile(values, 0.95) == 95.0
    assert percentile(values, 0.50) == 50.0
    assert percentile(values, 1.0) == 100.0


def test_p50_is_a_true_median_while_p95_stays_an_observed_sample():
    values = [float(v) for v in range(1, 21)]
    # Even count: the median is the average of the two middle samples, which is
    # the "typical cycle" number. The tail is never averaged.
    assert pipeline_timing._summarize(values)['p50'] == 10.5
    assert pipeline_timing._summarize(values)['p95'] == 19.0


def test_percentile_handles_empty_and_out_of_range():
    assert percentile([], 0.95) == 0.0
    assert percentile([5.0], 0.0) == 5.0
    assert percentile([5.0], 5.0) == 5.0
    assert percentile([5.0], -1.0) == 5.0


# ─── StageTimer ──────────────────────────────────────────────────────────────


def test_stage_context_manager_records_elapsed_milliseconds(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(pipeline_timing, '_clock', clock)
    timer = StageTimer()
    with timer.stage(STAGE_INFERENCE):
        clock.advance(0.012)  # 12 ms
    assert timer.stages() == {STAGE_INFERENCE: 12.0}


def test_unknown_stage_name_is_rejected_loudly():
    with pytest.raises(KeyError):
        StageTimer().stage('preprocess_images')


def test_repeated_stage_sums_because_the_stage_ran_twice():
    timer = _timed({STAGE_INFERENCE: 30.0}, total=100.0)
    timer.add(STAGE_INFERENCE, 12.0)
    assert timer.stages()[STAGE_INFERENCE] == 42.0


def test_a_stage_that_raised_still_records_its_cost(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(pipeline_timing, '_clock', clock)
    timer = StageTimer()
    with pytest.raises(RuntimeError):
        with timer.stage(STAGE_EVENT):
            clock.advance(0.4)  # 400 ms
            raise RuntimeError('ffmpeg exploded')
    # The exception still propagated...
    assert timer.stages()[STAGE_EVENT] == 400.0


def test_add_ignores_junk_rather_than_raising():
    timer = StageTimer()
    timer.add('not_a_stage', 5.0)
    timer.add(STAGE_MOTION, 'fast')
    timer.add(STAGE_MOTION, -1.0)
    assert timer.stages() == {}


def test_account_for_folds_in_the_detector_breakdown():
    timer = _timed({STAGE_INFERENCE: 40.0}, total=100.0)
    timer.account_for({'preprocess_ms': 4.0, 'inference_ms': 30.0, 'postprocess_ms': 6.0})
    stages = timer.stages()
    # The detector's real session measurement replaces the caller's wider
    # bracket; preprocess and postprocess become their own stages.
    assert stages[pipeline_timing.STAGE_PREPROCESS] == 4.0
    assert stages[pipeline_timing.STAGE_INFERENCE] == 30.0
    assert stages[pipeline_timing.STAGE_POSTPROCESS] == 6.0


def test_account_for_tolerates_a_detector_with_no_breakdown():
    timer = _timed({STAGE_INFERENCE: 40.0}, total=100.0)
    timer.account_for(None)
    timer.account_for({})
    timer.account_for('nonsense')
    assert timer.stages() == {STAGE_INFERENCE: 40.0}


def test_summary_reports_unaccounted_overhead():
    timer = _timed({STAGE_MOTION: 5.0, STAGE_INFERENCE: 20.0}, total=50.0)
    summary = timer.summary()
    assert summary['total_ms'] == 50.0
    assert summary['measured_ms'] == 25.0
    assert summary['unaccounted_ms'] == 25.0


# ─── recording / payload ─────────────────────────────────────────────────────


def test_payload_reports_per_stage_percentiles_in_pipeline_order():
    for index in range(20):
        record_pipeline_cycle(
            'cam-a',
            _timed({STAGE_MOTION: 1.0 + index, STAGE_INFERENCE: 10.0 + index}, total=40.0),
        )
    payload = pipeline_timing_payload('cam-a')
    assert payload['cycles_timed'] == 20
    assert list(payload['stages'])[:2] == [STAGE_MOTION, STAGE_INFERENCE]
    motion = payload['stages'][STAGE_MOTION]
    assert motion['p50'] == 10.5
    assert motion['max'] == 20.0
    assert motion['count'] == 20
    # ceil(0.95 * 20) = 19 -> values[18] = 19.0, not the 20.0 maximum.
    assert motion['p95'] == 19.0


def test_a_stage_that_never_ran_is_absent_not_zero():
    record_pipeline_cycle('cam-a', _timed({STAGE_MOTION: 2.0}, total=5.0))
    payload = pipeline_timing_payload('cam-a')
    assert STAGE_TRACKING not in payload['stages']


def test_overhead_budget_flags_an_incomplete_breakdown():
    # 30 ms of cycle, 5 ms measured: the instrumentation is missing something
    # and the operator must be told, not handed a confident-looking breakdown.
    record_pipeline_cycle('cam-a', _timed({STAGE_MOTION: 5.0}, total=30.0))
    payload = pipeline_timing_payload('cam-a')
    assert payload['unaccounted']['p95'] == 25.0
    assert payload['overhead_budget_ms'] == OVERHEAD_BUDGET_MS
    assert payload['over_budget'] is True


def test_overhead_budget_passes_for_a_complete_breakdown():
    for _ in range(5):
        record_pipeline_cycle('cam-a', _timed({STAGE_MOTION: 4.0, STAGE_INFERENCE: 20.0}, total=26.0))
    assert pipeline_timing_payload('cam-a')['over_budget'] is False


def test_record_frame_read_is_reported_outside_the_cycle_budget():
    for value in (2.0, 8.0, 5.0):
        record_frame_read('cam-a', value)
    record_pipeline_cycle('cam-a', _timed({STAGE_MOTION: 3.0}, total=4.0))
    payload = pipeline_timing_payload('cam-a')
    assert payload['frame_read']['p50'] == 5.0
    # It must not leak into the cycle total, which would make a starved
    # camera look like a slow one.
    assert payload['cycle']['max'] == 4.0
    assert payload['unaccounted']['max'] == 1.0


def test_frame_read_rejects_junk():
    record_frame_read('cam-a', 'slow')
    record_frame_read('cam-a', -3.0)
    assert pipeline_timing_payload('cam-a')['frame_read']['count'] == 0


def test_record_accepts_a_plain_summary_dict():
    record_pipeline_cycle('cam-a', {'stages': {STAGE_ALERTS: 3.0}, 'total_ms': 10.0})
    payload = pipeline_timing_payload('cam-a')
    assert payload['stages'][STAGE_ALERTS]['p50'] == 3.0
    assert payload['unaccounted']['p95'] == 7.0


def test_record_ignores_a_non_timer_and_a_junk_dict():
    assert record_pipeline_cycle('cam-a', object()) == {}
    record_pipeline_cycle('cam-a', {'stages': 'nope', 'total_ms': 'nope'})
    assert pipeline_timing_payload('cam-a')['cycles_timed'] == 0


def test_the_window_is_bounded_but_lifetime_totals_are_not():
    for index in range(WINDOW_SIZE + 25):
        record_pipeline_cycle('cam-a', _timed({STAGE_MOTION: 1.0}, total=1.0 + index))
    payload = pipeline_timing_payload('cam-a')
    assert payload['cycles_timed'] == WINDOW_SIZE
    assert payload['stages'][STAGE_MOTION]['count'] == WINDOW_SIZE
    assert payload['lifetime'][STAGE_MOTION]['count'] == WINDOW_SIZE + 25


def test_fleet_payload_pools_samples_and_keeps_per_camera_attribution():
    for _ in range(5):
        record_pipeline_cycle('slow', _timed({STAGE_INFERENCE: 90.0}, total=100.0))
    for _ in range(95):
        record_pipeline_cycle('fast', _timed({STAGE_INFERENCE: 10.0}, total=12.0))
    payload = pipeline_timing_payload()
    assert payload['cameras'] == ['fast', 'slow']
    # Pooled over every camera's retained window: 50 fast + 5 slow. Averaging
    # per-camera percentiles would have reported 50 here and hidden the slow
    # camera entirely; truncating the pooled list back to one window would have
    # silently dropped a camera instead.
    assert payload['stages'][STAGE_INFERENCE]['count'] == 55
    assert payload['stages'][STAGE_INFERENCE]['p95'] == 90.0
    assert payload['by_camera']['slow']['stages'][STAGE_INFERENCE]['p95'] == 90.0
    assert payload['by_camera']['fast']['stages'][STAGE_INFERENCE]['p95'] == 10.0


def test_unknown_camera_payload_is_empty_not_an_error():
    payload = pipeline_timing_payload('never-seen')
    assert payload['cycles_timed'] == 0
    assert payload['stages'] == {}
    assert payload['over_budget'] is False
    assert payload['frame_read']['count'] == 0


def test_account_for_replaces_inference_so_the_snapshot_must_be_the_base_call():
    """Regression: the caller must snapshot ``last_timing`` before re-runs.

    Region boost and tiling call the detector again, and every call overwrites
    ``detector.last_timing``. Because ``account_for`` REPLACES the inference
    stage, reading it after the sub-inferences would report the cost of the
    last tile as the cycle's whole model time and drop the base cost from the
    breakdown entirely. This test fails if anyone "simplifies" the capture back
    to a getattr after the try block.
    """
    detector = _LastTimingDetector([
        {'preprocess_ms': 2.0, 'inference_ms': 40.0, 'postprocess_ms': 3.0},  # base
        {'preprocess_ms': 1.0, 'inference_ms': 8.0, 'postprocess_ms': 1.0},   # boost
    ])
    timer = _timed({STAGE_INFERENCE: 50.0}, total=100.0)
    detector.detect_frame()                        # the base full-frame pass
    base = getattr(detector, 'last_timing', None)  # snapshotted HERE
    detector.detect_frame()                        # the region-boost re-run
    timer.account_for(base)
    assert timer.stages()[pipeline_timing.STAGE_INFERENCE] == 40.0
    assert timer.stages()[pipeline_timing.STAGE_POSTPROCESS] == 3.0


class _LastTimingDetector:
    """A duck-typed detector that republishes ``last_timing`` on every call."""

    def __init__(self, timings):
        self._timings = list(timings)
        self.last_timing = {}

    def detect_frame(self):
        self.last_timing = self._timings.pop(0)
        return []


def test_total_is_not_a_stage_of_its_own():
    # STAGE_TOTAL exists in the vocabulary so a caller can name it, but it must
    # never be double-counted as a stage cost.
    timer = _timed({STAGE_MOTION: 2.0, STAGE_TOTAL: 2.0}, total=2.0)
    assert timer.stages() == {STAGE_MOTION: 2.0}


def test_prune_drops_removed_cameras():
    record_pipeline_cycle('keep', _timed({STAGE_MOTION: 1.0}, total=1.0))
    record_pipeline_cycle('drop', _timed({STAGE_MOTION: 1.0}, total=1.0))
    pipeline_timing.prune_pipeline_timing({'keep'})
    assert pipeline_timing_payload()['cameras'] == ['keep']
    pipeline_timing.prune_pipeline_timing(None)
    assert pipeline_timing_payload()['cameras'] == ['keep']


class _FakeClock:
    """A manually advanced monotonic clock, so tests never sleep."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds
