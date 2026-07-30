"""Camera Offline Alert Settings APIRouter.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import utc_now
from app.auth_gates import require_admin
from app.camera_health import effective_camera_offline_alert_settings
from app.deps import get_database

router = APIRouter()


@router.get('/api/settings/camera-offline')
def get_camera_offline_alert_settings():
    return effective_camera_offline_alert_settings()


@router.put('/api/settings/camera-offline')
async def update_camera_offline_alert_settings(request: Request, db=Depends(get_database)):
    require_admin(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='Invalid settings payload')
    validated = {'enabled': bool(payload.get('enabled', False))}
    try:
        validated['offline_delay_minutes'] = max(1, int(payload.get('offline_delay_minutes', 1)))
    except (TypeError, ValueError):
        validated['offline_delay_minutes'] = 1
    validated['recipients'] = [
        r for r in (payload.get('recipients') or [])
        if isinstance(r, str) and '@' in r
    ]
    result = db.set_setting('camera_offline_alert', validated, utc_now())
    return result
