"""Snapshots APIRouter.

The Snapshots library (web/snapshots.html, under the Clips menu) lists every
event that captured a frame - ``events`` rows with a non-empty
``snapshot_path`` - and lets an admin remove a stored image. Deleting a
snapshot only clears the image file + path columns on the event; the event
row itself (and any linked recording) is untouched. That keeps the
destructive boundary distinct from ``DELETE /api/events/{id}``, which
removes the whole event.

Per-user recording scoping is shared with the events router so a viewer
never sees a snapshot whose linked recording belongs to someone else.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth_gates import require_admin, require_user
from app.deps import get_database
# Shared with the events router so the Snapshots library honours the exact
# same per-user recording scope (an event whose linked recording belongs to
# another user is hidden entirely). Kept on events_router as the canonical
# home; if it ever moves, update this import in lockstep.
from app.api.events_router import _scope_event_recordings
from app.media_utils import safe_storage_path
from app.request_helpers import write_audit_log

router = APIRouter()


@router.get('/api/snapshots')
def snapshots(
    request: Request,
    limit: int = Query(10000, ge=1, le=10000),
    since: str | None = Query(None),
    db=Depends(get_database),
):
    """List every event that saved a frame, newest first.

    Mirrors /api/events: viewers are limited to 10000 rows and every event
    passes through the same recording-scope filter (an event whose linked
    recording is outside the viewer's scope is hidden entirely).
    """
    user = require_user(request)
    fetch_limit = limit if str(user.get('role') or '').strip().lower() == 'admin' else 10000
    snapshot_list = db.list_snapshots(limit=fetch_limit, since=since)
    scoped = [_scope_event_recordings(event, user) for event in snapshot_list]
    return [event for event in scoped if event is not None][:limit]


@router.delete('/api/snapshots/{event_id}')
def delete_snapshot(event_id: int, request: Request, db=Depends(get_database)):
    """Delete the stored snapshot image for an event (admin only).

    Removes the image file(s) from disk and clears ``snapshot_path`` /
    ``thumbnail_path`` on the event, so the event stays visible but stops
    advertising ``has_snapshot``. The event itself and any linked recording
    are left intact - to remove those use DELETE /api/events/{event_id} or
    DELETE /api/recordings/{recording_id}.
    """
    require_admin(request)
    event = db.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail='Event not found')
    if not event.get('snapshot_path'):
        raise HTTPException(status_code=404, detail='Snapshot not found')
    for artifact_value in (event.get('snapshot_path'), event.get('thumbnail_path')):
        artifact = safe_storage_path(artifact_value, roots=('snapshots_dir',))
        if artifact is not None and artifact.exists() and artifact.is_file():
            artifact.unlink(missing_ok=True)
    db.clear_event_snapshot(event_id)
    write_audit_log(request, db, 'delete', 'snapshot', event_id)
    return {'ok': True}
