"""Cameras APIRouter.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.auth import utc_now
from app.auth_gates import require_admin
from app.camera_config import _migrate_camera_id, _redact_camera, normalize_camera_id
from app.config_facades import effective_cameras_config, get_camera_config
from app.utils import build_stream_url
from app.state import _camera_health_lock, _camera_health_state
import app.state as _state

logger = logging.getLogger('daygle.ai')
from app.deps import (
    get_apply_cameras_settings,
    get_database,
    get_recording_service,
)
from app.payload_validators import validate_camera_settings, validate_cameras_settings
from app.ptz import send_ptz_command, VALID_COMMANDS as PTZ_VALID_COMMANDS
from app.request_helpers import write_audit_log

router = APIRouter()


@router.get('/api/cameras')
def list_cameras():
    return {'cameras': [_redact_camera(c) for c in effective_cameras_config()]}


@router.get('/api/cameras/health')
def cameras_health():
    with _camera_health_lock:
        states = dict(_camera_health_state)
    online_count = 0
    offline_count = 0
    result = {}
    for cam_id, state in states.items():
        online = state.get('online', True)
        result[cam_id] = {'online': online}
        if online:
            online_count += 1
        else:
            offline_count += 1
    return {
        'cameras': result,
        'summary': {
            'online': online_count,
            'offline': offline_count,
            'total': online_count + offline_count,
        },
    }


@router.put('/api/cameras')
async def update_cameras(
    request: Request,
    db=Depends(get_database),
    apply_cameras_settings=Depends(get_apply_cameras_settings),
):
    require_admin(request)
    settings = validate_cameras_settings(await request.json())
    old_configs = list(effective_cameras_config())
    for old, new in zip(old_configs, settings):
        if old.get('id') and new.get('id') and old['id'] != new['id']:
            # Stop the OLD camera's ingest workers BEFORE renaming its on-disk
            # dirs. The running ffmpeg writes via precomputed path strings and
            # never re-mkdirs mid-run, so renaming underneath it makes every
            # segment/frame/audio write fail ENOENT until the stall detector
            # kill-loops the worker -- destroying that camera's rolling
            # prebuffer right when an operator renames it.
            stop_workers = getattr(
                getattr(_state, 'recording_service', None),
                'stop_camera_workers',
                None,
            )
            if callable(stop_workers):
                try:
                    stop_workers(str(old['id']))
                except Exception as exc:  # sentinel / mid-swap: rename is still safe
                    logger.debug('Could not stop workers before camera id rename: %s', exc)
            _migrate_camera_id(old['id'], new['id'])
    db.set_setting('cameras', settings, utc_now())
    apply_cameras_settings(settings)
    write_audit_log(request, db, 'update', 'settings.cameras', details={'count': len(settings)})
    return {'cameras': [_redact_camera(c) for c in settings]}


@router.put('/api/cameras/{camera_id}')
async def update_camera(
    camera_id: str,
    request: Request,
    db=Depends(get_database),
    apply_cameras_settings=Depends(get_apply_cameras_settings),
):
    require_admin(request)
    normalized = normalize_camera_id(camera_id)
    payload = await request.json()
    settings_list = list(effective_cameras_config())
    for index, current in enumerate(settings_list):
        if current.get('id') == normalized:
            settings_list[index] = validate_camera_settings({**payload, 'id': normalized}, current=current, index=index + 1)
            db.set_setting('cameras', settings_list, utc_now())
            apply_cameras_settings(settings_list)
            write_audit_log(request, db, 'update', 'settings.camera', normalized, {'camera_name': settings_list[index].get('name')})
            return _redact_camera(settings_list[index])
    # Upsert: a PUT to an unknown id creates the camera
    created = validate_camera_settings({**payload, 'id': normalized}, index=len(settings_list) + 1)
    settings_list.append(created)
    db.set_setting('cameras', settings_list, utc_now())
    apply_cameras_settings(settings_list)
    write_audit_log(request, db, 'create', 'settings.camera', normalized, {'camera_name': created.get('name')})
    return _redact_camera(created)


@router.post('/api/cameras/test-connection')
async def test_camera_connection(request: Request, recording_service=Depends(get_recording_service)):
    require_admin(request)
    payload = await request.json()
    stream_url = build_stream_url(payload)
    if not stream_url:
        raise HTTPException(status_code=400, detail='Provide a stream_url or host to test.')
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        raise HTTPException(status_code=503, detail='ffprobe is not installed - cannot test connection.')
    command = [ffprobe, '-v', 'quiet', '-rtsp_transport', 'tcp', '-i', stream_url,
               '-show_entries', 'stream=codec_type', '-of', 'json']
    try:
        result = await run_in_threadpool(
            subprocess.run, command, capture_output=True, timeout=8, check=False
        )
        if result.returncode == 0:
            return {'online': True, 'message': 'Stream is reachable.'}
        stderr = recording_service.redact_stream_credentials(result.stderr.decode('utf-8', errors='replace').strip())
        return {'online': False, 'message': f'Stream unreachable: {stderr[:300]}' if stderr else 'Stream unreachable.'}
    except subprocess.TimeoutExpired:
        return {'online': False, 'message': 'Connection timed out (8 s). Check host, port, and credentials.'}


@router.post('/api/cameras/{camera_id}/ptz')
async def camera_ptz(camera_id: str, request: Request):
    require_admin(request)
    payload = await request.json()
    command = str(payload.get('command', '')).strip().lower()
    if command not in PTZ_VALID_COMMANDS:
        raise HTTPException(status_code=400, detail=f'Invalid PTZ command. Valid: {sorted(PTZ_VALID_COMMANDS)}')

    cam = get_camera_config(camera_id)
    ptz = cam.get('ptz') or {}
    if not ptz.get('enabled'):
        raise HTTPException(status_code=400, detail='PTZ is not enabled for this camera.')

    host = cam.get('host') or ''
    if not host and cam.get('stream_url'):
        host = urlsplit(cam['stream_url']).hostname or ''
    if not host:
        raise HTTPException(status_code=400, detail='Cannot determine camera host for PTZ.')

    protocol = str(ptz.get('protocol') or 'http_cgi')
    http_port = int(ptz.get('http_port') or 80)
    tcp_port = int(ptz.get('port') or 6060)
    address = int(ptz.get('address') or 1)
    speed = int(ptz.get('speed') or 5)
    # Per-camera step duration: every ContinuousMove SOAP body carries an
    # ``<Timeout>`` value of ``step_duration`` seconds, so the camera
    # self-stops after that interval even if the explicit /api/.../ptz
    # ``stop`` call is dropped. Defaults to 0.4s when missing or invalid;
    # the normalizer is the canonical clamp site (0.1-5.0s).
    try:
        step_duration = float(ptz.get('step_duration') or 0.4)
    except (TypeError, ValueError):
        step_duration = 0.4
    username = str(cam.get('username') or '')
    password = str(cam.get('password') or '')

    try:
        await run_in_threadpool(
            send_ptz_command, host, command, speed, protocol,
            http_port=http_port, tcp_port=tcp_port, address=address,
            username=username, password=password,
            timeout_seconds=step_duration,
        )
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f'PTZ connection failed: {exc}') from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {'ok': True, 'command': command}
