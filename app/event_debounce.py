"""Per-camera live-detection backoff extracted from ``app/main.py`` (Phase-27).

``_live_backoff_lock`` + ``live_detection_failure_count`` +
``live_detection_retry_after`` are the per-camera backoff counters that
``schedule_live_camera_backoff`` writes to and that
``clear_live_camera_backoff`` clears on a successful frame read.

(The duplicate-event debounce that used to live here was replaced by the
presence gate in :mod:`app.event_presence`, which needs no cooldown.)

State PRIMITIVES stay on ``app.state``; ``clear_live_camera_backoff`` also
resets the per-camera motion-gate state (``_frame_motion_prev``,
``_frame_motion_error_cameras``, ``_periodic_scan_last_ts``) when a camera
transitions out of backoff, and logs the recovery via
``log_camera_diagnostic``.
"""

from __future__ import annotations

import time

import app.state as _state
from app.detection_status import update_live_detection_status
from app.diagnostics import log_camera_diagnostic


def clear_live_camera_backoff(camera_id: str) -> None:
    with _state._live_backoff_lock:
        was_backed_off = bool(_state.live_detection_failure_count.get(camera_id))
        _state.live_detection_retry_after.pop(camera_id, None)
        _state.live_detection_failure_count.pop(camera_id, None)
    if not was_backed_off:
        # Steady-state success (this runs on EVERY successful frame read, before
        # ``process_live_stream_alerts``): the backoff counters were already
        # empty, so there is nothing to recover. The per-camera motion models
        # and the periodic-scan clock MUST persist between frames -- the diff
        # engine accumulates its adaptive background in ``_frame_motion_prev``,
        # and the periodic scan measures its interval from
        # ``_periodic_scan_last_ts``. Clearing them unconditionally here reset
        # the diff background every cycle (so it never accumulated one and never
        # reported motion) and reset the scan clock every cycle (so the periodic
        # scan fired every frame instead of every N seconds). Only reset on a
        # genuine transition out of backoff.
        return
    log_camera_diagnostic(camera_id, 'detection_recovered', 'Live detection resumed after a successful frame read.', severity='info')
    # Genuine recovery from an outage: frames were unavailable for a while and
    # the scene may have changed, so drop the stale per-camera motion state for
    # BOTH engines (the diff engine's adaptive background and last-frame
    # buffers, and the MOG2 mixture model + scene-streak). Each engine reseeds
    # from the next frame instead of diffing against a pre-outage model and
    # emitting a spurious motion event on the first recovered frame.
    from app.detection_state import clear_frame_motion_state
    clear_frame_motion_state(camera_id)
    _state._periodic_scan_last_ts.pop(camera_id, None)


def schedule_live_camera_backoff(camera_id: str, message: str) -> float:
    """Record a per-camera detection failure + apply exponential backoff (max 300s).

    Mirror of ``clear_live_camera_backoff`` (Phase-27) but writes the state
    instead of clearing it. Lives here (rather than in a new ``camera_backoff``
    module) because the two helpers share the SAME three state primitives and
    are called from the same ``_detect_bg`` background closure.

    Pool-C reach (resolved lazily via lazy imports inside function body):
    - ``_state._live_backoff_lock``, ``_state.live_detection_failure_count``,
      ``_state.live_detection_retry_after`` (state primitives owned on state.py)
    - ``update_live_detection_status`` (Phase-29 rebind from
      app.detection_status)
    - ``log_camera_diagnostic`` (top-level helper on main.py at L1265).
    """
    with _state._live_backoff_lock:
        failure_count = _state.live_detection_failure_count.get(camera_id, 0) + 1
        _state.live_detection_failure_count[camera_id] = failure_count
        backoff_seconds = min(300.0, max(10.0, 5.0 * 2 ** min(failure_count - 1, 5)))
        retry_after = time.time() + backoff_seconds
        _state.live_detection_retry_after[camera_id] = retry_after
    update_live_detection_status(
        camera_id,
        state='error',
        reason=f'{message} Retrying in {int(backoff_seconds)}s.',
        detections=[],
    )
    if failure_count == 1:
        log_camera_diagnostic(
            camera_id,
            'detection_backoff',
            f'Live detection paused after error: {message}',
            severity='warning',
            details={'backoff_seconds': int(backoff_seconds)},
        )
    return backoff_seconds
