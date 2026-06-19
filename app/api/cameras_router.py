"""Cameras APIRouter.

Extracted from ``app/main.py`` (Phase 4 of the hybrid-pattern router split).
Same template as ``app/api/recordings_router.py``: ``import app.main as main``
at module level, every global / helper read through ``main.<name>`` *inside*
handler bodies.

Handlers moved (6):

- GET    /api/cameras
- GET    /api/cameras/health
- PUT    /api/cameras
- PUT    /api/cameras/{camera_id}
- POST   /api/cameras/test-connection
- POST   /api/cameras/{camera_id}/ptz

The splice was AST-driven, FunctionDef-by-FunctionDef, so the small in-block
helpers right next to these handlers stayed on ``app.main``:

- ``main._redact_camera`` (L4953-4956) - used by ``list_cameras``,
  ``update_cameras``, ``update_camera``.
- ``main._migrate_camera_id`` - used by ``update_cameras`` when an old
  camera id is being renamed and its recordings / events need to move.
- ``main._camera_health_state`` (L359 module-level dict) and
  ``main._camera_health_lock`` (L360 module-level ``threading.Lock``) -
  used by ``cameras_health`` to read the per-camera online state and the
  safe shared lock that ``_update_camera_health`` writes under.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.apply_cameras_settings``, ``main.build_stream_url``,
  ``main.get_camera_config``, ``main.normalize_camera_id``,
  ``main.validate_cameras_settings``, ``main.validate_camera_settings``,
  ``main._migrate_camera_id``, ``main._redact_camera`` - camera
  resolution / configuration helpers used by both the HTTP handlers and
  the live monitor loop.
- ``main.PTZ_VALID_COMMANDS`` (re-exported from ``app.ptz`` at the top
  of ``app.main``), ``main.send_ptz_command`` - PTZ implementation.
  Re-importing them here would not be honest since the PTZ module owns
  the canonical definitions; we just call into the existing aliases.
- ``main.recording_service`` - ``RecordingService`` instance; we reach
  its ``redact_stream_credentials`` static method directly here so the
  test-connection error path can scrub credentials from a stderr dump.
- ``main._camera_health_state``, ``main._camera_health_lock`` -
  module-level state owned by the camera module; the router only reads.
- ``main.write_audit_log``, ``main.require_admin``, ``main.utc_now`` -
  shared with every other router.
- ``main.database``, ``main.cameras_config`` - shared mutable state.
  ``update_cameras`` mutates ``main.cameras_config`` in-place rather than
  rebinding it; this matches what the live monitor loop already does and
  keeps ``main.cameras_config`` identity stable for any third-party
  reader that pinned to the original list object.

Tests do **not** exercise these endpoints' ``main.<helper>`` calls
directly (they go through ``LocalClient.request``), so no additional
``main.<attr>`` references were added to ``tests/test_api.py`` by this
extraction. The existing
``tests/test_api_router_split_invariants.py::test_all_main_attr_references_resolve_on_app_main``
invariant covers the broader hybrid-pattern contract, and the
``routes-coverage`` invariant auto-discovers this router via glob so
every ``main.<attr>`` lookup below is exercised on every test run.

See ``app/api/__init__.py`` for the full hybrid-pattern rules and the
route-coverage invariant that would have caught the e365ec5 over-deletion
regression.
"""

from __future__ import annotations

import shutil
import subprocess
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

import app.main as main

router = APIRouter()


@router.get('/api/cameras')
def list_cameras():
    return {'cameras': [main._redact_camera(c) for c in main.effective_cameras_config()]}


@router.get('/api/cameras/health')
def cameras_health():
    with main._camera_health_lock:
        states = dict(main._camera_health_state)
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
async def update_cameras(request: Request):
    main.require_admin(request)
    settings = main.validate_cameras_settings(await request.json())
    old_configs = list(main.cameras_config)
    for old, new in zip(old_configs, settings):
        if old.get('id') and new.get('id') and old['id'] != new['id']:
            main._migrate_camera_id(old['id'], new['id'])
    main.database.set_setting('cameras', settings, main.utc_now())
    main.apply_cameras_settings(settings)
    main.write_audit_log(request, 'update', 'settings.cameras', details={'count': len(settings)})
    return {'cameras': [main._redact_camera(c) for c in settings]}


@router.put('/api/cameras/{camera_id}')
async def update_camera(camera_id: str, request: Request):
    main.require_admin(request)
    normalized = main.normalize_camera_id(camera_id)
    payload = await request.json()
    settings_list = list(main.effective_cameras_config())
    for index, current in enumerate(settings_list):
        if current.get('id') == normalized:
            settings_list[index] = main.validate_camera_settings({**payload, 'id': normalized}, current=current, index=index + 1)
            main.database.set_setting('cameras', settings_list, main.utc_now())
            main.apply_cameras_settings(settings_list)
            main.write_audit_log(request, 'update', 'settings.camera', normalized, {'camera_name': settings_list[index].get('name')})
            return main._redact_camera(settings_list[index])
    # Upsert: a PUT to an unknown id creates the camera (there is no default
    # camera on a clean install anymore).
    created = main.validate_camera_settings({**payload, 'id': normalized}, index=len(settings_list) + 1)
    settings_list.append(created)
    main.database.set_setting('cameras', settings_list, main.utc_now())
    main.apply_cameras_settings(settings_list)
    main.write_audit_log(request, 'create', 'settings.camera', normalized, {'camera_name': created.get('name')})
    return main._redact_camera(created)


@router.post('/api/cameras/test-connection')
async def test_camera_connection(request: Request):
    main.require_admin(request)
    payload = await request.json()
    stream_url = main.build_stream_url(payload)
    if not stream_url:
        raise HTTPException(status_code=400, detail='Provide a stream_url or host to test.')
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        raise HTTPException(status_code=503, detail='ffprobe is not installed - cannot test connection.')
    command = [ffprobe, '-v', 'quiet', '-rtsp_transport', 'tcp', '-i', stream_url,
               '-show_entries', 'stream=codec_type', '-of', 'json']
    try:
        result = subprocess.run(command, capture_output=True, timeout=8, check=False)
        if result.returncode == 0:
            return {'online': True, 'message': 'Stream is reachable.'}
        stderr = main.recording_service.redact_stream_credentials(result.stderr.decode('utf-8', errors='replace').strip())
        return {'online': False, 'message': f'Stream unreachable: {stderr[:300]}' if stderr else 'Stream unreachable.'}
    except subprocess.TimeoutExpired:
        return {'online': False, 'message': 'Connection timed out (8 s). Check host, port, and credentials.'}


@router.post('/api/cameras/{camera_id}/ptz')
async def camera_ptz(camera_id: str, request: Request):
    main.require_admin(request)
    payload = await request.json()
    command = str(payload.get('command', '')).strip().lower()
    if command not in main.PTZ_VALID_COMMANDS:
        raise HTTPException(status_code=400, detail=f'Invalid PTZ command. Valid: {sorted(main.PTZ_VALID_COMMANDS)}')

    cam = main.get_camera_config(camera_id)
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
    username = str(cam.get('username') or '')
    password = str(cam.get('password') or '')

    try:
        await run_in_threadpool(
            main.send_ptz_command, host, command, speed, protocol,
            http_port=http_port, tcp_port=tcp_port, address=address,
            username=username, password=password,
        )
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f'PTZ connection failed: {exc}') from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {'ok': True, 'command': command}
