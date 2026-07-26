"""Admin APIRouter.

Direct imports replace the ``import app.main as main`` hybrid pattern.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.ai_settings import ai_status_payload
from app.auth_gates import require_admin, require_session, require_user
from app.config_facades import (
    effective_ai_config,
    effective_auth_config,
    effective_cameras_config,
    effective_live_config,
    effective_recording_config,
    effective_storage_config,
    get_camera_config,
)
from app.deps import get_auth_enabled, get_database, get_detector
from app.detector import DetectorUnavailableError
from app.request_helpers import write_audit_log, _read_uploaded_image
from app.state import active_rtsp_recordings, active_rtsp_recordings_lock
from app.alert_dispatch import compute_minimum_rule_confidence
from app.recording_extension import clear_runtime_media_directory, delete_recording_files
import app.state as _state

router = APIRouter()


@router.get('/api/audit')
def list_audit_log(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    action: str | None = None,
    username: str | None = None,
    resource: str | None = None,
    db=Depends(get_database),
):
    require_admin(request)
    entries = db.list_audit_logs(
        limit=limit,
        offset=offset,
        action=action or None,
        username=username or None,
        resource=resource or None,
    )
    total = db.count_audit_logs(
        action=action or None,
        username=username or None,
        resource=resource or None,
    )
    return {'entries': entries, 'total': total, 'limit': limit, 'offset': offset}


@router.get('/api/auth/me')
def me(request: Request):
    session = require_session(request)
    return {'user': session['user'], 'csrf_token': session['csrf_token'], 'expires_at': session['expires_at']}


@router.get('/api/auth/session-remaining')
def session_remaining(request: Request):
    """Return the current session's remaining lifetime in seconds.

    The server-side sliding window (``_renew_session_if_stale``) extends
    ``expires_at`` on each authenticated request, so this endpoint always
    reflects the most up-to-date expiry. A lightweight GET (no CSRF needed)
    that the frontend can poll on a 10-30 s interval for the countdown.

    When auth is disabled ``require_session`` returns an anonymous session
    with an empty ``expires_at`` - we catch the parse error and return 0
    so the frontend doesn't see a 500.
    """
    session = require_session(request)
    try:
        remaining = max(0, int(
            datetime.fromisoformat(session['expires_at']).timestamp()
            - datetime.now(timezone.utc).timestamp()
        ))
    except (TypeError, ValueError):
        remaining = 0
    return {'remaining_seconds': remaining, 'expires_at': session['expires_at']}


@router.get('/api/config')
def runtime_config(request: Request, auth_enabled: bool = Depends(get_auth_enabled)):
    require_user(request)
    ai_state = ai_status_payload()
    ai_cfg = effective_ai_config()
    return {
        'server': {'host': _state.config.get('server', {}).get('host'), 'port': _state.config.get('server', {}).get('port')},
        'camera': get_camera_config(None),
        'cameras': effective_cameras_config(),
        'ai': {
            'enabled': ai_cfg.get('enabled'),
            'backend': ai_cfg.get('backend'),
            'confidence': ai_cfg.get('confidence'),
            'iou_threshold': ai_cfg.get('iou_threshold'),
            'input_size': ai_cfg.get('input_size'),
            'model_path': ai_cfg.get('model_path'),
            'labels_path': ai_cfg.get('labels_path'),
            'active_backend': ai_state['active_backend'],
            'mode': ai_state['mode'],
            'available': ai_state['inference_available'],
            'model_loaded': ai_state['model_loaded'],
            'error': ai_state['error'],
            'categories': ai_cfg.get('categories', []),
        },
        'alerts': _state.config.get('alerts', {}),
        'auth': {
            'enabled': auth_enabled,
            'session_timeout_hours': effective_auth_config().get('session_timeout_hours'),
            'max_login_attempts': effective_auth_config().get('max_login_attempts'),
            'lockout_minutes': effective_auth_config().get('lockout_minutes'),
        },
        'storage': {
            'database': effective_storage_config().get('database'),
            'snapshots_dir': effective_storage_config().get('snapshots_dir'),
            'recordings_dir': effective_storage_config().get('recordings_dir'),
        },
        'live': effective_live_config(),
        'recording': effective_recording_config(),
    }


@router.post('/api/detect/frame')
async def detect_frame(request: Request, detector=Depends(get_detector)):
    require_admin(request)
    image_bytes, _filename, _content_type = await _read_uploaded_image(request)
    if not image_bytes:
        raise HTTPException(status_code=400, detail='Uploaded image is empty')
    ai_settings = effective_ai_config()
    ai_state = ai_status_payload(ai_settings)
    ai_error: str | None = None
    min_confidence = compute_minimum_rule_confidence()

    def _run_detection() -> list:
        return detector.detect_image(image_bytes, confidence=min_confidence)

    try:
        detections = await asyncio.get_running_loop().run_in_executor(None, _run_detection)
    except DetectorUnavailableError as exc:
        detections = []
        ai_error = str(exc) or ai_state.get('last_detector_error') or ai_state.get('error') or 'Detector unavailable.'
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'detections': detections, 'count': len(detections), 'ai_backend': ai_state['active_backend'], 'ai_error': ai_error}


@router.post('/api/system/runtime-data/preview')
def preview_delete_runtime_data(request: Request, db=Depends(get_database)):
    """M2 fix preview half.

    Returns a JSON body listing how many rows would be deleted by the
    full wipe (recordings, events, alerts/detections, camera
    diagnostics) and emits a single-use ``confirm_token`` that must be
    echoed in the ``X-Runtime-Data-Confirm`` header of the next
    ``DELETE /api/system/runtime-data?confirm=true`` call within
    ``_RUNTIME_DELETE_TOKEN_TTL_SECONDS`` (30s by default).

    The preview query is non-destructive: SELECT COUNT(*) against the
    same five tables the wipe removes. The audit log records the
    preview request itself (``preview_delete_all:runtime_data``) so a
    denied intent is still forensic.
    """
    require_admin(request)
    user = getattr(request.state, 'user', None) or {}
    user_id = user.get('id') if isinstance(user, dict) else None
    counts: dict[str, int] = {}
    with db.connect() as conn:
        for source, sql in (
            ('recordings', 'SELECT COUNT(*) FROM recordings'),
            ('events', 'SELECT COUNT(*) FROM events'),
            ('alerts', 'SELECT COUNT(*) FROM alert_history'),
            ('objects', 'SELECT COUNT(*) FROM detections'),
            ('camera_diagnostics', 'SELECT COUNT(*) FROM camera_diagnostics'),
        ):
            row = conn.execute(sql).fetchone()
            counts[source] = int(row[0]) if row else 0
    token = _state.issue_runtime_delete_token(user_id)
    write_audit_log(
        request, db, 'preview_delete_all', 'runtime_data',
        details={**counts, 'confirm_token_issued': True},
    )
    return {
        'ok': True,
        'confirm_token': token,
        'expires_in': int(_state._RUNTIME_DELETE_TOKEN_TTL_SECONDS),
        'counts': counts,
        'preserved': ['settings', 'users', 'sessions', 'rules'],
        'warning': 'This preview shows counts only; the actual wipe still happens via DELETE /api/system/runtime-data?confirm=true.',
    }


@router.delete('/api/system/runtime-data')
def delete_runtime_data(
    request: Request,
    confirm: str | None = Query(None, description='Must be the literal string "true" to confirm the destructive wipe.'),
    db=Depends(get_database),
):
    """M2 fix wipe half.

    Requires:
    - admin session (via ``require_admin`` + middleware)
    - ``?confirm=true`` query param (belt-and-braces against a
      silent DELETE-with-no-opts being accepted as "confirmed")
    - a still-valid ``X-Runtime-Data-Confirm`` header matching a token
      the SAME admin received from the preview endpoint within
      ``_RUNTIME_DELETE_TOKEN_TTL_SECONDS``
    - middleware's CSRF gate (X-CSRF-Token header)

    On any of the above failing, the response is HTTP 400 with a
    short "what to do next" detail. No audit-log row is written for
    the rejected attempts - only attempted wipes that PASS the
    confirm gate go to the audit log, so a noisy fail-counter can't
    be correlated to a compromised admin by an attacker who can
    read the DB.
    """
    require_admin(request)
    if confirm != 'true':
        raise HTTPException(
            status_code=400,
            detail='Missing or wrong ?confirm=true. Send ?confirm=true to confirm the destructive wipe.',
        )
    presented_token = request.headers.get('X-Runtime-Data-Confirm')
    if not presented_token:
        raise HTTPException(
            status_code=400,
            detail='Missing X-Runtime-Data-Confirm header. POST /api/system/runtime-data/preview to receive a token first.',
        )
    user = getattr(request.state, 'user', None) or {}
    user_id = user.get('id') if isinstance(user, dict) else None
    err = _state.consume_runtime_delete_token(user_id, presented_token)
    if err is not None:
        raise HTTPException(status_code=400, detail=err)
    recordings = db.delete_all_recordings()
    delete_recording_files(recordings)
    deleted_events = db.delete_all_events()
    deleted_alerts = db.delete_all_alerts()
    deleted_objects = db.delete_all_objects()
    deleted_diagnostics = db.delete_all_camera_diagnostics()
    storage_config = effective_storage_config()
    deleted_snapshots = clear_runtime_media_directory(storage_config.get('snapshots_dir'))
    deleted_event_artifacts = clear_runtime_media_directory(storage_config.get('events_dir'))
    with active_rtsp_recordings_lock:
        active_rtsp_recordings.clear()
    result = {
        'ok': True,
        'deleted': {
            'recordings': len(recordings),
            'events': deleted_events,
            'alerts': deleted_alerts,
            'objects': deleted_objects,
            'camera_diagnostics': deleted_diagnostics,
            'snapshot_files': deleted_snapshots,
            'event_artifacts': deleted_event_artifacts,
        },
        'preserved': ['settings', 'users', 'sessions', 'rules'],
    }
    write_audit_log(request, db, 'delete_all', 'runtime_data', details=result['deleted'])
    return result


