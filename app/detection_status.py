"""Live-detection status cluster extracted from ``app/main.py`` (Phase-29).

This module owns the five helpers that previously lived inline on
``app/main.py``:

* ``update_live_detection_status(camera_id, **updates)`` - thread-safe
  status dict update with timestamp.
* ``detection_label_strings(detections)`` - sorts + dedupes
  non-generic labels from a detections list.
* ``detection_label_confidences(detections)`` - returns the best
  confidence per non-generic label.
* ``live_detection_status_payload(camera_id=None)`` - assembles the
  API payload consumed by ``app/api/live_router.py`` and
  ``app/api/status_router.py``.
* ``_camera_has_live_alert_stream(settings)`` - predicate: does the
  camera have a stream URL?

The two pieces of state this cluster exclusively owns STAY on
``app.main`` (NOT moved here, reachable via Pool C):

* ``app.main.live_detection_status`` - ``dict`` keyed by camera_id.
* ``app.main.live_detection_status_lock`` - ``threading.Lock`` guarding
  the dict.

This mirrors the Phase-26 (``live_detection_history`` /
``live_detection_history_lock``), Phase-27 (``_notification_threads`` /
``_notification_threads_lock``) precedent for keeping state on
``app.main`` while the helper moves into a sibling module.

**Pool-A rebind (in ``app/main.py``):** all five helpers are re-bound as
``main.<orig_name>``. Keeping ``_camera_has_live_alert_stream``
underscored (matches its main.py-public-visible name) means existing
main.py body callers need zero modification.

**Pool-C reach sites used by this module (each resolved lazily via
``import app.main as main``):**

* ``main.live_detection_status`` and ``main.live_detection_status_lock``
  - state primitives (only touched by ``update_live_detection_status``
  and ``live_detection_status_payload``).
* ``main.get_camera_config`` - Phase-18 rebind from
  ``app.camera_config``. Used by ``live_detection_status_payload`` to
  resolve a camera_id to the camera config dict.
* ``main.ai_status_payload`` - Phase-20 rebind from
  ``app.ai_settings``. Used by ``live_detection_status_payload`` to
  attach the AI backend state to the response.
* ``main.build_stream_url`` - top-level helper on ``main.py`` at L588.
  Used by ``_camera_has_live_alert_stream`` to test for a viable RTSP
  stream URL.

**External callers reaching these helpers via ``main.<name>``**
(continued by Phase-29 rebind):

* ``app/api/live_router.py`` - ``main.live_detection_status_payload(camera_id)``.
* ``app/api/status_router.py`` - ``main.live_detection_status_payload(camera_id)``
  (multiple call sites in response dicts).
* ``tests/test_api.py`` - ``main.live_detection_status_payload('camera-1')``
  (multiple test cases).

**Internal main.py body callers** (bare names, resolve through the
Phase-29 rebind):

* ``update_live_detection_status`` - called from many sites
  (error handlers at L775, skip-detection branches at L1047/L1057,
  orchestration at L1099/L1128, alert/recording state updates at
  L1172/L1198, and more).
* ``detection_label_strings`` + ``detection_label_confidences`` - used
  in ``process_live_stream_alerts`` to seed per-recording label
  metadata.
* ``live_detection_status_payload`` - only used by external callers
  (API routers + tests), no main.py body callers.

**Logger acquisition:** the module uses its own child logger via
``logging.getLogger('daygle.ai')`` (matches Phase-26/27/28 precedent).
None of the five helpers currently emit log messages, but the logger
is wired up for consistency with sibling modules.

**Default-arg safety.** No function-default expressions evaluate
``main.<name>`` at module-load time (only ``None`` defaults are used),
so the Phase-29 rebind loop can fire BEFORE this module body executes
and all Pool-C reaches resolve lazily at call time.

**Skipped helpers (NOT moved):** ``extend_active_rtsp_recording``
(recording extension cluster, defer to a later phase) and
``schedule_live_camera_backoff`` (backoff scheduling, related to
Phase-27 event_debounce but tightly coupled to recording expiry
logic - defer). Both are left intact in ``main.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import app.state as _state
from app.config_facades import get_camera_config

logger = logging.getLogger('daygle.ai')

# Canonical set of detection labels that are NOT specific-object triggers.
# Used by detection_label_strings / detection_label_confidences to skip
# generic markers (motion, alert, human, object, none, off, continuous,
# empty) when computing per-recording label metadata, and called inside
# extend_active_rtsp_recording to decide whether a candidate trigger
# label should overwrite an existing generic trigger_label on a recording.
# Defined as a ``frozenset`` so callers cannot accidentally mutate the
# canonical list (add / remove / clear are not available on frozenset).
# Folded here as part of the F9 / P4 constants-extraction pass that
# consolidates the previously-duplicated ``generic`` set inside each of
# the two helpers below, plus the ``generic_labels`` literal that lived
# inline in ``app/recording_extension.extend_active_rtsp_recording``.
GENERIC_TRIGGER_LABELS: frozenset[str] = frozenset({
    '', 'motion', 'alert', 'human', 'object', 'none', 'off', 'continuous',
})


def update_live_detection_status(camera_id: str, **updates: Any) -> None:
    # Keep a best-confidence map alongside the raw detections. The live page
    # normally receives confidence through ``detections``, but some status
    # transitions carry only ``detected_labels``; preserving this map lets the
    # Vision lane render percentages using the same confidence source as the
    # Hearing lane instead of silently dropping the score.
    if 'detections' in updates:
        best_confidences: dict[str, float] = {}
        for detection in updates.get('detections') or []:
            if not isinstance(detection, dict):
                continue
            label = str(detection.get('label') or '').strip().lower()
            if not label:
                continue
            try:
                confidence = float(detection.get('confidence'))
            except (TypeError, ValueError):
                continue
            if label not in best_confidences or confidence > best_confidences[label]:
                best_confidences[label] = confidence
        updates['detection_confidences'] = best_confidences
    with _state.live_detection_status_lock:
        _state.live_detection_status[camera_id] = {
            **_state.live_detection_status.get(camera_id, {}),
            **updates,
            'camera_id': camera_id,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }


def detection_label_strings(detections: list[dict[str, Any]]) -> list[str]:
    """Return the sorted, de-duplicated, non-generic labels from a detections list.

    Used to seed the recording_labels join table so a recording's "labels" field
    reflects every object that appeared inside it, not just the trigger_label.
    """
    if not detections:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for detection in detections:
        label = str(detection.get('label') or '').strip().lower()
        if not label or label in GENERIC_TRIGGER_LABELS or label in seen:
            continue
        seen.add(label)
        out.append(label)
    out.sort()
    return out


def detection_label_confidences(detections: list[dict[str, Any]]) -> dict[str, float]:
    """Return the best confidence per non-generic label from a detections list.

    Used to persist a confidence alongside each recording label so the recordings
    list and timeline can show a percentage for secondary objects, not just the
    trigger object.
    """
    if not detections:
        return {}
    best: dict[str, float] = {}
    for detection in detections:
        label = str(detection.get('label') or '').strip().lower()
        if not label or label in GENERIC_TRIGGER_LABELS:
            continue
        try:
            confidence = float(detection.get('confidence'))
        except (TypeError, ValueError):
            continue
        if label not in best or confidence > best[label]:
            best[label] = confidence
    return best


def live_detection_status_payload(camera_id: str | None = None) -> dict[str, Any]:
    from app.ai_settings import ai_status_payload
    selected_config = get_camera_config(camera_id)
    camera_key = str(selected_config.get('id') or camera_id or 'camera')
    ai_state = ai_status_payload()
    with _state.live_detection_status_lock:
        status = _state.live_detection_status.get(
            camera_key,
            {'state': 'waiting', 'reason': 'No live detection has run yet.'},
        )
    return {
        'camera_id': camera_key,
        'camera_name': selected_config.get('name'),
        'ai_backend': ai_state['active_backend'],
        'ai_configured_backend': ai_state['configured_backend'],
        'ai_available': ai_state['inference_available'],
        'ai_mode': ai_state['mode'],
        'ai_error': ai_state['error'],
        **status,
    }


def _camera_has_live_alert_stream(settings: dict[str, Any]) -> bool:
    from app.utils import build_stream_url
    return bool(build_stream_url(settings))
