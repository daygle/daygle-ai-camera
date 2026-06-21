"""Email Alert Settings APIRouter.

Direct imports replace the ``import app.main as main`` hybrid pattern.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import utc_now
from app.auth_gates import require_admin
from app.deps import get_database, get_redacted_email_alert_settings
from app.email_alerts import EmailAlertError, EmailAlertService
from app.payload_validators import validate_alert_email_settings
from app.request_helpers import write_audit_log

router = APIRouter()


@router.get('/api/settings/alert-email')
def get_alert_email_settings(settings=Depends(get_redacted_email_alert_settings)):
    # The dep strips ``password`` for non-admin callers (see
    # ``app.deps.get_redacted_email_alert_settings``) so viewer / future
    # non-admin roles can read the SMTP configuration without seeing
    # the credential. Admin still receives the full dict so the
    # settings page can round-trip the password back to PUT.
    return settings


@router.put('/api/settings/alert-email')
async def update_alert_email_settings(request: Request, db=Depends(get_database)):
    require_admin(request)
    payload = await request.json()
    settings = validate_alert_email_settings(payload)
    result = db.set_setting('alert_email', settings, utc_now())
    write_audit_log(request, db, 'update', 'settings.alert_email')
    return result


@router.post('/api/settings/alert-email/test')
async def test_alert_email_settings(request: Request):
    payload = await request.json()
    settings = validate_alert_email_settings(
        payload.get('settings') if isinstance(payload.get('settings'), dict) else payload
    )
    recipient = str(
        payload.get('recipient') or settings.get('from_address') or ''
    ).strip()
    if '@' not in recipient:
        raise HTTPException(
            status_code=400, detail='Test recipient must be a valid email address.'
        )
    try:
        EmailAlertService(settings).send_test(recipient)
    except EmailAlertError as exc:
        raise HTTPException(
            status_code=400, detail=f'Test email failed: {exc}'
        ) from exc
    return {'ok': True, 'recipient': recipient}
