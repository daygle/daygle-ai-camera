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
- striped ``app.state._frame_motion_locks`` / ``main._frame_motion_prev`` /
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

import app.state as _state
from app.detection_status import update_live_detection_status
from app.diagnostics import log_camera_diagnostic

# After a non-motion event (person, car, sound) the background model briefly
# re-settles and can produce a short burst of spurious pixel-diff hits. Suppress
# motion-only events within this window so they don't create a second recording
# that's just noise from the same activity. Beyond this window, motion is treated
# as a genuinely independent event and records normally.
_MOTION_TRAILING_SUPPRESSION_SECONDS = 5.0

# Per-track cooldown bookkeeping: an anchor older than this is dropped rather
# than remembered, so the (camera, label, track) map cannot grow without bound
# across days of 4 Hz traffic. Generous: tracks live ~5 cycles; only a
# fragmented-reacquisition chain of distinct ids re-fires a label within the
# window, and pruning older anchors costs nothing.
_TRACK_COOLDOWN_PRUNE_SECONDS = 3600.0


def _track_fresh_labels(camera_id: str, label_cooldowns: dict[str, float], track_ids_by_label: dict[str, set[int]]) -> set[str]:
    """Return labels cooled down for their CURRENT track identities.

    A label is fresh when at least ONE track it is currently seeing is
    entitled to a new event: either the track has no anchor yet (a NEW object
    of that label -- the second car entering the frame) or its own window has
    elapsed since the last event that included it. When every current track is
    still inside its window the label is NOT fresh -- the same objects
    continuing are exactly what the legacy debounce suppresses. Labels with no
    tracked ids this cycle are reported fresh so the gate's mixed-set logic
    keeps legacy behavior for untracked detections.
    """
    now = time.time()
    with _state.live_event_last_emitted_lock:
        per_camera = _state.live_event_track_last_emitted.get(camera_id) or {}
        # Prune stale anchors while we hold the lock (amortized O(entries)).
        stale_cutoff = now - _TRACK_COOLDOWN_PRUNE_SECONDS
        for label_key in [k for k, v in per_camera.items() if float(v) < stale_cutoff]:
            del per_camera[label_key]
        fresh: set[str] = set()
        for label, cooldown in label_cooldowns.items():
            track_ids = track_ids_by_label.get(label, set())
            if not track_ids:
                fresh.add(label)  # no tracked ids -> nothing anchors this label
                continue
            for track_id in track_ids:
                anchor = per_camera.get(f'{label}#{track_id}')
                if anchor is None or now - float(anchor) > cooldown:
                    # New object, or this object's own window elapsed.
                    fresh.add(label)
                    break
        return fresh


def _remember_track_event(camera_id: str, track_ids_by_label: dict[str, set[int]]) -> None:
    """Record the emission time per (label, track) so each track's own window
    starts from the event that included it. Labels without tracked ids are
    skipped (they are anchored by the legacy label bookkeeping)."""
    now = time.time()
    with _state.live_event_last_emitted_lock:
        per_camera = _state.live_event_track_last_emitted.setdefault(camera_id, {})
        for label, track_ids in track_ids_by_label.items():
            for track_id in track_ids:
                per_camera[f'{label}#{track_id}'] = now


def live_event_is_debounced(camera_id: str, labels: set[str], debounce_seconds: float) -> bool:
    if debounce_seconds <= 0 or not labels:
        return False
    with _state.live_event_last_emitted_lock:
        previous = _state.live_event_last_emitted.get(camera_id)
    if not previous:
        return False
    elapsed = time.time() - float(previous.get('timestamp', 0))
    if elapsed > debounce_seconds:
        return False
    previous_labels = {str(label).strip().lower() for label in previous.get('labels', []) if str(label).strip()}
    # Motion-only events use a short trailing suppression window rather than the
    # full debounce window. If the prior event was also motion, use the normal
    # label-overlap path so back-to-back motion events still merge correctly.
    if labels <= {'motion'} and 'motion' not in previous_labels:
        return elapsed < _MOTION_TRAILING_SUPPRESSION_SECONDS
    return bool(previous_labels & labels)


def live_event_fresh_labels(camera_id: str, label_cooldowns: dict[str, float]) -> set[str]:
    """Return the labels whose OWN cooldown window has elapsed (fresh).

    Per-label debounce: each label's window starts from its last remembered
    emission, so a fast label can fire its own event while a slower label on
    the same camera is still cooling. A label with no prior emission is always
    fresh. Each label is anchored to the earlier of its per-label timestamp or
    the legacy event timestamp, so state written before per-label tracking
    (and tests that simulate elapsed time via ``timestamp``) stay correct.

    Motion-only events after a non-motion event use the short trailing
    suppression window (background re-settling noise) rather than the label
    cooldown - mirrors ``live_event_is_debounced``.
    """
    if not label_cooldowns:
        return set()
    with _state.live_event_last_emitted_lock:
        previous = _state.live_event_last_emitted.get(camera_id) or {}
        label_times = previous.get('label_times') or {}
        prev_timestamp = float(previous.get('timestamp') or 0)
        prev_labels = {str(label).strip().lower() for label in previous.get('labels', []) if str(label).strip()}
    now = time.time()
    fresh: set[str] = set()
    for label, cooldown in label_cooldowns.items():
        anchor = label_times.get(label)
        if anchor is None and label in prev_labels:
            anchor = prev_timestamp
        if anchor is None:
            fresh.add(label)
            continue
        anchor = min(float(anchor), prev_timestamp)
        if now - anchor > cooldown:
            fresh.add(label)
    # Motion-only events after a non-motion event: suppress within the trailing
    # window regardless of the motion rule's own cooldown.
    if fresh and fresh <= {'motion'} and 'motion' not in prev_labels and (now - prev_timestamp) < _MOTION_TRAILING_SUPPRESSION_SECONDS:
        fresh = set()
    return fresh


def remember_live_event(camera_id: str, labels: set[str], *, merge: bool=False) -> None:
    if not labels:
        return
    now = time.time()
    normalized = {str(label).strip().lower() for label in labels if str(label).strip()}
    if not normalized:
        return
    with _state.live_event_last_emitted_lock:
        previous = _state.live_event_last_emitted.get(camera_id) or {}
        if merge:
            normalized = normalized | {str(label).strip().lower() for label in previous.get('labels', []) if str(label).strip()}
        label_times = dict(previous.get('label_times') or {})
        for label in normalized:
            label_times[label] = now
        _state.live_event_last_emitted[camera_id] = {'timestamp': now, 'labels': sorted(normalized), 'label_times': label_times}


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
