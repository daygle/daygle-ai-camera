"""Camera-log APIRouter.

Direct imports replace the ``import app.main as main`` hybrid pattern.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.auth_gates import require_admin
from app.deps import get_database
from app.request_helpers import write_audit_log

router = APIRouter()


@router.get('/api/camera-log')
def list_camera_log(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    camera_id: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    db=Depends(get_database),
):
    require_admin(request)
    entries = db.list_camera_diagnostics(
        limit=limit,
        offset=offset,
        camera_id=camera_id or None,
        event_type=event_type or None,
        severity=severity or None,
    )
    total = db.count_camera_diagnostics(
        camera_id=camera_id or None,
        event_type=event_type or None,
        severity=severity or None,
    )
    return {'entries': entries, 'total': total, 'limit': limit, 'offset': offset}


@router.delete('/api/camera-log')
def clear_camera_log(request: Request, db=Depends(get_database)):
    require_admin(request)
    deleted = db.delete_all_camera_diagnostics()
    write_audit_log(request, db, 'delete_all', 'camera_log', details={'count': deleted})
    return {'ok': True, 'deleted': deleted}
