"""Push Notification Settings APIRouter.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.auth import utc_now
from app.auth_gates import require_admin
from app.deps import get_database, get_redacted_push_notification_settings
from app.payload_validators import validate_push_notification_settings
from app.push_notifications import PushNotificationError, PushNotificationService
from app.request_helpers import write_audit_log

router = APIRouter()


@router.get('/api/settings/alert-push')
def get_push_notification_settings(settings=Depends(get_redacted_push_notification_settings)):
    # The dep strips ``password`` for non-admin callers (see
    # ``app.deps.get_redacted_push_notification_settings``). Admin
    # still receives the full dict so the ntfy credentials round-trip
    # through PUT.
    return settings


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
    require_admin(request)
    payload = await request.json()
    settings = validate_push_notification_settings(
        payload.get('settings') if isinstance(payload.get('settings'), dict) else payload
    )
    try:
        await run_in_threadpool(PushNotificationService(settings).send_test)
    except PushNotificationError as exc:
        raise HTTPException(
            status_code=400, detail=f'Test notification failed: {exc}'
        ) from exc
    return {'ok': True}
