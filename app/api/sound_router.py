"""Sound detection APIRouter.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

import app.state as _state
from app.auth_gates import require_admin
from app.sound_monitor import _sound_status_reason

router = APIRouter()


@router.get('/api/sound/classes')
def list_sound_classes() -> dict[str, Any]:
    from app.sound_detector import SOUND_CLASSES
    return {
        'classes': [
            {
                'id': class_id,
                'label': meta['label'],
                'description': meta['description'],
                'default_threshold': meta['default_threshold'],
                'default_cooldown': meta['default_cooldown'],
            }
            for class_id, meta in SOUND_CLASSES.items()
        ]
    }


@router.get('/api/sound/status')
def get_sound_status(camera_id: str | None = Query(None)) -> dict[str, Any]:
    # LOCK-ORDER INVARIANT: _sound_statuses_lock taken FIRST and released
    # BEFORE _sound_detectors_lock is taken (sequential, never nested).
    with _state._sound_statuses_lock:
        if camera_id:
            status = dict(_state._sound_statuses.get(
                camera_id,
                {'state': 'disabled', 'last_detected_at': None, 'last_confidence': 0.0, 'backend': None},
            ))
        else:
            statuses = dict(_state._sound_statuses)

    if camera_id:
        with _state._sound_detectors_lock:
            det = _state._sound_detectors.get(camera_id)
        if det is not None:
            status['running'] = det.running
            status['detector_status'] = det.status
            status['backend'] = det.backend
            status['backend_reason'] = det.backend_reason
            status['last_confidences'] = {k: round(v, 3) for k, v in det.last_confidences().items()}
            diagnostics = det.diagnostics()
            status['diagnostics'] = diagnostics
            reason = _sound_status_reason(diagnostics)
            if reason:
                status['reason'] = reason
        else:
            status['running'] = False
            status['detector_status'] = status.get('state', 'stopped')
            status['last_confidences'] = {}
        return status

    with _state._sound_detectors_lock:
        detectors = list(_state._sound_detectors.values())

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


@router.get('/api/sound/model/info')
def get_sound_model_info(request: Request):
    """Return information about the installed YAMNet model."""
    require_admin(request)
    from app.sound_detector import _yamnet
    return _yamnet.installed_info()


@router.post('/api/sound/model/check')
def check_sound_model_update(request: Request):
    """Check if a newer YAMNet model is available."""
    require_admin(request)
    from app.sound_detector import _yamnet
    return _yamnet.check_for_update()


@router.post('/api/sound/model/reload')
def reload_sound_model(request: Request):
    """Re-download and reload the YAMNet model."""
    require_admin(request)
    from app.sound_detector import _yamnet
    ok = _yamnet.reload()
    return {'ok': ok, 'info': _yamnet.installed_info()}
