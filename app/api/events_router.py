"""Events APIRouter.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from app.auth_gates import require_admin, require_user
from app.deps import get_database
from app.request_helpers import write_audit_log
from app.media_utils import safe_storage_path
from app.live_snapshot import filter_object_priority_detections, render_live_snapshot_jpeg_overlay
from app.pagination import decode_cursor, encode_cursor

router = APIRouter()

# Gallery rows render the frame at 208 CSS px (2x for a retina display), so a
# ``?thumb=1`` request downscales to this width instead of pushing a
# full-resolution frame through the annotate/encode path for every row.
SNAPSHOT_THUMB_MAX_WIDTH = 416


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
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = Query(None),
    alerted_only: bool = False,
    with_recording: bool = False,
    since: str | None = Query(None),
    db=Depends(get_database),
):
    user = require_user(request)
    try:
        decoded = decode_cursor(cursor, 'events', 'newest') if cursor else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    owner_user_id = None if str(user.get('role') or '').lower() == 'admin' else int(user['id'])
    event_list, next_cursor = db.search_events_page(
        label=label,
        limit=limit,
        alerted_only=alerted_only,
        with_recording=with_recording,
        since=since,
        cursor=decoded,
        owner_user_id=owner_user_id,
    )
    scoped = [_scope_event_recordings(event, user) for event in event_list]
    return {
        'items': [event for event in scoped if event is not None],
        'next_cursor': (
            encode_cursor('events', 'newest', next_cursor[0], next_cursor[1])
            if next_cursor else None
        ),
    }


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


@router.get('/api/events/{event_id}/snapshot')
def event_snapshot(
    event_id: int,
    request: Request,
    boxes: bool = Query(True, description='Draw green detection boxes on the snapshot (as in alert emails).'),
    thumb: bool = Query(False, description='Downscale to a gallery thumbnail (416px wide) instead of full resolution.'),
    db=Depends(get_database),
):
    """Serve the event's saved snapshot, annotated with the same green
    detection boxes the alert emails use (via ``render_live_snapshot_jpeg_overlay``).

    Sound events and any event captured without a frame have no snapshot and
    return 404 - the client uses ``has_snapshot`` on the event payload to decide
    whether to offer the "open snapshot" action.
    """
    user = require_user(request)
    event = db.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail='Event not found')
    # Same visibility rule as event_detail: hide events whose linked recordings
    # are outside the viewer's scope (returns None -> 404, no existence leak).
    if _scope_event_recordings(event, user) is None:
        raise HTTPException(status_code=404, detail='Event not found')
    snapshot_path = safe_storage_path(event.get('snapshot_path'), roots=('snapshots_dir',))
    if snapshot_path is None or not snapshot_path.exists() or not snapshot_path.is_file():
        raise HTTPException(status_code=404, detail='Event snapshot not found')
    raw_bytes = snapshot_path.read_bytes()
    overlay_detections = filter_object_priority_detections([
        {
            'label': detection.get('label'),
            'confidence': detection.get('confidence'),
            'box': {
                'x': detection.get('x'),
                'y': detection.get('y'),
                'width': detection.get('width'),
                'height': detection.get('height'),
            },
            'motion_event': detection.get('motion_event', False),
        }
        for detection in (event.get('detections') or [])
    ]) if boxes else []
    image_bytes = render_live_snapshot_jpeg_overlay(
        raw_bytes,
        overlay_detections,
        max_width=SNAPSHOT_THUMB_MAX_WIDTH if thumb else None,
    )
    return Response(
        content=image_bytes,
        media_type='image/jpeg',
        headers={'Cache-Control': 'private, max-age=300'},
    )


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
