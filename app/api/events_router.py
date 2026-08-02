"""Events APIRouter.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth_gates import require_admin, require_user
from app.deps import get_database
from app.request_helpers import write_audit_log
from app.media_utils import safe_storage_path

router = APIRouter()


def _scope_event_recordings(event: dict, user: dict) -> dict | None:
    """Hide recordings owned by another user from event payloads.

    An event that has recordings but no visible recording is hidden entirely;
    otherwise its metadata and detections would still disclose a private clip.
    Events without recordings remain visible because they are system events.
    """
    if str(user.get('role') or '').strip().lower() == 'admin':
        return event
    recordings = event.get('recordings') or []
    user_id = int(user.get('id') or 0)
    visible = [
        recording for recording in recordings
        if recording.get('owner_user_id') is None
        or int(recording.get('owner_user_id') or 0) == user_id
    ]
    # Event-level detections and metadata describe the whole event, so keeping
    # a mixed event would still disclose details about a recording the viewer
    # cannot access. Hide the complete event whenever any linked recording is
    # outside the viewer's scope.
    if len(visible) != len(recordings):
        return None
    event['recordings'] = visible
    event['recording_status'] = 'linked' if visible else 'none'
    return event


@router.get('/api/events')
def events(
    request: Request,
    label: str | None = None,
    limit: int = Query(10000, ge=1, le=10000),
    alerted_only: bool = False,
    with_recording: bool = False,
    since: str | None = Query(None),
    db=Depends(get_database),
):
    user = require_user(request)
    fetch_limit = limit if str(user.get('role') or '').strip().lower() == 'admin' else 10000
    events = db.search_events(label=label, limit=fetch_limit, alerted_only=alerted_only, with_recording=with_recording, since=since)
    scoped = [_scope_event_recordings(event, user) for event in events]
    return [event for event in scoped if event is not None][:limit]


@router.get('/api/events/{event_id}')
def event_detail(event_id: int, request: Request, db=Depends(get_database)):
    user = require_user(request)
    event = db.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail='Event not found')
    scoped = _scope_event_recordings(event, user)
    if scoped is None:
        raise HTTPException(status_code=404, detail='Event not found')
    return scoped


@router.delete('/api/events/{event_id}')
def delete_event(event_id: int, request: Request, db=Depends(get_database)):
    require_admin(request)
    event = db.delete_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail='Event not found')
    for artifact_value in (event.get('snapshot_path'), event.get('thumbnail_path')):
        artifact = safe_storage_path(artifact_value, roots=('snapshots_dir',))
        if artifact is not None and artifact.exists() and artifact.is_file():
            artifact.unlink(missing_ok=True)
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
