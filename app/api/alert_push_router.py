"""Push Notification Settings APIRouter.

Direct imports replace the ``import app.main as main`` hybrid pattern.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import utc_now
from app.auth_gates import require_admin
from app.config_facades import effective_push_notification_settings
from app.deps import get_database
from app.payload_validators import validate_push_notification_settings
from app.push_notifications import PushNotificationError, PushNotificationService
from app.request_helpers import write_audit_log

router = APIRouter()


@router.get('/api/settings/alert-push')
def get_push_notification_settings():
    return effective_push_notification_settings()


@router.put('/api/settings/alert-push')
async def update_push_notification_settings(request: Request, db=Depends(get_database)):
    require_admin(request)
    payload = await request.json()
    settings = validate_push_notification_settings(payload)
    result = db.set_setting('alert_push', settings, utc_now())
    write_audit_log(request, db, 'update', 'settings.alert_push')
    return result


@router.post('/api/settings/alert-push/test')
async def test_push_notification_settings(request: Request):
    payload = await request.json()
    settings = validate_push_notification_settings(
        payload.get('settings') if isinstance(payload.get('settings'), dict) else payload
    )
    try:
        PushNotificationService(settings).send_test()
    except PushNotificationError as exc:
        raise HTTPException(
            status_code=400, detail=f'Test notification failed: {exc}'
        ) from exc
    return {'ok': True}
