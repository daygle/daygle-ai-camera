"""Camera-log APIRouter.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth_gates import require_admin
from app.deps import get_database
from app.request_helpers import write_audit_log
from app.utils import local_day_bounds_to_utc

router = APIRouter()
_DATE_FORMAT = '%Y-%m-%d'


def _parse_log_date(raw: str | None, field_name: str) -> date | None:
    if not raw:
        return None
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
        raise HTTPException(status_code=400, detail=f'{field_name} must be YYYY-MM-DD.')
    try:
        return datetime.strptime(raw, _DATE_FORMAT).date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f'{field_name} must be a valid date.') from exc


def _validate_date_range(date_from: str | None, date_to: str | None) -> tuple[str | None, str | None]:
    parsed_from = _parse_log_date(date_from, 'date_from')
    parsed_to = _parse_log_date(date_to, 'date_to')
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise HTTPException(status_code=400, detail='date_from must not be after date_to.')
    return (
        parsed_from.isoformat() if parsed_from else None,
        parsed_to.isoformat() if parsed_to else None,
    )


@router.get('/api/camera-log')
def list_camera_log(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    camera_id: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    tz_offset_minutes: int | None = Query(
        None, ge=-840, le=840,
        description="Browser Date.getTimezoneOffset(); resolves date_from/date_to in the viewer's local day. Omit for UTC days.",
    ),
    db=Depends(get_database),
):
    require_admin(request)
    date_from, date_to = _validate_date_range(date_from, date_to)
    # Resolve the requested calendar days in the viewer's timezone so the window
    # matches what they selected (mirrors /api/recordings/timeline).
    start, end = local_day_bounds_to_utc(date_from, date_to, tz_offset_minutes)
    created_after = start.isoformat() if start else None
    created_before = end.isoformat() if end else None
    entries = db.list_camera_diagnostics(
        limit=limit,
        offset=offset,
        camera_id=camera_id or None,
        event_type=event_type or None,
        severity=severity or None,
        created_after=created_after,
        created_before=created_before,
    )
    total = db.count_camera_diagnostics(
        camera_id=camera_id or None,
        event_type=event_type or None,
        severity=severity or None,
        created_after=created_after,
        created_before=created_before,
    )
    return {'entries': entries, 'total': total, 'limit': limit, 'offset': offset}


@router.delete('/api/camera-log')
def clear_camera_log(request: Request, db=Depends(get_database)):
    require_admin(request)
    deleted = db.delete_all_camera_diagnostics()
    write_audit_log(request, db, 'delete_all', 'camera_log', details={'count': deleted})
    return {'ok': True, 'deleted': deleted}
