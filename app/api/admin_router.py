"""Admin APIRouter.

Direct imports replace the ``import app.main as main`` hybrid pattern.
"""

from __future__ import annotations

import asyncio

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


@router.delete('/api/system/runtime-data')
def delete_runtime_data(request: Request, db=Depends(get_database)):
    require_admin(request)
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


@router.post('/api/admin/migrations/normalize-recording-timestamps')
def normalize_recording_timestamps(request: Request, db=Depends(get_database)):
    """One-shot admin migration: re-encode every datetime column on
    every row of ``recordings`` / ``events`` / ``camera_diagnostics``
    to canonical UTC ``+00:00`` form so SQLite's lexical compares for
    retention / timeline / list filters / camera-log purge all land
    correctly on historical data.

    The endpoint URL kept the original ``normalize-recording-timestamps``
    name for backwards compatibility with the Settings → Database
    button + the audit-log resource key, but the underlying walk is
    now three tables. The response is a nested counts dict keyed by
    table name (``recordings``, ``events``, ``camera_diagnostics``),
    each carrying ``rows_scanned`` / ``rows_changed`` / `created_at``
    (plus ``started_at`` / ``ended_at`` under ``recordings``) /
    ``errors``.

    Idempotent: a re-run on already-canonical data returns
    ``rows_changed == 0`` for every table and issues no UPDATEs.
    Malformed timestamps are counted under each table's ``errors``
    and skipped so a single bad row doesn't abort the whole pass.
    Admin-only.
    """
    require_admin(request)
    counts = db.migrate_recording_timestamps_to_utc()
    write_audit_log(request, db, 'migrate', 'recording_timestamps', details=counts)
    return {'ok': True, 'counts': counts}
