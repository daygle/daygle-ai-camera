"""Settings / Objects APIRouter.

Per-label still/moving object-detection behavior (the Objects page). Stores a
small ``{default_mode, labels, group_modes, still_alerts}`` dict in the
``objects`` database setting and exposes the available model labels so the
page can render one row per class.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.ai_settings import detector_status
from app.auth import utc_now
from app.auth_gates import require_admin
from app.config_facades import effective_ai_config
from app.deps import get_database
from app.object_settings import (
    VALID_MODES,
    effective_object_settings,
    normalize_object_settings,
)
from app.request_helpers import write_audit_log

logger = logging.getLogger('daygle.ai')

router = APIRouter()


@router.get('/api/settings/objects')
def get_object_settings(request: Request):
    require_admin(request)
    settings = effective_object_settings()
    labels = detector_status(effective_ai_config()).get('available_labels') or []
    return {
        **settings,
        'available_labels': labels,
    }


@router.put('/api/settings/objects')
async def update_object_settings(request: Request, db=Depends(get_database)):
    require_admin(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='Payload must be a JSON object.')

    raw_default = payload.get('default_mode', 'moving')
    default_mode = str(raw_default or '').strip().lower()
    if default_mode not in VALID_MODES:
        raise HTTPException(
            status_code=400,
            detail="default_mode must be 'any', 'moving', or 'still'.",
        )

    raw_labels = payload.get('labels', {})
    if raw_labels is None:
        raw_labels = {}
    if not isinstance(raw_labels, dict):
        raise HTTPException(status_code=400, detail='labels must be an object mapping label to mode.')
    for label, raw_mode in raw_labels.items():
        mode = str(raw_mode or '').strip().lower()
        if mode not in VALID_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"labels['{label}'] must be 'any', 'moving', or 'still'.",
            )

    raw_group_modes = payload.get('group_modes', {})
    if raw_group_modes is None:
        raw_group_modes = {}
    if not isinstance(raw_group_modes, dict):
        raise HTTPException(status_code=400, detail='group_modes must be an object mapping a group name to a mode.')
    for group, raw_mode in raw_group_modes.items():
        mode = str(raw_mode or '').strip().lower()
        if mode not in VALID_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"group_modes['{group}'] must be 'any', 'moving', or 'still'.",
            )

    raw_still = payload.get('still_alerts', {})
    if raw_still is None:
        raw_still = {}
    if not isinstance(raw_still, dict):
        raise HTTPException(status_code=400, detail='still_alerts must be an object mapping label to minutes.')
    for label, raw_minutes in raw_still.items():
        try:
            minutes = float(raw_minutes)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"still_alerts['{label}'] must be a number of minutes (>= 1) or 0 to disable.",
            ) from None
        if minutes < 0:
            raise HTTPException(
                status_code=400,
                detail=f"still_alerts['{label}'] must be a number of minutes (>= 1) or 0 to disable.",
            )

    normalized = normalize_object_settings({
        'default_mode': default_mode,
        'labels': raw_labels,
        'group_modes': raw_group_modes,
        'still_alerts': raw_still,
    })
    db.set_setting('objects', normalized, utc_now())
    write_audit_log(request, db, 'update', 'settings.objects', details={
        'default_mode': normalized['default_mode'],
        'labels': sorted(normalized['labels']),
        'group_modes': normalized['group_modes'],
        'still_alerts': normalized['still_alerts'],
    })
    return {
        **normalized,
        'available_labels': detector_status(effective_ai_config()).get('available_labels') or [],
    }
