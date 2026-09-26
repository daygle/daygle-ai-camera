"""Per-cycle stage timing for the live detection pipeline (roadmap: measure).

``detection_telemetry`` answers "which stage dropped the candidate?" with
counts. It cannot answer "which stage ate the frame budget?", because it records
no durations at all -- and the inference scheduler's ``run_seconds`` only
brackets the whole cycle. That gap is why broad, expensive performance work used
to be speculative: nobody could show that motion scoring, ONNX inference, zone
rule matching or event persistence was where the time actually went.

This module adds the missing measurement:

* :class:`StageTimer` wraps each stage as a context manager, so a stage cannot
  be timed wrongly by an out-of-sync ``t``/``now()`` pair around a branch, and
  the recorded milliseconds are additive rather than hand-subtracted.
* Every cycle is folded into a bounded rolling window per camera, and the read
  API returns p50/p95/max/mean per stage. p50 alone hides the spike that makes
  a camera feel broken; the mean alone hides it entirely.
* The sum of the recorded stages is compared against the measured total, and
  the difference is reported as ``unaccounted_ms``. This is the self-check on
  the instrumentation itself: a large unaccounted number means a stage is still
  unmeasured, so the breakdown cannot be trusted to be complete. The acceptance
  target from the roadmap -- "p95 cycle overhead < 10 ms" -- is evaluated
  against exactly this number, so the target doubles as a completeness check.

Design constraints (identical to ``detection_telemetry``):

* **Never on the critical path.** Timing is a couple of ``perf_counter`` calls
  and one dict update under a lock. Nothing here raises: a timing failure must
  not break detection.
* **No app imports.** This module is importable and unit-testable without
  numpy, cv2, ONNX or FastAPI, matching ``inference_scheduler`` /
  ``postprocess_pool`` / ``adaptive_cadence``.
* **Bounded.** A rolling window of the most recent ``WINDOW_SIZE`` cycles per
  camera plus lifetime sums, so a long-running process cannot grow without
  limit.
"""

from __future__ import annotations

import math
import statistics
import threading
import time
from typing import Any

# Rolling window of recent cycles retained per camera. Matches the telemetry
# window so the two payloads describe the same slice of history.
WINDOW_SIZE = 50

# The roadmap's acceptance target for stage-timing overhead. p95 of
# ``unaccounted_ms`` is expected to stay under this; exceeding it means either a
# stage is missing from the instrumentation or a genuinely unmeasured cost
# (lock contention, GIL handoff) crept in, and both need a human.
OVERHEAD_BUDGET_MS = 10.0

# Stable vocabulary of pipeline stages. A cycle that never reaches a stage
# simply omits it, so "absent" and "instant" are distinguishable. Kept as
# explicit constants so a typo at a call site fails loudly at review time
# instead of producing an unqueryable stage name.
#
# Frame acquisition is deliberately NOT in this vocabulary. It happens before
# the cycle timer starts (the scheduler reads the frame, then calls the cycle),
# so charging it to a cycle would either inflate ``total_ms`` or show up as
# unaccounted overhead. It is sampled separately by :func:`record_frame_read`
# and reported under the payload's ``frame_read`` section, which keeps the
# "is the camera feeding us fast enough" question answerable without blurring
# the per-cycle budget.
STAGE_MOTION = 'motion_detection'
STAGE_PREPROCESS = 'preprocess'
STAGE_INFERENCE = 'inference'
STAGE_POSTPROCESS = 'postprocess'
STAGE_FACE_PASS = 'face_pass'
STAGE_REGION_BOOST = 'region_boost'
STAGE_TILING = 'tiling'
STAGE_TRACKING = 'tracking'
STAGE_BEHAVIOUR = 'behaviour'
STAGE_FILTERING = 'filtering'
STAGE_CONFIRMATION = 'confirmation'
STAGE_FACE_IDENTITY = 'face_identity'
STAGE_ZONE_RULES = 'zone_rules'
STAGE_ALERTS = 'alerts'
STAGE_EVENT = 'event_persist'
STAGE_TOTAL = 'total'

# Stage names, in the order a cycle executes them. Used to order the payload so
# a reader sees the pipeline in its real sequence rather than in dict order.
STAGE_ORDER: tuple[str, ...] = (
    STAGE_MOTION,
    STAGE_PREPROCESS,
    STAGE_INFERENCE,
    STAGE_POSTPROCESS,
    STAGE_FACE_PASS,
    STAGE_REGION_BOOST,
    STAGE_TILING,
    STAGE_TRACKING,
    STAGE_BEHAVIOUR,
    STAGE_FILTERING,
    STAGE_CONFIRMATION,
    STAGE_FACE_IDENTITY,
    STAGE_ZONE_RULES,
    STAGE_ALERTS,
    STAGE_EVENT,
    STAGE_TOTAL,
)

# Stages a caller is expected to record. Anything else is rejected by
# :meth:`StageTimer.stage` so an invented name cannot silently create a stage
# that is never populated.
_KNOWN_STAGES = frozenset(STAGE_ORDER)

_timing_lock = threading.Lock()
# camera_id -> {'stages': {stage: [ms, ...]}, 'cycles': [ {...}, ... ],
#               'totals': {stage: {'count': n, 'sum_ms': x}}}
_timing: dict[str, dict[str, Any]] = {}

# A clock the tests can substitute. Matches the injectable-clock convention in
# ``inference_scheduler`` so a p95 can be asserted without real sleeps.
_clock = time.perf_counter


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of ``values``.

    Nearest-rank (rather than interpolated) is used deliberately: a tail
    latency number should be an OBSERVED sample, never a synthetic value
    between two samples. The index is ``ceil(fraction * n)`` so the p95 of 100
    samples is the 95th slowest one, and truncation cannot drift the answer.
    An empty input is 0.0 so a stage that never ran reads as "no cost" instead
    of crashing the payload.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    fraction = min(1.0, max(0.0, float(fraction)))
    if fraction <= 0.0:
        return float(ordered[0])
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return float(ordered[index])


def record_frame_read(camera_id: str, milliseconds: float) -> None:
    """Sample one frame acquisition (read + JPEG decode) for ``camera_id``.

    Sampled outside the cycle timer on purpose -- see the note on the stage
    vocabulary. Bounded to the same ``WINDOW_SIZE`` as the cycle stages.
    """
    try:
        value = float(milliseconds)
    except (TypeError, ValueError):
        return
    if value < 0:
        return
    key = str(camera_id)
    with _timing_lock:
        entry = _timing.setdefault(key, {'stages': {}, 'cycles': [], 'totals': {}, 'frame_read': []})
        samples = entry.setdefault('frame_read', [])
        samples.append(value)
        if len(samples) > WINDOW_SIZE:
            del samples[: len(samples) - WINDOW_SIZE]


def _summarize(values: list[float]) -> dict[str, float]:
    """p50/p95/max/mean for one stage over the retained window.

    p50 is a true ``statistics.median`` (which averages the two middle samples
    for an even count) while p95 is the nearest-rank observed sample. That split
    is intentional and matches the rest of the codebase: the median is the
    "typical cycle" an operator compares against the detection interval, and the
    p95 is the "worst recent cycle" they page on. Interpolating the tail would
    invent a latency that was never measured.
    """
    if not values:
        return {'count': 0, 'p50': 0.0, 'p95': 0.0, 'max': 0.0, 'mean': 0.0, 'sum': 0.0}
    return {
        'count': len(values),
        'p50': round(float(statistics.median(values)), 3),
        'p95': round(percentile(values, 0.95), 3),
        'max': round(max(values), 3),
        'mean': round(sum(values) / len(values), 3),
        'sum': round(sum(values), 3),
    }


class _StageContext:
    """Context manager returned by :meth:`StageTimer.stage`."""

    __slots__ = ('_timer', '_stage', '_started')

    def __init__(self, timer: 'StageTimer', stage: str) -> None:
        self._timer = timer
        self._stage = stage
        self._started = 0.0

    def __enter__(self) -> '_StageContext':
        self._started = _clock()
        return self

    def __exit__(self, *_exc: Any) -> bool:
        # A stage that raised still consumed time, and that time is the whole
        # point of the measurement: an exception path that costs 400 ms is
        # exactly what an operator needs to see. The re-raise is left to the
        # caller (``__exit__`` returns a falsy value).
        self._timer.add(self._stage, max(0.0, _clock() - self._started) * 1000.0)
        return False


class StageTimer:
    """Accumulates per-stage milliseconds for ONE detection cycle.

    Re-entrant per stage by sum: if a stage runs twice (the opt-in region
    boost and tiling passes re-run the detector over sub-regions) the two costs
    add up, which is what a cycle budget wants. ``total_ms`` is set explicitly
    by the caller rather than derived, because "cycle total" and "sum of the
    stages I chose to measure" are different numbers and the gap between them
    is the unaccounted overhead the budget is evaluated against.
    """

    __slots__ = ('_stages', '_total_ms', '_started')

    def __init__(self) -> None:
        self._stages: dict[str, float] = {}
        self._total_ms: float | None = None
        self._started = _clock()

    def stage(self, stage: str) -> _StageContext:
        """Time the enclosed block as ``stage``."""
        if stage not in _KNOWN_STAGES:
            raise KeyError(f'unknown pipeline stage: {stage!r}')
        return _StageContext(self, stage)

    def add(self, stage: str, milliseconds: float) -> None:
        """Record an externally measured ``milliseconds`` for ``stage``.

        Used for costs that are already measured elsewhere and should not be
        measured twice -- the detector's own preprocess/session/postprocess
        split, and the frame-read wait. Unknown stages are ignored rather than
        raising: a detector that is an older build simply has no breakdown.
        """
        if stage not in _KNOWN_STAGES:
            return
        try:
            value = float(milliseconds)
        except (TypeError, ValueError):
            return
        if value < 0:
            return
        self._stages[stage] = self._stages.get(stage, 0.0) + value

    def mark_total(self, milliseconds: float | None = None) -> float:
        """Set (or read back) the measured total cycle duration in ms."""
        if milliseconds is not None:
            try:
                self._total_ms = max(0.0, float(milliseconds))
            except (TypeError, ValueError):
                self._total_ms = None
        return self._total_ms if self._total_ms is not None else 0.0

    def elapsed_ms(self) -> float:
        """Milliseconds since this timer was created."""
        return max(0.0, _clock() - self._started) * 1000.0

    def stages(self) -> dict[str, float]:
        """Copy of the accumulated stage costs, rounded, excluding ``total``."""
        return {
            name: round(value, 3)
            for name, value in self._stages.items()
            if name != STAGE_TOTAL and value > 0
        }

    def account_for(self, detector_timing: Any) -> None:
        """Fold a detector's own preprocess/inference/postprocess split in.

        ``detector_timing`` is whatever ``getattr(detector, 'last_timing', None)``
        returned. Splitting the three apart matters because they have opposite
        fixes: inference scales with input size / precision, preprocessing is
        pure resize + letterbox overhead, and postprocess is NMS on a CPU. A
        single "inference" number cannot tell an operator which knob to turn.

        The caller's own ``STAGE_INFERENCE`` entry is REPLACED by the detector's
        when present, because the detector measures the real ONNX session run
        while the caller's bracket also contains the Python dispatch around it.
        """
        if not isinstance(detector_timing, dict):
            return
        for key, stage in (
            ('preprocess_ms', STAGE_PREPROCESS),
            ('inference_ms', STAGE_INFERENCE),
            ('postprocess_ms', STAGE_POSTPROCESS),
        ):
            try:
                value = float(detector_timing.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                self._stages[stage] = value

    def summary(self) -> dict[str, Any]:
        """The per-cycle record handed to :func:`record_pipeline_cycle`."""
        total = self.mark_total()
        measured = round(sum(self._stages.values()), 3)
        return {
            'stages': self.stages(),
            'total_ms': round(total, 3),
            'measured_ms': measured,
            'unaccounted_ms': round(max(0.0, total - measured), 3),
        }


def record_pipeline_cycle(camera_id: str, timer: StageTimer | dict[str, Any]) -> dict[str, Any]:
    """Fold one timed cycle into the rolling window for ``camera_id``.

    Accepts a :class:`StageTimer` or a pre-built summary dict. Never raises: a
    timing failure must not break a detection cycle.
    """
    try:
        if isinstance(timer, StageTimer):
            summary = timer.summary()
        elif isinstance(timer, dict):
            summary = {
                'stages': {
                    str(name): max(0.0, float(value))
                    for name, value in (timer.get('stages') or {}).items()
                    if name in _KNOWN_STAGES and name != STAGE_TOTAL
                },
                'total_ms': max(0.0, float(timer.get('total_ms') or 0.0)),
            }
            measured = round(sum(summary['stages'].values()), 3)
            summary['measured_ms'] = measured
            summary['unaccounted_ms'] = round(
                max(0.0, summary['total_ms'] - measured), 3,
            )
        else:
            return {}
        key = str(camera_id)
        with _timing_lock:
            entry = _timing.get(key)
            if entry is None:
                entry = {'stages': {}, 'cycles': [], 'totals': {}}
                _timing[key] = entry
            samples = entry['stages']
            for name, value in summary['stages'].items():
                bucket = samples.setdefault(name, [])
                bucket.append(value)
                if len(bucket) > WINDOW_SIZE:
                    del bucket[: len(bucket) - WINDOW_SIZE]
                totals = entry['totals'].setdefault(name, {'count': 0, 'sum_ms': 0.0})
                totals['count'] += 1
                totals['sum_ms'] += value
            cycles = entry['cycles']
            cycles.append({
                'total_ms': summary['total_ms'],
                'measured_ms': summary['measured_ms'],
                'unaccounted_ms': summary['unaccounted_ms'],
            })
            if len(cycles) > WINDOW_SIZE:
                del cycles[: len(cycles) - WINDOW_SIZE]
        return summary
    except Exception:  # noqa: BLE001 - timing must never break detection
        return {}


def _order_stages(stages: dict[str, Any]) -> dict[str, Any]:
    """Return ``stages`` in pipeline order, with any unknown names appended."""
    ordered = {name: stages[name] for name in STAGE_ORDER if name in stages}
    for name in sorted(stages):
        if name not in ordered:
            ordered[name] = stages[name]
    return ordered


def _entry_payload(camera_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    cycles = entry['cycles']
    unaccounted = [cycle['unaccounted_ms'] for cycle in cycles]
    totals = [cycle['total_ms'] for cycle in cycles]
    return {
        'camera_id': camera_id,
        'cycles_timed': len(cycles),
        'stages': _order_stages({
            name: _summarize(values) for name, values in entry['stages'].items()
        }),
        'frame_read': _summarize(entry.get('frame_read') or []),
        'lifetime': _order_stages({
            name: {
                'count': data['count'],
                'sum_ms': round(data['sum_ms'], 3),
                'mean_ms': round(data['sum_ms'] / data['count'], 3) if data['count'] else 0.0,
            }
            for name, data in entry['totals'].items()
        }),
        'cycle': _summarize(totals),
        'unaccounted': _summarize(unaccounted),
        'overhead_budget_ms': OVERHEAD_BUDGET_MS,
        # True means the p95 gap between the measured cycle and the sum of its
        # stages exceeds the roadmap's overhead target. It is the completeness
        # alarm for this instrumentation as much as a performance alarm.
        'over_budget': percentile(unaccounted, 0.95) > OVERHEAD_BUDGET_MS,
        'last_cycle': cycles[-1] if cycles else None,
    }


def pipeline_timing_payload(camera_id: str | None = None) -> dict[str, Any]:
    """Per-stage latency breakdown for one camera, or every camera.

    With ``camera_id`` the response is that camera's ``_entry_payload``. Without
    it, the response aggregates every camera's stage samples: stage percentiles
    are computed over the POOLED samples (a pooled p95 is the honest
    "how slow is this pipeline across the fleet" number, not an average of
    per-camera percentiles, which would hide one slow camera), while
    ``by_camera`` keeps the per-camera view for attribution.
    """
    with _timing_lock:
        if camera_id is not None:
            entry = _timing.get(str(camera_id))
            if entry is None:
                return {
                    'camera_id': str(camera_id),
                    'cycles_timed': 0,
                    'stages': {},
                    'frame_read': _summarize([]),
                    'lifetime': {},
                    'cycle': _summarize([]),
                    'unaccounted': _summarize([]),
                    'overhead_budget_ms': OVERHEAD_BUDGET_MS,
                    'over_budget': False,
                    'last_cycle': None,
                }
            return _entry_payload(str(camera_id), entry)

        pooled: dict[str, list[float]] = {}
        pooled_cycles: list[float] = []
        pooled_unaccounted: list[float] = []
        pooled_frame_read: list[float] = []
        by_camera: dict[str, Any] = {}
        for cam_id, entry in _timing.items():
            # Every camera's full retained window is pooled. Truncating the
            # pooled list back to WINDOW_SIZE would silently drop whichever
            # cameras sorted last, so a 12-camera deployment would report the
            # p95 of two of them and call it the fleet.
            for name, values in entry['stages'].items():
                pooled.setdefault(name, []).extend(values)
            pooled_frame_read.extend(entry.get('frame_read') or [])
            for cycle in entry['cycles']:
                pooled_cycles.append(cycle['total_ms'])
                pooled_unaccounted.append(cycle['unaccounted_ms'])
            by_camera[cam_id] = _entry_payload(cam_id, entry)
        cameras = sorted(by_camera)
    return {
        'camera_id': None,
        'cameras': cameras,
        'cycles_timed': len(pooled_cycles),
        'stages': _order_stages({
            name: _summarize(values) for name, values in pooled.items()
        }),
        'cycle': _summarize(pooled_cycles),
        'unaccounted': _summarize(pooled_unaccounted),
        'frame_read': _summarize(pooled_frame_read),
        'overhead_budget_ms': OVERHEAD_BUDGET_MS,
        'over_budget': percentile(pooled_unaccounted, 0.95) > OVERHEAD_BUDGET_MS,
        'by_camera': by_camera,
    }


def clear_pipeline_timing(camera_id: str | None = None) -> None:
    """Drop timings for one camera, or for all cameras (tests / camera delete)."""
    with _timing_lock:
        if camera_id is None:
            _timing.clear()
        else:
            _timing.pop(str(camera_id), None)


def prune_pipeline_timing(active_camera_ids: set[str] | None = None) -> None:
    """Drop timings for cameras that no longer exist.

    Called from the same pruning pass that clears per-camera motion state and
    detection telemetry, so a removed camera cannot leave a permanent entry
    behind.
    """
    if active_camera_ids is None:
        return
    with _timing_lock:
        for stale in [key for key in _timing if key not in active_camera_ids]:
            _timing.pop(stale, None)
