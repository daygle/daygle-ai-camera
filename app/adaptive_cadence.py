"""Adaptive detection cadence (Item 16 of the performance roadmap).

Detection ran at a fixed ``detection_interval_seconds`` per camera. That is
the right answer for a scene with something moving in it, and a waste for a
scene that has been still for ten minutes: the model re-looks at an identical
picture at 2 Hz, burns the inference budget item 3 carefully bounded, and
pushes every OTHER camera further down the queue to do it.

The project already has the two halves this needs. Motion gating decides
whether to run inference at all, and ``periodic_scan_interval_seconds``
periodically overrides that gate so a subject that stops moving is still
found. What is missing is the middle: a cadence that *adapts* between those
extremes, per camera, from two observable signals.

**Scene stability.** How long this camera has seen no motion. A still scene
can be sampled far less often, because a person who walks into it produces
motion on the very next sampled frame - the miss window is the sampling
interval, not the gate.

**Global inference load.** How saturated the scheduler is. When the queue is
already over budget the system is behind on cameras that are *actively*
detecting; stretching a still camera further is the cheapest available
relief, and it is the only lever that costs nothing in detection quality
because there is nothing to detect.

The critical safety property is the one that makes this safe to ship at all:

    **A camera is never sampled less often than ``max_stale_seconds``.**

Adaptive cadence is only legitimate because the guarantee it preserves is
"a stationary subject is still detected within a bounded time". Without that
floor, a scene that goes quiet could drop to a 10s interval and a person
standing still in it would be invisible. ``AdaptiveCadenceTracker`` therefore
tracks the last time each camera was *actually* sampled and forces a full
scan once that exceeds the staleness budget, regardless of how stable the
scene looks. Slowing down buys throughput; it never buys a missed detection.

Like ``app.inference_scheduler`` and ``app.postprocess_pool``, this module has
no app imports, so it is unit-testable without cv2 / ONNX Runtime present.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

# How much the interval may grow while a scene is stable. Bounded at 4x: the
# point is to cut steady-state work, not to redefine what "detecting" means.
MAX_STABILITY_STRETCH = 4.0

# Default staleness budget. A still subject is guaranteed a sample at least
# this often, so the worst case for "person stands still in a quiet driveway"
# is 10s rather than unbounded. Also the ceiling the stretch factor is capped
# by, which keeps the two guarantees consistent.
DEFAULT_MAX_STALE_SECONDS = 10.0

# Scheduler load (pending jobs) at which load-based stretching starts, and the
# multiple of max_workers at which it is fully applied. Between the two the
# stretch scales linearly, so a queue that briefly spikes does not
# immediately halve every cadence on the system.
LOAD_STRETCH_START = 0.5
LOAD_STRETCH_FULL = 2.0


def stretch_for_load(pending: int, max_workers: int) -> float:
    """Extra stretch multiplier from current scheduler pressure.

    1.0 (no stretch) at half a queue's worth of work, rising to
    ``MAX_STABILITY_STRETCH`` at twice the worker count. Returns 1.0 when the
    load cannot be determined, so a missing stat degrades to today's
    behaviour rather than to a guess.
    """
    workers = max(1, int(max_workers or 1))
    try:
        depth = max(0, int(pending))
    except (TypeError, ValueError):
        return 1.0
    ratio = depth / workers
    if ratio <= LOAD_STRETCH_START:
        return 1.0
    if ratio >= LOAD_STRETCH_FULL:
        return MAX_STABILITY_STRETCH
    span = LOAD_STRETCH_FULL - LOAD_STRETCH_START
    progress = (ratio - LOAD_STRETCH_START) / span
    return 1.0 + progress * (MAX_STABILITY_STRETCH - 1.0)


class AdaptiveCadenceTracker:
    """Per-camera stability state and the resulting effective interval.

    One instance per process, keyed by camera id. The live monitor consults it
    before deciding whether a camera is due; it is the only place that knows
    how long a camera has been still.
    """

    def __init__(
        self,
        *,
        max_stale_seconds: float = DEFAULT_MAX_STALE_SECONDS,
        now: Callable[[], float] = time.monotonic,
    ):
        self._max_stale = max(0.5, float(max_stale_seconds))
        self._now = now
        self._lock = threading.Lock()
        # camera_id -> monotonic ts of the last sample that ran inference
        self._last_sample: dict[str, float] = {}
        # camera_id -> monotonic ts of the last cycle that saw motion
        self._last_motion: dict[str, float] = {}
        self._overrides: dict[str, float] = {}

    # ── configuration ───────────────────────────────────────────────────

    def set_max_stale_seconds(self, seconds: float) -> None:
        """Bound the staleness guarantee (a settings write, not a hot path)."""
        with self._lock:
            self._max_stale = max(0.5, float(seconds))

    def clear_camera(self, camera_id: str) -> None:
        """Forget a camera's state. Called when it is removed or re-added, so a
        removed-and-re-added camera does not inherit a "quiet for 10 minutes"
        streak and start sampling on the slow cadence."""
        with self._lock:
            self._last_sample.pop(camera_id, None)
            self._last_motion.pop(camera_id, None)
            self._overrides.pop(camera_id, None)

    def clear(self) -> None:
        with self._lock:
            self._last_sample.clear()
            self._last_motion.clear()
            self._overrides.clear()

    # ── state updates ───────────────────────────────────────────────────

    def note_cycle(self, camera_id: str, *, had_motion: bool) -> None:
        """Record the outcome of a completed detection cycle.

        Called every cycle, whether or not motion was found - that is what
        makes the quiet-streak measurable rather than inferred from a gap in
        the sample times.
        """
        moment = self._now()
        with self._lock:
            self._last_sample[camera_id] = moment
            if had_motion:
                self._last_motion[camera_id] = moment

    def note_sample(self, camera_id: str) -> None:
        """Record that inference actually ran, without a motion result.

        For the motion-gated path, where a cycle that skipped inference is
        still evidence the scene is quiet.
        """
        with self._lock:
            self._last_sample[camera_id] = self._now()

    # ── decision ────────────────────────────────────────────────────────

    def effective_interval(
        self,
        camera_id: str,
        base_interval: float,
        *,
        pending: int = 0,
        max_workers: int = 1,
    ) -> float:
        """The interval this camera should be sampled at, right now.

        Returns ``base_interval`` unchanged unless the scene has been still
        long enough to justify stretching, and never returns more than the
        staleness budget allows. On the adaptive path the result is also
        floored at the configured base, so adapting can only ever slow a
        camera down from what the operator asked for - never speed it up past
        it. A per-camera override is the one thing that may raise the rate.
        """
        base = max(0.1, float(base_interval or 0.5))
        # Two different ceilings, deliberately not the same number:
        #   stretch_ceiling bounds how far AUTOMATIC adaptation may slow a
        #     camera (a fraction of what the operator asked for, and never
        #     past the staleness budget);
        #   stale_ceiling is the staleness budget itself, the only limit that
        #     applies to an explicit override. An operator pinning a camera to
        #     3s is not "adaptation" and must not be clipped to base*4 when
        #     base is 0.5s - that would silently ignore the setting.
        stretch_ceiling = min(base * MAX_STABILITY_STRETCH, self._max_stale)
        stale_ceiling = self._max_stale
        if stretch_ceiling <= base:
            return base

        moment = self._now()
        with self._lock:
            last_motion = self._last_motion.get(camera_id)
            last_sample = self._last_sample.get(camera_id)
            override = self._overrides.get(camera_id)

        # A camera that has never seen motion is not "stable", it is unknown.
        # Treating it as stable would start a new camera on the slow cadence
        # and could miss its very first subject, so only a camera with a known
        # motion history and a real quiet streak qualifies.
        if last_motion is None or last_sample is None:
            return base

        quiet_for = moment - last_motion
        since_sample = moment - last_sample

        # The staleness floor. Once the quiet streak has run this long the
        # camera MUST be sampled, whatever else the arithmetic says.
        if since_sample >= self._max_stale:
            return base

        if override is not None:
            # An override is explicit intent and is honoured in BOTH
            # directions - that is its whole purpose ("keep this camera at
            # full rate"). Only the staleness budget still applies, so a pin
            # can never breach the guarantee that a still subject is sampled.
            return min(max(0.1, float(override)), stale_ceiling)

        # Linear ramp over the staleness budget: no stretching at all for the
        # first half, full stretch at the budget. The first half matters most
        # - it keeps a scene that just went quiet at full rate, which is
        # exactly when something is about to walk into it.
        progress = max(0.0, min(1.0, quiet_for / self._max_stale))
        if progress <= 0.5:
            return base
        ramp = (progress - 0.5) / 0.5

        stretch = 1.0 + ramp * (MAX_STABILITY_STRETCH - 1.0) * stretch_for_load(pending, max_workers)
        return min(base * stretch, stretch_ceiling)

    def set_override(self, camera_id: str, interval: Optional[float]) -> None:
        """Pin a camera to a fixed interval (or clear the pin with None).

        Honoured in both directions - a pin may speed a camera back up to full
        rate as well as hold it slow - but never beyond the staleness ceiling,
        so pinning cannot reintroduce a missed-detection hole.

        Not used by the live monitor today; it exists so an operator-facing
        "always sample this camera fast" control has a home, and so a future
        profile automation can force a camera back to full rate without
        reaching into the monitor's internals.
        """
        with self._lock:
            if interval is None:
                self._overrides.pop(camera_id, None)
            else:
                self._overrides[camera_id] = max(0.1, float(interval))

    # ── introspection ───────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Per-camera cadence state, for telemetry and the status endpoints."""
        moment = self._now()
        with self._lock:
            cameras = {}
            for camera_id, last_motion in self._last_motion.items():
                last_sample = self._last_sample.get(camera_id)
                cameras[camera_id] = {
                    'quiet_for_seconds': round(max(0.0, moment - last_motion), 3),
                    'since_sample_seconds': (
                        round(max(0.0, moment - last_sample), 3) if last_sample is not None else None
                    ),
                    'overridden': camera_id in self._overrides,
                }
            return {
                'max_stale_seconds': self._max_stale,
                'cameras': cameras,
            }


# ── Process-wide tracker ──────────────────────────────────────────────────
# One instance for the process. Created lazily so importing this module has no
# side effects, which keeps it importable from tests and from the settings
# router without a running monitor.

_tracker: Optional[AdaptiveCadenceTracker] = None
_tracker_lock = threading.Lock()


def get_adaptive_cadence() -> AdaptiveCadenceTracker:
    """The shared cadence tracker, created on first use."""
    global _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = AdaptiveCadenceTracker()
        return _tracker


def reset_adaptive_cadence() -> None:
    """Drop all per-camera cadence state (tests, and a full service reload)."""
    global _tracker
    with _tracker_lock:
        _tracker = None
