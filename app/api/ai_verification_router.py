"""AI Alert Verification Settings APIRouter."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from app.ai_verification import (
    SETTINGS_KEY,
    VerificationError,
    description_backfill_status,
    effective_ai_verification_settings,
    model_health,
    run_connection_test,
    start_description_backfill,
    validate_ai_verification_settings,
    verification_stats,
)
from app.ai_insights import apply_ignore_area, apply_min_confidence, insights, remove_ignore_area
from app.auth import utc_now
from app.auth_gates import require_admin
from app.deps import get_apply_cameras_settings, get_database
from app.request_helpers import admin_json_object, read_json_object, write_audit_log

router = APIRouter()


@router.get('/api/settings/ai-verification')
def get_ai_verification_settings(request: Request):
    # Admin-only: the payload carries the model server's API key.
    require_admin(request)
    return effective_ai_verification_settings()


@router.get('/api/settings/ai-verification/status')
def get_ai_verification_status(request: Request):
    """Whether the model server answered lately (Alerts page AI Verify warning)."""
    require_admin(request)
    settings = effective_ai_verification_settings()
    return {**model_health(settings), 'configured': bool(settings.get('server_url') and settings.get('model'))}


@router.get('/api/settings/ai-verification/stats')
def get_ai_verification_stats(request: Request, days: int = Query(7, ge=1, le=90)):
    """Verified / filtered / unverified counts and model health for the AI page."""
    require_admin(request)
    return verification_stats(days)


@router.put('/api/settings/ai-verification')
def update_ai_verification_settings(request: Request, db=Depends(get_database), payload: dict[str, Any] = Depends(admin_json_object)):
    require_admin(request)
    settings = validate_ai_verification_settings(payload)
    db.set_setting(SETTINGS_KEY, settings, utc_now())
    write_audit_log(request, db, 'update', 'settings.ai_verification')
    return effective_ai_verification_settings()


@router.post('/api/settings/ai-verification/test')
async def test_ai_verification_settings(request: Request):
    """Check the model server with the submitted (unsaved) settings.

    Verifies the most recent object event's snapshot (or ``event_id``) so the
    admin sees a real verdict and the latency before enabling the filter.
    """
    require_admin(request)
    payload = await read_json_object(request)
    raw_settings = payload.get('settings') if isinstance(payload.get('settings'), dict) else payload
    settings = validate_ai_verification_settings(
        {key: value for key, value in raw_settings.items() if key != 'event_id'}
    )
    event_id = payload.get('event_id')
    if event_id is not None and (isinstance(event_id, bool) or not isinstance(event_id, int)):
        raise HTTPException(status_code=400, detail='event_id must be an integer.')
    try:
        return await run_in_threadpool(run_connection_test, settings, event_id=event_id)
    except VerificationError as exc:
        raise HTTPException(status_code=400, detail=f'AI verification test failed: {exc}') from exc


@router.get('/api/settings/ai-verification/describe-backfill')
def get_description_backfill(request: Request):
    require_admin(request)
    return description_backfill_status()


@router.post('/api/settings/ai-verification/describe-backfill')
async def start_description_backfill_route(request: Request, db=Depends(get_database)):
    """Describe past events so they become searchable (runs in the background)."""
    require_admin(request)
    payload = await read_json_object(request)
    hours = payload.get('hours', 24)
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 24 * 30:
        raise HTTPException(status_code=400, detail='hours must be an integer between 1 and 720.')
    try:
        status = await run_in_threadpool(start_description_backfill, hours)
    except VerificationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit_log(request, db, 'start', 'settings.ai_verification.describe_backfill')
    return status


@router.get('/api/settings/ai-insights')
def get_ai_insights(request: Request):
    """Tuning suggestions and frequent AI tags for the AI page (app.ai_insights)."""
    require_admin(request)
    return insights()


@router.post('/api/settings/ai-insights/apply')
def apply_ai_insight(
    request: Request,
    db=Depends(get_database),
    apply_cameras_settings=Depends(get_apply_cameras_settings),
    payload: dict[str, Any] = Depends(admin_json_object),
):
    """Raise a camera's minimum confidence for one label to a suggested value."""
    require_admin(request)
    camera_id = payload.get('camera_id')
    label = payload.get('label')
    if not isinstance(camera_id, str) or not isinstance(label, str):
        raise HTTPException(status_code=400, detail='camera_id and label must be strings.')
    changed = apply_min_confidence(camera_id, label, payload.get('min_confidence'), apply_cameras_settings)
    write_audit_log(request, db, 'update', 'settings.cameras', details={
        'camera_id': camera_id, 'label': label, 'min_confidence': payload.get('min_confidence'),
        'rules': changed, 'via': 'ai_insights',
    })
    return {'rules_changed': changed}


@router.post('/api/settings/ai-insights/ignore-area')
def apply_ai_ignore_area(
    request: Request,
    db=Depends(get_database),
    apply_cameras_settings=Depends(get_apply_cameras_settings),
    payload: dict[str, Any] = Depends(admin_json_object),
):
    """Ignore one label in an area of a camera's zones (an AI Insights suggestion)."""
    require_admin(request)
    camera_id, label = payload.get('camera_id'), payload.get('label')
    if not isinstance(camera_id, str) or not isinstance(label, str):
        raise HTTPException(status_code=400, detail='camera_id and label must be strings.')
    changed = apply_ignore_area(camera_id, label, payload.get('area'), apply_cameras_settings)
    write_audit_log(request, db, 'update', 'settings.cameras', details={
        'camera_id': camera_id, 'label': label, 'area': payload.get('area'), 'zones': changed,
        'via': 'ai_insights.ignore_area',
    })
    return {'zones_changed': changed}


@router.post('/api/settings/ai-insights/ignore-area/remove')
def remove_ai_ignore_area(
    request: Request,
    db=Depends(get_database),
    apply_cameras_settings=Depends(get_apply_cameras_settings),
    payload: dict[str, Any] = Depends(admin_json_object),
):
    """Stop ignoring an area (by camera, zone and the area's position in the zone)."""
    require_admin(request)
    camera_id, zone_id = payload.get('camera_id'), payload.get('zone_id')
    if not isinstance(camera_id, str) or not isinstance(zone_id, str):
        raise HTTPException(status_code=400, detail='camera_id and zone_id must be strings.')
    remove_ignore_area(camera_id, zone_id, payload.get('index'), apply_cameras_settings)
    write_audit_log(request, db, 'update', 'settings.cameras', details={
        'camera_id': camera_id, 'zone_id': zone_id, 'index': payload.get('index'),
        'via': 'ai_insights.ignore_area_remove',
    })
    return {'ok': True}

