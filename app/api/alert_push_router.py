"""Push Notification Settings APIRouter.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.alert_recipients import resolve_push_topics
from app.auth import utc_now
from app.auth_gates import require_admin
from app.deps import get_database, get_redacted_push_notification_settings
from app.payload_validators import validate_push_notification_settings
from app.push_notifications import PushNotificationError, PushNotificationService
from app.request_helpers import admin_json_object, read_json_object, write_audit_log
from app.utils import normalize_push_recipients

router = APIRouter()


@router.get('/api/settings/alert-push')
def get_push_notification_settings(request: Request, settings=Depends(get_redacted_push_notification_settings)):
    # The dep strips ``password`` for non-admin callers (see
    # ``app.deps.get_redacted_push_notification_settings``). Admin
    # still receives the full dict so the ntfy credentials round-trip
    # through PUT.
    #
    # ``user_topic`` is the CALLER's own ntfy topic (set on their profile):
    # pushes from alerts that name this user go there, while ``topic`` keeps
    # the pushes for alerts that name nobody. The Android app subscribes to
    # both. It is per-caller and read-only here (PUT ignores it).
    user = getattr(request.state, 'user', None) or {}
    return {**settings, 'user_topic': str(user.get('ntfy_topic') or '')}


@router.put('/api/settings/alert-push')
def update_push_notification_settings(request: Request, db=Depends(get_database), payload: dict[str, Any] = Depends(admin_json_object)):
    require_admin(request)
    settings = validate_push_notification_settings(payload)
    result = db.set_setting('alert_push', settings, utc_now())
    write_audit_log(request, db, 'update', 'settings.alert_push')
    return result


@router.post('/api/settings/alert-push/test')
async def test_push_notification_settings(request: Request):
    require_admin(request)
    payload = await read_json_object(request)
    settings = validate_push_notification_settings(
        payload.get('settings') if isinstance(payload.get('settings'), dict) else payload
    )
    # Users picked in Settings (``user:<id>``) get the test on their own
    # topics; nobody picked (or an older client sending no list) means the
    # shared topic, as before.
    configured = normalize_push_recipients(payload.get('recipients') or [])
    topics = await run_in_threadpool(resolve_push_topics, configured) if configured else []
    if configured and not topics:
        raise HTTPException(
            status_code=400, detail='None of the chosen users has an active push topic.'
        )
    try:
        await run_in_threadpool(PushNotificationService(settings).send_test, topics)
    except PushNotificationError as exc:
        raise HTTPException(
            status_code=400, detail=f'Test notification failed: {exc}'
        ) from exc
    return {'ok': True, 'topics': topics or [settings.get('topic')]}
