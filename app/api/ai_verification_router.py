"""AI Alert Verification Settings APIRouter."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.ai_verification import (
    SETTINGS_KEY,
    VerificationError,
    effective_ai_verification_settings,
    run_connection_test,
    validate_ai_verification_settings,
)
from app.auth import utc_now
from app.auth_gates import require_admin
from app.deps import get_database
from app.request_helpers import read_json_object, write_audit_log

router = APIRouter()


@router.get('/api/settings/ai-verification')
def get_ai_verification_settings(request: Request):
    # Admin-only: the payload carries the model server's API key.
    require_admin(request)
    return effective_ai_verification_settings()


@router.put('/api/settings/ai-verification')
async def update_ai_verification_settings(request: Request, db=Depends(get_database)):
    require_admin(request)
    payload = await read_json_object(request)
    settings = validate_ai_verification_settings(payload)
    db.set_setting(SETTINGS_KEY, settings, utc_now())
    write_audit_log(request, db, 'update', 'settings.ai_verification')
    return effective_ai_verification_settings()


@router.post('/api/settings/ai-verification/test')
async def test_ai_verification_settings(request: Request):
    """Check the model server with the submitted (unsaved) settings.

    Verifies the most recent object event's snapshot (or ``event_id``) so the
    admin sees a real verdict and the latency before enabling the filter.
    """
    require_admin(request)
    payload = await read_json_object(request)
    raw_settings = payload.get('settings') if isinstance(payload.get('settings'), dict) else payload
    settings = validate_ai_verification_settings(
        {key: value for key, value in raw_settings.items() if key != 'event_id'}
    )
    event_id = payload.get('event_id')
    if event_id is not None and (isinstance(event_id, bool) or not isinstance(event_id, int)):
        raise HTTPException(status_code=400, detail='event_id must be an integer.')
    try:
        return await run_in_threadpool(run_connection_test, settings, event_id=event_id)
    except VerificationError as exc:
        raise HTTPException(status_code=400, detail=f'AI verification test failed: {exc}') from exc
