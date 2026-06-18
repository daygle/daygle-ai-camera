"""Sound detection APIRouter.

Uses the Option-3 hybrid pattern: ``import app.main as main`` at the top of this
file, then every global, lock, and test-referenced helper is read through
``main.<name>`` *inside* handler bodies. This preserves test back-compat
(``tests/test_api.py`` references ``main._sound_status_reason`` directly).

See ``app/api/__init__.py`` for the full hybrid-pattern rules; the short version
is: globals stay defined on ``app.main`` even if the router is their only caller.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

import app.main as main

router = APIRouter()


@router.get('/api/sound/classes')
def list_sound_classes() -> dict[str, Any]:
    return {
        'classes': [
            {
                'id': class_id,
                'label': meta['label'],
                'description': meta['description'],
                'default_threshold': meta['default_threshold'],
                'default_cooldown': meta['default_cooldown'],
            }
            for class_id, meta in main.SOUND_CLASSES.items()
        ]
    }


@router.get('/api/sound/status')
def get_sound_status(camera_id: str | None = Query(None)) -> dict[str, Any]:
    # LOCK-ORDER INVARIANT (set/detail and aggregate both share this):
    # _sound_statuses_lock is taken FIRST and released BEFORE _sound_detectors_lock
    # is taken, so the two acquisitions are *sequential*, never nested. Lock A
    # only protects a brief copy of the statuses dict into a local; Lock B is the
    # read-side lock for detectors. Consequence: a future edit must NEVER move
    # the second ``with`` block inside the first ``with`` block (no atomic
    # snapshot across both is required) and must NEVER introduce a third lock
    # that takes detectors while status still holds.
    # Lock A: _sound_statuses_lock (acquired first, released before Lock B).
    with main._sound_statuses_lock:
        if camera_id:
            status = dict(main._sound_statuses.get(
                camera_id,
                {'state': 'disabled', 'last_detected_at': None, 'last_confidence': 0.0, 'backend': None},
            ))
        else:
            statuses = dict(main._sound_statuses)

    if camera_id:
        # Don't hold Lock A while doing per-detector IO; release it before reading det.
        # The earlier `with main._sound_statuses_lock` block already exited.
        # Lock B: _sound_detectors_lock (sequential after Lock A, briefly held).
        with main._sound_detectors_lock:
            det = main._sound_detectors.get(camera_id)
        if det is not None:
            status['running'] = det.running
            status['detector_status'] = det.status
            status['backend'] = det.backend
            status['backend_reason'] = det.backend_reason
            status['last_confidences'] = {k: round(v, 3) for k, v in det.last_confidences().items()}
            diagnostics = det.diagnostics()
            status['diagnostics'] = diagnostics
            reason = main._sound_status_reason(diagnostics)
            if reason:
                status['reason'] = reason
        else:
            status['running'] = False
            status['detector_status'] = status.get('state', 'stopped')
            status['last_confidences'] = {}
        return status

    # Aggregate path: Lock A was released above; take Lock B now.
    # Lock B: _sound_detectors_lock (sequential after Lock A, briefly held).
    with main._sound_detectors_lock:
        detectors = list(main._sound_detectors.values())

    if not statuses:
        return {'state': 'disabled', 'running': False, 'detector_status': 'disabled', 'last_confidences': {}}
    most_recent: dict[str, Any] = {}
    most_recent_at: str | None = None
    for s in statuses.values():
        detected_at = s.get('last_detected_at')
        if detected_at and (most_recent_at is None or detected_at > most_recent_at):
            most_recent = s
            most_recent_at = detected_at
    if not most_recent:
        most_recent = next(iter(statuses.values()))
    result = dict(most_recent)
    running_detectors = [det for det in detectors if det.running]
    representative = running_detectors[0] if running_detectors else (detectors[0] if detectors else None)
    result['running'] = bool(running_detectors) or any(s.get('state') == 'listening' for s in statuses.values())
    result['detector_status'] = most_recent.get('state', 'stopped')
    if representative is not None:
        result['backend'] = representative.backend
        result['backend_reason'] = representative.backend_reason
    result['last_confidences'] = {}
    return result
