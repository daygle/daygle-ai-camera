"""Email Alert Settings APIRouter.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.alert_recipients import resolve_email_recipients
from app.auth import utc_now
from app.auth_gates import require_admin
from app.deps import get_database, get_redacted_email_alert_settings
from app.email_alerts import EmailAlertError, EmailAlertService
from app.payload_validators import validate_alert_email_settings
from app.request_helpers import admin_json_object, read_json_object, write_audit_log
from app.utils import normalize_email_recipients

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
def update_alert_email_settings(request: Request, db=Depends(get_database), payload: dict[str, Any] = Depends(admin_json_object)):
    require_admin(request)
    settings = validate_alert_email_settings(payload)
    result = db.set_setting('alert_email', settings, utc_now())
    write_audit_log(request, db, 'update', 'settings.alert_email')
    return result


@router.post('/api/settings/alert-email/test')
async def test_alert_email_settings(request: Request):
    require_admin(request)
    payload = await read_json_object(request)
    settings = validate_alert_email_settings(
        payload.get('settings') if isinstance(payload.get('settings'), dict) else payload
    )
    if isinstance(payload.get('recipients'), list):
        return await _send_test_to_recipients(settings, payload['recipients'])
    recipient = str(
        payload.get('recipient') or settings.get('from_address') or ''
    ).strip()
    if '@' not in recipient:
        raise HTTPException(
            status_code=400, detail='Test recipient must be a valid email address.'
        )
    try:
        await run_in_threadpool(EmailAlertService(settings).send_test, recipient)
    except EmailAlertError as exc:
        raise HTTPException(
            status_code=400, detail=f'Test email failed: {exc}'
        ) from exc
    return {'ok': True, 'recipient': recipient}


async def _send_test_to_recipients(settings: dict, raw: list) -> dict:
    """Send the test email to users picked in Settings (``user:<id>``,
    resolved to their current address) or literal addresses. Nobody picked
    means the From address, as for an empty single ``recipient``."""
    configured = normalize_email_recipients(raw)
    if configured:
        recipients = await run_in_threadpool(resolve_email_recipients, configured)
        if not recipients:
            raise HTTPException(
                status_code=400,
                detail='None of the chosen users has an active email address.',
            )
    else:
        from_address = str(settings.get('from_address') or '').strip()
        if '@' not in from_address:
            raise HTTPException(
                status_code=400, detail='Choose a recipient or set a From address first.'
            )
        recipients = [from_address]
    mailer = EmailAlertService(settings)
    errors: list[str] = []
    for recipient in recipients:
        try:
            await run_in_threadpool(mailer.send_test, recipient)
        except EmailAlertError as exc:
            errors.append(f'{recipient}: {exc}')
    if errors:
        raise HTTPException(status_code=400, detail=f"Test email failed: {'; '.join(errors)}")
    return {'ok': True, 'recipients': recipients}
