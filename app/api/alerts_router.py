"""Alerts APIRouter.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.auth_gates import require_admin
from app.deps import get_database
from app.request_helpers import write_audit_log

router = APIRouter()


@router.get('/api/alerts')
def alert_history(limit: int = Query(25, ge=1, le=10000), since: str | None = Query(None), db=Depends(get_database)):
    return db.alerts(limit=limit, since=since)


@router.delete('/api/alerts')
def delete_all_alert_history(request: Request, db=Depends(get_database)):
    require_admin(request)
    deleted = db.delete_all_alerts()
    write_audit_log(request, db, 'delete_all', 'alert_history', details={'count': deleted})
    return {'ok': True, 'deleted': deleted}


@router.post('/api/alerts/dismiss-all')
def dismiss_all_alerts_route(request: Request, db=Depends(get_database)):
    require_admin(request)
    dismissed = db.dismiss_all_alerts()
    return {'ok': True, 'dismissed': dismissed}


@router.post('/api/alerts/{group_key}/dismiss')
def dismiss_alert_group_route(group_key: str, request: Request, db=Depends(get_database)):
    require_admin(request)
    dismissed = db.dismiss_alert_group(group_key)
    return {'ok': True, 'dismissed': dismissed}
