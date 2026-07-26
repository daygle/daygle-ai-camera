"""Events APIRouter.

Direct imports replace the ``import app.main as main`` hybrid pattern.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth_gates import require_admin
from app.deps import get_database
from app.request_helpers import write_audit_log

router = APIRouter()


@router.get('/api/events')
def events(
    label: str | None = None,
    limit: int = Query(10000, ge=1, le=10000),
    alerted_only: bool = False,
    with_recording: bool = False,
    since: str | None = Query(None),
    db=Depends(get_database),
):
    return db.search_events(label=label, limit=limit, alerted_only=alerted_only, with_recording=with_recording, since=since)


@router.get('/api/events/{event_id}')
def event_detail(event_id: int, db=Depends(get_database)):
    event = db.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail='Event not found')
    return event


@router.delete('/api/events/{event_id}')
def delete_event(event_id: int, request: Request, db=Depends(get_database)):
    require_admin(request)
    event = db.delete_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail='Event not found')
    snapshot_path = event.get('snapshot_path')
    if snapshot_path:
        snapshot = Path(snapshot_path)
        if snapshot.exists() and snapshot.is_file():
            snapshot.unlink(missing_ok=True)
    write_audit_log(request, db, 'delete', 'event', event_id)
    return {'ok': True}


@router.delete('/api/events')
def delete_all_events(request: Request, db=Depends(get_database)):
    require_admin(request)
    deleted = db.delete_all_events()
    write_audit_log(request, db, 'delete_all', 'events', details={'count': deleted})
    return {'ok': True, 'deleted': deleted}


@router.post('/api/events/dismiss-all')
def dismiss_all_events_route(request: Request, db=Depends(get_database)):
    require_admin(request)
    dismissed = db.dismiss_all_events()
    return {'ok': True, 'dismissed': dismissed}


@router.post('/api/events/{event_id}/dismiss')
def dismiss_event_route(event_id: int, request: Request, db=Depends(get_database)):
    require_admin(request)
    ok = db.dismiss_event(event_id)
    if not ok:
        raise HTTPException(status_code=404, detail='Event not found')
    return {'ok': True}
