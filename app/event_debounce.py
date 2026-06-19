"""Live-event debounce cluster extracted from ``app/main.py`` (Phase-27).

The three helpers shipped here cluster around the **per-camera event
backoff / debounce bookkeeping** that the live-detection monitor writes to
between cycles:

- ``live_event_last_emitted_lock`` + ``live_event_last_emitted``: a
  ``dict[str, dict]`` keyed by ``camera_id`` carrying the last
  timestamp+labels that triggered an alert, used by
  ``live_event_is_debounced`` and ``remember_live_event`` to suppress
  duplicate alert events within the configured cooldown window.
- ``_live_backoff_lock`` + ``live_detection_failure_count`` +
  ``live_detection_retry_after``: the per-camera backoff counters that
  ``schedule_live_camera_backoff`` writes to and that
  ``clear_live_camera_backoff`` clears on a successful frame read.

State PRIMITIVES themselves STAY on ``app.main`` (the live-detection
worker thread, ``schedule_live_camera_backoff``, ``run_live_alert_monitor_once``
and ``process_live_stream_alerts`` all mutate them on main.py), and the
new module reaches them at call time via Pool C. This mirrors the
precedent set by :mod:`app.detection_state`,
:mod:`app.zone_schema`, :mod:`app.zone_detection`.

**Pool-A rebind (in ``app/main.py``):** ``live_event_is_debounced``,
``remember_live_event``, ``clear_live_camera_backoff`` are re-bound
as ``main.<name>``.

**Pool-C reach into ``main`` (resolved at call time):**

- ``main.live_event_last_emitted_lock`` -
  ``live_event_is_debounced``, ``remember_live_event``
- ``main.live_event_last_emitted`` - same two
- ``main._live_backoff_lock`` - ``clear_live_camera_backoff``
- ``main.live_detection_failure_count`` - ``clear_live_camera_backoff``
- ``main.live_detection_retry_after`` - ``clear_live_camera_backoff``
- ``main._frame_motion_lock`` / ``main._frame_motion_prev`` /
  ``main._frame_motion_error_cameras`` /
  ``main._periodic_scan_last_ts`` - ``clear_live_camera_backoff`` resets
  the per-camera motion-gate state when a transition out of backoff is
  detected; these all STAY on ``app.main`` (they are the same
  primitives ``detect_frame_motion`` writes to via
  :mod:`app.detection_state`).
- ``main.log_camera_diagnostic`` -
  ``clear_live_camera_backoff`` calls this when the camera was
  previously backed off and is now healthy.

Cross-module consumers unchanged:

- ``tests/test_api.py`` rehearses both ``main.live_event_last_emitted``
  (mutated directly) AND ``main.live_event_is_debounced`` /
  ``remember_live_event`` - both keep working via unchanged
  state-on-main + Pool-A rebind without any test edit.
"""

from __future__ import annotations

import time
from typing import Any

import app.main as main


def live_event_is_debounced(camera_id: str, labels: set[str], debounce_seconds: float) -> bool:
    if debounce_seconds <= 0 or not labels:
        return False
    with main.live_event_last_emitted_lock:
        previous = main.live_event_last_emitted.get(camera_id)
    if not previous:
        return False
    elapsed = time.time() - float(previous.get('timestamp', 0))
    if elapsed > debounce_seconds:
        return False
    if labels <= {'motion'}:
        return True
    previous_labels = {str(label).strip().lower() for label in previous.get('labels', []) if str(label).strip()}
    return bool(previous_labels & labels)


def remember_live_event(camera_id: str, labels: set[str], *, merge: bool=False) -> None:
    if not labels:
        return
    with main.live_event_last_emitted_lock:
        if merge:
            previous = main.live_event_last_emitted.get(camera_id) or {}
            labels = labels | {str(label).strip().lower() for label in previous.get('labels', []) if str(label).strip()}
        main.live_event_last_emitted[camera_id] = {'timestamp': time.time(), 'labels': sorted(labels)}


def clear_live_camera_backoff(camera_id: str) -> None:
    with main._live_backoff_lock:
        was_backed_off = bool(main.live_detection_failure_count.get(camera_id))
        main.live_detection_retry_after.pop(camera_id, None)
        main.live_detection_failure_count.pop(camera_id, None)
    if was_backed_off:
        main.log_camera_diagnostic(camera_id, 'detection_recovered', 'Live detection resumed after a successful frame read.', severity='info')
    with main._frame_motion_lock:
        main._frame_motion_prev.pop(camera_id, None)
    main._frame_motion_error_cameras.discard(camera_id)
    main._periodic_scan_last_ts.pop(camera_id, None)
def schedule_live_camera_backoff(camera_id: str, message: str) -> float:
    """Record a per-camera detection failure + apply exponential backoff (max 300s).

    Mirror of ``clear_live_camera_backoff`` (Phase-27) but writes the state
    instead of clearing it. Lives here (rather than in a new ``camera_backoff``
    module) because the two helpers share the SAME three state primitives and
    are called from the same ``_detect_bg`` background closure.

    Pool-C reach (resolved lazily via ``import app.main as main``):
    - ``main._live_backoff_lock``, ``main.live_detection_failure_count``,
      ``main.live_detection_retry_after`` (state primitives owned on main.py)
    - ``main.update_live_detection_status`` (Phase-29 rebind from
      app.detection_status)
    - ``main.log_camera_diagnostic`` (top-level helper on main.py at L1265).
    """
    with main._live_backoff_lock:
        failure_count = main.live_detection_failure_count.get(camera_id, 0) + 1
        main.live_detection_failure_count[camera_id] = failure_count
        backoff_seconds = min(300.0, max(10.0, 5.0 * 2 ** min(failure_count - 1, 5)))
        retry_after = time.time() + backoff_seconds
        main.live_detection_retry_after[camera_id] = retry_after
    main.update_live_detection_status(
        camera_id,
        state='error',
        reason=f'{message} Retrying in {int(backoff_seconds)}s.',
        detections=[],
    )
    if failure_count == 1:
        main.log_camera_diagnostic(
            camera_id,
            'detection_backoff',
            f'Live detection paused after error: {message}',
            severity='warning',
            details={'backoff_seconds': int(backoff_seconds)},
        )
    return backoff_seconds
