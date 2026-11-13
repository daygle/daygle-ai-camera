"""Alert Delivery Outbox APIRouter.

Lists the deliveries the mail relay or ntfy server never accepted (see
``app.alert_outbox``) and lets an admin retry one now, or make every queued row
due so the background worker picks it up on its next pass. Admin-only: the
entries carry recipient addresses and topics.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app import alert_outbox
from app.auth_gates import require_admin
from app.deps import get_database
from app.request_helpers import write_audit_log

router = APIRouter()


@router.get('/api/alerts/outbox')
def list_alert_outbox(request: Request):
    require_admin(request)
    return {
        'entries': alert_outbox.public_entries(),
        'total': alert_outbox.pending_count(),
    }


@router.post('/api/alerts/outbox/{row_id}/retry')
async def retry_alert_outbox_entry(request: Request, row_id: int, db=Depends(get_database)):
    require_admin(request)
    # A delivery attempt can take a couple of seconds when the server is still
    # down, so keep it off the event loop.
    result = await run_in_threadpool(alert_outbox.retry_now, row_id)
    if result is None:
        raise HTTPException(status_code=404, detail='That queued delivery no longer exists.')
    write_audit_log(request, db, 'retry', 'alerts.outbox', str(row_id))
    return {'row_id': row_id, **result}


@router.post('/api/alerts/outbox/retry')
def retry_alert_outbox(request: Request, db=Depends(get_database)):
    require_admin(request)
    # Not attempted inline: with the server still down each row costs its
    # transports' retry budget. Making them due lets the worker drain them.
    queued = alert_outbox.retry_all_now()
    write_audit_log(request, db, 'retry', 'alerts.outbox', 'all')
    return {'queued': queued}
