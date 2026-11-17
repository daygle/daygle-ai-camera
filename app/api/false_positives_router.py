"""False Positives APIRouter (app.false_positives).

Any signed-in user (admins and viewers) may mark an event or recording;
every mark is audit-logged and stored with who made it (username and role),
so the purge can leave viewers' marks alone (``purge_include_viewers``). The settings, stats and
purge stay admin-only. A body of ``{"false_positive": true}``
marks, ``false`` says the detection was real (the AI check then leaves it
alone), and ``null`` clears the decision.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth import utc_now
from app.auth_gates import require_admin, require_user
from app.deps import get_database
from app.false_positives import (
    SETTINGS_KEY,
    effective_false_positive_settings,
    false_positive_stats,
    purge_false_positives_by_policy,
    validate_false_positive_settings,
)
from app.request_helpers import DbRowId, admin_json_object, user_json_object, write_audit_log

router = APIRouter()


def _decision(payload: dict[str, Any]) -> tuple[bool | None, str | None]:
    if 'false_positive' not in payload:
        raise HTTPException(status_code=400, detail='false_positive is required (true, false or null).')
    value = payload['false_positive']
    if value is not None and not isinstance(value, bool):
        raise HTTPException(status_code=400, detail='false_positive must be true, false or null.')
    reason = payload.get('reason')
    if reason is not None and not isinstance(reason, str):
        raise HTTPException(status_code=400, detail='reason must be a string.')
    return value, reason


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    return {key: event.get(key) for key in (
        'id', 'false_positive', 'false_positive_by', 'false_positive_at', 'false_positive_reason',
        'false_positive_user', 'false_positive_role',
    )}


@router.get('/api/settings/false-positives')
def get_false_positive_settings(request: Request):
    require_admin(request)
    return effective_false_positive_settings()


@router.put('/api/settings/false-positives')
def update_false_positive_settings(request: Request, db=Depends(get_database),
                                   payload: dict[str, Any] = Depends(admin_json_object)):
    require_admin(request)
    settings = validate_false_positive_settings(payload)
    db.set_setting(SETTINGS_KEY, settings, utc_now())
    write_audit_log(request, db, 'update', 'settings.false_positives')
    return effective_false_positive_settings()


@router.get('/api/settings/false-positives/stats')
def get_false_positive_stats(request: Request, days: int = Query(7, ge=1, le=90)):
    require_admin(request)
    return false_positive_stats(days)


@router.post('/api/settings/false-positives/purge')
def purge_false_positives_now(request: Request, db=Depends(get_database)):
    """Purge false positives past the grace period now, even with purging off."""
    require_admin(request)
    result = purge_false_positives_by_policy(force=True)
    write_audit_log(request, db, 'purge', 'false_positives', details={
        'recordings': result['recordings_purged'], 'events': result['events_purged'],
    })
    return result


@router.post('/api/events/{event_id}/false-positive')
def mark_event_false_positive(event_id: DbRowId, request: Request, db=Depends(get_database),
                              payload: dict[str, Any] = Depends(user_json_object)):
    """Mark an event (or its snapshot) as a false positive, real, or undecided."""
    user = require_user(request)
    value, reason = _decision(payload)
    if not db.set_event_false_positive(event_id, value, by='user', at=utc_now(), reason=reason,
                                       user=user.get('username'), role=user.get('role')):
        raise HTTPException(status_code=404, detail='Event not found')
    write_audit_log(request, db, 'false_positive', 'event', event_id, details={'false_positive': value})
    return _event_payload(db.get_event(event_id) or {'id': event_id})


@router.post('/api/recordings/{recording_id}/false-positive')
def mark_recording_false_positive(recording_id: DbRowId, request: Request, db=Depends(get_database),
                                  payload: dict[str, Any] = Depends(user_json_object)):
    """Apply one decision to every event in a recording."""
    user = require_user(request)
    value, reason = _decision(payload)
    recording = db.get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail='Recording not found')
    changed = db.set_recording_false_positive(recording_id, value, by='user', at=utc_now(), reason=reason,
                                              user=user.get('username'), role=user.get('role'))
    if not changed:
        raise HTTPException(status_code=409, detail='This recording has no detections to mark.')
    write_audit_log(request, db, 'false_positive', 'recording', recording_id,
                    details={'false_positive': value, 'events': changed})
    recording = db.get_recording(recording_id) or {}
    return {'id': recording_id, 'events_updated': changed, 'false_positive': recording.get('false_positive')}
